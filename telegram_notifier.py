"""Модуль отправки push-уведомлений в Telegram через Bot API (без внешних библиотек)."""

import html
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from state_io import STATE_DIR, atomic_write_json


CONFIG_FILE = STATE_DIR / "telegram_config.json"
TG_MAX_LEN = 4096


def load_tg_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "admin_chat_id": os.environ.get("TELEGRAM_ADMIN_CHAT_ID", ""),
        "alerts_enabled": True,
    }


def save_tg_config(cfg: dict) -> None:
    atomic_write_json(CONFIG_FILE, cfg, mode=0o600)


def html_to_plain(text: str) -> str:
    """Снимает HTML-разметку для повторной отправки сообщения простым текстом."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def fit_message(text: str, limit: int = TG_MAX_LEN) -> str:
    """Обрезает сообщение под лимит Telegram (только для простого текста — HTML-теги могут порваться)."""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(обрезано)"


def tg_api_call(token: str, method: str, payload: dict, timeout: int = 35) -> dict:
    """Вызов Bot API. Ошибки не бросает, а возвращает {"ok": False, "error": ...}."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            return {"ok": False, "error": body.get("description", str(e))}
        except Exception:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tg_send_message(token: str, chat_id: str, text: str, reply_markup: Optional[dict] = None) -> bool:
    """Отправляет HTML-сообщение; если Telegram его отверг (разметка/длина) — повторяет простым текстом."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    res = tg_api_call(token, "sendMessage", payload) if len(text) <= TG_MAX_LEN else {"ok": False, "error": "message too long"}
    if res.get("ok"):
        return True

    print(f"[telegram] sendMessage (HTML) не доставлено: {res.get('error')} — повтор простым текстом", flush=True)
    payload.pop("parse_mode")
    payload["text"] = fit_message(html_to_plain(text))
    res = tg_api_call(token, "sendMessage", payload)
    if not res.get("ok"):
        print(f"[telegram] sendMessage не доставлено: {res.get('error')}", flush=True)
    return bool(res.get("ok"))


def send_telegram_alert(message: str, chat_id: Optional[str] = None) -> bool:
    """Отправляет мгновенное push-сообщение администратору в Telegram."""
    cfg = load_tg_config()
    if not cfg.get("alerts_enabled", True):
        return False

    token = cfg.get("bot_token")
    target_id = chat_id or cfg.get("admin_chat_id")
    if not token or not target_id:
        return False

    return tg_send_message(token, str(target_id), message)
