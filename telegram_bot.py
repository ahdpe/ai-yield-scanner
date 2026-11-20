"""Интерактивный Telegram-бот для управления и мониторинга Live-портфеля Polymarket."""

import json
import os
import sys
import time
import traceback
import urllib.parse
from typing import Any, Dict, List, Tuple

from state_io import STATE_DIR, esc


def _load_live_env() -> Dict[str, str]:
    """Читает state/live_config.env и пробрасывает параметры в os.environ до импорта ai_analyzer."""
    cfg: Dict[str, str] = {}
    env_path = STATE_DIR / "live_config.env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                key, val = k.strip(), v.strip().strip("'\"")
                cfg[key] = val
                if val:
                    os.environ.setdefault(key, val)
    return cfg


_LIVE_CFG = _load_live_env()

from ai_analyzer import get_ai_budget, get_ai_health, get_backup_ai_budget, get_venice_models, ping_venice_ai  # noqa: E402
from error_alerts import bot_alerts  # noqa: E402
from telegram_notifier import load_tg_config, tg_api_call, tg_send_message  # noqa: E402


STATE_FILE = STATE_DIR / "live_portfolio_state.json"
LOG_FILE = STATE_DIR / "live_trader.log"
AI_CACHE_FILE = STATE_DIR / "ai_audits_cache.json"
TG_SAFE_HTML_LEN = 3800


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


def _live_config() -> Dict[str, str]:
    """Возвращает актуальные настройки из live_config.env с приоритетом os.environ."""
    cfg = dict(_LIVE_CFG)
    cfg.update(_load_live_env())
    for key in (
        "LIVE_TOTAL_DEPOSIT",
        "LIVE_SLOT_SIZE",
        "LIVE_MAX_SLOTS",
        "POLY_FUNDER_ADDRESS",
        "POLY_SIGNATURE_TYPE",
        "VENICE_MODEL",
        "VENICE_MODELS",
    ):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def _models_label() -> Tuple[str, str]:
    """Возвращает (строка моделей, описание режима консенсуса)."""
    models = get_venice_models()
    label = " + ".join(models)
    mode = "кросс-консенсус 2/2" if len(models) > 1 else "консенсус 2/2"
    return label, mode


def live_portfolio_totals(state: Dict[str, Any], cfg: Dict[str, str]) -> Dict[str, Any]:
    """Считает сводные показатели реального портфеля, резервы и средневзвешенный APR."""
    positions = state.get("positions") or []
    open_pos = [p for p in positions if p.get("status") == "OPEN"]
    closed_pos = [p for p in positions if p.get("status") != "OPEN"]

    deposit = float(cfg.get("LIVE_TOTAL_DEPOSIT") or state.get("initial_deposit_usd") or 500.0)
    slot_size = float(cfg.get("LIVE_SLOT_SIZE") or 100.0)
    max_slots = int(cfg.get("LIVE_MAX_SLOTS") or 5)

    cash = float(state.get("available_cash_usd") or 0.0)
    reserved = sum(
        max(0.0, float(p.get("requested_shares") or 0.0) - float(p.get("confirmed_bought_shares") or 0.0))
        * float(p.get("requested_price") or p.get("entry_price") or 0.0)
        for p in open_pos
        if not p.get("entry_terminal")
    )
    spendable = max(0.0, cash - reserved)

    open_cost = sum(float(p.get("allocated_usd") or 0.0) for p in open_pos)
    open_value = sum(
        float(p.get("shares") or 0.0) * float(p.get("current_price") or p.get("entry_price") or 0.0)
        for p in open_pos
    )
    unrealized = sum(float(p.get("unrealized_pnl_usd") or 0.0) for p in open_pos)
    realized = float(state.get("realized_pnl_usd") or 0.0)
    net_pnl = realized + unrealized
    equity = cash + open_value

    weighted_apr = (
        sum(float(p.get("expected_apr") or 0.0) * float(p.get("allocated_usd") or 0.0) for p in open_pos) / open_cost
        if open_cost > 0
        else 0.0
    )
    daily_est = sum(
        float(p.get("allocated_usd") or 0.0) * (float(p.get("expected_apr") or 0.0) / 100.0) / 365.0
        for p in open_pos
    )
    needs_reconcile = any((p.get("execution_unknown") or p.get("reconciliation_pending")) for p in open_pos)

    return {
        "deposit": deposit,
        "slot_size": slot_size,
        "max_slots": max_slots,
        "cash": cash,
        "reserved": reserved,
        "spendable": spendable,
        "open_pos": open_pos,
        "closed_pos": closed_pos,
        "open_cost": open_cost,
        "open_value": open_value,
        "unrealized": unrealized,
        "realized": realized,
        "net_pnl": net_pnl,
        "equity": equity,
        "weighted_apr": weighted_apr,
        "daily_est": daily_est,
        "needs_reconcile": needs_reconcile,
    }


