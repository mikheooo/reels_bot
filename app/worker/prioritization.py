import json
import logging

from app.core.config import settings
from app.worker.factcheck import call_gemini_api
from app.worker.schemas import PriorityScore

logger = logging.getLogger(__name__)

WEIGHT_IMPORTANCE = 0.25
WEIGHT_VIRALITY = 0.25
WEIGHT_NOVELTY = 0.15
WEIGHT_VIEWS_POTENTIAL = 0.15
WEIGHT_AUDIENCE_VALUE = 0.20

WEIGHTS = {
    "importance": WEIGHT_IMPORTANCE,
    "virality": WEIGHT_VIRALITY,
    "novelty": WEIGHT_NOVELTY,
    "views_potential": WEIGHT_VIEWS_POTENTIAL,
    "audience_value": WEIGHT_AUDIENCE_VALUE,
}

DEFAULT_PUBLISH_THRESHOLD = 0.6

SCORING_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "importance": {"type": "NUMBER"},
        "virality": {"type": "NUMBER"},
        "novelty": {"type": "NUMBER"},
        "views_potential": {"type": "NUMBER"},
        "audience_value": {"type": "NUMBER"},
        "reasons": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["importance", "virality", "novelty", "views_potential", "audience_value", "reasons"],
}


def decide_publish(scores: dict, threshold: float) -> tuple[bool, float]:
    overall = sum(scores.get(key, 0.0) * weight for key, weight in WEIGHTS.items())
    return overall >= threshold, overall


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[len("json"):].lstrip()
    return json.loads(text)


async def score_content(transcript: str, analysis_summary: str | None = None) -> PriorityScore:
    threshold = getattr(settings, "publish_threshold", DEFAULT_PUBLISH_THRESHOLD)

    summary_block = f"\nРезультаты предыдущего анализа:\n{analysis_summary}" if analysis_summary else ""
    prompt = f"""
Оцени контент ролика по 5 критериям для решения о публикации.

Каждый критерий оцени как число от 0.0 до 1.0:
- importance: насколько тема важна и актуальна для аудитории
- virality: насколько ролик способен распространяться (кликабельность, эмоции, спорность)
- novelty: насколько информация новая / малоизвестная
- views_potential: вероятность того, что ролик наберёт просмотры
- audience_value: практическая ценность для зрителя

Также дай короткий список reasons (на русском) — почему такие оценки.

СТРОГО: возвращай только JSON по схеме, числа строго в диапазоне 0.0-1.0.

Транскрипт:
{transcript}
{summary_block}
"""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": SCORING_SCHEMA,
        },
    }

    resp_json = await call_gemini_api(payload)
    data = _parse_json_response(resp_json["candidates"][0]["content"]["parts"][0]["text"])

    scores = {key: float(data[key]) for key in WEIGHTS}
    publish, overall = decide_publish(scores, threshold)
    result = PriorityScore(overall=overall, publish=publish, reasons=list(data.get("reasons", [])), **scores)
    logger.info(f"Priority scoring done: overall={overall:.3f}, publish={publish} (threshold={threshold})")
    return result
