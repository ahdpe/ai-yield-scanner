"""Журнал сделок, shadow-трекер кандидатов и аналитика expectancy для Live Smart Bonding.

Модуль работает строго в режиме пассивного наблюдения:
- НЕ меняет торговые решения, лимиты и исполнение ордеров;
- все хуки записи fail-safe (ошибка диска/JSON никогда не ломает торговый тик или Stop-Loss);
- append-only JSONL-журналы + атомарный кэш резолюций Gamma API.

Файлы в state/ (git-ignored):
- trade_journal.jsonl       — хронология реальных входов, выходов и закрытий со снимками стакана;
- shadow_candidates.jsonl   — все кандидаты воронки отбора (включая отклонённые фильтрами и ИИ);
- market_resolutions.json   — кэш фактических исходов рынков для counterfactual-анализа.

CLI (read-only отчёт без блокировки live_trader.lock):
    python3 trade_analytics.py --report
    python3 trade_analytics.py --resolve --report
    python3 trade_analytics.py --json
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from polymarket_scanner import estimate_taker_fee_usd, fetch_market_by_slug, parse_outcome_prices
from state_io import STATE_DIR, atomic_write_json


TRADE_JOURNAL_FILE = STATE_DIR / "trade_journal.jsonl"
SHADOW_CANDIDATES_FILE = STATE_DIR / "shadow_candidates.jsonl"
MARKET_RESOLUTIONS_FILE = STATE_DIR / "market_resolutions.json"
LIVE_STATE_FILE = STATE_DIR / "live_portfolio_state.json"

SHADOW_DEDUP_TTL_SEC = 24 * 3600.0
RESOLUTION_REFRESH_INTERVAL_SEC = 6 * 3600.0
RESOLUTION_PAST_DUE_RECHECK_SEC = 6 * 3600.0
RESOLUTION_EARLY_RECHECK_SEC = 24 * 3600.0
DEFAULT_MAX_RESOLUTIONS_PER_RUN = 15
DEFAULT_SHADOW_SLOT_USD = 50.0
MIN_SIGNIFICANT_SAMPLE = 50

PRICE_BAND_ORDER = (
    "<94.5¢",
    "94.5-96.5¢",
    "96.5-97.5¢",
    "97.5-98.0¢",
    "98.0-98.5¢",
    ">98.5¢",
    "unknown",
)

EXIT_CATEGORY_ORDER = (
    "resolution_win",
    "resolution_loss",
    "resolution_partial",
    "take_profit",
    "stop_loss",
    "ai_skip",
    "other_exit",
)

EXIT_LABELS_RU = {
    "resolution_win": "Резолюция WIN ($1.00)",
    "resolution_loss": "Резолюция LOSS ($0.00)",
    "resolution_partial": "Резолюция (частичная)",
    "take_profit": "Take-Profit",
    "stop_loss": "Stop-Loss",
    "ai_skip": "Выход по AI-SKIP",
    "other_exit": "Прочий выход",
}

FUNNEL_STAGE_PRIORITY = {
    "event_dedup": 10,
    "tier_max_days": 20,
    "long_horizon_cap": 25,
    "topic_cluster": 30,
    "macro_risk_bucket": 35,
    "fee_blocked": 40,
    "price_out_of_range": 50,
    "wide_spread": 60,
    "thin_ask_depth": 65,
    "thin_bid_depth": 70,
    "low_net_return": 75,
    "tier_min_apr": 80,
    "ai_unavailable": 85,
    "ai_skip": 90,
    "tier_min_confidence": 95,
    "entry_exec_skipped": 98,
    "entered": 100,
}

STAGE_LABELS_RU = {
    "ai_skip": "Отклонён ИИ (SKIP)",
    "tier_min_confidence": "ИИ APPROVE, но ниже порога лестницы",
    "ai_unavailable": "ИИ недоступен / лимит бюджета",
    "tier_min_apr": "APR ниже порога лестницы",
    "low_net_return": "Низкая чистая доходность (<0.3%)",
    "thin_bid_depth": "Тонкий стакан бидов (<$250)",
    "thin_ask_depth": "Мало объёма на best_ask (<слот)",
    "wide_spread": "Широкий спред стакана",
    "price_out_of_range": "Цена CLOB вне 94.5-98.5¢",
    "fee_blocked": "Комиссия не допущена / сбой /fee-rate",
    "topic_cluster": "Лимит кластера тем (2 слота)",
    "macro_risk_bucket": "Лимит макро-бакета корреляции",
    "long_horizon_cap": "Лимит длинных слотов (>21 дн.)",
    "tier_max_days": "Срок больше порога лестницы",
    "event_dedup": "Дедупликация по event_id",
    "entry_exec_skipped": "Одобрен ИИ, но вход сорвался",
    "entered": "Вошли в позицию",
}

# Кэш дедупликации shadow-кандидатов по пути файла: {path_str: {(slug, outcome, stage): last_ts}}
_shadow_dedup_cache: Dict[str, Dict[Tuple[str, str, str], float]] = {}


def reset_dedup_cache() -> None:
    """Сбрасывает in-memory кэш дедупликации shadow-кандидатов (для тестов)."""
    _shadow_dedup_cache.clear()


def _iso_utc(ts: Optional[float] = None) -> str:
    val = float(ts if ts is not None else time.time())
    return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_end_ts(end_date: Any) -> Optional[float]:
    clean = str(end_date or "").strip()
    if not clean:
        return None
    try:
        dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


def _round_opt(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def classify_price_band(price: Optional[float]) -> str:
    """Распределяет цену входа по аналитическим ценовым бэндам."""
    px = _safe_float(price)
    if px is None or px <= 0:
        return "unknown"
    if px < 0.945:
        return "<94.5¢"
    if px < 0.965:
        return "94.5-96.5¢"
    if px < 0.975:
        return "96.5-97.5¢"
    if px < 0.980:
        return "97.5-98.0¢"
    if px <= 0.985 + 1e-9:
        return "98.0-98.5¢"
    return ">98.5¢"


def classify_exit_category(pos: Dict[str, Any], payout: Optional[float] = None) -> str:
    """Классифицирует причину закрытия позиции в единую категорию аналитики."""
    status = str(pos.get("status") or "").upper()
    if status == "RESOLVED":
        px = _safe_float(payout if payout is not None else pos.get("current_price"))
        if px is not None:
            if px >= 0.99:
                return "resolution_win"
            if px <= 0.01:
                return "resolution_loss"
            return "resolution_partial"
        pnl = _safe_float(pos.get("realized_pnl_usd"), 0.0) or 0.0
        return "resolution_win" if pnl >= 0 else "resolution_loss"

    exit_kind = str(pos.get("exit_kind") or "").lower()
    reason = str(pos.get("close_reason") or "").lower()
    if exit_kind == "take_profit" or "take-profit" in reason or "take profit" in reason:
        return "take_profit"
    if "skip" in reason or "ии дважды" in reason or "реаудит" in reason:
        return "ai_skip"
    if "стоп" in reason or "stop" in reason or "пробой" in reason or "ниже стопа" in reason:
        return "stop_loss"
    return "other_exit"


def extract_book_snapshot(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Извлекает компактный снимок стакана CLOB (без хранения всех уровней bids/asks)."""
    if not isinstance(summary, dict):
        return None
    best_bid = _safe_float(summary.get("best_bid"))
    best_ask = _safe_float(summary.get("best_ask"))
    mid = None
    spread = None
    if best_bid is not None and best_ask is not None:
        mid = round((best_bid + best_ask) / 2.0, 4)
        spread = round(best_ask - best_bid, 4)
    elif best_bid is not None:
        mid = round(best_bid, 4)
    elif best_ask is not None:
        mid = round(best_ask, 4)
    return {
        "best_bid": _round_opt(best_bid, 4),
        "best_ask": _round_opt(best_ask, 4),
        "mid": mid,
        "spread": spread,
        "best_bid_size": _round_opt(_safe_float(summary.get("best_bid_size"), 0.0), 2),
        "best_ask_size": _round_opt(_safe_float(summary.get("best_ask_size"), 0.0), 2),
        "near_bid_depth_usd": _round_opt(_safe_float(summary.get("near_bid_depth_usd"), 0.0), 2),
        "tick_size": str(summary.get("tick_size") or "0.01"),
    }


