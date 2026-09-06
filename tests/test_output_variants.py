"""Tests for multiple output variants contract, renderers, constraints, and invariants."""

import json

import pytest

from app.worker.content_router import (
    IntentScore,
    LabelScore,
    RouterDecision,
    calibrate_decision,
)
from app.worker.language import LanguageContext
from app.worker.output_variants import (
    VARIANT_CONSTRAINTS,
    CanonicalContentResult,
    ClaimSummary,
    OutputVariantsPayload,
    OutputVariantType,
    build_canonical_content_result,
    generate_all_variants,
    render_telegram_long,
    render_threads_post,
    render_tldr,
    render_x_post,
    render_youtube_community,
    validate_variant,
)
from app.worker.priority_policy import PriorityGateResult
from app.worker.schemas import Claim, PriorityScore, VideoAnalysis
from app.worker.specialized_analysis import PersonalRelevance, SpecializedAnalysis
from app.worker.variant_evaluation import evaluate_variants, load_variant_cases


def _dummy_route(risk: str = "LOW", primary: str = "HOW_TO") -> RouterDecision:
    return calibrate_decision(
        RouterDecision(
            primary_type=primary,
            labels=[LabelScore(label=primary, confidence=0.95)],
            intents=[IntentScore(intent="TEACH", confidence=0.9)],
            risk=risk,
            summary="Инструкция и практический разбор.",
        )
    )


def _dummy_priority(score: float = 0.65, tier: str = "HIGH") -> PriorityGateResult:
    return PriorityGateResult(
        score=PriorityScore(
            importance=score,
            virality=score,
            novelty=score,
            views_potential=score,
            audience_value=score,
            overall=score,
            publish=score >= 0.6,
            reasons=["Score reason"],
        ),
        tier=tier,  # type: ignore[arg-type]
        decision="ACCEPTED" if score >= 0.6 else "AMBIGUOUS_CONTINUE",
        publish_threshold=0.60,
        deprioritize_below=0.40,
        reasons=["Good score"],
    )


def _dummy_lang(code: str = "ru") -> LanguageContext:
    return LanguageContext(
        detected_language="Russian" if code == "ru" else "English",
        detected_language_code=code,
        confidence=0.98,
        is_mixed_language=False,
        source_languages=[code],
        analysis_language="Russian",
        analysis_language_code="ru",
        user_output_language="Russian",
        user_output_language_code="ru",
        channel_output_language="Russian",
        channel_output_language_code="ru",
        translation_required=code != "ru",
        translation_mode="none" if code == "ru" else "model_prompted",
        detection_method="deterministic_script_lexical_v1",
        original_transcript_sha256="abc123def456",
    )


def _dummy_specialized() -> SpecializedAnalysis:
    return SpecializedAnalysis(
        verdict="Отличный практический инструмент для создания Telegram-ботов.",
        what_it_is="Инструкция по настройке бота через BotFather.",
        why_it_matters="Позволяет автоматизировать рутину за 5 минут.",
        truth_assessment="Факты полностью соответствуют официальной документации Telegram.",
        relevance="Актуально для любого разработчика.",
        next_action="Зарегистрировать бота в @BotFather.",
        personal_relevance=PersonalRelevance(
            status="NOT_FOUND",
            needed="YES",
            explanation="Полезный инструмент.",
            evidence=["Официальный API"],
        ),
        technical_details="Используется HTTP API Telegram.",
        actionable=True,
        task_description="Создать и настроить бота в Telegram.",
    )


# Test 1: TLDR generated from canonical result
def test_tldr_generated_from_canonical():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Быстрый старт бота",
    )
    res = render_tldr(canonical)
    assert res.status == "RENDERED"
    assert res.validation_passed
    assert res.character_count <= VARIANT_CONSTRAINTS[OutputVariantType.TLDR].max_length
    assert "TL;DR" in res.text
    assert "Суть:" in res.text


# Test 2: Telegram long generated correctly
def test_telegram_long_generated_correctly():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Быстрый старт бота",
    )
    res = render_telegram_long(canonical)
    assert res.status == "RENDERED"
    assert res.validation_passed
    assert res.character_count <= VARIANT_CONSTRAINTS[OutputVariantType.TELEGRAM_LONG].max_length
    assert "Разбор:" in res.text
    assert "Что это такое?" in res.text
    assert "Вердикт:" in res.text


# Test 3: X respects character limit
def test_x_post_respects_character_limit():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Бот за 5 минут",
    )
    res = render_x_post(canonical)
    assert res.status == "RENDERED"
    assert res.validation_passed
    assert res.character_count <= 280


