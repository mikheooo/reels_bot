import asyncio
import json
import logging
from typing import Any

from app.worker.factcheck import call_gemini_api
from app.worker.schemas import (
    BusinessCheckResult,
    Claim,
    VideoAnalysis,
)

logger = logging.getLogger(__name__)

BUSINESS_CHECK_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "offer": {
            "type": "OBJECT",
            "properties": {
                "type": {
                    "type": "STRING",
                    "enum": [
                        "tool", "service", "course", "subscription", "community",
                        "telegram_channel", "affiliate_product", "consulting",
                        "software", "lead_magnet", "audience_growth", "other"
                    ]
                },
                "description": {"type": "STRING"}
            },
            "required": ["type", "description"]
        },
        "monetization_hypothesis": {
            "type": "OBJECT",
            "properties": {
                "type": {
                    "type": "STRING",
                    "enum": [
                        "product_sales", "subscription", "affiliate", "ads",
                        "lead_generation", "paid_community", "consulting",
                        "course_sale", "saas", "audience_growth", "unknown"
                    ]
                },
                "reason": {"type": "STRING"}
            },
            "required": ["type", "reason"]
        },
        "cta": {
            "type": "OBJECT",
            "properties": {
                "detected": {"type": "BOOLEAN"},
                "type": {
                    "type": "STRING",
                    "enum": [
                        "link_click", "telegram", "website", "promo_code",
                        "registration", "purchase", "subscription", "download",
                        "none", "other"
                    ]
                },
                "destination": {"type": "STRING"},
                "action_prompt": {"type": "STRING", "nullable": True}
            },
            "required": ["detected", "type", "destination"]
        },
        "promises": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "claim": {"type": "STRING"},
                    "target_audience": {"type": "STRING", "nullable": True},
                    "expected_result": {"type": "STRING", "nullable": True},
                    "timeframe": {"type": "STRING", "nullable": True},
                    "conditions": {"type": "STRING", "nullable": True},
                    "has_concrete_metrics": {"type": "BOOLEAN"},
                    "evidence_status": {
                        "type": "STRING",
                        "enum": ["VERIFIED", "UNVERIFIED", "REFUTED", "NOT_APPLICABLE"]
                    }
                },
                "required": ["claim", "has_concrete_metrics", "evidence_status"]
            }
        },
        "missing_economics": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "item": {"type": "STRING"},
                    "status": {
                        "type": "STRING",
                        "enum": ["NOT_STATED", "REQUIRES_VERIFICATION", "PROBABLE_LIMITATION"]
                    },
                    "description": {"type": "STRING"}
                },
                "required": ["item", "status", "description"]
            }
        },
        "reproducibility": {
            "type": "OBJECT",
            "properties": {
                "level": {
                    "type": "STRING",
                    "enum": ["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
                },
                "reason": {"type": "STRING"}
            },
            "required": ["level", "reason"]
        },
        "alternatives": {
            "type": "OBJECT",
            "properties": {
                "status": {
                    "type": "STRING",
                    "enum": ["FOUND", "NOT_ENOUGH_DATA"]
                },
                "items": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"}
                }
            },
            "required": ["status", "items"]
        },
        "commercial_interest": {
            "type": "OBJECT",
            "properties": {
                "level": {
                    "type": "STRING",
                    "enum": ["NONE_DETECTED", "POSSIBLE", "CLEAR", "UNKNOWN"]
                },
                "reason": {"type": "STRING"}
            },
            "required": ["level", "reason"]
        },
        "verdict": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "enum": [
                        "EDUCATIONAL", "PRODUCT_PROMOTION", "LEAD_GENERATION",
                        "AFFILIATE_PROMOTION", "AUDIENCE_GROWTH", "MIXED", "UNCLEAR"
                    ]
                },
                "assessment": {
                    "type": "STRING",
                    "enum": [
                        "GOOD_VALUE", "POTENTIALLY_USEFUL", "MARKETING_HEAVY",
                        "INSUFFICIENT_EVIDENCE", "NOT_ENOUGH_DATA"
                    ]
                },
                "summary": {"type": "STRING"}
            },
            "required": ["category", "assessment", "summary"]
        }
    },
    "required": [
        "offer", "monetization_hypothesis", "cta", "promises",
        "missing_economics", "reproducibility", "alternatives",
        "commercial_interest", "verdict"
    ]
}