def make_main_keyboard() -> dict:
    """Формирует универсальную инлайн-клавиатуру бота с кнопкой инструкции."""
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Портфель", "callback_data": "menu_portfolio"},
                {"text": "🧠 Решения ИИ", "callback_data": "menu_ai"},
            ],
            [
                {"text": "🛡 Аварийный режим", "callback_data": "menu_emergency"},
                {"text": "⚙️ Настройки", "callback_data": "menu_settings"},
            ],
            [
                {"text": "📜 Логи событий", "callback_data": "menu_logs"},
                {"text": "⚡️ Пинг ИИ", "callback_data": "action_ping_ai"},
            ],
            [
                {"text": "📖 Инструкция", "url": "https://github.com/ahdpe/ai-yield-scanner/blob/main/MANUAL.md"},
                {"text": "🔄 Обновить", "callback_data": "menu_portfolio"},
            ],
        ]
    }


def _append_bounded(lines: List[str], block: str, max_len: int = TG_SAFE_HTML_LEN) -> bool:
    """Добавляет блок в список строк, если итоговая длина сообщения не превысит max_len."""
    current_len = sum(len(x) + 1 for x in lines)
    if current_len + len(block) + 1 > max_len:
        return False
    lines.append(block)
    return True


def format_portfolio_msg() -> str:
    """Формирует экран сводки реального Live-портфеля Polymarket."""
    if not STATE_FILE.exists():
        return "⚠️ <b>Live-портфель еще не инициализирован.</b>"

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"⚠️ Ошибка чтения live-портфеля: {esc(exc)}"

    cfg = _live_config()
    health = get_ai_health()
    is_em = bool(health.get("emergency_mode", False))
    t = live_portfolio_totals(state, cfg)

    started_at = float(state.get("started_at") or time.time())
    elapsed_hours = max(0.01, (time.time() - started_at) / 3600.0)
    last_tick = float(state.get("last_tick_at") or 0.0)
    last_tick_str = time.strftime("%H:%M:%S UTC", time.gmtime(last_tick)) if last_tick else "—"

    net_pnl = t["net_pnl"]
    pnl_sign = "+" if net_pnl >= 0 else ""
    pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"

    msg: List[str] = ["🔴 <b>СВОДКА LIVE-ПОРТФЕЛЯ (Polymarket Smart Bonding)</b>"]
    if is_em:
        msg.append(
            "🚨 <b>ВНИМАНИЕ: АВАРИЙНЫЙ РЕЖИМ ИИ!</b>\n"
            "<i>Связь с Venice AI временно отсутствует. Новые входы заблокированы (Fail-Closed).</i>\n"
        )
    if t["needs_reconcile"]:
        msg.append(
            "⏸ <b>Новые входы на паузе:</b> <i>ожидается сверка исполнения по открытой заявке с биржей.</i>\n"
        )

    cash_line = (
        f"💳 <b>Кэш CLOB:</b> ${t['cash']:,.2f} pUSD "
        f"(свободно: <b>${t['spendable']:,.2f}</b> | резерв: ${t['reserved']:,.2f})"
        if t["reserved"] >= 0.01
        else f"💳 <b>Свободный кэш CLOB:</b> <b>${t['cash']:,.2f} pUSD</b>"
    )

    msg.extend([
        f"⏱ <i>В работе: {elapsed_hours:.1f} ч | Тиков: {state.get('tick_count', 0)} (посл. {last_tick_str})</i>\n",
        f"💼 <b>Лимит стратегии:</b> ${t['deposit']:,.0f} USDC ({t['max_slots']} слотов × ${t['slot_size']:,.0f})",
        f"💵 <b>Оценка счёта (Equity):</b> <b>${t['equity']:,.2f} pUSD</b>",
        f"{pnl_emoji} <b>Чистый PnL:</b> <b>{pnl_sign}${net_pnl:,.2f} USDC</b>",
        f"   • Переоценка открытых: <code>${t['unrealized']:+.2f}</code> | Зафиксировано: <code>${t['realized']:+.2f}</code>",
        f"📦 <b>В позициях (подтв.):</b> ${t['open_cost']:,.2f} → сейчас ${t['open_value']:,.2f}",
        f"🔥 <b>APR активных слотов:</b> <b>{t['weighted_apr']:.1f}% годовых</b> (~${t['daily_est']:.2f}/день)",
        f"{cash_line}\n",
        f"<b>Открытые позиции ({len(t['open_pos'])} / {t['max_slots']}):</b>",
    ])

    if not t["open_pos"]:
        msg.append("   <i>Открытых позиций пока нет.</i>")
    else:
        for i, p in enumerate(t["open_pos"], 1):
            shares = float(p.get("shares") or 0.0)
            req_shares = float(p.get("requested_shares") or 0.0)
            alloc = float(p.get("allocated_usd") or 0.0)
            entry = float(p.get("entry_price") or 0.0)
            cur = float(p.get("current_price") or entry)
            apr = float(p.get("expected_apr") or 0.0)
            upnl = float(p.get("unrealized_pnl_usd") or 0.0)
            ord_status = str(p.get("order_status") or "—")
            link = format_market_link(p.get("title", "Без названия"), p.get("slug", ""))

            lines_pos = [f"{i}. 🎯 <b>{link}</b>"]
            if req_shares > shares + 0.01 and not p.get("entry_terminal"):
                lines_pos.append(
                    f"   ⏳ Куплено: <code>{shares:.2f}</code> из <code>{req_shares:.2f}</code> акций "
                    f"(<code>${alloc:.2f}</code>, ордер <code>{esc(ord_status)}</code>)"
                )
            else:
                lines_pos.append(
                    f"   Акций: <code>{shares:.2f}</code> (<code>${alloc:.2f}</code>) | "
                    f"Статус: <code>{esc(ord_status)}</code>"
                )
            stop_px = float(p.get("stop_loss_price") or (round(max(0.85, entry - 0.035), 4) if entry > 0 else 0.95))
            entry_fee = float(p.get("entry_fee_usd") or 0.0)
            fee_txt = f" | Ком.: <code>${entry_fee:.2f}</code>" if entry_fee > 0 else ""
            lines_pos.append(
                f"   Вход: <code>{entry:.4f}</code> → сейчас <code>{cur:.4f}</code> (SL <code>{stop_px:.4f}</code>) | "
                f"APR: <code>{apr:.1f}%</code> | PnL: <b>{upnl:+.2f}</b>{fee_txt}"
            )
            if p.get("execution_unknown") or p.get("reconciliation_pending"):
                lines_pos.append("   ⚠️ <i>Требуется сверка исполнения с биржей</i>")
            _append_bounded(msg, "\n".join(lines_pos))

    if t["closed_pos"]:
        recent_closed = list(reversed(t["closed_pos"]))
        _append_bounded(msg, f"\n<b>Последние закрытые / разрешённые ({len(recent_closed)}):</b>")
        shown = 0
        for idx, p in enumerate(recent_closed, 1):
            rpnl = float(p.get("realized_pnl_usd") or 0.0)
            st = str(p.get("status") or "CLOSED")
            reason = str(p.get("close_reason") or "").strip()
            link = format_market_link(p.get("title", "Без названия"), p.get("slug", ""))
            reason_txt = f"\n   <i>{esc(reason[:80])}</i>" if reason else ""
            block = (
                f"{idx}. ⏹ {link} (<code>{esc(st)}</code>)\n"
                f"   Итог слота: <b>{rpnl:+.2f} USDC</b>{reason_txt}"
            )
            if not _append_bounded(msg, block, max_len=TG_SAFE_HTML_LEN - 80):
                left = len(recent_closed) - shown
                msg.append(f"<i>… и ещё {left} закрытых сделок</i>")
                break
            shown += 1

    return "\n".join(msg)


