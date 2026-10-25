"""Интерактивный Telegram-бот для управления и мониторинга портфеля $5,000 USDC."""

import json
import os
import sys
import time
import traceback
from pathlib import Path

from telegram_notifier import load_tg_config, tg_api_call, tg_send_message
from ai_analyzer import get_ai_budget, get_ai_health, ping_venice_ai
from paper_trader import is_open, portfolio_totals, position_net_pnl
from state_io import STATE_DIR, esc


STATE_FILE = STATE_DIR / "portfolio_state.json"
LOG_FILE = STATE_DIR / "paper_trader.log"
AI_CACHE_FILE = STATE_DIR / "ai_audits_cache.json"


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


def format_portfolio_msg() -> str:
    if not STATE_FILE.exists():
        return "⚠️ <b>Портфель еще не инициализирован.</b>"

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return f"⚠️ Ошибка чтения портфеля: {e}"

    health = get_ai_health()
    is_em = health.get("emergency_mode", False) or state.get("emergency_mode", False)

    t = portfolio_totals(state)
    deposit, net_pnl, weighted_apr = t["deposit"], t["net_pnl"], t["weighted_apr"]
    elapsed_hours = max(0.01, (time.time() - float(state["started_at"])) / 3600.0)

    pnl_sign = "+" if net_pnl >= 0 else ""
    pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"

    msg = ["📊 <b>СВОДКА ИИ-ПОРТФЕЛЯ (Paper-Trading)</b>"]
    if is_em:
        msg.append(
            "🚨 <b>ВНИМАНИЕ: АКТИВИРОВАН АВАРИЙНЫЙ РЕЖИМ!</b>\n"
            "<i>Связь с Venice AI временно отсутствует. Все MM-лимитки сняты для защиты капитала.</i>\n"
        )

    msg.extend([
        f"⏱ <i>В работе: {elapsed_hours:.1f} ч (тиков: {state.get('ticks_count', 0)})</i>\n",
        f"💰 <b>Стартовый депозит:</b> ${deposit:,.2f} USDC",
        f"💵 <b>Текущий баланс:</b> <b>${t['equity']:,.4f} USDC</b>",
        f"{pnl_emoji} <b>Чистый PnL:</b> <b>{pnl_sign}${net_pnl:,.4f} USDC</b>",
        f"   • Реварды и фандинг: +${t['rewards']:,.4f}",
        f"   • Издержки / комиссии: -${t['losses']:,.4f}",
        f"   • Переоценка бондов: ${t['unrealized']:+,.4f} | Реализовано: ${t['realized']:+,.4f}",
        f"🔥 <b>APR открытых слотов:</b> <b>{weighted_apr:.1f}% годовых</b> (~${deposit*(weighted_apr/100)/365:.2f}/день)",
        f"💤 <b>Свободный капитал:</b> ${t['idle_usd']:,.0f}\n",
        f"<b>Слоты ($1,000 каждый), открыто: {t['open_count']}:</b>",
    ])

    for i, p in enumerate(state["positions"], 1):
        slot_pnl = position_net_pnl(p)
        sp_sign = "+" if slot_pnl >= 0 else ""
        icon = "🏦" if p["type"] == "poly_mm" else ("🎯" if p["type"] == "poly_bond" else "⚡️")
        q_short = p['title'][:42] + ("..." if len(p['title']) > 42 else "")
        msg.append(
            f"{i}. {icon} <b>{esc(q_short)}</b>\n"
            f"   APR: <code>{p['expected_apr']:.1f}%</code> | PnL: <b>{sp_sign}${slot_pnl:.4f}</b>"
        )
        if not is_open(p):
            msg.append(f"   ⏹ <i>Закрыта ({esc(p.get('status'))}): {esc(p.get('close_reason', ''))}</i>")
        elif p.get("mm_defensive_paused", False):
            msg.append("   ⚠️ <i>Лимитки сняты (Защитная пауза: нет связи с ИИ)</i>")
        elif float(p.get("mm_paused_until") or 0.0) > time.time():
            until = time.strftime("%H:%M", time.gmtime(float(p["mm_paused_until"])))
            msg.append(f"   ⏸ <i>Лимитки сняты после исполнения, пауза до {until} UTC</i>")
        elif p["type"] == "poly_mm":
            msg.append(f"   Коридор: <code>{p['bid_limit']:.4f} .. {p['ask_limit']:.4f}</code> (сейчас: {p['current_mid']:.4f})")
        elif p["type"] == "poly_bond":
            msg.append(f"   Вход: <code>{p['entry_price']:.4f}</code> → сейчас <code>{p['current_price']:.4f}</code>")

    return "\n".join(msg)


