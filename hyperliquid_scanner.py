"""Сканер аномалий ставок финансирования (Funding Rate) на некастодиальной бирже Hyperliquid L1
с обязательной проверкой наличия ликвидного спотового рынка на Hyperliquid Spot для дельта-нейтрального хеджа."""

import json
import time
import urllib.request
from typing import Dict, Any, List


HL_INFO_URL = "https://api.hyperliquid.xyz/info"


def hl_request(payload: Dict[str, Any]) -> Any:
    """Отправляет POST-запрос к публичному API Hyperliquid."""
    req = urllib.request.Request(
        HL_INFO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_hl_spot_hedge_map(min_spot_vol_usd: float = 100_000.0) -> Dict[str, Dict[str, Any]]:
    """Загружает все спотовые пары Hyperliquid L1 и сопоставляет их с тикерами перпов
    (например: BTC -> UBTC, ETH -> UETH, SOL -> USOL, ZEC -> UZEC, XMR -> XMR1, NEAR -> ONEAR, HYPE -> HYPE).
    Возвращает только активы с реальным суточным спот-объемом >= min_spot_vol_usd."""
    sm = hl_request({"type": "spotMetaAndAssetCtxs"})
    tokens = {t["index"]: t for t in sm[0].get("tokens", [])}
    ctx_by_coin = {c.get("coin"): c for c in sm[1]}

    alias_map = {
        "UBTC": "BTC",
        "UETH": "ETH",
        "USOL": "SOL",
        "UZEC": "ZEC",
        "UPUMP": "PUMP",
        "UENA": "ENA",
        "UFART": "FARTCOIN",
        "XMR1": "XMR",
        "ONEAR": "NEAR",
        "AAVE0": "AAVE",
        "AVAX0": "AVAX",
        "TAO1": "TAO",
        "TRX1": "TRX",
    }

    hedge_map: Dict[str, Dict[str, Any]] = {}
    for u in sm[0].get("universe", []):
        pair_id = u.get("name", "")
        c = ctx_by_coin.get(pair_id) or {}
        vol_24h = float(c.get("dayNtlVlm") or 0.0)
        if vol_24h < min_spot_vol_usd:
            continue
        tok_indices = u.get("tokens") or []
        if not tok_indices:
            continue
        t = tokens.get(tok_indices[0], {})
        spot_symbol = t.get("name", "")
        perp_coin = alias_map.get(spot_symbol, spot_symbol)
        existing = hedge_map.get(perp_coin)
        if not existing or vol_24h > existing["spot_vol_24h_usd"]:
            hedge_map[perp_coin] = {
                "spot_symbol": spot_symbol,
                "spot_pair_id": pair_id,
                "spot_full_name": t.get("fullName") or spot_symbol,
                "spot_mark_px": float(c.get("markPx") or 0.0),
                "spot_vol_24h_usd": vol_24h,
            }
    return hedge_map


def scan_funding_anomalies(
    min_oi_usd: float = 15_000_000.0,
    min_abs_apr: float = 18.0,
    require_hl_spot_hedge: bool = True,
) -> List[Dict[str, Any]]:
    """Находит крупные рынки Hyperliquid с положительным фандингом, проверяет наличие
    ликвидного спот-рынка на Hyperliquid L1 для безрискового LONG Spot + SHORT Perp хеджа
    и устойчивость ставки за 72 часа."""
    meta_ctx = hl_request({"type": "metaAndAssetCtxs"})
    universe = meta_ctx[0]["universe"]
    ctxs = meta_ctx[1]
    spot_map = fetch_hl_spot_hedge_map(min_spot_vol_usd=100_000.0) if require_hl_spot_hedge else {}

    anomalies: List[Dict[str, Any]] = []
    now_ms = int(time.time() * 1000)
    hours_72_ms = 72 * 3600 * 1000

    for u, c in zip(universe, ctxs):
        coin = u["name"]
        mark_px = float(c.get("markPx") or 0.0)
        oi_usd = float(c.get("openInterest") or 0.0) * mark_px
        vol_24h = float(c.get("dayNtlVlm") or 0.0)
        cur_fr = float(c.get("funding") or 0.0)
        cur_apr = cur_fr * 24.0 * 365.0 * 100.0

        if oi_usd < min_oi_usd or abs(cur_apr) < min_abs_apr:
            continue

        # Без маржинального займа спота зашортить спот нельзя, поэтому дельта-нейтральная
        # некастодиальная конструкция возможна только при положительном фандинге (SHORT Perp + LONG Spot)
        if require_hl_spot_hedge and (cur_apr <= 0 or coin not in spot_map):
            continue

        spot_info = spot_map.get(coin)

        # Проверяем устойчивость аномалии за последние 72 часа
        hist = hl_request({
            "type": "fundingHistory",
            "coin": coin,
            "startTime": now_ms - hours_72_ms,
        })
        rates = [float(x["fundingRate"]) for x in hist] if hist else [cur_fr]
        avg_72h_apr = (sum(rates) / len(rates)) * 24.0 * 365.0 * 100.0

        if cur_apr > 0:
            same_sign_hours = sum(1 for r in rates if r > 0)
            if spot_info:
                strategy = (
                    f"SHORT {coin} Perp + LONG {spot_info['spot_symbol']} Spot на Hyperliquid "
                    f"(пара {spot_info['spot_pair_id']}, спот-объем 24ч ${spot_info['spot_vol_24h_usd']/1e6:.2f}M)"
                )
            else:
                strategy = "SHORT Perp на Hyperliquid + LONG спот (внешний хедж)"
        else:
            same_sign_hours = sum(1 for r in rates if r < 0)
            strategy = "LONG Perp на Hyperliquid + SHORT хедж (требуется займ спота)"

        net_spread_est = abs(cur_apr)

        anomalies.append({
            "coin": coin,
            "oi_usd": oi_usd,
            "vol_24h_usd": vol_24h,
            "current_apr": cur_apr,
            "avg_72h_apr": avg_72h_apr,
            "same_sign_pct_72h": (same_sign_hours / max(1, len(rates))) * 100.0,
            "strategy": strategy,
            "combined_cross_dex_apr_est": net_spread_est,
            "spot_hedge": spot_info,
        })

    anomalies.sort(key=lambda x: abs(x["current_apr"]), reverse=True)
    return anomalies
