"""Главный CLI-скрипт сканера ИИ-неэффективностей (Polymarket + Hyperliquid)."""

import argparse
from datetime import datetime

from polymarket_scanner import (
    fetch_polymarket_markets,
    scan_liquidity_rewards,
    scan_smart_bonding,
)
from hyperliquid_scanner import scan_funding_anomalies


def run_report(deposit_usd: float = 1000.0) -> None:
    print("=" * 88)
    print(f"🚀 ИИ-СКАНЕР РЫНОЧНЫХ НЕЭФФЕКТИВНОСТЕЙ (Депозит для расчета: ${deposit_usd:,.0f} USDC)")
    print(f"📅 Время сканирования: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 88)

    # 1. Polymarket Liquidity Rewards
    print("\n[1/3] Загрузка активных рынков Polymarket и фильтрация через ИИ-анализатор рисков...")
    markets = fetch_polymarket_markets()
    reward_opps = scan_liquidity_rewards(markets, deposit_usd=deposit_usd, min_safety_score=70)

    print(f"\n✅ НАПРАВЛЕНИЕ 1: ИИ-Маркетмейкинг на Polymarket (Оплата в USDC за стояние в стакане)")
    print(f"Отобрано безопасных «спящих» рынков (без лайв-спорта): {len(reward_opps)}\n")

    for idx, item in enumerate(reward_opps[:6], start=1):
        print(f"  {idx}. {item['question']}")
        print(
            f"     • Пул наград: ${item['daily_reward_pool_usd']:,.0f}/день | "
            f"Стакан: ${item['book_liquidity_usd']:,.0f} | "
            f"Макс. отступ лимиток: ±{item['max_spread_cents']:.1f}¢"
        )
        print(
            f"     • Расчетный APR: {item['conservative_apr']:.1f}% (консервативно) — "
            f"{item['optimal_apr']:.1f}% (ближе к спреду)"
        )
        print(
            f"     • Доход на ваши ${deposit_usd:,.0f}: "
            f"${item['daily_profit_conservative']:.2f} – ${item['daily_profit_optimal']:.2f} в день "
            f"(~${item['daily_profit_optimal']*30:,.0f}/мес в чистом USDC)"
        )
        print(
            f"     • ИИ-оценка безопасности: {item['risk']['score']}/100 ({item['risk']['verdict']}) | "
            f"{'; '.join(item['risk']['reasons'][:2])}\n"
        )

    # 2. Polymarket Smart Bonding
    bonding_opps = scan_smart_bonding(markets, deposit_usd=deposit_usd)
    print("-" * 88)
    print(f"\n✅ НАПРАВЛЕНИЕ 2: ИИ-Бондинг на Polymarket (Покупка исходов 96.5¢–99.4¢ перед экспирацией)")
    print(f"Найдено кандидатов без лайв-спорта: {len(bonding_opps)}\n")

    for idx, b in enumerate(bonding_opps[:5], start=1):
        print(f"  {idx}. {b['question']} -> Исход [{b['lead_outcome']}] по цене ${b['entry_price']:.4f}")
        print(
            f"     • Чистый профит к погашению: +{b['net_return_pct']:.2f}% (${b['profit_usd']:.2f} на ${deposit_usd:,.0f}) | "
            f"До экспирации ({b['end_date']}): ~{b['days_left']:.0f} дн."
        )
        print(
            f"     • Эквивалент в годовых (APR): {b['annualized_apr']:.1f}% годовых | "
            f"Ликвидность стакана: ${b['book_liquidity_usd']:,.0f}"
        )
        print(f"     • Выдержка из правил для проверки ИИ: {b['description'][:140]}...\n")

    # 3. Hyperliquid Funding Anomalies
    print("-" * 88)
    print("\n[3/3] Сканирование фандинг-аномалий на Hyperliquid (с проверкой истории за 72 часа)...")
    hl_anomalies = scan_funding_anomalies(min_oi_usd=15_000_000.0, min_abs_apr=18.0)

    print(f"\n✅ НАПРАВЛЕНИЕ 3: Аномалии ставок финансирования (Hyperliquid L1, OI > $15M)")
    print(f"Обнаружено устойчивых перекосов: {len(hl_anomalies)}\n")

    for idx, a in enumerate(hl_anomalies[:6], start=1):
        daily_hl_usd = (deposit_usd / 2.0) * (abs(a["current_apr"]) / 100.0) / 365.0
        print(
            f"  {idx}. Монета: {a['coin']:10s} | Open Interest: ${a['oi_usd']/1e6:6.1f}M | "
            f"Текущий фандинг: {a['current_apr']:+6.2f}% годовых"
        )
        print(
            f"     • Средний APR за 72 часа: {a['avg_72h_apr']:+6.2f}% | "
            f"Устойчивость знака (72ч): {a['same_sign_pct_72h']:.0f}% часов подряд"
        )
        print(f"     • Схема: {a['strategy']}")
        print(
            f"     • Фандинг на перп-ноге: ~{a['combined_cross_dex_apr_est']:.1f}% годовых "
            f"(~${daily_hl_usd:.2f}/день на ногу Hyperliquid)\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ИИ-сканер рыночных неэффективностей")
    parser.add_argument("--deposit", type=float, default=1000.0, help="Сумма депозита в USDC")
    args = parser.parse_args()
    run_report(deposit_usd=args.deposit)