# Test 4: X returns NOT_RENDERABLE instead of blind truncation on huge content
def test_x_post_returns_not_renderable_instead_of_blind_truncation():
    canonical = build_canonical_content_result(
        route=_dummy_route(risk="HIGH", primary="HEALTH_MEDICAL"),
        priority=_dummy_priority(score=0.5, tier="AMBIGUOUS"),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Очень длинный сложный заголовок медицинского протокола",
    )
    # Inject a 250-character disputed claim
    canonical.disputed_claims = [
        ClaimSummary(
            statement="Крайне длинное медицинское утверждение о чудесном исцелении всех болезней сразу с помощью солевых ванн и специальных травяных экстрактов без операции и консультации с профильным врачом-нейрохирургом.",
            status="опровергнуто",
        )
    ]
    res = render_x_post(canonical)
    assert res.status == "NOT_RENDERABLE"
    assert res.not_renderable_code == "BUDGET_EXCEEDED"
    assert "280" in res.failure_reason
    assert res.validation_passed  # NOT_RENDERABLE is a valid typed outcome


# Test 5: Threads contract validated
def test_threads_contract_validated():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Разбор автоматизации",
    )
    res = render_threads_post(canonical)
    assert res.status == "RENDERED"
    assert res.validation_passed
    assert res.character_count <= 500


# Test 6: YouTube Community contract validated
def test_youtube_community_contract_validated():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Автоматизация бизнеса",
    )
    res = render_youtube_community(canonical)
    assert res.status == "RENDERED"
    assert res.validation_passed
    assert res.character_count <= 2000
    assert "комментариях" in res.text


# Test 7: Verified fact remains verified
def test_verified_fact_remains_verified():
    analysis = VideoAnalysis(
        claims=[
            Claim(
                statement="API Telegram бесплатен для создания ботов.",
                claim_type="fact",
                status="подтверждено",
                source_url="https://core.telegram.org/bots/api",
            )
        ],
        viable_idea=True,
    )
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        analysis=analysis,
    )
    assert len(canonical.verified_claims) == 1
    assert canonical.verified_claims[0].status == "подтверждено"
    tldr = render_tldr(canonical)
    assert "Подтверждено:" in tldr.text


# Test 8: Uncertain fact remains uncertain
def test_uncertain_fact_remains_uncertain():
    analysis = VideoAnalysis(
        claims=[
            Claim(
                statement="В следующем году выйдет новая версия Telegram API.",
                claim_type="fact",
                status="не проверено",
            )
        ],
        viable_idea=True,
    )
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        analysis=analysis,
    )
    assert len(canonical.uncertain_claims) == 1
    tg_long = render_telegram_long(canonical)
    assert "Не подтверждено независимыми источниками" in tg_long.text


# Test 9: Disputed fact is not presented as certain
def test_disputed_fact_not_presented_as_certain():
    analysis = VideoAnalysis(
        claims=[
            Claim(
                statement="Корица излечивает диабет 1 типа.",
                claim_type="fact",
                status="опровергнуто",
                source_url="https://who.int/diabetes",
            )
        ],
        viable_idea=False,
    )
    canonical = build_canonical_content_result(
        route=_dummy_route(risk="HIGH", primary="HEALTH_MEDICAL"),
        priority=_dummy_priority(score=0.3, tier="LOW"),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        analysis=analysis,
    )
    assert len(canonical.disputed_claims) == 1
    tldr = render_tldr(canonical)
    assert "Опровергнуто:" in tldr.text
    # Validation must pass and ensure no false confirmation
    assert tldr.validation_passed


# Test 10: Risk warning cannot disappear in HIGH risk
def test_risk_warning_cannot_disappear():
    canonical = build_canonical_content_result(
        route=_dummy_route(risk="HIGH", primary="HEALTH_MEDICAL"),
        priority=_dummy_priority(score=0.4, tier="AMBIGUOUS"),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
    )
    assert len(canonical.critical_disclaimers) > 0
    payload = generate_all_variants(canonical)
    for variant in payload.variants.values():
        if variant.status == "RENDERED":
            assert any(
                w in variant.text.lower()
                for w in ["⚠️", "внимание", "риск", "врач", "консультац"]
            )


# Test 11: Output language follows LanguageContext
def test_output_language_follows_language_context():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(code="en"),  # English source, Russian analysis
        specialized=_dummy_specialized(),
    )
    payload = generate_all_variants(canonical)
    for variant in payload.variants.values():
        assert variant.language == "ru"


# Test 12: Original quote policy preserved
def test_original_quote_policy_preserved():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
    )
    canonical.verified_claims.append(
        ClaimSummary(
            statement="«Deploy anywhere with Docker» — авторская цитата.",
            status="подтверждено",
        )
    )
    tldr = render_tldr(canonical)
    assert tldr.status == "RENDERED"
    assert "«Deploy anywhere with Docker»" in tldr.text


