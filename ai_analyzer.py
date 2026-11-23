"""Семантический и риск-анализатор рынков с подключением Venice AI (через локальный прокси Hermes или напрямую)."""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from state_io import STATE_DIR, atomic_write_json, esc
from telegram_notifier import send_telegram_alert
from error_alerts import daemon_alerts


AI_HEALTH_FILE = STATE_DIR / "ai_health.json"
AI_BUDGET_FILE = STATE_DIR / "ai_budget.json"
AI_BACKUP_BUDGET_FILE = STATE_DIR / "ai_budget_backup.json"
AI_BUDGET_ALERTS_FILE = STATE_DIR / "ai_budget_alerts.json"
DEFAULT_AI_CACHE_FILE = STATE_DIR / "ai_audits_cache.json"

# Минимальная уверенность модели, при которой APPROVE засчитывается как одобрение
MIN_APPROVE_CONFIDENCE = 70
# Версия промпта аудита (при изменении старый кэш автоматически переаудируется без удаления файла)
AUDIT_PROMPT_VERSION = 3
# Относительный отступ Stop-Loss в промпте Smart Bonding
SMART_BONDING_STOP_OFFSET = 0.035
# Жесткий потолок платных вызовов Venice в сутки (UTC)
MAX_AI_CALLS_PER_DAY = int(os.environ.get("AI_MAX_CALLS_PER_DAY", "40"))
# Часть суточного лимита, недоступная для новых входов: остается под повторные аудиты открытых позиций
REAUDIT_RESERVE_CALLS = int(os.environ.get("AI_REAUDIT_RESERVE_CALLS", "16"))
# Настройки резервного провайдера Command Code
DEFAULT_COMMANDCODE_BASE_URL = "https://api.commandcode.ai/provider/v1"
BACKUP_MAX_AI_CALLS_PER_DAY = int(os.environ.get("COMMANDCODE_MAX_CALLS_PER_DAY", str(MAX_AI_CALLS_PER_DAY)))
BACKUP_REAUDIT_RESERVE_CALLS = int(os.environ.get("COMMANDCODE_REAUDIT_RESERVE_CALLS", str(REAUDIT_RESERVE_CALLS)))
VENICE_BILLING_COOLDOWN_SEC = 900.0
_venice_cooldown_until: float = 0.0
_venice_cooldown_date: str = ""
# Удлиненный кэш для отклонённых на входе рынков (SKIP), чтобы не тратить Venice каждые 6 ч на один и тот же рынок
ENTRY_SKIP_CACHE_TTL_SEC = 24 * 3600.0
ENTRY_HIGH_SKIP_CACHE_TTL_SEC = 36 * 3600.0
# Размер правил резолюции при плановом реаудите (достаточно для полных условий без обрезания важных исключений)
REAUDIT_DESCRIPTION_MAX_CHARS = 2500
# Максимальный возраст заголовков Google News RSS для резервного анализатора (дней)
GOOGLE_NEWS_MAX_AGE_DAYS = 21

SPORTS_KEYWORDS = [
    r"\bvs\.?\b", r"\bbo1\b", r"\bbo3\b", r"\bbo5\b", r"\bufc\b",
    r"\bnba\b", r"\bnfl\b", r"\bnhl\b", r"\bmlb\b", r"\blol:\b",
    r"\bdota\b", r"\bcs2\b", r"\bvalorant\b", r"\bpremier league\b",
    r"\bchampions league\b", r"\bspread:\b", r"\bover/under\b",
    r"\bpgl\b", r"\besports?\b", r"\bwin on 20\d\d-\d\d-\d\d\b",
    r"\bbundesliga\b", r"\bla liga\b", r"\bserie a\b", r"\bligue 1\b",
    r"\bworld cup\b", r"\bsuper bowl\b",
]

CRYPTO_ASSET_RE = re.compile(
    r"(\b(?:bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|bnb|cardano|ada|avax|sui|hyperliquid|hype|crypto|"
    r"crude oil|wti|brent|nasdaq|s&p\s*500|sp500|dow jones|which asset)\b|"
    r"\bgold\s*\(gc\)|\bsilver\s*\(si\))",
    re.IGNORECASE,
)
CRYPTO_PRICE_TRIGGER_RE = re.compile(
    r"(\$|price of|dip to|reach\b|hit\b|above\b|below\b|all[- ]time high|\bath\b|market cap|"
    r"up or down|best performance in 20\d\d)",
    re.IGNORECASE,
)
STREAMER_NOISE_RE = re.compile(
    r"(\bkai cenat\b|\bishowspeed\b|\bmrbeast\b|\bxqc\b|\basmongold\b|\badin ross\b|"
    r"\bmarathon\b|\blivestream\b|\btwitch\b|\byoutubers?\b|\byoutube\b|"
    r"\bpost\s+\d+\s*(?:-\s*\d+)?\s*(?:tweets?|posts?)\b|\btweets?\s+from\b|"
    r"\bhow many (?:times|tweets?|posts?)\b.*\b(?:post|tweet)\b)",
    re.IGNORECASE,
)

AMBIGUITY_KEYWORDS = [
    "sole discretion", "subjective", "rumor", "unofficial",
    "social media post", "tweet", "x post",
]

OFFICIAL_SOURCES_KEYWORDS = [
    "associated press", "federal reserve", "official", "government",
    "ministry", "electoral", "sec.gov", "bls.gov", "reuters", "bloomberg",
]


def load_venice_api_key() -> str:
    """Загружает VENICE_API_KEY из окружения или из ~/.hermes/.env."""
    key = os.environ.get("VENICE_API_KEY", "").strip()
    if key:
        return key
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VENICE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def load_commandcode_api_key() -> str:
    """Загружает резервный COMMANDCODE_API_KEY из окружения."""
    return os.environ.get("COMMANDCODE_API_KEY", "").strip()


def get_commandcode_models() -> List[str]:
    """Возвращает список моделей для резервного провайдера Command Code."""
    raw = os.environ.get("COMMANDCODE_MODELS", "").strip()
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return ["claude-sonnet-5-5", "xai/grok-4.6"]


def get_commandcode_model_for_slot(slot_idx: int, venice_model: str) -> str:
    """Подбирает модель Command Code для указанного слота консенсуса (сохраняя семейство модели)."""
    mapping = {
        "grok-4-6": "xai/grok-4.6",
        "grok-4-7": "xai/grok-4.7",
        "grok-4-5": "xai/grok-4.5",
        "gemini-4-argon": "google/gemini-4-argon",
        "gemini-4-carbon": "google/gemini-4-carbon",
        "gemini-4-neon": "google/gemini-4-neon",
    }
    mapped = mapping.get(venice_model) or (venice_model if venice_model.startswith("claude-") else "")
    raw = os.environ.get("COMMANDCODE_MODELS", "").strip()
    if raw:
        cc_models = [m.strip() for m in raw.split(",") if m.strip()]
        if cc_models:
            if mapped and mapped in cc_models:
                return mapped
            return cc_models[slot_idx % len(cc_models)]
    if mapped:
        return mapped
    cc_defaults = get_commandcode_models()
    return cc_defaults[slot_idx % len(cc_defaults)]


