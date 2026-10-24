"""Атомарная запись JSON-состояния и безопасное оформление текста для Telegram (HTML)."""

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"


def atomic_write_json(path: Path, data: Any, mode: int = 0o644) -> None:
    """Пишет JSON во временный файл рядом и атомарно подменяет им целевой (без битых файлов при сбое)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def esc(value: Any) -> str:
    """Экранирует произвольный текст для parse_mode=HTML в Telegram."""
    return html.escape(str(value), quote=False)