def format_emergency_msg() -> str:
    """Формирует экран статуса аварийного режима и протокола безопасности."""
    health = get_ai_health()
    is_em = health.get("emergency_mode", False)
    status = health.get("status", "HEALTHY")

    if is_em:
        status_line = "🔴 <b>АКТИВЕН (СВЯЗЬ С ИИ ПОТЕРЯНА)</b>"
    elif status == "DEGRADED":
        status_line = "🟡 <b>ДЕГРАДАЦИЯ (Были задержки ответа)</b>"
    else:
        status_line = "🟢 <b>ОНЛАЙН (Штатная работа, всё защищено)</b>"

    last_succ = time.strftime('%H:%M:%S', time.localtime(health.get('last_success_ts', time.time())))
    latency = health.get("last_latency_sec", 0.0)
    fails = health.get("consecutive_failures", 0)
    last_err = health.get("last_error", "") or "Ошибок нет"
    model = os.environ.get("VENICE_MODEL", "gemini-4-argon")

    msg = [
        "🛡 <b>ПРОТОКОЛ БЕЗОПАСНОСТИ И АВАРИЙНЫЙ РЕЖИМ</b>\n",
        f"Статус ИИ-сторожа: {status_line}",
        f"🤖 <b>Модель:</b> <code>{model}</code>",
        f"⏱ <b>Задержка последнего ответа:</b> <code>{latency:.2f} сек</code>",
        f"🕒 <b>Последний успешный опрос:</b> <code>{last_succ} UTC</code>",
        f"⚠️ <b>Ошибок подряд:</b> <code>{fails}</code>",
        f"📋 <b>Последнее системное событие:</b> <code>{esc(last_err[:90])}</code>\n",
        "<b>Как система защищает капитал при сбое связи:</b>",
        "1. 🔒 <b>Fail-Closed:</b> Если ИИ недоступен, бот <b>категорически блокирует</b> вход в любые новые позиции. Никаких сделок «вслепую».",
        "2. 🛑 <b>MM Defensive Pause:</b> Лимитные ордера в стакане Polymarket немедленно снимаются, исключая риск токсичного выкупа информированными трейдерами.",
        "3. 🎯 <b>Bonding Stop-Loss:</b> Если котировка купленного бонда проседает ниже 95.0¢, позиция продается (действует всегда, не только при сбое ИИ).",
        "4. ⚡️ <b>Funding Auto-Halt:</b> Фандинг Hyperliquid отслеживается через L1 API; позиция закрывается, если знак ставки 2 тика подряд против нас.",
        "5. 🧠 <b>Повторный аудит ИИ:</b> Открытые позиции Polymarket перепроверяются каждые 6 ч; два SKIP подряд — позиция закрывается.",
        "6. 📲 <b>Push-Оповещения:</b> Мгновенный алерт в Telegram при сбое и автоматическое возобновление при поднятии связи.",
    ]
    return "\n".join(msg)


