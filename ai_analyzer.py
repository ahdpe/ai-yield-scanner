"""Семантический и риск-анализатор рынков с подключением Venice AI (через локальный прокси Hermes или напрямую)."""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

from state_io import STATE_DIR, atomic_write_json, esc
from telegram_notifier import send_telegram_alert


AI_HEALTH_FILE = STATE_DIR / "ai_health.json"
AI_BUDGET_FILE = STATE_DIR / "ai_budget.json"
DEFAULT_AI_CACHE_FILE = STATE_DIR / "ai_audits_cache.json"

# Минимальная уверенность модели, при которой APPROVE засчитывается как одобрение
MIN_APPROVE_CONFIDENCE = 70
# Жесткий потолок платных вызовов Venice в сутки (UTC): ~0.003 DIEM за вызов → ≤0.12 DIEM/день
MAX_AI_CALLS_PER_DAY = int(os.environ.get("AI_MAX_CALLS_PER_DAY", "40"))
# Часть суточного лимита, недоступная для новых входов: остается под повторные аудиты открытых позиций
# (4 позиции Polymarket × аудит раз в 6 ч = 16 вызовов в сутки)
REAUDIT_RESERVE_CALLS = int(os.environ.get("AI_REAUDIT_RESERVE_CALLS", "16"))

SPORTS_KEYWORDS = [
    r"\bvs\.?\b", r"\bbo1\b", r"\bbo3\b", r"\bbo5\b", r"\bufc\b",
    r"\bnba\b", r"\bnfl\b", r"\bnhl\b", r"\bmlb\b", r"\blol:\b",
    r"\bdota\b", r"\bcs2\b", r"\bvalorant\b", r"\bpremier league\b",
    r"\bchampions league\b", r"\bspread:\b", r"\bover/under\b",
]

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

    is_sports = any(re.search(pat, question) for pat in SPORTS_KEYWORDS)
    has_ambiguity = any(kw in full_text for kw in AMBIGUITY_KEYWORDS)
    has_official_source = any(kw in full_text for kw in OFFICIAL_SOURCES_KEYWORDS)

    hr_change = abs(float(market.get("oneHourPriceChange") or 0.0))
    day_change = abs(float(market.get("oneDayPriceChange") or 0.0))

    score = 80
    reasons: List[str] = []

    if is_sports:
        score -= 45
        reasons.append("Спорт/Киберспорт (высокий риск резких скачков цены в лайве)")
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
        "hr_change_cents": hr_change * 100,
        "day_change_cents": day_change * 100,
        "reasons": reasons,
    }


def _consume_ai_call_budget(reserve: int = 0) -> bool:
    """Учитывает платный вызов Venice в суточном лимите. False — лимит на сегодня исчерпан.
    reserve — сколько вызовов из лимита этому вызывающему трогать нельзя (они оставлены под другие задачи)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    budget: Dict[str, Any] = {}
    if AI_BUDGET_FILE.exists():
        try:
            budget = json.loads(AI_BUDGET_FILE.read_text(encoding="utf-8"))
        except Exception:
            budget = {}
    if budget.get("date") != today:
        budget = {"date": today, "calls": 0}
    if budget["calls"] >= MAX_AI_CALLS_PER_DAY - reserve:
        return False
    budget["calls"] += 1
    atomic_write_json(AI_BUDGET_FILE, budget)
    return True


def get_ai_budget() -> Dict[str, Any]:
    """Сколько платных вызовов Venice сделано сегодня (UTC) и каков лимит."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    calls = 0
    if AI_BUDGET_FILE.exists():
        try:
            b = json.loads(AI_BUDGET_FILE.read_text(encoding="utf-8"))
            calls = int(b.get("calls", 0)) if b.get("date") == today else 0
        except Exception:
            pass
    return {"date": today, "calls": calls, "limit": MAX_AI_CALLS_PER_DAY, "reserve": REAUDIT_RESERVE_CALLS}


def entry_budget_exhausted() -> bool:
    """Исчерпана ли часть суточного лимита, доступная для новых входов (без резерва под повторные аудиты)."""
    return get_ai_budget()["calls"] >= MAX_AI_CALLS_PER_DAY - REAUDIT_RESERVE_CALLS


STRATEGY_BRIEFS = {
    "polymarket_liquidity_maker": (
        "Стратегия: ставим двусторонние лимитные ордера (bid и ask) вокруг текущей цены ради ежедневных USDC-ревардов Polymarket. "
        "Главный риск — резкий скачок цены (новость, событие в ближайшие дни), из-за которого наши ордера исполнят информированные трейдеры."
    ),
    "polymarket_smart_bonding": (
        "Стратегия: ПОКУПАЕМ исход [{outcome}] по цене {price:.4f} и держим до резолюции. "
        "Позиция получает $1.00 за акцию, если в итоге наступит именно исход [{outcome}], и $0.00 в противном случае. "
        "Оцени, насколько исход [{outcome}] действительно почти гарантирован и нет ли в правилах ловушки или риска затяжной/спорной резолюции UMA."
    ),
    "hyperliquid_funding": (
        "Стратегия: дельта-нейтральный сбор фандинга на Hyperliquid (перп-нога + хедж). "
        "Главные риски — разворот знака фандинга, ликвидация перп-ноги при резком движении цены, недоступность хеджа для этого актива."
    ),
}