def format_emergency_msg() -> str:
    """Формирует экран статуса аварийного режима и протокола безопасности Live-бота."""
    health = get_ai_health()
    is_em = bool(health.get("emergency_mode", False))
    status = str(health.get("status", "HEALTHY"))

    needs_reconcile = False
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            needs_reconcile = any(
                (p.get("execution_unknown") or p.get("reconciliation_pending"))
                for p in st.get("positions", [])
                if p.get("status") == "OPEN"
            )
        except Exception:
            pass

    if is_em:
        status_line = "🔴 <b>АКТИВЕН (СВЯЗЬ С ИИ ПОТЕРЯНА)</b>"
    elif status == "DEGRADED":
        status_line = "🟡 <b>ДЕГРАДАЦИЯ (Были задержки ответа)</b>"
    else:
        status_line = "🟢 <b>ОНЛАЙН (Штатная работа, всё защищено)</b>"

    reconcile_line = (
        "🟡 <b>НА ПАУЗЕ (требуется сверка открытой заявки)</b>"
        if needs_reconcile
        else "🟢 <b>ВСЕ ИСПОЛНЕНИЯ ПОДТВЕРЖДЕНЫ</b>"
    )

    last_succ = time.strftime("%H:%M:%S", time.gmtime(float(health.get("last_success_ts") or time.time())))
    latency = float(health.get("last_latency_sec") or 0.0)
    fails = int(health.get("consecutive_failures") or 0)
    last_err = str(health.get("last_error") or "") or "Ошибок нет"
    model_str, consensus_mode = _models_label()

    msg = [
        "🛡 <b>ПРОТОКОЛ БЕЗОПАСНОСТИ И АВАРИЙНЫЙ РЕЖИМ (LIVE)</b>\n",
        f"Статус ИИ-сторожа: {status_line}",
        f"Статус сверки ордеров: {reconcile_line}",
        f"🤖 <b>Модели ИИ:</b> <code>{esc(model_str)}</code> ({consensus_mode})",
        f"⏱ <b>Задержка последнего ответа:</b> <code>{latency:.2f} сек</code>",
        f"🕒 <b>Последний успешный опрос:</b> <code>{last_succ} UTC</code>",
        f"⚠️ <b>Ошибок подряд:</b> <code>{fails}</code>",
        f"📋 <b>Последнее системное событие:</b> <code>{esc(last_err[:90])}</code>\n",
        "<b>Как Live-система защищает реальный капитал:</b>",
        "1. 🔒 <b>Fail-Closed и резерв:</b> Если Venice AI недоступен или упёрся в лимит, бот автоматически переключается на резервный <b>Command Code</b> (с отдельным суточным лимитом); если недоступны оба провайдера — покупки <b>полностью заблокированы</b>.",
        "2. 🧾 <b>Сверка исполнений и комиссий:</b> Позиция учитывается только по подтверждённым сделкам и фактическому остатку токенов на кошельке (с учётом удержанной комиссии); при сбое сверки новые входы встают на паузу.",
        "3. 🎯 <b>Относительный Stop-Loss (каждые 20 с):</b> Индивидуальный уровень <b>-3.5¢</b> от цены входа (плюс лимит корреляции ≤ 2 слотов на тему); стаканы открытых позиций проверяются каждые 20 с, при пробое bid ниже SL бот продаёт подтверждённый остаток.",
        "4. 🧠 <b>Повторный аудит ИИ (без холостых выходов):</b> Открытые позиции перепроверяются каждые 6 ч со специальным HOLD-промптом; досрочная продажа запускается только после двух подтверждённых <code>SKIP (≥75%)</code> подряд по новым негативным фактам.",
        "5. 🔐 <b>Защита от дублей:</b> Идемпотентная запись намерения до отправки ордера + блокировка второго процесса.",
        "6. 📲 <b>Push-оповещения:</b> Мгновенные алерты по всем ордерам, отменам и сбоям в Telegram.",
    ]
    return "\n".join(msg)