def _build_business_check_prompt(
    transcript: str,
    claims: list[Claim] | None = None,
    factcheck_analysis: VideoAnalysis | None = None,
    metadata: dict[str, Any] | None = None
) -> str:
    claims_info = []
    if factcheck_analysis and factcheck_analysis.claims:
        for c in factcheck_analysis.claims:
            status_upper = "UNVERIFIED"
            if c.status == "подтверждено":
                status_upper = "VERIFIED"
            elif c.status == "опровергнуто":
                status_upper = "REFUTED"
            elif c.status == "пропущено":
                status_upper = "NOT_APPLICABLE"
            claims_info.append(f"- Утверждение: \"{c.statement}\" | Тип: {c.claim_type} | Статус FactCheck: {c.status} ({status_upper})")
    elif claims:
        for c in claims:
            claims_info.append(f"- Утверждение: \"{c.statement}\" | Тип: {c.claim_type} | Статус FactCheck: {c.status}")

    claims_block = "\n".join(claims_info) if claims_info else "Утверждения из Fact Check отсутствуют или не извлечены."
    meta_block = json.dumps(metadata, ensure_ascii=False) if metadata else "Метаданные отсутствуют."

    prompt = f"""Ты — независимый бизнес-аналитик и эксперт по анализу коммерческих и маркетинговых механик в видео (Reels/Shorts/TikTok).
Твоя задача — проанализировать бизнес-слой видео (Business Check): что автор предлагает, как зарабатывает (или планирует), к чему призывает, какие расходы/условия скрыты и насколько результат воспроизводим.

ГЛАВНЫЙ ПРИНЦИП:
Ты отвечаешь НЕ на вопрос "Автор врёт или нет?" (за это отвечает Fact Check), а на вопрос:
"Что здесь реально полезно зрителю, что является маркетингом, какие условия скрыты, и есть ли основания считать предложение выгодным?"

ИСХОДНЫЕ ДАННЫЕ:
1. Транскрипт и текст видео:
{transcript}

2. Извлеченные утверждения и результаты проверки фактов (Fact Check):
{claims_block}

3. Метаданные (ссылки, описание):
{meta_block}

ПРАВИЛА И СТРOГИЕ ОГРАНИЧЕНИЯ:
1. OFFER (Предложение): Укажи фактическое предложение (tool, service, course, subscription, community, telegram_channel, affiliate_product, consulting, software, lead_magnet, audience_growth, другое).
2. MONETIZATION HYPOTHESIS: Это гипотеза о модели заработка (product_sales, subscription, affiliate, ads, lead_generation, paid_community, consulting, course_sale, saas, audience_growth, unknown). Не утверждай о намерениях автора как об установленном факте.
3. CTA: Определи призыв к действию (link_click, telegram, website, promo_code, registration, purchase, subscription, download, none, другое). Выделяй назначение (destination) ТОЛЬКО из реальных данных в видео. Не придумывай URL!
4. PROMISES: Раздели обещания (что, кому, результат, срок, условия). Выделяй конкретные метрики ("заработаешь $X", "экономит X%", "за X дней", "работает у всех", "без навыков").
5. MISSING ECONOMICS: Ищи скрытые или неоговоренные затраты (подписки, API, реклама, оборудование, время, аудитория, навыки, платный трафик, комиссии, человеческий труд). Используй формулировки status: NOT_STATED ("не указано"), REQUIRES_VERIFICATION ("требует проверки"), PROBABLE_LIMITATION ("вероятное ограничение"). Не утверждай то, что нельзя обосновать.
6. REPRODUCIBILITY: Оцени воспроизводимость (HIGH, MEDIUM, LOW, UNKNOWN) и объясни, что потребуется обычному человеку.
7. ALTERNATIVES: Если данных достаточно — перечисли бесплатные, дешёвые или простые альтернативы, статус "FOUND". Если данных НЕ достаточно — ставь status: "NOT_ENOUGH_DATA" и пустой список items. НЕ ПРИДУМЫВАЙ альтернатив при нехватке данных!
8. COMMERCIAL INTEREST: Уровень (NONE_DETECTED, POSSIBLE, CLEAR, UNKNOWN). Важно: Наличие ссылки или продажи НЕ является поводом называть автора мошенником.
9. VERDICT:
   - category: EDUCATIONAL, PRODUCT_PROMOTION, LEAD_GENERATION, AFFILIATE_PROMOTION, AUDIENCE_GROWTH, MIXED, UNCLEAR.
   - assessment: GOOD_VALUE, POTENTIALLY_USEFUL, MARKETING_HEAVY, INSUFFICIENT_EVIDENCE, NOT_ENOUGH_DATA.
   - summary: Чёткое резюме без воды.
"""
    return prompt


