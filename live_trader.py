"""Live-трейдер Smart Bonding на Polymarket с реальным депозитом и ИИ-риск-аудитом Venice AI.

Реализует стратегию 'poly_bond' (Smart Bonding):
- Покупка почти гарантированных исходов (94.5¢ - 98.5¢, ≤ 45 дн.) с доходностью к экспирации.
- ИИ-аудит Venice AI (fail-closed, консенсус 2/2, учет дедлайнов и субъективных условий).
- Реальное исполнение ордеров через официальный unified SDK (`polymarket-client` SecureClient)
  с поддержкой Deposit Wallet (signature_type=3, ERC-7739, V2/V3 и pUSD).
- Относительный Stop-Loss при падении лучшего bid на -3.5¢ от цены входа.
- Лимит корреляции (до 2 позиций на тему), автоматический учет баланса, открытых позиций и PnL.
- Telegram-уведомления с динамическим бейджем [LIVE].
"""

import argparse
import fcntl
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import math
import os
import re
import signal
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

from polymarket import SecureClient
from polymarket.errors import RequestRejectedError
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

from ai_analyzer import REAUDIT_RESERVE_CALLS, call_venice_market_audit, entry_budget_exhausted
from error_alerts import daemon_alerts
from polymarket_scanner import (
    BOND_MIN_ENTRY_APR,
    estimate_taker_fee_usd,
    extract_event_id,
    fetch_clob_book,
    fetch_market_by_slug,
    fetch_polymarket_markets,
    parse_clob_token_ids,
    parse_fee_schedule,
    parse_outcome_prices,
    scan_smart_bonding,
)
from live_execution import read_conditional_balance, read_execution
from state_io import STATE_DIR, atomic_write_json, esc
from telegram_notifier import send_telegram_alert


def format_market_link(title: str, slug: str = "") -> str:
    """Форматирует название рынка в кликабельную HTML-ссылку для Telegram (отделяя [исход])."""
    clean_slug = (slug or "").strip()
    if not clean_slug:
        return esc(title)
    url = f"https://polymarket.com/market/{urllib.parse.quote(clean_slug, safe='')}"
    if " [" in title:
        question_part, outcome_part = title.rsplit(" [", 1)
        return f'<a href="{esc(url)}">{esc(question_part)}</a> [{esc(outcome_part)}'
    return f'<a href="{esc(url)}">{esc(title)}</a>'

LIVE_STATE_FILE = STATE_DIR / "live_portfolio_state.json"
LIVE_LOG_FILE = STATE_DIR / "live_trader.log"
CONFIG_FILE = STATE_DIR / "live_config.env"

# Настройки депозита, слотов и кошелька по умолчанию
DEFAULT_TOTAL_DEPOSIT = 500.0
DEFAULT_SLOT_SIZE = 100.0
DEFAULT_MAX_SLOTS = 5
DEFAULT_SIGNATURE_TYPE = "3"  # Deposit Wallet (аккаунты Polymarket от мая 2026+)
PUSD_CONTRACT = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLYGON_RPCS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
)

BOND_STOP_LOSS_OFFSET = 0.0350    # Относительный Stop-Loss (-3.5¢ от цены входа)
BOND_STOP_LOSS_FLOOR = 0.8500     # Нижняя граница безопасности для относительного стопа
BOND_STOP_LOSS = 0.9500           # Резервный уровень для старых записей без цены входа
MAX_ENTRY_PRICE = 0.9850
MIN_ENTRY_PRICE = 0.9450
MAX_ENTRY_SPREAD = 0.0150         # Макс. допустимый спред (best_ask - best_bid <= 1.5¢)
MAX_SLOTS_PER_TOPIC = 2           # Лимит корреляции: макс. 2 открытые позиции на одну тему
MAX_ENTRY_FEE_RATE = 0.06         # Допускаем геополитику (0), финансы/политику/техно (0.04), экономику/ФРС (0.05); отсекаем крипту (0.07)
MIN_NET_RETURN_PCT = 0.30         # Минимальная чистая доходность к экспирации после комиссии (%)
DUST_SHARES_THRESHOLD = 0.01      # Неторгуемая комиссионная «пыль» < 0.01 акции после полного SELL
ENTRY_AI_SAMPLES = 2              # Вход только при двух независимых APPROVE от модели
MAX_FRESH_AUDITS_PER_TICK = 2     # Ограничение свежих (не кэшированных) аудитов за один тик
ENTRY_AUDIT_CACHE_TTL_SEC = 21600.0  # 6 часов кэша для APPROVE на входе (SKIP кэшируется на 24-36 ч)
REAUDIT_CALM_INTERVAL_SEC = 86400.0  # 24 часа между реаудитами спокойных позиций (просадка < 1¢, срок > 3 дн.)
REAUDIT_INTERVAL_SEC = 21600.0    # 6 часов между реаудитами настороженных позиций (просадка 1-2¢ или срок <= 3 дн.)
REAUDIT_RECHECK_SEC = 3600.0      # 1 час для позиций под давлением (просадка >= 2¢ или после первого SKIP)
REAUDIT_WATCH_DROP = 0.0100       # Просадка от цены входа >= 1.0¢ переводит реаудит в 6-часовой режим на Venice
REAUDIT_STRESS_DROP = 0.0200      # Просадка от цены входа >= 2.0¢ переводит реаудит в 1-часовой режим на Venice
REAUDIT_NEAR_EXPIRY_DAYS = 3.0    # Менее 3 суток до экспирации -> проверяем каждые 6 ч на Venice
REOPEN_COOLDOWN_SEC = 12 * 3600.0 # Пауза перед повторным входом в закрытый рынок
MISSING_BUY_CONFIRM_SEC = 90.0    # Две сверки исчезнувшей заявки в разных циклах
MISSING_BUY_MAX_GAP_SEC = 360.0
SELL_REPLACE_AFTER_SEC = 60.0     # Незаполненный Stop-Loss SELL перевыставляем через минуту
SELL_AGGRESSIVE_TICKS = 1         # На сколько тиков ниже best_bid ставить SELL для выкупа
REAUDIT_EXIT_MIN_CONFIDENCE = 75  # Досрочный выход по реаудиту только при явном SKIP с уверенностью >= 75%
FAST_GUARD_INTERVAL_SEC = 20      # Быстрая проверка стаканов и Stop-Loss открытых позиций каждые 20 с
MIN_ENTRY_BID_DEPTH_USD = 250.0   # Мин. живая глубина бидов в пределах 2¢ от best_bid для входа ($)
BID_DEPTH_BAND = 0.0200           # Ширина полосы от best_bid для оценки ликвидности стакана на выход (2.0¢)
STOP_LOSS_CONFIRM_STREAK = 2      # Число последовательных проверок ниже стопа для защиты от секундных проколов
STOP_LOSS_MAX_HOLE_SPREAD = 0.0300  # Спред > 3.0¢ при mid >= stop_px считается дыркой в стакане, а не обвалом
STOP_LOSS_PANIC_OFFSET = 0.0300   # Глубокий обвал (bid <= stop - 3¢ и ask <= stop + 0.5¢) -> выход с 1-го замера
STOP_LOSS_STREAK_MAX_GAP_SEC = 180.0  # Серия пробоев стопа засчитывается только в пределах 3 мин подряд
LONG_HORIZON_DAYS = 21.0              # Позиции со сроком > 21 дн. считаются длинными
MAX_LONG_HORIZON_SHARE = 0.40         # Макс. доля длинных слотов (> 21 дн.) от max_slots (40%)
TAKE_PROFIT_MIN_DAYS_LEFT = 10.0      # Досрочный Take-Profit только если до экспирации >= 10 дн.
TAKE_PROFIT_MAX_REM_APR = 20.0        # Take-Profit когда оставшийся APR к $1.00 упал ниже 20%
TAKE_PROFIT_MIN_GAIN_CENTS = 0.0180   # Минимальный рост bid от входа (+1.8¢) для ротации
TAKE_PROFIT_HIGH_BID = 0.9880         # Или достижение bid >= $0.988 с чистой прибылью после комиссий
TAKE_PROFIT_MIN_NET_PROFIT_USD = 0.20 # Минимальная чистая прибыль ($) после комиссии SELL
TAKE_PROFIT_MIN_NET_GAIN_PER_SHARE = 0.0030  # Минимум +0.3¢/акция чистыми после комиссии

TOPIC_CLUSTER_PATTERNS = {
    "iran_mideast": re.compile(
        r"\b(iran|iranian|israel|hormuz|centcom|khamenei|tehran|hezbollah|hamas|gaza|mandeb|yemen|houthi|middle east)\b",
        re.IGNORECASE,
    ),
    "russia_ukraine": re.compile(
        r"\b(russia|russian|ukraine|ukrainian|putin|zelensky|nato|kremlin|kyiv)\b",
        re.IGNORECASE,
    ),
    "fed_rates": re.compile(
        r"\b(fed|fomc|federal reserve|interest rate|rate cut|rate hike|powell|warsh)\b",
        re.IGNORECASE,
    ),
    "china_asia": re.compile(
        r"\b(china|chinese|taiwan|xi jinping|kim jong|north korea|pyongyang|beijing)\b",
        re.IGNORECASE,
    ),
    "nvidia_huang": re.compile(
        r"\b(nvidia|nvda|jensen huang)\b",
        re.IGNORECASE,
    ),
    "trump_us_pol": re.compile(
        r"\b(trump|hegseth|vance|musk|elon|white house|congress|senate|impeach)\b",
        re.IGNORECASE,
    ),
}


def pos_stop_loss_price(pos: Dict[str, Any]) -> float:
    """Возвращает уровень защитного Stop-Loss для позиции (entry_price - 3.5¢)."""
    base = float(pos.get("entry_price") or pos.get("requested_price") or 0.0)
    if base <= 0:
        return BOND_STOP_LOSS
    return round(max(BOND_STOP_LOSS_FLOOR, base - BOND_STOP_LOSS_OFFSET), 4)


def _days_left_from_end_date(end_str: str, now: Optional[float] = None) -> Optional[float]:
    """Возвращает число дней до даты экспирации ISO или None, если дата отсутствует/невалидна."""
    clean = str(end_str or "").strip()
    if not clean:
        return None
    cur_now = float(now if now is not None else time.time())
    try:
        end_dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    return max(0.0, (end_dt.timestamp() - cur_now) / 86400.0)


def get_entry_tier_requirements(open_count: int, max_slots: int) -> Dict[str, Any]:
    """Лестница строгости входа по заполненности слотов (сохраняет резерв 20% под топ-перекосы)."""
    fill_ratio = float(open_count) / float(max(1, max_slots))
    if fill_ratio < 0.50:
        return {"tier": "core", "min_apr": 35.0, "max_days": 45.0, "min_confidence": 75}
    if fill_ratio < 0.80:
        return {"tier": "selective", "min_apr": 50.0, "max_days": 21.0, "min_confidence": 80}
    return {"tier": "reserve", "min_apr": 80.0, "max_days": 14.0, "min_confidence": 85}