def _build_audit_prompt(question: str, description: str, price_yes: float, strategy_type: str, outcome_label: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    brief = STRATEGY_BRIEFS.get(strategy_type, f"Стратегия: {strategy_type}.").format(outcome=outcome_label, price=price_yes)
    if strategy_type == "hyperliquid_funding":
        market_block = f"Актив: {question}\nДанные: {description[:1200]}\n"
    else:
        market_block = (
            f"Вопрос рынка: {question}\n"
            f"Рассматриваемый исход: [{outcome_label}], текущая цена ${price_yes:.4f}\n"
            f"Правила резолюции: {description[:1200]}\n"
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


def _venice_single_audit(prompt: str, api_key: str, base_url: str, model: str) -> Dict[str, Any]:
    """Один вызов Venice. Бросает исключение при сетевой ошибке или неразборчивом ответе."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 900,
        "reasoning_effort": "low",
        "temperature": 0.1,
        # Без системного промпта Venice входных токенов в ~6 раз меньше (≈400 вместо ≈2500)
        "venice_parameters": {"include_venice_system_prompt": False},
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
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    latency = time.time() - t0
    raw_text = (data["choices"][0]["message"].get("content") or "").strip()
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in: {raw_text[:80]}")
    parsed = json.loads(match.group(0))

    # Строгий разбор (Fail-Closed): всё, что не явный APPROVE с достаточной уверенностью, — SKIP
    decision = str(parsed.get("decision", "")).strip().upper()
    try:
        confidence = max(0, min(100, int(parsed.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    if decision != "APPROVE" or confidence < MIN_APPROVE_CONFIDENCE:
        decision = "SKIP"
    return {
        "decision": decision,
        "confidence": confidence,
        "summary": str(parsed.get("summary", "")),
        "watch_keywords": parsed.get("watch_keywords", []) if isinstance(parsed.get("watch_keywords"), list) else [],
        "model_used": data.get("model", model),
        "model_requested": model,
        "latency_sec": round(latency, 2),
    }


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
) -> Dict[str, Any]:
    """Вызывает Venice AI для анализа правил рынка и принятия решения APPROVE / SKIP.

    samples > 1 — консенсус: APPROVE только если все выборки одобрили (сглаживает недетерминизм модели).
    В аварийном режиме, при сбое сети или исчерпании суточного лимита строго соблюдается Fail-Closed (вход блокируется).
    Поле "audited_at" меняется только при свежем аудите (по нему можно отличить кэш от нового вердикта).
    budget_reserve — сколько вызовов суточного лимита не трогать (для новых входов: резерв под повторные аудиты)."""
    cache_path = Path(cache_file) if cache_file else DEFAULT_AI_CACHE_FILE
    cache: Dict[str, Any] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    cache_key = f"{strategy_type}::{outcome_label}::{question[:120]}"
    now = time.time()
    if cache_key in cache and now - cache[cache_key].get("ts", 0) < cache_ttl_sec:
        return cache[cache_key]["result"]

    api_key = load_venice_api_key()
    if not api_key:
        update_ai_health(success=False, error="Ключ VENICE_API_KEY не найден локально")
        return {
            "decision": "SKIP",
            "confidence": 0,
            "summary": "Аварийная блокировка (Fail-Closed): VENICE_API_KEY не найден.",
            "watch_keywords": [],
            "emergency_mode": True,
            "model_used": "none",
        }

    base_url = os.environ.get("VENICE_BASE_URL", "http://127.0.0.1:8787/api/v1")
    model = os.environ.get("VENICE_MODEL", "gemini-4-argon")
    prompt = _build_audit_prompt(question, description, price_yes, strategy_type, outcome_label)

    votes: List[Dict[str, Any]] = []
    last_err = ""
    for _ in range(max(1, samples)):
        if not _consume_ai_call_budget(budget_reserve):
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
        try:
            vote = _venice_single_audit(prompt, api_key, base_url, model)
        except Exception as exc:
            last_err = str(exc)
            update_ai_health(success=False, error=last_err, latency=0.0)
            break
        update_ai_health(success=True, latency=vote["latency_sec"])
        votes.append(vote)
        if vote["decision"] != "APPROVE":
            break  # одного SKIP достаточно — экономим бюджет

    skip_vote = next((v for v in votes if v["decision"] != "APPROVE"), None)
    if skip_vote is None and len(votes) < max(1, samples):
        # Связь оборвалась до полного консенсуса. Принцип закрытого отказа (Fail-Closed): не входить вслепую
        return {
            "decision": "SKIP",
            "confidence": 0,
            "summary": f"Аварийный отказ (Fail-Closed): нет связи с Venice AI ({last_err[:80]}). Вход строго заблокирован.",
            "watch_keywords": [],
            "emergency_mode": True,
            "model_used": "none (circuit-breaker)",
        }

    chosen = skip_vote or min(votes, key=lambda v: v["confidence"])
    result = {
        "decision": "SKIP" if skip_vote else "APPROVE",
        "confidence": chosen["confidence"],
        "summary": chosen["summary"],
        "watch_keywords": chosen["watch_keywords"],
        "model_used": chosen["model_used"],
        "model_requested": model,
        "samples": len(votes),
        "audited_at": now,
        "emergency_mode": False,
    }
    cache[cache_key] = {"ts": now, "result": result}
    atomic_write_json(cache_path, cache)
    return result