async def run_business_check(
    transcript: str,
    claims: list[Claim] | None = None,
    factcheck_analysis: VideoAnalysis | None = None,
    metadata: dict[str, Any] | None = None
) -> BusinessCheckResult:
    """Run independent Business Check analysis on Reel contents and returns structured BusinessCheckResult."""
    logger.info("Executing Business Check analysis layer...")
    prompt = _build_business_check_prompt(transcript, claims, factcheck_analysis, metadata)

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": BUSINESS_CHECK_RESPONSE_SCHEMA,
        }
    }

    last_error = None
    for attempt in range(3):
        try:
            resp_json = await call_gemini_api(payload)
            text_resp = resp_json["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text_resp)
            return BusinessCheckResult(**data)
        except Exception as e:
            logger.warning(f"Business Check attempt {attempt + 1} failed: {e}")
            last_error = e
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

    raise Exception(f"Failed to perform Business Check after retries: {last_error}")


def format_business_check_markdown(bc: BusinessCheckResult) -> str:
    """Render BusinessCheckResult as clean human-readable Markdown."""
    lines = [
        "💼 **Бизнес-анализ механики (Business Check):**\n",
        f"- **Предложение (Offer):** [{bc.offer.type}] {bc.offer.description}",
        f"- **Модель монетизации (гипотеза):** [{bc.monetization_hypothesis.type}] {bc.monetization_hypothesis.reason}",
        f"- **Призыв к действию (CTA):** {'Обнаружен' if bc.cta.detected else 'Не обнаружен'} (Тип: {bc.cta.type}, Назначение: {bc.cta.destination})",
        f"- **Коммерческий интерес:** {bc.commercial_interest.level} — {bc.commercial_interest.reason}",
        f"- **Воспроизводимость:** {bc.reproducibility.level} — {bc.reproducibility.reason}",
    ]

    if bc.promises:
        lines.append("\n📌 **Обещания и заявленные результаты:**")
        for p in bc.promises:
            metric_tag = " 📊 [Конкретные метрики]" if p.has_concrete_metrics else ""
            lines.append(f"  • {p.claim}{metric_tag} (Статус проверки: {p.evidence_status})")

    if bc.missing_economics:
        lines.append("\n💡 **Скрытые расходы и ограничения:**")
        for m in bc.missing_economics:
            lines.append(f"  • [{m.status}] {m.item}: {m.description}")

    lines.append("\n🔄 **Альтернативы:**")
    if bc.alternatives.status == "FOUND" and bc.alternatives.items:
        for alt in bc.alternatives.items:
            lines.append(f"  • {alt}")
    else:
        lines.append("  • Недостаточно данных для определения альтернатив (NOT_ENOUGH_DATA)")

    lines.append(f"\n📊 **Бизнес-вердикт:** Category: `{bc.verdict.category}` | Assessment: `{bc.verdict.assessment}`")
    lines.append(f"_{bc.verdict.summary}_")

    return "\n".join(lines)