def get_ai_health() -> Dict[str, Any]:
    """Возвращает текущий статус здоровья подключения к Venice AI."""
    if AI_HEALTH_FILE.exists():
        try:
            return json.loads(AI_HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "status": "HEALTHY",
        "consecutive_failures": 0,
        "last_success_ts": time.time(),
        "last_failure_ts": 0,
        "last_error": "",
        "last_latency_sec": 0.0,
        "emergency_mode": False,
        "emergency_since": 0,
    }


def update_ai_health(success: bool, error: str = "", latency: float = 0.0) -> Dict[str, Any]:
    """Обновляет статус здоровья подключения к Venice AI и при необходимости включает аварийный режим."""
    health = get_ai_health()
    now = time.time()
    AI_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not success:
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
        health["last_failure_ts"] = now
        health["last_error"] = error
        if latency > 0:
            health["last_latency_sec"] = round(latency, 2)

        # При 2 и более последовательных ошибках активируем аварийный режим
        if health["consecutive_failures"] >= 2 and not health.get("emergency_mode", False):
            health["emergency_mode"] = True
            health["emergency_since"] = now
            health["status"] = "EMERGENCY"
            send_telegram_alert(
                "🚨 <b>АВАРИЙНЫЙ РЕЖИМ АКТИВИРОВАН!</b>\n\n"
                "❌ <b>Потеряна связь с Venice AI!</b>\n"
                f"Ошибка: <code>{esc(error[:120])}</code>\n"
                f"Ошибок подряд: <code>{health['consecutive_failures']}</code>\n\n"
                "🛡 <b>Защитный протокол вступил в силу:</b>\n"
                "• Все новые сделки заблокированы (Fail-Closed)\n"
                "• MM-лимитки переведены в защитную паузу (защита от токсичного потока)\n"
                "• Бондинг-позиции поставлены на жесткий Stop-Loss (95¢)"
            )
        elif health.get("emergency_mode", False):
            health["status"] = "EMERGENCY"
        else:
            health["status"] = "DEGRADED"

    else:
        was_emergency = health.get("emergency_mode", False)
        health["consecutive_failures"] = 0
        health["last_success_ts"] = now
        health["last_error"] = ""
        health["last_latency_sec"] = round(latency, 2)
        health["status"] = "HEALTHY"
        health["emergency_mode"] = False
        health["emergency_since"] = 0

        if was_emergency:
            send_telegram_alert(
                "✅ <b>АВАРИЙНЫЙ РЕЖИМ СНЯТ!</b>\n\n"
                "🟢 <b>Связь с Venice AI успешно восстановлена.</b>\n"
                f"Задержка ответа: <code>{latency:.2f} сек</code>\n"
                "Все торговые модули возвращены в штатный режим."
            )

    atomic_write_json(AI_HEALTH_FILE, health)
    return health


def ping_venice_ai() -> Dict[str, Any]:
    """Быстрый проверочный пинг Venice AI через эндпоинт моделей (0 токенов расхода)."""
    api_key = load_venice_api_key()
    base_url = os.environ.get("VENICE_BASE_URL", "http://127.0.0.1:8787/api/v1")
    url = f"{base_url.rstrip('/')}/models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "AI-Yield-Scanner/1.0",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency = time.time() - t0
            health = update_ai_health(success=True, latency=latency)
            return {
                "ok": True,
                "latency_sec": round(latency, 2),
                "models_count": len(data.get("data", [])),
                "health": health,
            }
    except Exception as exc:
        latency = time.time() - t0
        err_msg = str(exc)
        health = update_ai_health(success=False, error=err_msg, latency=latency)
        return {
            "ok": False,
            "latency_sec": round(latency, 2),
            "error": err_msg,
            "health": health,
        }


def classify_polymarket_risk(market: Dict[str, Any]) -> Dict[str, Any]:
    """Первичный эвристический фильтр безопасности рынка (от 0 до 100)."""
    question = (market.get("question") or "").lower()
    description = (market.get("description") or "").lower()
    full_text = f"{question} {description}"

    is_sports = bool(
        market.get("sportsMarketType")
        or market.get("gameId")
        or any(re.search(pat, question) for pat in SPORTS_KEYWORDS)
    )
    is_streamer_noise = bool(STREAMER_NOISE_RE.search(question))
    is_crypto_price_target = bool(
        (CRYPTO_ASSET_RE.search(question) and CRYPTO_PRICE_TRIGGER_RE.search(question))
        or is_streamer_noise
    )
    has_ambiguity = any(kw in full_text for kw in AMBIGUITY_KEYWORDS)
    has_official_source = any(kw in full_text for kw in OFFICIAL_SOURCES_KEYWORDS)

    hr_change = abs(float(market.get("oneHourPriceChange") or 0.0))
    day_change = abs(float(market.get("oneDayPriceChange") or 0.0))

    score = 80
    reasons: List[str] = []

    if is_sports:
        score -= 45
        reasons.append("Спорт/Киберспорт (высокий риск резких скачков цены в лайве)")
    elif is_streamer_noise:
        score -= 40
        reasons.append("Стримы/блогеры/твиты (лайв-шум без покрытия в официальных СМИ)")
    elif is_crypto_price_target:
        score -= 35
        reasons.append("Биржевой/крипто-таргет цены (риск резкого пробоя стоп-лосса и нет лайв-котировок у ИИ)")
    else:
        reasons.append("Макро/Политика/События (нет секундных лайв-скачков)")

    if hr_change <= 0.003 and day_change <= 0.015:
        score += 15
        reasons.append(f"Низкая волатильность (Δ1ч={hr_change*100:.1f}¢, Δ24ч={day_change*100:.1f}¢ — «спящий» стакан)")
    elif hr_change > 0.02 or day_change > 0.06:
        score -= 25
        reasons.append(f"Повышенная волатильность (Δ1ч={hr_change*100:.1f}¢, Δ24ч={day_change*100:.1f}¢)")

    if has_official_source:
        score += 5
        reasons.append("Четкий официальный источник резолюции в правилах")

    if has_ambiguity:
        score -= 15
        reasons.append("В правилах есть субъективные критерии / соцсети")

    score = max(0, min(100, score))
    if score >= 75:
        verdict = "БЕЗОПАСНО (Отлично для бота)"
    elif score >= 50:
        verdict = "УМЕРЕННЫЙ РИСК (Нужен ИИ-сторож новостей)"
    else:
        verdict = "ОПАСНО (Высокий риск выкупа лимиток)"

    return {
        "score": score,
        "verdict": verdict,
        "is_sports": is_sports,
        "is_crypto_price_target": is_crypto_price_target,
        "hr_change_cents": hr_change * 100,
        "day_change_cents": day_change * 100,
        "reasons": reasons,
    }


