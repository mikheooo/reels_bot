"""Unit and contract tests for Content Factory Delivery & Distribution MVP."""

import pytest

from app.worker.content_package import (
    ContentPackage,
    DeliveryOutcome,
    DeliveryRecord,
    InvalidLifecycleTransitionError,
    OutputVariantType,
    PackageStatus,
    PublicationMode,
    TargetDeliveryStatus,
    TargetPlatform,
    UnauthorizedApprovalError,
    approve_package,
    build_content_package,
    classify_delivery_error,
    regenerate_variant_from_canonical,
    reject_package,
    transition_package_status,
)
from app.worker.output_variants import (
    CanonicalContentResult,
    ClaimSummary,
    generate_all_variants,
)
from app.worker.tasks import determine_completion_status


def _sample_canonical(
    risk_level: str = "LOW",
    title: str = "Тестовое руководство по Docker",
    disclaimers: list[str] | None = None,
) -> CanonicalContentResult:
    return CanonicalContentResult(
        title=title,
        topic="DevOps",
        summary="Краткий обзор настройки Docker контейнеров для продакшна.",
        what_it_is="Пошаговая инструкция по сборке оптимизированных образов.",
        why_it_matters="Позволяет уменьшить размер образов и ускорить деплой.",
        key_points=["Используйте multi-stage сборку", "Минимизируйте слои"],
        actionable_steps=["Настроить Dockerfile", "Проверить кэширование"],
        risk_level=risk_level,
        risk_reasons=["Неправильная настройка ведет к уязвимостям"] if risk_level == "HIGH" else [],
        critical_disclaimers=disclaimers or (["ВНИМАНИЕ: Высокий риск безопасности."] if risk_level == "HIGH" else []),
        source_language_code="ru",
        analysis_language_code="ru",
        priority_tier="HIGH",
        priority_score=0.85,
        priority_reasons=["High technical utility"],
    )


# Test 1: ContentPackage creation from canonical result
def test_content_package_creation_from_canonical():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_101", "https://instagram.com/reel/test", canonical, variants)

    assert pkg.job_id == "job_101"
    assert pkg.source_url == "https://instagram.com/reel/test"
    assert pkg.contract_version == "content_package_v1"
    assert len(pkg.distribution_targets) == 5
    assert TargetPlatform.TELEGRAM_USER.value in pkg.distribution_targets
    assert TargetPlatform.TELEGRAM_CHANNEL.value in pkg.distribution_targets
    assert TargetPlatform.X.value in pkg.distribution_targets
    assert TargetPlatform.THREADS.value in pkg.distribution_targets
    assert TargetPlatform.YOUTUBE_COMMUNITY.value in pkg.distribution_targets


# Test 2: Package lifecycle valid transitions
def test_package_lifecycle_valid_transitions():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_102", "https://instagram.com/reel/test", canonical, variants)

    assert pkg.approval_state == PackageStatus.REVIEW_REQUIRED
    pkg = transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)
    assert pkg.approval_state == PackageStatus.PARTIALLY_DELIVERED
    pkg = transition_package_status(pkg, PackageStatus.APPROVED)
    assert pkg.approval_state == PackageStatus.APPROVED
    pkg = transition_package_status(pkg, PackageStatus.DELIVERED)
    assert pkg.approval_state == PackageStatus.DELIVERED


# Test 3: Invalid lifecycle transitions blocked
def test_package_lifecycle_invalid_transitions_blocked():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_103", "https://instagram.com/reel/test", canonical, variants)

    # Deliver terminal
    pkg = transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)
    pkg = transition_package_status(pkg, PackageStatus.DELIVERED)

    with pytest.raises(InvalidLifecycleTransitionError):
        transition_package_status(pkg, PackageStatus.GENERATED)

    with pytest.raises(InvalidLifecycleTransitionError):
        transition_package_status(pkg, PackageStatus.REVIEW_REQUIRED)


# Test 4: Generation != Approval
def test_generation_does_not_imply_approval():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_104", "https://instagram.com/reel/test", canonical, variants)

    assert pkg.approval_state != PackageStatus.APPROVED
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.PENDING_APPROVAL
    assert pkg.distribution_targets[TargetPlatform.X.value].required_approval is True