def format_ai_verdicts_msg() -> str:
    if not AI_CACHE_FILE.exists():
        return "🧠 <b>Журнал аудитов ИИ пока пуст.</b>"

    try:
        cache = json.loads(AI_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return f"⚠️ Ошибка чтения кэша ИИ: {e}"

    msg = ["🧠 <b>ПОСЛЕДНИЕ РЕШЕНИЯ VENICE AI</b>\n"]
    items = list(cache.items())[-8:]
    for key, data in reversed(items):
        parts = key.split("::")
        strat = parts[0]
        q = parts[-1][:45]
        res = data.get("result", {})
        dec = res.get("decision", "UNKNOWN")
        dec_icon = "✅" if dec == "APPROVE" else "❌"
        conf = res.get("confidence", 0)
        summary = res.get("summary", "")

        msg.append(
            f"{dec_icon} <b>{esc(dec)} ({conf}%)</b> | <i>{esc(strat)}</i>\n"
            f"<b>{esc(q)}...</b>\n"
            f"<i>\"{esc(summary[:220])}\"</i>\n"
        )
    return "\n".join(msg)


def format_logs_msg() -> str:
    if not LOG_FILE.exists():
        return "📜 <b>Лог событий пока пуст.</b>"

    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    # Берем строки с конца, пока сообщение укладывается в лимит Telegram (4096 символов)
    recent, size = [], 0
    for line in reversed(lines[-12:]):
        line = esc(line if len(line) <= 300 else line[:297] + "...")
        if size + len(line) + 1 > 3700:
            break
        recent.insert(0, line)
        size += len(line) + 1
    return f"📜 <b>ПОСЛЕДНИЕ {len(recent)} СОБЫТИЙ:</b>\n\n<code>" + "\n".join(recent) + "</code>"


def format_settings_msg() -> str:
    cfg = load_tg_config()
    alerts = "Включены ✅" if cfg.get("alerts_enabled", True) else "Выключены ❌"
    model = os.environ.get("VENICE_MODEL", "gemini-4-argon")
    budget = get_ai_budget()
    deposit = 5000.0
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            deposit = float(st.get("initial_deposit_usd", 5000.0))
        except Exception:
            pass
    slot_sz = deposit / 5.0
    return (
        "⚙️ <b>НАСТРОЙКИ СИСТЕМЫ</b>\n\n"
        f"🤖 <b>Модель Venice AI:</b> <code>{esc(model)}</code> (консенсус 2/2 при входе)\n"
        f"💳 <b>Вызовов ИИ сегодня:</b> {budget['calls']} из {budget['limit']} (~0.003 DIEM за вызов; "
        f"{budget['reserve']} зарезервировано под повторные аудиты)\n"
        f"🔔 <b>Push-алерты:</b> {alerts}\n"
        f"📐 <b>Формула ревардов MM:</b> Квадратичная по живому стакану CLOB (потолок 365% годовых)\n"
        f"🔎 <b>Исполнение лимиток:</b> по ленте сделок Polymarket, проверка раз в 60 с; пауза 3 ч после исполнения\n"
        f"📅 <b>Фильтр MM:</b> только рынки, до истечения которых ≥ 7 дней (снятие при < 2 дн.)\n"
        f"💼 <b>Размер депозита:</b> ${deposit:,.0f} USDC (5 слотов по ${slot_sz:,.0f})\n"
        f"🛡 <b>Kill-Switch:</b> Активен (сдвиг ≥1.0¢ от цены выставления лимиток, повторный аудит ИИ каждые 6ч)\n"
        f"🚨 <b>Аварийный режим (Fail-Closed):</b> Активен (пульс ИИ каждый тик, автоснятие ордеров)\n\n"
        "📖 Подробная справка по всем формулам доступна по кнопке <b>«Инструкция»</b> ниже."
    )


def format_manual_msg() -> str:
    return (
        "📖 <b>ПОЛНОЕ РУКОВОДСТВО ПОЛЬЗОВАТЕЛЯ И ТЕХНИЧЕСКАЯ СПРАВКА</b>\n\n"
        "Вся исчерпывающая документация проекта доступна в репозитории GitHub:\n"
        "🔗 <a href='https://github.com/ahdpe/ai-yield-scanner/blob/main/MANUAL.md'><b>Открыть MANUAL.md</b></a>\n\n"
        "<b>В руководстве описаны:</b>\n"
        "• Архитектурная концепция и некастодиальная безопасность\n"
        "• 3 стратегии (Polymarket MM, Smart Bonding, Hyperliquid Funding)\n"
        "• Математика расстановки ордеров и сбора USDC-ревардов\n"
        "• Экономика Venice AI (gemini-4-argon, лимит 0.3 DIEM/день)\n"
        "• Аварийный протокол безопасности при потере связи\n"
        "• Команды и управление через Telegram-бота"
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
        print(f"Запуск Telegram-бота (Admin ID: {self.admin_id})...", flush=True)
        # Отправляем приветственное сообщение при старте
        self.send_menu(
            self.admin_id,
            "🚀 <b>ИИ-Бот запущен и готов к работе!</b>\n\n"
            "Портфель: <b>$5,000 USDC</b>\n"
            "ИИ: <b>Venice AI (gemini-4-argon)</b>\n"
            "Аварийный режим: <b>Fail-Closed (Защищен)</b>\n\n"
            "Используйте кнопки меню ниже для управления и чтения инструкции:",
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

                    # Обработка текстовых команд
                    if "message" in u:
                        msg = u["message"]
                        chat_id = str(msg["chat"]["id"])
                        if chat_id != self.admin_id:
                            continue  # игнорируем чужие сообщения

                        text = msg.get("text", "").strip()
                        if text in ["/start", "/menu"]:
                            self.send_menu(chat_id, format_portfolio_msg())
                        elif text in ["/portfolio", "/status"]:
                            self.send_menu(chat_id, format_portfolio_msg())
                        elif text in ["/ai", "/verdicts"]:
                            self.send_menu(chat_id, format_ai_verdicts_msg())
                        elif text in ["/emergency", "/safe"]:
                            self.send_menu(chat_id, format_emergency_msg())
                        elif text == "/ping":
                            p = ping_venice_ai()
                            res_str = f"✅ Venice AI Ping: {p.get('latency_sec')}с" if p.get("ok") else f"❌ Сбой: {esc(p.get('error'))}"
                            self.send_menu(chat_id, f"⚡️ <b>Результат проверки связи:</b>\n{res_str}\n\n" + format_emergency_msg())
                        elif text == "/logs":
                            self.send_menu(chat_id, format_logs_msg())
                        elif text == "/settings":
                            self.send_menu(chat_id, format_settings_msg())
                        elif text in ["/manual", "/help", "/docs"]:
                            self.send_menu(chat_id, format_manual_msg())

                    # Обработка инлайн-кнопок
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

            except Exception:
                print(f"[bot] ошибка обработки обновлений:\n{traceback.format_exc()}", flush=True)
                time.sleep(3)


if __name__ == "__main__":
    cfg = load_tg_config()
    token = cfg.get("bot_token")
    admin_id = cfg.get("admin_chat_id")

    if not token or not admin_id:
        print("Ошибка: задайте bot_token и admin_chat_id в state/telegram_config.json")
        sys.exit(1)

    bot = SimpleTelegramBot(token, admin_id)
    bot.run()