def format_ai_verdicts_msg() -> str:
    """Показывает 8 самых свежих вердиктов Venice AI, отсортированных по времени."""
    if not AI_CACHE_FILE.exists():
        return "🧠 <b>Журнал аудитов ИИ пока пуст.</b>"

    try:
        cache = json.loads(AI_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"⚠️ Ошибка чтения кэша ИИ: {esc(exc)}"

    if not cache:
        return "🧠 <b>Журнал аудитов ИИ пока пуст.</b>"

    sorted_items = sorted(
        cache.items(),
        key=lambda kv: float((kv[1] or {}).get("ts") or (kv[1] or {}).get("result", {}).get("audited_at") or 0.0),
        reverse=True,
    )[:8]

    msg: List[str] = ["🧠 <b>ПОСЛЕДНИЕ РЕШЕНИЯ VENICE AI</b>\n"]
    for key, data in sorted_items:
        parts = key.split("::")
        strat = parts[0] if parts else "unknown"
        outcome = parts[1] if len(parts) >= 3 else ""
        q = parts[-1][:55]
        res = (data or {}).get("result", {})
        ts = float((data or {}).get("ts") or res.get("audited_at") or 0.0)
        ts_str = time.strftime("%d.%m %H:%M UTC", time.gmtime(ts)) if ts else ""
        dec = str(res.get("decision", "UNKNOWN"))
        dec_icon = "✅" if dec == "APPROVE" else "❌"
        conf = res.get("confidence", 0)
        summary = str(res.get("summary", ""))
        out_tag = f" [{esc(outcome)}]" if outcome else ""
        used_m = str(res.get("model_used") or "")
        m_tag = f" | <code>{esc(used_m)}</code>" if used_m else ""

        block = (
            f"{dec_icon} <b>{esc(dec)} ({conf}%)</b> | <i>{esc(ts_str)}</i>{m_tag}\n"
            f"<b>{esc(q)}</b>{out_tag}\n"
            f"<i>\"{esc(summary[:180])}\"</i>\n"
        )
        if not _append_bounded(msg, block):
            break

    return "\n".join(msg)


def format_logs_msg() -> str:
    """Возвращает последние события из live_trader.log в пределах лимита Telegram."""
    if not LOG_FILE.exists():
        return "📜 <b>Лог Live-событий пока пуст.</b>"

    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    recent: List[str] = []
    size = 0
    for line in reversed(lines[-15:]):
        clean = esc(line if len(line) <= 300 else line[:297] + "...")
        if size + len(clean) + 1 > 3700:
            break
        recent.insert(0, clean)
        size += len(clean) + 1
    if not recent:
        return "📜 <b>Лог Live-событий пока пуст.</b>"
    return f"📜 <b>ПОСЛЕДНИЕ {len(recent)} LIVE-СОБЫТИЙ:</b>\n\n<code>" + "\n".join(recent) + "</code>"


def format_settings_msg() -> str:
    """Формирует экран настроек Live-бота и расходов Venice AI."""
    tg_cfg = load_tg_config()
    cfg = _live_config()
    alerts = "Включены ✅" if tg_cfg.get("alerts_enabled", True) else "Выключены ❌"
    model_str, consensus_mode = _models_label()
    budget = get_ai_budget()
    backup = get_backup_ai_budget()
    backup_line = (
        f"🛟 <b>Резерв (Command Code):</b> <code>{esc(', '.join(backup['models']))}</code> | "
        f"вызовов сегодня: {backup['calls']} из {backup['limit']} (резерв {backup['reserve']})\n"
        if backup.get("enabled")
        else ""
    )

    deposit = float(cfg.get("LIVE_TOTAL_DEPOSIT") or 500.0)
    slot_sz = float(cfg.get("LIVE_SLOT_SIZE") or 100.0)
    max_slots = int(cfg.get("LIVE_MAX_SLOTS") or 5)
    sig_type = str(cfg.get("POLY_SIGNATURE_TYPE") or "3")
    funder = str(cfg.get("POLY_FUNDER_ADDRESS") or "").strip()
    wallet_short = f"{funder[:6]}…{funder[-4:]}" if len(funder) >= 12 else (funder or "не задан")

    cash_str = "н/д"
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if not cfg.get("LIVE_TOTAL_DEPOSIT"):
                deposit = float(st.get("initial_deposit_usd") or deposit)
            cash_str = f"${float(st.get('available_cash_usd') or 0.0):,.2f} pUSD"
        except Exception:
            pass

    wallet_label = "Deposit Wallet" if sig_type == "3" else f"sig_type={sig_type}"
    return (
        "⚙️ <b>НАСТРОЙКИ LIVE-СИСТЕМЫ</b>\n\n"
        f"🔴 <b>Режим работы:</b> Реальный счёт (<code>{esc(wallet_label)}</code>, кошелёк <code>{esc(wallet_short)}</code>)\n"
        f"💳 <b>Кэш на CLOB:</b> <code>{esc(cash_str)}</code>\n"
        f"💼 <b>Лимит портфеля:</b> ${deposit:,.0f} USDC (до {max_slots} слотов по ${slot_sz:,.0f})\n"
        "🎯 <b>Стратегия:</b> Smart Bonding (бонды 94.5¢–98.5¢, ≤ 45 дн., ≤ 2 слота на тему, учёт комиссий до <code>rate ≤ 0.06</code>, без спорта и крипто-таргетов)\n"
        "⏱ <b>Цикл проверки:</b> стопы/стаканы каждые 20 с | скан рынков каждые 120 с | Stop-Loss: -3.5¢ | Реаудит: 6 ч\n"
        f"🤖 <b>Модели Venice AI:</b> <code>{esc(model_str)}</code> ({consensus_mode} при входе, веб-поиск <code>{esc(os.environ.get('VENICE_WEB_SEARCH', 'on'))}</code>)\n"
        f"📊 <b>Вызовов ИИ сегодня:</b> {budget['calls']} из {budget['limit']} (~0.025 DIEM/вызов с веб-поиском; "
        f"{budget['reserve']} в резерве под реаудиты)\n"
        f"{backup_line}"
        f"🔔 <b>Push-алерты:</b> {alerts}\n"
        "🚨 <b>Fail-Closed и сверка исполнений:</b> Активны\n\n"
        "📖 Подробная справка доступна по кнопке <b>«Инструкция»</b> ниже."
    )


def format_manual_msg() -> str:
    return (
        "📖 <b>ПОЛНОЕ РУКОВОДСТВО ПОЛЬЗОВАТЕЛЯ И ТЕХНИЧЕСКАЯ СПРАВКА</b>\n\n"
        "Вся документация проекта доступна в репозитории GitHub:\n"
        "🔗 <a href='https://github.com/ahdpe/ai-yield-scanner/blob/main/MANUAL.md'><b>Открыть MANUAL.md</b></a>\n\n"
        "<b>В Live-контуре активно:</b>\n"
        "• Стратегия Smart Bonding (покупка предрезолюционных бондов 94.5¢–98.5¢, срок ≤ 45 дн.)\n"
        "• Консенсус-аудит Venice AI (2/2 APPROVE с уверенностью ≥70%, живой веб-поиск)\n"
        "• Подтверждённый учёт частичных исполнений, резерва под открытые заявки, лимит ≤ 2 слота на тему и Stop-Loss -3.5¢ от входа"
    )


class SimpleTelegramBot:
    def __init__(self, token: str, admin_id: str):
        self.token = token
        self.admin_id = str(admin_id)
        self.last_update_id = 0

    def api_call(self, method: str, payload: dict) -> dict:
        return tg_api_call(self.token, method, payload)

    def send_menu(self, chat_id: str, text: str):
        tg_send_message(self.token, chat_id, text, reply_markup=make_main_keyboard())

    def answer_callback(self, cb_id: str, text: str = ""):
        self.api_call("answerCallbackQuery", {
            "callback_query_id": cb_id,
            "text": text,
            "show_alert": False,
        })

    def run(self):
        cfg = _live_config()
        deposit = float(cfg.get("LIVE_TOTAL_DEPOSIT") or 500.0)
        slot_sz = float(cfg.get("LIVE_SLOT_SIZE") or 100.0)
        max_slots = int(cfg.get("LIVE_MAX_SLOTS") or 5)
        model_str, consensus_mode = _models_label()

        print(f"Запуск Live Telegram-бота (Admin ID: {self.admin_id})...", flush=True)
        self.send_menu(
            self.admin_id,
            "🔴 <b>Live ИИ-Бот запущен и переключён на реальный счёт!</b>\n\n"
            f"Лимит стратегии: <b>${deposit:,.0f} USDC</b> ({max_slots}×${slot_sz:,.0f})\n"
            f"ИИ: <b>Venice AI ({esc(model_str)}, {consensus_mode})</b>\n"
            "Режим защиты: <b>Fail-Closed + сверка исполнений</b>\n\n"
            "Используйте кнопки меню ниже для просмотра живых сделок и логов:",
        )

        while True:
            try:
                res = self.api_call("getUpdates", {
                    "offset": self.last_update_id + 1,
                    "timeout": 25,
                })
                if not res.get("ok"):
                    print(f"[bot] getUpdates: {res.get('error') or res.get('description')}", flush=True)
                    time.sleep(2)
                    continue

                for u in res.get("result", []):
                    self.last_update_id = u["update_id"]

                    if "message" in u:
                        msg = u["message"]
                        chat_id = str(msg["chat"]["id"])
                        if chat_id != self.admin_id:
                            continue

                        text = msg.get("text", "").strip()
                        if text in ("/start", "/menu", "/portfolio", "/status"):
                            self.send_menu(chat_id, format_portfolio_msg())
                        elif text in ("/ai", "/verdicts"):
                            self.send_menu(chat_id, format_ai_verdicts_msg())
                        elif text in ("/emergency", "/safe"):
                            self.send_menu(chat_id, format_emergency_msg())
                        elif text == "/ping":
                            p = ping_venice_ai()
                            res_str = (
                                f"✅ Venice AI Ping: {p.get('latency_sec')}с"
                                if p.get("ok")
                                else f"❌ Сбой: {esc(p.get('error'))}"
                            )
                            self.send_menu(chat_id, f"⚡️ <b>Результат проверки связи:</b>\n{res_str}\n\n" + format_emergency_msg())
                        elif text == "/logs":
                            self.send_menu(chat_id, format_logs_msg())
                        elif text == "/settings":
                            self.send_menu(chat_id, format_settings_msg())
                        elif text in ("/manual", "/help", "/docs"):
                            self.send_menu(chat_id, format_manual_msg())

                    elif "callback_query" in u:
                        cb = u["callback_query"]
                        cb_id = cb["id"]
                        data = cb.get("data", "")
                        chat_id = str(cb["message"]["chat"]["id"])
                        if chat_id != self.admin_id:
                            continue

                        if data == "menu_portfolio":
                            self.answer_callback(cb_id)
                            self.send_menu(chat_id, format_portfolio_msg())
                        elif data == "menu_ai":
                            self.answer_callback(cb_id)
                            self.send_menu(chat_id, format_ai_verdicts_msg())
                        elif data == "menu_emergency":
                            self.answer_callback(cb_id)
                            self.send_menu(chat_id, format_emergency_msg())
                        elif data == "menu_logs":
                            self.answer_callback(cb_id)
                            self.send_menu(chat_id, format_logs_msg())
                        elif data == "menu_settings":
                            self.answer_callback(cb_id)
                            self.send_menu(chat_id, format_settings_msg())
                        elif data == "action_ping_ai":
                            p = ping_venice_ai()
                            if p.get("ok"):
                                pop_text = f"✅ Venice AI OK! Задержка: {p.get('latency_sec')}с"
                            else:
                                pop_text = f"❌ Ошибка пинга: {p.get('error', '')[:40]}"
                            self.answer_callback(cb_id, text=pop_text)
                            self.send_menu(chat_id, format_emergency_msg())

            except Exception as exc:
                print(f"[bot] ошибка обработки обновлений:\n{traceback.format_exc()}", flush=True)
                bot_alerts.problem(
                    "bot_handler", "сбой обработки команды Telegram-бота (ошибка в коде)",
                    f"{type(exc).__name__}: {exc}", immediate=True,
                )
                time.sleep(3)
            bot_alerts.sweep()


if __name__ == "__main__":
    cfg = load_tg_config()
    token = cfg.get("bot_token")
    admin_id = cfg.get("admin_chat_id")

    if not token or not admin_id:
        print("Ошибка: задайте bot_token и admin_chat_id в state/telegram_config.json")
        sys.exit(1)

    bot = SimpleTelegramBot(token, admin_id)
    bot.run()