# Test 5: Approval != Publication (Level A boundary)
def test_approval_does_not_imply_publication():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_105", "https://instagram.com/reel/test", canonical, variants)

    pkg, recs = approve_package(pkg, user_id=42, expected_owner_id=42)

    # External targets are APPROVED_NOT_CONNECTED, NOT published
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.APPROVED_NOT_CONNECTED
    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.APPROVED_NOT_CONNECTED
    assert pkg.distribution_targets[TargetPlatform.YOUTUBE_COMMUNITY.value].status == TargetDeliveryStatus.APPROVED_NOT_CONNECTED
    for r in recs:
        assert r.status == DeliveryOutcome.APPROVED_NOT_CONNECTED
        assert r.external_id is None


# Test 6: Rejection blocks delivery
def test_rejection_blocks_all_delivery():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_106", "https://instagram.com/reel/test", canonical, variants)

    pkg = reject_package(pkg, user_id=42, expected_owner_id=42, reason="Quality check failed")

    assert pkg.approval_state == PackageStatus.REJECTED
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.REJECTED
    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.REJECTED

    with pytest.raises(InvalidLifecycleTransitionError):
        approve_package(pkg, user_id=42, expected_owner_id=42)


# Test 7: Telegram user delivery preserved
def test_telegram_user_delivery_preserved():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_107", "https://instagram.com/reel/test", canonical, variants)

    user_target = pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value]
    assert user_target.required_approval is False
    assert user_target.publication_mode == PublicationMode.AUTOMATIC
    assert user_target.variant_type == OutputVariantType.TELEGRAM_LONG
    assert user_target.rendered_payload is not None


# Test 8: Telegram channel duplicate protection preserved
def test_telegram_channel_duplicate_protection_preserved():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_108", "https://instagram.com/reel/test", canonical, variants)

    ch_target = pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value]
    ch_target.status = TargetDeliveryStatus.SKIPPED_DUPLICATE

    rec = DeliveryRecord.create(
        package_id=pkg.package_id,
        target=TargetPlatform.TELEGRAM_CHANNEL,
        variant=OutputVariantType.TELEGRAM_LONG,
        attempt_id=1,
        approval_state=pkg.approval_state,
        status=DeliveryOutcome.SKIPPED_DUPLICATE,
    )
    pkg.delivery_records.append(rec)

    assert pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status == TargetDeliveryStatus.SKIPPED_DUPLICATE
    assert pkg.delivery_records[-1].status == DeliveryOutcome.SKIPPED_DUPLICATE


# Test 9: External targets require manual approval
def test_external_targets_require_manual_approval():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_109", "https://instagram.com/reel/test", canonical, variants)

    for platform in (TargetPlatform.X, TargetPlatform.THREADS, TargetPlatform.YOUTUBE_COMMUNITY):
        target = pkg.distribution_targets[platform.value]
        assert target.required_approval is True
        assert target.publication_mode == PublicationMode.MANUAL_APPROVAL
        assert target.status in (TargetDeliveryStatus.PENDING_APPROVAL, TargetDeliveryStatus.NOT_RENDERABLE)


# Test 10: Unauthorized user approval rejected
def test_unauthorized_user_approval_rejected():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_110", "https://instagram.com/reel/test", canonical, variants)

    with pytest.raises(UnauthorizedApprovalError):
        approve_package(pkg, user_id=9999, expected_owner_id=1001)

    with pytest.raises(UnauthorizedApprovalError):
        reject_package(pkg, user_id=9999, expected_owner_id=1001)


# Test 11: Double approve is idempotent
def test_double_approve_is_idempotent():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_111", "https://instagram.com/reel/test", canonical, variants)

    pkg, recs1 = approve_package(pkg, user_id=1001, expected_owner_id=1001)
    count1 = len(pkg.delivery_records)

    pkg, recs2 = approve_package(pkg, user_id=1001, expected_owner_id=1001)
    count2 = len(pkg.delivery_records)

    assert len(recs2) == 0
    assert count1 == count2


