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
                statement=c.statement,
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
    """Render full structured Markdown for Telegram."""
    constraints = VARIANT_CONSTRAINTS[OutputVariantType.TELEGRAM_LONG]
    sections: list[str] = []

    # Header
    sections.append(f"📋 **Разбор: {canonical.title}**")

    # Section 1: Суть
    sections.append(
        f"🧠 **Что это такое?**\n{canonical.what_it_is}\n\n🎯 **Зачем это знать?**\n{canonical.why_it_matters}"
    )

    # Section 2: Итог и вердикт
    sections.append(f"⚖️ **Вердикт:**\n{canonical.summary}")

    # Section 3: Проверка фактов
    fact_lines: list[str] = ["🔎 **Фактологическая проверка:**"]
    if canonical.verified_claims:
        fact_lines.append("✅ **Подтверждённые данные:**")
        for c in canonical.verified_claims:
            line = f"- {c.statement}"
            if c.source_url:
                line += f" ([источник]({c.source_url}))"
            fact_lines.append(line)
    if canonical.disputed_claims:
        fact_lines.append("❌ **Опровергнутые утверждения:**")
        for c in canonical.disputed_claims:
            line = f"- {c.statement}"
            if c.source_url:
                line += f" ([опровержение]({c.source_url}))"
            fact_lines.append(line)
    if canonical.uncertain_claims:
        fact_lines.append("⚠️ **Не подтверждено независимыми источниками:**")
        for c in canonical.uncertain_claims:
            fact_lines.append(f"- {c.statement}")

    if len(fact_lines) > 1:
        sections.append("\n".join(fact_lines))

    # Section 4: Бизнес-контекст (если есть)
    if canonical.business_summary:
        sections.append(f"💼 **Бизнес-разбор:**\n{canonical.business_summary}")

    # Section 5: Риски и дисклеймеры
    risk_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}[canonical.risk_level]
    risk_sec = [f"{risk_icon} **Уровень риска: {canonical.risk_level}**"]
    if canonical.critical_disclaimers:
        risk_sec.extend(canonical.critical_disclaimers)
    if canonical.risk_reasons:
        risk_sec.append("Факторы риска: " + ", ".join(canonical.risk_reasons))
    sections.append("\n".join(risk_sec))

    # Section 6: Действия
    if canonical.actionable_steps:
        steps = "\n".join(f"- {step}" for step in canonical.actionable_steps)
        sections.append(f"➡️ **Рекомендуемые шаги:**\n{steps}")

    # Footer
    sections.append(
        f"🏷 `Приоритет: {canonical.priority_tier} ({canonical.priority_score:.2f})` · `Язык: {canonical.source_language_code}`"
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

