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
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

from ai_analyzer import REAUDIT_RESERVE_CALLS, call_venice_market_audit, entry_budget_exhausted
from error_alerts import daemon_alerts
from polymarket_scanner import (
    estimate_taker_fee_usd,
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
ENTRY_AUDIT_CACHE_TTL_SEC = 21600.0  # 6 часов кэша для аудитов входа (экономия DIEM с веб-поиском)
REAUDIT_INTERVAL_SEC = 21600.0    # 6 часов между плановыми реаудитами
REAUDIT_RECHECK_SEC = 3600.0      # Перепроверка через час после первого SKIP
REOPEN_COOLDOWN_SEC = 12 * 3600.0 # Пауза перед повторным входом в закрытый рынок
SELL_REPLACE_AFTER_SEC = 60.0     # Незаполненный Stop-Loss SELL перевыставляем через минуту
SELL_AGGRESSIVE_TICKS = 1         # На сколько тиков ниже best_bid ставить SELL для выкупа

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
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": bids[0][0] if bids else None,
        "best_bid_size": bids[0][1] if bids else 0.0,
        "best_ask": asks[0][0] if asks else None,
        "best_ask_size": asks[0][1] if asks else 0.0,
        "tick_size": str(book.get("tick_size") or "0.01"),
        "min_order_size": float(book.get("min_order_size") or 5.0),
    }


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
            try:
                return read_execution(
                    client, order_id, pos["token_id"], side, exp_size,
                    market=market_id, fee_schedule=fee_sched,
                )
            except ValueError as err:
                if "fee_schedule" in str(err):
                    loaded = _resolve_pos_fee_schedule(pos)
                    if loaded is not None:
                        fee_sched = loaded
                        return read_execution(
                            client, order_id, pos["token_id"], side, exp_size,
                            market=market_id, fee_schedule=fee_sched,
                        )
                raise

        entry = _read_with_schedule(pos["order_id"], "BUY", requested, pos.get("condition_id") or "")
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


