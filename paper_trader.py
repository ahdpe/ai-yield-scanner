"""Демон честной симуляции (Paper-Trading) портфеля $5,000 USDC с аудитом через Venice AI,
квадратичной формулой ревардов по живому стакану Polymarket CLOB и проверкой спот-хеджа на Hyperliquid."""

import argparse
from datetime import datetime, timezone
import json
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai_analyzer import (
    REAUDIT_RESERVE_CALLS,
    call_venice_market_audit,
    entry_budget_exhausted,
    get_ai_health,
    ping_venice_ai,
)
from state_io import STATE_DIR, atomic_write_json, esc
from telegram_notifier import send_telegram_alert
from polymarket_scanner import (
    compute_quadratic_mm_metrics,
    days_until,
    fetch_clob_book,
    fetch_polymarket_markets,
    fetch_market_by_slug,
    fetch_recent_trades,
    parse_clob_token_ids,
    parse_outcome_prices,
    scan_liquidity_rewards,
    scan_smart_bonding,
    trade_yes_price,
)
from hyperliquid_scanner import fetch_hl_spot_hedge_map, scan_funding_anomalies, hl_request


STATE_FILE = STATE_DIR / "portfolio_state.json"
LOG_FILE = STATE_DIR / "paper_trader.log"

KILL_SWITCH_MOVE = 0.01           # сдвиг цены (1.0¢), при котором лимитки переставляются заранее
BOND_STOP_LOSS = 0.95             # бонд продается, если цена исхода опустилась ниже 95¢
HL_FEE_RATE = 0.0007              # комиссия 0.035% × 2 ноги — при входе и при выходе
ENTRY_AI_SAMPLES = 2              # вход только при двух независимых APPROVE от модели
REAUDIT_INTERVAL_SEC = 6 * 3600   # плановый повторный аудит открытых позиций Polymarket
REAUDIT_RECHECK_SEC = 3600        # после первого SKIP перепроверяем через час
AI_SKIP_STREAK_TO_CLOSE = 2       # закрываем позицию после двух свежих SKIP подряд
HL_ADVERSE_TICKS_TO_CLOSE = 2     # закрываем фандинг после двух тиков с разворотом знака
MAX_ACCRUAL_GAP_SEC = 3600        # дольше часа без наблюдения рынка доход не начисляем
REOPEN_COOLDOWN_SEC = 12 * 3600   # не переоткрываем закрытый рынок в течение 12 часов
REFILL_FRESH_AUDITS_PER_STRATEGY = 1  # за тик — не больше 1 нового кандидата на стратегию (≤3 платных проверки)
MM_SPREAD_FRACTION = 0.65         # отступ наших лимиток от mid в долях rewardsMaxSpread
MM_POLL_SEC = 60                  # между полными тиками стакан и лента сделок MM-слотов проверяются раз в минуту
MM_FILL_PAUSE_SEC = 3 * 3600      # после исполнения лимитки котирование рынка на паузе 3 часа
MM_FILLS_TO_CLOSE = 2             # второе исполнение за сутки — слот закрывается, капитал уходит в другой рынок
MM_FILL_WINDOW_SEC = 24 * 3600
MM_MIN_DAYS_TO_END_HOLD = 2.0     # открытый MM-слот снимается, когда до истечения рынка меньше 2 дней
MM_MAX_ACCRUAL_APR = 365.0        # потолок APR ревардов при начислении (1% в день)
MM_APR_MAX_RISE_PER_HOUR = 2.0    # расчетный APR растет не быстрее чем вдвое за час (пустой стакан после обвала — не доход)
LOG_THROTTLE_SEC = 900            # повторяющиеся служебные сообщения MM-слота — не чаще раза в 15 минут
MM_TRADE_LOOKBACK_SEC = 900       # лента сделок Polymarket запаздывает на 1.5–4 мин: каждую проверку берем сделки за 15 мин


