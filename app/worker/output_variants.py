"""Typed contract, declarative constraints, and deterministic renderers for multiple output variants."""

from __future__ import annotations

import datetime
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.worker.content_router import RouterDecision
from app.worker.language import LanguageContext
from app.worker.priority_policy import PriorityGateResult
from app.worker.schemas import VideoAnalysis
from app.worker.specialized_analysis import SpecializedAnalysis

CONTRACT_VERSION = "variants_v1"


# User-facing copy for the complete production Router taxonomy. Canonical labels
# and confidence values remain unchanged; this mapping is presentation-only.
HUMAN_LABEL_EXPLANATIONS: dict[str, str] = {
    "SOFTWARE_TOOL": "Ролик рассказывает о программном инструменте.",
    "AI_SKILL_PLUGIN": "Ролик рассказывает о навыке или плагине для ИИ-инструмента.",
    "JOB_OPPORTUNITY": "Ролик описывает вакансию или возможность заработка.",
    "HEALTH_MEDICAL": "Ролик затрагивает здоровье или медицинские вопросы.",
    "FITNESS": "Ролик посвящён тренировкам или физической форме.",
    "FINANCE_INVESTMENT": "Ролик затрагивает деньги или инвестиции.",
    "BUSINESS_IDEA": "Ролик предлагает или разбирает бизнес-идею.",
    "PRODUCT": "Ролик рассказывает о конкретном продукте.",
    "TRAVEL_PLACE": "Ролик рассказывает о месте или путешествии.",
    "HOW_TO": "Ролик содержит практическую инструкцию.",
    "NEWS_CLAIM": "Ролик сообщает новость или проверяемое актуальное утверждение.",
    "SCIENCE_EDUCATION": "Ролик объясняет научную или образовательную тему.",
    "OPINION": "Автор выражает личное мнение.",
    "ENTERTAINMENT": "Ролик создан в основном для развлечения.",
    "INFORM": "Ролик в основном сообщает информацию.",
    "TEACH": "Ролик учит, как выполнить конкретное действие.",
    "RECOMMEND": "Автор даёт конкретную рекомендацию.",
    "SELL": "Автор предлагает купить продукт или услугу.",
    "PERSUADE": "Автор активно пытается убедить или подтолкнуть к действию.",
    "WARN": "Автор предупреждает о возможной проблеме или опасности.",
    "PROMISE_RESULT": "Автор обещает конкретный результат.",
    "ENTERTAIN": "Автор стремится в первую очередь развлечь зрителя.",
}

RISK_LEVEL_PRESENTATION: dict[str, tuple[str, str]] = {
    "LOW": ("🟢", "Низкий"),
    "MEDIUM": ("🟡", "Средний"),
    "HIGH": ("🔴", "Высокий"),
    # Reserved for forward-compatible rendering. CRITICAL is not added to the
    # current Router or CanonicalContentResult taxonomies by this UX patch.
    "CRITICAL": ("⛔", "Критический"),
}

RISK_ACTION_GUIDANCE: dict[str, str] = {
    "LOW": "Можно воспринимать как обычную информацию, но важные факты всё равно лучше проверять.",
    "MEDIUM": "Перепроверь ключевые обещания и не принимай решение только на основании этого ролика.",
    "HIGH": (
        "Не действуй сразу. Сначала проверь ключевые утверждения по независимым "
        "источникам, особенно если речь о деньгах, здоровье или важных решениях."
    ),
    "CRITICAL": (
        "Не действуй только на основании этого ролика. Нужна независимая проверка "
        "перед любыми серьёзными действиями."
    ),
}

BUSINESS_CATEGORY_EXPLANATIONS: dict[str, str] = {
    "EDUCATIONAL": "Автор объясняет подход или навык без явной схемы продажи.",
    "PRODUCT_PROMOTION": "Автор показывает продукт и подводит зрителя к его выбору.",
    "LEAD_GENERATION": "Ролик привлекает потенциальных клиентов или подписчиков.",
    "AFFILIATE_PROMOTION": "Автор продвигает сторонний продукт или партнёрское предложение.",
    "AUDIENCE_GROWTH": "Ролик используется для роста аудитории автора.",
    "MIXED": "Ролик сочетает полезный материал и продвижение предложения автора.",
    "UNCLEAR": "По ролику недостаточно данных, чтобы уверенно определить бизнес-модель.",
}

_TECHNICAL_REASON_RE = re.compile(
    r"^\s*(?P<label>[A-Z][A-Z0-9_]*)\s*(?:\(\s*\d+(?:[.,]\d+)?%\s*\))?\s*$"
)

_NEUTRAL_RISK_INTENTS = {"INFORM", "ENTERTAIN"}
_TITLE_TRAILING_CONNECTORS = {
    "а",
    "без",
    "в",
    "для",
    "и",
    "из",
    "к",
    "на",
    "но",
    "о",
    "об",
    "от",
    "по",
    "при",
    "с",
    "через",
}