def execute_buy_bond(secure_client: SecureClient, candidate: Dict[str, Any], slot_size: float,
                     state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
    now = time.time()
    pos = dict(id=f"live_bond_{token_id[:12]}_{time.time_ns()}", type="poly_bond", status="OPEN",
               opened_at=now, title=f"{candidate['question']} [{candidate['lead_outcome']}]",
               question=candidate["question"], slug=candidate.get("slug", ""),
               outcome=candidate["lead_outcome"], token_id=token_id,
               fee_schedule=fee_sched,
               requested_shares=shares, requested_price=float(price), shares=0.0, allocated_usd=0.0,
               entry_price=float(price), current_price=float(price),
               stop_loss_price=round(max(BOND_STOP_LOSS_FLOOR, float(price) - BOND_STOP_LOSS_OFFSET), 4),
               realized_pnl_usd=0.0,
               unrealized_pnl_usd=0.0, expected_apr=candidate.get("annualized_apr", 0),
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
        pos.update(status="OPEN", execution_unknown=True)
        save_live_state(state)
        log_live_event(f"🚨 Результат BUY неизвестен: {type(exc).__name__}; повтор заблокирован.")
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


def execute_sell_bond(secure_client: SecureClient, pos: Dict[str, Any], reason: str,
                      state: Dict[str, Any]) -> bool:
    """Не освобождает слот до подтверждения SELL и не «зависает» на незаполненной заявке."""
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
                    f"Stop-Loss SELL не перевыставлен: {pos.get('title', '')[:35]}",
                    f"стакан bid={best_bid:.4f}, заявка ${old_price:.4f}",
                    immediate=True,
                )
                return False
            log_live_event(f"🔁 Stop-Loss SELL ${old_price:.4f} отменён.")

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

    # После отмены стакан мог сдвинуться: перечитываем bid и агрессивно ставим на 1 тик ниже.
    summary = _parse_book_summary(_safe_fetch_book(pos["token_id"]))
    if not summary or summary["best_bid"] is None or summary["best_bid"] <= 0:
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
    raw_price = Decimal(str(best_bid)) - tick * Decimal(SELL_AGGRESSIVE_TICKS)
    price = _round_price_to_tick(float(max(tick, raw_price)), summary["tick_size"])
    log_live_event(
        f"🔁 Перевыставляем SELL: ${float(price):.4f} "
        f"(bid ${best_bid:.4f}, {SELL_AGGRESSIVE_TICKS} тик ниже)."
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
            save_live_state(state)
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
    notify_live(f"🔴 <b>[LIVE] Заявка SELL принята</b>\n{format_market_link(pos['title'], pos.get('slug', ''))}\n"
                f"Причина: {esc(reason)}\nОстаток позиции: {pos['shares']:.2f} акций.")
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
                    save_live_state(state)
                    if bid < stop_px:
                        finished = find_market_for_pos(pos, [])
                        if not finished or not market_trading_finished(finished):
                            execute_sell_bond(secure_client, pos, f"лучший bid ${bid:.4f} ниже стопа ${stop_px:.4f} (-3.5¢)", state)
                            continue
                else:
                    log_live_event(f"⚠️ Нет bid для {pos['title'][:40]}; позиция не считается закрытой.")

                m = find_market_for_pos(pos, [])
                pending_exit = any(not x.get("complete") for x in pos.get("exit_orders", []))
                if pending_exit and (not m or not market_trading_finished(m)):
                    execute_sell_bond(
                        secure_client,
                        pos,
                        pos.get("close_reason") or "сопровождение незаполненного SELL",
                        state,
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
                streak = int(pos.get("ai_skip_streak", 0))
                interval = REAUDIT_INTERVAL_SEC if streak == 0 else REAUDIT_RECHECK_SEC
                if now - float(pos.get("last_audit_ts", 0)) >= interval:
                    audit = call_venice_market_audit(
                        question=pos.get("question") or pos["title"].split(" [")[0],
                        description=m.get("description", ""), price_yes=float(pos.get("current_price", entry_price)),
                        strategy_type="polymarket_smart_bonding", outcome_label=pos.get("outcome", "Yes"),
                        cache_ttl_sec=interval, end_date=m.get("endDate", ""))
                    audited_at = float(audit.get("audited_at") or 0)
                    if audited_at and audited_at != float(pos.get("last_audit_ts", 0)):
                        pos.update(last_audit_ts=audited_at, ai_audit=audit,
                                   ai_skip_streak=0 if audit.get("decision") == "APPROVE" else streak + 1)
                        save_live_state(state)
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
        candidates = scan_smart_bonding(markets, deposit_usd=slot_size)
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

        active_open = [p for p in state.get("positions", []) if p.get("status") == "OPEN"]
        fresh_audits = 0
        for cand in candidates:
            if available_slots <= 0 or cash_balance < 10.0:
                break

            slug = cand.get("slug")
            if slug in blocked_slugs:
                continue

            m_cand = next((m for m in markets if m.get("slug") == slug), None)
            if not m_cand:
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

            # Пересчитываем чистую доходность и APR по реальному best_ask за вычетом комиссии
            est_gross_shares = slot_size / best_ask
            est_fee_usd = estimate_taker_fee_usd(est_gross_shares, best_ask, fee_sched)
            est_net_shares = max(0.0, est_gross_shares - (est_fee_usd / best_ask))
            net_ret_pct = ((est_net_shares - slot_size) / slot_size) * 100.0 if slot_size > 0 else 0.0
            if net_ret_pct < MIN_NET_RETURN_PCT:
                continue
            cand["annualized_apr"] = net_ret_pct * (365.0 / max(1.0, float(cand.get("days_left", 14.0))))
            cand["estimated_entry_fee_usd"] = round(est_fee_usd, 4)

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

            if audit.get("decision") == "APPROVE":
                actual_slot = min(slot_size, cash_balance)
                pos = execute_buy_bond(secure_client, cand, slot_size=actual_slot, state=state)
                if pos:
                    if pos.get("status") == "OPEN":
                        active_open.append(pos)
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
        for _ in range(int(sleep_time)):
            if not running:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