def log_event(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_state(state: Dict[str, Any]) -> None:
    atomic_write_json(STATE_FILE, state)


def is_open(pos: Dict[str, Any]) -> bool:
    return pos.get("status", "OPEN") == "OPEN"


def position_net_pnl(pos: Dict[str, Any]) -> float:
    """Чистый результат слота: реварды/фандинг − издержки + реализованный и нереализованный PnL."""
    return (
        float(pos.get("accrued_rewards_usd", 0.0))
        - float(pos.get("adverse_loss_usd", 0.0))
        + float(pos.get("realized_pnl_usd", 0.0))
        + float(pos.get("unrealized_pnl_usd", 0.0))
    )


def portfolio_totals(state: Dict[str, Any]) -> Dict[str, float]:
    """Сводка портфеля — единая формула для демона, CLI и Telegram-бота."""
    positions = state["positions"]
    deposit = float(state["initial_deposit_usd"])
    net_pnl = sum(position_net_pnl(p) for p in positions)
    open_pos = [p for p in positions if is_open(p)]
    return {
        "deposit": deposit,
        "rewards": sum(float(p.get("accrued_rewards_usd", 0.0)) for p in positions),
        "losses": sum(float(p.get("adverse_loss_usd", 0.0)) for p in positions),
        "realized": sum(float(p.get("realized_pnl_usd", 0.0)) for p in positions),
        "unrealized": sum(float(p.get("unrealized_pnl_usd", 0.0)) for p in positions),
        "net_pnl": net_pnl,
        "equity": deposit + net_pnl,
        "weighted_apr": sum(float(p["expected_apr"]) * (float(p["allocated_usd"]) / deposit) for p in open_pos),
        "open_count": len(open_pos),
        "idle_usd": max(0.0, deposit - sum(float(p["allocated_usd"]) for p in open_pos)),
    }


def close_position(pos: Dict[str, Any], status: str, reason: str) -> None:
    """Закрывает слот: нереализованный PnL становится реализованным, капитал освобождается."""
    pos["realized_pnl_usd"] = round(float(pos.get("realized_pnl_usd", 0.0)) + float(pos.get("unrealized_pnl_usd", 0.0)), 4)
    pos["unrealized_pnl_usd"] = 0.0
    pos["status"] = status
    pos["closed_at"] = time.time()
    pos["close_reason"] = reason
    net = position_net_pnl(pos)
    log_event(f"⏹ [{status}] {pos['title'][:50]}: {reason}. Итог слота: ${net:+.2f}")
    send_telegram_alert(
        f"⏹ <b>Позиция закрыта ({esc(status)})</b>\n"
        f"Рынок: <i>{esc(pos['title'])}</i>\n"
        f"Причина: {esc(reason)}\n"
        f"Итог слота: <b>${net:+.2f} USDC</b>"
    )


def outcome_price(m: Dict[str, Any], outcome: str, fallback: float) -> float:
    outcomes, prices = parse_outcome_prices(m)
    if outcome in outcomes and outcomes.index(outcome) < len(prices):
        return prices[outcomes.index(outcome)]
    return prices[0] if prices else fallback


def market_trading_finished(m: Dict[str, Any]) -> bool:
    """Рынок закрыт, не принимает ордера или его срок уже истек — реварды MM больше не начисляются."""
    if m.get("closed") or m.get("acceptingOrders") is False:
        return True
    end_str = m.get("endDate") or ""
    if end_str:
        try:
            return datetime.fromisoformat(end_str.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            pass
    return False


def hl_funding_brief(c: Dict[str, Any]) -> str:
    spot = c.get("spot_hedge") or {}
    spot_txt = (
        f"Спот-хедж на Hyperliquid L1: {spot.get('spot_symbol')} (пара {spot.get('spot_pair_id')}, "
        f"объем 24ч ${spot.get('spot_vol_24h_usd', 0)/1e6:.2f}M)."
        if spot else "Спот-хедж на Hyperliquid L1 отсутствует."
    )
    return (
        f"Текущий фандинг {c['current_apr']:+.1f}% годовых, средний за 72ч {c['avg_72h_apr']:+.1f}%, "
        f"знак держался {c['same_sign_pct_72h']:.0f}% часов; Open Interest ${c['oi_usd']/1e6:.1f}M, "
        f"объем 24ч ${c['vol_24h_usd']/1e6:.1f}M. {spot_txt} Схема: {c['strategy']}."
    )


def _is_cached_verdict(audit: Dict[str, Any], now: float) -> bool:
    """Вердикт взят из кэша аудитов (не свежий и не служебный отказ по бюджету/связи)."""
    if str(audit.get("model_used", "")).startswith("none"):
        return False
    return float(audit.get("audited_at") or 0.0) < now


def _open_mm_slot(item: Dict[str, Any], slot_size: float, now: float) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    audit = call_venice_market_audit(
        question=item["question"],
        description=item["description"],
        price_yes=item["price_yes"],
        strategy_type="polymarket_liquidity_maker",
        samples=ENTRY_AI_SAMPLES,
        budget_reserve=REAUDIT_RESERVE_CALLS,
        end_date=item.get("end_date", ""),
    )
    if not _is_cached_verdict(audit, now):  # кэшированные SKIP не дублируем в логе каждый тик
        log_event(
            f"Venice AI ({audit.get('model_used', 'local')}) аудит [{item['question'][:48]}...]: "
            f"{audit['decision']} (conf={audit['confidence']}%) — {audit['summary']}"
        )
    if audit["decision"] != "APPROVE":
        return None, audit

    mid = item["price_yes"]
    return {
        "id": f"mm_{item['slug'][:30]}",
        "type": "poly_mm",
        "status": "OPEN",
        "opened_at": now,
        "title": item["question"],
        "slug": item["slug"],
        "token_id": item.get("token_id", ""),
        "condition_id": item.get("condition_id", ""),
        "end_date": item.get("end_date", ""),
        "daily_reward_pool_usd": item.get("daily_reward_pool_usd", 0.0),
        "min_order_size": item.get("min_order_size", 0.0),
        "allocated_usd": slot_size,
        "entry_mid": mid,
        "current_mid": mid,
        "bid_limit": item["bid_limit"],
        "ask_limit": item["ask_limit"],
        "quote_anchor_mid": mid,
        "quote_log": [[now, item["bid_limit"], item["ask_limit"]]],
        "last_mm_check_at": now,
        "reward_apr_ref": min(float(item["conservative_apr"]), MM_MAX_ACCRUAL_APR),
        "book_spread": item.get("book_spread", 0.01),
        "book_quadratic_score": item.get("book_quadratic_score", 0.0),
        "max_spread_cents": item["max_spread_cents"],
        # Используем честный квадратичный APR при отступе 0.65 * max_spread против реального стакана CLOB
        "expected_apr": round(min(float(item["conservative_apr"]), MM_MAX_ACCRUAL_APR), 2),
        "accrued_rewards_usd": 0.0,
        "adverse_loss_usd": 0.0,
        "rebalances_count": 0,
        "ai_audit": audit,
        "last_audit_ts": audit.get("audited_at"),
    }, audit


def _open_bond_slot(b: Dict[str, Any], slot_size: float, now: float) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    audit = call_venice_market_audit(
        question=b["question"],
        description=b["description"],
        price_yes=b["entry_price"],
        strategy_type="polymarket_smart_bonding",
        outcome_label=b["lead_outcome"],
        samples=ENTRY_AI_SAMPLES,
        budget_reserve=REAUDIT_RESERVE_CALLS,
        end_date=b.get("end_date", ""),
    )
    if not _is_cached_verdict(audit, now):  # кэшированные SKIP не дублируем в логе каждый тик
        log_event(
            f"Venice AI бондинг-аудит [{b['question'][:48]}...]: "
            f"{audit['decision']} (conf={audit['confidence']}%) — {audit['summary']}"
        )
    if audit["decision"] != "APPROVE":
        return None, audit

    return {
        "id": f"bond_{b['question'][:24]}",
        "type": "poly_bond",
        "status": "OPEN",
        "opened_at": now,
        "title": f"{b['question']} [{b['lead_outcome']}]",
        "slug": b["slug"],
        "outcome": b["lead_outcome"],
        "allocated_usd": slot_size,
        "shares": round(slot_size / b["entry_price"], 4),
        "entry_price": b["entry_price"],
        "current_price": b["entry_price"],
        "expected_apr": round(float(b["annualized_apr"]), 2),
        "accrued_rewards_usd": 0.0,
        "adverse_loss_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
        "ai_audit": audit,
        "last_audit_ts": audit.get("audited_at"),
    }, audit


def _open_hl_slot(c: Dict[str, Any], slot_size: float, now: float) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    direction = "SHORT" if c["current_apr"] > 0 else "LONG"
    audit = call_venice_market_audit(
        question=f"Hyperliquid {c['coin']} funding carry ({direction} perp)",
        description=hl_funding_brief(c),
        price_yes=0.0,
        strategy_type="hyperliquid_funding",
        outcome_label=direction,
        samples=ENTRY_AI_SAMPLES,
        budget_reserve=REAUDIT_RESERVE_CALLS,
    )
    if not _is_cached_verdict(audit, now):  # кэшированные SKIP не дублируем в логе каждый тик
        log_event(
            f"Venice AI фандинг-аудит [Hyperliquid {c['coin']} {direction}]: "
            f"{audit['decision']} (conf={audit['confidence']}%) — {audit['summary']}"
        )
    if audit["decision"] != "APPROVE":
        return None, audit

    entry_fee = round(slot_size * HL_FEE_RATE, 4)
    spot = c.get("spot_hedge") or {}
    return {
        "id": f"hl_{c['coin']}",
        "type": "hl_funding",
        "status": "OPEN",
        "opened_at": now,
        "title": f"Hyperliquid {c['coin']} ({c['strategy'][:42]})",
        "coin": c["coin"],
        "direction": direction,
        "spot_hedge": spot,
        "allocated_usd": slot_size,
        # Фандинг идет на перп-ногу (половина слота), вторая половина — спот-хедж на Hyperliquid L1
        "expected_apr": round(abs(c["current_apr"]) / 2.0, 2),
        "accrued_rewards_usd": 0.0,
        "adverse_loss_usd": entry_fee,
        "adverse_ticks": 0,
        "ai_audit": audit,
    }, audit


def init_portfolio(deposit_usd: float = 5000.0) -> Dict[str, Any]:
    """Инициализирует портфель на $5,000 USDC и распределяет слоты после проверки Venice AI."""
    log_event(f"Инициализация портфеля Paper-Trading на сумму ${deposit_usd:,.2f} USDC...")
    markets = fetch_polymarket_markets()
    reward_candidates = scan_liquidity_rewards(markets, deposit_usd=deposit_usd / 5.0, min_safety_score=75)
    bonding_candidates = scan_smart_bonding(markets, deposit_usd=deposit_usd / 5.0)
    hl_candidates = [a for a in scan_funding_anomalies(require_hl_spot_hedge=True) if a["same_sign_pct_72h"] >= 60.0]

    positions: List[Dict[str, Any]] = []
    slot_size = deposit_usd / 5.0
    now = time.time()

    # Слоты 1-3: Polymarket Liquidity Rewards ($3,000)
    for item in reward_candidates:
        if len([p for p in positions if p["type"] == "poly_mm"]) >= 3:
            break
        pos, _ = _open_mm_slot(item, slot_size, now)
        if pos:
            positions.append(pos)

    # Слот 4: Polymarket Smart Bonding ($1,000)
    for b in bonding_candidates:
        pos, _ = _open_bond_slot(b, slot_size, now)
        if pos:
            positions.append(pos)
            break

    # Слот 5: Hyperliquid Funding Anomaly ($1,000) — только со спот-хеджем на Hyperliquid и после аудита ИИ
    for c in hl_candidates[:3]:
        pos, _ = _open_hl_slot(c, slot_size, now)
        if pos:
            positions.append(pos)
            log_event(f"Открыт слот Hyperliquid {c['coin']} (списана комиссия входа -${pos['adverse_loss_usd']:.2f}).")
            break

    state = {
        "schema": 4,
        "started_at": now,
        "started_at_iso": datetime.now(timezone.utc).isoformat(),
        "last_tick_at": now,
        "initial_deposit_usd": deposit_usd,
        "ticks_count": 0,
        "positions": positions,
    }
    save_state(state)
    return state


def refill_open_slots(
    state: Dict[str, Any],
    markets: List[Dict[str, Any]],
    poly_ok: bool,
    hl_ok: bool,
) -> None:
    """Перераспределяет освободившийся капитал в новые слоты, если закрылись старые позиции."""
    deposit = float(state["initial_deposit_usd"])
    slot_size = deposit / 5.0
    open_pos = [p for p in state["positions"] if is_open(p)]
    if len(open_pos) >= 5:
        return

    now = time.time()
    # Не открываем повторно рынки, которые уже открыты или были закрыты за последние 12 часов
    blocked_slugs = {
        p.get("slug")
        for p in state["positions"]
        if p.get("slug") and (is_open(p) or now - float(p.get("closed_at", 0)) < REOPEN_COOLDOWN_SEC)
    }
    blocked_coins = {
        p.get("coin")
        for p in state["positions"]
        if p.get("coin") and (is_open(p) or now - float(p.get("closed_at", 0)) < REOPEN_COOLDOWN_SEC)
    }

    open_mm = len([p for p in open_pos if p["type"] == "poly_mm"])
    open_bond = len([p for p in open_pos if p["type"] == "poly_bond"])
    open_hl = len([p for p in open_pos if p["type"] == "hl_funding"])

    if entry_budget_exhausted():
        return  # лимит ИИ на новые входы на сегодня исчерпан — даже не сканируем рынки

    def try_candidates(
        candidates: List[Dict[str, Any]],
        opener: Callable[[Dict[str, Any], float, float], Tuple[Optional[Dict[str, Any]], Dict[str, Any]]],
        is_blocked: Callable[[Dict[str, Any]], bool],
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Перебирает кандидатов одной стратегии, делая не больше REFILL_FRESH_AUDITS_PER_STRATEGY свежих аудитов ИИ.
        Кэшированные вердикты бесплатны и в лимит не входят. Возвращает (открытая позиция, продолжать ли с другими стратегиями)."""
        fresh = 0
        for cand in candidates:
            if is_blocked(cand):
                continue
            pos, audit = opener(cand, slot_size, now)
            if pos:
                return pos, True
            if audit.get("budget_exhausted") or audit.get("emergency_mode"):
                return None, False  # лимит на входы исчерпан или нет связи с Venice — до следующего тика
            if float(audit.get("audited_at") or 0.0) >= now:
                fresh += 1
                if fresh >= REFILL_FRESH_AUDITS_PER_STRATEGY:
                    break
        return None, True

    # За один тик открываем не более 1 новой позиции и делаем не более 1 свежего аудита на стратегию
    if poly_ok and open_mm < 3:
        pos, go_on = try_candidates(
            scan_liquidity_rewards(markets, deposit_usd=slot_size, min_safety_score=75),
            _open_mm_slot,
            lambda c: c["slug"] in blocked_slugs,
        )
        if pos:
            state["positions"].append(pos)
            log_event(f"♻️ Перераспределение капитала: открыт новый MM-слот [{pos['title'][:45]}] с квадратичным APR {pos['expected_apr']:.1f}%.")
            send_telegram_alert(
                f"♻️ <b>Открыт новый MM-слот ($1,000)</b>\n"
                f"Рынок: <i>{esc(pos['title'])}</i>\n"
                f"Честный квадратичный APR (CLOB): <code>{pos['expected_apr']:.1f}%</code>"
            )
            return
        if not go_on:
            return

    if poly_ok and open_bond < 1:
        pos, go_on = try_candidates(
            scan_smart_bonding(markets, deposit_usd=slot_size),
            _open_bond_slot,
            lambda b: b["slug"] in blocked_slugs,
        )
        if pos:
            state["positions"].append(pos)
            log_event(f"♻️ Перераспределение капитала: открыт новый бонд [{pos['title'][:45]}] по ${pos['entry_price']:.4f}.")
            send_telegram_alert(
                f"♻️ <b>Открыт новый бонд-слот ($1,000)</b>\n"
                f"Рынок: <i>{esc(pos['title'])}</i>\n"
                f"Вход: <code>{pos['entry_price']:.4f}</code> (APR к погашению: <code>{pos['expected_apr']:.1f}%</code>)"
            )
            return
        if not go_on:
            return

    if hl_ok and open_hl < 1:
        try:
            hl_cands = [a for a in scan_funding_anomalies(require_hl_spot_hedge=True) if a["same_sign_pct_72h"] >= 60.0]
        except Exception:
            hl_cands = []
        pos, _ = try_candidates(hl_cands[:3], _open_hl_slot, lambda c: c["coin"] in blocked_coins)
        if pos:
            state["positions"].append(pos)
            log_event(f"♻️ Перераспределение капитала: открыт новый фандинг-слот Hyperliquid {pos['coin']} (APR {pos['expected_apr']:.1f}%).")
            send_telegram_alert(
                f"♻️ <b>Открыт новый фандинг-слот ($1,000)</b>\n"
                f"Позиция: <i>{esc(pos['title'])}</i>\n"
                f"Чистый APR на капитал: <code>{pos['expected_apr']:.1f}%</code>"
            )


def migrate_state(
    state: Dict[str, Any],
    by_slug: Dict[str, Dict[str, Any]],
    by_q: Dict[str, Dict[str, Any]],
) -> None:
    """Приводит портфель, созданный старой версией, к текущей схеме (идемпотентно)."""
    cur_schema = int(state.get("schema", 1))
    if cur_schema < 2:
        for pos in state["positions"]:
            pos.setdefault("status", "OPEN")
            pos.setdefault("opened_at", state.get("started_at"))
            if pos["type"] == "poly_bond":
                if float(pos.get("accrued_rewards_usd", 0.0)):
                    log_event(f"Миграция: у бонда {pos['title'][:40]} списаны фиктивные APR-реварды ${pos['accrued_rewards_usd']:.4f}.")
                pos["accrued_rewards_usd"] = 0.0
                pos.setdefault("shares", round(float(pos["allocated_usd"]) / float(pos["entry_price"]), 4))
                pos.setdefault("realized_pnl_usd", 0.0)
                pos.setdefault("unrealized_pnl_usd", 0.0)
                if not pos.get("slug"):
                    m = by_q.get(pos["title"].split(" [")[0])
                    if m and m.get("slug"):
                        pos["slug"] = m["slug"]
            elif pos["type"] == "hl_funding":
                pos.setdefault("adverse_ticks", 0)
        state["schema"] = 2

    if int(state.get("schema", 2)) < 3:
        # Схема 3: перевод всех poly_mm слотов с линейной оценки на квадратичную формулу по живому стакану CLOB
        # и привязка спот-хеджа на Hyperliquid L1 для hl_funding
        try:
            spot_map = fetch_hl_spot_hedge_map(min_spot_vol_usd=100_000.0)
        except Exception:
            spot_map = {}

        for pos in state["positions"]:
            if pos["type"] == "poly_mm":
                m = by_slug.get(pos.get("slug")) or by_q.get(pos.get("title"))
                if m is None and pos.get("slug"):
                    try:
                        m = fetch_market_by_slug(pos["slug"])
                    except Exception:
                        m = None
                if m:
                    tids = parse_clob_token_ids(m)
                    if tids:
                        pos["token_id"] = tids[0]
                    rewards = m.get("clobRewards") or []
                    pos["daily_reward_pool_usd"] = sum(float(r.get("rewardsDailyRate") or 0.0) for r in rewards)
                    pos["min_order_size"] = float(m.get("rewardsMinSize") or 0.0)
                    if pos.get("token_id"):
                        try:
                            book = fetch_clob_book(pos["token_id"])
                            qm = compute_quadratic_mm_metrics(
                                book=book,
                                daily_reward_usd=pos["daily_reward_pool_usd"],
                                max_spread_cents=float(pos["max_spread_cents"]),
                                min_size=pos["min_order_size"],
                                deposit_usd=float(pos["allocated_usd"]),
                                spread_fraction=MM_SPREAD_FRACTION,
                            ) if book else None
                            if qm:
                                old_apr = max(0.01, float(pos.get("expected_apr", 1.0)))
                                new_apr = qm["quadratic_apr"]
                                old_rew = float(pos.get("accrued_rewards_usd", 0.0))
                                new_rew = round(old_rew * (new_apr / old_apr), 4)
                                pos["expected_apr"] = new_apr
                                pos["accrued_rewards_usd"] = new_rew
                                pos["book_spread"] = qm["book_spread"]
                                pos["book_quadratic_score"] = qm["book_q"]
                                log_event(
                                    f"Миграция CLOB [{pos['title'][:38]}]: линейный APR {old_apr:.1f}% -> "
                                    f"честный квадратичный APR {new_apr:.1f}% (реварды пересчитаны ${old_rew:.4f} -> ${new_rew:.4f})."
                                )
                        except Exception as exc:
                            log_event(f"⚠️ Ошибка миграции CLOB для {pos['title'][:35]}: {exc}")

            elif pos["type"] == "hl_funding" and is_open(pos):
                coin = pos.get("coin", "")
                if spot_map:
                    if pos.get("direction") != "SHORT" or coin not in spot_map:
                        close_position(pos, "CLOSED_RULE", f"у актива {coin} нет ликвидного спот-хеджа на Hyperliquid L1")
                    else:
                        spot = spot_map[coin]
                        pos["spot_hedge"] = spot
                        pos["title"] = (
                            f"Hyperliquid {coin} (SHORT Perp + LONG {spot['spot_symbol']} Spot {spot['spot_pair_id']})"
                        )
        state["schema"] = 3

    if int(state.get("schema", 3)) < 4:
        # Схема 4: исполнение лимиток по ленте сделок, пауза после исполнения, демпфированное начисление ревардов
        for pos in state["positions"]:
            if pos["type"] != "poly_mm" or not is_open(pos):
                continue
            m = by_slug.get(pos.get("slug")) or by_q.get(pos.get("title"))
            if m is None and pos.get("slug"):
                try:
                    m = fetch_market_by_slug(pos["slug"])
                except Exception:
                    m = None
            if m:
                pos["condition_id"] = m.get("conditionId", "")
                pos["end_date"] = m.get("endDate") or ""
            pos.setdefault("quote_anchor_mid", pos.get("current_mid"))
            pos.setdefault("last_mm_check_at", state.get("last_tick_at"))
            pos["reward_apr_ref"] = min(float(pos.get("expected_apr", 0.0)), MM_MAX_ACCRUAL_APR)
            pos["expected_apr"] = round(pos["reward_apr_ref"], 2)
        state["schema"] = 4


def _log_throttled(pos: Dict[str, Any], key: str, msg: str, now: float) -> None:
    stamps = pos.setdefault("log_throttle", {})
    if now - float(stamps.get(key, 0.0)) >= LOG_THROTTLE_SEC:
        stamps[key] = now
        log_event(msg)


def _log_quotes(pos: Dict[str, Any], ts: float, bid: Optional[float], ask: Optional[float]) -> None:
    """История лимиток [момент, bid, ask] для сверки с запоздавшими сделками; None — лимиток в стакане нет."""
    history = pos.setdefault("quote_log", [])
    history.append([ts, bid, ask])
    cutoff = ts - MM_TRADE_LOOKBACK_SEC - 600
    while len(history) > 1 and history[1][0] <= cutoff:  # последнее состояние до окна сохраняем
        history.pop(0)


def _quotes_at(pos: Dict[str, Any], ts: float) -> Optional[Tuple[float, float]]:
    """Какие лимитки стояли в момент ts (None — их не было или слот тогда еще не котировал)."""
    state: Optional[Tuple[float, float]] = None
    for placed_at, bid, ask in pos.get("quote_log", []):
        if placed_at > ts:
            break
        state = (bid, ask) if bid is not None else None
    return state


def _place_quotes(pos: Dict[str, Any], mid: float, now: float) -> None:
    """Выставляет двусторонние лимитки вокруг mid и запоминает цену выставления для Kill-Switch."""
    half_c = (float(pos["max_spread_cents"]) / 100.0) * MM_SPREAD_FRACTION
    pos["bid_limit"] = max(0.01, round(mid - half_c, 4))
    pos["ask_limit"] = min(0.99, round(mid + half_c, 4))
    pos["quote_anchor_mid"] = mid
    _log_quotes(pos, now, pos["bid_limit"], pos["ask_limit"])


def _accrue_mm_rewards(pos: Dict[str, Any], cur_apr: float, elapsed_sec: float) -> None:
    """Начисляет реварды за окно, в котором лимитки простояли без исполнения.
    Берется меньший из APR на начало и конец окна; рост APR ограничен (вдвое за час) и есть потолок 365%."""
    prev = float(pos.get("reward_apr_ref", min(float(pos.get("expected_apr", 0.0)), MM_MAX_ACCRUAL_APR)))
    damped = min(cur_apr, max(prev, 0.01) * MM_APR_MAX_RISE_PER_HOUR ** (elapsed_sec / 3600.0), MM_MAX_ACCRUAL_APR)
    apr = min(prev, damped)
    pos["reward_apr_ref"] = round(damped, 4)
    pos["expected_apr"] = round(damped, 2)
    gain = float(pos["allocated_usd"]) * (apr / 100.0) * elapsed_sec / (365.0 * 86400.0)
    pos["accrued_rewards_usd"] = round(float(pos["accrued_rewards_usd"]) + gain, 6)


def _handle_mm_fill(
    pos: Dict[str, Any],
    fill_ts: float,
    bid: float,
    ask: float,
    bid_hit: bool,
    ask_hit: bool,
    cross_prices: List[float],
    mid: float,
    now: float,
) -> None:
    """Лимитку исполнили в момент fill_ts (узнали о ней сейчас — лента запаздывает).
    Инвентарь закрывается консервативно: по худшей из цен — текущий mid или цена пробившей сделки — плюс проскальзывание."""
    alloc = float(pos["allocated_usd"])
    shares = alloc / max(0.20, bid + (1.0 - ask))
    slip = max(float(pos.get("book_spread", 0.01)) / 2.0, 0.008)
    if bid_hit and ask_hit:
        pnl, what = shares * (ask - bid), f"обе лимитки ({bid:.4f} и {ask:.4f})"
    elif bid_hit:
        pnl, what = shares * ((min([mid] + cross_prices) - slip) - bid), f"покупка по {bid:.4f}"
    else:
        pnl, what = shares * (ask - (max([mid] + cross_prices) + slip)), f"продажа по {ask:.4f}"
    pnl = round(pnl, 4)
    if pnl < 0:
        pos["adverse_loss_usd"] = round(float(pos["adverse_loss_usd"]) - pnl, 4)
    else:
        pos["realized_pnl_usd"] = round(float(pos.get("realized_pnl_usd", 0.0)) + pnl, 4)

    # Реварды, начисленные между исполнением и его появлением в ленте, списываем: лимитки уже не стояли
    lag = max(0.0, now - fill_ts)
    clawback = alloc * (min(float(pos.get("reward_apr_ref", 0.0)), MM_MAX_ACCRUAL_APR) / 100.0) * lag / (365.0 * 86400.0)
    pos["accrued_rewards_usd"] = round(max(0.0, float(pos["accrued_rewards_usd"]) - clawback), 6)

    # С момента исполнения лимиток в стакане нет: более поздние состояния истории (перестановки) отбрасываем
    pos["quote_log"] = [q for q in pos.get("quote_log", []) if q[0] <= fill_ts] + [[fill_ts, None, None]]
    fills = [t for t in pos.get("fill_times", []) if fill_ts - float(t) < MM_FILL_WINDOW_SEC] + [fill_ts]
    pos["fill_times"] = fills
    pos["rebalances_count"] = int(pos.get("rebalances_count", 0)) + 1
    pos["current_mid"] = mid
    fill_hm = datetime.fromtimestamp(fill_ts, timezone.utc).strftime("%H:%M:%S")
    log_event(
        f"⚠️ ИСПОЛНЕНИЕ ЛИМИТКИ [{pos['title'][:40]}]: по ленте сделок — {what} в {fill_hm} UTC "
        f"(лента показала через {lag:.0f} с), рынок {mid:.4f}; инвентарь {shares:,.0f} акций закрыт, результат ${pnl:+.2f}."
    )
    if len(fills) >= MM_FILLS_TO_CLOSE:
        close_position(pos, "CLOSED_RULE", f"{len(fills)} исполнения лимиток за сутки — рынок слишком нервный для MM")
        return
    pos["mm_paused_until"] = max(fill_ts + MM_FILL_PAUSE_SEC, now + MM_POLL_SEC)
    resume_at = datetime.fromtimestamp(pos["mm_paused_until"], timezone.utc).strftime("%H:%M")
    send_telegram_alert(
        f"⚠️ <b>Исполнена лимитка Polymarket</b>\n"
        f"Рынок: <i>{esc(pos['title'])}</i>\n"
        f"Сделка: {esc(what)} в {fill_hm} UTC, рынок сейчас <code>{mid:.4f}</code>\n"
        f"Результат закрытия инвентаря: <b>${pnl:+.2f} USDC</b>\n"
        f"Котирование на паузе до <b>{resume_at} UTC</b>"
    )


def mm_quote_step(pos: Dict[str, Any], now: float) -> None:
    """Проверка MM-слота: исполнение лимиток по ленте сделок (с запасом на ее задержку и сверкой с историей лимиток),
    Kill-Switch от цены выставления, начисление ревардов."""
    window_start = float(pos.get("last_mm_check_at") or now)
    elapsed_sec = min(max(0.0, now - window_start), MAX_ACCRUAL_GAP_SEC)
    pos["last_mm_check_at"] = now
    title = pos["title"][:40]

    paused_until = float(pos.get("mm_paused_until") or 0.0)
    if "quote_log" not in pos:
        # Слот открыт до появления истории лимиток: сделки раньше этого момента сверить не с чем
        _log_quotes(pos, now, None if paused_until else pos["bid_limit"], None if paused_until else pos["ask_limit"])
    if paused_until > now:
        return  # лимитки сняты после исполнения — ни риска, ни ревардов

    qm = None
    try:
        book = fetch_clob_book(pos["token_id"]) if pos.get("token_id") else None
        if book:
            qm = compute_quadratic_mm_metrics(
                book=book,
                daily_reward_usd=float(pos.get("daily_reward_pool_usd", 0.0)),
                max_spread_cents=float(pos["max_spread_cents"]),
                min_size=float(pos.get("min_order_size", 0.0)),
                deposit_usd=float(pos["allocated_usd"]),
                spread_fraction=MM_SPREAD_FRACTION,
            )
    except Exception as exc:
        _log_throttled(pos, "book", f"⚠️ CLOB стакан [{title}] недоступен: {exc}", now)

    trades: Optional[List[Dict[str, Any]]] = None
    new_trades: List[Dict[str, Any]] = []
    if pos.get("condition_id"):
        try:
            trades = fetch_recent_trades(pos["condition_id"], now - MM_TRADE_LOOKBACK_SEC)
        except Exception as exc:
            _log_throttled(pos, "trades", f"⚠️ Лента сделок [{title}] недоступна: {exc} — исполнение проверяется по mid.", now)
    if trades is not None:
        if len(trades) >= 500:
            _log_throttled(pos, "trades500", f"⚠️ Лента сделок [{title}]: 500+ сделок за 15 мин, часть могла не попасть в проверку.", now)
        seen = pos.get("seen_trades", {})
        for t in trades:
            key = f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('price')}:{t.get('size')}:{t.get('timestamp')}"
            if key not in seen:
                seen[key] = float(t["timestamp"])
                new_trades.append(t)
        pos["seen_trades"] = {k: ts for k, ts in seen.items() if ts >= now - MM_TRADE_LOOKBACK_SEC - 60}

    if qm:
        mid = qm["mid"]
        pos["book_spread"] = qm["book_spread"]
        pos["book_quadratic_score"] = qm["book_q"]
    elif trades:
        mid = trade_yes_price(trades[0])  # лента отсортирована от новых к старым
    else:
        mid = float(pos["current_mid"])

    if paused_until:
        # Пауза закончилась: возвращаем лимитки, только если стакан снова нормальный; за окно паузы ничего не начисляем
        if not qm:
            _log_throttled(pos, "resume", f"⏸ [{title}] пауза продлена: стакан пуст или спред шире зоны ревардов.", now)
            return
        pos["mm_paused_until"] = 0.0
        _place_quotes(pos, mid, now)
        pos["current_mid"] = mid
        log_event(f"▶️ [{title}] лимитки возвращены после паузы: {pos['bid_limit']:.4f} .. {pos['ask_limit']:.4f}.")
        return

    # Исполнение: новая (ранее не виденная) сделка по цене не хуже лимитки, которая стояла в момент этой сделки
    fill = None
    if trades is not None:
        for t in sorted(new_trades, key=lambda x: float(x["timestamp"])):
            ts = float(t["timestamp"])
            quotes = _quotes_at(pos, ts)
            if not quotes:
                continue
            bid, ask = quotes
            px = trade_yes_price(t)
            if px <= bid or px >= ask:
                same = [trade_yes_price(x) for x in new_trades if float(x["timestamp"]) == ts]
                cross = [q for q in same if q <= bid or q >= ask]
                fill = (ts, bid, ask, any(q <= bid for q in same), any(q >= ask for q in same), cross)
                break
    else:
        bid, ask = float(pos["bid_limit"]), float(pos["ask_limit"])
        if mid <= bid or mid >= ask:
            fill = (now, bid, ask, mid <= bid, mid >= ask, [mid])
    if fill:
        _handle_mm_fill(pos, *fill, mid=mid, now=now)
        return  # за окно с исполнением реварды не начисляются

    if qm:
        _accrue_mm_rewards(pos, qm["quadratic_apr"], elapsed_sec)
    else:
        _log_throttled(pos, "noqm", f"⚠️ [{title}] стакан пуст или спред шире зоны ревардов — реварды не начисляются.", now)

    # Kill-Switch: переставляем лимитки, если цена ушла на ≥1¢ от цены, при которой они выставлены
    anchor = float(pos.get("quote_anchor_mid") or pos["current_mid"])
    if abs(mid - anchor) >= KILL_SWITCH_MOVE:
        _place_quotes(pos, mid, now)
        pos["rebalances_count"] = int(pos.get("rebalances_count", 0)) + 1
        pos["ks_pending"] = int(pos.get("ks_pending", 0)) + 1
        if now - float(pos.get("ks_logged_at", 0.0)) >= LOG_THROTTLE_SEC:
            log_event(
                f"🛡️ KILL-SWITCH [{title}]: лимитки переставлены {pos['ks_pending']} раз(а), "
                f"цена {anchor:.4f} -> {mid:.4f}, коридор {pos['bid_limit']:.4f} .. {pos['ask_limit']:.4f}."
            )
            pos["ks_pending"] = 0
            pos["ks_logged_at"] = now
    pos["current_mid"] = mid


def fast_mm_tick() -> None:
    """Минутная проверка открытых MM-слотов между полными тиками: только стакан и лента сделок, без Gamma API и без ИИ."""
    if not STATE_FILE.exists() or get_ai_health().get("emergency_mode", False):
        return
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    active = [
        p for p in state["positions"]
        if is_open(p) and p["type"] == "poly_mm" and not p.get("mm_defensive_paused", False)
    ]
    if not active:
        return
    now = time.time()
    for pos in active:
        mm_quote_step(pos, now)
    save_state(state)


def maybe_reaudit(
    pos: Dict[str, Any], strategy_type: str, price: float, description: str, outcome_label: str = "YES", end_date: str = ""
) -> bool:
    """Повторный аудит ИИ открытой позиции Polymarket. True — позицию нужно закрыть (два свежих SKIP подряд)."""
    streak = int(pos.get("ai_skip_streak", 0))
    audit = call_venice_market_audit(
        question=pos["title"].split(" [")[0] if pos["type"] == "poly_bond" else pos["title"],
        description=description,
        price_yes=price,
        strategy_type=strategy_type,
        outcome_label=outcome_label,
        cache_ttl_sec=REAUDIT_INTERVAL_SEC if streak == 0 else REAUDIT_RECHECK_SEC,
        end_date=end_date,
    )
    audited_at = audit.get("audited_at")
    if not audited_at or audited_at == pos.get("last_audit_ts"):
        return False  # вердикт из кэша, сбой связи или исчерпан бюджет — новой информации нет

    pos["last_audit_ts"] = audited_at
    pos["ai_audit"] = audit
    if audit["decision"] == "APPROVE":
        if streak:
            log_event(f"🧠 Повторный аудит [{pos['title'][:40]}]: снова APPROVE ({audit['confidence']}%).")
        pos["ai_skip_streak"] = 0
        return False

    pos["ai_skip_streak"] = streak + 1
    log_event(
        f"🧠 Повторный аудит [{pos['title'][:40]}]: SKIP ({audit['confidence']}%), "
        f"{pos['ai_skip_streak']}/{AI_SKIP_STREAK_TO_CLOSE} — {audit['summary']}"
    )
    return pos["ai_skip_streak"] >= AI_SKIP_STREAK_TO_CLOSE


def step_simulation(deposit_usd: float = 5000.0) -> Dict[str, Any]:
    """Выполняет один шаг проверки реального рынка и начисляет честный PnL."""
    if not STATE_FILE.exists():
        return init_portfolio(deposit_usd)

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    now = time.time()
    elapsed_sec = max(1.0, now - float(state.get("last_tick_at", now)))
    if elapsed_sec > MAX_ACCRUAL_GAP_SEC:
        log_event(f"⏸ Пропуск наблюдения {elapsed_sec/3600:.1f} ч — доход начислен только за последний час.")
    elapsed_years = min(elapsed_sec, MAX_ACCRUAL_GAP_SEC) / (365.0 * 86400.0)

    # Пульс связи с Venice (эндпоинт /models, 0 токенов) — держит статус аварийного режима актуальным
    ping_venice_ai()
    ai_health = get_ai_health()
    is_emergency = ai_health.get("emergency_mode", False)

    try:
        markets = fetch_polymarket_markets()
        poly_ok = True
    except Exception as exc:
        log_event(f"⚠️ Polymarket Gamma API недоступен: {exc}. Реварды Polymarket за этот тик не начисляются.")
        markets, poly_ok = [], False
    by_slug = {m.get("slug"): m for m in markets if m.get("slug")}
    by_q = {m.get("question"): m for m in markets if m.get("question")}

    migrate_state(state, by_slug, by_q)

    def find_market(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        m = by_slug.get(pos.get("slug")) or by_q.get(pos["title"].split(" [")[0])
        if m is None and poly_ok and pos.get("slug"):
            try:
                m = fetch_market_by_slug(pos["slug"])  # рынок выпал из топ-250 или уже закрыт
            except Exception as exc:
                log_event(f"⚠️ Не удалось загрузить рынок {pos['slug'][:40]}: {exc}")
        if m is None:
            pos["stale_ticks"] = int(pos.get("stale_ticks", 0)) + 1
            log_event(f"⚠️ Нет актуальных данных по [{pos['title'][:40]}] — тик {pos['stale_ticks']} без начислений.")
        else:
            pos["stale_ticks"] = 0
            if not pos.get("slug") and m.get("slug"):
                pos["slug"] = m["slug"]
        return m

    # Свежие ставки Hyperliquid; None — данных нет, начисления по фандингу пропускаются
    try:
        meta_ctx = hl_request({"type": "metaAndAssetCtxs"})
        hl_map: Optional[Dict[str, float]] = {
            u["name"]: float(c.get("funding") or 0.0) * 24.0 * 365.0 * 100.0
            for u, c in zip(meta_ctx[0]["universe"], meta_ctx[1])
        }
    except Exception as exc:
        log_event(f"⚠️ Hyperliquid API недоступен: {exc}. Фандинг за этот тик не начисляется.")
        hl_map = None

    for pos in state["positions"]:
        if not is_open(pos):
            continue
        alloc = float(pos["allocated_usd"])

        if pos["type"] == "poly_mm":
            m = find_market(pos)
            if m is not None:
                if market_trading_finished(m):
                    close_position(pos, "RESOLVED", "рынок закрыт или истек — лимитки сняты")
                    continue
                pos["end_date"] = m.get("endDate") or pos.get("end_date", "")
                days_left = days_until(pos["end_date"])
                if days_left is not None and days_left < MM_MIN_DAYS_TO_END_HOLD:
                    close_position(
                        pos, "CLOSED_RULE",
                        f"до истечения рынка {max(days_left, 0.0):.1f} дн. (< {MM_MIN_DAYS_TO_END_HOLD:.0f}) — MM снят до развязки",
                    )
                    continue
                if not pos.get("token_id"):
                    tids = parse_clob_token_ids(m)
                    if tids:
                        pos["token_id"] = tids[0]
                pos["condition_id"] = m.get("conditionId") or pos.get("condition_id", "")
                rewards = m.get("clobRewards") or []
                daily_pool = sum(float(r.get("rewardsDailyRate") or 0.0) for r in rewards)
                pos["daily_reward_pool_usd"] = daily_pool
                pos["min_order_size"] = float(m.get("rewardsMinSize") or 0.0)
                if daily_pool <= 0.0:
                    close_position(pos, "CLOSED_RULE", "пул дневных ревардов clobRewards обнулился")
                    continue

            if is_emergency:
                # В аварийном режиме (нет связи с Venice AI) лимитки сняты: окно паузы не начисляется
                if not pos.get("mm_defensive_paused", False):
                    pos["mm_defensive_paused"] = True
                    _log_quotes(pos, now, None, None)
                    log_event(f"🛡️ [АВАРИЙНЫЙ РЕЖИМ] {pos['title'][:35]}: лимитки сняты для защиты капитала.")
                pos["last_mm_check_at"] = now
                continue
            if pos.get("mm_defensive_paused", False):
                pos["mm_defensive_paused"] = False
                pos["mm_paused_until"] = max(float(pos.get("mm_paused_until") or 0.0), now - 1.0)  # вернуть лимитки через проверку стакана
                log_event(f"✅ [ШТАТНЫЙ РЕЖИМ] {pos['title'][:35]}: связь с ИИ восстановлена, лимитки вернутся после проверки стакана.")

            if m is not None and maybe_reaudit(
                pos, "polymarket_liquidity_maker", float(pos["current_mid"]), m.get("description") or "", "YES", pos.get("end_date", "")
            ):
                close_position(pos, "CLOSED_AI", "повторный аудит ИИ дважды подряд вернул SKIP — лимитки сняты")
                continue

            mm_quote_step(pos, now)

        elif pos["type"] == "poly_bond":
            m = find_market(pos)
            if m is None:
                continue
            outcome = pos.get("outcome", "Yes")
            cur_price = outcome_price(m, outcome, pos["current_price"])
            shares = float(pos["shares"])
            pos["current_price"] = cur_price

            if m.get("closed"):
                payout = 1.0 if cur_price >= 0.5 else 0.0
                pos["unrealized_pnl_usd"] = round(shares * (payout - float(pos["entry_price"])), 4)
                close_position(pos, "RESOLVED", f"рынок разрешен, выплата ${payout:.2f} за акцию")
                continue

            # Переоценка по рынку (mark-to-market) вместо линейного APR
            pos["unrealized_pnl_usd"] = round(shares * (cur_price - float(pos["entry_price"])), 4)

            # Stop-Loss действует всегда, а не только в аварийном режиме
            if cur_price < BOND_STOP_LOSS:
                close_position(pos, "STOPPED_OUT", f"цена исхода упала до {cur_price:.4f} (< {BOND_STOP_LOSS:.2f}) — бонд продан")
                continue

            if not is_emergency and maybe_reaudit(
                pos, "polymarket_smart_bonding", cur_price, m.get("description") or "", outcome, m.get("endDate") or ""
            ):
                close_position(pos, "CLOSED_AI", f"повторный аудит ИИ дважды подряд вернул SKIP — бонд продан по {cur_price:.4f}")
                continue

        elif pos["type"] == "hl_funding":
            if hl_map is None:
                continue
            coin = pos["coin"]
            if coin not in hl_map:
                pos["adverse_loss_usd"] = round(pos["adverse_loss_usd"] + alloc * HL_FEE_RATE, 4)
                close_position(pos, "CLOSED_RULE", f"{coin} больше не торгуется на Hyperliquid")
                continue
            cur_apr = hl_map[coin]
            # Положительное значение — фандинг платят нам; начисляется на перп-ногу (половина слота)
            received_apr = cur_apr if pos["direction"] == "SHORT" else -cur_apr
            effective_apr = received_apr / 2.0
            pos["expected_apr"] = round(effective_apr, 2)
            pos["last_funding_apr"] = round(cur_apr, 2)
            funding_delta = alloc * (effective_apr / 100.0) * elapsed_years
            if funding_delta >= 0:
                pos["accrued_rewards_usd"] = round(pos["accrued_rewards_usd"] + funding_delta, 4)
            else:
                pos["adverse_loss_usd"] = round(pos["adverse_loss_usd"] + abs(funding_delta), 4)

            # Funding Auto-Halt: знак фандинга развернулся против позиции и держится
            pos["adverse_ticks"] = int(pos.get("adverse_ticks", 0)) + 1 if received_apr < 0 else 0
            if pos["adverse_ticks"] >= HL_ADVERSE_TICKS_TO_CLOSE:
                pos["adverse_loss_usd"] = round(pos["adverse_loss_usd"] + alloc * HL_FEE_RATE, 4)
                close_position(pos, "CLOSED_RULE", f"фандинг {cur_apr:+.1f}% годовых против позиции {pos['adverse_ticks']} тика подряд")
                continue

    # Если есть закрытые слоты и свободный капитал — автоматически подбираем замену
    if not is_emergency:
        refill_open_slots(state, markets, poly_ok, hl_map is not None)

    state["last_tick_at"] = now
    state["ticks_count"] = int(state.get("ticks_count", 0)) + 1
    state["emergency_mode"] = is_emergency
    save_state(state)

    t = portfolio_totals(state)
    log_event(
        f"Тик #{state['ticks_count']} завершен | Реварды/фандинг: +${t['rewards']:.2f} | "
        f"Издержки: -${t['losses']:.2f} | Переоценка/реализовано: ${t['unrealized'] + t['realized']:+.2f} | "
        f"Чистый PnL: ${t['net_pnl']:+.2f} USDC | Открыто слотов: {t['open_count']}"
    )
    return state


def print_status() -> None:
    """Выводит подробный отчет о текущем состоянии виртуального портфеля $5,000."""
    if not STATE_FILE.exists():
        print("Портфель еще не инициализирован. Запустите: python3 paper_trader.py --once")
        return

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    t = portfolio_totals(state)
    deposit = t["deposit"]
    elapsed_hours = max(0.01, (time.time() - float(state["started_at"])) / 3600.0)

    print("=" * 90)
    print(f"📊 ОТЧЕТ ПОРТФЕЛЯ ИИ-СИМУЛЯЦИИ (Старт: {state['started_at_iso'][:19]} UTC | В работе: {elapsed_hours:.2f} ч)")
    print("=" * 90)
    print(f"💰 Стартовый депозит:   ${deposit:,.2f} USDC")
    print(f"📈 Реварды и фандинг:  +${t['rewards']:,.4f} USDC")
    print(f"📉 Комиссии и пробои:  -${t['losses']:,.4f} USDC")
    print(f"⚖️  Переоценка бондов:   ${t['unrealized']:+,.4f} | Реализовано: ${t['realized']:+,.4f} USDC")
    print(f"💵 Текущий баланс:      ${t['equity']:,.4f} USDC (Чистый PnL: ${t['net_pnl']:+.4f} USDC)")
    print(f"🔥 Средневзвешенный APR открытых слотов (CLOB Quadratic): {t['weighted_apr']:.1f}% годовых (~${deposit*(t['weighted_apr']/100)/365:.2f}/день)")
    print(f"💤 Свободный капитал: ${t['idle_usd']:,.0f}")
    print("-" * 90)
    for i, p in enumerate(state["positions"], 1):
        status = p.get("status", "OPEN")
        print(f"{i}. [{p['type'].upper()}] [{status}] {p['title']}")
        print(
            f"   • Выделено: ${p['allocated_usd']:,.0f} | Текущий APR слота: {p['expected_apr']:.1f}% | "
            f"Итог слота: ${position_net_pnl(p):+.4f}"
        )
        if p["type"] == "poly_mm" and status == "OPEN":
            print(
                f"   • Текущая цена: {p['current_mid']:.4f} | Наши лимитки: Bid {p['bid_limit']:.4f} .. Ask {p['ask_limit']:.4f} "
                f"| Конкуренция стакана (Q): {p.get('book_quadratic_score', 0):,.0f} | Перестановок: {p['rebalances_count']}"
            )
            if float(p.get("mm_paused_until") or 0.0) > time.time():
                until = datetime.fromtimestamp(float(p["mm_paused_until"]), timezone.utc).strftime("%H:%M")
                print(f"   • ⏸ Пауза после исполнения лимитки до {until} UTC (исполнений за сутки: {len(p.get('fill_times', []))})")
        elif p["type"] == "poly_bond":
            print(f"   • Вход: {p['entry_price']:.4f} | Сейчас: {p['current_price']:.4f} | Акций: {p.get('shares', 0):.2f}")
        elif p["type"] == "hl_funding" and p.get("spot_hedge"):
            sh = p["spot_hedge"]
            print(f"   • Хедж: LONG {sh.get('spot_symbol')} Spot ({sh.get('spot_pair_id')}, 24ч объем ${sh.get('spot_vol_24h_usd', 0)/1e6:.2f}M)")
        if status != "OPEN":
            print(f"   • Причина закрытия: {p.get('close_reason', '')}")
        print(f"   • Вердикт Venice AI: {p['ai_audit'].get('summary', '')}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper-Trading демон с Venice AI")
    parser.add_argument("--deposit", type=float, default=5000.0)
    parser.add_argument("--interval", type=int, default=900, help="Интервал проверки в секундах (по умолчанию 900с = 15 мин)")
    parser.add_argument("--mm-poll", type=int, default=MM_POLL_SEC, help="Интервал минутной проверки MM-слотов, сек (по умолчанию 60)")
    parser.add_argument("--once", action="store_true", help="Выполнить 1 шаг и выйти")
    parser.add_argument("--status", action="store_true", help="Показать текущий отчет")
    parser.add_argument("--reset", action="store_true", help="Пересоздать портфель заново")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        init_portfolio(args.deposit)
        print_status()
    elif args.once:
        step_simulation(args.deposit)
        print_status()
    else:
        failures = 0
        fast_failures_logged_at = 0.0
        next_full_at = 0.0
        while True:
            if time.time() >= next_full_at:
                next_full_at = time.time() + args.interval
                try:
                    step_simulation(args.deposit)
                    if failures:
                        log_event(f"✅ Тики снова проходят штатно (после {failures} сбоев подряд).")
                        send_telegram_alert(f"✅ <b>Демон симуляции восстановился</b> после {failures} сбоев подряд.")
                    failures = 0
                except Exception as exc:
                    failures += 1
                    log_event(f"❌ Сбой тика ({failures} подряд): {type(exc).__name__}: {exc}")
                    print(traceback.format_exc(), flush=True)
                    if failures in (1, 4):
                        send_telegram_alert(
                            f"❌ <b>Сбой тика симуляции</b> ({failures} подряд)\n"
                            f"<code>{esc(type(exc).__name__)}: {esc(str(exc)[:200])}</code>"
                        )
            else:
                try:
                    fast_mm_tick()
                except Exception as exc:
                    print(traceback.format_exc(), flush=True)
                    if time.time() - fast_failures_logged_at >= LOG_THROTTLE_SEC:
                        fast_failures_logged_at = time.time()
                        log_event(f"⚠️ Сбой минутной проверки MM: {type(exc).__name__}: {exc}")
            time.sleep(max(1.0, min(args.mm_poll, next_full_at - time.time())))