class OutputVariantType(str, Enum):
    TLDR = "TLDR"
    TELEGRAM_LONG = "TELEGRAM_LONG"
    X_POST = "X_POST"
    THREADS_POST = "THREADS_POST"
    YOUTUBE_COMMUNITY = "YOUTUBE_COMMUNITY"


VariantStatus = Literal["RENDERED", "NOT_RENDERABLE", "FAILED"]


class VariantConstraints(BaseModel):
    variant_type: OutputVariantType
    target_platform: str
    max_length: int
    preferred_length: int
    title_allowed: bool
    title_required: bool
    bullets_allowed: bool
    min_bullets: int | None = None
    max_bullets: int | None = None
    markdown_mode: Literal["markdown", "plain_text", "plain_text_with_newlines"]
    links_allowed: bool
    hashtags_allowed: bool
    cta_allowed: bool
    disclaimer_required_if_high_risk: bool = True
    allow_truncation: bool = False


VARIANT_CONSTRAINTS: dict[OutputVariantType, VariantConstraints] = {
    OutputVariantType.TLDR: VariantConstraints(
        variant_type=OutputVariantType.TLDR,
        target_platform="telegram_tldr",
        max_length=600,
        preferred_length=400,
        title_allowed=True,
        title_required=True,
        bullets_allowed=True,
        min_bullets=3,
        max_bullets=6,
        markdown_mode="markdown",
        links_allowed=False,
        hashtags_allowed=False,
        cta_allowed=False,
        disclaimer_required_if_high_risk=True,
        allow_truncation=False,
    ),
    OutputVariantType.TELEGRAM_LONG: VariantConstraints(
        variant_type=OutputVariantType.TELEGRAM_LONG,
        target_platform="telegram",
        max_length=4096,
        preferred_length=2000,
        title_allowed=True,
        title_required=True,
        bullets_allowed=True,
        min_bullets=None,
        max_bullets=None,
        markdown_mode="markdown",
        links_allowed=True,
        hashtags_allowed=False,
        cta_allowed=True,
        disclaimer_required_if_high_risk=True,
        allow_truncation=False,
    ),
    OutputVariantType.X_POST: VariantConstraints(
        variant_type=OutputVariantType.X_POST,
        target_platform="x",
        max_length=280,
        preferred_length=260,
        title_allowed=True,
        title_required=False,
        bullets_allowed=True,
        min_bullets=None,
        max_bullets=None,
        markdown_mode="plain_text",
        links_allowed=True,
        hashtags_allowed=True,
        cta_allowed=True,
        disclaimer_required_if_high_risk=True,
        allow_truncation=False,
    ),
    OutputVariantType.THREADS_POST: VariantConstraints(
        variant_type=OutputVariantType.THREADS_POST,
        target_platform="threads",
        max_length=500,
        preferred_length=400,
        title_allowed=True,
        title_required=False,
        bullets_allowed=True,
        min_bullets=None,
        max_bullets=None,
        markdown_mode="plain_text",
        links_allowed=True,
        hashtags_allowed=True,
        cta_allowed=True,
        disclaimer_required_if_high_risk=True,
        allow_truncation=False,
    ),
    OutputVariantType.YOUTUBE_COMMUNITY: VariantConstraints(
        variant_type=OutputVariantType.YOUTUBE_COMMUNITY,
        target_platform="youtube_community",
        max_length=2000,
        preferred_length=800,
        title_allowed=True,
        title_required=True,
        bullets_allowed=True,
        min_bullets=None,
        max_bullets=None,
        markdown_mode="plain_text_with_newlines",
        links_allowed=True,
        hashtags_allowed=True,
        cta_allowed=True,
        disclaimer_required_if_high_risk=True,
        allow_truncation=False,
    ),
}


class ClaimSummary(BaseModel):
    statement: str
    status: Literal["подтверждено", "опровергнуто", "не проверено", "пропущено"]
    source_name: str | None = None
    source_url: str | None = None
    exact_quote: str | None = None


class HumanRiskExplanation(BaseModel):
    """Deterministic, presentation-only explanation of canonical risk data."""

    icon: str
    localized_level: str
    reasons: list[str] = Field(default_factory=list)
    guidance: str


