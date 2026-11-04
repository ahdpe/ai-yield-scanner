#!/usr/bin/env python3
"""ExecStopPost-хук systemd: сообщает в Telegram, что сервис упал (а не был остановлен штатно).

Использование в unit-файле:  ExecStopPost=/usr/bin/python3 %h/ai_yield_scanner/service_exit_alert.py %n
systemd передает итог остановки в переменных SERVICE_RESULT / EXIT_CODE / EXIT_STATUS.
При штатной остановке или перезапуске (SERVICE_RESULT=success) ничего не отправляется.
Падения в цикле перезапусков сообщаются не чаще раза в THROTTLE_SEC, с числом пропущенных.

Скрипт намеренно самодостаточный (только stdlib, сам читает state/telegram_config.json):
он должен сработать, даже если основной код сломан и не импортируется."""

import html
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
CONFIG_FILE = STATE_DIR / "telegram_config.json"
THROTTLE_FILE = STATE_DIR / "service_exit_alerts.json"
THROTTLE_SEC = 30 * 60


def journal_tail(unit: str, lines: int = 12) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return ""
    return out.strip()[-1500:]


def send(cfg: dict, text: str) -> bool:
    token, chat_id = cfg.get("bot_token"), cfg.get("admin_chat_id")
    if not token or not chat_id or not cfg.get("alerts_enabled", True):
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception as exc:
        print(f"[service_exit_alert] Telegram не принял сообщение: {exc}", file=sys.stderr)
        return False


def main() -> int:
    result = os.environ.get("SERVICE_RESULT", "")
    if result in ("", "success"):
        return 0
    unit = sys.argv[1] if len(sys.argv) > 1 else "unknown.service"
    code, status = os.environ.get("EXIT_CODE", "?"), os.environ.get("EXIT_STATUS", "?")
    now = time.time()

    try:
        throttle = json.loads(THROTTLE_FILE.read_text(encoding="utf-8"))
    except Exception:
        throttle = {}
    rec = throttle.get(unit, {"last_alert": 0.0, "suppressed": 0})
    if now - float(rec["last_alert"]) < THROTTLE_SEC:
        rec["suppressed"] = int(rec["suppressed"]) + 1
    else:
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        extra = f"\nС прошлого сообщения падал еще {rec['suppressed']} раз." if rec["suppressed"] else ""
        tail = html.escape(journal_tail(unit), quote=False)[-3000:]
        text = (
            f"💥 <b>Сервис упал: {html.escape(unit)}</b>\n"
            f"Причина: <code>{html.escape(result)}</code> ({html.escape(code)}/{html.escape(status)}). "
            f"systemd перезапустит его автоматически.{extra}\n\n"
            f"Последние строки журнала:\n<pre>{tail or 'журнал недоступен'}</pre>"
        )
        if send(cfg, text):
            rec = {"last_alert": now, "suppressed": 0}
    throttle[unit] = rec
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = THROTTLE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(throttle), encoding="utf-8")
        os.replace(tmp, THROTTLE_FILE)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