def _compact_ai_audit(audit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Компактная выжимка вердикта ИИ для JSONL без избыточных полей."""
    if not isinstance(audit, dict) or not audit:
        return {}
    votes_out = []
    for v in (audit.get("votes") or []):
        if isinstance(v, dict):
            votes_out.append({
                "model": str(v.get("model") or v.get("model_used") or ""),
                "provider": str(v.get("provider") or ""),
                "decision": str(v.get("decision") or ""),
                "confidence": int(_safe_float(v.get("confidence"), 0.0) or 0),
            })
    result: Dict[str, Any] = {
        "decision": str(audit.get("decision") or "").upper(),
        "raw_decision": str(audit.get("raw_decision") or audit.get("decision") or "").upper(),
        "confidence": int(_safe_float(audit.get("confidence"), 0.0) or 0),
        "model_used": str(audit.get("model_used") or ""),
        "provider": str(audit.get("provider") or audit.get("provider_used") or ""),
        "summary": str(audit.get("summary") or "")[:240],
    }
    if votes_out:
        result["votes"] = votes_out
    p_yes = _safe_float(audit.get("p_yes"))
    if p_yes is not None and 0.0 <= p_yes <= 1.0:
        result["p_yes"] = round(p_yes, 4)
    return result


def _compact_fee_schedule(fee_sched: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(fee_sched, dict) or not fee_sched:
        return None
    return {
        "enabled": bool(fee_sched.get("enabled")),
        "rate": _safe_float(fee_sched.get("rate"), 0.0) or 0.0,
        "exponent": _safe_float(fee_sched.get("exponent"), 1.0) or 1.0,
        "taker_only": bool(fee_sched.get("taker_only", True)),
        "unknown": bool(fee_sched.get("unknown", False)),
    }


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _ensure_shadow_dedup(shadow_path: Path) -> Dict[Tuple[str, str, str], float]:
    key = str(shadow_path.resolve())
    cached = _shadow_dedup_cache.get(key)
    if cached is not None:
        return cached
    seen: Dict[Tuple[str, str, str], float] = {}
    for row in _read_jsonl(shadow_path):
        slug = str(row.get("slug") or "")
        outcome = str(row.get("outcome") or "")
        stage = str(row.get("stage") or "")
        ts = _safe_float(row.get("ts"), 0.0) or 0.0
        if slug and stage:
            k = (slug, outcome, stage)
            if ts > seen.get(k, 0.0):
                seen[k] = ts
    _shadow_dedup_cache[key] = seen
    return seen


# ---------------------------------------------------------------------------
# Fail-safe хуки записи для live_trader.py
# ---------------------------------------------------------------------------


def record_entry_submitted(
    pos: Dict[str, Any],
    candidate: Optional[Dict[str, Any]] = None,
    *,
    slot_size: float = 0.0,
    est_fee_usd: float = 0.0,
    journal_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Фиксирует отправку BUY-ордера со снимком стакана и контекстом входа."""
    try:
        ts = float(now if now is not None else time.time())
        cand = candidate or {}
        book = pos.get("entry_book_snapshot") if isinstance(pos.get("entry_book_snapshot"), dict) else {}
        req_px = _safe_float(pos.get("requested_price") or pos.get("entry_price"))
        risk = cand.get("risk") if isinstance(cand.get("risk"), dict) else {}
        fee_sched = _compact_fee_schedule(pos.get("fee_schedule"))
        record = {
            "schema": 1,
            "event_type": "ENTRY_SUBMITTED",
            "ts": round(ts, 3),
            "iso_utc": _iso_utc(ts),
            "pos_id": str(pos.get("id") or ""),
            "order_id": str(pos.get("order_id") or ""),
            "order_status": str(pos.get("order_status") or ""),
            "slug": str(pos.get("slug") or ""),
            "question": str(pos.get("question") or "")[:220],
            "outcome": str(pos.get("outcome") or ""),
            "token_id": str(pos.get("token_id") or ""),
            "event_id": str(pos.get("event_id") or ""),
            "end_date": str(pos.get("end_date") or ""),
            "days_left": _round_opt(_safe_float(pos.get("days_left_at_entry") or cand.get("days_left")), 2),
            "tier": str(pos.get("entry_tier") or cand.get("entry_tier") or ""),
            "open_slots_before": cand.get("open_slots_before"),
            "max_slots": cand.get("max_slots"),
            "slot_size": _round_opt(_safe_float(slot_size), 2),
            "requested_price": _round_opt(req_px, 4),
            "requested_shares": _round_opt(_safe_float(pos.get("requested_shares")), 4),
            "stop_loss_price": _round_opt(_safe_float(pos.get("stop_loss_price")), 4),
            "expected_apr": _round_opt(_safe_float(pos.get("expected_apr")), 2),
            "est_entry_fee_usd": _round_opt(_safe_float(est_fee_usd), 5),
            "fee_schedule": fee_sched,
            "price_band": classify_price_band(req_px),
            "book": book,
            "quality_score": _round_opt(_safe_float(cand.get("quality_score")), 2),
            "book_liquidity_usd": _round_opt(_safe_float(cand.get("book_liquidity_usd")), 2),
            "risk_score": risk.get("score"),
            "hr_change_cents": _round_opt(_safe_float(risk.get("hr_change_cents")), 3),
            "day_change_cents": _round_opt(_safe_float(risk.get("day_change_cents")), 3),
            "topic_clusters": list(pos.get("topic_clusters") or []),
            "ai_audit": _compact_ai_audit(pos.get("ai_audit")),
        }
        _append_jsonl(journal_path or TRADE_JOURNAL_FILE, record)
        return True
    except Exception:
        return False


def record_entry_rejected(
    pos: Dict[str, Any],
    reason: str = "",
    *,
    journal_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Фиксирует явный отказ биржи на BUY-ордере."""
    try:
        ts = float(now if now is not None else time.time())
        record = {
            "schema": 1,
            "event_type": "ENTRY_REJECTED",
            "ts": round(ts, 3),
            "iso_utc": _iso_utc(ts),
            "pos_id": str(pos.get("id") or ""),
            "slug": str(pos.get("slug") or ""),
            "outcome": str(pos.get("outcome") or ""),
            "requested_price": _round_opt(_safe_float(pos.get("requested_price")), 4),
            "requested_shares": _round_opt(_safe_float(pos.get("requested_shares")), 4),
            "reason": str(reason or pos.get("rejection_reason") or "")[:200],
            "book": pos.get("entry_book_snapshot") if isinstance(pos.get("entry_book_snapshot"), dict) else {},
        }
        _append_jsonl(journal_path or TRADE_JOURNAL_FILE, record)
        return True
    except Exception:
        return False


def record_entry_confirmed(
    pos: Dict[str, Any],
    entry_exec: Optional[Dict[str, Any]] = None,
    *,
    journal_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Фиксирует терминально подтверждённый вход с расчётом фактического slippage и комиссий."""
    try:
        ts = float(now if now is not None else time.time())
        gross_bought = _safe_float(pos.get("confirmed_bought_shares"), 0.0) or 0.0
        net_bought = _safe_float(pos.get("confirmed_net_bought_shares"), gross_bought) or 0.0
        cost = _safe_float(pos.get("entry_confirmed_cost_usd"), 0.0) or 0.0
        gross_avg = (cost / gross_bought) if gross_bought > 0 else _safe_float(pos.get("entry_price"))
        eff_entry = _safe_float(pos.get("entry_price"))
        req_px = _safe_float(pos.get("requested_price"))

        book = pos.get("entry_book_snapshot") if isinstance(pos.get("entry_book_snapshot"), dict) else {}
        best_ask = _safe_float(book.get("best_ask"))
        best_bid = _safe_float(book.get("best_bid"))
        mid = _safe_float(book.get("mid"))
        spread = _safe_float(book.get("spread"))

        slip_vs_ask = (gross_avg - best_ask) if (gross_avg is not None and best_ask is not None) else None
        slip_vs_mid = (gross_avg - mid) if (gross_avg is not None and mid is not None) else None
        slip_vs_req = (gross_avg - req_px) if (gross_avg is not None and req_px is not None) else None

        audit = _compact_ai_audit(pos.get("ai_audit"))
        record = {
            "schema": 1,
            "event_type": "ENTRY_CONFIRMED",
            "ts": round(ts, 3),
            "iso_utc": _iso_utc(ts),
            "pos_id": str(pos.get("id") or ""),
            "order_id": str(pos.get("order_id") or ""),
            "order_status": str((entry_exec or {}).get("status") or pos.get("order_status") or ""),
            "slug": str(pos.get("slug") or ""),
            "outcome": str(pos.get("outcome") or ""),
            "opened_at": _round_opt(_safe_float(pos.get("opened_at")), 3),
            "requested_shares": _round_opt(_safe_float(pos.get("requested_shares")), 4),
            "gross_bought_shares": _round_opt(gross_bought, 6),
            "net_bought_shares": _round_opt(net_bought, 6),
            "entry_cost_usd": _round_opt(cost, 6),
            "entry_fee_usd": _round_opt(_safe_float(pos.get("entry_fee_usd"), 0.0), 6),
            "entry_fee_shares": _round_opt(_safe_float(pos.get("entry_fee_shares"), 0.0), 6),
            "requested_price": _round_opt(req_px, 4),
            "gross_avg_price": _round_opt(gross_avg, 6),
            "entry_price": _round_opt(eff_entry, 6),
            "entry_best_bid": _round_opt(best_bid, 4),
            "entry_best_ask": _round_opt(best_ask, 4),
            "entry_mid": _round_opt(mid, 4),
            "entry_spread": _round_opt(spread, 4),
            "slippage_vs_ask": _round_opt(slip_vs_ask, 6),
            "slippage_vs_mid": _round_opt(slip_vs_mid, 6),
            "slippage_vs_req": _round_opt(slip_vs_req, 6),
            "price_band": classify_price_band(eff_entry or req_px),
            "tier": str(pos.get("entry_tier") or ""),
            "days_left_at_entry": _round_opt(_safe_float(pos.get("days_left_at_entry")), 2),
            "ai_confidence": audit.get("confidence"),
            "ai_provider": audit.get("provider"),
            "ai_p_yes": audit.get("p_yes"),
        }
        _append_jsonl(journal_path or TRADE_JOURNAL_FILE, record)
        return True
    except Exception:
        return False


def record_entry_cancelled(
    pos: Dict[str, Any],
    *,
    journal_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Фиксирует отменённый BUY-ордер с нулевым исполнением (для замера fill rate)."""
    try:
        ts = float(now if now is not None else time.time())
        record = {
            "schema": 1,
            "event_type": "ENTRY_CANCELLED",
            "ts": round(ts, 3),
            "iso_utc": _iso_utc(ts),
            "pos_id": str(pos.get("id") or ""),
            "order_id": str(pos.get("order_id") or ""),
            "slug": str(pos.get("slug") or ""),
            "outcome": str(pos.get("outcome") or ""),
            "requested_price": _round_opt(_safe_float(pos.get("requested_price")), 4),
            "requested_shares": _round_opt(_safe_float(pos.get("requested_shares")), 4),
            "order_status": str(pos.get("order_status") or ""),
        }
        _append_jsonl(journal_path or TRADE_JOURNAL_FILE, record)
        return True
    except Exception:
        return False


def record_exit_triggered(
    pos: Dict[str, Any],
    order: Dict[str, Any],
    summary: Optional[Dict[str, Any]] = None,
    reason: str = "",
    *,
    journal_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Фиксирует отправку SELL-ордера (Stop-Loss / Take-Profit / AI-SKIP) со снимком стакана."""
    try:
        ts = float(now if now is not None else time.time())
        snap = extract_book_snapshot(summary) or {}
        temp_pos = dict(pos)
        if reason:
            temp_pos["close_reason"] = reason
        exit_cat = classify_exit_category(temp_pos)
        attempt_no = len(pos.get("exit_orders") or [])
        record = {
            "schema": 1,
            "event_type": "EXIT_TRIGGERED",
            "ts": round(ts, 3),
            "iso_utc": _iso_utc(ts),
            "pos_id": str(pos.get("id") or ""),
            "order_id": str(order.get("order_id") or ""),
            "attempt_no": attempt_no,
            "slug": str(pos.get("slug") or ""),
            "outcome": str(pos.get("outcome") or ""),
            "exit_kind": str(pos.get("exit_kind") or ""),
            "exit_category": exit_cat,
            "reason": str(reason or pos.get("close_reason") or "")[:240],
            "requested_price": _round_opt(_safe_float(order.get("requested_price")), 4),
            "requested_shares": _round_opt(_safe_float(order.get("requested_shares")), 4),
            "entry_price": _round_opt(_safe_float(pos.get("entry_price")), 6),
            "stop_loss_price": _round_opt(_safe_float(pos.get("stop_loss_price")), 4),
            "book": snap,
        }
        _append_jsonl(journal_path or TRADE_JOURNAL_FILE, record)
        return True
    except Exception:
        return False


def record_trade_closed(
    pos: Dict[str, Any],
    *,
    payout: Optional[float] = None,
    journal_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Фиксирует окончательное закрытие или резолюцию позиции со всеми метриками."""
    try:
        ts = float(now if now is not None else time.time())
        status = str(pos.get("status") or "").upper()
        opened_at = _safe_float(pos.get("opened_at"))
        closed_at = _safe_float(pos.get("closed_at"), ts) or ts
        holding_hours = ((closed_at - opened_at) / 3600.0) if (opened_at is not None and closed_at >= opened_at) else None

        entry_price = _safe_float(pos.get("entry_price"))
        stop_px = _safe_float(pos.get("stop_loss_price"))
        gross_bought = _safe_float(pos.get("confirmed_bought_shares"), 0.0) or 0.0
        net_bought = _safe_float(pos.get("confirmed_net_bought_shares"))
        if net_bought is None:
            net_bought = gross_bought if gross_bought > 0 else (_safe_float(pos.get("shares"), 0.0) or 0.0)
        entry_cost = _safe_float(pos.get("entry_confirmed_cost_usd"))
        if entry_cost is None and entry_price and net_bought:
            entry_cost = entry_price * net_bought

        exit_orders = pos.get("exit_orders") or []
        sold_shares = sum((_safe_float(x.get("confirmed_shares"), 0.0) or 0.0) for x in exit_orders)
        gross_proceeds = sum((_safe_float(x.get("confirmed_cash"), 0.0) or 0.0) for x in exit_orders)
        if status == "RESOLVED":
            avg_exit_price = _safe_float(payout if payout is not None else pos.get("current_price"))
        else:
            avg_exit_price = (gross_proceeds / sold_shares) if sold_shares > 0 else None

        realized_pnl = _safe_float(pos.get("realized_pnl_usd"), 0.0) or 0.0
        return_pct = (realized_pnl / entry_cost * 100.0) if (entry_cost and entry_cost > 0) else None
        exit_cat = classify_exit_category(pos, payout=payout)

        entry_book = pos.get("entry_book_snapshot") if isinstance(pos.get("entry_book_snapshot"), dict) else {}
        trigger_snap = pos.get("exit_trigger_snapshot") if isinstance(pos.get("exit_trigger_snapshot"), dict) else {}
        trigger_bid = _safe_float(trigger_snap.get("best_bid"))

        slip_vs_stop = (
            (avg_exit_price - stop_px)
            if (exit_cat == "stop_loss" and avg_exit_price is not None and stop_px is not None)
            else None
        )
        slip_vs_trigger = (
            (avg_exit_price - trigger_bid)
            if (status == "CLOSED" and avg_exit_price is not None and trigger_bid is not None)
            else None
        )

        audit = _compact_ai_audit(pos.get("ai_audit"))
        record = {
            "schema": 1,
            "event_type": "TRADE_CLOSED",
            "ts": round(ts, 3),
            "iso_utc": _iso_utc(ts),
            "pos_id": str(pos.get("id") or ""),
            "slug": str(pos.get("slug") or ""),
            "question": str(pos.get("question") or pos.get("title") or "")[:220],
            "outcome": str(pos.get("outcome") or ""),
            "token_id": str(pos.get("token_id") or ""),
            "event_id": str(pos.get("event_id") or ""),
            "end_date": str(pos.get("end_date") or ""),
            "status": status,
            "exit_category": exit_cat,
            "close_reason": str(pos.get("close_reason") or "")[:240],
            "opened_at": _round_opt(opened_at, 3),
            "closed_at": _round_opt(closed_at, 3),
            "holding_hours": _round_opt(holding_hours, 2),
            "entry_price": _round_opt(entry_price, 6),
            "stop_loss_price": _round_opt(stop_px, 4),
            "entry_cost_usd": _round_opt(entry_cost, 6),
            "net_bought_shares": _round_opt(net_bought, 6),
            "entry_fee_usd": _round_opt(_safe_float(pos.get("entry_fee_usd"), 0.0), 6),
            "entry_mid": _round_opt(_safe_float(entry_book.get("mid")), 4),
            "entry_best_ask": _round_opt(_safe_float(entry_book.get("best_ask")), 4),
            "entry_spread": _round_opt(_safe_float(entry_book.get("spread")), 4),
            "price_band": classify_price_band(entry_price),
            "tier": str(pos.get("entry_tier") or ""),
            "days_left_at_entry": _round_opt(_safe_float(pos.get("days_left_at_entry")), 2),
            "topic_clusters": list(pos.get("topic_clusters") or []),
            "sold_shares": _round_opt(sold_shares, 6),
            "avg_exit_price": _round_opt(avg_exit_price, 6),
            "exit_fee_usd": _round_opt(_safe_float(pos.get("exit_fee_usd"), 0.0), 6),
            "exit_attempts": len(exit_orders),
            "exit_trigger_bid": _round_opt(trigger_bid, 4),
            "exit_slippage_vs_stop": _round_opt(slip_vs_stop, 6),
            "exit_slippage_vs_trigger_bid": _round_opt(slip_vs_trigger, 6),
            "realized_pnl_usd": _round_opt(realized_pnl, 6),
            "return_pct": _round_opt(return_pct, 3),
            "is_win": realized_pnl > 0,
            "needs_resolution_check": status != "RESOLVED",
            "ai_audit": audit,
        }
        _append_jsonl(journal_path or TRADE_JOURNAL_FILE, record)
        return True
    except Exception:
        return False


def record_shadow_candidate(
    cand: Dict[str, Any],
    *,
    stage: str,
    detail: str = "",
    tier_req: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
    fee_sched: Optional[Dict[str, Any]] = None,
    audit: Optional[Dict[str, Any]] = None,
    slot_size: float = DEFAULT_SHADOW_SLOT_USD,
    topic_clusters: Optional[Iterable[str]] = None,
    pos_id: str = "",
    shadow_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Логирует кандидата воронки Smart Bonding (включая отклонённых) с 24ч-дедупликацией."""
    try:
        ts = float(now if now is not None else time.time())
        path = shadow_path or SHADOW_CANDIDATES_FILE
        slug = str(cand.get("slug") or "").strip()
        outcome = str(cand.get("lead_outcome") or cand.get("outcome") or "").strip()
        if not slug or not stage:
            return False

        seen = _ensure_shadow_dedup(path)
        dedup_key = (slug, outcome, stage)
        if stage != "entered":
            prev_ts = seen.get(dedup_key, 0.0)
            if prev_ts > 0 and 0.0 <= (ts - prev_ts) < SHADOW_DEDUP_TTL_SEC:
                return False

        book = extract_book_snapshot(summary) if summary else None
        best_ask = _safe_float((book or {}).get("best_ask"))
        gamma_px = _safe_float(cand.get("entry_price"))
        sim_price = best_ask if best_ask is not None else gamma_px
        risk = cand.get("risk") if isinstance(cand.get("risk"), dict) else {}
        fee_info = _compact_fee_schedule(fee_sched or cand.get("fee_schedule"))

        record = {
            "schema": 1,
            "ts": round(ts, 3),
            "iso_utc": _iso_utc(ts),
            "slug": slug,
            "question": str(cand.get("question") or "")[:220],
            "outcome": outcome,
            "token_id": str(cand.get("token_id") or ""),
            "event_id": str(cand.get("event_id") or ""),
            "end_date": str(cand.get("end_date") or ""),
            "days_left": _round_opt(_safe_float(cand.get("days_left")), 2),
            "stage": stage,
            "detail": str(detail or "")[:180],
            "pos_id": str(pos_id or ""),
            "tier": str((tier_req or {}).get("tier") or ""),
            "tier_min_apr": (tier_req or {}).get("min_apr"),
            "tier_min_confidence": (tier_req or {}).get("min_confidence"),
            "slot_size": _round_opt(_safe_float(slot_size, DEFAULT_SHADOW_SLOT_USD), 2),
            "gamma_price": _round_opt(gamma_px, 4),
            "sim_entry_price": _round_opt(sim_price, 4),
            "price_source": "clob_ask" if best_ask is not None else "gamma",
            "price_band": classify_price_band(sim_price),
            "annualized_apr": _round_opt(_safe_float(cand.get("annualized_apr")), 2),
            "quality_score": _round_opt(_safe_float(cand.get("quality_score")), 2),
            "book_liquidity_usd": _round_opt(_safe_float(cand.get("book_liquidity_usd")), 2),
            "risk_score": risk.get("score"),
            "hr_change_cents": _round_opt(_safe_float(risk.get("hr_change_cents")), 3),
            "day_change_cents": _round_opt(_safe_float(risk.get("day_change_cents")), 3),
            "topic_clusters": sorted(set(topic_clusters or [])),
            "book": book,
            "fee_schedule": fee_info,
            "ai_audit": _compact_ai_audit(audit),
        }
        _append_jsonl(path, record)
        seen[dedup_key] = ts
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Кэш и обновление фактических резолюций Gamma API
# ---------------------------------------------------------------------------


def load_resolutions(path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(path or MARKET_RESOLUTIONS_FILE)
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("markets"), dict):
                return data
        except Exception:
            pass
    return {"schema": 1, "last_refresh_ts": 0.0, "markets": {}}


def save_resolutions(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    atomic_write_json(Path(path or MARKET_RESOLUTIONS_FILE), data)


def _market_is_resolved(m: Dict[str, Any]) -> bool:
    if not isinstance(m, dict) or not m.get("closed"):
        return False
    if str(m.get("umaResolutionStatus") or "").lower() == "resolved":
        return True
    if str(m.get("resolutionStatus") or "").lower() == "resolved":
        return True
    _, prices = parse_outcome_prices(m)
    return bool(prices) and all(p in (0.0, 1.0) for p in prices)


def get_resolved_payout(resolutions: Dict[str, Any], slug: str, outcome: str) -> Optional[float]:
    """Возвращает подтверждённую выплату ($0.00..$1.00) для (slug, outcome) или None."""
    if not slug:
        return None
    entry = (resolutions.get("markets") or {}).get(slug)
    if not isinstance(entry, dict) or not entry.get("resolved"):
        return None
    outcomes = entry.get("outcomes") or []
    prices = entry.get("prices") or []
    if not prices:
        return None
    if outcome in outcomes:
        idx = outcomes.index(outcome)
        if idx < len(prices):
            return _safe_float(prices[idx])
    # Регистронезависимый fallback
    low_outcome = str(outcome or "").strip().lower()
    for idx, label in enumerate(outcomes):
        if str(label).strip().lower() == low_outcome and idx < len(prices):
            return _safe_float(prices[idx])
    return None


def _collect_tracked_slugs(
    state: Optional[Dict[str, Any]],
    journal_rows: List[Dict[str, Any]],
    shadow_rows: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Собирает все slug (с их end_date), для которых нужен counterfactual-исход."""
    slugs: Dict[str, str] = {}

    def _add(slug: Any, end_date: Any) -> None:
        s = str(slug or "").strip()
        if not s:
            return
        ed = str(end_date or "").strip()
        if s not in slugs or (ed and not slugs[s]):
            slugs[s] = ed

    for row in shadow_rows:
        _add(row.get("slug"), row.get("end_date"))
    for row in journal_rows:
        if row.get("event_type") == "TRADE_CLOSED" and row.get("needs_resolution_check"):
            _add(row.get("slug"), row.get("end_date"))
    if isinstance(state, dict):
        for pos in state.get("positions", []):
            if str(pos.get("status") or "").upper() == "CLOSED":
                _add(pos.get("slug"), pos.get("end_date"))
    return slugs


def refresh_resolutions(
    *,
    state: Optional[Dict[str, Any]] = None,
    active_slugs: Optional[Set[str]] = None,
    max_checks: int = DEFAULT_MAX_RESOLUTIONS_PER_RUN,
    force: bool = False,
    fetcher: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    journal_path: Optional[Path] = None,
    shadow_path: Optional[Path] = None,
    resolutions_path: Optional[Path] = None,
    state_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Обновляет кэш резолюций Gamma API для закрытых и shadow-рынков (троттлинг 6 ч)."""
    ts = float(now if now is not None else time.time())
    res_path = Path(resolutions_path or MARKET_RESOLUTIONS_FILE)
    res_data = load_resolutions(res_path)
    last_refresh = _safe_float(res_data.get("last_refresh_ts"), 0.0) or 0.0

    if not force and last_refresh > 0 and (ts - last_refresh) < RESOLUTION_REFRESH_INTERVAL_SEC:
        return {"skipped": True, "checked": 0, "newly_resolved": 0, "errors": 0}

    if state is None:
        st_path = Path(state_path or LIVE_STATE_FILE)
        if st_path.exists():
            try:
                state = json.loads(st_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        else:
            state = {}

    journal_rows = _read_jsonl(journal_path or TRADE_JOURNAL_FILE)
    shadow_rows = _read_jsonl(shadow_path or SHADOW_CANDIDATES_FILE)
    tracked = _collect_tracked_slugs(state, journal_rows, shadow_rows)
    active_set = active_slugs or set()
    markets_map: Dict[str, Any] = res_data.setdefault("markets", {})

    # Приоритет 1: просроченные end_date (не проверялись > 6ч); Приоритет 2: ранние резолюции (не проверялись > 24ч)
    due_queue: List[Tuple[int, float, str, str]] = []
    for slug, end_date in tracked.items():
        existing = markets_map.get(slug) if isinstance(markets_map.get(slug), dict) else {}
        if existing.get("resolved"):
            continue
        if slug in active_set:
            continue
        last_checked = _safe_float(existing.get("checked_at"), 0.0) or 0.0
        end_ts = _parse_end_ts(end_date or existing.get("end_date"))
        is_past_due = end_ts is not None and ts >= (end_ts + 1800.0)
        if is_past_due:
            if force or (ts - last_checked) >= RESOLUTION_PAST_DUE_RECHECK_SEC:
                due_queue.append((0, last_checked, slug, end_date))
        else:
            if force or (ts - last_checked) >= RESOLUTION_EARLY_RECHECK_SEC:
                due_queue.append((1, last_checked, slug, end_date))

    due_queue.sort(key=lambda item: (item[0], item[1], item[2]))
    fetch_fn = fetcher or fetch_market_by_slug
    checked = 0
    newly_resolved = 0
    errors = 0

    for _prio, _last_chk, slug, end_date in due_queue[: max(0, int(max_checks))]:
        checked += 1
        try:
            mkt = fetch_fn(slug)
        except Exception:
            errors += 1
            continue
        if not isinstance(mkt, dict):
            markets_map[slug] = {
                "resolved": False,
                "closed": False,
                "not_found": True,
                "end_date": end_date,
                "checked_at": round(ts, 3),
            }
            continue
        resolved = _market_is_resolved(mkt)
        outcomes, prices = parse_outcome_prices(mkt)
        rec = {
            "resolved": resolved,
            "closed": bool(mkt.get("closed")),
            "outcomes": outcomes,
            "prices": prices,
            "end_date": str(mkt.get("endDate") or end_date or ""),
            "checked_at": round(ts, 3),
        }
        if resolved:
            rec["resolved_at"] = round(ts, 3)
            newly_resolved += 1
        markets_map[slug] = rec

    res_data["last_refresh_ts"] = round(ts, 3)
    if checked > 0 or not res_path.exists() or force:
        save_resolutions(res_data, res_path)
    return {
        "skipped": False,
        "checked": checked,
        "newly_resolved": newly_resolved,
        "errors": errors,
        "remaining_due": max(0, len(due_queue) - checked),
    }


# ---------------------------------------------------------------------------
# Аналитика и формирование отчёта (Этап 1)
# ---------------------------------------------------------------------------


def _simulate_hold_pnl(
    entry_price: Optional[float],
    payout: Optional[float],
    slot_size: float = DEFAULT_SHADOW_SLOT_USD,
    fee_schedule: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    px = _safe_float(entry_price)
    pay = _safe_float(payout)
    if px is None or pay is None or not (0.0 < px < 1.0) or not (0.0 <= pay <= 1.0) or slot_size <= 0:
        return None
    gross_shares = slot_size / px
    fee_usd = estimate_taker_fee_usd(gross_shares, px, fee_schedule) if isinstance(fee_schedule, dict) else 0.0
    net_shares = max(0.0, gross_shares - (fee_usd / px))
    return round(net_shares * pay - slot_size, 6)


def _merge_live_trades(
    state: Dict[str, Any],
    journal_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Объединяет live_portfolio_state.json и trade_journal.jsonl в единый список позиций."""
    by_id: Dict[str, Dict[str, Any]] = {}

    for pos in state.get("positions", []) if isinstance(state, dict) else []:
        pid = str(pos.get("id") or "")
        if not pid:
            continue
        status = str(pos.get("status") or "").upper()
        entry_price = _safe_float(pos.get("entry_price"))
        req_price = _safe_float(pos.get("requested_price"), entry_price)
        gross_bought = _safe_float(pos.get("confirmed_bought_shares"), 0.0) or 0.0
        net_bought = _safe_float(pos.get("confirmed_net_bought_shares"))
        if net_bought is None:
            net_bought = gross_bought if gross_bought > 0 else (_safe_float(pos.get("shares"), 0.0) or 0.0)
        entry_cost = _safe_float(pos.get("entry_confirmed_cost_usd"))
        if entry_cost is None and entry_price and net_bought:
            entry_cost = entry_price * net_bought
        exit_orders = pos.get("exit_orders") or []
        sold_shares = sum((_safe_float(x.get("confirmed_shares"), 0.0) or 0.0) for x in exit_orders)
        gross_proceeds = sum((_safe_float(x.get("confirmed_cash"), 0.0) or 0.0) for x in exit_orders)
        avg_exit = (
            _safe_float(pos.get("current_price"))
            if status == "RESOLVED"
            else ((gross_proceeds / sold_shares) if sold_shares > 0 else None)
        )
        entry_book = pos.get("entry_book_snapshot") if isinstance(pos.get("entry_book_snapshot"), dict) else {}
        trigger_snap = pos.get("exit_trigger_snapshot") if isinstance(pos.get("exit_trigger_snapshot"), dict) else {}
        opened_at = _safe_float(pos.get("opened_at"))
        closed_at = _safe_float(pos.get("closed_at"))
        hold_h = ((closed_at - opened_at) / 3600.0) if (opened_at and closed_at and closed_at >= opened_at) else None
        audit = _compact_ai_audit(pos.get("ai_audit"))
        by_id[pid] = {
            "pos_id": pid,
            "slug": str(pos.get("slug") or ""),
            "question": str(pos.get("question") or pos.get("title") or ""),
            "outcome": str(pos.get("outcome") or ""),
            "end_date": str(pos.get("end_date") or ""),
            "status": status,
            "exit_category": classify_exit_category(pos) if status in ("CLOSED", "RESOLVED") else "",
            "close_reason": str(pos.get("close_reason") or ""),
            "opened_at": opened_at,
            "closed_at": closed_at,
            "holding_hours": hold_h,
            "entry_price": entry_price,
            "requested_price": req_price,
            "stop_loss_price": _safe_float(pos.get("stop_loss_price")),
            "entry_cost_usd": entry_cost,
            "net_bought_shares": net_bought,
            "entry_fee_usd": _safe_float(pos.get("entry_fee_usd"), 0.0) or 0.0,
            "exit_fee_usd": _safe_float(pos.get("exit_fee_usd"), 0.0) or 0.0,
            "realized_pnl_usd": _safe_float(pos.get("realized_pnl_usd"), 0.0) or 0.0,
            "unrealized_pnl_usd": _safe_float(pos.get("unrealized_pnl_usd"), 0.0) or 0.0,
            "avg_exit_price": avg_exit,
            "exit_attempts": len(exit_orders),
            "entry_best_ask": _safe_float(entry_book.get("best_ask")),
            "entry_mid": _safe_float(entry_book.get("mid")),
            "entry_spread": _safe_float(entry_book.get("spread")),
            "slippage_vs_ask": None,
            "slippage_vs_mid": None,
            "exit_trigger_bid": _safe_float(trigger_snap.get("best_bid")),
            "exit_slippage_vs_stop": None,
            "exit_slippage_vs_trigger_bid": None,
            "price_band": classify_price_band(entry_price or req_price),
            "tier": str(pos.get("entry_tier") or ""),
            "days_left_at_entry": _safe_float(pos.get("days_left_at_entry")),
            "ai_audit": audit,
            "fee_schedule": _compact_fee_schedule(pos.get("fee_schedule")),
        }

    # Обогащаем точными данными из trade_journal.jsonl (и подхватываем записи, если их нет в state)
    for row in journal_rows:
        pid = str(row.get("pos_id") or "")
        if not pid:
            continue
        ev = str(row.get("event_type") or "")
        rec = by_id.setdefault(pid, {
            "pos_id": pid,
            "slug": str(row.get("slug") or ""),
            "question": str(row.get("question") or ""),
            "outcome": str(row.get("outcome") or ""),
            "end_date": str(row.get("end_date") or ""),
            "status": "OPEN",
            "exit_category": "",
            "close_reason": "",
            "opened_at": _safe_float(row.get("opened_at") or row.get("ts")),
            "closed_at": None,
            "holding_hours": None,
            "entry_price": _safe_float(row.get("entry_price") or row.get("requested_price")),
            "requested_price": _safe_float(row.get("requested_price")),
            "stop_loss_price": _safe_float(row.get("stop_loss_price")),
            "entry_cost_usd": _safe_float(row.get("entry_cost_usd")),
            "net_bought_shares": _safe_float(row.get("net_bought_shares"), 0.0) or 0.0,
            "entry_fee_usd": 0.0,
            "exit_fee_usd": 0.0,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "avg_exit_price": None,
            "exit_attempts": 0,
            "entry_best_ask": None,
            "entry_mid": None,
            "entry_spread": None,
            "slippage_vs_ask": None,
            "slippage_vs_mid": None,
            "exit_trigger_bid": None,
            "exit_slippage_vs_stop": None,
            "exit_slippage_vs_trigger_bid": None,
            "price_band": str(row.get("price_band") or "unknown"),
            "tier": str(row.get("tier") or ""),
            "days_left_at_entry": _safe_float(row.get("days_left") or row.get("days_left_at_entry")),
            "ai_audit": row.get("ai_audit") if isinstance(row.get("ai_audit"), dict) else {},
            "fee_schedule": row.get("fee_schedule") if isinstance(row.get("fee_schedule"), dict) else None,
        })
        if ev == "ENTRY_SUBMITTED":
            book = row.get("book") if isinstance(row.get("book"), dict) else {}
            if rec.get("entry_best_ask") is None:
                rec["entry_best_ask"] = _safe_float(book.get("best_ask"))
            if rec.get("entry_mid") is None:
                rec["entry_mid"] = _safe_float(book.get("mid"))
            if rec.get("entry_spread") is None:
                rec["entry_spread"] = _safe_float(book.get("spread"))
            if not rec.get("tier") and row.get("tier"):
                rec["tier"] = str(row["tier"])
            if rec.get("days_left_at_entry") is None and row.get("days_left") is not None:
                rec["days_left_at_entry"] = _safe_float(row.get("days_left"))
            if not rec.get("ai_audit") and isinstance(row.get("ai_audit"), dict):
                rec["ai_audit"] = row["ai_audit"]
        elif ev == "ENTRY_CONFIRMED":
            for fld in ("entry_best_ask", "entry_mid", "entry_spread", "slippage_vs_ask", "slippage_vs_mid"):
                val = _safe_float(row.get(fld))
                if val is not None:
                    rec[fld] = val
            if row.get("entry_cost_usd") is not None:
                rec["entry_cost_usd"] = _safe_float(row.get("entry_cost_usd"))
            if row.get("net_bought_shares") is not None:
                rec["net_bought_shares"] = _safe_float(row.get("net_bought_shares"))
            if row.get("entry_price") is not None:
                rec["entry_price"] = _safe_float(row.get("entry_price"))
                rec["price_band"] = classify_price_band(rec["entry_price"])
        elif ev == "EXIT_TRIGGERED":
            book = row.get("book") if isinstance(row.get("book"), dict) else {}
            if rec.get("exit_trigger_bid") is None:
                rec["exit_trigger_bid"] = _safe_float(book.get("best_bid"))
            rec["exit_attempts"] = max(int(rec.get("exit_attempts") or 0), int(row.get("attempt_no") or 1))
        elif ev == "TRADE_CLOSED":
            rec["status"] = str(row.get("status") or rec.get("status") or "CLOSED").upper()
            rec["exit_category"] = str(row.get("exit_category") or rec.get("exit_category") or "")
            if row.get("close_reason"):
                rec["close_reason"] = str(row["close_reason"])
            for fld in (
                "closed_at", "holding_hours", "entry_cost_usd", "net_bought_shares",
                "entry_fee_usd", "exit_fee_usd", "realized_pnl_usd", "avg_exit_price",
                "exit_trigger_bid", "exit_slippage_vs_stop", "exit_slippage_vs_trigger_bid",
                "entry_mid", "entry_best_ask", "entry_spread",
            ):
                val = _safe_float(row.get(fld))
                if val is not None:
                    rec[fld] = val
            if row.get("exit_attempts"):
                rec["exit_attempts"] = max(int(rec.get("exit_attempts") or 0), int(row["exit_attempts"]))
        elif ev == "ENTRY_CANCELLED" and rec["status"] == "OPEN":
            rec["status"] = "CANCELLED"
        elif ev == "ENTRY_REJECTED" and rec["status"] == "OPEN":
            rec["status"] = "REJECTED"

    # Досчитываем exit_slippage_vs_stop / vs_trigger для исторических записей из state
    for rec in by_id.values():
        avg_exit = _safe_float(rec.get("avg_exit_price"))
        stop_px = _safe_float(rec.get("stop_loss_price"))
        trig_bid = _safe_float(rec.get("exit_trigger_bid"))
        if rec.get("exit_slippage_vs_stop") is None and rec.get("exit_category") == "stop_loss" and avg_exit is not None and stop_px is not None:
            rec["exit_slippage_vs_stop"] = round(avg_exit - stop_px, 6)
        if rec.get("exit_slippage_vs_trigger_bid") is None and rec.get("status") == "CLOSED" and avg_exit is not None and trig_bid is not None:
            rec["exit_slippage_vs_trigger_bid"] = round(avg_exit - trig_bid, 6)
        if rec.get("price_band") in ("", "unknown"):
            rec["price_band"] = classify_price_band(rec.get("entry_price") or rec.get("requested_price"))

    return list(by_id.values())


def _profit_factor(pnls: List[float]) -> Optional[float]:
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss <= 1e-9:
        return None if gross_win <= 1e-9 else float("inf")
    return round(gross_win / gross_loss, 3)


def _max_drawdown_from_pnls(chronological_pnls: List[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in chronological_pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def compute_analytics(
    *,
    state: Optional[Dict[str, Any]] = None,
    journal_rows: Optional[List[Dict[str, Any]]] = None,
    shadow_rows: Optional[List[Dict[str, Any]]] = None,
    resolutions: Optional[Dict[str, Any]] = None,
    state_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Вычисляет полную статистику expectancy, атрибуции PnL, slippage, shadow и калибровки ИИ."""
    ts = float(now if now is not None else time.time())
    base_dir = Path(state_dir) if state_dir is not None else STATE_DIR

    if state is None:
        st_file = base_dir / "live_portfolio_state.json"
        if st_file.exists():
            try:
                state = json.loads(st_file.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        else:
            state = {}
    if journal_rows is None:
        journal_rows = _read_jsonl(base_dir / "trade_journal.jsonl")
    if shadow_rows is None:
        shadow_rows = _read_jsonl(base_dir / "shadow_candidates.jsonl")
    if resolutions is None:
        resolutions = load_resolutions(base_dir / "market_resolutions.json")

    trades = _merge_live_trades(state, journal_rows)
    open_trades = [t for t in trades if t["status"] == "OPEN"]
    closed_trades = [t for t in trades if t["status"] == "CLOSED"]
    resolved_trades = [t for t in trades if t["status"] == "RESOLVED"]
    completed = sorted(
        closed_trades + resolved_trades,
        key=lambda t: _safe_float(t.get("closed_at") or t.get("opened_at"), 0.0) or 0.0,
    )
    cancelled_count = sum(1 for t in trades if t["status"] == "CANCELLED")
    rejected_count = sum(1 for t in trades if t["status"] == "REJECTED")

    pnls = [float(t["realized_pnl_usd"]) for t in completed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    costs = [float(t["entry_cost_usd"]) for t in completed if (t.get("entry_cost_usd") or 0) > 0]
    hold_hours = [float(t["holding_hours"]) for t in completed if t.get("holding_hours") is not None]

    total_realized = round(sum(pnls), 4)
    total_cost_completed = sum(costs)
    expectancy_usd = round(total_realized / len(completed), 4) if completed else 0.0
    expectancy_pct = round((total_realized / total_cost_completed) * 100.0, 3) if total_cost_completed > 0 else None

    summary = {
        "generated_at_utc": _iso_utc(ts),
        "open_count": len(open_trades),
        "closed_early_count": len(closed_trades),
        "resolved_count": len(resolved_trades),
        "completed_count": len(completed),
        "cancelled_count": cancelled_count,
        "rejected_count": rejected_count,
        "win_count": len(wins),
        "loss_count": len(losses),
        "breakeven_count": len(completed) - len(wins) - len(losses),
        "win_rate_pct": round(len(wins) / len(completed) * 100.0, 1) if completed else None,
        "gross_profit_usd": round(sum(wins), 4),
        "gross_loss_usd": round(sum(losses), 4),
        "net_realized_pnl_usd": total_realized,
        "unrealized_open_pnl_usd": round(sum(float(t["unrealized_pnl_usd"]) for t in open_trades), 4),
        "profit_factor": _profit_factor(pnls),
        "expectancy_usd": expectancy_usd,
        "expectancy_pct": expectancy_pct,
        "avg_win_usd": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss_usd": round(sum(losses) / len(losses), 4) if losses else None,
        "median_holding_hours": round(statistics.median(hold_hours), 1) if hold_hours else None,
        "total_entry_fees_usd": round(sum(float(t["entry_fee_usd"]) for t in completed + open_trades), 4),
        "total_exit_fees_usd": round(sum(float(t["exit_fee_usd"]) for t in completed), 4),
        "max_drawdown_usd": _max_drawdown_from_pnls(pnls),
        "sample_warning": len(completed) < MIN_SIGNIFICANT_SAMPLE,
    }

    # 1. Когортный анализ по неделям открытия (убирает иллюзию Duration Bias в первые недели)
    cohorts_map: Dict[str, Dict[str, Any]] = {}
    for t in open_trades + completed:
        opened = _safe_float(t.get("opened_at"))
        if opened is None:
            wk = "unknown"
        else:
            iso = datetime.fromtimestamp(opened, tz=timezone.utc).isocalendar()
            wk = f"{iso.year}-W{iso.week:02d}"
        c = cohorts_map.setdefault(wk, {
            "cohort": wk,
            "opened": 0,
            "completed": 0,
            "still_open": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
        })
        c["opened"] += 1
        if t["status"] == "OPEN":
            c["still_open"] += 1
            c["unrealized_pnl_usd"] = round(c["unrealized_pnl_usd"] + float(t["unrealized_pnl_usd"]), 4)
        else:
            c["completed"] += 1
            pnl_val = float(t["realized_pnl_usd"])
            c["realized_pnl_usd"] = round(c["realized_pnl_usd"] + pnl_val, 4)
            if pnl_val > 0:
                c["wins"] += 1
            elif pnl_val < 0:
                c["losses"] += 1
    cohorts = []
    for wk in sorted(cohorts_map.keys()):
        row = cohorts_map[wk]
        row["total_pnl_usd"] = round(row["realized_pnl_usd"] + row["unrealized_pnl_usd"], 4)
        cohorts.append(row)

    # 2. Атрибуция по причинам выхода + counterfactual (что было бы, если держать до резолюции)
    exit_buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in EXIT_CATEGORY_ORDER}
    for t in completed:
        cat = t.get("exit_category") or "other_exit"
        exit_buckets.setdefault(cat, []).append(t)

    by_exit_category: List[Dict[str, Any]] = []
    for cat in EXIT_CATEGORY_ORDER:
        items = exit_buckets.get(cat) or []
        if not items:
            continue
        cat_pnls = [float(x["realized_pnl_usd"]) for x in items]
        cat_holds = [float(x["holding_hours"]) for x in items if x.get("holding_hours") is not None]
        stop_slips = [float(x["exit_slippage_vs_stop"]) * 100.0 for x in items if x.get("exit_slippage_vs_stop") is not None]
        trig_slips = [float(x["exit_slippage_vs_trigger_bid"]) * 100.0 for x in items if x.get("exit_slippage_vs_trigger_bid") is not None]

        # Counterfactual только по подмножеству уже разрешившихся рынков (честное сравнение)
        cf_resolved = 0
        cf_wins = 0
        cf_losses = 0
        cf_actual_pnl = 0.0
        cf_hold_pnl = 0.0
        if not cat.startswith("resolution_"):
            for x in items:
                pay = get_resolved_payout(resolutions, x["slug"], x["outcome"])
                if pay is None:
                    continue
                net_sh = _safe_float(x.get("net_bought_shares"), 0.0) or 0.0
                cost_usd = _safe_float(x.get("entry_cost_usd"), 0.0) or 0.0
                if net_sh <= 0 or cost_usd <= 0:
                    continue
                hold_pnl = net_sh * pay - cost_usd
                cf_resolved += 1
                if pay >= 0.99:
                    cf_wins += 1
                elif pay <= 0.01:
                    cf_losses += 1
                cf_actual_pnl += float(x["realized_pnl_usd"])
                cf_hold_pnl += hold_pnl

        by_exit_category.append({
            "category": cat,
            "label": EXIT_LABELS_RU.get(cat, cat),
            "count": len(items),
            "pnl_usd": round(sum(cat_pnls), 4),
            "avg_pnl_usd": round(sum(cat_pnls) / len(items), 4),
            "avg_holding_hours": round(sum(cat_holds) / len(cat_holds), 1) if cat_holds else None,
            "avg_slippage_vs_stop_cents": round(sum(stop_slips) / len(stop_slips), 3) if stop_slips else None,
            "avg_slippage_vs_trigger_cents": round(sum(trig_slips) / len(trig_slips), 3) if trig_slips else None,
            "cf_resolved": cf_resolved,
            "cf_wins": cf_wins,
            "cf_losses": cf_losses,
            "cf_actual_pnl_usd": round(cf_actual_pnl, 4) if cf_resolved else None,
            "cf_hold_pnl_usd": round(cf_hold_pnl, 4) if cf_resolved else None,
            "cf_impact_usd": round(cf_actual_pnl - cf_hold_pnl, 4) if cf_resolved else None,
        })

    # 3. Разбивка по ценовым бэндам
    by_price_band: List[Dict[str, Any]] = []
    for band in PRICE_BAND_ORDER:
        comp_b = [t for t in completed if t.get("price_band") == band]
        open_b = [t for t in open_trades if t.get("price_band") == band]
        if not comp_b and not open_b:
            continue
        b_pnls = [float(t["realized_pnl_usd"]) for t in comp_b]
        b_wins = sum(1 for p in b_pnls if p > 0)
        b_losses = sum(1 for p in b_pnls if p < 0)
        spreads = [float(t["entry_spread"]) * 100.0 for t in comp_b + open_b if t.get("entry_spread") is not None]
        slips_ask = [float(t["slippage_vs_ask"]) * 100.0 for t in comp_b + open_b if t.get("slippage_vs_ask") is not None]
        by_price_band.append({
            "band": band,
            "completed": len(comp_b),
            "open": len(open_b),
            "wins": b_wins,
            "losses": b_losses,
            "win_rate_pct": round(b_wins / len(comp_b) * 100.0, 1) if comp_b else None,
            "pnl_usd": round(sum(b_pnls), 4),
            "expectancy_usd": round(sum(b_pnls) / len(comp_b), 4) if comp_b else None,
            "profit_factor": _profit_factor(b_pnls),
            "avg_entry_spread_cents": round(sum(spreads) / len(spreads), 3) if spreads else None,
            "avg_entry_slippage_ask_cents": round(sum(slips_ask) / len(slips_ask), 3) if slips_ask else None,
        })

    # 4. Исполнение и slippage
    all_live = completed + open_trades
    entry_spreads = [float(t["entry_spread"]) * 100.0 for t in all_live if t.get("entry_spread") is not None]
    entry_slip_ask = [float(t["slippage_vs_ask"]) * 100.0 for t in all_live if t.get("slippage_vs_ask") is not None]
    entry_slip_mid = [float(t["slippage_vs_mid"]) * 100.0 for t in all_live if t.get("slippage_vs_mid") is not None]

    stop_items = exit_buckets.get("stop_loss") or []
    stop_pnls = [float(x["realized_pnl_usd"]) for x in stop_items]
    stop_cents_per_sh = [
        (float(x["avg_exit_price"]) - float(x["entry_price"])) * 100.0
        for x in stop_items
        if x.get("avg_exit_price") is not None and x.get("entry_price") is not None
    ]
    stop_slip_vs_stop = [float(x["exit_slippage_vs_stop"]) * 100.0 for x in stop_items if x.get("exit_slippage_vs_stop") is not None]
    stop_slip_vs_trig = [float(x["exit_slippage_vs_trigger_bid"]) * 100.0 for x in stop_items if x.get("exit_slippage_vs_trigger_bid") is not None]
    stop_attempts = [int(x.get("exit_attempts") or 1) for x in stop_items if x.get("exit_attempts")]

    execution = {
        "entry_spread_samples": len(entry_spreads),
        "avg_entry_spread_cents": round(sum(entry_spreads) / len(entry_spreads), 3) if entry_spreads else None,
        "entry_slippage_samples": len(entry_slip_ask),
        "avg_entry_slippage_vs_ask_cents": round(sum(entry_slip_ask) / len(entry_slip_ask), 3) if entry_slip_ask else None,
        "avg_entry_slippage_vs_mid_cents": round(sum(entry_slip_mid) / len(entry_slip_mid), 3) if entry_slip_mid else None,
        "stop_loss_count": len(stop_items),
        "avg_stop_pnl_usd": round(sum(stop_pnls) / len(stop_pnls), 4) if stop_pnls else None,
        "avg_stop_move_cents": round(sum(stop_cents_per_sh) / len(stop_cents_per_sh), 3) if stop_cents_per_sh else None,
        "avg_stop_slippage_vs_stop_cents": round(sum(stop_slip_vs_stop) / len(stop_slip_vs_stop), 3) if stop_slip_vs_stop else None,
        "avg_stop_slippage_vs_trigger_cents": round(sum(stop_slip_vs_trig) / len(stop_slip_vs_trig), 3) if stop_slip_vs_trig else None,
        "avg_stop_attempts": round(sum(stop_attempts) / len(stop_attempts), 2) if stop_attempts else None,
    }

    # 5. Shadow-анализ отклонённых кандидатов (без двойного счёта по (slug, outcome))
    entered_keys = {(t["slug"], t["outcome"]) for t in all_live if t.get("slug")}
    shadow_best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    later_entered_keys: Set[Tuple[str, str]] = set()

    for row in shadow_rows:
        slug = str(row.get("slug") or "")
        outcome = str(row.get("outcome") or "")
        stage = str(row.get("stage") or "")
        if not slug or not stage:
            continue
        k = (slug, outcome)
        if stage == "entered" or k in entered_keys:
            later_entered_keys.add(k)
            continue
        prio = FUNNEL_STAGE_PRIORITY.get(stage, 0)
        prev = shadow_best.get(k)
        if prev is None:
            shadow_best[k] = row
        else:
            prev_prio = FUNNEL_STAGE_PRIORITY.get(str(prev.get("stage") or ""), 0)
            if prio > prev_prio or (prio == prev_prio and (_safe_float(row.get("ts"), 0.0) or 0.0) > (_safe_float(prev.get("ts"), 0.0) or 0.0)):
                shadow_best[k] = row

    shadow_by_stage_map: Dict[str, List[Dict[str, Any]]] = {}
    shadow_by_band_map: Dict[str, List[Dict[str, Any]]] = {}
    for row in shadow_best.values():
        shadow_by_stage_map.setdefault(str(row.get("stage") or ""), []).append(row)
        shadow_by_band_map.setdefault(str(row.get("price_band") or classify_price_band(row.get("sim_entry_price"))), []).append(row)

    def _summarize_shadow_group(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        res_cnt = 0
        w_cnt = 0
        l_cnt = 0
        cf_pnls: List[float] = []
        for r in rows:
            pay = get_resolved_payout(resolutions, str(r.get("slug") or ""), str(r.get("outcome") or ""))
            if pay is None:
                continue
            slot_usd = _safe_float(r.get("slot_size"), DEFAULT_SHADOW_SLOT_USD) or DEFAULT_SHADOW_SLOT_USD
            cf_pnl = _simulate_hold_pnl(
                r.get("sim_entry_price") or r.get("gamma_price"),
                pay,
                slot_size=slot_usd,
                fee_schedule=r.get("fee_schedule"),
            )
            if cf_pnl is None:
                continue
            res_cnt += 1
            if pay >= 0.99:
                w_cnt += 1
            elif pay <= 0.01:
                l_cnt += 1
            cf_pnls.append(cf_pnl)
        return {
            "total": len(rows),
            "resolved": res_cnt,
            "wins": w_cnt,
            "losses": l_cnt,
            "win_rate_pct": round(w_cnt / res_cnt * 100.0, 1) if res_cnt else None,
            "cf_pnl_usd": round(sum(cf_pnls), 4) if res_cnt else None,
            "cf_expectancy_usd": round(sum(cf_pnls) / res_cnt, 4) if res_cnt else None,
        }

    ordered_stages = sorted(
        shadow_by_stage_map.keys(),
        key=lambda st: FUNNEL_STAGE_PRIORITY.get(st, 0),
        reverse=True,
    )
    shadow_by_stage = [
        {"stage": st, "label": STAGE_LABELS_RU.get(st, st), **_summarize_shadow_group(shadow_by_stage_map[st])}
        for st in ordered_stages
    ]
    shadow_by_band = [
        {"band": b, **_summarize_shadow_group(shadow_by_band_map[b])}
        for b in PRICE_BAND_ORDER
        if b in shadow_by_band_map
    ]

    # 6. Калибровка ИИ по разрешённым рынкам (реальные + shadow, строго разделяя APPROVE и SKIP)
    audited_resolved: Dict[Tuple[str, str], Dict[str, Any]] = {}
    # Сначала реальные позиции (у них приоритет, если рынок был открыт)
    for t in all_live:
        slug = str(t.get("slug") or "")
        outcome = str(t.get("outcome") or "")
        if not slug:
            continue
        pay = (
            _safe_float(t.get("avg_exit_price"))
            if t["status"] == "RESOLVED"
            else get_resolved_payout(resolutions, slug, outcome)
        )
        if pay is None:
            continue
        audit = t.get("ai_audit") if isinstance(t.get("ai_audit"), dict) else {}
        dec = str(audit.get("decision") or "").upper()
        if dec not in ("APPROVE", "SKIP"):
            continue
        audited_resolved[(slug, outcome)] = {
            "decision": dec,
            "confidence": int(_safe_float(audit.get("confidence"), 0.0) or 0),
            "p_yes": _safe_float(audit.get("p_yes")),
            "entry_price": _safe_float(t.get("entry_price") or t.get("requested_price")),
            "payout": pay,
            "fee_schedule": t.get("fee_schedule"),
            "slot_size": _safe_float(t.get("entry_cost_usd"), DEFAULT_SHADOW_SLOT_USD) or DEFAULT_SHADOW_SLOT_USD,
        }
    # Затем отклонённые shadow-рынки с реальным вердиктом ИИ
    for k, row in shadow_best.items():
        if k in audited_resolved:
            continue
        if str(row.get("stage") or "") not in ("ai_skip", "tier_min_confidence", "entry_exec_skipped"):
            continue
        pay = get_resolved_payout(resolutions, k[0], k[1])
        if pay is None:
            continue
        audit = row.get("ai_audit") if isinstance(row.get("ai_audit"), dict) else {}
        dec = str(audit.get("decision") or "").upper()
        if dec not in ("APPROVE", "SKIP"):
            continue
        audited_resolved[k] = {
            "decision": dec,
            "confidence": int(_safe_float(audit.get("confidence"), 0.0) or 0),
            "p_yes": _safe_float(audit.get("p_yes")),
            "entry_price": _safe_float(row.get("sim_entry_price") or row.get("gamma_price")),
            "payout": pay,
            "fee_schedule": row.get("fee_schedule"),
            "slot_size": _safe_float(row.get("slot_size"), DEFAULT_SHADOW_SLOT_USD) or DEFAULT_SHADOW_SLOT_USD,
        }

    def _bucket_stats(rows: List[Dict[str, Any]], ranges: List[Tuple[str, int, int]]) -> List[Dict[str, Any]]:
        out = []
        for label, lo, hi in ranges:
            sub = [r for r in rows if lo <= r["confidence"] <= hi]
            if not sub:
                continue
            w = sum(1 for r in sub if r["payout"] >= 0.99)
            l = sum(1 for r in sub if r["payout"] <= 0.01)
            cf_pnls = [
                p for p in (
                    _simulate_hold_pnl(r["entry_price"], r["payout"], slot_size=DEFAULT_SHADOW_SLOT_USD, fee_schedule=r.get("fee_schedule"))
                    for r in sub
                )
                if p is not None
            ]
            out.append({
                "bucket": label,
                "resolved": len(sub),
                "wins": w,
                "losses": l,
                "win_rate_pct": round(w / len(sub) * 100.0, 1),
                "loss_rate_pct": round(l / len(sub) * 100.0, 1),
                "cf_pnl_50_usd": round(sum(cf_pnls), 4) if cf_pnls else None,
            })
        return out

    approve_rows = [r for r in audited_resolved.values() if r["decision"] == "APPROVE"]
    skip_rows = [r for r in audited_resolved.values() if r["decision"] == "SKIP"]

    mkt_briers = [
        (r["entry_price"] - r["payout"]) ** 2
        for r in audited_resolved.values()
        if r.get("entry_price") is not None
    ]
    ai_p_briers = [
        (r["p_yes"] - r["payout"]) ** 2
        for r in audited_resolved.values()
        if r.get("p_yes") is not None
    ]

    ai_calibration = {
        "total_resolved_audited": len(audited_resolved),
        "approve_buckets": _bucket_stats(approve_rows, [
            ("70-74%", 70, 74), ("75-79%", 75, 79), ("80-84%", 80, 84), ("85-89%", 85, 89), ("90-100%", 90, 100),
        ]),
        "skip_buckets": _bucket_stats(skip_rows, [
            ("<75%", 0, 74), ("75-84%", 75, 84), ("85-100%", 85, 100),
        ]),
        "market_brier_score": round(sum(mkt_briers) / len(mkt_briers), 5) if mkt_briers else None,
        "ai_p_yes_brier_score": round(sum(ai_p_briers) / len(ai_p_briers), 5) if ai_p_briers else None,
        "ai_p_yes_samples": len(ai_p_briers),
    }

    # 7. Очередь ожидающих резолюции рынков
    tracked_all = _collect_tracked_slugs(state, journal_rows, shadow_rows)
    res_markets = resolutions.get("markets") or {}
    unresolved_total = 0
    unresolved_past_due = 0
    for slug, end_date in tracked_all.items():
        if isinstance(res_markets.get(slug), dict) and res_markets[slug].get("resolved"):
            continue
        unresolved_total += 1
        end_ts = _parse_end_ts(end_date)
        if end_ts is not None and ts >= (end_ts + 1800.0):
            unresolved_past_due += 1

    return {
        "summary": summary,
        "cohorts": cohorts,
        "by_exit_category": by_exit_category,
        "by_price_band": by_price_band,
        "execution": execution,
        "shadow": {
            "unique_rejected": len(shadow_best),
            "later_entered": len(later_entered_keys),
            "by_stage": shadow_by_stage,
            "by_price_band": shadow_by_band,
        },
        "ai_calibration": ai_calibration,
        "pending_resolutions": {
            "unresolved_total": unresolved_total,
            "unresolved_past_due": unresolved_past_due,
            "last_refresh_utc": _iso_utc(resolutions.get("last_refresh_ts")) if resolutions.get("last_refresh_ts") else "никогда",
        },
    }


def _fmt_usd(val: Optional[float], signed: bool = True) -> str:
    if val is None:
        return "н/д"
    if not math.isfinite(val):
        return "∞"
    if signed:
        return f"{'+' if val >= 0 else '-'}${abs(val):.2f}"
    return f"${val:.2f}"


def _fmt_pf(val: Optional[float]) -> str:
    if val is None:
        return "н/д"
    if math.isinf(val):
        return "∞"
    return f"{val:.2f}"


def _fmt_cents(val: Optional[float], signed: bool = True) -> str:
    if val is None:
        return "н/д"
    if signed:
        return f"{'+' if val >= 0 else ''}{val:.2f}¢"
    return f"{val:.2f}¢"


def format_analytics_report(data: Dict[str, Any]) -> str:
    """Форматирует структурированную аналитику в читаемый текстовый отчёт на русском."""
    s = data["summary"]
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(f" ОТЧЁТ АНАЛИТИКИ SMART BONDING | {s['generated_at_utc']}")
    lines.append("=" * 72)
    lines.append(
        f" Сделки: завершено {s['completed_count']} "
        f"(резолюция: {s['resolved_count']}, досрочный выход: {s['closed_early_count']}), "
        f"открыто: {s['open_count']} | отмен BUY: {s['cancelled_count']}, отказов: {s['rejected_count']}"
    )
    if s["completed_count"] > 0:
        wr = f"{s['win_rate_pct']:.1f}%" if s["win_rate_pct"] is not None else "н/д"
        exp_pct = f" ({s['expectancy_pct']:+.2f}%)" if s["expectancy_pct"] is not None else ""
        med_h = f"{s['median_holding_hours']:.1f} ч" if s["median_holding_hours"] is not None else "н/д"
        lines.append(
            f" Win Rate: {wr} ({s['win_count']}W / {s['loss_count']}L) | "
            f"Profit Factor: {_fmt_pf(s['profit_factor'])} | "
            f"Expectancy: {_fmt_usd(s['expectancy_usd'])}{exp_pct}"
        )
        lines.append(
            f" Реализованный PnL: {_fmt_usd(s['net_realized_pnl_usd'])} "
            f"(+{_fmt_usd(s['gross_profit_usd'], False)} / {_fmt_usd(s['gross_loss_usd'])}) | "
            f"Нереализ. (открытые): {_fmt_usd(s['unrealized_open_pnl_usd'])}"
        )
        lines.append(
            f" Ср. выигрыш: {_fmt_usd(s['avg_win_usd'])} | Ср. убыток: {_fmt_usd(s['avg_loss_usd'])} | "
            f"Max DD (realized): {_fmt_usd(s['max_drawdown_usd'], False)} | Медиана удержания: {med_h}"
        )
        lines.append(
            f" Комиссии: вход {_fmt_usd(s['total_entry_fees_usd'], False)}, выход {_fmt_usd(s['total_exit_fees_usd'], False)}"
        )
    else:
        lines.append(" Завершённых сделок пока нет.")
    if s.get("sample_warning"):
        lines.append(
            f" [!] Малая выборка ({s['completed_count']} < {MIN_SIGNIFICANT_SAMPLE}): "
            "статистика ещё шумная и смещена в сторону ранних выходов; параметры менять рано."
        )

    # Когорты
    cohorts = data.get("cohorts") or []
    if cohorts:
        lines.append("\n--- 1. Когорты по неделям открытия (учёт ещё открытых позиций) ---")
        for c in cohorts:
            lines.append(
                f"  {c['cohort']}: открыто {c['opened']} (заверш. {c['completed']}: {c['wins']}W/{c['losses']}L, "
                f"в работе {c['still_open']}) | Realized {_fmt_usd(c['realized_pnl_usd'])} | "
                f"MTM {_fmt_usd(c['unrealized_pnl_usd'])} | Итого {_fmt_usd(c['total_pnl_usd'])}"
            )

    # Атрибуция выходов + counterfactual
    exits = data.get("by_exit_category") or []
    if exits:
        lines.append("\n--- 2. PnL по причинам выхода (+ counterfactual: если бы держали до финиша) ---")
        for e in exits:
            base = (
                f"  {e['label']:<25} | n={e['count']:<3} | PnL {_fmt_usd(e['pnl_usd']):>8} "
                f"(ср. {_fmt_usd(e['avg_pnl_usd'])})"
            )
            if e["cf_resolved"] > 0:
                base += (
                    f" | Разр. {e['cf_resolved']} ({e['cf_wins']}W/{e['cf_losses']}L): "
                    f"факт {_fmt_usd(e['cf_actual_pnl_usd'])} vs hold {_fmt_usd(e['cf_hold_pnl_usd'])} "
                    f"(эффект {_fmt_usd(e['cf_impact_usd'])})"
                )
            lines.append(base)

    # Ценовые бэнды
    bands = data.get("by_price_band") or []
    if bands:
        lines.append("\n--- 3. Ценовые бэнды входа (живые сделки) ---")
        for b in bands:
            wr = f"{b['win_rate_pct']:.1f}%" if b["win_rate_pct"] is not None else "н/д"
            lines.append(
                f"  {b['band']:<11} | заверш. {b['completed']:<3} (откр. {b['open']:<2}) | "
                f"WR {wr:>5} | PF {_fmt_pf(b['profit_factor']):>4} | "
                f"Exp {_fmt_usd(b['expectancy_usd']):>7} | PnL {_fmt_usd(b['pnl_usd']):>8} | "
                f"спред {_fmt_cents(b['avg_entry_spread_cents'], False)}"
            )

    # Исполнение и slippage
    ex = data.get("execution") or {}
    lines.append("\n--- 4. Качество исполнения и Slippage ---")
    lines.append(
        f"  Вход (n={ex['entry_spread_samples']}): ср. спред {_fmt_cents(ex['avg_entry_spread_cents'], False)} | "
        f"slippage vs ask {_fmt_cents(ex['avg_entry_slippage_vs_ask_cents'])} | "
        f"vs mid {_fmt_cents(ex['avg_entry_slippage_vs_mid_cents'])}"
    )
    if ex.get("stop_loss_count"):
        lines.append(
            f"  Stop-Loss (n={ex['stop_loss_count']}): ср. убыток {_fmt_usd(ex['avg_stop_pnl_usd'])} "
            f"({_fmt_cents(ex['avg_stop_move_cents'])}/акц.) | "
            f"vs stop {_fmt_cents(ex['avg_stop_slippage_vs_stop_cents'])} | "
            f"vs trigger bid {_fmt_cents(ex['avg_stop_slippage_vs_trigger_cents'])} | "
            f"ср. попыток: {ex['avg_stop_attempts'] or 'н/д'}"
        )

    # Shadow-анализ
    sh = data.get("shadow") or {}
    lines.append(
        f"\n--- 5. Shadow-кандидаты (отклонённые: {sh.get('unique_rejected', 0)} уник., "
        f"позже вошли: {sh.get('later_entered', 0)}) ---"
    )
    sh_stages = sh.get("by_stage") or []
    if sh_stages:
        for st in sh_stages:
            wr = f"{st['win_rate_pct']:.1f}%" if st["win_rate_pct"] is not None else "н/д"
            lines.append(
                f"  {st['label']:<36} | всего {st['total']:<4} | разр. {st['resolved']:<3} "
                f"({st['wins']}W/{st['losses']}L, WR {wr:>5}) | "
                f"hold PnL {_fmt_usd(st['cf_pnl_usd']):>8} (ср. {_fmt_usd(st['cf_expectancy_usd'])})"
            )
    else:
        lines.append("  Записей в shadow_candidates.jsonl пока нет (начнут копиться после запуска).")

    sh_bands = sh.get("by_price_band") or []
    if any(b["resolved"] > 0 for b in sh_bands):
        lines.append("  [Shadow по ценовым бэндам (только отклонённые, hold до резолюции)]:")
        for b in sh_bands:
            if b["resolved"] == 0:
                continue
            wr = f"{b['win_rate_pct']:.1f}%" if b["win_rate_pct"] is not None else "н/д"
            lines.append(
                f"    {b['band']:<11} | разр. {b['resolved']:<3} ({b['wins']}W/{b['losses']}L, WR {wr:>5}) | "
                f"hold PnL {_fmt_usd(b['cf_pnl_usd']):>8}"
            )

    # Калибровка ИИ
    cal = data.get("ai_calibration") or {}
    lines.append(f"\n--- 6. Калибровка ИИ (разрешённых с вердиктом ИИ: {cal.get('total_resolved_audited', 0)}) ---")
    if cal.get("approve_buckets"):
        lines.append("  APPROVE по уверенности (ожидание: выше уверенность -> выше Win Rate):")
        for b in cal["approve_buckets"]:
            lines.append(
                f"    {b['bucket']:<8} | n={b['resolved']:<3} | WR {b['win_rate_pct']:>5.1f}% "
                f"({b['wins']}W/{b['losses']}L) | hold PnL@50: {_fmt_usd(b['cf_pnl_50_usd'])}"
            )
    if cal.get("skip_buckets"):
        lines.append("  SKIP по уверенности (ожидание: выше уверенность SKIP -> выше доля провалов):")
        for b in cal["skip_buckets"]:
            lines.append(
                f"    {b['bucket']:<8} | n={b['resolved']:<3} | Loss {b['loss_rate_pct']:>5.1f}% "
                f"({b['wins']}W/{b['losses']}L) | hold PnL@50: {_fmt_usd(b['cf_pnl_50_usd'])}"
            )
    mkt_brier = cal.get("market_brier_score")
    ai_brier = cal.get("ai_p_yes_brier_score")
    if mkt_brier is not None:
        ai_part = f" | AI p_yes Brier: {ai_brier:.5f} (n={cal['ai_p_yes_samples']})" if ai_brier is not None else " | AI p_yes: не задаётся в промпте v3"
        lines.append(f"  Market Brier Score: {mkt_brier:.5f}{ai_part}")

    # Очередь резолюций
    pend = data.get("pending_resolutions") or {}
    lines.append(
        f"\n--- Очередь резолюций: ждут {pend.get('unresolved_total', 0)} "
        f"(просрочили endDate: {pend.get('unresolved_past_due', 0)}), "
        f"последнее обновление: {pend.get('last_refresh_utc')} ---"
    )
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Аналитика журнала сделок и shadow-кандидатов Smart Bonding")
    parser.add_argument("--report", action="store_true", help="Вывести текстовый отчёт аналитики")
    parser.add_argument("--json", action="store_true", help="Вывести отчёт в формате JSON")
    parser.add_argument("--resolve", action="store_true", help="Догрузить фактические резолюции из Gamma API")
    parser.add_argument("--max-checks", type=int, default=40, help="Макс. запросов к Gamma при --resolve (по умолч. 40)")
    parser.add_argument("--state-dir", type=str, default="", help="Каталог state/ (по умолчанию ./state)")
    args = parser.parse_args()

    sdir = Path(args.state_dir) if args.state_dir else STATE_DIR
    if args.resolve:
        res = refresh_resolutions(
            max_checks=args.max_checks,
            force=True,
            journal_path=sdir / "trade_journal.jsonl",
            shadow_path=sdir / "shadow_candidates.jsonl",
            resolutions_path=sdir / "market_resolutions.json",
            state_path=sdir / "live_portfolio_state.json",
        )
        if not args.json:
            print(
                f"Обновление резолюций: проверено {res['checked']}, "
                f"новых резолюций {res['newly_resolved']}, ошибок {res['errors']}, "
                f"осталось в очереди {res.get('remaining_due', 0)}."
            )

    report_data = compute_analytics(state_dir=sdir)
    if args.json:
        print(json.dumps(report_data, ensure_ascii=False, indent=2))
    else:
        print(format_analytics_report(report_data))


if __name__ == "__main__":
    main()