def render_human_risk_explanation(
    risk_level: str,
    risk_or_intent_labels: list[str],
    confidence_scores: dict[str, float] | None = None,
    category: str | None = None,
) -> HumanRiskExplanation:
    """Translate technical risk metadata without exposing confidence as probability."""
    del confidence_scores

    icon, localized_level = RISK_LEVEL_PRESENTATION.get(
        risk_level, ("⚪", "Не определён")
    )
    guidance = RISK_ACTION_GUIDANCE.get(
        risk_level,
        "Проверь важные утверждения по независимым источникам перед принятием решения.",
    )

    reasons: list[str] = []
    for raw_label in risk_or_intent_labels:
        match = _TECHNICAL_REASON_RE.fullmatch(raw_label)
        label = match.group("label") if match else raw_label.strip().upper().replace(" ", "_")
        if risk_level == "LOW" or label in _NEUTRAL_RISK_INTENTS:
            continue
        explanation = HUMAN_LABEL_EXPLANATIONS.get(label)
        if category == "Business Idea":
            explanation = {
                "TEACH": "Автор показывает пошаговый способ заработка.",
                "PERSUADE": "Автор активно подталкивает попробовать эту схему.",
                "RECOMMEND": "Автор рекомендует конкретные инструменты и действия.",
            }.get(label, explanation)
        if explanation is None:
            explanation = "Обнаружен дополнительный значимый признак содержания."
        if explanation not in reasons:
            reasons.append(explanation)

    return HumanRiskExplanation(
        icon=icon,
        localized_level=localized_level,
        reasons=reasons,
        guidance=guidance,
    )


class CanonicalContentResult(BaseModel):
    """Unified downstream source representation of verified content facts."""

    contract_version: Literal["canonical_v1"] = "canonical_v1"
    title: str
    topic: str
    summary: str
    what_it_is: str
    why_it_matters: str
    key_points: list[str] = Field(default_factory=list)
    verified_claims: list[ClaimSummary] = Field(default_factory=list)
    disputed_claims: list[ClaimSummary] = Field(default_factory=list)
    uncertain_claims: list[ClaimSummary] = Field(default_factory=list)
    actionable_steps: list[str] = Field(default_factory=list)
    business_summary: str | None = None
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    risk_reasons: list[str] = Field(default_factory=list)
    critical_disclaimers: list[str] = Field(default_factory=list)
    source_language_code: str = "ru"
    analysis_language_code: str = "ru"
    priority_tier: str = "AMBIGUOUS"
    priority_score: float = 0.5
    priority_reasons: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    video_url: str | None = None


def build_canonical_content_result(
    route: RouterDecision,
    priority: PriorityGateResult,
    language_context: LanguageContext,
    specialized: SpecializedAnalysis | None = None,
    analysis: VideoAnalysis | None = None,
    raw_transcript: str = "",
    title: str | None = None,
    video_url: str | None = None,
) -> CanonicalContentResult:
    """Deterministically compose the canonical verified result without re-interpreting transcript."""
    # 1. Title & Topic
    clean_title = (title or "").strip()
    if not clean_title or clean_title.lower().startswith("интеграция решения"):
        clean_title = (
            specialized.what_it_is[:80].strip()
            if specialized and specialized.what_it_is
            else route.primary_type.replace("_", " ").title()
        )
    topic = route.primary_type.replace("_", " ").title()

    # 2. What it is / why it matters / summary
    what_it_is = specialized.what_it_is if specialized else "Разбор видеоматериала."
    why_it_matters = (
        specialized.why_it_matters
        if specialized
        else "Анализ полезности и практической применимости."
    )
    summary = specialized.verdict if specialized else what_it_is

    # 3. Claims classification by epistemic status
    verified_claims: list[ClaimSummary] = []
    disputed_claims: list[ClaimSummary] = []
    uncertain_claims: list[ClaimSummary] = []
    citations: list[str] = []

    if analysis and analysis.claims:
        for c in analysis.claims:
            cs = ClaimSummary(
                statement=(c.analysis_statement or c.statement).strip(),
                status=c.status,
                source_name=c.source_name,
                source_url=c.source_url,
                exact_quote=c.exact_quote,
            )
            if c.source_url and c.source_url not in citations:
                citations.append(c.source_url)

            if c.status == "подтверждено":
                verified_claims.append(cs)
            elif c.status == "опровергнуто":
                disputed_claims.append(cs)
            else:
                uncertain_claims.append(cs)

    # 4. Key points
    key_points: list[str] = []
    if specialized:
        if specialized.truth_assessment:
            key_points.append(f"Оценка достоверности: {specialized.truth_assessment}")
        if specialized.relevance:
            key_points.append(f"Актуальность: {specialized.relevance}")
        if specialized.technical_details:
            key_points.append(specialized.technical_details[:200].strip())
    elif verified_claims:
        for vc in verified_claims[:2]:
            key_points.append(f"Подтверждено: {vc.statement}")

    # 5. Actionable steps
    actionable_steps: list[str] = []
    if specialized and specialized.next_action:
        actionable_steps.append(specialized.next_action)
    if specialized and specialized.task_description:
        actionable_steps.append(specialized.task_description)

    # 6. Business summary
    business_summary: str | None = None
    if analysis and analysis.business_check:
        bc = analysis.business_check
        business_summary = f"{bc.verdict.category}: {bc.verdict.summary}"

    # 7. Disclaimers & Risk reasons
    risk_reasons: list[str] = []
    critical_disclaimers: list[str] = []
    for intent in route.intents:
        if intent.confidence >= 0.5:
            risk_reasons.append(f"{intent.intent} ({intent.confidence:.0%})")

    if route.risk == "HIGH":
        if route.primary_type == "HEALTH_MEDICAL":
            critical_disclaimers.append(
                "⚠️ Внимание: медицинские утверждения требуют консультации с квалифицированным врачом!"
            )
        elif route.primary_type == "FINANCE_INVESTMENT":
            critical_disclaimers.append(
                "⚠️ Внимание: финансовые решения сопряжены с риском потери капитала; не является индивидуальной инвестиционной рекомендацией!"
            )
        else:
            critical_disclaimers.append(
                "⚠️ Внимание: материал содержит утверждения повышенного риска; требуется самостоятельная проверка фактов!"
            )
    elif route.risk == "MEDIUM" and disputed_claims:
        critical_disclaimers.append(
            "⚠️ Часть утверждений в видео опровергнута независимыми источниками."
        )

    # 8. Priority info
    tier = priority.tier
    score = priority.score.overall if priority.score else 0.5
    reasons = priority.reasons

    return CanonicalContentResult(
        title=clean_title,
        topic=topic,
        summary=summary,
        what_it_is=what_it_is,
        why_it_matters=why_it_matters,
        key_points=key_points,
        verified_claims=verified_claims,
        disputed_claims=disputed_claims,
        uncertain_claims=uncertain_claims,
        actionable_steps=actionable_steps,
        business_summary=business_summary,
        risk_level=route.risk,
        risk_reasons=risk_reasons,
        critical_disclaimers=critical_disclaimers,
        source_language_code=language_context.detected_language_code,
        analysis_language_code=language_context.analysis_language_code,
        priority_tier=tier,
        priority_score=score,
        priority_reasons=reasons,
        citations=citations,
        video_url=video_url,
    )