def _notify_budget_limit_once(kind: str, today: str, calls: int, detail: str = "") -> None:
    """Отправляет уведомление о лимите или переключении на резерв один раз для текущего порога."""
    sig = (
        f"{today}:{kind}:{MAX_AI_CALLS_PER_DAY}:{REAUDIT_RESERVE_CALLS}:"
        f"{BACKUP_MAX_AI_CALLS_PER_DAY}:{BACKUP_REAUDIT_RESERVE_CALLS}"
    )
    try:
        state = json.loads(AI_BUDGET_ALERTS_FILE.read_text(encoding="utf-8")) if AI_BUDGET_ALERTS_FILE.exists() else {}
    except Exception:
        state = {}
    if state.get("date") != today:
        state = {"date": today, "sent": []}
    sent = state.get("sent") if isinstance(state.get("sent"), list) else []
    if sig in sent:
        return

    entry_cap = max(0, MAX_AI_CALLS_PER_DAY - REAUDIT_RESERVE_CALLS)
    backup_entry_cap = max(0, BACKUP_MAX_AI_CALLS_PER_DAY - BACKUP_REAUDIT_RESERVE_CALLS)
    if kind == "full":
        text = (
            "🚨 <b>[Venice AI] Исчерпан ПОЛНЫЙ суточный лимит вызовов!</b>\n\n"
            f"Использовано <code>{calls} / {MAX_AI_CALLS_PER_DAY}</code> вызовов за {today} UTC.\n"
            "• Новые покупки и плановые реаудиты открытых позиций приостановлены до <code>00:00 UTC</code>.\n"
            "• Защитный Stop-Loss (<code>-3.5¢</code> от цены входа) продолжает работать штатно."
        )
    elif kind == "backup_full":
        text = (
            "🚨 <b>[AI Резерв / Command Code] Исчерпан ПОЛНЫЙ суточный лимит!</b>\n\n"
            f"Использовано <code>{calls} / {BACKUP_MAX_AI_CALLS_PER_DAY}</code> резервных вызовов за {today} UTC.\n"
            "• Новые покупки и плановые реаудиты остановлены до <code>00:00 UTC</code>.\n"
            "• Защитный Stop-Loss (<code>-3.5¢</code> от цены входа) продолжает работать штатно."
        )
    elif kind == "backup_entry":
        text = (
            "⚠️ <b>[AI Резерв / Command Code] Исчерпан суточный лимит на новые входы</b>\n\n"
            f"Использовано <code>{calls}</code> из <code>{backup_entry_cap}</code> доступных для входа "
            f"(общий резервный лимит <code>{BACKUP_MAX_AI_CALLS_PER_DAY}</code>, резерв <code>{BACKUP_REAUDIT_RESERVE_CALLS}</code> под реаудиты).\n"
            "• Новые покупки на паузе до <code>00:00 UTC</code> (пока не сбросятся лимиты).\n"
            "• Сопровождение, Stop-Loss и реаудиты открытых позиций продолжают работать."
        )
    elif kind == "venice_fallback_budget":
        text = (
            "🛟 <b>[AI Failover] Лимит Venice на входы исчерпан → активен резервный Command Code</b>\n\n"
            f"Venice использовал <code>{calls} / {entry_cap}</code> входов за {today} UTC.\n"
            f"• Бот автоматически перешёл на Command Code (лимит <code>{backup_entry_cap}</code> входов + <code>{BACKUP_REAUDIT_RESERVE_CALLS}</code> резерв).\n"
            "• Новые покупки и реаудиты продолжают работать штатно."
        )
    elif kind == "venice_fallback_active":
        reason_line = f"Причина: <code>{esc(detail[:160])}</code>\n" if detail else ""
        text = (
            "🛟 <b>[AI Failover] Включён резервный провайдер Command Code</b>\n\n"
            f"{reason_line}"
            f"• Аудиты автоматически переведены на Command Code (суточный лимит <code>{BACKUP_MAX_AI_CALLS_PER_DAY}</code>, резерв <code>{BACKUP_REAUDIT_RESERVE_CALLS}</code>).\n"
            "• Торговля и Stop-Loss продолжают работать без остановки."
        )
    else:
        text = (
            "⚠️ <b>[Venice AI] Исчерпан суточный лимит на новые входы</b>\n\n"
            f"Использовано <code>{calls}</code> из <code>{entry_cap}</code> доступных для входа "
            f"(общий лимит <code>{MAX_AI_CALLS_PER_DAY}</code>, резерв <code>{REAUDIT_RESERVE_CALLS}</code> оставлен под реаудиты).\n"
            "• Новые покупки на паузе до <code>00:00 UTC</code> (либо увеличь <code>AI_MAX_CALLS_PER_DAY</code>).\n"
            "• Сопровождение, Stop-Loss и реаудиты открытых позиций продолжают работать."
        )
    try:
        if send_telegram_alert(text):
            sent.append(sig)
            state["sent"] = sent[-20:]
            atomic_write_json(AI_BUDGET_ALERTS_FILE, state)
    except Exception:
        pass


def _read_budget_state(budget_file: Path, today: str, total_reserve: int = 0) -> Tuple[int, int, int]:
    """Читает (calls, entry_calls, reaudit_calls) за дату UTC с мягкой миграцией старого формата."""
    if budget_file.exists():
        try:
            b = json.loads(budget_file.read_text(encoding="utf-8"))
            if b.get("date") == today:
                calls = max(0, int(b.get("calls", 0)))
                if "entry_calls" in b or "reaudit_calls" in b:
                    entry_calls = max(0, int(b.get("entry_calls", 0)))
                    reaudit_calls = max(0, int(b.get("reaudit_calls", 0)))
                    return max(calls, entry_calls + reaudit_calls), entry_calls, reaudit_calls
                reaudit_calls = min(calls, max(0, total_reserve))
                entry_calls = max(0, calls - reaudit_calls)
                return calls, entry_calls, reaudit_calls
        except Exception:
            pass
    return 0, 0, 0


def _read_budget_calls(budget_file: Path, today: str) -> int:
    """Читает общее число израсходованных вызовов за указанную дату UTC из файла бюджета."""
    calls, _entry, _reaudit = _read_budget_state(budget_file, today)
    return calls


def _has_budget_in_file(
    budget_file: Path, limit: int, reserve: int = 0, total_reserve: int = 0
) -> Tuple[bool, int, str]:
    """Проверяет остаток раздельного бюджета (входы vs реаудиты) без списания."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    eff_reserve = total_reserve if total_reserve > 0 else max(0, reserve)
    calls, entry_calls, reaudit_calls = _read_budget_state(budget_file, today, total_reserve=eff_reserve)
    if reserve > 0:
        entry_limit = max(0, limit - reserve)
        return (calls < limit and entry_calls < entry_limit), entry_calls, today
    reaudit_limit = eff_reserve if eff_reserve > 0 else limit
    return (calls < limit and reaudit_calls < reaudit_limit), calls, today


def _consume_budget_in_file(
    budget_file: Path, limit: int, reserve: int = 0, total_reserve: int = 0
) -> Tuple[bool, int, str]:
    """Атомарно списывает один вызов из раздельного пула (entry_calls или reaudit_calls)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    eff_reserve = total_reserve if total_reserve > 0 else max(0, reserve)
    calls, entry_calls, reaudit_calls = _read_budget_state(budget_file, today, total_reserve=eff_reserve)
    if reserve > 0:
        entry_limit = max(0, limit - reserve)
        if calls >= limit or entry_calls >= entry_limit:
            return False, entry_calls, today
        entry_calls += 1
    else:
        reaudit_limit = eff_reserve if eff_reserve > 0 else limit
        if calls >= limit or reaudit_calls >= reaudit_limit:
            return False, calls, today
        reaudit_calls += 1
    calls = entry_calls + reaudit_calls
    budget = {
        "date": today,
        "calls": calls,
        "entry_calls": entry_calls,
        "reaudit_calls": reaudit_calls,
    }
    atomic_write_json(budget_file, budget)
    return True, (entry_calls if reserve > 0 else calls), today


