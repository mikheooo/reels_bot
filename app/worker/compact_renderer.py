"""Deterministic compact Telegram rendering for routed analysis."""

from app.worker.content_router import RouterDecision
from app.worker.specialized_analysis import SpecializedAnalysis


def _clean(value: str, limit: int = 700) -> str:
    text = " ".join((value or "Неизвестно").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_compact_analysis(route: RouterDecision, result: SpecializedAnalysis) -> str:
    labels = ", ".join(
        f"{item.label} {item.confidence:.0%}" for item in route.labels[:3]
    )
    intents = ", ".join(
        f"{item.intent} {item.confidence:.0%}" for item in route.intents[:2]
    )
    risk_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}[route.risk]
    return "\n\n".join(
        [
            f"**Вердикт:** {_clean(result.verdict, 500)}",
            f"🧠 **Что это?** {_clean(result.what_it_is)}",
            f"🎯 **Зачем это знать?** {_clean(result.why_it_matters)}",
            f"✅ **Насколько это правда?** {_clean(result.truth_assessment)}",
            f"👤 **Актуально ли тебе?** {_clean(result.relevance)}",
            f"➡️ **Что делать?** {_clean(result.next_action)}",
            f"{risk_icon} `{route.risk}` · `{labels}` · `{intents}`",
        ]
    )


def build_detail_sections(
    result: SpecializedAnalysis,
    fact_check_text: str | None,
    business_check_text: str | None,
    legacy_structured_text: str | None,
) -> dict[str, str]:
    details: dict[str, str] = {}
    if fact_check_text:
        details["fact"] = fact_check_text
    if result.technical_details:
        details["tech"] = result.technical_details
    elif legacy_structured_text:
        details["tech"] = legacy_structured_text
    if business_check_text:
        details["business"] = business_check_text
    if result.type_specific_details:
        details["type"] = result.type_specific_details
    details["relevance"] = "\n".join(
        [
            f"Статус: {result.personal_relevance.status}",
            f"Нужно: {result.personal_relevance.needed}",
            result.personal_relevance.explanation,
            *[f"- {item}" for item in result.personal_relevance.evidence],
        ]
    )
    return details
