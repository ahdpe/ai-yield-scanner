"""Проверка исполнения ордера по данным биржи, без изменения портфеля."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional


_EPS = Decimal("0.000001")
_MIN_FEE_USD = Decimal("0.00001")
_MAX_ALLOWED_FEE_RATE = Decimal("0.25")
_TOKEN_SCALE = Decimal("1000000")
_TERMINAL = {"MATCHED", "FILLED", "CANCELED", "CANCELLED", "EXPIRED"}
_KNOWN = _TERMINAL | {"LIVE", "OPEN", "DELAYED", "PENDING", "UNMATCHED"}


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value: Any) -> str:
    value = getattr(value, "value", value)
    return str(value if value is not None else "")


def _number(value: Any) -> Decimal:
    try:
        result = Decimal(_text(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Биржа вернула некорректное число исполнения") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("Биржа вернула недопустимое число исполнения")
    return result


def _rows(response: Any) -> list:
    if isinstance(response, (list, tuple)):
        return list(response)
    rows = _field(response, "data")
    if isinstance(rows, (list, tuple)):
        next_cursor = _field(response, "next_cursor")
        if _field(response, "has_more", False) or next_cursor not in (None, "", "LTE="):
            raise ValueError("Список биржи содержит непрочитанные страницы")
        return list(rows)
    # Unified SDK возвращает Paginator, итерация которого дает Page, не Trade.
    if not isinstance(response, (dict, str, bytes)) and hasattr(response, "__iter__"):
        result = []
        has_more = False
        received_page = False
        for page in response:
            received_page = True
            if _field(page, "limit_reached", False):
                raise ValueError("Список биржи обрезан лимитом пагинации")
            items = _field(page, "items")
            if not isinstance(items, (list, tuple)):
                raise ValueError("Неизвестный формат страницы исполнений")
            result.extend(items)
            has_more = bool(_field(page, "has_more", False))
        if not received_page or has_more:
            raise ValueError("Не получен полный список биржи")
        return result
    raise ValueError("Биржа вернула неизвестный формат списка исполнений")


def calculate_taker_fee_usd(
    quantity: Decimal, price: Decimal, fee_schedule: Optional[Dict[str, Any]]
) -> Decimal:
    """Вычисляет тейкерскую комиссию Polymarket: C * rate * (p * (1 - p))^exponent."""
    if not isinstance(fee_schedule, dict) or fee_schedule.get("unknown"):
        raise ValueError("Отсутствует валидное расписание комиссий fee_schedule")
    rate = _number(fee_schedule.get("rate", 0))
    if rate <= 0 or rate > _MAX_ALLOWED_FEE_RATE:
        raise ValueError(f"Недопустимая ставка комиссии в fee_schedule: {rate}")
    exp_raw = fee_schedule.get("exponent", 1)
    exp_dec = _number(exp_raw)
    if exp_dec <= 0 or exp_dec > Decimal("4"):
        raise ValueError(f"Недопустимая степень комиссии в fee_schedule: {exp_dec}")
    pq = price * (Decimal(1) - price)
    if exp_dec == int(exp_dec):
        factor = pq ** int(exp_dec)
    else:
        factor = Decimal(str(float(pq) ** float(exp_dec)))
    raw = quantity * rate * factor
    if raw < _MIN_FEE_USD:
        return Decimal(0)
    return raw.quantize(_MIN_FEE_USD, rounding=ROUND_HALF_UP)


def read_conditional_balance(client: Any, token_id: str, *, require_both: bool = False) -> Decimal:
    """Читает фактический чистый остаток условного токена на кошельке (V1 + V2)."""
    if not token_id:
        raise ValueError("Не указан token_id для проверки остатка токена")
    total = Decimal(0)
    succeeded = 0
    last_err: Optional[Exception] = None
    for asset_type in ("CONDITIONAL", "CONDITIONAL-V2"):
        try:
            resp = client.get_balance_allowance(asset_type=asset_type, token_id=token_id)
            raw_bal = _field(resp, "balance", None if require_both else 0)
            total += _number(raw_bal) / _TOKEN_SCALE
            succeeded += 1
        except Exception as exc:
            last_err = exc
    if succeeded < (2 if require_both else 1):
        raise ValueError("Не удалось получить фактический остаток токена на кошельке") from last_err
    return total


def _sdk_received_null(error: Exception) -> bool:
    """Отличает JSON null от сетевой ошибки и несовместимой схемы SDK."""
    if (type(error).__module__, type(error).__name__) != ("polymarket.errors", "UnexpectedResponseError"):
        return False
    errors = getattr(error.__cause__, "errors", None)
    if not callable(errors):
        return False
    details = errors()
    return (isinstance(details, list) and len(details) == 1 and isinstance(details[0], dict)
            and details[0].get("type") == "model_type" and details[0].get("loc") == ()
            and "input" in details[0] and details[0]["input"] is None)


def _check_open_order_absence(client: Any, market: str, order_id: str, token_id: str) -> bool:
    """Проверяет оба полных списка и возвращает наличие других заявок того же токена."""
    other_token_order = False
    for list_orders in (client.list_open_orders, client.list_wallet_open_orders):
        for order in _rows(list_orders(market=market)):
            found_id = _text(_field(order, "id"))
            asset = _text(_field(order, "asset_id"))
            if not found_id or not asset:
                raise ValueError("Не удалось сверить идентификатор открытого ордера")
            if found_id == order_id:
                raise ValueError("Заявка всё ещё числится открытой по данным биржи")
            other_token_order |= asset == token_id
    return other_token_order


def read_execution(client: Any, order_id: str, token_id: str, side: str,
                   expected_size: float, *, market: str = "",
                   fee_schedule: Optional[Dict[str, Any]] = None,
                   probe_missing_partial: bool = False) -> Dict[str, Any]:
    """Возвращает подтвержденное исполнение; при сомнениях бросает исключение.

    complete означает, что заявка терминальна и все исполненные акции имеют
    подтвержденные сделки. У отмененной заявки это может быть лишь часть объема.
    UNMATCHED не считается терминальным статусом: он требует дальнейшей сверки.
    """
    expected = _number(expected_size)
    side = side.upper()
    if not order_id or not token_id or side not in {"BUY", "SELL"} or expected <= 0:
        raise ValueError("Недостаточно данных для сверки ордера")

    order_error = None
    order_is_null = False
    try:
        order = client.get_order(order_id=order_id)
        if order is None:
            order_is_null = True
            order_error = ValueError("Биржа вернула пустую запись ордера")
    except Exception as exc:
        order_error = exc
        order_is_null = _sdk_received_null(exc)
    if order_error is None:
        actual_id = _field(order, "id", _field(order, "order_id"))
        if (_text(actual_id) != order_id
                or _text(_field(order, "asset_id")) != token_id
                or _text(_field(order, "side")).upper() != side):
            raise ValueError("Ответ биржи не соответствует запрошенному ордеру")
        original = _number(_field(order, "original_size"))
        matched = _number(_field(order, "size_matched"))
        if original <= 0 or abs(original - expected) > _EPS or matched > original + _EPS:
            raise ValueError("Объем ордера не соответствует ожидаемому")
        status = _text(_field(order, "status")).upper()
        if status not in _KNOWN:
            raise ValueError("Неизвестный статус ордера; требуется повторная сверка")
        order_market = _text(_field(order, "condition_id") or _field(order, "market"))
        if market and order_market and market != order_market:
            raise ValueError("Ордер относится к другому рынку")
        market = order_market or market

    # Верхний asset_id сделки может обозначать противоположный исход.
    # Фильтруем по общему рынку, а для старых ордеров без рынка читаем сделки аккаунта.
    trades = client.list_account_trades(**({"market": market} if market else {}))
    confirmed = Decimal(0)
    cash = Decimal(0)
    has_fee = False
    total_fee_usd = Decimal(0)
    total_fee_shares = Decimal(0)
    seen = set()
    confirmed_market = market
    unsettled_trades = False
    taker_only = True
    if isinstance(fee_schedule, dict):
        taker_only = bool(fee_schedule.get("taker_only", fee_schedule.get("takerOnly", True)))
    for trade in _rows(trades):
        same_taker_id = _text(_field(trade, "taker_order_id")) == order_id
        taker_match = same_taker_id and _text(_field(trade, "trader_side")).upper() == "TAKER"
        if same_taker_id and not taker_match:
            unsettled_trades = True
        maker_matches = [m for m in (_field(trade, "maker_orders", []) or [])
                         if _text(_field(m, "order_id")) == order_id]
        if not taker_match and not maker_matches:
            continue
        if _text(_field(trade, "status")).upper() != "CONFIRMED":
            unsettled_trades = True
            continue
        trade_market = _text(_field(trade, "condition_id") or _field(trade, "market"))
        if confirmed_market and trade_market and trade_market != confirmed_market:
            raise ValueError("Исполнения ордера относятся к разным рынкам")
        confirmed_market = confirmed_market or trade_market
        trade_id = _text(_field(trade, "id"))
        if not trade_id:
            raise ValueError("У подтвержденного исполнения отсутствует идентификатор")
        if trade_id in seen:
            continue
        seen.add(trade_id)
        if taker_match and maker_matches:
            raise ValueError("Неоднозначная сторона подтвержденного исполнения")
        legs = [trade] if taker_match else maker_matches
        for leg in legs:
            asset = _field(leg, "asset_id")
            if asset is not None and _text(asset) != token_id:
                raise ValueError("Исполнение относится к другому активу")
            leg_side = _field(leg, "side")
            if (not taker_match and _text(leg_side).upper() != side) or (
                    taker_match and leg_side is not None and _text(leg_side).upper() != side):
                raise ValueError("Сторона исполнения не соответствует ордеру")
            if order_error is not None and (_text(asset) != token_id or _text(leg_side).upper() != side):
                raise ValueError("Без снимка ордера требуются актив и сторона подтвержденной сделки")
            quantity = _number(_field(leg, "size" if taker_match else "matched_amount"))
            price = _number(_field(leg, "price"))
            if price <= 0 or price >= 1 or quantity <= 0:
                raise ValueError("Некорректная цена или объем подтвержденной сделки")
            leg_fee_bps = _number(_field(leg, "fee_rate_bps", 0) or 0)
            if leg_fee_bps > 0 and (taker_match or not taker_only):
                has_fee = True
                leg_fee_usd = calculate_taker_fee_usd(quantity, price, fee_schedule)
                total_fee_usd += leg_fee_usd
                if side == "BUY" and leg_fee_usd > 0:
                    total_fee_shares += (leg_fee_usd / price).quantize(_EPS, rounding=ROUND_HALF_UP)
            confirmed += quantity
            cash += quantity * price
    if order_error is not None:
        if abs(confirmed - expected) <= _EPS:
            status = "CONFIRMED"
        elif (probe_missing_partial and order_is_null and side in ("BUY", "SELL") and market
              and 0 < confirmed < expected - _EPS and not unsettled_trades):
            other_token_order = _check_open_order_absence(client, market, order_id, token_id)
            return {
                "status": "MISSING_PARTIAL",
                "matched_shares": float(confirmed),
                "confirmed_shares": float(confirmed),
                "confirmed_cash": float(cash),
                "has_fee": has_fee,
                "estimated_fee_usd": float(total_fee_usd),
                "estimated_fee_shares": float(total_fee_shares),
                "condition_id": confirmed_market,
                "terminal": False, "settled": False, "complete": False,
                "missing_partial": True,
                "other_token_order": other_token_order,
            }
        else:
            raise ValueError("Без снимка ордера не подтвержден весь запрошенный объем") from order_error
        matched = confirmed
    if confirmed > matched + _EPS:
        raise ValueError("Список сделок опережает снимок ордера; нужна повторная сверка")
    terminal = status in _TERMINAL or status == "CONFIRMED"
    return {
        "status": status,
        "matched_shares": float(matched),
        "confirmed_shares": float(confirmed),
        "confirmed_cash": float(cash),
        "has_fee": has_fee,
        "estimated_fee_usd": float(total_fee_usd),
        "estimated_fee_shares": float(total_fee_shares),
        "terminal": terminal,
        "settled": abs(confirmed - matched) <= _EPS,
        "complete": terminal and abs(confirmed - matched) <= _EPS,
        "condition_id": confirmed_market,
    }
