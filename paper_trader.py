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
    fetch_clob_book,
    fetch_polymarket_markets,
    fetch_market_by_slug,
    parse_clob_token_ids,
    parse_outcome_prices,
    scan_liquidity_rewards,
    scan_smart_bonding,
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
        "daily_reward_pool_usd": item.get("daily_reward_pool_usd", 0.0),
        "min_order_size": item.get("min_order_size", 0.0),
        "allocated_usd": slot_size,
        "entry_mid": mid,
        "current_mid": mid,
        "bid_limit": item["bid_limit"],
        "ask_limit": item["ask_limit"],
        "book_spread": item.get("book_spread", 0.01),
        "book_quadratic_score": item.get("book_quadratic_score", 0.0),
        "max_spread_cents": item["max_spread_cents"],
        # Используем честный квадратичный APR при отступе 0.65 * max_spread против реального стакана CLOB
        "expected_apr": round(float(item["conservative_apr"]), 2),
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
        "schema": 3,
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


def maybe_reaudit(pos: Dict[str, Any], strategy_type: str, price: float, description: str, outcome_label: str = "YES") -> bool:
    """Повторный аудит ИИ открытой позиции Polymarket. True — позицию нужно закрыть (два свежих SKIP подряд)."""
    streak = int(pos.get("ai_skip_streak", 0))
    audit = call_venice_market_audit(
        question=pos["title"].split(" [")[0] if pos["type"] == "poly_bond" else pos["title"],
        description=description,
        price_yes=price,
        strategy_type=strategy_type,
        outcome_label=outcome_label,
        cache_ttl_sec=REAUDIT_INTERVAL_SEC if streak == 0 else REAUDIT_RECHECK_SEC,
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
            if m is None:
                continue
            if market_trading_finished(m):
                close_position(pos, "RESOLVED", "рынок закрыт или истек — лимитки сняты")
                continue

            # Загружаем живой стакан CLOB для точной проверки цены, спреда и квадратичного APR
            if not pos.get("token_id"):
                tids = parse_clob_token_ids(m)
                if tids:
                    pos["token_id"] = tids[0]
            rewards = m.get("clobRewards") or []
            daily_pool = sum(float(r.get("rewardsDailyRate") or 0.0) for r in rewards)
            pos["daily_reward_pool_usd"] = daily_pool
            pos["min_order_size"] = float(m.get("rewardsMinSize") or 0.0)
            if daily_pool <= 0.0:
                close_position(pos, "CLOSED_RULE", "пул дневных ревардов clobRewards обнулился")
                continue

            qm = None
            if pos.get("token_id"):
                try:
                    book = fetch_clob_book(pos["token_id"])
                    if book:
                        qm = compute_quadratic_mm_metrics(
                            book=book,
                            daily_reward_usd=daily_pool,
                            max_spread_cents=float(pos["max_spread_cents"]),
                            min_size=pos["min_order_size"],
                            deposit_usd=alloc,
                            spread_fraction=MM_SPREAD_FRACTION,
                        )
                except Exception as exc:
                    log_event(f"⚠️ CLOB стакан [{pos['title'][:35]}] недоступен: {exc}")

            new_mid = qm["mid"] if qm else outcome_price(m, "Yes", pos["current_mid"])
            hr_move = abs(float(m.get("oneHourPriceChange") or 0.0))
            if qm:
                pos["expected_apr"] = qm["quadratic_apr"]
                pos["book_spread"] = qm["book_spread"]
                pos["book_quadratic_score"] = qm["book_q"]

            if is_emergency:
                if not pos.get("mm_defensive_paused", False):
                    pos["mm_defensive_paused"] = True
                    log_event(
                        f"🛡️ [АВАРИЙНЫЙ РЕЖИМ] {pos['title'][:35]}: лимитки сняты для защиты капитала."
                    )
                pos["current_mid"] = new_mid
                continue

            if pos.get("mm_defensive_paused", False):
                pos["mm_defensive_paused"] = False
                half_c = (pos["max_spread_cents"] / 100.0) * MM_SPREAD_FRACTION
                pos["bid_limit"] = max(0.01, round(new_mid - half_c, 4))
                pos["ask_limit"] = min(0.99, round(new_mid + half_c, 4))
                log_event(
                    f"✅ [ШТАТНЫЙ РЕЖИМ] {pos['title'][:35]}: лимитки возвращены в стакан ({pos['bid_limit']:.4f} .. {pos['ask_limit']:.4f})."
                )

            # Проверяем: не пробила ли реальная цена наши лимитные ордера?
            # При исполнении лимитки маркет-мейкер получает токсичный инвентарь (~shares акций)
            # и несет убыток от гэпа за предел лимитки + половину спреда стакана на выход (adverse selection >= 0.8¢/акцию)
            if new_mid <= pos["bid_limit"] or new_mid >= pos["ask_limit"]:
                unit_cost = max(0.20, float(pos["bid_limit"]) + (1.0 - float(pos["ask_limit"])))
                shares = alloc / unit_cost
                gap = max(pos["bid_limit"] - new_mid, new_mid - pos["ask_limit"], 0.0)
                unwind_slippage = max(float(pos.get("book_spread", 0.01)) / 2.0, 0.008)
                penalty = round(shares * (gap + unwind_slippage), 4)
                pos["adverse_loss_usd"] = round(pos["adverse_loss_usd"] + penalty, 4)
                pos["rebalances_count"] += 1
                half_c = (pos["max_spread_cents"] / 100.0) * MM_SPREAD_FRACTION
                pos["bid_limit"] = max(0.01, round(new_mid - half_c, 4))
                pos["ask_limit"] = min(0.99, round(new_mid + half_c, 4))
                log_event(
                    f"⚠️ ПРОБОЙ КОРИДОРА [{pos['title'][:40]}]: цена {new_mid:.4f}, списан adverse-убыток -${penalty:.2f}, ордера переставлены."
                )
                send_telegram_alert(
                    f"⚠️ <b>Пробой коридора Polymarket!</b>\n"
                    f"Рынок: <i>{esc(pos['title'])}</i>\n"
                    f"Новая цена: <code>{new_mid:.4f}</code>\n"
                    f"Списан убыток (adverse selection + спред): <b>-${penalty:.2f} USDC</b>\n"
                    f"Лимитки переставлены: <code>{pos['bid_limit']:.4f} .. {pos['ask_limit']:.4f}</code>"
                )
            elif hr_move >= KILL_SWITCH_MOVE or abs(new_mid - pos["current_mid"]) >= KILL_SWITCH_MOVE:
                # Правило защиты до пробоя лимиток (порог движения цены, без вызова ИИ)
                pos["rebalances_count"] += 1
                half_c = (pos["max_spread_cents"] / 100.0) * MM_SPREAD_FRACTION
                pos["bid_limit"] = max(0.01, round(new_mid - half_c, 4))
                pos["ask_limit"] = min(0.99, round(new_mid + half_c, 4))
                log_event(
                    f"🛡️ KILL-SWITCH [{pos['title'][:40]}]: движение цены ({pos['current_mid']:.4f} -> {new_mid:.4f}), лимитки сдвинуты."
                )
                send_telegram_alert(
                    f"🛡️ <b>Сработал Kill-Switch!</b>\n"
                    f"Рынок: <i>{esc(pos['title'])}</i>\n"
                    f"Цена сдвинулась: <code>{pos['current_mid']:.4f} -&gt; {new_mid:.4f}</code>\n"
                    f"Лимитки сдвинуты до пробоя коридора."
                )
            pos["current_mid"] = new_mid

            if maybe_reaudit(pos, "polymarket_liquidity_maker", new_mid, m.get("description") or ""):
                close_position(pos, "CLOSED_AI", "повторный аудит ИИ дважды подряд вернул SKIP — лимитки сняты")
                continue

            # Начисляем Liquidity Rewards по честному квадратичному APR из живого CLOB стакана
            reward_gain = alloc * (float(pos["expected_apr"]) / 100.0) * elapsed_years
            pos["accrued_rewards_usd"] = round(pos["accrued_rewards_usd"] + reward_gain, 4)

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

            if not is_emergency and maybe_reaudit(pos, "polymarket_smart_bonding", cur_price, m.get("description") or "", outcome):
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
        while True:
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
            time.sleep(args.interval)
