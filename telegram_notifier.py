"""Модуль отправки push-уведомлений в Telegram через Bot API (без внешних библиотек)."""

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


CONFIG_FILE = Path("state/telegram_config.json")


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
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def send_telegram_alert(message: str, chat_id: Optional[str] = None) -> bool:
    """Отправляет мгновенное push-сообщение администратору в Telegram."""
    cfg = load_tg_config()
    if not cfg.get("alerts_enabled", True):
        return False

    token = cfg.get("bot_token")
    target_id = chat_id or cfg.get("admin_chat_id")
    if not token or not target_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": target_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok"))
    except Exception:
        return False