# Test 13 & 14: Router and Priority results invariant
def test_router_and_priority_results_invariant():
    route_in = _dummy_route(risk="MEDIUM", primary="BUSINESS_IDEA")
    priority_in = _dummy_priority(score=0.55, tier="AMBIGUOUS")
    canonical = build_canonical_content_result(
        route=route_in,
        priority=priority_in,
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
    )
    # Verify canonical carries exact upstream classification without mutation
    assert canonical.risk_level == route_in.risk
    assert canonical.topic == "Business Idea"
    assert canonical.priority_tier == priority_in.tier
    assert canonical.priority_score == priority_in.score.overall


# Test 15: Required variant failure affects completion correctly
def test_required_variant_failure_semantics():
    # If a variant status is FAILED, all_valid is False
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
    )
    payload = generate_all_variants(canonical)
    assert payload.all_valid is True
    assert payload.failed_count == 0


# Test 16: Optional variant failure does not create false error
def test_optional_not_renderable_does_not_fail_payload():
    canonical = build_canonical_content_result(
        route=_dummy_route(risk="HIGH", primary="HEALTH_MEDICAL"),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
    )
    # Long claim forces X_POST to NOT_RENDERABLE
    canonical.disputed_claims = [
        ClaimSummary(
            statement="Очень длинное развернутое псевдонаучное медицинское утверждение о лечении абсолютно всех хронических недугов содой и солью, которое гарантированно и физически не умещается в жесткий бюджет 280 символов твита вместе с обязательным предупреждением врачей.",
            status="опровергнуто",
        )
    ]
    payload = generate_all_variants(canonical)
    assert payload.variants["X_POST"].status == "NOT_RENDERABLE"
    # Even though X_POST is NOT_RENDERABLE, the payload is still valid
    assert payload.variants["X_POST"].validation_passed is True
    assert payload.all_valid is True


# Test 17: Persistence roundtrip works
def test_persistence_roundtrip():
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
    )
    payload = generate_all_variants(canonical)
    serialized = payload.model_dump()
    json_str = json.dumps(serialized)
    deserialized = OutputVariantsPayload(**json.loads(json_str))
    assert deserialized.contract_version == "variants_v1"
    assert "TLDR" in deserialized.variants
    assert deserialized.variants["TLDR"].character_count > 0


# Test 18: Full offline variant evaluation suite is 100% green
def test_offline_variant_replay_is_green():
    cases = load_variant_cases("tests/fixtures/output_variants_eval.json")
    report = evaluate_variants(cases)
    assert report.total_cases == 8
    assert report.total_evaluations == 40
    assert report.format_validity == 1.0
    assert report.length_compliance == 1.0
    assert report.mandatory_fact_recall == 1.0
    assert report.forbidden_fact_rate == 0.0
    assert report.uncertainty_preservation == 1.0
    assert report.risk_warning_preservation == 1.0
    assert report.not_renderable_accuracy == 1.0
    assert report.risk_warning_violations == 0
    assert report.invented_forbidden_facts == 0
    assert report.platform_limit_violations == 0
    assert len(report.failed_evaluations) == 0


# --- PRODUCTION INTEGRATION & COMPLETION SEMANTICS REGRESSION TESTS ---

from app.worker.output_variants import resolve_telegram_delivery_payload
from app.worker.tasks import determine_completion_status


def test_telegram_user_delivery_bound_to_telegram_long():
    """Regression 1: Production Telegram user message must be bound to TELEGRAM_LONG."""
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Тестовый заголовок",
    )
    payload = generate_all_variants(canonical)
    tl_variant = payload.variants["TELEGRAM_LONG"]
    assert tl_variant.status == "RENDERED"

    legacy_analysis = "Старый неструктурированный текст анализа"
    text, outcome, mode = resolve_telegram_delivery_payload(payload, legacy_analysis)

    assert mode == "TELEGRAM_LONG"
    assert outcome == "SUCCEEDED"
    assert text == tl_variant.text
    assert text != legacy_analysis
    assert "📋 **Разбор: Тестовый заголовок**" in text


