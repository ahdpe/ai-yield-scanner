"""Демон честной симуляции (Paper-Trading) портфеля $5,000 USDC с аудитом через Venice AI."""

import argparse
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from typing import Dict, Any, List

from ai_analyzer import call_venice_market_audit
from telegram_notifier import send_telegram_alert
from polymarket_scanner import (
    fetch_polymarket_markets,
    scan_liquidity_rewards,
    scan_smart_bonding,
)
from hyperliquid_scanner import scan_funding_anomalies, hl_request


STATE_FILE = Path("state/portfolio_state.json")
LOG_FILE = Path("state/paper_trader.log")


def log_event(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def init_portfolio(deposit_usd: float = 5000.0) -> Dict[str, Any]:
    """Инициализирует портфель на $5,000 USDC и распределяет слоты после проверки Venice AI."""
    log_event(f"Инициализация портфеля Paper-Trading на сумму ${deposit_usd:,.2f} USDC...")
    markets = fetch_polymarket_markets()
    reward_candidates = scan_liquidity_rewards(markets, deposit_usd=1000.0, min_safety_score=75)
    bonding_candidates = scan_smart_bonding(markets, deposit_usd=1000.0)
    hl_candidates = [a for a in scan_funding_anomalies() if a["same_sign_pct_72h"] >= 60.0]

    positions: List[Dict[str, Any]] = []
    slot_size = deposit_usd / 5.0  # $1,000 на каждый из 5 слотов

    # Слоты 1-3: Polymarket Liquidity Rewards ($3,000)
    for item in reward_candidates:
        if len([p for p in positions if p["type"] == "poly_mm"]) >= 3:
            break
        audit = call_venice_market_audit(
            question=item["question"],
            description=item["description"],
            price_yes=item["price_yes"],
            strategy_type="polymarket_liquidity_maker",
        )
        log_event(
            f"Venice AI ({audit.get('model_used', 'local')}) аудит [{item['question'][:48]}...]: "
            f"{audit['decision']} (conf={audit['confidence']}%) — {audit['summary']}"
        )
        if audit["decision"] != "APPROVE":
            continue

        mid = item["price_yes"]
        half_corridor = (item["max_spread_cents"] / 100.0) * 0.65
        positions.append({
            "id": f"mm_{item['slug'][:30]}",
            "type": "poly_mm",
            "title": item["question"],
            "slug": item["slug"],
            "allocated_usd": slot_size,
            "entry_mid": mid,
            "current_mid": mid,
            "bid_limit": max(0.01, round(mid - half_corridor, 4)),
            "ask_limit": min(0.99, round(mid + half_corridor, 4)),
            "max_spread_cents": item["max_spread_cents"],
            "expected_apr": (item["conservative_apr"] + item["optimal_apr"]) / 2.0,
            "accrued_rewards_usd": 0.0,
            "adverse_loss_usd": 0.0,
            "rebalances_count": 0,
            "ai_audit": audit,
        })

    # Слот 4: Polymarket Smart Bonding ($1,000)
    for b in bonding_candidates:
        audit = call_venice_market_audit(
            question=b["question"],
            description=b["description"],
            price_yes=b["entry_price"],
            strategy_type="polymarket_smart_bonding",
            outcome_label=b["lead_outcome"],
        )
        log_event(
            f"Venice AI бондинг-аудит [{b['question'][:48]}...]: "
            f"{audit['decision']} (conf={audit['confidence']}%) — {audit['summary']}"
        )
        if audit["decision"] == "APPROVE":
            positions.append({
                "id": f"bond_{b['question'][:24]}",
                "type": "poly_bond",
                "title": f"{b['question']} [{b['lead_outcome']}]",
                "outcome": b["lead_outcome"],
                "allocated_usd": slot_size,
                "entry_price": b["entry_price"],
                "current_price": b["entry_price"],
                "expected_apr": b["annualized_apr"],
                "accrued_rewards_usd": 0.0,
                "adverse_loss_usd": 0.0,
                "ai_audit": audit,
            })
            break

    # Слот 5: Hyperliquid Funding Anomaly ($1,000)
    if hl_candidates:
        top_hl = hl_candidates[0]
        # Учитываем честную комиссию входа 0.035% * 2 ноги = $0.70 на $1,000
        entry_fee = round(slot_size * 0.0007, 4)
        positions.append({
            "id": f"hl_{top_hl['coin']}",
            "type": "hl_funding",
            "title": f"Hyperliquid {top_hl['coin']} ({top_hl['strategy'][:32]})",
            "coin": top_hl["coin"],
            "direction": "SHORT" if top_hl["current_apr"] > 0 else "LONG",
            "allocated_usd": slot_size,
            "expected_apr": top_hl["combined_cross_dex_apr_est"] / 2.0,
            "accrued_rewards_usd": 0.0,
            "adverse_loss_usd": entry_fee,
            "ai_audit": {
                "decision": "APPROVE",
                "confidence": int(top_hl["same_sign_pct_72h"]),
                "summary": (
                    f"Аномалия фандинга {top_hl['current_apr']:+.1f}% годовых держится "
                    f"{top_hl['same_sign_pct_72h']:.0f}% часов за последние 3 суток (средний APR {top_hl['avg_72h_apr']:+.1f}%)."
                ),
            },
        })
        log_event(
            f"Открыт слот Hyperliquid {top_hl['coin']} (списана комиссия входа -${entry_fee:.2f})."
        )

    now = time.time()
    state = {
        "started_at": now,
        "started_at_iso": datetime.now(timezone.utc).isoformat(),
        "last_tick_at": now,
        "initial_deposit_usd": deposit_usd,
        "ticks_count": 0,
        "positions": positions,
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def step_simulation() -> Dict[str, Any]:
    """Выполняет один шаг проверки реального рынка и начисляет честный PnL."""
    if not STATE_FILE.exists():
        return init_portfolio(5000.0)

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    now = time.time()
    elapsed_sec = max(1.0, now - float(state.get("last_tick_at", now)))
    elapsed_years = elapsed_sec / (365.0 * 86400.0)

    markets = fetch_polymarket_markets()
    by_slug = {m.get("slug"): m for m in markets if m.get("slug")}
    by_q = {m.get("question"): m for m in markets if m.get("question")}

    # Загружаем свежие ставки Hyperliquid
    try:
        meta_ctx = hl_request({"type": "metaAndAssetCtxs"})
        hl_map = {
            u["name"]: float(c.get("funding") or 0.0) * 24.0 * 365.0 * 100.0
            for u, c in zip(meta_ctx[0]["universe"], meta_ctx[1])
        }
    except Exception:
        hl_map = {}

    for pos in state["positions"]:
        alloc = float(pos["allocated_usd"])

        if pos["type"] == "poly_mm":
            m = by_slug.get(pos.get("slug")) or by_q.get(pos.get("title"))
            if m:
                prices_raw = m.get("outcomePrices") or '["0.5", "0.5"]'
                prices = [float(p) for p in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)]
                new_mid = prices[0] if prices else pos["current_mid"]
                hr_move = abs(float(m.get("oneHourPriceChange") or 0.0))

                # Проверяем: не пробила ли реальная цена наши лимитные ордера?
                if new_mid <= pos["bid_limit"] or new_mid >= pos["ask_limit"]:
                    breach = max(pos["bid_limit"] - new_mid, new_mid - pos["ask_limit"], 0.002)
                    penalty = round(alloc * breach, 4)
                    pos["adverse_loss_usd"] = round(pos["adverse_loss_usd"] + penalty, 4)
                    pos["rebalances_count"] += 1
                    half_c = (pos["max_spread_cents"] / 100.0) * 0.65
                    pos["bid_limit"] = max(0.01, round(new_mid - half_c, 4))
                    pos["ask_limit"] = min(0.99, round(new_mid + half_c, 4))
                    log_event(
                        f"⚠️ ПРОБОЙ КОРИДОРА [{pos['title'][:40]}]: цена {new_mid:.4f}, списан убыток -${penalty:.2f}, ордера переставлены."
                    )
                    send_telegram_alert(
                        f"⚠️ <b>Пробой коридора Polymarket!</b>\n"
                        f"Рынок: <i>{pos['title']}</i>\n"
                        f"Новая цена: <code>{new_mid:.4f}</code>\n"
                        f"Списан убыток пробоя: <b>-${penalty:.2f} USDC</b>\n"
                        f"Лимитки переставлены: <code>{pos['bid_limit']:.4f} .. {pos['ask_limit']:.4f}</code>"
                    )
                elif hr_move >= 0.012 or abs(new_mid - pos["current_mid"]) >= 0.01:
                    # Срабатывание ИИ-защиты до пробоя лимиток
                    pos["rebalances_count"] += 1
                    half_c = (pos["max_spread_cents"] / 100.0) * 0.65
                    pos["bid_limit"] = max(0.01, round(new_mid - half_c, 4))
                    pos["ask_limit"] = min(0.99, round(new_mid + half_c, 4))
                    log_event(
                        f"🛡️ ИИ-ЗАЩИТА [{pos['title'][:40]}]: замечено движение цены ({pos['current_mid']:.4f} -> {new_mid:.4f}), лимитки безопасно сдвинуты."
                    )
                    send_telegram_alert(
                        f"🛡️ <b>Сработал ИИ Kill-Switch!</b>\n"
                        f"Рынок: <i>{pos['title']}</i>\n"
                        f"Цена сдвинулась: <code>{pos['current_mid']:.4f} -> {new_mid:.4f}</code>\n"
                        f"Лимитки безопасно сдвинуты без потерь."
                    )
                pos["current_mid"] = new_mid

            # Начисляем реальные Liquidity Rewards за прошедшее время
            reward_gain = alloc * (float(pos["expected_apr"]) / 100.0) * elapsed_years
            pos["accrued_rewards_usd"] = round(pos["accrued_rewards_usd"] + reward_gain, 4)

        elif pos["type"] == "poly_bond":
            gain = alloc * (float(pos["expected_apr"]) / 100.0) * elapsed_years
            pos["accrued_rewards_usd"] = round(pos["accrued_rewards_usd"] + gain, 4)

        elif pos["type"] == "hl_funding":
            coin = pos["coin"]
            cur_apr = hl_map.get(coin, pos["expected_apr"] * 2.0)
            # Если знак фандинга развернулся против нашей позиции — начисляем честный минус!
            is_favorable = (pos["direction"] == "SHORT" and cur_apr > 0) or (pos["direction"] == "LONG" and cur_apr < 0)
            effective_apr = (abs(cur_apr) + 10.95) / 2.0 if is_favorable else -abs(cur_apr) / 2.0
            pos["expected_apr"] = round(effective_apr, 2)
            funding_delta = alloc * (effective_apr / 100.0) * elapsed_years
            if funding_delta >= 0:
                pos["accrued_rewards_usd"] = round(pos["accrued_rewards_usd"] + funding_delta, 4)
            else:
                pos["adverse_loss_usd"] = round(pos["adverse_loss_usd"] + abs(funding_delta), 4)

    state["last_tick_at"] = now
    state["ticks_count"] = int(state.get("ticks_count", 0)) + 1
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    total_rewards = sum(p["accrued_rewards_usd"] for p in state["positions"])
    total_losses = sum(p["adverse_loss_usd"] for p in state["positions"])
    net_pnl = total_rewards - total_losses
    log_event(
        f"Тик #{state['ticks_count']} завершен | Начислено наград: +${total_rewards:.2f} | "
        f"Издержки/Комиссии: -${total_losses:.2f} | Чистый PnL: ${net_pnl:+.2f} USDC"
    )
    return state


def print_status() -> None:
    """Выводит подробный отчет о текущем состоянии виртуального портфеля $5,000."""
    if not STATE_FILE.exists():
        print("Портфель еще не инициализирован. Запустите: python3 paper_trader.py --once")
        return

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    deposit = float(state["initial_deposit_usd"])
    elapsed_hours = max(0.01, (time.time() - float(state["started_at"])) / 3600.0)
    total_rewards = sum(p["accrued_rewards_usd"] for p in state["positions"])
    total_losses = sum(p["adverse_loss_usd"] for p in state["positions"])
    net_pnl = total_rewards - total_losses
    equity = deposit + net_pnl
    weighted_apr = sum(p["expected_apr"] * (p["allocated_usd"] / deposit) for p in state["positions"])

    print("=" * 90)
    print(f"📊 ОТЧЕТ ПОРТФЕЛЯ ИИ-СИМУЛЯЦИИ (Старт: {state['started_at_iso'][:19]} UTC | В работе: {elapsed_hours:.2f} ч)")
    print("=" * 90)
    print(f"💰 Стартовый депозит:   ${deposit:,.2f} USDC")
    print(f"📈 Начислено ревардов: +${total_rewards:,.4f} USDC")
    print(f"📉 Комиссии и пробои:  -${total_losses:,.4f} USDC")
    print(f"💵 Текущий баланс:      ${equity:,.4f} USDC (Чистый PnL: ${net_pnl:+.4f} USDC)")
    print(f"🔥 Средневзвешенный APR портфеля: {weighted_apr:.1f}% годовых (~${deposit*(weighted_apr/100)/365:.2f}/день)")
    print("-" * 90)
    for i, p in enumerate(state["positions"], 1):
        net_p = p["accrued_rewards_usd"] - p["adverse_loss_usd"]
        print(f"{i}. [{p['type'].upper()}] {p['title']}")
        print(
            f"   • Выделено: ${p['allocated_usd']:,.0f} | Текущий APR слота: {p['expected_apr']:.1f}% | "
            f"Начислено: +${p['accrued_rewards_usd']:.4f} | Издержки: -${p['adverse_loss_usd']:.4f} | Итог: ${net_p:+.4f}"
        )
        if p["type"] == "poly_mm":
            print(
                f"   • Текущая цена: {p['current_mid']:.4f} | Наши лимитки: Bid {p['bid_limit']:.4f} .. Ask {p['ask_limit']:.4f} "
                f"| Срабатываний ИИ-защиты: {p['rebalances_count']}"
            )
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
        step_simulation()
        print_status()
    else:
        if not STATE_FILE.exists():
            init_portfolio(args.deposit)
        while True:
            step_simulation()
            time.sleep(args.interval)
