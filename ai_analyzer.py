"""Семантический и риск-анализатор рынков с подключением Venice AI (через локальный прокси Hermes или напрямую)."""

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, Any, List


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


def call_venice_market_audit(
    question: str,
    description: str,
    price_yes: float,
    strategy_type: str = "liquidity_rewards",
    outcome_label: str = "YES",
    cache_file: str = "state/ai_audits_cache.json",
) -> Dict[str, Any]:
    """Вызывает Venice AI (модель gemini-3-8-flash через локальный прокси Hermes или api.venice.ai)
    для анализа правил рынка и принятия решения APPROVE / SKIP."""
    cache_path = Path(cache_file)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache: Dict[str, Any] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    cache_key = f"{strategy_type}::{outcome_label}::{question[:120]}"
    now = time.time()
    # Кэшируем аудит правил на 3 часа (бюджет 0.3 DIEM/день), чтобы оперативно учитывать новости
    if cache_key in cache and now - cache[cache_key].get("ts", 0) < 10800:
        return cache[cache_key]["result"]

    api_key = load_venice_api_key()
    if not api_key:
        return {
            "decision": "APPROVE",
            "confidence": 75,
            "summary": "Эвристический допуск (ключ VENICE_API_KEY не найден локально).",
            "watch_keywords": [],
        }

    base_url = os.environ.get("VENICE_BASE_URL", "http://127.0.0.1:8787/api/v1")
    model = os.environ.get("VENICE_MODEL", "gemini-4-argon")

    prompt = (
        f"Ты — количественный риск-менеджер алгоритмического фонда на Polymarket.\n"
        f"Текущая дата: 25 октября 2026 года.\n"
        f"Тип стратегии: {strategy_type} (если polymarket_liquidity_maker — ставим двусторонние лимитки со спредом ради ежедневных USDC-ревардов; "
        f"если polymarket_smart_bonding — покупаем исход [{outcome_label}] по цене {price_yes:.4f} для удержания до выплаты $1.00).\n"
        f"Вопрос рынка: {question}\n"
        f"Рассматриваемый исход: [{outcome_label}] по цене ${price_yes:.4f}\n"
        f"Правила резолюции: {description[:1200]}\n\n"
        f"Ответь СТРОГО в формате JSON без markdown-обертки:\n"
        f'{{"decision": "APPROVE" или "SKIP", "confidence": число_0_100, '
        f'"summary": "краткое обоснование на русском (1-2 предложения: безопасно ли заходить сейчас или есть риск скачка/ловушки в правилах)", '
        f'"watch_keywords": ["слово1", "слово2"]}}'
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 900,
        "reasoning_effort": "low",
        "temperature": 0.1,
    }

    url = f"{base_url.rstrip('/')}/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "AI-Yield-Scanner/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_text = (data["choices"][0]["message"].get("content") or "").strip()
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if not match:
                raise ValueError(f"No JSON found in: {raw_text[:80]}")
            parsed = json.loads(match.group(0))
            result = {
                "decision": str(parsed.get("decision", "APPROVE")).upper(),
                "confidence": int(parsed.get("confidence", 80)),
                "summary": parsed.get("summary", ""),
                "watch_keywords": parsed.get("watch_keywords", []),
                "model_used": data.get("model", model),
            }
            cache[cache_key] = {"ts": now, "result": result}
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
    except Exception as exc:
        last_err = str(exc)

    return {
        "decision": "APPROVE",
        "confidence": 70,
        "summary": f"Резервный эвристический фильтр (ответ прокси: {last_err[:80]})",
        "watch_keywords": [],
    }
