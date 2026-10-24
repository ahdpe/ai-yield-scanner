"""Интерактивный Telegram-бот для управления и мониторинга портфеля $5,000 USDC."""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Any, List

from telegram_notifier import load_tg_config, save_tg_config


STATE_FILE = Path("state/portfolio_state.json")
LOG_FILE = Path("state/paper_trader.log")
AI_CACHE_FILE = Path("state/ai_audits_cache.json")


def format_portfolio_msg() -> str:
    if not STATE_FILE.exists():
        return "⚠️ <b>Портфель еще не инициализирован.</b>"

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return f"⚠️ Ошибка чтения портфеля: {e}"

    deposit = float(state["initial_deposit_usd"])
    elapsed_hours = max(0.01, (time.time() - float(state["started_at"])) / 3600.0)
    total_rewards = sum(p["accrued_rewards_usd"] for p in state["positions"])
    total_losses = sum(p["adverse_loss_usd"] for p in state["positions"])
    net_pnl = total_rewards - total_losses
    equity = deposit + net_pnl
    weighted_apr = sum(p["expected_apr"] * (p["allocated_usd"] / deposit) for p in state["positions"])

    pnl_sign = "+" if net_pnl >= 0 else ""
    pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"

    msg = [
        "📊 <b>СВОДКА ИИ-ПОРТФЕЛЯ (Paper-Trading)</b>",
        f"⏱ <i>В работе: {elapsed_hours:.1f} ч (тиков: {state.get('ticks_count', 0)})</i>\n",
        f"💰 <b>Стартовый депозит:</b> ${deposit:,.2f} USDC",
        f"💵 <b>Текущий баланс:</b> <b>${equity:,.4f} USDC</b>",
        f"{pnl_emoji} <b>Чистый PnL:</b> <b>{pnl_sign}${net_pnl:,.4f} USDC</b>",
        f"   • Начислено ревардов: +${total_rewards:,.4f}",
        f"   • Издержки / комиссии: -${total_losses:,.4f}",
        f"🔥 <b>Средний APR:</b> <b>{weighted_apr:.1f}% годовых</b> (~${deposit*(weighted_apr/100)/365:.2f}/день)\n",
        "<b>Текущие открытые слоты ($1,000 каждый):</b>",
    ]

    for i, p in enumerate(state["positions"], 1):
        slot_pnl = p["accrued_rewards_usd"] - p["adverse_loss_usd"]
        sp_sign = "+" if slot_pnl >= 0 else ""
        icon = "🏦" if p["type"] == "poly_mm" else ("🎯" if p["type"] == "poly_bond" else "⚡️")
        q_short = p['title'][:42] + ("..." if len(p['title']) > 42 else "")
        msg.append(
            f"{i}. {icon} <b>{q_short}</b>\n"
            f"   APR: <code>{p['expected_apr']:.1f}%</code> | PnL: <b>{sp_sign}${slot_pnl:.4f}</b>"
        )
        if p["type"] == "poly_mm":
            msg.append(f"   Коридор: <code>{p['bid_limit']:.4f} .. {p['ask_limit']:.4f}</code> (сейчас: {p['current_mid']:.4f})")

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
        model = res.get("model_used", "venice")

        msg.append(
            f"{dec_icon} <b>{dec} ({conf}%)</b> | <i>{strat}</i>\n"
            f"<b>{q}...</b>\n"
            f"<i>\"{summary}\"</i>\n"
        )
    return "\n".join(msg)


def format_logs_msg() -> str:
    if not LOG_FILE.exists():
        return "📜 <b>Лог событий пока пуст.</b>"

    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    recent = lines[-12:]
    return "📜 <b>ПОСЛЕДНИЕ 12 СОБЫТИЙ:</b>\n\n<code>" + "\n".join(recent) + "</code>"