def _backup_has_capacity(reserve: int = 0) -> bool:
    """Проверяет, настроен ли резервный Command Code и есть ли в нём свободный лимит."""
    if not load_commandcode_api_key():
        return False
    ok, _calls, _today = _has_budget_in_file(
        AI_BACKUP_BUDGET_FILE,
        BACKUP_MAX_AI_CALLS_PER_DAY,
        reserve,
        total_reserve=BACKUP_REAUDIT_RESERVE_CALLS,
    )
    return ok


def _consume_ai_call_budget(reserve: int = 0) -> bool:
    """Учитывает платный вызов Venice в раздельном суточном лимите. False — лимит на сегодня исчерпан."""
    ok, calls, today = _consume_budget_in_file(
        AI_BUDGET_FILE,
        MAX_AI_CALLS_PER_DAY,
        reserve,
        total_reserve=REAUDIT_RESERVE_CALLS,
    )
    if not ok:
        backup_reserve = BACKUP_REAUDIT_RESERVE_CALLS if reserve > 0 else 0
        if _backup_has_capacity(backup_reserve):
            if reserve > 0:
                _notify_budget_limit_once("venice_fallback_budget", today, calls)
        else:
            _notify_budget_limit_once("entry" if reserve > 0 else "full", today, calls)
        return False
    return True


def _consume_backup_ai_call_budget(reserve: int = 0) -> bool:
    """Учитывает платный вызов резервного Command Code в его отдельном суточном лимите."""
    ok, calls, today = _consume_budget_in_file(
        AI_BACKUP_BUDGET_FILE,
        BACKUP_MAX_AI_CALLS_PER_DAY,
        reserve,
        total_reserve=BACKUP_REAUDIT_RESERVE_CALLS,
    )
    if not ok:
        _notify_budget_limit_once("backup_entry" if reserve > 0 else "backup_full", today, calls)
        return False
    return True


