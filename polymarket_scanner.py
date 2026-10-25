"""Сканер неэффективностей Polymarket: Liquidity Rewards (USDC) по реальному стакану CLOB (квадратичная формула) и Smart Bonding."""

from datetime import datetime, timezone
import json
import urllib.parse
import urllib.request
from typing import Dict, Any, List, Optional, Tuple

from ai_analyzer import classify_polymarket_risk


GAMMA_MARKETS_URL = (
    "https://gamma-api.polymarket.com/markets"
    "?active=true&closed=false&order=volume24hr&ascending=false&limit=250"
)
GAMMA_MARKET_BY_SLUG_URL = "https://gamma-api.polymarket.com/markets?slug={slug}"
CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"
DATA_TRADES_URL = "https://data-api.polymarket.com/trades?market={condition_id}&limit={limit}"

# MM только на рынках, до истечения которых не меньше недели: ближе к дате резолюции растет риск новостного обвала
MM_MIN_DAYS_TO_END = 7.0


def fetch_polymarket_markets() -> List[Dict[str, Any]]:
    """Загружает топ-250 активных рынков Polymarket из публичного Gamma API."""
    req = urllib.request.Request(
        GAMMA_MARKETS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """Загружает один рынок по slug (в том числе закрытый/разрешенный, которого уже нет в топ-250)."""
    req = urllib.request.Request(
        GAMMA_MARKET_BY_SLUG_URL.format(slug=urllib.parse.quote(slug, safe="")),
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data[0] if data else None


def fetch_clob_book(token_id: str) -> Optional[Dict[str, Any]]:
    """Загружает реальный стакан ордеров (bids/asks) из Polymarket CLOB API."""
    if not token_id:
        return None
    req = urllib.request.Request(
        CLOB_BOOK_URL.format(token_id=urllib.parse.quote(str(token_id), safe="")),
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def days_until(end_str: str) -> Optional[float]:
    """Сколько дней осталось до даты (ISO, с 'Z' или без зоны — считается UTC). None — дата не задана или битая."""
    if not end_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    return (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0


def fetch_recent_trades(condition_id: str, since_ts: float, limit: int = 500) -> List[Dict[str, Any]]:
    """Сделки рынка (обе стороны: YES и NO) строго после since_ts из публичной ленты Polymarket Data API."""
    req = urllib.request.Request(
        DATA_TRADES_URL.format(condition_id=urllib.parse.quote(condition_id, safe=""), limit=limit),
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        trades = json.loads(resp.read().decode("utf-8"))
    return [t for t in trades if float(t.get("timestamp") or 0) > since_ts]


def trade_yes_price(trade: Dict[str, Any]) -> float:
    """Цена сделки в терминах первого исхода (YES): покупка NO по p равна продаже YES по 1 - p."""
    price = float(trade["price"])
    return price if int(trade.get("outcomeIndex", 0)) == 0 else 1.0 - price


def parse_clob_token_ids(m: Dict[str, Any]) -> List[str]:
    raw = m.get("clobTokenIds")
    if not raw:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else [str(x) for x in raw]
    except Exception:
        return []


def compute_quadratic_mm_metrics(
    book: Dict[str, Any],
    daily_reward_usd: float,
    max_spread_cents: float,
    min_size: float = 0.0,
    deposit_usd: float = 1000.0,
    spread_fraction: float = 0.65,
) -> Optional[Dict[str, float]]:
    """Рассчитывает честный APR маркет-мейкинга по официальной квадратичной формуле Polymarket
    против реальных лимитных ордеров, стоящих сейчас в CLOB стакане:
      S(v, s) = ((v - s) / v)^2 * size, где v = max_spread, s = |price - mid|.
    Для двусторонней котировки (Bid YES по p_bid + Ask YES / Bid NO по 1 - p_ask)
    1 сбалансированная акция с обеих сторон требует p_bid + (1 - p_ask) ≈ $1.00 залога."""
    v = max_spread_cents / 100.0
    if v <= 0.005 or daily_reward_usd <= 0:
        return None

    bids = sorted(
        [(float(x["price"]), float(x["size"])) for x in (book.get("bids") or [])],
        reverse=True,
    )
    asks = sorted(
        [(float(x["price"]), float(x["size"])) for x in (book.get("asks") or [])],
    )
    if not bids or not asks:
        return None

    best_bid, best_ask = bids[0][0], asks[0][0]
    book_spread = max(0.0, best_ask - best_bid)
    if book_spread > v * 1.8:
        return None
    mid = (best_bid + best_ask) / 2.0

    # Квадратичный вес всех чужих ордеров внутри зоны наград [mid - v, mid + v]
    bid_q = sum(
        ((v - (mid - p)) / v) ** 2 * sz
        for p, sz in bids
        if 0.0 <= (mid - p) < v and sz >= min_size
    )
    ask_q = sum(
        ((v - (p - mid)) / v) ** 2 * sz
        for p, sz in asks
        if 0.0 <= (p - mid) < v and sz >= min_size
    )
    # По правилам Polymarket: при mid в [0.10, 0.90] односторонний перекос штрафуется в c=3 раза,
    # а за пределами [0.10, 0.90] учитывается строго min(Q_bid, Q_ask)
    if 0.10 <= mid <= 0.90:
        book_q = max(min(bid_q, ask_q), max(bid_q / 3.0, ask_q / 3.0))
    else:
        book_q = min(bid_q, ask_q)

    def apr_at_fraction(frac: float) -> Tuple[float, float, float, float, float]:
        s = min(v * frac, mid - 0.01, 0.99 - mid)
        if s <= 0.002:
            return 0.0, 0.0, max(0.01, mid - 0.01), min(0.99, mid + 0.01), 0.0
        p_bid = max(0.01, round(mid - s, 4))
        p_ask = min(0.99, round(mid + s, 4))
        unit_collateral = max(0.20, p_bid + (1.0 - p_ask))
        shares = deposit_usd / unit_collateral
        if shares < min_size:
            return 0.0, 0.0, p_bid, p_ask, shares
        s_eff = max(mid - p_bid, p_ask - mid)
        if s_eff >= v:
            return 0.0, 0.0, p_bid, p_ask, shares
        our_q = ((v - s_eff) / v) ** 2 * shares
        share = our_q / (book_q + our_q) if (book_q + our_q) > 0 else 0.0
        daily_usd = daily_reward_usd * share
        apr = (daily_usd * 365.0 / deposit_usd) * 100.0
        return apr, daily_usd, p_bid, p_ask, shares

    cons_apr, cons_daily, bid_lim, ask_lim, shares = apr_at_fraction(spread_fraction)
    opt_apr, opt_daily, _, _, _ = apr_at_fraction(0.50)

    return {
        "mid": round(mid, 4),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "book_spread": round(book_spread, 4),
        "book_q": round(book_q, 1),
        "bid_limit": bid_lim,
        "ask_limit": ask_lim,
        "balanced_shares": round(shares, 2),
        "quadratic_apr": round(cons_apr, 2),
        "optimal_apr": round(opt_apr, 2),
        "daily_usd_conservative": round(cons_daily, 4),
        "daily_usd_optimal": round(opt_daily, 4),
    }


def parse_outcome_prices(m: Dict[str, Any]) -> Tuple[List[str], List[float]]:
    """Возвращает (исходы, цены) рынка Gamma API; пустые списки, если данные битые."""
    try:
        outcomes_raw = m.get("outcomes") or '["Yes", "No"]'
        prices_raw = m.get("outcomePrices") or "[]"
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw)
        prices = [float(p) for p in (json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw)]
        return outcomes, prices
    except Exception:
        return [], []


def scan_liquidity_rewards(
    markets: List[Dict[str, Any]],
    deposit_usd: float = 1000.0,
    min_safety_score: int = 65,
    min_days_to_end: float = MM_MIN_DAYS_TO_END,
) -> List[Dict[str, Any]]:
    """Находит лучшие безопасные рынки с ежедневными выплатами в USDC, проверяя реальный стакан CLOB.
    Рынки, которые истекают раньше чем через min_days_to_end дней (или без даты), отбрасываются."""
    pre_candidates: List[Tuple[float, Dict[str, Any], Dict[str, Any], str]] = []

    for m in markets:
        rewards = m.get("clobRewards") or []
        daily_reward_usd = sum(float(r.get("rewardsDailyRate") or 0.0) for r in rewards)
        if daily_reward_usd < 50.0:
            continue

        book_liq = float(m.get("liquidityClob") or m.get("liquidity") or 0.0)
        if book_liq < 10000.0:
            continue

        max_spread_cents = float(m.get("rewardsMaxSpread") or 0.0)
        if max_spread_cents <= 1.5:
            continue

        days_left = days_until(m.get("endDate") or "")
        if days_left is None or days_left < min_days_to_end:
            continue

        risk_info = classify_polymarket_risk(m)
        if risk_info["score"] < min_safety_score:
            continue

        token_ids = parse_clob_token_ids(m)
        if not token_ids:
            continue

        naive_ratio = daily_reward_usd / max(book_liq, 10000.0)
        pre_candidates.append((naive_ratio, m, risk_info, token_ids[0]))

    # Загружаем реальные стаканы CLOB для топ-25 кандидатов
    pre_candidates.sort(key=lambda x: x[0], reverse=True)
    results: List[Dict[str, Any]] = []

    for _, m, risk_info, token_id in pre_candidates[:25]:
        try:
            book = fetch_clob_book(token_id)
        except Exception:
            book = None
        if not book:
            continue

        rewards = m.get("clobRewards") or []
        daily_reward_usd = sum(float(r.get("rewardsDailyRate") or 0.0) for r in rewards)
        max_spread_cents = float(m.get("rewardsMaxSpread") or 0.0)
        min_size = float(m.get("rewardsMinSize") or 0.0)
        book_liq = float(m.get("liquidityClob") or m.get("liquidity") or 0.0)

        q_metrics = compute_quadratic_mm_metrics(
            book=book,
            daily_reward_usd=daily_reward_usd,
            max_spread_cents=max_spread_cents,
            min_size=min_size,
            deposit_usd=deposit_usd,
            spread_fraction=0.65,
        )
        if not q_metrics or q_metrics["quadratic_apr"] < 5.0:
            continue

        results.append({
            "question": m.get("question", ""),
            "slug": m.get("slug", ""),
            "token_id": token_id,
            "daily_reward_pool_usd": daily_reward_usd,
            "book_liquidity_usd": book_liq,
            "book_quadratic_score": q_metrics["book_q"],
            "max_spread_cents": max_spread_cents,
            "min_order_size": min_size,
            "price_yes": q_metrics["mid"],
            "book_spread": q_metrics["book_spread"],
            "bid_limit": q_metrics["bid_limit"],
            "ask_limit": q_metrics["ask_limit"],
            "balanced_shares": q_metrics["balanced_shares"],
            "conservative_apr": q_metrics["quadratic_apr"],
            "optimal_apr": q_metrics["optimal_apr"],
            "daily_profit_conservative": q_metrics["daily_usd_conservative"],
            "daily_profit_optimal": q_metrics["daily_usd_optimal"],
            "holding_rewards": bool(m.get("holdingRewardsEnabled")),
            "risk": risk_info,
            "description": m.get("description", ""),
            "condition_id": m.get("conditionId", ""),
            "end_date": m.get("endDate") or "",
        })

    results.sort(key=lambda x: x["conservative_apr"] * (x["risk"]["score"] / 100.0), reverse=True)
    return results


def scan_smart_bonding(
    markets: List[Dict[str, Any]],
    deposit_usd: float = 1000.0,
) -> List[Dict[str, Any]]:
    """Находит рынки с высокой вероятностью исхода (96.5¢ - 99.4¢) для стратегии ИИ-бондинга."""
    now = datetime.now(timezone.utc)
    candidates: List[Dict[str, Any]] = []

    for m in markets:
        risk_info = classify_polymarket_risk(m)
        if risk_info["is_sports"]:
            continue

        book_liq = float(m.get("liquidityClob") or m.get("liquidity") or 0.0)
        if book_liq < 25000.0:
            continue

        outcomes, prices = parse_outcome_prices(m)
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
                if diff_days <= 0:
                    continue
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
            "slug": m.get("slug", ""),
            "lead_outcome": lead_outcome,
            "entry_price": lead_price,
            "net_return_pct": net_return_pct,
            "days_left": days_left,
            "annualized_apr": annualized_apr,
            "profit_usd": profit_usd,
            "book_liquidity_usd": book_liq,
            "end_date": end_str,
            "risk": risk_info,
            "description": (m.get("description") or "")[:300],
        })

    candidates.sort(key=lambda x: x["annualized_apr"], reverse=True)
    return candidates