def format_settings_msg() -> str:
    cfg = load_tg_config()
    alerts = "Включены ✅" if cfg.get("alerts_enabled", True) else "Выключены ❌"
    model = os.environ.get("VENICE_MODEL", "gemini-4-argon")
    return (
        "⚙️ <b>НАСТРОЙКИ СИСТЕМЫ</b>\n\n"
        f"🤖 <b>Модель Venice AI:</b> <code>{model}</code> (флагманское качество, лимит 0.3 DIEM)\n"
        f"🔔 <b>Push-алерты:</b> {alerts}\n"
        f"⏱ <b>Интервал тиков:</b> 15 минут (900 сек)\n"
        f"💼 <b>Размер депозита:</b> $5,000 USDC (5 слотов по $1,000)\n"
        f"🛡 <b>ИИ Kill-Switch:</b> Активен (автоотмена лимиток при сдвиге цены >1.0¢)\n"
    )


class SimpleTelegramBot:
    def __init__(self, token: str, admin_id: str):
        self.token = token
        self.admin_id = str(admin_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.last_update_id = 0

    def api_call(self, method: str, payload: dict) -> dict:
        url = f"{self.base_url}/{method}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def send_menu(self, chat_id: str, text: str):
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 Портфель", "callback_data": "menu_portfolio"},
                    {"text": "🧠 Решения ИИ", "callback_data": "menu_ai"},
                ],
                [
                    {"text": "📜 Логи событий", "callback_data": "menu_logs"},
                    {"text": "⚙️ Настройки", "callback_data": "menu_settings"},
                ],
                [
                    {"text": "🔄 Обновить сводку", "callback_data": "menu_portfolio"},
                ],
            ]
        }
        self.api_call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        })

    def answer_callback(self, cb_id: str, text: str = ""):
        self.api_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

    def run(self):
        print(f"Запуск Telegram-бота (Admin ID: {self.admin_id})...", flush=True)
        # Отправляем приветственное сообщение при старте
        self.send_menu(
            self.admin_id,
            "🚀 <b>ИИ-Бот запущен на сервере Hermes!</b>\n\n"
            "Портфель: <b>$5,000 USDC</b>\n"
            "ИИ: <b>Venice AI (gemini-4-argon)</b>\n"
            "Нажмите кнопку ниже для просмотра статуса:",
        )

        while True:
            try:
                res = self.api_call("getUpdates", {
                    "offset": self.last_update_id + 1,
                    "timeout": 25,
                })
                if not res.get("ok"):
                    time.sleep(2)
                    continue

                for u in res.get("result", []):
                    self.last_update_id = u["update_id"]

                    # Обработка сообщений
                    if "message" in u:
                        msg = u["message"]
                        chat_id = str(msg["chat"]["id"])
                        if chat_id != self.admin_id:
                            continue  # игнорируем чужие сообщения

                        text = msg.get("text", "")
                        if text in ["/start", "/menu"]:
                            self.send_menu(chat_id, format_portfolio_msg())
                        elif text == "/portfolio":
                            self.send_menu(chat_id, format_portfolio_msg())
                        elif text == "/ai":
                            self.send_menu(chat_id, format_ai_verdicts_msg())
                        elif text == "/logs":
                            self.send_menu(chat_id, format_logs_msg())
                        elif text == "/settings":
                            self.send_menu(chat_id, format_settings_msg())

                    # Обработка инлайн-кнопок
                    elif "callback_query" in u:
                        cb = u["callback_query"]
                        cb_id = cb["id"]
                        data = cb.get("data", "")
                        chat_id = str(cb["message"]["chat"]["id"])
                        if chat_id != self.admin_id:
                            continue

                        self.answer_callback(cb_id)
                        if data == "menu_portfolio":
                            self.send_menu(chat_id, format_portfolio_msg())
                        elif data == "menu_ai":
                            self.send_menu(chat_id, format_ai_verdicts_msg())
                        elif data == "menu_logs":
                            self.send_menu(chat_id, format_logs_msg())
                        elif data == "menu_settings":
                            self.send_menu(chat_id, format_settings_msg())

            except Exception as exc:
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