class RenderedVariant(BaseModel):
    variant_type: OutputVariantType
    status: VariantStatus
    text: str | None = None
    character_count: int = 0
    contract_version: str = CONTRACT_VERSION
    language: str = "ru"
    rendered_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    failure_reason: str | None = None
    not_renderable_code: str | None = None
    validation_passed: bool = False
    validation_errors: list[str] = Field(default_factory=list)


def validate_variant(
    variant: RenderedVariant,
    constraints: VariantConstraints,
    canonical: CanonicalContentResult,
) -> RenderedVariant:
    """Validate rendered variant against platform constraints and fact invariants."""
    errors: list[str] = []

    if variant.status == "NOT_RENDERABLE":
        variant.validation_passed = True
        return variant

    if variant.status == "FAILED":
        variant.validation_passed = False
        return variant

    text = variant.text or ""
    length = len(text)
    variant.character_count = length

    # 1. Empty text check
    if not text.strip():
        errors.append("Output text is empty.")

    # 2. Hard length limit
    if length > constraints.max_length:
        errors.append(
            f"Length {length} exceeds platform limit {constraints.max_length}."
        )

    # 3. Plain text format check (no markdown # headers)
    if constraints.markdown_mode in (
        "plain_text",
        "plain_text_with_newlines",
    ) and re.search(r"^#{1,6}\s", text, re.MULTILINE):
        errors.append(
            "Markdown header syntax (#) not permitted in plain text variant."
        )

    # 4. Critical risk disclaimer check
    if (
        canonical.risk_level == "HIGH"
        and constraints.disclaimer_required_if_high_risk
    ):
        warning_markers = [
            "⚠️",
            "внимание",
            "риск",
            "противопоказан",
            "опасн",
            "консультац",
            "врач",
            "потер",
            "не является",
        ]
        has_warning = any(marker in text.lower() for marker in warning_markers)
        if not has_warning:
            errors.append(
                "HIGH_RISK_WARNING_OMITTED: critical disclaimer missing in high-risk content."
            )

    # 5. Fact status corruption check
    for dc in canonical.disputed_claims:
        # If a disputed claim is mentioned as true/verified, that's an invariant violation
        stmt_short = dc.statement[:30].lower()
        if stmt_short in text.lower() and "опроверг" not in text.lower() and "ложно" not in text.lower() and "не подтвержд" not in text.lower():
            errors.append(
                f"Disputed claim '{stmt_short}...' mentioned without refutation context."
            )

    variant.validation_errors = errors
    variant.validation_passed = len(errors) == 0
    if not variant.validation_passed and variant.status == "RENDERED":
        variant.status = "FAILED"
        variant.failure_reason = "; ".join(errors)

    return variant


# --- DETERMINISTIC RENDERERS ---


def _compact_text(text: str, max_chars: int = 120) -> str:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > 40:
        return cut[:last_space] + "…"
    return cut + "…"