def should_trigger_take_profit(
    pos: Dict[str, Any],
    summary: Dict[str, Any],
    market: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Tuple[bool, str, float]:
    """Проверяет досрочную фиксацию прибыли (Take-Profit) для освобождения длинного слота.

    Учитывает комиссию SELL и глубину бидов стакана; при неизвестном расписании комиссий не срабатывает."""
    if pos.get("status") != "OPEN" or not pos.get("entry_terminal"):
        return False, "", 0.0
    if any(not x.get("complete") for x in pos.get("exit_orders", [])):
        return False, "", 0.0
    if not summary or summary.get("best_bid") is None:
        return False, "", 0.0
    best_bid = float(summary["best_bid"])
    if best_bid <= 0:
        return False, "", 0.0

    shares = float(pos.get("shares") or 0.0)
    order_shares = math.floor((shares + 1e-9) * 100) / 100
    min_order_size = float(summary.get("min_order_size") or 5.0)
    if order_shares <= 0 or order_shares < min_order_size:
        return False, "", 0.0

    entry_price = float(pos.get("entry_price") or 0.0)
    sell_price = float(_round_price_to_tick(best_bid, str(summary.get("tick_size") or "0.01")))
    if entry_price <= 0 or sell_price <= entry_price:
        return False, "", 0.0

    end_str = str((market or {}).get("endDate") or pos.get("end_date") or "").strip()
    days_left = _days_left_from_end_date(end_str, now)
    if days_left is None or days_left < TAKE_PROFIT_MIN_DAYS_LEFT:
        return False, "", 0.0

    rem_apr = ((1.0 - sell_price) / sell_price) * 100.0 * (365.0 / days_left)
    if rem_apr >= TAKE_PROFIT_MAX_REM_APR:
        return False, "", 0.0
    if (sell_price - entry_price) < TAKE_PROFIT_MIN_GAIN_CENTS and sell_price < TAKE_PROFIT_HIGH_BID:
        return False, "", 0.0

    fee_sched = pos.get("fee_schedule")
    if not (isinstance(fee_sched, dict) and not fee_sched.get("unknown")):
        fee_sched = parse_fee_schedule(market) if isinstance(market, dict) else None
    if not isinstance(fee_sched, dict) or fee_sched.get("unknown"):
        return False, "", 0.0

    est_exit_fee = estimate_taker_fee_usd(order_shares, sell_price, fee_sched)
    alloc = float(pos.get("allocated_usd") or (shares * entry_price))
    if alloc <= 0:
        return False, "", 0.0
    net_proceeds = order_shares * sell_price - est_exit_fee
    net_profit_usd = net_proceeds - alloc
    net_gain_per_share = net_profit_usd / order_shares
    if net_profit_usd + 1e-9 < TAKE_PROFIT_MIN_NET_PROFIT_USD or net_gain_per_share + 1e-9 < TAKE_PROFIT_MIN_NET_GAIN_PER_SHARE:
        return False, "", 0.0

    position_bid_usd = order_shares * sell_price
    near_bid_depth = float(summary.get("near_bid_depth_usd") or 0.0)
    best_bid_size = float(summary.get("best_bid_size") or 0.0)
    if near_bid_depth < position_bid_usd or best_bid_size + 1e-9 < order_shares:
        return False, "", 0.0

    fee_per_share = est_exit_fee / order_shares
    min_unit_gain = max(
        TAKE_PROFIT_MIN_NET_GAIN_PER_SHARE,
        TAKE_PROFIT_MIN_NET_PROFIT_USD / order_shares,
    )
    raw_min_px = (alloc / order_shares) + fee_per_share + min_unit_gain
    min_profitable_px = math.ceil(raw_min_px * 10000.0 - 1e-9) / 10000.0
    min_sell_price = round(min(sell_price, max(min_profitable_px, sell_price - 0.002)), 4)
    reason = (
        f"Take-Profit: bid ${sell_price:.4f} (вход ${entry_price:.4f}, прибыль ~+${net_profit_usd:.2f}, "
        f"ост. APR {rem_apr:.1f}% на {days_left:.0f} дн.)"
    )
    return True, reason, min_sell_price


def compute_reaudit_plan(
    pos: Dict[str, Any],
    market: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Tuple[float, bool, str]:
    """Вычисляет (интервал_сек, prefer_backup, режим) для адаптивного реаудита открытой позиции.

    - 'stressed' (есть страйк SKIP или просадка >= 2.0¢ от входа): 1 ч на Venice (веб-поиск).
    - 'watch' (просадка 1.0–2.0¢ или до экспирации <= 3 дн.): 6 ч на Venice (веб-поиск).
    - 'calm' (просадка < 1.0¢, 0 страйков, до экспирации > 3 дн.): 24 ч через Command Code (экономя Venice)."""
    cur_now = float(now if now is not None else time.time())
    streak = int(pos.get("ai_skip_streak", 0))
    entry_price = float(pos.get("entry_price") or pos.get("requested_price") or 0.0)
    current_price = float(pos.get("current_price") or entry_price)
    drop = max(0.0, round(entry_price - current_price, 6)) if entry_price > 0 else 0.0

    end_str = str((market or {}).get("endDate") or pos.get("end_date") or "").strip()
    days_left = _days_left_from_end_date(end_str, cur_now)
    if days_left is None:
        days_left = 14.0
        if pos.get("days_left") is not None:
            try:
                days_left = max(0.0, float(pos["days_left"]))
            except (TypeError, ValueError):
                pass

    if streak > 0 or drop >= REAUDIT_STRESS_DROP:
        return REAUDIT_RECHECK_SEC, False, "stressed"
    if drop >= REAUDIT_WATCH_DROP or days_left <= REAUDIT_NEAR_EXPIRY_DAYS:
        return REAUDIT_INTERVAL_SEC, False, "watch"
    return REAUDIT_CALM_INTERVAL_SEC, True, "calm"


def _position_topic_clusters(text: str) -> Set[str]:
    """Определяет тематические кластеры рынка для контроля корреляции."""
    if not text:
        return set()
    return {name for name, pat in TOPIC_CLUSTER_PATTERNS.items() if pat.search(text)}


def _topic_cluster_blocked(
    question: str,
    open_positions: List[Dict[str, Any]],
    max_per_topic: int = MAX_SLOTS_PER_TOPIC,
) -> Optional[str]:
    """Возвращает имя переполненного кластера (>= max_per_topic открытых слотов) или None."""
    cand_clusters = _position_topic_clusters(question)
    if not cand_clusters:
        return None
    counts: Dict[str, int] = {c: 0 for c in cand_clusters}
    for p in open_positions:
        p_text = f"{p.get('question', '')} {p.get('title', '')}"
        for cl in _position_topic_clusters(p_text):
            if cl in counts:
                counts[cl] += 1
    for cl, cnt in counts.items():
        if cnt >= max_per_topic:
            return cl
    return None


def outcome_price(m: Dict[str, Any], outcome: str, fallback: float) -> float:
    """Возвращает текущую рыночную цену выбранного исхода."""
    outcomes, prices = parse_outcome_prices(m)
    if outcome in outcomes and outcomes.index(outcome) < len(prices):
        return prices[outcomes.index(outcome)]
    return prices[0] if prices else fallback


def market_trading_finished(m: Dict[str, Any]) -> bool:
    """Торги по рынку прекращены (наступила дата экспирации или флаг закрытия)."""
    if m.get("closed") or m.get("acceptingOrders") is False or not m.get("active", True):
        return True

    return False


def market_resolved(m: Dict[str, Any]) -> bool:
    """Исход подтвержден: рынок закрыт и оракул/протокол разрешил его (или цены ровно 0/1)."""
    if not m.get("closed"):
        return False
    if str(m.get("umaResolutionStatus") or "").lower() == "resolved":
        return True
    if str(m.get("resolutionStatus") or "").lower() == "resolved":
        return True
    _, prices = parse_outcome_prices(m)
    return bool(prices) and all(p in (0.0, 1.0) for p in prices)


def extract_asset_id(m: Dict[str, Any], outcome: str) -> Optional[str]:
    """Возвращает ID актива для выбранного исхода: positionIds для v2, clobTokenIds для v1."""
    outcomes, _ = parse_outcome_prices(m)
    idx = outcomes.index(outcome) if outcome in outcomes else 0
    version = str(m.get("version") or "v1").lower()

    if version == "v2":
        raw_ids = m.get("positionIds")
        if isinstance(raw_ids, str):
            try:
                raw_ids = json.loads(raw_ids)
            except Exception:
                raw_ids = []
        ids = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else []
    else:
        ids = [str(x) for x in parse_clob_token_ids(m)]

    return ids[idx] if idx < len(ids) else None


def load_config() -> Dict[str, str]:
    """Загружает параметры из live_config.env и экспортирует недостающие в os.environ."""
    cfg: Dict[str, str] = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                key = k.strip()
                val = v.strip().strip("'\"")
                cfg[key] = val
                if val:
                    os.environ.setdefault(key, val)

    # Переменные окружения имеют приоритет
    for key in [
        "POLY_PRIVATE_KEY",
        "POLY_FUNDER_ADDRESS",
        "POLY_SIGNATURE_TYPE",
        "LIVE_TOTAL_DEPOSIT",
        "LIVE_SLOT_SIZE",
        "LIVE_MAX_SLOTS",
        "VENICE_API_KEY",
        "VENICE_BASE_URL",
        "VENICE_MODEL",
        "VENICE_MODELS",
        "AI_MAX_CALLS_PER_DAY",
        "AI_REAUDIT_RESERVE_CALLS",
    ]:
        if os.environ.get(key):
            cfg[key] = os.environ[key]

    cfg.setdefault("POLY_SIGNATURE_TYPE", DEFAULT_SIGNATURE_TYPE)
    return cfg


def log_live_event(message: str) -> None:
    """Логирует событие в live_trader.log и stdout с таймстампом UTC."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIVE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        print(f"Ошибка записи лога: {exc}", file=sys.stderr)


def init_clob_client(cfg: Dict[str, str]) -> ClobClient:
    """Инициализирует ClobClient с аутентификацией L1/L2 (для чтения баланса/ордеров)."""
    pk = cfg.get("POLY_PRIVATE_KEY", "").strip()
    if not pk:
        raise ValueError("POLY_PRIVATE_KEY не задан в конфигурации!")

    funder = cfg.get("POLY_FUNDER_ADDRESS", "").strip()
    sig_type = int(cfg.get("POLY_SIGNATURE_TYPE", DEFAULT_SIGNATURE_TYPE))
    host = os.environ.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLY_CHAIN_ID", "137"))

    client = ClobClient(
        host=host,
        key=pk,
        chain_id=chain_id,
        signature_type=sig_type,
        funder=funder if funder else None,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    return client


def init_secure_client(cfg: Dict[str, str]) -> SecureClient:
    """Инициализирует официальный unified SecureClient (поддержка DepositWallet, ERC-7739, V2/V3)."""
    pk = cfg.get("POLY_PRIVATE_KEY", "").strip()
    if not pk:
        raise ValueError("POLY_PRIVATE_KEY не задан в конфигурации!")
    funder = cfg.get("POLY_FUNDER_ADDRESS", "").strip()
    return SecureClient.create(
        private_key=pk,
        wallet=funder if funder else None,
    )


def _fetch_onchain_pusd_balance(wallet_address: str) -> float:
    """Резервная проверка ончейн-баланса pUSD (6 знаков) на Polygon через публичный RPC."""
    if not wallet_address:
        return 0.0
    call_data = "0x70a08231" + wallet_address.lower().replace("0x", "").zfill(64)
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": PUSD_CONTRACT, "data": call_data}, "latest"],
        "id": 1,
    }).encode("utf-8")

    for rpc_url in POLYGON_RPCS:
        try:
            req = urllib.request.Request(
                rpc_url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "AI-Yield-Scanner/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                raw_hex = body.get("result")
                if raw_hex and raw_hex != "0x":
                    return round(int(raw_hex, 16) / 1e6, 2)
        except Exception:
            continue
    return 0.0


def get_live_cash_balance(client: ClobClient, cfg: Optional[Dict[str, str]] = None) -> float:
    """Возвращает доступный баланс обеспечения (pUSD/USDC) в долларах."""
    sig_type = int((cfg or {}).get("POLY_SIGNATURE_TYPE", DEFAULT_SIGNATURE_TYPE))
    funder = (cfg or {}).get("POLY_FUNDER_ADDRESS", "").strip()
    try:
        res = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        )
        raw_bal = float(res.get("balance", "0"))
        return round(raw_bal / 1e6, 2)
    except Exception as exc:
        log_live_event(f"⚠️ Ошибка получения баланса CLOB ({exc}), пробуем ончейн pUSD...")
        daemon_alerts.problem("clob_balance", "CLOB API не отдал баланс (используется резерв pUSD)", f"{type(exc).__name__}: {exc}")
        return _fetch_onchain_pusd_balance(funder)


def load_live_state(deposit: float = DEFAULT_TOTAL_DEPOSIT) -> Dict[str, Any]:
    """Загружает или создает состояние реального портфеля."""
    if LIVE_STATE_FILE.exists():
        try:
            state = json.loads(LIVE_STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("positions"), list):
                raise ValueError("Некорректная структура портфеля")
            return state
        except Exception as exc:
            raise RuntimeError("Существующее LIVE-состояние не читается; автоматический сброс запрещён") from exc

    now = time.time()
    state = {
        "schema": 1,
        "mode": "LIVE",
        "initial_deposit_usd": deposit,
        "started_at": now,
        "last_tick_at": now,
        "tick_count": 0,
        "realized_pnl_usd": 0.0,
        "positions": [],
    }
    save_live_state(state)
    return state


def save_live_state(state: Dict[str, Any]) -> None:
    """Атомарно сохраняет состояние в файл."""
    atomic_write_json(LIVE_STATE_FILE, state)
    # Намерение должно попасть на диск до отправки ордера.
    with open(LIVE_STATE_FILE, "rb") as committed:
        os.fsync(committed.fileno())
    dir_fd = os.open(str(LIVE_STATE_FILE.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def find_market_for_pos(pos: Dict[str, Any], markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Находит свежий объект рынка по slug или вопросу (с резервным запросом /markets/slug/{slug})."""
    slug = pos.get("slug")
    if slug:
        for m in markets:
            if m.get("slug") == slug:
                return m
        try:
            fetched = fetch_market_by_slug(slug)
            if fetched is not None:
                return fetched
        except Exception:
            pass

    q = pos.get("question") or pos.get("title", "").split(" [")[0]
    for m in markets:
        if m.get("question") == q:
            return m
    return None


def _safe_fetch_book(token_id: str) -> Optional[Dict[str, Any]]:
    """Безопасно загружает стакан CLOB без падения всего тика на разовом HTTP-сбое."""
    try:
        return fetch_clob_book(token_id)
    except Exception as exc:
        log_live_event(f"⚠️ Не удалось получить стакан для {str(token_id)[:18]}...: {exc}")
        daemon_alerts.problem("clob_book", "ошибка загрузки стакана Polymarket CLOB", f"[{str(token_id)[:12]}] {type(exc).__name__}: {exc}")
        return None


def _parse_book_summary(book: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Сортирует уровни стакана (CLOB отдает их в произвольном/обратном порядке) и извлекает лучшие цены."""
    if not book or not isinstance(book, dict):
        return None
    bids = sorted(
        [(float(b["price"]), float(b["size"])) for b in (book.get("bids") or []) if float(b.get("size", 0)) > 0],
        key=lambda x: x[0],
        reverse=True,
    )
    asks = sorted(
        [(float(a["price"]), float(a["size"])) for a in (book.get("asks") or []) if float(a.get("size", 0)) > 0],
        key=lambda x: x[0],
    )
    best_bid = bids[0][0] if bids else None
    near_bid_depth_usd = (
        round(sum(p * s for p, s in bids if p >= best_bid - BID_DEPTH_BAND - 1e-9), 2)
        if best_bid is not None
        else 0.0
    )
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_bid_size": bids[0][1] if bids else 0.0,
        "near_bid_depth_usd": near_bid_depth_usd,
        "best_ask": asks[0][0] if asks else None,
        "best_ask_size": asks[0][1] if asks else 0.0,
        "tick_size": str(book.get("tick_size") or "0.01"),
        "min_order_size": float(book.get("min_order_size") or 5.0),
    }


def should_trigger_stop_loss(
    pos: Dict[str, Any], summary: Dict[str, Any], stop_px: float
) -> Tuple[bool, str]:
    """Проверяет пробой Stop-Loss с защитой от одиночных ночных проколов пустого стакана."""
    if not summary or summary.get("best_bid") is None:
        pos.pop("stop_breach_streak", None)
        pos.pop("stop_breach_at", None)
        return False, ""
    bid = float(summary["best_bid"])
    if bid >= stop_px:
        pos.pop("stop_breach_streak", None)
        pos.pop("stop_breach_at", None)
        return False, ""

    # Если защитный SELL уже выставлен и не завершён — продолжаем его сопровождение без задержки
    # Ожидающий Take-Profit НЕ отключает защиту от ночных проколов стакана и 2-кратное подтверждение!
    if pos.get("exit_kind") != "take_profit" and any(not x.get("complete") for x in pos.get("exit_orders", [])):
        return True, f"лучший bid ${bid:.4f} ниже стопа ${stop_px:.4f} (-3.5¢)"

    ask = float(summary["best_ask"]) if summary.get("best_ask") is not None else bid
    spread = ask - bid
    mid = (bid + ask) / 2.0
    now = time.time()

    # Если аск остался высоко (mid >= stop_px), а спред разъехался > 3¢ — это дырка в бидах, а не обвал
    if spread > STOP_LOSS_MAX_HOLE_SPREAD and mid >= stop_px:
        pos.pop("stop_breach_streak", None)
        pos.pop("stop_breach_at", None)
        if now - float(pos.get("last_hole_log_at") or 0.0) >= 300.0:
            pos["last_hole_log_at"] = now
            log_live_event(
                f"🛡 Игнорируем прокол пустого стакана {pos.get('title', '')[:38]}: "
                f"bid=${bid:.4f} < SL=${stop_px:.4f}, но ask=${ask:.4f} (mid=${mid:.4f}, спред {spread*100:.1f}¢)"
            )
        return False, ""

    # Экстренный обвал всего стакана (и bid, и ask глубоко под стопом) — выходим сразу с 1-го замера
    if bid <= stop_px - STOP_LOSS_PANIC_OFFSET and ask <= stop_px + 0.005:
        pos["stop_breach_streak"] = STOP_LOSS_CONFIRM_STREAK
        pos["stop_breach_at"] = now
        return True, f"экстренный пробой стопа: bid ${bid:.4f} / ask ${ask:.4f} ниже ${stop_px:.4f}"

    prev_at = float(pos.get("stop_breach_at") or 0.0)
    prev_streak = int(pos.get("stop_breach_streak") or 0)
    if prev_streak > 0 and (prev_at <= 0.0 or not (0.0 <= now - prev_at <= STOP_LOSS_STREAK_MAX_GAP_SEC)):
        prev_streak = 0
    streak = prev_streak + 1
    pos["stop_breach_streak"] = streak
    pos["stop_breach_at"] = now
    if streak < STOP_LOSS_CONFIRM_STREAK:
        log_live_event(
            f"⏳ Пробой стопа {pos.get('title', '')[:38]} ({streak}/{STOP_LOSS_CONFIRM_STREAK}): "
            f"bid=${bid:.4f} < SL=${stop_px:.4f} (ask=${ask:.4f}), ждём подтверждения..."
        )
        return False, ""

    return (
        True,
        f"лучший bid ${bid:.4f} ниже стопа ${stop_px:.4f} (-3.5¢, подтв. {streak}/{STOP_LOSS_CONFIRM_STREAK})",
    )


def _round_price_to_tick(price: float, tick_size_str: str) -> Decimal:
    """Приводит цену к кратности tick_size без хвостов двоичной арифметики float."""
    try:
        tick = Decimal(str(tick_size_str))
        if tick <= 0:
            tick = Decimal("0.01")
    except InvalidOperation:
        tick = Decimal("0.01")
    d_price = Decimal(str(price))
    steps = (d_price / tick).to_integral_value(rounding=ROUND_DOWN)
    rounded = steps * tick
    min_price = tick
    max_price = Decimal("1") - tick
    if rounded < min_price:
        rounded = min_price
    elif rounded > max_price:
        rounded = max_price
    return rounded.normalize()


def _format_decimal(val: Decimal) -> str:
    """Преобразует Decimal в обычную десятичную строку без научной нотации."""
    s = format(val, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _extract_order_result(resp: Any) -> Tuple[bool, str, str, str]:
    """Унифицированно извлекает (ok, order_id, status, error_msg) из OrderResponse или dict."""
    if isinstance(resp, dict):
        ok = bool(resp.get("ok", resp.get("success", False)))
        order_id = str(resp.get("order_id") or resp.get("orderID") or resp.get("id") or "")
        status = str(resp.get("status") or "")
        err = str(resp.get("message") or resp.get("errorMsg") or resp.get("code") or "")
        return ok, order_id, status, err

    ok = bool(getattr(resp, "ok", False))
    if ok:
        order_id = str(getattr(resp, "order_id", "") or "")
        status_raw = getattr(resp, "status", "")
        status = getattr(status_raw, "value", str(status_raw or ""))
        return True, order_id, status, ""

    code = getattr(resp, "code", "")
    msg = getattr(resp, "message", "")
    err = f"{code}: {msg}".strip(": ")
    return False, "", "", err


def _live_badge(cfg: Optional[Dict[str, str]] = None) -> str:
    """Формирует бейдж вида [LIVE $1000] по текущему лимиту LIVE_TOTAL_DEPOSIT."""
    raw = (cfg or {}).get("LIVE_TOTAL_DEPOSIT") or os.environ.get("LIVE_TOTAL_DEPOSIT") or DEFAULT_TOTAL_DEPOSIT
    try:
        dep = int(round(float(raw)))
    except (TypeError, ValueError):
        dep = int(DEFAULT_TOTAL_DEPOSIT)
    return f"[LIVE ${dep}]"


def notify_live(message: str) -> None:
    """Сбой Telegram не меняет результат торговой операции."""
    try:
        send_telegram_alert(message)
    except Exception as exc:
        log_live_event(f"⚠️ Уведомление недоступно: {type(exc).__name__}")


def spendable_after_pending(state: Dict[str, Any], cash: float) -> float:
    reserved = sum(max(0.0, float(p.get("requested_shares", 0)) - float(p.get("confirmed_bought_shares", 0)))
                   * float(p.get("requested_price", p.get("entry_price", 0)))
                   for p in state.get("positions", []) if p.get("status") == "OPEN" and not p.get("entry_terminal"))
    return max(0.0, cash - reserved)


def pos_unknown(state: Dict[str, Any]) -> bool:
    return any((p.get("execution_unknown") or p.get("reconciliation_pending")) for p in state.get("positions", []) if p.get("status") == "OPEN")


def _resolve_pos_fee_schedule(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Возвращает сохранённое расписание комиссий позиции или догружает его из Gamma по slug."""
    sched = pos.get("fee_schedule")
    if isinstance(sched, dict) and not sched.get("unknown"):
        return sched
    slug = pos.get("slug")
    if not slug:
        return None
    try:
        m = fetch_market_by_slug(slug)
        if m:
            parsed = parse_fee_schedule(m)
            if not parsed.get("unknown"):
                pos["fee_schedule"] = parsed
                return parsed
    except Exception:
        pass
    return None


def _confirm_missing_buy(client: SecureClient, state: Dict[str, Any], pos: Dict[str, Any],
                         entry: Dict[str, Any]) -> bool:
    """Признаёт остаток неактивным после двух совпадающих сверок исполнений и кошелька."""
    snapshot = {
        "version": 1,
        "order_id": pos["order_id"],
        "token_id": pos["token_id"],
        "condition_id": pos["condition_id"],
        "requested_shares": float(pos.get("requested_shares", pos["shares"])),
        "confirmed_shares": entry["confirmed_shares"],
        "confirmed_cash": entry["confirmed_cash"],
        "estimated_fee_shares": entry.get("estimated_fee_shares", 0.0),
    }
    recovered = pos.get("missing_buy_recovery") or {}
    if recovered.get("snapshot") == snapshot:
        return True
    if pos.get("exit_orders") or entry.get("other_token_order"):
        raise ValueError("Нельзя восстановить BUY при других заявках по тому же активу")
    if any(p is not pos and p.get("status") == "OPEN" and p.get("token_id") == pos["token_id"]
           for p in state["positions"]):
        raise ValueError("Нельзя однозначно сопоставить баланс нескольким позициям по активу")

    gross = Decimal(str(entry["confirmed_shares"]))
    expected_net = gross - Decimal(str(snapshot["estimated_fee_shares"]))
    previous_gross = Decimal(str(pos.get("confirmed_bought_shares") or 0))
    if gross < previous_gross - Decimal("0.000001"):
        raise ValueError("Подтверждённый объём исчезнувшей заявки уменьшился")
    if entry.get("has_fee") and gross == previous_gross and pos.get("confirmed_net_bought_shares") is not None:
        expected_net = Decimal(str(pos["confirmed_net_bought_shares"]))
    balance = read_conditional_balance(client, pos["token_id"], require_both=True)
    if expected_net <= 0 or abs(balance - expected_net) > Decimal("0.000001"):
        raise ValueError("Остаток кошелька не совпадает с исполнением исчезнувшей заявки")

    observation = {**snapshot, "wallet_shares": str(balance.normalize())}
    now = time.time()
    previous = pos.get("missing_buy_observation") or {}
    elapsed = now - float(previous.get("at") or 0)
    if previous.get("snapshot") != observation or not 0 <= elapsed <= MISSING_BUY_MAX_GAP_SEC:
        pos["missing_buy_observation"] = {"at": now, "snapshot": observation}
        pos["reconciliation_pending"] = True
        save_live_state(state)
        log_live_event(f"⏳ Заявка отсутствует в списках биржи, ждём повторную сверку: {pos['title'][:45]}.")
        return False
    if elapsed < MISSING_BUY_CONFIRM_SEC:
        return False

    pos["missing_buy_recovery"] = {"at": now, "snapshot": snapshot, "wallet_shares": observation["wallet_shares"]}
    pos.pop("missing_buy_observation", None)
    log_live_event(
        f"✅ Сверка BUY восстановлена: {pos['title'][:45]}, "
        f"подтверждено {gross} акций, неисполненный остаток заявки отсутствует на бирже."
    )
    return True


def _confirm_missing_sell(
    client: SecureClient,
    state: Dict[str, Any],
    pos: Dict[str, Any],
    order: Dict[str, Any],
    exit_rec: Dict[str, Any],
    condition_id: str,
    expected_remaining: Decimal,
    entry_has_fee: bool,
    has_other_pending_exits: bool = False,
) -> bool:
    """Признаёт остаток исчезнувшей заявки SELL неактивным после двух совпадающих сверок и проверки кошелька."""
    snapshot = {
        "version": 1,
        "order_id": str(order["order_id"]),
        "token_id": pos["token_id"],
        "condition_id": condition_id,
        "requested_shares": float(order["requested_shares"]),
        "confirmed_shares": exit_rec["confirmed_shares"],
        "confirmed_cash": exit_rec["confirmed_cash"],
        "estimated_fee_usd": exit_rec.get("estimated_fee_usd", 0.0),
        "expected_remaining": str(expected_remaining.normalize()),
    }
    recovered = order.get("missing_sell_recovery") or {}
    if recovered.get("snapshot") == snapshot:
        return True
    if has_other_pending_exits or exit_rec.get("other_token_order"):
        raise ValueError("Нельзя восстановить SELL при других незавершённых заявках по тому же активу")
    if any(p is not pos and p.get("status") == "OPEN" and p.get("token_id") == pos["token_id"]
           for p in state["positions"]):
        raise ValueError("Нельзя однозначно сопоставить баланс нескольким позициям по активу")
    if entry_has_fee and pos.get("confirmed_net_bought_shares") is None:
        raise ValueError("Нельзя восстановить SELL без ранее подтверждённого чистого остатка BUY")

    confirmed_sold = Decimal(str(exit_rec["confirmed_shares"]))
    previous_sold = Decimal(str(order.get("confirmed_shares") or 0))
    if confirmed_sold < previous_sold - Decimal("0.000001"):
        raise ValueError("Подтверждённый объём исчезнувшей заявки SELL уменьшился")
    if expected_remaining < Decimal("0"):
        raise ValueError("Ожидаемый остаток после SELL отрицательный")

    balance = read_conditional_balance(client, pos["token_id"], require_both=True)
    if abs(balance - expected_remaining) > Decimal("0.000001"):
        raise ValueError("Остаток кошелька не совпадает с частичным исполнением исчезнувшей заявки SELL")

    observation = {**snapshot, "wallet_shares": str(balance.normalize())}
    now = time.time()
    previous = order.get("missing_sell_observation") or {}
    elapsed = now - float(previous.get("at") or 0)
    if previous.get("snapshot") != observation or not 0 <= elapsed <= MISSING_BUY_MAX_GAP_SEC:
        order["missing_sell_observation"] = {"at": now, "snapshot": observation}
        pos["reconciliation_pending"] = True
        save_live_state(state)
        log_live_event(f"⏳ Заявка SELL отсутствует в списках биржи, ждём повторную сверку: {pos['title'][:45]}.")
        return False
    if elapsed < MISSING_BUY_CONFIRM_SEC:
        return False

    order["missing_sell_recovery"] = {"at": now, "snapshot": snapshot, "wallet_shares": observation["wallet_shares"]}
    order.pop("missing_sell_observation", None)
    log_live_event(
        f"✅ Сверка SELL восстановлена: {pos['title'][:45]}, "
        f"подтверждена продажа {confirmed_sold} акций, неисполненный остаток заявки отсутствует на бирже."
    )
    return True


def reconcile_position(client: SecureClient, state: Dict[str, Any], pos: Dict[str, Any]) -> bool:
    """Только подтверждённые исполнения меняют количество, стоимость и PnL.

    Учитывает фактические комиссии Polymarket:
    - на BUY комиссия удерживается в акциях → сверяем чистый остаток токена на кошельке;
    - на SELL комиссия удерживается из pUSD → вычитаем из выручки;
    - неторгуемый остаток < 0.01 акции после полной продажи списывается как комиссионная пыль.
    """
    if pos.get("execution_unknown") or not pos.get("order_id"):
        pos["reconciliation_pending"] = True
        return False
    try:
        requested = float(pos.get("requested_shares", pos["shares"]))
        fee_sched = pos.get("fee_schedule") if isinstance(pos.get("fee_schedule"), dict) else None

        def _read_with_schedule(order_id: str, side: str, exp_size: float, market_id: str) -> Dict[str, Any]:
            nonlocal fee_sched
            if side == "BUY":
                probe_missing = bool(pos.get("execution_checked_at") and pos.get("entry_settled") and market_id)
            else:
                probe_missing = bool(pos.get("entry_terminal") and market_id)
            try:
                return read_execution(
                    client, order_id, pos["token_id"], side, exp_size,
                    market=market_id, fee_schedule=fee_sched,
                    probe_missing_partial=probe_missing,
                )
            except ValueError as err:
                if "fee_schedule" in str(err):
                    loaded = _resolve_pos_fee_schedule(pos)
                    if loaded is not None:
                        fee_sched = loaded
                        return read_execution(
                            client, order_id, pos["token_id"], side, exp_size,
                            market=market_id, fee_schedule=fee_sched,
                            probe_missing_partial=probe_missing,
                        )
                raise

        entry = _read_with_schedule(pos["order_id"], "BUY", requested, pos.get("condition_id") or "")
        if entry.get("missing_partial"):
            if not _confirm_missing_buy(client, state, pos, entry):
                return False
            entry.update(status="RECOVERED_PARTIAL", terminal=True, settled=True, complete=True)
        else:
            pos.pop("missing_buy_observation", None)
            if not entry["terminal"]:
                pos.pop("missing_buy_recovery", None)
        condition_id = entry.get("condition_id") or pos.get("condition_id") or ""
        exits = []
        for order in pos.get("exit_orders", []):
            if not order.get("order_id"):
                return False
            exits.append(_read_with_schedule(order["order_id"], "SELL", order["requested_shares"], condition_id))

        gross_bought = entry["confirmed_shares"]
        cost = entry["confirmed_cash"]
        sold = sum(x["confirmed_shares"] for x in exits)
        gross_proceeds = sum(x["confirmed_cash"] for x in exits)
        exit_fee_usd = round(sum(float(x.get("estimated_fee_usd", 0.0)) for x in exits), 6)
        net_proceeds = max(0.0, gross_proceeds - exit_fee_usd)

        # Уменьшение уже подтверждённых накопительных исполнений — противоречивый ответ API.
        if gross_bought + 0.000001 < float(pos.get("confirmed_bought_shares", 0)):
            raise ValueError("Подтверждённые покупки уменьшились")
        if sold + 0.000001 < float(pos.get("confirmed_sold_shares", 0)):
            raise ValueError("Подтверждённые продажи уменьшились")

        # Определяем фактическое чистое количество акций после комиссии BUY
        if not entry.get("has_fee") or gross_bought <= 0:
            net_bought = gross_bought
            entry_fee_shares = 0.0
            entry_fee_usd = 0.0
        else:
            prev_gross = float(pos.get("confirmed_bought_shares", 0.0))
            prev_net = pos.get("confirmed_net_bought_shares")
            if prev_net is not None and abs(gross_bought - prev_gross) <= 0.000001 and float(prev_net) > 0:
                net_bought = float(prev_net)
            else:
                actual_bal = float(read_conditional_balance(client, pos["token_id"]))
                est_fee_sh = float(entry.get("estimated_fee_shares", 0.0))
                expected_net = max(0.0, gross_bought - est_fee_sh)
                observed_net = actual_bal + sold
                if observed_net > gross_bought + 0.000001:
                    raise ValueError(
                        f"Баланс токена ({observed_net:.6f}) превышает купленное ({gross_bought:.6f})"
                    )
                tol = max(0.05, gross_bought * 0.005)
                if observed_net <= 0 or abs(observed_net - expected_net) > tol:
                    pos["reconciliation_pending"] = True
                    save_live_state(state)
                    log_live_event(
                        f"⏳ Ожидание зачисления чистых долей {pos.get('title', '')[:40]}: "
                        f"баланс={observed_net:.4f}, ожид.≈{expected_net:.4f}"
                    )
                    return False
                net_bought = observed_net
            entry_fee_shares = max(0.0, round(gross_bought - net_bought, 8))
            gross_avg = cost / gross_bought if gross_bought > 0 else float(pos["entry_price"])
            entry_fee_usd = round(entry_fee_shares * gross_avg, 6)

        if sold > net_bought + 0.000001:
            raise ValueError("Продано больше подтверждённого чистого количества")

        exit_orders_list = pos.get("exit_orders", [])
        unrecovered_missing = []
        for idx, (order, exit_rec) in enumerate(zip(exit_orders_list, exits)):
            if exit_rec.get("missing_partial"):
                sold_up_to_idx = sum(Decimal(str(x["confirmed_shares"])) for x in exits[:idx + 1])
                expected_rem = max(Decimal("0"), Decimal(str(round(net_bought, 8))) - sold_up_to_idx)
                has_other_pending = any(
                    j != idx and (exits[j].get("missing_partial") or not (exits[j]["terminal"] and exits[j]["settled"]))
                    for j in range(len(exits))
                )
                # Проверяем кэш или ставим на восстановление по порядку
                unrecovered_missing.append((idx, order, exit_rec, expected_rem, has_other_pending))
            else:
                order.pop("missing_sell_observation", None)
                if not exit_rec["terminal"]:
                    order.pop("missing_sell_recovery", None)

        if unrecovered_missing:
            for idx, order, exit_rec, expected_rem, _ in unrecovered_missing:
                if not (entry["terminal"] and entry["settled"]):
                    raise ValueError("Нельзя восстановить SELL при незавершённой заявке BUY")
                has_other_pending = any(
                    j != idx and not (exits[j]["terminal"] and exits[j]["settled"])
                    for j in range(len(exits))
                )
                if not _confirm_missing_sell(
                    client, state, pos, order, exit_rec, condition_id,
                    expected_rem, bool(entry.get("has_fee")),
                    has_other_pending_exits=has_other_pending,
                ):
                    return False
                exit_rec.update(status="RECOVERED_PARTIAL", terminal=True, settled=True, complete=True)

        remaining = max(0.0, net_bought - sold)
        avg = cost / net_bought if net_bought > 0 else float(pos["entry_price"])
        all_complete = entry["complete"] and all(x["complete"] for x in exits)

        # Неторгуемый комиссионный остаток < 0.01 акции после полной продажи закрываем честно
        if all_complete and net_bought > 0 and sold > 0 and remaining < DUST_SHARES_THRESHOLD:
            dust_shares = round(remaining, 8)
            remaining = 0.0
            realized = round(net_proceeds - cost, 6)
            if dust_shares > 0:
                pos["dust_shares_written_off"] = dust_shares
        else:
            realized = round(net_proceeds - sold * avg, 6)

        delta = realized - float(pos.get("execution_realized_pnl_usd", 0.0))
        pos.update(
            requested_shares=requested,
            requested_price=pos.get("requested_price", pos["entry_price"]),
            confirmed_bought_shares=gross_bought,
            confirmed_net_bought_shares=round(net_bought, 8),
            confirmed_sold_shares=sold,
            entry_confirmed_cost_usd=cost,
            entry_fee_shares=entry_fee_shares,
            entry_fee_usd=entry_fee_usd,
            exit_fee_usd=exit_fee_usd,
            shares=round(remaining, 8),
            allocated_usd=round(remaining * avg, 6),
            entry_price=avg,
            stop_loss_price=round(max(BOND_STOP_LOSS_FLOOR, avg - BOND_STOP_LOSS_OFFSET), 4) if avg > 0 else BOND_STOP_LOSS,
            order_status=entry["status"],
            entry_terminal=entry["terminal"],
            entry_settled=entry["settled"],
            execution_realized_pnl_usd=realized,
            realized_pnl_usd=realized,
            execution_checked_at=time.time(),
        )
        if condition_id:
            pos["condition_id"] = condition_id
        state["realized_pnl_usd"] = round(float(state.get("realized_pnl_usd", 0)) + delta, 6)
        for order, result in zip(pos.get("exit_orders", []), exits):
            order.update(
                status=result["status"],
                complete=result["complete"],
                confirmed_shares=result["confirmed_shares"],
                confirmed_cash=result["confirmed_cash"],
                estimated_fee_usd=result.get("estimated_fee_usd", 0.0),
            )
        if all_complete and remaining < 0.000001:
            pos.update(status="CLOSED" if net_bought > 0 else "CANCELLED", closed_at=time.time(), unrealized_pnl_usd=0.0)
        settled = entry["settled"] and all(x["settled"] for x in exits)
        pos["reconciliation_pending"] = not settled
        save_live_state(state)
        return settled
    except Exception as exc:
        pos["reconciliation_pending"] = True
        pos.pop("missing_buy_observation", None)
        for exit_order in pos.get("exit_orders", []):
            exit_order.pop("missing_sell_observation", None)
        detail = " ".join(str(exc).split())[:240]
        log_live_event(
            f"⚠️ Сверка исполнения {pos.get('title', '')[:45]} недоступна: "
            f"{type(exc).__name__}: {detail}; состояние сохранено."
        )
        return False


def fetch_fee_rate_bps(token_id: str) -> Optional[Decimal]:
    """Запрашивает base_fee (bps) по token_id в Polymarket CLOB; при ошибке возвращает None."""
    url = "https://clob.polymarket.com/fee-rate?token_id=" + urllib.parse.quote(token_id, safe="")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.load(response)
        fee = Decimal(str(data["base_fee"]))
        return fee if fee.is_finite() and fee >= 0 else None
    except Exception:
        return None


def entry_fee_allowed(market: Optional[Dict[str, Any]], token_id: str) -> Tuple[bool, Dict[str, Any], str]:
    """Проверяет допустимость комиссии для нового входа (fail-closed при сбое API или неизвестном тарифе)."""
    if not token_id:
        return False, {}, "отсутствует token_id"
    base_fee = fetch_fee_rate_bps(token_id)
    if base_fee is None:
        return False, {}, "недоступен /fee-rate"
    if base_fee == 0:
        return True, {
            "enabled": False,
            "rate": 0.0,
            "exponent": 1,
            "taker_only": True,
            "unknown": False,
            "base_fee_bps": 0,
        }, "0%"
    sched = parse_fee_schedule(market or {})
    if not sched.get("enabled") or sched.get("unknown"):
        return False, {}, f"base_fee={base_fee}, но нет валидного feeSchedule"
    rate = float(sched.get("rate", 0.0))
    if rate <= 0 or rate > MAX_ENTRY_FEE_RATE:
        return False, {}, f"высокая ставка комиссии rate={rate:.2f} > {MAX_ENTRY_FEE_RATE:.2f}"
    sched["base_fee_bps"] = int(base_fee)
    return True, sched, f"rate={rate:.2f}"


def _buy_rejection_reason(error: Exception) -> Optional[str]:
    """Признаёт только явный отказ площадки; дубли и сбои транспорта требуют сверки."""
    if not isinstance(error, RequestRejectedError):
        return None
    if error.status == 503 and error.code == "post_only_mode":
        return "post_only_mode"
    if error.status != 400:
        return None
    known = {
        "not_enough_balance": "not enough balance / allowance",
        "invalid_expiration": "invalid expiration",
        "market_not_ready": "the market is not yet ready to process new orders",
        "post_only_would_cross": "invalid post-only order: order crosses book",
    }
    if error.code:
        return error.code if error.code in known else None
    message = str(error).strip().lower()
    if message.startswith("not enough balance / allowance:"):
        return "not_enough_balance"
    return next((reason for reason, text in known.items() if message == text), None)


def execute_buy_bond(
    secure_client: SecureClient,
    candidate: Dict[str, Any],
    slot_size: float,
    state: Dict[str, Any],
    min_apr: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """Сохраняет намерение до отправки; принятый ордер резервирует слот до сверки."""
    token_id = str(candidate.get("token_id") or "")
    fee_sched = candidate.get("fee_schedule")
    if isinstance(fee_sched, dict) and not fee_sched.get("unknown"):
        base_fee = fetch_fee_rate_bps(token_id) if token_id else None
        if base_fee is None:
            log_live_event("⚠️ Новый вход пропущен: не удалось перепроверить /fee-rate.")
            return None
        if base_fee > 0 and (not fee_sched.get("enabled") or float(fee_sched.get("rate", 0)) > MAX_ENTRY_FEE_RATE):
            log_live_event(f"⚠️ Новый вход пропущен: несоответствие комиссии (base_fee={base_fee}).")
            return None
        fee_sched = dict(fee_sched)
        fee_sched["base_fee_bps"] = int(base_fee)
    else:
        fee_ok, fee_sched, fee_reason = entry_fee_allowed(candidate.get("market"), token_id)
        if not fee_ok:
            log_live_event(f"⚠️ Новый вход пропущен: комиссия не допущена ({fee_reason}).")
            return None
    summary = _parse_book_summary(_safe_fetch_book(token_id)) if token_id else None
    if not summary or summary["best_ask"] is None:
        return None
    ask = float(summary["best_ask"])
    bid = summary.get("best_bid")
    if not MIN_ENTRY_PRICE <= ask <= MAX_ENTRY_PRICE:
        return None
    if bid is None or (ask - float(bid)) > MAX_ENTRY_SPREAD:
        log_live_event("⚠️ Новый вход пропущен: спред стакана шире 1.5¢ или отсутствует bid.")
        return None
    if float(summary.get("near_bid_depth_usd") or 0.0) < MIN_ENTRY_BID_DEPTH_USD:
        log_live_event(
            f"⚠️ Новый вход пропущен: тонкий стакан на покупку (${float(summary.get('near_bid_depth_usd') or 0.0):.0f} < ${MIN_ENTRY_BID_DEPTH_USD:.0f})."
        )
        return None
    price = _round_price_to_tick(ask, summary["tick_size"])
    shares = math.floor(slot_size / float(price) * 100) / 100
    if shares < summary["min_order_size"] or shares <= 0:
        return None
    est_fee_usd = estimate_taker_fee_usd(shares, float(price), fee_sched)
    est_net_shares = max(0.0, shares - (est_fee_usd / float(price)))
    est_cost = shares * float(price)
    est_net_ret_pct = ((est_net_shares - est_cost) / est_cost) * 100.0 if est_cost > 0 else 0.0
    if est_net_ret_pct < MIN_NET_RETURN_PCT:
        log_live_event(
            f"⚠️ Новый вход пропущен: чистая доходность {est_net_ret_pct:.2f}% ниже минимума {MIN_NET_RETURN_PCT:.2f}%."
        )
        return None
    final_days = _days_left_from_end_date(str(candidate.get("end_date") or ""))
    if final_days is None:
        final_days = float(candidate.get("days_left") or 14.0)
    final_apr = est_net_ret_pct * (365.0 / max(1.0, final_days))
    if min_apr > 0 and final_apr < min_apr:
        log_live_event(
            f"⚠️ Новый вход пропущен: финальный APR {final_apr:.1f}% по цене ${float(price):.4f} "
            f"ниже порога {min_apr:.1f}%."
        )
        return None
    now = time.time()
    pos = dict(id=f"live_bond_{token_id[:12]}_{time.time_ns()}", type="poly_bond", status="OPEN",
               opened_at=now, title=f"{candidate['question']} [{candidate['lead_outcome']}]",
               question=candidate["question"], slug=candidate.get("slug", ""),
               event_id=candidate.get("event_id", ""), end_date=candidate.get("end_date", ""),
               outcome=candidate["lead_outcome"], token_id=token_id,
               fee_schedule=fee_sched,
               requested_shares=shares, requested_price=float(price), shares=0.0, allocated_usd=0.0,
               entry_price=float(price), current_price=float(price),
               stop_loss_price=round(max(BOND_STOP_LOSS_FLOOR, float(price) - BOND_STOP_LOSS_OFFSET), 4),
               realized_pnl_usd=0.0,
               unrealized_pnl_usd=0.0, expected_apr=round(final_apr, 2),
               ai_audit=candidate.get("ai_audit", {}), last_audit_ts=now,
               order_status="SUBMITTING", execution_unknown=True)
    state.setdefault("positions", []).append(pos)
    save_live_state(state)  # При таймауте не отправлять вторую покупку.
    try:
        resp = secure_client.place_limit_order(asset_id=token_id, side="BUY",
                    price=_format_decimal(price), size=f"{shares:.2f}")
        ok, order_id, status, err = _extract_order_result(resp)
        if not ok:
            if type(resp).__name__ != "RejectedOrder":
                raise RuntimeError("Неоднозначный отказ BUY; требуется сверка")
            pos.update(status="REJECTED", closed_at=time.time(), order_status="REJECTED", execution_unknown=False)
            save_live_state(state)
            notify_live(f"⚠️ <b>[LIVE] Покупка отклонена</b>\n{format_market_link(pos['title'], pos['slug'])}")
            return None
        if not order_id:
            raise ValueError("Ответ биржи без order_id")
        pos.update(order_id=order_id, order_status=status, execution_unknown=False)
        save_live_state(state)
    except Exception as exc:
        rejection = _buy_rejection_reason(exc)
        if rejection and not pos.get("order_id"):
            pos.update(status="REJECTED", closed_at=time.time(), order_status="REJECTED",
                       execution_unknown=False, reconciliation_pending=False, rejection_reason=rejection)
            save_live_state(state)
            log_live_event(f"⚠️ Биржа явно отклонила BUY: HTTP {exc.status}, {rejection}; слот свободен.")
            notify_live(f"⚠️ <b>[LIVE] Покупка отклонена</b>\n{format_market_link(pos['title'], pos['slug'])}\n"
                        f"Причина: <code>{esc(rejection)}</code>.")
            return None
        pos.update(status="OPEN", execution_unknown=True)
        detail = type(exc).__name__
        if isinstance(exc, RequestRejectedError):
            safe_code = exc.code if isinstance(exc.code, str) and re.fullmatch(r"[a-z_]{1,40}", exc.code) else None
            detail += f", HTTP {exc.status}" + (f", код {safe_code}" if safe_code else "")
            pos["submit_http_status"] = exc.status
            if safe_code:
                pos["submit_error_code"] = safe_code
        save_live_state(state)
        log_live_event(f"🚨 Результат BUY неизвестен: {detail}; повтор заблокирован.")
        notify_live("🚨 <b>[LIVE] Результат покупки требует сверки.</b> Повторные входы заблокированы; действующие позиции сопровождаются.")
        return pos
    reconcile_position(secure_client, state, pos)
    audit = pos.get("ai_audit") or {}
    confirmed = float(pos.get("shares") or 0.0)
    entry_px = float(pos.get("entry_price") or price)
    price_txt = f"{entry_px:.4f}".rstrip("0").rstrip(".") or "0"
    req_cost = round(shares * float(price), 2)
    alloc = float(pos.get("allocated_usd") or 0.0)
    ord_status = str(pos.get("order_status") or status or "accepted")
    apr = float(pos.get("expected_apr") or 0.0)
    real_fee_usd = float(pos.get("entry_fee_usd") or 0.0)
    real_fee_sh = float(pos.get("entry_fee_shares") or 0.0)

    badge = _live_badge()
    if pos.get("entry_terminal") and confirmed > 0:
        header = f"🔴 <b>{badge} ОТКРЫТА ПОЗИЦИЯ SMART BONDING</b>"
        sum_line = f"💵 <b>Сумма:</b> <code>${alloc:.2f} USDC</code> ({confirmed:.2f} чистых акций, eff. ${price_txt})"
    else:
        header = f"🔴 <b>{badge} ЗАЯВКА BUY ПРИНЯТА (SMART BONDING)</b>"
        sum_line = (
            f"💵 <b>Заявка:</b> <code>${req_cost:.2f} USDC</code> "
            f"(запрошено {shares:.2f}, подтв. {confirmed:.2f} акций по ${price_txt})"
        )

    if real_fee_usd > 0:
        fee_line = f"💸 <b>Комиссия входа (факт):</b> <code>${real_fee_usd:.2f}</code> ({real_fee_sh:.3f} акций)\n"
    elif est_fee_usd > 0:
        fee_line = f"💸 <b>Комиссия входа (оценка):</b> <code>~${est_fee_usd:.2f}</code>\n"
    else:
        fee_line = ""

    votes = audit.get("votes") or []
    if len(votes) > 1:
        votes_txt = ", ".join(f"{v.get('model')}: {v.get('confidence')}%" for v in votes)
        audit_tag = f" [<code>{esc(votes_txt)}</code>]"
    elif audit.get("model_used"):
        audit_tag = f" [<code>{esc(audit['model_used'])}</code>]"
    else:
        audit_tag = ""

    stop_px = pos_stop_loss_price(pos)
    notify_live(
        f"{header}\n\n"
        f"🎯 <b>Рынок:</b> {format_market_link(pos['title'], pos.get('slug', ''))}\n"
        f"{sum_line}\n"
        f"{fee_line}"
        f"🛡 <b>Защитный Stop-Loss:</b> <code>${stop_px:.4f}</code> (-3.5¢)\n"
        f"📈 <b>Ожидаемый чистый APR:</b> <code>+{apr:.1f}%</code>\n"
        f"🤖 <b>Venice AI аудит:</b> {esc(audit.get('decision', 'APPROVE'))} ({int(audit.get('confidence', 0))}%){audit_tag}\n"
        f"📝 <i>{esc(str(audit.get('summary', ''))[:1200])}</i>\n"
        f"🆔 <b>Order:</b> <code>{esc(str(order_id)[:24])}</code> ({esc(ord_status)})"
    )
    return pos


def _abort_take_profit(pos: Dict[str, Any], state: Dict[str, Any], msg: str) -> bool:
    """Сбрасывает пометки Take-Profit и оставляет позицию открытой."""
    log_live_event(msg)
    pos.pop("take_profit_min_price", None)
    pos.pop("exit_kind", None)
    pos.pop("close_reason", None)
    save_live_state(state)
    return False


def execute_sell_bond(
    secure_client: SecureClient,
    pos: Dict[str, Any],
    reason: str,
    state: Dict[str, Any],
    min_sell_price: Optional[float] = None,
) -> bool:
    """Не освобождает слот до подтверждения SELL и не «зависает» на незаполненной заявке."""
    is_take_profit = min_sell_price is not None
    if is_take_profit:
        pos["exit_kind"] = "take_profit"
        pos["take_profit_min_price"] = float(min_sell_price)
    else:
        pos.pop("take_profit_min_price", None)
        pos["exit_kind"] = "protective"

    if not reconcile_position(secure_client, state, pos) or pos.get("status") != "OPEN":
        return False

    summary = _parse_book_summary(_safe_fetch_book(pos["token_id"]))
    if not summary or summary["best_bid"] is None or summary["best_bid"] <= 0:
        log_live_event("⚠️ Нет доступного bid для защитной продажи; позиция остаётся под наблюдением.")
        daemon_alerts.problem(
            f"sell_blocked_{pos.get('id', 'pos')}",
            f"нет bid для защитной продажи {pos.get('title', '')[:35]}",
            reason,
            immediate=True,
        )
        return False

    best_bid = float(summary["best_bid"])
    pending_exits = [x for x in pos.get("exit_orders", []) if not x.get("complete")]
    if pending_exits:
        now = time.time()
        to_cancel = []
        for order in pending_exits:
            if not order.get("order_id"):
                log_live_event("⚠️ Незаполненный SELL без order_id; нужна ручная сверка.")
                daemon_alerts.problem(
                    f"sell_blocked_{pos.get('id', 'pos')}",
                    f"SELL без order_id у {pos.get('title', '')[:35]}",
                    reason,
                    immediate=True,
                )
                return False
            submitted_at = float(order.get("submitted_at") or 0.0)
            age = now - submitted_at if submitted_at > 0 else float("inf")
            requested_price = float(order.get("requested_price") or 0.0)
            if age < SELL_REPLACE_AFTER_SEC and requested_price > 0 and requested_price <= best_bid + 1e-9:
                continue  # заявка молодая и стоит на текущем bid — ждём исполнения
            to_cancel.append(order)

        if not to_cancel:
            return False

        for order in to_cancel:
            order_id = str(order["order_id"])
            old_price = float(order.get("requested_price") or 0.0)
            try:
                secure_client.cancel_order(order_id=order_id)
            except Exception as exc:
                # Ордер мог уже исполниться/отмениться — истину покажет сверка ниже.
                log_live_event(
                    f"⚠️ Отмена SELL {old_price:.4f} не подтверждена: {type(exc).__name__}: "
                    f"{' '.join(str(exc).split())[:160]}"
                )
            if not reconcile_position(secure_client, state, pos):
                log_live_event(f"⚠️ Ждём подтверждения отмены SELL ${old_price:.4f}; новая заявка не отправлена.")
                return False
            if any(not x.get("complete") for x in pos.get("exit_orders", [])):
                log_live_event(f"⚠️ SELL ${old_price:.4f} ещё не отменён/не исполнен; новая заявка не отправлена.")
                daemon_alerts.problem(
                    f"sell_requote_{pos.get('id', 'pos')}",
                    f"SELL не перевыставлен: {pos.get('title', '')[:35]}",
                    f"стакан bid={best_bid:.4f}, заявка ${old_price:.4f}",
                    immediate=True,
                )
                return False
            log_live_event(f"🔁 Незаполненный SELL ${old_price:.4f} отменён.")

        if pos.get("status") != "OPEN":
            return pos.get("status") == "CLOSED"

    # Сначала подтверждаем отмену неисполненного остатка BUY, чтобы он не наполнился после выхода.
    if not pos.get("entry_terminal"):
        try:
            secure_client.cancel_order(order_id=pos["order_id"])
            if not reconcile_position(secure_client, state, pos) or not pos.get("entry_terminal"):
                return False
        except Exception as exc:
            log_live_event(f"⚠️ Отмена остатка BUY не подтверждена: {type(exc).__name__}")
            daemon_alerts.problem(
                f"sell_blocked_{pos.get('id', 'pos')}",
                f"защитная продажа {pos.get('title', '')[:35]} заблокирована (не отменен BUY)",
                f"{type(exc).__name__}: {exc}",
                immediate=True,
            )
            return False

    shares = math.floor((float(pos["shares"]) + 1e-9) * 100) / 100
    if shares <= 0:
        log_live_event("⚠️ Остаток SELL равен нулю; позиция остаётся под наблюдением.")
        return False

    # После отмены стакан мог сдвинуться: перечитываем bid.
    # Защитный выход ставим агрессивно (-1 тик), Take-Profit — по текущему bid с проверкой min_sell_price.
    summary = _parse_book_summary(_safe_fetch_book(pos["token_id"]))
    if not summary or summary["best_bid"] is None or summary["best_bid"] <= 0:
        if is_take_profit:
            return _abort_take_profit(
                pos, state, "🟢 Take-Profit отменён: исчез bid в стакане, позиция остаётся в портфеле."
            )
        log_live_event("⚠️ Нет доступного bid для защитной продажи после отмены; позиция под наблюдением.")
        daemon_alerts.problem(
            f"sell_blocked_{pos.get('id', 'pos')}",
            f"нет bid для защитной продажи после отмены {pos.get('title', '')[:35]}",
            reason,
            immediate=True,
        )
        return False
    if shares < summary["min_order_size"]:
        log_live_event("⚠️ Остаток меньше минимального размера SELL; требуется ручной контроль.")
        daemon_alerts.problem(
            f"sell_blocked_{pos.get('id', 'pos')}",
            f"остаток {shares:.2f} меньше минимума SELL ({summary['min_order_size']}) для {pos.get('title', '')[:30]}",
            reason,
            immediate=True,
        )
        return False

    best_bid = float(summary["best_bid"])
    try:
        tick = Decimal(str(summary["tick_size"]))
        if tick <= 0:
            tick = Decimal("0.01")
    except InvalidOperation:
        tick = Decimal("0.01")
    aggressive_ticks = 0 if is_take_profit else SELL_AGGRESSIVE_TICKS
    raw_price = Decimal(str(best_bid)) - tick * Decimal(aggressive_ticks)
    price = _round_price_to_tick(float(max(tick, raw_price)), summary["tick_size"])

    if is_take_profit:
        if float(price) + 1e-9 < float(min_sell_price):
            return _abort_take_profit(
                pos,
                state,
                "🟢 Take-Profit отменён: bid опустился ниже минимальной прибыльной цены, позиция остаётся в портфеле.",
            )
        if float(summary.get("best_bid_size") or 0.0) + 1e-9 < shares:
            return _abort_take_profit(
                pos,
                state,
                f"🟢 Take-Profit отменён: объём на лучшем bid ({float(summary.get('best_bid_size') or 0.0):.2f}) меньше позиции ({shares:.2f}).",
            )
        tp_fee_sched = pos.get("fee_schedule")
        if not (isinstance(tp_fee_sched, dict) and not tp_fee_sched.get("unknown")):
            tp_fee_sched = _resolve_pos_fee_schedule(pos)
        tp_alloc = float(pos.get("allocated_usd") or (float(pos.get("shares") or shares) * float(pos.get("entry_price") or 0.0)))
        if tp_fee_sched is None or tp_fee_sched.get("unknown") or tp_alloc <= 0:
            return _abort_take_profit(
                pos, state, "🟢 Take-Profit отменён: неизвестно расписание комиссий или стоимость позиции."
            )
        tp_fee = estimate_taker_fee_usd(shares, float(price), tp_fee_sched)
        tp_net_profit = shares * float(price) - tp_fee - tp_alloc
        tp_unit_gain = tp_net_profit / shares
        if tp_net_profit + 1e-9 < TAKE_PROFIT_MIN_NET_PROFIT_USD or tp_unit_gain + 1e-9 < TAKE_PROFIT_MIN_NET_GAIN_PER_SHARE:
            return _abort_take_profit(
                pos,
                state,
                f"🟢 Take-Profit отменён: чистая прибыль ${tp_net_profit:.2f} по цене ${float(price):.4f} ниже порога.",
            )

    log_live_event(
        f"🔁 Выставляем SELL: ${float(price):.4f} "
        f"(bid ${best_bid:.4f}, {aggressive_ticks} тик ниже)."
    )

    order = dict(requested_shares=shares, requested_price=float(price), submitted_at=time.time(),
                 status="SUBMITTING", complete=False)
    pos.setdefault("exit_orders", []).append(order)
    pos.update(close_reason=reason, execution_unknown=True)
    save_live_state(state)
    try:
        resp = secure_client.place_limit_order(asset_id=pos["token_id"], side="SELL",
                    price=_format_decimal(price), size=f"{shares:.2f}")
        ok, order_id, status, err = _extract_order_result(resp)
        if not ok:
            if type(resp).__name__ != "RejectedOrder":
                raise RuntimeError("Неоднозначный отказ SELL; требуется сверка")
            pos["exit_orders"].pop()
            pos["execution_unknown"] = False
            if is_take_profit:
                pos.pop("take_profit_min_price", None)
                pos.pop("exit_kind", None)
                pos.pop("close_reason", None)
            save_live_state(state)
            if not is_take_profit:
                notify_live("🚨 <b>[LIVE] Защитная продажа отклонена.</b> Позиция остаётся открытой.")
            return False
        if not order_id:
            raise ValueError("Ответ биржи без order_id")
        order.update(order_id=order_id, status=status)
        pos["execution_unknown"] = False
        save_live_state(state)
    except Exception as exc:
        pos["execution_unknown"] = True
        save_live_state(state)
        log_live_event(f"🚨 Результат SELL неизвестен: {type(exc).__name__}; повтор заблокирован.")
        notify_live("🚨 <b>[LIVE] Продажа требует сверки.</b> Позиция не считается закрытой; повторная продажа заблокирована.")
        return False
    reconcile_position(secure_client, state, pos)
    sell_header = (
        "🟢 <b>[LIVE] Заявка Take-Profit SELL принята</b>"
        if is_take_profit
        else "🔴 <b>[LIVE] Заявка SELL принята</b>"
    )
    notify_live(
        f"{sell_header}\n{format_market_link(pos['title'], pos.get('slug', ''))}\n"
        f"Причина: {esc(reason)}\nОстаток позиции: {pos['shares']:.2f} акций."
    )
    return pos.get("status") == "CLOSED"


def _is_cached_audit(audit: Dict[str, Any], tick_now: float) -> bool:
    """Проверяет, был ли ответ Venice AI взят из кэша, а не получен новым платным вызовом."""
    if str(audit.get("model_used", "")).startswith("none"):
        return True
    return float(audit.get("audited_at") or 0.0) < tick_now


def run_live_tick(cfg: Dict[str, str]) -> None:
    """Один полный рабочий тик live-демона."""
    secure_client = init_secure_client(cfg)
    try:
        total_deposit = float(cfg.get("LIVE_TOTAL_DEPOSIT", str(DEFAULT_TOTAL_DEPOSIT)))
        slot_size = float(cfg.get("LIVE_SLOT_SIZE", str(DEFAULT_SLOT_SIZE)))
        max_slots = int(cfg.get("LIVE_MAX_SLOTS", str(DEFAULT_MAX_SLOTS)))
        state = load_live_state(deposit=total_deposit)
        now = time.time()
        state["tick_count"] = int(state.get("tick_count", 0)) + 1
        state["last_tick_at"] = now
        reconciliation_ok = True
        open_positions = [p for p in state.get("positions", []) if p.get("status") == "OPEN"]
        log_live_event(f"--- Тик #{state['tick_count']} | Открыто слотов: {len(open_positions)}/{max_slots} ---")

        # Защита открытых позиций выполняется до баланса и общего сканирования Gamma.
        for pos in open_positions:
            try:
                stop_px = pos_stop_loss_price(pos)
                if not reconcile_position(secure_client, state, pos):
                    reconciliation_ok = False
                    # Неизвестное количество не продаём повторно, но цену продолжаем проверять.
                    book = _parse_book_summary(_safe_fetch_book(pos["token_id"]))
                    crossed = book and book["best_bid"] is not None and book["best_bid"] < stop_px
                    if (crossed or pos.get("execution_unknown")) and now - float(pos.get("last_sync_alert_at", 0)) >= 1800:
                        pos["last_sync_alert_at"] = now
                        save_live_state(state)
                        notify_live(f"🚨 <b>[LIVE] Требуется проверка исполнения</b>\n{format_market_link(pos['title'], pos.get('slug', ''))}\n"
                                    "Автоматическая повторная заявка заблокирована до сверки. "
                                    + (f"Цена ниже стопа ${stop_px:.4f}!" if crossed else "Результат отправки ордера неизвестен."))
                    continue
                if pos.get("status") != "OPEN":
                    continue
                stop_px = pos_stop_loss_price(pos)
                shares = float(pos["shares"])
                entry_price = float(pos["entry_price"])
                summary = _parse_book_summary(_safe_fetch_book(pos["token_id"]))
                if summary and summary["best_bid"] is not None:
                    bid = float(summary["best_bid"])
                    ask = float(summary["best_ask"]) if summary["best_ask"] is not None else bid
                    pos["current_price"] = round((bid + ask) / 2, 4)
                    pos["unrealized_pnl_usd"] = round(shares * (pos["current_price"] - entry_price), 6)
                    trigger_sl, sl_reason = should_trigger_stop_loss(pos, summary, stop_px)
                    save_live_state(state)
                    if trigger_sl:
                        finished = find_market_for_pos(pos, [])
                        if not finished or not market_trading_finished(finished):
                            execute_sell_bond(secure_client, pos, sl_reason, state)
                            continue
                else:
                    if "stop_breach_streak" in pos or "stop_breach_at" in pos:
                        pos.pop("stop_breach_streak", None)
                        pos.pop("stop_breach_at", None)
                        save_live_state(state)
                    log_live_event(f"⚠️ Нет bid для {pos['title'][:40]}; позиция не считается закрытой.")

                m = find_market_for_pos(pos, [])
                if m:
                    if m.get("endDate") and not pos.get("end_date"):
                        pos["end_date"] = m["endDate"]
                    ev_id = extract_event_id(m)
                    if ev_id and not pos.get("event_id"):
                        pos["event_id"] = ev_id
                pending_exit = any(not x.get("complete") for x in pos.get("exit_orders", []))
                if pending_exit and (not m or not market_trading_finished(m)):
                    tp_floor = pos.get("take_profit_min_price") if pos.get("exit_kind") == "take_profit" else None
                    execute_sell_bond(
                        secure_client,
                        pos,
                        pos.get("close_reason") or "сопровождение незаполненного SELL",
                        state,
                        min_sell_price=tp_floor,
                    )
                    continue
                if m and market_resolved(m) and pos.get("entry_terminal") and not pending_exit:
                    payout = outcome_price(m, pos.get("outcome", "Yes"), float("nan"))
                    if not math.isfinite(payout) or not 0 <= payout <= 1:
                        raise ValueError("Некорректная выплата исхода")
                    pnl = round(shares * (payout - entry_price), 6)
                    pos.update(status="RESOLVED", closed_at=now, current_price=payout,
                               realized_pnl_usd=round(float(pos.get("realized_pnl_usd", 0)) + pnl, 6),
                               unrealized_pnl_usd=0.0, redemption_pending=True)
                    state["realized_pnl_usd"] = round(float(state.get("realized_pnl_usd", 0)) + pnl, 6)
                    save_live_state(state)
                    notify_live(f"🟢 <b>[LIVE] Рынок разрешён</b>\n{format_market_link(pos['title'], pos.get('slug', ''))}\n"
                                f"Выплата: ${payout:.4f}/акция. Получение средств (redeem) отдельно не подтверждено.")
                    continue
                if not m or market_trading_finished(m) or shares <= 0 or pending_exit:
                    continue
                if summary and summary.get("best_bid") is not None:
                    trigger_tp, tp_reason, tp_min_px = should_trigger_take_profit(pos, summary, m, now)
                    if trigger_tp:
                        execute_sell_bond(secure_client, pos, tp_reason, state, min_sell_price=tp_min_px)
                        continue
                streak = int(pos.get("ai_skip_streak", 0))
                interval, prefer_cc, _reaudit_tier = compute_reaudit_plan(pos, m, now)
                if now - float(pos.get("last_audit_ts", 0)) >= interval:
                    audit = call_venice_market_audit(
                        question=pos.get("question") or pos["title"].split(" [")[0],
                        description=m.get("description", ""), price_yes=float(pos.get("current_price", entry_price)),
                        strategy_type="polymarket_smart_bonding_reaudit", outcome_label=pos.get("outcome", "Yes"),
                        cache_ttl_sec=interval, end_date=m.get("endDate", ""), stop_loss_price=stop_px,
                        prefer_backup=prefer_cc)
                    audited_at = float(audit.get("audited_at") or 0)
                    if audited_at and audited_at != float(pos.get("last_audit_ts", 0)):
                        raw_dec = str(audit.get("raw_decision") or audit.get("decision") or "").upper()
                        conf = int(audit.get("confidence") or 0)
                        is_hard_exit_skip = (raw_dec == "SKIP" and conf >= REAUDIT_EXIT_MIN_CONFIDENCE)
                        pos.update(
                            last_audit_ts=audited_at,
                            ai_audit=audit,
                            ai_skip_streak=streak + 1 if is_hard_exit_skip else 0,
                        )
                        save_live_state(state)
                        if is_hard_exit_skip:
                            log_live_event(
                                f"⚠️ Реаудит [{pos['title'][:42]}]: SKIP ({conf}%, страйк {pos['ai_skip_streak']}/2) — {audit.get('summary')}"
                            )
                        if pos["ai_skip_streak"] >= 2:
                            execute_sell_bond(secure_client, pos, f"ИИ дважды вернул SKIP: {audit.get('summary')}", state)
            except Exception as exc:
                reconciliation_ok = False
                log_live_event(f"⚠️ Ошибка сопровождения {pos.get('title', '')[:40]}: {type(exc).__name__}; остальные позиции проверяются.")
                daemon_alerts.problem(
                    f"reconcile_{pos.get('id', 'pos')}",
                    f"ошибка сопровождения/сверки {pos.get('title', '')[:35]}",
                    f"{type(exc).__name__}: {exc}",
                )
        save_live_state(state)

        # 2. Новые входы разрешены только после успешной сверки всех активных ордеров.
        if not reconciliation_ok or pos_unknown(state):
            log_live_event("⚠️ Новые входы на паузе: требуется сверка исполнения; сопровождение продолжается.")
            pending_titles = ", ".join(
                p.get("title", "")[:35]
                for p in open_positions
                if p.get("reconciliation_pending") or p.get("execution_unknown")
            )
            daemon_alerts.problem(
                "entries_paused",
                "новые покупки на паузе до сверки исполнения",
                pending_titles or "сбой сверки одной из позиций",
            )
            return
        open_count = len([p for p in state.get("positions", []) if p.get("status") == "OPEN"])
        available_slots = max_slots - open_count

        # Баланс читаем и сохраняем даже при 10/10: бот берёт available_cash_usd из state.
        try:
            clob_client = init_clob_client(cfg)
            cash_balance = get_live_cash_balance(clob_client, cfg)
            state["available_cash_usd"] = cash_balance
            save_live_state(state)
            cash_balance = spendable_after_pending(state, cash_balance)
        except Exception as exc:
            log_live_event(f"⚠️ Не удалось обновить баланс CLOB: {type(exc).__name__}; позиции уже проверены.")
            daemon_alerts.problem(
                "clob_balance",
                "не удалось обновить баланс CLOB",
                f"{type(exc).__name__}: {exc}",
            )
            return

        if available_slots <= 0:
            log_live_event(f"Слоты заполнены ({open_count}/{max_slots}). Поиск новых кандидатов пропущен.")
            return

        if cash_balance < 10.0:
            log_live_event(f"⚠️ Доступный баланс (${cash_balance:.2f}) недостаточен для открытия слота ($10+).")
            return

        if entry_budget_exhausted():
            log_live_event("Суточный лимит вызовов ИИ для новых входов исчерпан.")
            return

        try:
            markets = fetch_polymarket_markets()
        except Exception as exc:
            log_live_event(f"⚠️ Поиск новых рынков недоступен: {type(exc).__name__}; позиции уже проверены.")
            daemon_alerts.problem(
                "live_scan",
                "недоступны Polymarket CLOB/Gamma при поиске рынков",
                f"{type(exc).__name__}: {exc}",
            )
            return

        log_live_event("🔍 Сканирование рынков для Smart Bonding...")
        candidates = scan_smart_bonding(markets, deposit_usd=slot_size, min_apr=BOND_MIN_ENTRY_APR)
        blocked_tokens = {
            p.get("token_id")
            for p in state.get("positions", [])
            if p.get("token_id")
            and (p.get("status") == "OPEN" or now - float(p.get("closed_at", 0.0)) < REOPEN_COOLDOWN_SEC)
        }
        blocked_slugs = {
            p.get("slug")
            for p in state.get("positions", [])
            if p.get("slug")
            and (p.get("status") == "OPEN" or now - float(p.get("closed_at", 0.0)) < REOPEN_COOLDOWN_SEC)
        }

        markets_by_slug = {m.get("slug"): m for m in markets if m.get("slug")}
        active_open = [p for p in state.get("positions", []) if p.get("status") == "OPEN"]
        blocked_event_ids: Set[str] = set()
        long_open_count = 0
        for p in active_open:
            p_mkt = markets_by_slug.get(p.get("slug"))
            ev_id = p.get("event_id") or extract_event_id(p_mkt)
            if ev_id:
                blocked_event_ids.add(ev_id)
            p_days = _days_left_from_end_date(
                str(p.get("end_date") or (p_mkt or {}).get("endDate") or ""), now
            )
            if p_days is not None and p_days > LONG_HORIZON_DAYS:
                long_open_count += 1
        max_long_slots = max(1, int(math.floor(max_slots * MAX_LONG_HORIZON_SHARE)))

        fresh_audits = 0
        for cand in candidates:
            if available_slots <= 0 or cash_balance < 10.0:
                break

            tier_req = get_entry_tier_requirements(len(active_open), max_slots)

            slug = cand.get("slug")
            if slug in blocked_slugs:
                continue

            m_cand = markets_by_slug.get(slug)
            if not m_cand:
                continue

            cand_event_id = cand.get("event_id") or extract_event_id(m_cand)
            if cand_event_id and cand_event_id in blocked_event_ids:
                continue
            cand["event_id"] = cand_event_id

            cand_days = float(cand.get("days_left") or 14.0)
            if cand_days > tier_req["max_days"]:
                continue
            if cand_days > LONG_HORIZON_DAYS and long_open_count >= max_long_slots:
                continue

            lead_outcome = cand.get("lead_outcome", "Yes")
            token_id = extract_asset_id(m_cand, lead_outcome)
            if not token_id or token_id in blocked_tokens:
                continue
            cand["token_id"] = token_id

            # Лимит корреляции: не более MAX_SLOTS_PER_TOPIC позиций в одном тематическом кластере (экономим DIEM)
            if _topic_cluster_blocked(cand["question"], active_open):
                continue

            # Проверяем допустимость комиссии ДО чтения стакана и платного вызова Venice (экономим DIEM)
            fee_ok, fee_sched, _fee_reason = entry_fee_allowed(m_cand, token_id)
            if not fee_ok:
                continue
            cand["fee_schedule"] = fee_sched
            cand["market"] = m_cand

            # Предварительно проверим стакан и спред до платного вызова Venice (экономим DIEM)
            summary = _parse_book_summary(_safe_fetch_book(token_id))
            if not summary or summary["best_ask"] is None or summary["best_bid"] is None:
                continue
            best_ask = float(summary["best_ask"])
            best_bid = float(summary["best_bid"])
            if best_ask > MAX_ENTRY_PRICE or best_ask < MIN_ENTRY_PRICE:
                continue
            if (best_ask - best_bid) > MAX_ENTRY_SPREAD:
                continue
            if float(summary.get("near_bid_depth_usd") or 0.0) < MIN_ENTRY_BID_DEPTH_USD:
                continue

            # Пересчитываем чистую доходность и APR по реальному best_ask за вычетом комиссии
            est_gross_shares = slot_size / best_ask
            est_fee_usd = estimate_taker_fee_usd(est_gross_shares, best_ask, fee_sched)
            est_net_shares = max(0.0, est_gross_shares - (est_fee_usd / best_ask))
            net_ret_pct = ((est_net_shares - slot_size) / slot_size) * 100.0 if slot_size > 0 else 0.0
            if net_ret_pct < MIN_NET_RETURN_PCT:
                continue
            cand["annualized_apr"] = net_ret_pct * (365.0 / max(1.0, cand_days))
            cand["estimated_entry_fee_usd"] = round(est_fee_usd, 4)
            if cand["annualized_apr"] < tier_req["min_apr"]:
                continue

            tick_now = time.time()
            audit = call_venice_market_audit(
                question=cand["question"],
                description=m_cand.get("description", ""),
                price_yes=summary["best_ask"],
                strategy_type="polymarket_smart_bonding",
                outcome_label=lead_outcome,
                samples=ENTRY_AI_SAMPLES,
                cache_ttl_sec=ENTRY_AUDIT_CACHE_TTL_SEC,
                budget_reserve=REAUDIT_RESERVE_CALLS,
                end_date=cand.get("end_date", ""),
            )
            cand["ai_audit"] = audit
            if not _is_cached_audit(audit, tick_now):
                fresh_audits += 1
                used_m = audit.get("model_used") or ""
                m_suffix = f" [{used_m}]" if used_m else ""
                log_live_event(
                    f"🤖 ИИ-аудит [{cand['question'][:42]} / {lead_outcome}]{m_suffix}: "
                    f"{audit.get('decision')} ({audit.get('confidence', 0)}%) — {audit.get('summary')}"
                )

            audit_conf = int(audit.get("confidence") or 0)
            if audit.get("decision") == "APPROVE" and audit_conf < tier_req["min_confidence"]:
                log_live_event(
                    f"⏭ Кандидат {cand['question'][:42]} пропущен по лестнице "
                    f"[{tier_req['tier']}]: уверенность {audit_conf}% < {tier_req['min_confidence']}%."
                )
            elif audit.get("decision") == "APPROVE":
                actual_slot = min(slot_size, cash_balance)
                pos = execute_buy_bond(
                    secure_client, cand, slot_size=actual_slot, state=state, min_apr=tier_req["min_apr"]
                )
                if pos:
                    if pos.get("status") == "OPEN":
                        active_open.append(pos)
                        if cand_event_id:
                            blocked_event_ids.add(cand_event_id)
                        if cand_days > LONG_HORIZON_DAYS:
                            long_open_count += 1
                    save_live_state(state)
                    cash_balance = get_live_cash_balance(clob_client, cfg)
                    state["available_cash_usd"] = cash_balance
                    cash_balance = spendable_after_pending(state, cash_balance)
                    available_slots -= 1
                    blocked_tokens.add(token_id)
                    blocked_slugs.add(slug)
                    time.sleep(2)

            if pos_unknown(state):
                break
            if fresh_audits >= MAX_FRESH_AUDITS_PER_TICK:
                break

        save_live_state(state)
    finally:
        secure_client.close()


def run_fast_stop_guard(cfg: Dict[str, str]) -> None:
    """Быстрая проверка стаканов и Stop-Loss открытых позиций (каждые 20 с) без скана Gamma и без вызовов ИИ."""
    total_deposit = float(cfg.get("LIVE_TOTAL_DEPOSIT", str(DEFAULT_TOTAL_DEPOSIT)))
    state = load_live_state(deposit=total_deposit)
    open_positions = [p for p in state.get("positions", []) if p.get("status") == "OPEN"]
    if not open_positions:
        return

    now = time.time()
    secure_client: Optional[SecureClient] = None
    state_changed = False

    def _client() -> SecureClient:
        nonlocal secure_client
        if secure_client is None:
            secure_client = init_secure_client(cfg)
        return secure_client

    try:
        for pos in open_positions:
            try:
                stop_px = pos_stop_loss_price(pos)
                pending_exit = any(not x.get("complete") for x in pos.get("exit_orders", []))
                needs_sync = (
                    not pos.get("entry_terminal")
                    or pending_exit
                    or pos.get("reconciliation_pending")
                    or pos.get("execution_unknown")
                )
                if needs_sync:
                    state_changed = True
                    if not reconcile_position(_client(), state, pos):
                        book = _parse_book_summary(_safe_fetch_book(pos["token_id"]))
                        crossed = book and book["best_bid"] is not None and book["best_bid"] < stop_px
                        if (crossed or pos.get("execution_unknown")) and now - float(pos.get("last_sync_alert_at", 0)) >= 1800:
                            pos["last_sync_alert_at"] = now
                            save_live_state(state)
                            notify_live(
                                f"🚨 <b>[LIVE] Требуется проверка исполнения</b>\n"
                                f"{format_market_link(pos['title'], pos.get('slug', ''))}\n"
                                "Автоматическая повторная заявка заблокирована до сверки. "
                                + (f"Цена ниже стопа ${stop_px:.4f}!" if crossed else "Результат отправки ордера неизвестен.")
                            )
                        continue
                    if pos.get("status") != "OPEN":
                        continue
                    stop_px = pos_stop_loss_price(pos)
                    pending_exit = any(not x.get("complete") for x in pos.get("exit_orders", []))

                shares = float(pos.get("shares") or 0.0)
                entry_price = float(pos.get("entry_price") or 0.0)
                summary = _parse_book_summary(_safe_fetch_book(pos["token_id"]))
                if summary and summary["best_bid"] is not None:
                    bid = float(summary["best_bid"])
                    ask = float(summary["best_ask"]) if summary["best_ask"] is not None else bid
                    new_cur = round((bid + ask) / 2, 4)
                    new_upnl = round(shares * (new_cur - entry_price), 6)
                    prev_streak = pos.get("stop_breach_streak")
                    prev_breach_at = pos.get("stop_breach_at")
                    trigger_sl, sl_reason = should_trigger_stop_loss(pos, summary, stop_px)
                    if (
                        pos.get("current_price") != new_cur
                        or pos.get("unrealized_pnl_usd") != new_upnl
                        or pos.get("stop_breach_streak") != prev_streak
                        or pos.get("stop_breach_at") != prev_breach_at
                    ):
                        pos["current_price"] = new_cur
                        pos["unrealized_pnl_usd"] = new_upnl
                        state_changed = True
                    if trigger_sl:
                        finished = find_market_for_pos(pos, [])
                        if not finished or not market_trading_finished(finished):
                            execute_sell_bond(
                                _client(),
                                pos,
                                sl_reason,
                                state,
                            )
                            state_changed = True
                            continue
                elif "stop_breach_streak" in pos or "stop_breach_at" in pos:
                    pos.pop("stop_breach_streak", None)
                    pos.pop("stop_breach_at", None)
                    state_changed = True

                if pending_exit:
                    m = find_market_for_pos(pos, [])
                    if not m or not market_trading_finished(m):
                        tp_floor = pos.get("take_profit_min_price") if pos.get("exit_kind") == "take_profit" else None
                        execute_sell_bond(
                            _client(),
                            pos,
                            pos.get("close_reason") or "сопровождение незаполненного SELL",
                            state,
                            min_sell_price=tp_floor,
                        )
                        state_changed = True
            except Exception as exc:
                log_live_event(
                    f"⚠️ Ошибка быстрой проверки {pos.get('title', '')[:40]}: {type(exc).__name__}"
                )
        if state_changed:
            save_live_state(state)
    finally:
        if secure_client is not None:
            secure_client.close()


def print_status(cfg: Dict[str, str]) -> None:
    """Выводит сводку состояния портфеля в терминал."""
    state = load_live_state()
    try:
        client = init_clob_client(cfg)
        cash = get_live_cash_balance(client, cfg)
    except Exception:
        cash = float(state.get("available_cash_usd", 0.0))

    positions = state.get("positions", [])
    open_pos = [p for p in positions if p.get("status") == "OPEN"]

    total_unrealized = sum(float(p.get("unrealized_pnl_usd", 0.0)) for p in open_pos)
    total_realized = float(state.get("realized_pnl_usd", 0.0))

    print("\n" + "=" * 65)
    print(" 🔴 POLYMARKET LIVE TRADER — ТЕКУЩИЙ СТАТУС ПОРТФЕЛЯ")
    print("=" * 65)
    print(" Режим:                   LIVE (Реальный счёт, Deposit Wallet)")
    print(f" Доступный баланс USDC:   ${cash:,.2f}")
    print(f" Всего тиков:             {state.get('tick_count', 0)}")
    print(f" Открытых позиций:        {len(open_pos)} / {cfg.get('LIVE_MAX_SLOTS', DEFAULT_MAX_SLOTS)}")
    print(f" Нереализованный PnL:     {'+' if total_unrealized >= 0 else ''}${total_unrealized:.2f}")
    print(f" Реализованный PnL:       {'+' if total_realized >= 0 else ''}${total_realized:.2f}")
    print(f" Общий PnL:               {'+' if (total_realized + total_unrealized) >= 0 else ''}${total_realized + total_unrealized:.2f}")
    print("-" * 65)

    if open_pos:
        print(" ОТКРЫТЫЕ ПОЗИЦИИ:")
        for idx, p in enumerate(open_pos, 1):
            print(
                f"  {idx}. {p['title'][:40]:<40} | "
                f"Вход: ${p['entry_price']:.4f} | "
                f"Текущая: ${p.get('current_price', 0.0):.4f} | "
                f"PnL: {'+' if p.get('unrealized_pnl_usd', 0) >= 0 else ''}${p.get('unrealized_pnl_usd', 0):.2f}"
            )
    else:
        print(" Открытых позиций нет (ожидание свободных средств или сигналов)")

    print("=" * 65 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Live Bonding Trader")
    parser.add_argument("--once", action="store_true", help="Выполнить один тик и завершить работу")
    parser.add_argument("--status", action="store_true", help="Показать статус портфеля (read-only)")
    parser.add_argument("--interval", type=int, default=120, help="Интервал тика демона в секундах (по умолч. 120)")
    args = parser.parse_args()

    cfg = load_config()

    if args.status:
        print_status(cfg)
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    process_lock = open(STATE_DIR / "live_trader.lock", "a")
    try:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Другой экземпляр live-трейдера уже работает")

    if args.once:
        run_live_tick(cfg)
        return

    log_live_event("🚀 Запуск Live-демона Polymarket Smart Bonding...")
    if load_live_state().get("tick_count", 0) == 0:
        notify_live(
            f"🚀 <b>{_live_badge(cfg)} СЕРВИС ТОРГОВЛИ ЗАПУЩЕН!</b>\n\n"
            "🟢 Стратегия: <b>Smart Bonding (100% аллокация)</b>\n"
            f"🎯 Лимит слотов: <code>{cfg.get('LIVE_MAX_SLOTS', DEFAULT_MAX_SLOTS)} по ${cfg.get('LIVE_SLOT_SIZE', DEFAULT_SLOT_SIZE)}</code>\n"
            "🛡 ИИ-контроль: <b>Venice AI (consensus 2/2, fail-closed)</b>\n"
            "⏱ Интервал проверки: <code>120 сек</code>"
        )

    daemon_alerts.clear_after = max(daemon_alerts.clear_after, 2.5 * args.interval)
    running = True

    def _sig_handler(signum, frame):
        nonlocal running
        log_live_event(f"Получен сигнал завершения ({signum}), остановка...")
        running = False

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    while running:
        t0 = time.time()
        try:
            run_live_tick(cfg)
            log_live_event("Тик завершен; состояние сохранено.")
        except Exception as exc:
            log_live_event(f"💥 Ошибка в цикле тика: {exc}")
            import traceback

            log_live_event(traceback.format_exc())
            daemon_alerts.problem(
                "live_tick",
                "сбой рабочего тика live-трейдера",
                f"{type(exc).__name__}: {exc}",
                immediate=True,
            )
        daemon_alerts.sweep()

        elapsed = time.time() - t0
        sleep_time = max(5.0, args.interval - elapsed)
        waited = 0
        next_fast_guard = FAST_GUARD_INTERVAL_SEC
        while running and waited < int(sleep_time):
            time.sleep(1)
            waited += 1
            if not running:
                break
            if waited >= next_fast_guard and (int(sleep_time) - waited) >= 5:
                try:
                    run_fast_stop_guard(cfg)
                except Exception as exc:
                    log_live_event(f"⚠️ Ошибка в быстром цикле Stop-Loss: {type(exc).__name__}: {exc}")
                next_fast_guard += FAST_GUARD_INTERVAL_SEC


if __name__ == "__main__":
    main()