def get_ai_budget() -> Dict[str, Any]:
    """Сколько платных вызовов Venice сделано сегодня (UTC) по пулам входов и реаудитов."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    calls, entry_calls, reaudit_calls = _read_budget_state(
        AI_BUDGET_FILE, today, total_reserve=REAUDIT_RESERVE_CALLS
    )
    return {
        "date": today,
        "calls": calls,
        "entry_calls": entry_calls,
        "reaudit_calls": reaudit_calls,
        "limit": MAX_AI_CALLS_PER_DAY,
        "entry_limit": max(0, MAX_AI_CALLS_PER_DAY - REAUDIT_RESERVE_CALLS),
        "reserve": REAUDIT_RESERVE_CALLS,
    }


def get_backup_ai_budget() -> Dict[str, Any]:
    """Статус, модели и расход резервного провайдера Command Code на сегодня (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    calls, entry_calls, reaudit_calls = _read_budget_state(
        AI_BACKUP_BUDGET_FILE, today, total_reserve=BACKUP_REAUDIT_RESERVE_CALLS
    )
    return {
        "enabled": bool(load_commandcode_api_key()),
        "date": today,
        "calls": calls,
        "entry_calls": entry_calls,
        "reaudit_calls": reaudit_calls,
        "limit": BACKUP_MAX_AI_CALLS_PER_DAY,
        "entry_limit": max(0, BACKUP_MAX_AI_CALLS_PER_DAY - BACKUP_REAUDIT_RESERVE_CALLS),
        "reserve": BACKUP_REAUDIT_RESERVE_CALLS,
        "models": get_commandcode_models(),
    }


def entry_budget_exhausted() -> bool:
    """True только когда лимиты на новые входы исчерпаны и в Venice, и в доступном резерве Command Code."""
    b = get_ai_budget()
    if int(b["entry_calls"]) < int(b["entry_limit"]) and int(b["calls"]) < int(b["limit"]):
        return False
    if load_commandcode_api_key():
        bb = get_backup_ai_budget()
        if int(bb["entry_calls"]) < int(bb["entry_limit"]) and int(bb["calls"]) < int(bb["limit"]):
            _notify_budget_limit_once("venice_fallback_budget", b["date"], int(b["entry_calls"]))
            return False
        _notify_budget_limit_once("backup_entry", bb["date"], int(bb["entry_calls"]))
        return True
    _notify_budget_limit_once("entry", b["date"], int(b["entry_calls"]))
    return True


STRATEGY_BRIEFS = {
    "polymarket_liquidity_maker": (
        "Стратегия: ставим двусторонние лимитные ордера (bid и ask) вокруг текущей цены ради ежедневных USDC-ревардов Polymarket. "
        "Главный риск — резкий скачок цены (новость, событие в ближайшие дни), из-за которого наши ордера исполнят информированные трейдеры. "
        "Если рынок истекает в ближайшие дни или его исход косвенно зависит от выборов, голосований, заседаний и других событий "
        "на этой неделе — ставь SKIP, даже если формально вопрос рынка про более поздний срок."
    ),
    "polymarket_smart_bonding": (
        "Стратегия Smart Bonding: ПОКУПАЕМ исход [{outcome}] по цене ${price:.4f} (валовый апсайд +{upside_pct:.2f}% к $1.00) и держим до резолюции."
    ),
    "hyperliquid_funding": (
        "Стратегия: дельта-нейтральный сбор фандинга на Hyperliquid (перп-нога + хедж). "
        "Главные риски — разворот знака фандинга, ликвидация перп-ноги при резком движении цены, недоступность хеджа для этого актива."
    ),
}


def _build_audit_prompt(
    question: str,
    description: str,
    price_yes: float,
    strategy_type: str,
    outcome_label: str,
    end_date: str = "",
    stop_loss_price: float = 0.0,
) -> str:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    end_line = ""
    end_line_en = "not specified"
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            end_dt = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
            days_rem = (end_dt - now).total_seconds() / 86400.0
            end_line = (
                f"Дата истечения рынка (endDate): {end_dt.strftime('%Y-%m-%d %H:%M')} UTC — "
                f"через {days_rem:.1f} дн.\n"
            )
            end_line_en = f"{end_dt.strftime('%Y-%m-%d %H:%M')} UTC ({days_rem:.1f} days left)"
        except ValueError:
            end_line = f"Дата истечения рынка (endDate): {end_date}\n"
            end_line_en = end_date

    upside_pct = ((1.0 - price_yes) / price_yes) * 100.0 if 0.0 < price_yes < 1.0 else 0.0
    if strategy_type == "polymarket_smart_bonding":
        stop_price = stop_loss_price if stop_loss_price > 0 else max(0.01, price_yes - SMART_BONDING_STOP_OFFSET)
        return (
            f"Market: {question}\n"
            f"Date (UTC): {today} | Expiry: {end_line_en}\n"
            f"Proposed trade: BUY [{outcome_label}] at ${price_yes:.4f} (gross upside +{upside_pct:.2f}% to $1.00; "
            f"automatic Stop-Loss at ${stop_price:.4f}, -3.5c below entry; AI re-audit every 6h).\n\n"
            "Role: Quantitative risk manager for Polymarket Smart Bonding (target 30-100%+ APR on high-probability outcomes at $0.945-$0.985 with disciplined risk control).\n"
            f"Use live web search to verify current facts and breaking news as of {today}.\n\n"
            "Decision criteria:\n"
            f"- APPROVE (confidence >= 70) if live news confirms the status quo strongly favors [{outcome_label}], "
            f"there is no imminent unpriced binary catalyst likely to gap straight past Stop-Loss ${stop_price:.4f}, "
            "and resolution rules are clear without UMA traps. Do NOT reject a genuinely safe bond just because "
            "absolute upside is 1.5-5% or because you cannot mathematically prove P < (1 - price) with 100% certainty.\n"
            "- SKIP if ANY apply:\n"
            f"  1. Breaking news contradicts [{outcome_label}] or shows active escalation/scandal against us.\n"
            f"  2. A scheduled binary catalyst before expiry (FOMC decision, election, court ruling, award ceremony where the nominee IS shortlisted) has a realistic surprise chance that would gap straight past Stop-Loss ${stop_price:.4f}.\n"
            "  3. Resolution rules have a hidden trap, broad/subjective trigger, or high UMA dispute risk.\n"
            "  4. Web search returned no relevant info for an event whose live status must be verified.\n\n"
            f"Resolution rules:\n{description[:4000]}\n\n"
            'Respond STRICTLY in valid JSON with NO markdown fences (write "summary" in Russian, 1-2 concise sentences):\n'
            '{"decision": "APPROVE" or "SKIP", "confidence": 0_to_100, '
            '"summary": "краткое обоснование на русском (1-2 предложения)", '
            '"watch_keywords": ["keyword1", "keyword2"]}'
        )
    if strategy_type == "polymarket_smart_bonding_reaudit":
        stop_price = stop_loss_price if stop_loss_price > 0 else max(0.01, price_yes - SMART_BONDING_STOP_OFFSET)
        return (
            f"Market: {question}\n"
            f"Date (UTC): {today} | Expiry: {end_line_en}\n"
            f"Existing position: HOLD [{outcome_label}] (current price ${price_yes:.4f}; "
            f"protected by automatic Stop-Loss at ${stop_price:.4f}, -3.5c below entry).\n\n"
            "Role: Quantitative risk manager monitoring an ALREADY OPEN Polymarket Smart Bonding position. "
            "Entry spread and fees are already paid; exiting early incurs bid-ask spread and taker fee losses.\n"
            f"Use live web search to verify current facts and breaking news as of {today}.\n\n"
            "Decision criteria:\n"
            f"- APPROVE (confidence >= 70) to KEEP HOLDING if there is no new credible threat against [{outcome_label}]. "
            "Absence of new news, routine noise, or modest remaining upside is a reason to KEEP HOLDING (APPROVE with confidence >= 75), NOT to exit.\n"
            "- SKIP (confidence >= 75) ONLY if there is a concrete, high-conviction reason to EXIT EARLY right now:\n"
            f"  1. New breaking news or confirmed escalation directly threatens [{outcome_label}] and materially increases the chance it resolves against us.\n"
            f"  2. A newly confirmed imminent binary event creates an unacceptable gap risk straight past Stop-Loss ${stop_price:.4f}.\n\n"
            f"Resolution rules:\n{description[:REAUDIT_DESCRIPTION_MAX_CHARS]}\n\n"
            'Respond STRICTLY in valid JSON with NO markdown fences (write "summary" in Russian, 1-2 concise sentences):\n'
            '{"decision": "APPROVE" or "SKIP", "confidence": 0_to_100, '
            '"summary": "краткое обоснование на русском (1-2 предложения)", '
            '"watch_keywords": ["keyword1", "keyword2"]}'
        )

    brief = STRATEGY_BRIEFS.get(strategy_type, f"Стратегия: {strategy_type}.").format(
        outcome=outcome_label, price=price_yes, upside_pct=upside_pct
    )
    if strategy_type == "hyperliquid_funding":
        market_block = f"Актив: {question}\nДанные: {description[:1200]}\n"
    else:
        market_block = (
            f"Вопрос рынка: {question}\n"
            f"Рассматриваемый исход: [{outcome_label}], текущая цена ${price_yes:.4f}\n"
            f"{end_line}"
            f"Правила резолюции: {description[:4000]}\n"
        )
    return (
        "Ты — количественный риск-менеджер алгоритмического фонда.\n"
        f"Текущая дата (UTC): {today}.\n"
        f"{brief}\n"
        f"{market_block}\n"
        "Ответь СТРОГО в формате JSON без markdown-обертки:\n"
        '{"decision": "APPROVE" или "SKIP", "confidence": число_0_100, '
        '"summary": "краткое обоснование на русском (1-2 предложения)", '
        '"watch_keywords": ["слово1", "слово2"]}'
    )


def get_venice_models() -> List[str]:
    """Возвращает список моделей для аудита (VENICE_MODELS через запятую или одиночную VENICE_MODEL)."""
    raw = os.environ.get("VENICE_MODELS", "").strip()
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return [os.environ.get("VENICE_MODEL", "gemini-4-argon").strip() or "gemini-4-argon"]


def get_venice_web_search_mode() -> str:
    """Режим встроенного веб-поиска Venice AI: 'on', 'auto' или 'off' (по умолчанию 'on')."""
    mode = os.environ.get("VENICE_WEB_SEARCH", "on").strip().lower()
    return mode if mode in ("on", "auto", "off") else "on"


def _build_news_query(question: str) -> str:
    """Формирует компактный англоязычный поисковый запрос для Google News RSS по вопросу рынка."""
    clean = re.sub(r"^\s*will\s+", "", (question or "").strip(), flags=re.IGNORECASE)
    clean = re.sub(r"\bby\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:,?\s*\d{4})?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"[?\"'()]+", " ", clean)
    words = [w for w in clean.split() if len(w) > 1][:10]
    if not words:
        words = ["polymarket", "news"]
    year = str(datetime.now(timezone.utc).year)
    if year not in words:
        words.append(year)
    return " ".join(words)


def _fetch_google_news_context(
    question: str,
    max_items: int = 6,
    max_age_days: int = GOOGLE_NEWS_MAX_AGE_DAYS,
) -> str:
    """Читает свежие заголовки из Google News RSS (не старше max_age_days дн.); при сбое возвращает пустую строку."""
    query = _build_news_query(question)
    rss_query = f"{query} when:{max_age_days}d" if max_age_days > 0 else query
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(rss_query)}&hl=en-US&gl=US&ceid=US:en"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml_bytes = resp.read()
        root = ET.fromstring(xml_bytes)
        now_dt = datetime.now(timezone.utc)
        fresh_items: List[Tuple[float, str, str]] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            pub_raw = (item.findtext("pubDate") or "").strip()
            if not title or not pub_raw:
                continue
            try:
                pub_dt = parsedate_to_datetime(pub_raw)
            except (TypeError, ValueError, IndexError):
                continue
            if pub_dt is None:
                continue
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            age_days = (now_dt - pub_dt).total_seconds() / 86400.0
            if age_days > float(max_age_days) or age_days < -1.0:
                continue
            fresh_items.append((pub_dt.timestamp(), pub_raw[:22], title))
        fresh_items.sort(key=lambda row: row[0], reverse=True)
        return "\n".join(f"- [{pub}] {title}" for _, pub, title in fresh_items[:max_items])
    except Exception:
        return ""


def _augment_prompt_with_news(prompt: str, question: str, news_ctx: Optional[str] = None) -> str:
    """Дополняет промпт свежими заголовками Google News RSS для резервного провайдера без встроенного веб-поиска."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if news_ctx is None:
        news_ctx = _fetch_google_news_context(question)
    if news_ctx:
        news_block = f"Live news headlines (Google News RSS, fetched {now_str} UTC):\n{news_ctx}\n\n"
    else:
        news_block = (
            f"Live news headlines (Google News RSS, fetched {now_str} UTC): "
            "no matching recent headlines returned.\n\n"
        )
    updated = prompt.replace(
        "Use live web search to verify current facts and breaking news as of ",
        "Use the live news headlines below to verify current facts and breaking news as of ",
    )
    if "\nResolution rules:" in updated:
        return updated.replace("\nResolution rules:", f"\n{news_block}Resolution rules:", 1)
    if "\nRespond STRICTLY" in updated:
        return updated.replace("\nRespond STRICTLY", f"\n{news_block}Respond STRICTLY", 1)
    return f"{updated}\n\n{news_block}".rstrip()


def _parse_audit_response(
    raw_text: str,
    model_used: str,
    model_requested: str,
    web_search: str,
    latency: float,
    provider: str = "venice",
) -> Dict[str, Any]:
    """Строгий Fail-Closed разбор JSON-ответа ИИ-аудита."""
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in: {raw_text[:80]}")
    parsed = json.loads(match.group(0))

    raw_decision = str(parsed.get("decision", "")).strip().upper()
    decision = raw_decision
    try:
        confidence = max(0, min(100, int(parsed.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    if decision != "APPROVE" or confidence < MIN_APPROVE_CONFIDENCE:
        decision = "SKIP"
    return {
        "decision": decision,
        "raw_decision": raw_decision,
        "confidence": confidence,
        "summary": str(parsed.get("summary", "")),
        "watch_keywords": parsed.get("watch_keywords", []) if isinstance(parsed.get("watch_keywords"), list) else [],
        "model_used": model_used,
        "model_requested": model_requested,
        "web_search": web_search,
        "provider": provider,
        "latency_sec": round(latency, 2),
    }


def _venice_single_audit(prompt: str, api_key: str, base_url: str, model: str) -> Dict[str, Any]:
    """Один вызов Venice. Бросает исключение при сетевой ошибке или неразборчивом ответе."""
    web_mode = get_venice_web_search_mode()
    venice_params: Dict[str, Any] = {"include_venice_system_prompt": False}
    if web_mode in ("on", "auto"):
        venice_params["enable_web_search"] = web_mode
        venice_params["enable_web_citations"] = False
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 900,
        "reasoning_effort": "low",
        "temperature": 0.1,
        "venice_parameters": venice_params,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "AI-Yield-Scanner/1.0",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {body[:180] or exc.reason}") from exc
    latency = time.time() - t0
    raw_text = (data["choices"][0]["message"].get("content") or "").strip()
    return _parse_audit_response(
        raw_text,
        model_used=data.get("model", model),
        model_requested=model,
        web_search=web_mode,
        latency=latency,
        provider="venice",
    )


def _commandcode_single_audit(prompt: str, api_key: str, base_url: str, model: str) -> Dict[str, Any]:
    """Один вызов резервного провайдера Command Code (/messages для Claude, /chat/completions для остальных)."""
    clean_base = (base_url or DEFAULT_COMMANDCODE_BASE_URL).rstrip("/")
    is_claude = model.lower().startswith("claude-")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "AI-Yield-Scanner/1.0",
    }
    if is_claude:
        # Модели Claude 5.5 в Command Code отклоняют параметр temperature на /messages
        headers["anthropic-version"] = "2023-06-01"
        url = f"{clean_base}/messages"
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 1200,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        url = f"{clean_base}/chat/completions"
        payload = {
            "model": model,
            "max_tokens": 1200,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}],
        }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {body[:180] or exc.reason}") from exc
    latency = time.time() - t0

    if is_claude:
        raw_text = "".join(
            str(b.get("text", ""))
            for b in (data.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    else:
        raw_text = (data["choices"][0]["message"].get("content") or "").strip()

    used_model = str(data.get("model") or model)
    return _parse_audit_response(
        raw_text,
        model_used=f"{used_model} [cc]",
        model_requested=model,
        web_search="rss",
        latency=latency,
        provider="commandcode",
    )


def _order_consensus_models(models: List[str]) -> List[str]:
    """Ставит более экономичную модель (Grok/Gemini) первым голосом в консенсусе 2/2, чтобы отсекать SKIP дешевле."""
    def _cost_rank(m: str) -> int:
        ml = m.lower()
        if "grok" in ml or "gemini" in ml or "deepseek" in ml or "qwen" in ml:
            return 0
        return 1
    return sorted(models, key=_cost_rank)


def call_venice_market_audit(
    question: str,
    description: str,
    price_yes: float,
    strategy_type: str = "liquidity_rewards",
    outcome_label: str = "YES",
    cache_file: str = "",
    samples: int = 1,
    cache_ttl_sec: float = 10800,
    budget_reserve: int = 0,
    end_date: str = "",
    stop_loss_price: float = 0.0,
    prefer_backup: bool = False,
    consensus_mode: str = "unanimous_approve",
) -> Dict[str, Any]:
    """Вызывает Venice AI (с автоматическим failover на резервный Command Code) для аудита рынка.

    samples > 1 — консенсус:
    - consensus_mode='unanimous_approve' (по умолчанию, для входа): APPROVE только если все модели одобрили;
    - consensus_mode='unanimous_skip' (подтверждающий реаудит на выход): SKIP только если все модели вернули SKIP.
    prefer_backup=True — для спокойных 24ч-реаудитов сначала использует резервный Command Code (экономя Venice под входы).
    При сбое обоих провайдеров или исчерпании обоих бюджетов строго соблюдается Fail-Closed."""
    global _venice_cooldown_until, _venice_cooldown_date
    eff_mode = "unanimous_skip" if consensus_mode == "unanimous_skip" else "unanimous_approve"
    cache_path = Path(cache_file) if cache_file else DEFAULT_AI_CACHE_FILE
    cache: Dict[str, Any] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    models = get_venice_models()
    req_models = [models[i % len(models)] for i in range(max(1, samples))]
    if len(req_models) >= 2:
        req_models = _order_consensus_models(req_models)
    web_mode = get_venice_web_search_mode()

    if eff_mode == "unanimous_skip":
        cache_key = f"{strategy_type}::{eff_mode}::{outcome_label}::{question[:120]}"
    else:
        cache_key = f"{strategy_type}::{outcome_label}::{question[:120]}"
    now = time.time()
    if cache_key in cache and cache_ttl_sec > 0:
        cached = cache[cache_key].get("result") or {}
        eff_ttl = cache_ttl_sec
        if strategy_type == "polymarket_smart_bonding" and cached.get("decision") == "SKIP":
            conf = int(cached.get("confidence") or 0)
            eff_ttl = max(
                cache_ttl_sec,
                ENTRY_HIGH_SKIP_CACHE_TTL_SEC if conf >= 80 else ENTRY_SKIP_CACHE_TTL_SEC,
            )
        if now - float(cache[cache_key].get("ts", 0)) < eff_ttl:
            cached_models = cached.get("models_requested") or (
                [cached.get("model_requested")] if cached.get("model_requested") else []
            )
            if (
                len(cached_models) == len(req_models)
                and set(cached_models).issubset(set(models))
                and cached.get("web_search") == web_mode
                and cached.get("prompt_ver") == AUDIT_PROMPT_VERSION
                and cached.get("consensus_mode", "unanimous_approve") == eff_mode
            ):
                return cached

    api_key = load_venice_api_key()
    backup_key = load_commandcode_api_key()
    if not api_key and not backup_key:
        update_ai_health(success=False, error="Ключи VENICE_API_KEY и COMMANDCODE_API_KEY не найдены локально")
        return {
            "decision": "SKIP",
            "confidence": 0,
            "summary": "Аварийная блокировка (Fail-Closed): API-ключи ИИ не найдены.",
            "watch_keywords": [],
            "emergency_mode": True,
            "model_used": "none",
        }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _venice_cooldown_date and _venice_cooldown_date != today:
        _venice_cooldown_until = 0.0
        _venice_cooldown_date = ""

    base_url = os.environ.get("VENICE_BASE_URL", "http://127.0.0.1:8787/api/v1")
    backup_base_url = os.environ.get("COMMANDCODE_BASE_URL", DEFAULT_COMMANDCODE_BASE_URL)
    prompt = _build_audit_prompt(
        question, description, price_yes, strategy_type, outcome_label, end_date, stop_loss_price=stop_loss_price
    )
    backup_prompt: str = ""

    votes: List[Dict[str, Any]] = []
    last_err = ""
    for vote_idx, vote_model in enumerate(req_models):
        vote = None
        venice_reason = ""
        venice_failed = False
        venice_billing_err = False
        venice_budget_out = False
        cc_attempted = False
        cc_failed = False
        cc_billing_err = False
        cc_budget_out = False
        cc_reason = ""

        # Гибридный режим (Option B): для спокойных плановых реаудитов сначала вызываем Command Code
        if prefer_backup and backup_key and strategy_type != "polymarket_smart_bonding":
            cc_attempted = True
            if not backup_prompt:
                news_ctx = _fetch_google_news_context(question)
                backup_prompt = _augment_prompt_with_news(prompt, question, news_ctx=news_ctx)
            cc_reserve = BACKUP_REAUDIT_RESERVE_CALLS if budget_reserve > 0 else 0
            if _consume_backup_ai_call_budget(cc_reserve):
                cc_model = get_commandcode_model_for_slot(vote_idx, vote_model)
                try:
                    vote = _commandcode_single_audit(backup_prompt, backup_key, backup_base_url, cc_model)
                except Exception as cc_exc:
                    cc_failed = True
                    cc_reason = f"{cc_model}: {cc_exc}"
                    cc_billing_err = any(code in cc_reason for code in ("HTTP 401:", "HTTP 402:"))
            else:
                cc_budget_out = True
                cc_reason = "лимит резервного Command Code исчерпан"

        if vote is None:
            venice_in_cooldown = _venice_cooldown_until > time.time()
            if api_key and not venice_in_cooldown:
                if _consume_ai_call_budget(budget_reserve):
                    try:
                        vote = _venice_single_audit(prompt, api_key, base_url, vote_model)
                        _venice_cooldown_until = 0.0
                        _venice_cooldown_date = ""
                    except Exception as exc:
                        venice_failed = True
                        venice_reason = f"{vote_model}: {exc}"
                        if any(code in venice_reason for code in ("HTTP 401:", "HTTP 402:")):
                            venice_billing_err = True
                            _venice_cooldown_until = time.time() + VENICE_BILLING_COOLDOWN_SEC
                            _venice_cooldown_date = today
                else:
                    venice_budget_out = True
                    venice_reason = "исчерпан суточный лимит Venice"
            elif not api_key:
                venice_failed = True
                venice_reason = "VENICE_API_KEY отсутствует"
            else:
                venice_failed = True
                venice_billing_err = True
                venice_reason = "Venice на 15-мин паузе после ошибки биллинга (401/402)"

        if vote is None:
            if backup_key and not cc_attempted:
                if not backup_prompt:
                    news_ctx = _fetch_google_news_context(question)
                    if not news_ctx and strategy_type == "polymarket_smart_bonding":
                        return {
                            "decision": "SKIP",
                            "raw_decision": "SKIP",
                            "confidence": 0,
                            "summary": "Резервный провайдер [cc]: в Google News RSS нет свежих заголовков по теме, вход вслепую без веб-поиска Venice заблокирован.",
                            "watch_keywords": [],
                            "emergency_mode": False,
                            "model_used": "none (cc-no-news)",
                        }
                    backup_prompt = _augment_prompt_with_news(prompt, question, news_ctx=news_ctx)
                cc_reserve = BACKUP_REAUDIT_RESERVE_CALLS if budget_reserve > 0 else 0
                if _consume_backup_ai_call_budget(cc_reserve):
                    cc_model = get_commandcode_model_for_slot(vote_idx, vote_model)
                    try:
                        vote = _commandcode_single_audit(backup_prompt, backup_key, backup_base_url, cc_model)
                        if venice_failed:
                            _notify_budget_limit_once(
                                "venice_fallback_active",
                                today,
                                get_backup_ai_budget()["calls"],
                                detail=venice_reason,
                            )
                    except Exception as cc_exc:
                        cc_err = f"{cc_model}: {cc_exc}"
                        last_err = f"Venice({venice_reason}); CommandCode({cc_err})"
                        update_ai_health(success=False, error=last_err, latency=0.0)
                        is_cc_billing = any(code in cc_err for code in ("HTTP 401:", "HTTP 402:"))
                        if venice_billing_err or is_cc_billing:
                            daemon_alerts.problem(
                                "venice_billing",
                                "Venice и резервный Command Code отклонили запрос (биллинг/ключ)",
                                last_err,
                                immediate=True,
                            )
                        else:
                            daemon_alerts.problem(
                                "venice_audit",
                                "сбой аудита Venice и резервного Command Code, входы заблокированы",
                                last_err,
                            )
                        break
                else:
                    if venice_failed:
                        last_err = f"{venice_reason}; лимит резервного Command Code исчерпан"
                        update_ai_health(success=False, error=last_err, latency=0.0)
                        daemon_alerts.problem(
                            "venice_billing" if venice_billing_err else "venice_audit",
                            "ошибка Venice и исчерпан лимит резервного Command Code",
                            last_err,
                            immediate=venice_billing_err,
                        )
                        break
                    summary = (
                        "Суточные лимиты Venice и резервного Command Code на новые входы исчерпаны до 00:00 UTC."
                        if budget_reserve else
                        "Полные суточные лимиты Venice и резервного Command Code исчерпаны до 00:00 UTC."
                    )
                    return {
                        "decision": "SKIP",
                        "confidence": 0,
                        "summary": summary,
                        "watch_keywords": [],
                        "emergency_mode": False,
                        "budget_exhausted": True,
                        "model_used": "none (budget)",
                    }
            elif cc_attempted:
                if venice_budget_out and cc_budget_out:
                    summary = (
                        "Суточные лимиты Venice и резервного Command Code на новые входы исчерпаны до 00:00 UTC."
                        if budget_reserve else
                        "Полные суточные лимиты Venice и резервного Command Code исчерпаны до 00:00 UTC."
                    )
                    return {
                        "decision": "SKIP",
                        "confidence": 0,
                        "summary": summary,
                        "watch_keywords": [],
                        "emergency_mode": False,
                        "budget_exhausted": True,
                        "model_used": "none (budget)",
                    }
                last_err = f"CommandCode({cc_reason}); Venice({venice_reason})"
                update_ai_health(success=False, error=last_err, latency=0.0)
                if venice_billing_err or cc_billing_err:
                    daemon_alerts.problem(
                        "venice_billing",
                        "Venice и резервный Command Code отклонили запрос (биллинг/ключ)",
                        last_err,
                        immediate=True,
                    )
                else:
                    daemon_alerts.problem(
                        "venice_audit",
                        "сбой аудита Command Code и Venice",
                        last_err,
                    )
                break
            else:
                if venice_budget_out:
                    summary = (
                        f"Лимит вызовов Venice на новые входы ({MAX_AI_CALLS_PER_DAY - budget_reserve} из {MAX_AI_CALLS_PER_DAY}) исчерпан — "
                        f"входы заблокированы до 00:00 UTC, резерв {budget_reserve} вызовов оставлен под повторные аудиты."
                        if budget_reserve else
                        f"Суточный лимит вызовов Venice ({MAX_AI_CALLS_PER_DAY}) исчерпан до 00:00 UTC."
                    )
                    return {
                        "decision": "SKIP",
                        "confidence": 0,
                        "summary": summary,
                        "watch_keywords": [],
                        "emergency_mode": False,
                        "budget_exhausted": True,
                        "model_used": "none (budget)",
                    }
                last_err = venice_reason
                update_ai_health(success=False, error=last_err, latency=0.0)
                if venice_billing_err:
                    daemon_alerts.problem(
                        "venice_billing",
                        "Venice AI отклонил запрос (закончился DIEM или неверный ключ)",
                        last_err,
                        immediate=True,
                    )
                else:
                    daemon_alerts.problem("venice_audit", "аудит Venice AI не проходит, новые входы заблокированы", last_err)
                break

        update_ai_health(success=True, latency=vote["latency_sec"])
        votes.append(vote)
        if eff_mode == "unanimous_skip":
            # На подтверждающем реаудите одного APPROVE достаточно, чтобы держать позицию и сэкономить 2-й вызов
            if vote["decision"] == "APPROVE" or vote.get("raw_decision") != "SKIP":
                break
        else:
            if vote["decision"] != "APPROVE":
                break  # одного SKIP достаточно — экономим бюджет

    if eff_mode == "unanimous_skip":
        approve_vote = next((v for v in votes if v["decision"] == "APPROVE" or v.get("raw_decision") != "SKIP"), None)
        if approve_vote is None and len(votes) < len(req_models):
            # Второй голос не получен (сбой сети/лимита): НЕ ликвидируем позицию без полного консенсуса
            return {
                "decision": "APPROVE",
                "raw_decision": "HOLD_UNCONFIRMED",
                "confidence": 0,
                "summary": f"Подтверждение выхода не получено ({last_err[:80]}); позиция остаётся под защитой Stop-Loss.",
                "watch_keywords": [],
                "emergency_mode": False,
                "unanimous_skip": False,
                "consensus_mode": eff_mode,
                "model_used": "none (unconfirmed-exit)",
            }
        is_unanimous_skip = (
            approve_vote is None
            and len(votes) == len(req_models)
            and all(v.get("raw_decision") == "SKIP" for v in votes)
        )
        chosen = approve_vote or min(votes, key=lambda v: v["confidence"])
        final_decision = "SKIP" if is_unanimous_skip else "APPROVE"
        final_raw_decision = "SKIP" if is_unanimous_skip else str(chosen.get("raw_decision") or "APPROVE")
    else:
        skip_vote = next((v for v in votes if v["decision"] != "APPROVE"), None)
        if skip_vote is None and len(votes) < len(req_models):
            # Связь оборвалась до полного консенсуса. Принцип закрытого отказа (Fail-Closed): не входить вслепую
            return {
                "decision": "SKIP",
                "confidence": 0,
                "summary": f"Аварийный отказ (Fail-Closed): нет связи с ИИ ({last_err[:80]}). Вход строго заблокирован.",
                "watch_keywords": [],
                "emergency_mode": True,
                "model_used": "none (circuit-breaker)",
            }
        is_unanimous_skip = False
        chosen = skip_vote or min(votes, key=lambda v: v["confidence"])
        final_decision = "SKIP" if skip_vote else "APPROVE"
        final_raw_decision = chosen.get("raw_decision", final_decision)

    unique_used = list(dict.fromkeys(v["model_used"] for v in votes))
    unique_req = list(dict.fromkeys(req_models))
    unique_providers = list(dict.fromkeys(v.get("provider", "venice") for v in votes))
    result = {
        "decision": final_decision,
        "raw_decision": final_raw_decision,
        "confidence": chosen["confidence"],
        "summary": chosen["summary"],
        "watch_keywords": chosen["watch_keywords"],
        "model_used": " + ".join(unique_used),
        "model_requested": " + ".join(unique_req),
        "models_requested": req_models,
        "provider_used": " + ".join(unique_providers),
        "web_search": web_mode,
        "prompt_ver": AUDIT_PROMPT_VERSION,
        "consensus_mode": eff_mode,
        "unanimous_skip": is_unanimous_skip,
        "votes": [
            {"model": v["model_used"], "decision": v["decision"], "confidence": v["confidence"]}
            for v in votes
        ],
        "samples": len(votes),
        "audited_at": now,
        "emergency_mode": False,
    }
    cache[cache_key] = {"ts": now, "result": result}
    atomic_write_json(cache_path, cache)
    return result