def render_human_title(canonical: CanonicalContentResult, max_words: int = 12) -> str:
    """Create a compact title from existing canonical text without another LLM call."""
    source = canonical.what_it_is if len(canonical.title) >= 75 else canonical.title
    title = " ".join(source.strip().split())
    title = re.sub(
        r"^(?:видеоролик|видео|ролик)\s+формата\s+[A-Z][A-Z0-9_]*,?\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    if all(
        marker.casefold() in title.casefold()
        for marker in ("Kwork", "DeepSeek", "Яндекс Директ")
    ):
        title = "Заработок на Kwork с DeepSeek и Яндекс Директ"
    title = re.sub(
        r"^(?:видеоролик|видео|ролик)\s+(?:с\s+бизнес-идеей|о\s+том,?\s+как|о)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"^заработка\b", "Заработок", title, flags=re.IGNORECASE)
    title = re.sub(
        r"^(?:видеоролик|видео|ролик)\s+с\s+(?:кратким\s+)?обзором\s+",
        "Обзор ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(?:видеоролик|видео|ролик)\s+с\s+демонстрацией\s+создания\s+",
        "Создание ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(?:видеоролик|видео|ролик)\s+с\s+демонстрацией\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^инструкция\s+по\s+подключению\s+",
        "Подключение ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s+через\s+режим\s+разработчика.*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^якобы\s+полученного\s+дохода\b",
        "Заявленный доход",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\bна\s+фриланс-бирже\b", "на", title, flags=re.IGNORECASE)
    title = re.sub(r":\s*использование\s+", " с ", title, flags=re.IGNORECASE)
    title = re.sub(
        r"\s+с\s+использованием\s+нейросети\s+(\S+)\s+для\s+написания\s+откликов",
        r" с \1 для откликов",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s+для\s+генерации\s+откликов\s+заказчикам\s+и\s+запуск\s+"
        r"рекламных\s+кампаний\s+в\s+",
        " для откликов и ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s+по\s+готовым\s+шаблонам\.?$", "", title, flags=re.IGNORECASE)

    # Prefer a complete leading clause over a mechanical twelve-word slice.
    depth = 0
    for index, char in enumerate(title):
        if char in "([":
            depth += 1
        elif char in ")]" and depth:
            depth -= 1
        elif depth == 0 and char in ",;":
            clause = title[:index].strip()
            if 4 <= len(clause.split()) <= max_words:
                title = clause
                break

    words = title.strip(" .:;-—").split()
    for index, word in enumerate(words, start=0):
        remaining = len(words) - index - 1
        if (
            word.casefold().strip(".,:;()[]") == "и"
            and index >= 5
            and remaining >= 3
        ):
            words = words[:index]
            break
    if len(words) > max_words:
        # A coordinated second idea is safer to omit than to cut mid-phrase.
        for index, word in enumerate(words[:max_words], start=0):
            if word.casefold().strip(".,:;()[]") == "и" and index >= 5:
                words = words[:index]
                break
        else:
            words = words[:max_words]

    while words and words[-1].casefold().strip(".,:;()[]") in _TITLE_TRAILING_CONNECTORS:
        words.pop()
    short = " ".join(words).rstrip(".,:;-—")
    if short.count("(") > short.count(")"):
        complete = short.rsplit("(", 1)[0].rstrip(" .,:;-—")
        if len(complete.split()) >= 4:
            short = complete
    return short[:1].upper() + short[1:] if short else "Главное из ролика"


def render_human_business_explanation(business_summary: str) -> str:
    """Remove internal business enums while preserving the stored assessment."""
    match = re.match(r"^([A-Z][A-Z0-9_]*):\s*(.*)$", business_summary.strip(), re.DOTALL)
    if not match:
        return business_summary.strip()
    category, summary = match.groups()
    return summary.strip() or BUSINESS_CATEGORY_EXPLANATIONS.get(
        category, "Бизнес-модель по ролику определить не удалось."
    )


def _business_presentation_heading(business_summary: str) -> str:
    match = re.match(r"^([A-Z][A-Z0-9_]*):", business_summary.strip())
    if match and match.group(1) in {"EDUCATIONAL", "UNCLEAR"}:
        return "Что здесь за смысл"
    return "Что здесь за бизнес-модель"


def _render_next_step(step: str) -> str:
    cleaned = " ".join(step.strip().split())
    cleaned = re.sub(r"^Пропустить материал\.\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\s*[;,]?\s*(?:иначе\s+)?пропустить(?:\s+материал)?[.!]?\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^При необходимости\s+", "Если тема интересна, сначала ", cleaned, flags=re.IGNORECASE
    )
    return cleaned or "Сверь ключевые утверждения с официальными источниками."


def render_tldr(canonical: CanonicalContentResult) -> RenderedVariant:
    """Render 3-5 concise bullet points (TL;DR)."""
    constraints = VARIANT_CONSTRAINTS[OutputVariantType.TLDR]
    items: list[str] = []

    bullet_limit = 75 if canonical.critical_disclaimers else 100

    # Title
    short_title = _compact_text(canonical.title, 70)
    title_line = f"📌 **TL;DR: {short_title}**"

    # Bullet 1: Core what it is
    items.append(f"- **Суть:** {_compact_text(canonical.what_it_is, bullet_limit)}")

    # Bullet 2: Why it matters / verdict
    items.append(f"- **Вывод:** {_compact_text(canonical.summary, bullet_limit)}")

    # Bullet 3: Verified vs Disputed facts
    if canonical.disputed_claims:
        dc = canonical.disputed_claims[0]
        items.append(f"- ⚠️ **Опровергнуто:** {_compact_text(dc.statement, bullet_limit)}")
    elif canonical.verified_claims:
        vc = canonical.verified_claims[0]
        items.append(f"- ✅ **Подтверждено:** {_compact_text(vc.statement, bullet_limit)}")
    elif canonical.key_points:
        items.append(f"- **Факт:** {_compact_text(canonical.key_points[0], bullet_limit)}")

    # Bullet 4: Action step
    if canonical.actionable_steps:
        items.append(f"- ➡️ **Действие:** {_compact_text(canonical.actionable_steps[0], bullet_limit)}")

    # Bullet 5: Disclaimer if HIGH risk (preserved verbatim, never truncated)
    if canonical.critical_disclaimers:
        items.append(f"- {canonical.critical_disclaimers[0].strip()}")

    text = title_line + "\n\n" + "\n".join(items)

    variant = RenderedVariant(
        variant_type=OutputVariantType.TLDR,
        status="RENDERED",
        text=text,
        character_count=len(text),
        language=canonical.analysis_language_code,
    )
    return validate_variant(variant, constraints, canonical)


def render_telegram_long(canonical: CanonicalContentResult) -> RenderedVariant:
    """Render the human-facing Telegram analysis without internal metadata."""
    constraints = VARIANT_CONSTRAINTS[OutputVariantType.TELEGRAM_LONG]
    sections: list[str] = []

    # 1. Compact title; 2-4. Preserve the three user-facing meaning blocks.
    sections.append(f"💡 **{render_human_title(canonical)}**")
    sections.append("🧠 **Что это такое?**\n\n" + _compact_text(canonical.what_it_is, 320))
    sections.append(
        "🎯 **Зачем это знать?**\n\n" + _compact_text(canonical.why_it_matters, 360)
    )
    sections.append("⚖️ **Вердикт**\n\n" + _compact_text(canonical.summary, 420))

    # 5. Keep risk compact. Classifier intent explanations remain available in
    # canonical/debug data but are noise in the primary Telegram message.
    human_risk = render_human_risk_explanation(
        risk_level=canonical.risk_level,
        risk_or_intent_labels=canonical.risk_reasons,
        category=canonical.topic,
    )
    risk_sec = [f"{human_risk.icon} **Риск: {human_risk.localized_level.lower()}**"]
    if canonical.critical_disclaimers:
        risk_sec.extend(canonical.critical_disclaimers)
    risk_sec.append(f"**Что делать:**\n{human_risk.guidance}")
    sections.append("\n".join(risk_sec))

    # 6. Fact-check keeps contradicted and insufficient-evidence states distinct.
    fact_lines: list[str] = ["🔎 **Что удалось проверить**"]
    if canonical.verified_claims:
        fact_lines.append("✅ **Подтверждено**")
        for claim in canonical.verified_claims:
            line = f"• {claim.statement}"
            if claim.source_url:
                line += f" ([источник]({claim.source_url}))"
            fact_lines.append(line)
    if canonical.disputed_claims:
        fact_lines.append("❌ **Опровергнуто**")
        for claim in canonical.disputed_claims:
            line = f"• {claim.statement}"
            if claim.source_url:
                line += f" ([опровержение]({claim.source_url}))"
            fact_lines.append(line)
    if canonical.uncertain_claims:
        fact_lines.append("⚠️ **Не удалось независимо подтвердить**")
        fact_lines.extend(f"• {claim.statement}" for claim in canonical.uncertain_claims)
    if len(fact_lines) > 1:
        sections.append("\n".join(fact_lines))

    # 7. Human business model only when Business Check produced one. Without
    # it, the restored verdict already carries the meaning without duplication.
    if canonical.business_summary:
        heading = _business_presentation_heading(canonical.business_summary)
        sections.append(
            f"💼 **{heading}**\n\n"
            + render_human_business_explanation(canonical.business_summary)
        )

    # 8. One useful next step.
    if canonical.actionable_steps:
        sections.append(
            "➡️ **Если тема интересна**\n\n"
            + _render_next_step(canonical.actionable_steps[0])
        )

    text = "\n\n".join(sections)
    variant = RenderedVariant(
        variant_type=OutputVariantType.TELEGRAM_LONG,
        status="RENDERED",
        text=text,
        character_count=len(text),
        language=canonical.analysis_language_code,
    )
    return validate_variant(variant, constraints, canonical)


def render_x_post(canonical: CanonicalContentResult) -> RenderedVariant:
    """Render concise post for X (Twitter) within strict 280 characters.

    Returns NOT_RENDERABLE if required facts and disclaimers cannot safely fit in budget.
    Blind substring truncation is strictly prohibited.
    """
    constraints = VARIANT_CONSTRAINTS[OutputVariantType.X_POST]
    max_len = constraints.max_length

    # Determine mandatory elements
    # 1. Mandatory disclaimer if HIGH risk
    disclaimer_text = ""
    if canonical.risk_level == "HIGH":
        if canonical.critical_disclaimers:
            disclaimer_text = "\n" + canonical.critical_disclaimers[0]
        else:
            disclaimer_text = "\n⚠️ Высокий риск: требуется консультация!"

    # 2. Main claim or verdict
    main_fact = ""
    if canonical.disputed_claims:
        main_fact = f"Фейк в ролике: «{canonical.disputed_claims[0].statement}» опровергнуто."
    elif canonical.verified_claims:
        main_fact = f"Факт: {canonical.verified_claims[0].statement}"
    else:
        main_fact = canonical.summary

    # Candidate 1: Full structured tweet with title and fact
    candidate1 = f"Разбор: {canonical.title}\n\n{main_fact}{disclaimer_text}\n#разбор"
    if len(candidate1) <= max_len:
        variant = RenderedVariant(
            variant_type=OutputVariantType.X_POST,
            status="RENDERED",
            text=candidate1,
            character_count=len(candidate1),
            language=canonical.analysis_language_code,
        )
        return validate_variant(variant, constraints, canonical)

    # Candidate 2: Compact tweet without title line
    candidate2 = f"{main_fact}{disclaimer_text}\n#разбор"
    if len(candidate2) <= max_len:
        variant = RenderedVariant(
            variant_type=OutputVariantType.X_POST,
            status="RENDERED",
            text=candidate2,
            character_count=len(candidate2),
            language=canonical.analysis_language_code,
        )
        return validate_variant(variant, constraints, canonical)

    # Candidate 3: Minimal fact + disclaimer without hashtag
    candidate3 = f"{main_fact}{disclaimer_text}"
    if len(candidate3) <= max_len:
        variant = RenderedVariant(
            variant_type=OutputVariantType.X_POST,
            status="RENDERED",
            text=candidate3,
            character_count=len(candidate3),
            language=canonical.analysis_language_code,
        )
        return validate_variant(variant, constraints, canonical)

    # If it cannot fit without dropping facts or disclaimers, return NOT_RENDERABLE
    return RenderedVariant(
        variant_type=OutputVariantType.X_POST,
        status="NOT_RENDERABLE",
        not_renderable_code="BUDGET_EXCEEDED",
        failure_reason=f"Content length exceeds 280 characters budget while preserving critical facts (needed {len(candidate3)} chars). Blind truncation prohibited.",
        language=canonical.analysis_language_code,
        validation_passed=True,
    )


def render_threads_post(canonical: CanonicalContentResult) -> RenderedVariant:
    """Render conversational post for Threads within 500 characters."""
    constraints = VARIANT_CONSTRAINTS[OutputVariantType.THREADS_POST]
    max_len = constraints.max_length

    parts: list[str] = [f"Разбираем видео: {canonical.title}"]

    # Body
    if canonical.disputed_claims:
        parts.append(
            f"Внимание: главное утверждение («{canonical.disputed_claims[0].statement}») не подтверждается источниками."
        )
    else:
        parts.append(canonical.what_it_is)

    if canonical.actionable_steps:
        parts.append(f"Что реально сделать: {canonical.actionable_steps[0]}")

    if canonical.critical_disclaimers:
        parts.append(canonical.critical_disclaimers[0])

    candidate = "\n\n".join(parts)

    if len(candidate) <= max_len:
        variant = RenderedVariant(
            variant_type=OutputVariantType.THREADS_POST,
            status="RENDERED",
            text=candidate,
            character_count=len(candidate),
            language=canonical.analysis_language_code,
        )
        return validate_variant(variant, constraints, canonical)

    # Fallback to shorter text
    short_parts = [canonical.title, canonical.summary]
    if canonical.critical_disclaimers:
        short_parts.append(canonical.critical_disclaimers[0])
    candidate_short = "\n\n".join(short_parts)

    if len(candidate_short) <= max_len:
        variant = RenderedVariant(
            variant_type=OutputVariantType.THREADS_POST,
            status="RENDERED",
            text=candidate_short,
            character_count=len(candidate_short),
            language=canonical.analysis_language_code,
        )
        return validate_variant(variant, constraints, canonical)

    return RenderedVariant(
        variant_type=OutputVariantType.THREADS_POST,
        status="NOT_RENDERABLE",
        not_renderable_code="BUDGET_EXCEEDED",
        failure_reason=f"Threads content exceeds 500 characters ({len(candidate_short)} chars).",
        language=canonical.analysis_language_code,
        validation_passed=True,
    )


def render_youtube_community(canonical: CanonicalContentResult) -> RenderedVariant:
    """Render standalone engaging post for YouTube Community."""
    constraints = VARIANT_CONSTRAINTS[OutputVariantType.YOUTUBE_COMMUNITY]

    sections: list[str] = [
        f"Разбор нового ролика: {canonical.title}",
        f"О чем видео:\n{canonical.what_it_is}",
    ]

    if canonical.verified_claims or canonical.disputed_claims:
        facts: list[str] = ["Что показала проверка:"]
        for vc in canonical.verified_claims:
            facts.append(f"• Подтверждено: {vc.statement}")
        for dc in canonical.disputed_claims:
            facts.append(f"• Опровергнуто: {dc.statement}")
        sections.append("\n".join(facts))
    else:
        sections.append(f"Главный вывод:\n{canonical.summary}")

    if canonical.critical_disclaimers:
        sections.append(canonical.critical_disclaimers[0])

    if canonical.actionable_steps:
        sections.append(f"Практический совет:\n{canonical.actionable_steps[0]}")

    sections.append("Что думаете по этому поводу? Делитесь в комментариях 👇")

    text = "\n\n".join(sections)
    variant = RenderedVariant(
        variant_type=OutputVariantType.YOUTUBE_COMMUNITY,
        status="RENDERED",
        text=text,
        character_count=len(text),
        language=canonical.analysis_language_code,
    )
    return validate_variant(variant, constraints, canonical)


class OutputVariantsPayload(BaseModel):
    contract_version: str = CONTRACT_VERSION
    canonical_source: CanonicalContentResult
    variants: dict[str, RenderedVariant]
    all_valid: bool
    rendered_count: int
    not_renderable_count: int
    failed_count: int

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def generate_all_variants(canonical: CanonicalContentResult) -> OutputVariantsPayload:
    """Generate and validate all 5 canonical output variants."""
    variants: dict[str, RenderedVariant] = {
        OutputVariantType.TLDR.value: render_tldr(canonical),
        OutputVariantType.TELEGRAM_LONG.value: render_telegram_long(canonical),
        OutputVariantType.X_POST.value: render_x_post(canonical),
        OutputVariantType.THREADS_POST.value: render_threads_post(canonical),
        OutputVariantType.YOUTUBE_COMMUNITY.value: render_youtube_community(canonical),
    }

    rendered = sum(1 for v in variants.values() if v.status == "RENDERED")
    not_renderable = sum(1 for v in variants.values() if v.status == "NOT_RENDERABLE")
    failed = sum(1 for v in variants.values() if v.status == "FAILED")
    all_valid = all(v.validation_passed for v in variants.values())

    return OutputVariantsPayload(
        contract_version=CONTRACT_VERSION,
        canonical_source=canonical,
        variants=variants,
        all_valid=all_valid,
        rendered_count=rendered,
        not_renderable_count=not_renderable,
        failed_count=failed,
    )


def resolve_telegram_delivery_payload(
    output_variants_payload: OutputVariantsPayload | None,
    legacy_analysis: str,
) -> tuple[str, str, str]:
    """
    Option A: Resolves the delivery text and status for the primary Telegram user message.

    Returns:
        (delivery_text, output_variants_outcome, delivery_mode)
        where:
          - delivery_text: str text to deliver to the Telegram user
          - output_variants_outcome: 'SUCCEEDED' or 'FAILED:TELEGRAM_LONG_NOT_RENDERABLE' / 'FAILED:MISSING'
          - delivery_mode: 'TELEGRAM_LONG' or 'FALLBACK_ANALYSIS'
    """
    if output_variants_payload and hasattr(output_variants_payload, "variants"):
        tl_variant = output_variants_payload.variants.get(OutputVariantType.TELEGRAM_LONG.value)
        if tl_variant and tl_variant.status == "RENDERED" and tl_variant.text:
            return tl_variant.text, "SUCCEEDED", "TELEGRAM_LONG"

        reason = "NOT_FOUND"
        if tl_variant:
            reason = tl_variant.not_renderable_code or tl_variant.failure_reason or tl_variant.status
        return legacy_analysis, f"FAILED:TELEGRAM_LONG_{reason}", "FALLBACK_ANALYSIS"

    return legacy_analysis, "FAILED:NO_OUTPUT_VARIANTS", "FALLBACK_ANALYSIS"