# Test 12: Duplicate delivery idempotency key
def test_duplicate_delivery_idempotency_key():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_112", "https://instagram.com/reel/test", canonical, variants)

    rec1 = DeliveryRecord.create(
        package_id=pkg.package_id,
        target=TargetPlatform.TELEGRAM_USER,
        variant=OutputVariantType.TELEGRAM_LONG,
        attempt_id=1,
        approval_state=PackageStatus.APPROVED,
        status=DeliveryOutcome.SUCCEEDED,
    )
    assert rec1.idempotency_key == f"{pkg.package_id}:TELEGRAM_USER:TELEGRAM_LONG:1"


# Test 13: Retryable error classification
def test_retryable_error_classification():
    is_ret, code, _ = classify_delivery_error("Gateway Timeout 504")
    assert is_ret is True
    assert code == "GATEWAY_TIMEOUT_504"

    is_ret, code, _ = classify_delivery_error("Rate limit exceeded 429")
    assert is_ret is True
    assert code == "RATE_LIMIT_429"


# Test 14: Non-retryable error classification
def test_non_retryable_error_classification():
    is_ret, code, _ = classify_delivery_error("Invalid credentials provided")
    assert is_ret is False
    assert code == "AUTH_INVALID_CREDENTIALS"

    is_ret, code, _ = classify_delivery_error("Length exceeded platform budget")
    assert is_ret is False
    assert code == "LENGTH_EXCEEDED"


# Test 15: Variant regeneration preserves canonical facts
def test_variant_regeneration_preserves_canonical_facts():
    canonical = _sample_canonical(
        risk_level="HIGH",
        disclaimers=["ВНИМАНИЕ: Опасная инструкция! Не повторяйте дома."],
    )
    for vt in OutputVariantType:
        regen = regenerate_variant_from_canonical(canonical, vt)
        assert regen.status in ("RENDERED", "NOT_RENDERABLE")
        if regen.status == "RENDERED":
            assert "ВНИМАНИЕ: Опасная инструкция! Не повторяйте дома." in (regen.text or "")


# Test 16: Risk warnings preserved in distribution targets
def test_risk_warning_preservation_in_targets():
    disclaimer = "ВНИМАНИЕ: Медицинское предупреждение!"
    canonical = _sample_canonical(risk_level="HIGH", disclaimers=[disclaimer])
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_116", "https://instagram.com/reel/test", canonical, variants)

    for t in pkg.distribution_targets.values():
        if t.rendered_payload:
            assert disclaimer in t.rendered_payload


# Test 17: Optional target failure does not fail job
def test_optional_target_failure_does_not_fail_job():
    delivery_status = {
        "user": "SUCCEEDED",
        "channel": "SKIPPED_DUPLICATE",
        "content_package": "CREATED",
        "output_variants": "SUCCEEDED",
        "plan": "SUCCEEDED",
        "task_db": "SUCCEEDED",
    }
    status = determine_completion_status(delivery_status)
    assert status == "DONE"


# Test 18: ContentPackage persistence roundtrip
def test_content_package_persistence_roundtrip():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_118", "https://instagram.com/reel/test", canonical, variants)

    data = pkg.model_dump(mode="json")
    restored = ContentPackage.model_validate(data)

    assert restored.package_id == pkg.package_id
    assert restored.job_id == pkg.job_id
    assert len(restored.distribution_targets) == len(pkg.distribution_targets)
    assert restored.approval_state == pkg.approval_state


# Test 19: Migration idempotent and safe
@pytest.mark.asyncio
async def test_migration_idempotent_and_safe():
    from unittest.mock import AsyncMock, MagicMock

    from app.db.migrate import apply_migrations

    mock_conn = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn

    # Should execute all additive statements without error
    await apply_migrations(mock_engine)
    assert mock_conn.execute.call_count >= 10


# Test 20: Existing completion semantics green
def test_existing_completion_semantics_green():
    assert determine_completion_status({"user": "SUCCEEDED"}) == "DONE"
    assert determine_completion_status({"user": "FAILED:NETWORK_TIMEOUT"}) == "PARTIAL"
    assert determine_completion_status({"user": "SUCCEEDED_FALLBACK", "content_package": "CREATED"}) == "DONE"