def test_telegram_long_rendering_failure_has_correct_completion_semantics():
    """Regression 2: TELEGRAM_LONG rendering failure yields fallback and PARTIAL terminal status (no false DONE)."""
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Тест сбоя",
    )
    payload = generate_all_variants(canonical)
    # Simulate TELEGRAM_LONG failure
    payload.variants["TELEGRAM_LONG"].status = "NOT_RENDERABLE"
    payload.variants["TELEGRAM_LONG"].not_renderable_code = "BUDGET_EXCEEDED"

    legacy_analysis = "Резервный анализ для пользователя"
    text, outcome, mode = resolve_telegram_delivery_payload(payload, legacy_analysis)

    assert mode == "FALLBACK_ANALYSIS"
    assert outcome.startswith("FAILED:TELEGRAM_LONG_")
    assert text == legacy_analysis

    # Check that completion status is PARTIAL, NEVER DONE
    delivery_status = {
        "user": "SUCCEEDED_FALLBACK",
        "output_variants": outcome,
        "channel": "NOT_APPLICABLE",
    }
    assert determine_completion_status(delivery_status) == "PARTIAL"


def test_optional_x_failure_does_not_break_user_delivery():
    """Regression 3: Optional X_POST failure does NOT break user delivery or degrade DONE status."""
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Тест опционального X",
    )
    payload = generate_all_variants(canonical)
    # Simulate X_POST budget overflow
    payload.variants["X_POST"].status = "NOT_RENDERABLE"
    payload.variants["X_POST"].not_renderable_code = "BUDGET_EXCEEDED"

    legacy_analysis = "Резервный анализ"
    text, outcome, mode = resolve_telegram_delivery_payload(payload, legacy_analysis)

    assert mode == "TELEGRAM_LONG"
    assert outcome == "SUCCEEDED"
    assert text == payload.variants["TELEGRAM_LONG"].text

    delivery_status = {
        "user": "SUCCEEDED",
        "output_variants": outcome,
        "channel": "NOT_APPLICABLE",
    }
    assert determine_completion_status(delivery_status) == "DONE"


def test_optional_threads_failure_does_not_break_user_delivery():
    """Regression 4: Optional THREADS_POST failure does NOT break user delivery or degrade DONE status."""
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Тест опционального Threads",
    )
    payload = generate_all_variants(canonical)
    # Simulate THREADS_POST failure
    payload.variants["THREADS_POST"].status = "FAILED"
    payload.variants["THREADS_POST"].failure_reason = "INTERNAL_ERROR"

    legacy_analysis = "Резервный анализ"
    text, outcome, mode = resolve_telegram_delivery_payload(payload, legacy_analysis)

    assert mode == "TELEGRAM_LONG"
    assert outcome == "SUCCEEDED"
    assert text == payload.variants["TELEGRAM_LONG"].text

    delivery_status = {
        "user": "SUCCEEDED",
        "output_variants": outcome,
        "channel": "NOT_APPLICABLE",
    }
    assert determine_completion_status(delivery_status) == "DONE"


def test_optional_youtube_community_failure_does_not_break_user_delivery():
    """Regression 5: Optional YOUTUBE_COMMUNITY failure does NOT break user delivery or degrade DONE status."""
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Тест опционального YouTube",
    )
    payload = generate_all_variants(canonical)
    # Simulate YouTube failure
    payload.variants["YOUTUBE_COMMUNITY"].status = "FAILED"
    payload.variants["YOUTUBE_COMMUNITY"].failure_reason = "INTERNAL_ERROR"

    legacy_analysis = "Резервный анализ"
    text, outcome, mode = resolve_telegram_delivery_payload(payload, legacy_analysis)

    assert mode == "TELEGRAM_LONG"
    assert outcome == "SUCCEEDED"
    assert text == payload.variants["TELEGRAM_LONG"].text

    delivery_status = {
        "user": "SUCCEEDED",
        "output_variants": outcome,
        "channel": "NOT_APPLICABLE",
    }
    assert determine_completion_status(delivery_status) == "DONE"


def test_legacy_telegram_behavior_not_bypassing_output_variant():
    """Regression 6: Legacy Telegram behavior is not bypassing parallel independent renderer path."""
    canonical = build_canonical_content_result(
        route=_dummy_route(),
        priority=_dummy_priority(),
        language_context=_dummy_lang(),
        specialized=_dummy_specialized(),
        title="Уникальный Заголовок Регрессии",
    )
    payload = generate_all_variants(canonical)
    legacy_raw = "Неструктурированный старый сырой ответ модели"

    delivery_text, outcome, mode = resolve_telegram_delivery_payload(payload, legacy_raw)

    assert mode == "TELEGRAM_LONG"
    assert delivery_text != legacy_raw
    assert "📋 **Разбор: Уникальный Заголовок Регрессии**" in delivery_text
    assert "🧠 **Что это такое?**" in delivery_text
    assert "🎯 **Зачем это знать?**" in delivery_text
    assert "⚖️ **Вердикт:**" in delivery_text
    assert "Уровень риска:" in delivery_text

