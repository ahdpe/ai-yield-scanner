"""Сканер аномалий ставок финансирования (Funding Rate) на некастодиальной бирже Hyperliquid L1."""

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


def scan_funding_anomalies(
    min_oi_usd: float = 15_000_000.0,
    min_abs_apr: float = 18.0,
) -> List[Dict[str, Any]]:
    """Находит крупные рынки Hyperliquid с аномальным фандингом и проверяет их устойчивость за 72 часа."""
    meta_ctx = hl_request({"type": "metaAndAssetCtxs"})
    universe = meta_ctx[0]["universe"]
    ctxs = meta_ctx[1]

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
            strategy = "SHORT Perp на Hyperliquid + LONG на смежной DEX/Споте"
            net_spread_est = cur_apr - 10.95  # если на второй DEX базовые +10.95%
        else:
            same_sign_hours = sum(1 for r in rates if r < 0)
            strategy = "LONG Perp на Hyperliquid (шорты платят вам!) + SHORT на другой DEX (лонги платят вам!)"
            net_spread_est = abs(cur_apr) + 10.95  # обе ноги платят вам!

        anomalies.append({
            "coin": coin,
            "oi_usd": oi_usd,
            "vol_24h_usd": vol_24h,
            "current_apr": cur_apr,
            "avg_72h_apr": avg_72h_apr,
            "same_sign_pct_72h": (same_sign_hours / max(1, len(rates))) * 100.0,
            "strategy": strategy,
            "combined_cross_dex_apr_est": net_spread_est,
        })

    anomalies.sort(key=lambda x: abs(x["current_apr"]), reverse=True)
    return anomalies
