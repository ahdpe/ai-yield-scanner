"""Сканер неэффективностей Polymarket: Liquidity Rewards (USDC) и Smart Bonding."""

from datetime import datetime, timezone
import json
import urllib.request
from typing import Dict, Any, List

from ai_analyzer import classify_polymarket_risk


GAMMA_MARKETS_URL = (
    "https://gamma-api.polymarket.com/markets"
    "?active=true&closed=false&order=volume24hr&ascending=false&limit=250"
)


def fetch_polymarket_markets() -> List[Dict[str, Any]]:
    """Загружает топ-250 активных рынков Polymarket из публичного Gamma API."""
    req = urllib.request.Request(
        GAMMA_MARKETS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def scan_liquidity_rewards(
    markets: List[Dict[str, Any]],
    deposit_usd: float = 1000.0,
    min_safety_score: int = 65,
) -> List[Dict[str, Any]]:
    """Находит лучшие безопасные рынки с ежедневными выплатами в USDC за лимитные ордера."""
    results: List[Dict[str, Any]] = []

    for m in markets:
        rewards = m.get("clobRewards") or []
        daily_reward_usd = sum(float(r.get("rewardsDailyRate") or 0.0) for r in rewards)
        if daily_reward_usd < 100.0:
            continue

        book_liq = float(m.get("liquidityClob") or m.get("liquidity") or 0.0)
        if book_liq < 15000.0:
            continue

        risk_info = classify_polymarket_risk(m)
        if risk_info["score"] < min_safety_score:
            continue

        max_spread_cents = float(m.get("rewardsMaxSpread") or 0.0)
        min_size = float(m.get("rewardsMinSize") or 0.0)

        # Консервативная оценка APR (против всего стакана)
        conservative_apr = (daily_reward_usd * 365.0 / (book_liq + deposit_usd)) * 100.0
        # Оценка при размещении ближе к спреду (около 45% стакана находится в зоне наград)
        active_zone_liq = max(book_liq * 0.45, 10000.0)
        optimal_apr = (daily_reward_usd * 365.0 / (active_zone_liq + deposit_usd)) * 100.0

        daily_usd_conservative = deposit_usd * (conservative_apr / 100.0) / 365.0
        daily_usd_optimal = deposit_usd * (optimal_apr / 100.0) / 365.0

        prices_raw = m.get("outcomePrices") or '["0.5", "0.5"]'
        try:
            prices = [float(p) for p in json.loads(prices_raw)] if isinstance(prices_raw, str) else [float(p) for p in prices_raw]
        except Exception:
            prices = [0.5, 0.5]

        results.append({
            "question": m.get("question", ""),
            "slug": m.get("slug", ""),
            "daily_reward_pool_usd": daily_reward_usd,
            "book_liquidity_usd": book_liq,
            "max_spread_cents": max_spread_cents,
            "min_order_size": min_size,
            "price_yes": prices[0] if prices else 0.5,
            "conservative_apr": conservative_apr,
            "optimal_apr": optimal_apr,
            "daily_profit_conservative": daily_usd_conservative,
            "daily_profit_optimal": daily_usd_optimal,
            "holding_rewards": bool(m.get("holdingRewardsEnabled")),
            "risk": risk_info,
            "description": m.get("description", ""),
        })

    # Сортируем по комбинации консервативного APR и безопасности
    results.sort(key=lambda x: x["conservative_apr"] * (x["risk"]["score"] / 100.0), reverse=True)
    return results


def scan_smart_bonding(
    markets: List[Dict[str, Any]],
    deposit_usd: float = 1000.0,
) -> List[Dict[str, Any]]:
    """Находит рынки с высокой вероятностью исхода (97.0¢ - 99.4¢) для стратегии ИИ-бондинга."""
    now = datetime.now(timezone.utc)
    candidates: List[Dict[str, Any]] = []

    for m in markets:
        risk_info = classify_polymarket_risk(m)
        if risk_info["is_sports"]:
            continue

        book_liq = float(m.get("liquidityClob") or m.get("liquidity") or 0.0)
        if book_liq < 25000.0:
            continue

        prices_raw = m.get("outcomePrices") or "[]"
        outcomes_raw = m.get("outcomes") or '["Yes", "No"]'
        try:
            prices = [float(p) for p in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)]
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except Exception:
            continue

        if len(prices) < 2:
            continue

        best_idx = 0 if prices[0] >= prices[1] else 1
        lead_price = prices[best_idx]
        lead_outcome = outcomes[best_idx] if best_idx < len(outcomes) else "Yes"

        # Ищем цену в диапазоне 0.965 .. 0.994 (запас профита 0.6% .. 3.5% до $1.00)
        if not (0.965 <= lead_price <= 0.994):
            continue

        end_str = m.get("endDate") or ""
        days_left = 14.0
        if end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                diff_days = (end_dt - now).total_seconds() / 86400.0
                days_left = max(1.0, diff_days)
            except Exception:
                days_left = 14.0

        if days_left > 30.0:
            continue

        net_return_pct = ((1.0 - lead_price) / lead_price) * 100.0
        annualized_apr = net_return_pct * (365.0 / days_left)
        profit_usd = deposit_usd * (net_return_pct / 100.0)

        candidates.append({
            "question": m.get("question", ""),
            "lead_outcome": lead_outcome,
            "entry_price": lead_price,
            "net_return_pct": net_return_pct,
            "days_left": days_left,
            "annualized_apr": annualized_apr,
            "profit_usd": profit_usd,
            "book_liquidity_usd": book_liq,
            "end_date": end_str[:10],
            "risk": risk_info,
            "description": (m.get("description") or "")[:300],
        })

    candidates.sort(key=lambda x: x["annualized_apr"], reverse=True)
    return candidates
