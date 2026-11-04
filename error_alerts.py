"""Оповещения в Telegram о затяжных сбоях.

Разовая ошибка API (одиночный 403, таймаут) не беспокоит: алерт уходит, только если проблема
повторяется не меньше alert_after секунд подряд (перерывы между повторами короче clear_after).
Пока проблема не ушла, раз в REMIND_EVERY_SEC приходит напоминание; когда ошибка перестала
повторяться дольше clear_after, приходит сообщение «восстановлено».
Ошибки кода (immediate=True) сообщаются сразу, без ожидания.
Состояние хранится в state/, поэтому перезапуск процесса не дублирует уже отправленные алерты."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from state_io import STATE_DIR, atomic_write_json, esc
from telegram_notifier import send_telegram_alert


ALERT_AFTER_SEC = 20 * 60      # столько проблема должна длиться, чтобы прислать алерт
CLEAR_AFTER_SEC = 15 * 60      # столько без повторов — и проблема считается решенной
REMIND_EVERY_SEC = 6 * 3600    # напоминание о нерешенной проблеме
RETRY_SEND_SEC = 5 * 60        # повтор отправки, если Telegram не принял сообщение


def fmt_duration(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return "меньше минуты"
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


class ProblemTracker:
    """Учет повторяющихся ошибок одного процесса (у каждого процесса свой файл состояния)."""

    def __init__(self, path: Path, alert_after: float = ALERT_AFTER_SEC, clear_after: float = CLEAR_AFTER_SEC):
        self.path = Path(path)
        self.alert_after = alert_after
        self.clear_after = clear_after

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _send(self, rec: Dict[str, Any], text: str, now: float) -> bool:
        rec["last_send_try"] = now
        try:
            return bool(send_telegram_alert(text))
        except Exception:
            return False

    def problem(self, key: str, title: str, detail: str = "", immediate: bool = False, now: Optional[float] = None) -> None:
        """Отметить очередной случай проблемы key. Ничего не бросает: учет ошибок не должен ронять демон."""
        try:
            now = time.time() if now is None else now
            data = self._load()
            rec = data.get(key)
            if rec is None or now - float(rec["last_seen"]) > self.clear_after:
                rec = {"first_seen": now, "count": 0, "alerted_at": 0.0, "last_send_try": 0.0}
            rec.update(title=title, detail=str(detail)[:300], last_seen=now, count=int(rec["count"]) + 1)

            alerted_at = float(rec.get("alerted_at") or 0.0)
            duration = now - float(rec["first_seen"])
            due = (immediate or duration >= self.alert_after) if not alerted_at else now - alerted_at >= REMIND_EVERY_SEC
            if due and now - float(rec.get("last_send_try") or 0.0) >= RETRY_SEND_SEC:
                head = "🔴 <b>Ошибка" if not alerted_at else "🔴 <b>Все еще не исправлено"
                text = (
                    f"{head}: {esc(title)}</b>\n"
                    f"Длится: {fmt_duration(duration)}, повторов: {rec['count']}\n"
                    f"<code>{esc(rec['detail'])}</code>"
                )
                if self._send(rec, text, now):
                    rec["alerted_at"] = now
            data[key] = rec
            atomic_write_json(self.path, data)
        except Exception as exc:
            print(f"[error_alerts] не удалось учесть проблему {key}: {exc}", flush=True)

    def sweep(self, now: Optional[float] = None) -> None:
        """Снять проблемы, которые перестали повторяться, и сообщить о восстановлении тех, о которых был алерт."""
        try:
            now = time.time() if now is None else now
            data = self._load()
            changed = False
            for key, rec in list(data.items()):
                if now - float(rec["last_seen"]) <= self.clear_after:
                    continue
                if float(rec.get("alerted_at") or 0.0):
                    if now - float(rec.get("last_send_try") or 0.0) < RETRY_SEND_SEC:
                        continue
                    lasted = float(rec["last_seen"]) - float(rec["first_seen"])
                    text = f"🟢 <b>Восстановлено: {esc(rec['title'])}</b>\nПроблема длилась {fmt_duration(lasted)}."
                    if not self._send(rec, text, now) and now - float(rec["last_seen"]) < 24 * 3600:
                        changed = True  # Telegram не принял сообщение — повторим позже (запоминаем время попытки)
                        continue
                del data[key]
                changed = True
            if changed:
                atomic_write_json(self.path, data)
        except Exception as exc:
            print(f"[error_alerts] сбой проверки восстановления: {exc}", flush=True)


# Демон симуляции и Telegram-бот — разные процессы, поэтому файлы состояния у них разные
daemon_alerts = ProblemTracker(STATE_DIR / "error_alerts.json")
bot_alerts = ProblemTracker(STATE_DIR / "error_alerts_bot.json")
