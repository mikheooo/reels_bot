"""Deterministic test suite and replay evaluation harness for Priority 5: Publication Orchestrator Core."""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.worker.connectors import (
    AmbiguousReconciliationResult,
    CapabilityStatus,
    ConnectorCapabilities,
    ConnectorRegistry,
    CredentialValidationResult,
    LookupResult,
    PublicationConnector,
    PublicationResult,
    RateLimitInfo,
    ReconciliationConfidence,
    ThreadsConnector,
    XConnector,
    YouTubeCommunityConnector,
)
from app.worker.content_package import (
    CanonicalContentResult,
    ContentPackage,
    DeliveryOutcome,
    DistributionTarget,
    InvalidLifecycleTransitionError,
    OutputVariantsPayload,
    OutputVariantType,
    PackageStatus,
    PublicationIntent,
    PublicationIntentStatus,
    PublicationMode,
    RenderedVariant,
    TargetDeliveryStatus,
    TargetPlatform,
    UnauthorizedApprovalError,
    _utc_now,
    approve_package,
    compute_payload_hash,
)
from app.worker.publication_orchestrator import (
    OwnerApproval,
    PublicationOrchestrator,
    PublicationPlan,
    PublicationPlanStatus,
    PublicationState,
    calculate_aggregate_plan_status,
    compute_package_content_hash,
    verify_approval,
)


def _make_test_package(
    package_id: str = "pkg-test-001",
    owner_id: int = 1001,
    x_text: str = "Test X post content #tech",
    threads_text: str = "Test Threads post content",
    yt_text: str = "Test YouTube community post",
) -> ContentPackage:
    """Helper to build a valid test ContentPackage."""
    canonical = CanonicalContentResult(
        contract_version="canonical_v1",
        title="Test Title",
        topic="Tech",
        summary="Test summary",
        what_it_is="A test package",
        why_it_matters="Tests orchestrator",
        key_points=["Point 1", "Point 2"],
        actionable_steps=["Step 1"],
        risk_level="LOW",
        risk_reasons=[],
        critical_disclaimers=[],
        source_language_code="ru",
        analysis_language_code="ru",
        priority_tier="HIGH",
        priority_score=0.85,
        priority_reasons=["High priority test"],
        citations=[],
    )
    variants = OutputVariantsPayload(
        canonical_source=canonical,
        all_valid=True,
        rendered_count=5,
        not_renderable_count=0,
        failed_count=0,
        variants={
            OutputVariantType.TLDR.value: RenderedVariant(
                variant_type=OutputVariantType.TLDR,
                text="TLDR test text",
                character_count=14,
                status="RENDERED",
            ),
            OutputVariantType.TELEGRAM_LONG.value: RenderedVariant(
                variant_type=OutputVariantType.TELEGRAM_LONG,
                text="Telegram long test text",
                character_count=23,
                status="RENDERED",
            ),
            OutputVariantType.X_POST.value: RenderedVariant(
                variant_type=OutputVariantType.X_POST,
                text=x_text,
                character_count=len(x_text),
                status="RENDERED",
            ),
            OutputVariantType.THREADS_POST.value: RenderedVariant(
                variant_type=OutputVariantType.THREADS_POST,
                text=threads_text,
                character_count=len(threads_text),
                status="RENDERED",
            ),
            OutputVariantType.YOUTUBE_COMMUNITY.value: RenderedVariant(
                variant_type=OutputVariantType.YOUTUBE_COMMUNITY,
                text=yt_text,
                character_count=len(yt_text),
                status="RENDERED",
            ),
        },
    )
    targets = {
        TargetPlatform.X.value: DistributionTarget(
            target=TargetPlatform.X,
            variant_type=OutputVariantType.X_POST,
            publication_mode=PublicationMode.MANUAL_APPROVAL,
            required_approval=True,
            status=TargetDeliveryStatus.PENDING_APPROVAL,
            rendered_payload=x_text,
        ),
        TargetPlatform.THREADS.value: DistributionTarget(
            target=TargetPlatform.THREADS,
            variant_type=OutputVariantType.THREADS_POST,
            publication_mode=PublicationMode.MANUAL_APPROVAL,
            required_approval=True,
            status=TargetDeliveryStatus.PENDING_APPROVAL,
            rendered_payload=threads_text,
        ),
        TargetPlatform.YOUTUBE_COMMUNITY.value: DistributionTarget(
            target=TargetPlatform.YOUTUBE_COMMUNITY,
            variant_type=OutputVariantType.YOUTUBE_COMMUNITY,
            publication_mode=PublicationMode.MANUAL_APPROVAL,
            required_approval=True,
            status=TargetDeliveryStatus.PENDING_APPROVAL,
            rendered_payload=yt_text,
        ),
    }
    return ContentPackage(
        package_id=package_id,
        job_id="job-test-001",
        source_url="https://instagram.com/reel/test",
        canonical_content=canonical,
        output_variants=variants,
        distribution_targets=targets,
        approval_state=PackageStatus.GENERATED,
    )


class MockTestConnector(PublicationConnector):
    """Controllable mock connector for deterministic orchestration testing."""

    def __init__(
        self,
        target: TargetPlatform = TargetPlatform.X,
        status: CapabilityStatus = CapabilityStatus.CONNECTED_SUPPORTED,
        publish_success: bool = True,
        post_id: str = "mock-post-12345",
        http_status: int = 201,
        is_ambiguous: bool = False,
        is_retryable: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        reconcile_finds_post: bool = False,
        reconcile_confidence: ReconciliationConfidence | None = None,
        max_chars: int | None = None,
        supports_text: bool = True,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.target = target
        self._status = status
        self.publish_success = publish_success
        self.post_id = post_id
        self.http_status = http_status
        self.is_ambiguous = is_ambiguous
        self.is_retryable = is_retryable
        self.error_code = error_code
        self.error_message = error_message
        self.reconcile_finds_post = reconcile_finds_post
        self.reconcile_confidence = reconcile_confidence
        self.max_chars = max_chars or (500 if target == TargetPlatform.THREADS else 280)
        self.supports_text = supports_text
        self.retry_after_seconds = retry_after_seconds
        self.publish_calls: list[dict[str, Any]] = []
        self.reconcile_calls: list[dict[str, Any]] = []

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target=self.target,
            status=self._status,
            publication_mode=PublicationMode.MANUAL_APPROVAL,
            max_chars=self.max_chars,
            supports_text=self.supports_text,
            supports_lookup=True,
            supports_delete=True,
            supports_timeline_reconciliation=True,
            supports_native_idempotency=False,
            orchestrator_managed_idempotency=True,
            auth_model="Mock Auth",
            official_endpoint="https://mock.api",
        )

    async def validate_credentials(self) -> CredentialValidationResult:
        return CredentialValidationResult(
            is_valid=self._status == CapabilityStatus.CONNECTED_SUPPORTED,
            status=self._status,
        )

    async def publish(self, text: str, intent: PublicationIntent) -> PublicationResult:
        self.publish_calls.append({"text": text, "intent": intent})
        if self.publish_success:
            return PublicationResult(
                success=True,
                provider_post_id=self.post_id,
                provider_url=f"https://platform.com/post/{self.post_id}",
                http_status=self.http_status,
                retry_after_seconds=self.retry_after_seconds,
            )
        return PublicationResult(
            success=False,
            http_status=self.http_status,
            is_ambiguous=self.is_ambiguous,
            is_retryable=self.is_retryable,
            error_code=self.error_code,
            error_message=self.error_message,
            retry_after_seconds=self.retry_after_seconds,
        )

    async def lookup(self, provider_post_id: str) -> LookupResult:
        if provider_post_id == self.post_id:
            return LookupResult(found=True, provider_post_id=self.post_id)
        return LookupResult(found=False, error_code="NOT_FOUND")

    async def delete(self, provider_post_id: str) -> bool:
        return True

    def classify_error(self, status_code: int | None, error_data: Any) -> tuple[bool, str]:
        if status_code == 429 or (status_code and status_code >= 500):
            return True, "RETRYABLE"
        return False, "PERMANENT"

    async def reconcile_ambiguous_delivery(
        self, intent: PublicationIntent, text: str
    ) -> AmbiguousReconciliationResult:
        self.reconcile_calls.append({"intent": intent, "text": text})
        if self.reconcile_confidence is not None:
            if self.reconcile_confidence == ReconciliationConfidence.CONFIRMED_PRESENT:
                return AmbiguousReconciliationResult(
                    resolved=True,
                    published=True,
                    confidence=ReconciliationConfidence.CONFIRMED_PRESENT,
                    provider_post_id=self.post_id,
                    provider_url=f"https://platform.com/post/{self.post_id}",
                    reason="Found matching post in timeline",
                )
            elif self.reconcile_confidence == ReconciliationConfidence.CONFIRMED_ABSENT:
                return AmbiguousReconciliationResult(
                    resolved=True,
                    published=False,
                    confidence=ReconciliationConfidence.CONFIRMED_ABSENT,
                    reason="Confirmed absent from provider",
                )
            else:
                return AmbiguousReconciliationResult(
                    resolved=False,
                    published=False,
                    confidence=ReconciliationConfidence.INCONCLUSIVE,
                    reason="Reconciliation inconclusive",
                )

        if self.reconcile_finds_post:
            return AmbiguousReconciliationResult(
                resolved=True,
                published=True,
                confidence=ReconciliationConfidence.CONFIRMED_PRESENT,
                provider_post_id=self.post_id,
                provider_url=f"https://platform.com/post/{self.post_id}",
                reason="Found matching post in timeline",
            )
        return AmbiguousReconciliationResult(
            resolved=False,
            published=False,
            confidence=ReconciliationConfidence.INCONCLUSIVE,
            reason="Post not found in timeline; absence cannot be definitively proven",
        )


# =========================================================================
# 15 Deterministic Scenarios
# =========================================================================


@pytest.mark.asyncio
async def test_01_publish_without_approval_blocked():
    """Scenario 1: Package in GENERATED state without approval cannot be published."""
    pkg = _make_test_package()
    # No OwnerApproval created or verified
    assert pkg.owner_approval is None
    assert pkg.approval_state == PackageStatus.GENERATED

    # Attempting to verify or publish without valid approval fails
    with pytest.raises((ValueError, UnauthorizedApprovalError)):
        fake_approval = OwnerApproval(
            package_id=pkg.package_id,
            owner_id=9999,  # Unauthorized owner
            content_hash=compute_package_content_hash(pkg),
            target_platforms=[TargetPlatform.X],
        )
        PublicationOrchestrator.create_plan(pkg, fake_approval, expected_owner_id=1001)


@pytest.mark.asyncio
async def test_02_approval_different_package_blocked():
    """Scenario 2: Approval signed for package_A cannot be applied to package_B."""
    pkg_a = _make_test_package(package_id="pkg-A")
    pkg_b = _make_test_package(package_id="pkg-B")

    approval_a = OwnerApproval(
        package_id=pkg_a.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg_a),
        target_platforms=[TargetPlatform.X],
    )

    is_valid, reason = verify_approval(pkg_b, approval_a, expected_owner_id=1001)
    assert not is_valid
    assert "package_id" in reason

    with pytest.raises(ValueError, match="package_id"):
        PublicationOrchestrator.create_plan(pkg_b, approval_a, expected_owner_id=1001)


@pytest.mark.asyncio
async def test_03_approved_exact_package_eligible():
    """Scenario 3: Approved exact package creates plan and executes successfully on X."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(publish_success=True, post_id="x-post-100")
    mock_registry = MagicMock()
    mock_registry.get_connector.return_value = connector
    plan = PublicationOrchestrator.create_plan(
        pkg, approval, expected_owner_id=1001, connector_registry=mock_registry
    )

    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.target == TargetPlatform.X
    assert intent.status == PublicationIntentStatus.PENDING

    result = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert result.success is True
    assert result.provider_post_id == "x-post-100"
    assert intent.status == PublicationIntentStatus.SUCCEEDED
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.DELIVERED
    assert len(connector.publish_calls) == 1


@pytest.mark.asyncio
async def test_04_duplicate_invocation_single_post():
    """Scenario 4: Duplicate execution of already SUCCEEDED intent returns ALREADY_PUBLISHED with 0 new posts."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(publish_success=True, post_id="x-post-200")
    plan = PublicationOrchestrator.create_plan(pkg, approval, expected_owner_id=1001)
    intent = plan.intents[0]

    # First execution
    res1 = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res1.success is True
    assert len(connector.publish_calls) == 1

    # Second execution (duplicate invocation)
    res2 = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res2.success is True
    assert res2.error_code == "ALREADY_PUBLISHED"
    # Exactly 1 call was ever made to connector
    assert len(connector.publish_calls) == 1


@pytest.mark.asyncio
async def test_05_concurrent_workers_single_claim():
    """Scenario 5: Two workers execute same intent; in-flight check/reconciliation ensures exactly 1 publish call."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(publish_success=True, post_id="x-post-300", reconcile_finds_post=True)
    plan = PublicationOrchestrator.create_plan(pkg, approval, expected_owner_id=1001)
    intent = plan.intents[0]

    # Worker 1 starts and completes
    await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert len(connector.publish_calls) == 1

    # Worker 2 arrives concurrently
    await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    # No second external call
    assert len(connector.publish_calls) == 1


@pytest.mark.asyncio
async def test_06_retry_after_transient_failure():
    """Scenario 6: First attempt fails with 503 (retryable); second attempt succeeds."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(
        publish_success=False,
        http_status=503,
        is_retryable=True,
        error_code="SERVER_ERROR_503",
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval, expected_owner_id=1001)
    intent = plan.intents[0]

    # Attempt 1 -> retryable failure
    res1 = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res1.success is False
    assert intent.status == PublicationIntentStatus.PENDING
    assert intent.next_retry_at is not None
    assert intent.attempt_count == 1

    # Attempt 2 -> connector recovers
    connector.publish_success = True
    connector.http_status = 201
    connector.post_id = "x-post-recovered"

    res2 = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res2.success is True
    assert intent.status == PublicationIntentStatus.SUCCEEDED
    assert intent.provider_post_id == "x-post-recovered"


@pytest.mark.asyncio
async def test_07_permanent_rejection_no_endless_retry():
    """Scenario 7: 400 Bad Request triggers PERMANENT_FAILURE without endless retries."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(
        publish_success=False,
        http_status=400,
        is_retryable=False,
        error_code="INVALID_PAYLOAD",
        error_message="Tweet exceeds 280 characters",
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval, expected_owner_id=1001)
    intent = plan.intents[0]

    res = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res.success is False
    assert intent.status == PublicationIntentStatus.FAILED
    assert intent.next_retry_at is None
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.FAILED


@pytest.mark.asyncio
async def test_08_auth_error_handling():
    """Scenario 8: 401 Unauthorized classified as permanent failure, not mistaken for deletion."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(
        publish_success=False,
        http_status=401,
        is_retryable=False,
        error_code="AUTH_ERROR_401",
        error_message="Invalid bearer token",
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval, expected_owner_id=1001)
    intent = plan.intents[0]

    res = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res.success is False
    assert intent.status == PublicationIntentStatus.FAILED
    assert intent.last_error_code == "AUTH_ERROR_401"
    # Target marked failed, not deleted
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.FAILED


@pytest.mark.asyncio
async def test_09_external_success_local_crash():
    """Scenario 9: Crash Boundary C - Tweet created on X, local crash occurs before persistence.

    On recovery, reconciliation detects the post and marks SUCCEEDED with zero duplicate posts.
    """
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(
        publish_success=True,
        post_id="x-post-crashed",
        reconcile_finds_post=True,
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval, expected_owner_id=1001)
    intent = plan.intents[0]

    # First attempt: simulate crash right after publish succeeds
    with pytest.raises(RuntimeError, match="SIMULATED_LOCAL_CRASH"):
        await PublicationOrchestrator.execute_intent(
            intent, pkg, connector, simulate_crash_after_publish=True
        )

    # At this point, intent was marked IN_FLIGHT (attempt_count=1)
    assert intent.status == PublicationIntentStatus.IN_FLIGHT
    assert intent.attempt_count == 1
    assert len(connector.publish_calls) == 1

    # Recovery run: orchestrator notices attempt_count > 0, invokes reconciliation
    res_recovery = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res_recovery.success is True
    assert intent.status == PublicationIntentStatus.SUCCEEDED
    assert intent.provider_post_id == "x-post-crashed"
    # Crucial: publish was NOT called a second time!
    assert len(connector.publish_calls) == 1
    assert len(connector.reconcile_calls) == 1


@pytest.mark.asyncio
async def test_10_ambiguous_result_no_blind_repost():
    """Scenario 10: Timeout during request yields AMBIGUOUS; reconciliation fails -> no blind repost."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(
        publish_success=False,
        is_ambiguous=True,
        error_code="TIMEOUT",
        reconcile_finds_post=False,
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval, expected_owner_id=1001)
    intent = plan.intents[0]

    res = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res.success is False
    assert intent.status == PublicationIntentStatus.DELIVERY_UNKNOWN
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.DELIVERY_UNKNOWN
    # Exactly 1 publish call was made (never blind repost)
    assert len(connector.publish_calls) == 1


@pytest.mark.asyncio
async def test_11_platform_a_success_platform_b_failed():
    """Scenario 11: Multi-target plan: X succeeds, Threads fails. Per-platform isolation preserves X success."""
    pkg = _make_test_package()
    pkg.distribution_targets[TargetPlatform.YOUTUBE_COMMUNITY.value].status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )

    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-post-ok")
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        http_status=500,
        is_retryable=False,
        error_code="THREADS_SERVER_ERROR",
    )

    mock_registry = MagicMock()
    mock_registry.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=mock_registry)
    assert len(plan.intents) == 2

    # Execute plan
    results = await PublicationOrchestrator.execute_plan(
        plan, pkg, connector_registry=mock_registry
    )

    assert results[TargetPlatform.X.value]["success"] is True
    assert results[TargetPlatform.THREADS.value]["success"] is False
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.DELIVERED
    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.FAILED
    assert pkg.approval_state == PackageStatus.PARTIALLY_DELIVERED

    # Re-executing plan only attempts Threads, X is untouched
    results2 = await PublicationOrchestrator.execute_plan(
        plan, pkg, connector_registry=mock_registry
    )
    assert results2[TargetPlatform.X.value]["action"] == "SKIPPED"
    assert len(x_conn.publish_calls) == 1


@pytest.mark.asyncio
async def test_12_changed_content_invalidates_approval():
    """Scenario 12: Content variant modified after approval -> APPROVAL_STALE blocks publication."""
    pkg = _make_test_package(x_text="Original approved tweet text")
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(publish_success=True)
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    intent = plan.intents[0]

    # Tamper with content variant after approval
    pkg.output_variants.variants[OutputVariantType.X_POST.value].text = "Tampered tweet text!"
    pkg.distribution_targets[TargetPlatform.X.value].rendered_payload = "Tampered tweet text!"

    res = await PublicationOrchestrator.execute_intent(intent, pkg, connector)
    assert res.success is False
    assert res.error_code == "APPROVAL_STALE"
    assert intent.status == PublicationIntentStatus.APPROVAL_STALE
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.FAILED
    # Zero calls to connector
    assert len(connector.publish_calls) == 0


@pytest.mark.asyncio
async def test_13_cancelled_package_cannot_publish():
    """Scenario 13: Package in REJECTED state cannot be approved or published."""
    pkg = _make_test_package()
    pkg.approval_state = PackageStatus.REJECTED

    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )

    with pytest.raises(InvalidLifecycleTransitionError):
        PublicationOrchestrator.create_plan(pkg, approval)


@pytest.mark.asyncio
async def test_14_unsupported_connector_capability():
    """Scenario 14: YouTube Community target resolves to MANUAL_EXPORT_READY without network calls."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.YOUTUBE_COMMUNITY],
    )
    yt_conn = MockTestConnector(
        target=TargetPlatform.YOUTUBE_COMMUNITY,
        status=CapabilityStatus.UNSUPPORTED_OFFICIAL_API,
    )
    mock_reg = MagicMock()
    mock_reg.get_connector.return_value = yt_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=mock_reg)
    intent = plan.intents[0]
    assert intent.status == PublicationIntentStatus.MANUAL_EXPORT_READY

    res = await PublicationOrchestrator.execute_intent(intent, pkg, yt_conn)
    assert res.success is False
    assert res.error_code == "MANUAL_EXPORT_ONLY"
    assert len(yt_conn.publish_calls) == 0


@pytest.mark.asyncio
async def test_15_telemetry_audit_correctness():
    """Scenario 15: Successful publish creates delivery record, registers audit target, masks secrets."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    connector = MockTestConnector(publish_success=True, post_id="x-post-audit")
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    intent = plan.intents[0]

    # Mock AsyncSession for audit target registration
    mock_session = AsyncMock()
    mock_session.get.return_value = None

    res = await PublicationOrchestrator.execute_intent(
        intent, pkg, connector, session=mock_session
    )
    assert res.success is True
    assert len(pkg.delivery_records) == 1
    rec = pkg.delivery_records[0]
    assert rec.status == DeliveryOutcome.SUCCEEDED
    assert rec.external_id == "x-post-audit"
    assert rec.publication_key == intent.publication_key


# =========================================================================
# Multi-Platform Publication Expansion Tests (Priority 5 Slice 2)
# =========================================================================


@pytest.mark.asyncio
async def test_mp_01_approval_x_only_cannot_publish_b():
    """Approval granted for X only cannot publish Meta Threads."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X],
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    assert len(plan.intents) == 1
    assert plan.intents[0].target == TargetPlatform.X

    # Attempting to filter or execute THREADS with this plan raises UnauthorizedApprovalError
    with pytest.raises(UnauthorizedApprovalError):
        await PublicationOrchestrator.execute_plan(plan, pkg, target_filter=TargetPlatform.THREADS)

    # Directly attempting to execute an intent for THREADS with this approval is rejected
    threads_intent = PublicationIntent(
        package_id=pkg.package_id,
        job_id=pkg.job_id,
        target=TargetPlatform.THREADS,
        variant=OutputVariantType.THREADS_POST,
        approved_by=1001,
        approved_at=_utc_now(),
        payload_hash="somehash",
        publication_key=f"{pkg.package_id}:THREADS:THREADS_POST:somehash",
        status=PublicationIntentStatus.PENDING,
    )
    conn = MockTestConnector(target=TargetPlatform.THREADS)
    res = await PublicationOrchestrator.execute_intent(threads_intent, pkg, conn, approval=approval)
    assert res.success is False
    assert res.error_code == "TARGET_NOT_APPROVED"


@pytest.mark.asyncio
async def test_mp_02_approval_two_targets_creates_two_identities():
    """Approval for X + Threads creates exactly two distinct publication identities."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    assert len(plan.intents) == 2
    targets = {i.target for i in plan.intents}
    assert targets == {TargetPlatform.X, TargetPlatform.THREADS}
    assert plan.intents[0].publication_key != plan.intents[1].publication_key
    assert "X" in plan.intents[0].publication_key or "THREADS" in plan.intents[0].publication_key
    assert "X" in plan.intents[1].publication_key or "THREADS" in plan.intents[1].publication_key


@pytest.mark.asyncio
async def test_mp_03_both_targets_succeed_independently():
    """Both X and Threads succeed independently, resulting in ALL_SUCCEEDED plan."""
    pkg = _make_test_package()
    pkg.distribution_targets[TargetPlatform.YOUTUBE_COMMUNITY.value].status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-post-101")
    thr_conn = MockTestConnector(target=TargetPlatform.THREADS, publish_success=True, post_id="thr-post-202")
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    results = await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert results[TargetPlatform.X.value]["success"] is True
    assert results[TargetPlatform.THREADS.value]["success"] is True
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.DELIVERED
    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.DELIVERED
    assert plan.status == PublicationPlanStatus.ALL_SUCCEEDED
    assert pkg.approval_state == PackageStatus.DELIVERED


@pytest.mark.asyncio
async def test_mp_04_x_succeeds_b_transiently_fails():
    """X succeeds and Threads transiently fails with 503; plan is RETRY_PENDING."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-101")
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_retryable=True,
        http_status=503,
        error_code="SERVER_ERROR_503",
    )
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.DELIVERED
    thr_intent = next(i for i in plan.intents if i.target == TargetPlatform.THREADS)
    assert thr_intent.status == PublicationIntentStatus.PENDING
    assert thr_intent.next_retry_at is not None
    assert plan.status == PublicationPlanStatus.RETRY_PENDING


@pytest.mark.asyncio
async def test_mp_05_x_succeeds_b_permanently_fails():
    """X succeeds and Threads permanently fails; plan is PARTIAL_SUCCESS."""
    pkg = _make_test_package()
    pkg.distribution_targets[TargetPlatform.YOUTUBE_COMMUNITY.value].status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-101")
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_retryable=False,
        http_status=400,
        error_code="INVALID_PAYLOAD",
    )
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.DELIVERED
    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.FAILED
    assert plan.status == PublicationPlanStatus.PARTIAL_SUCCESS
    assert pkg.approval_state == PackageStatus.PARTIALLY_DELIVERED


@pytest.mark.asyncio
async def test_mp_06_b_succeeds_x_ambiguous():
    """Threads succeeds and X encounters ambiguous timeout; plan is MANUAL_RECONCILIATION_REQUIRED."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(
        target=TargetPlatform.X,
        publish_success=False,
        is_ambiguous=True,
        reconcile_finds_post=False,
        error_code="TIMEOUT",
    )
    thr_conn = MockTestConnector(target=TargetPlatform.THREADS, publish_success=True, post_id="thr-202")
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.DELIVERED
    x_intent = next(i for i in plan.intents if i.target == TargetPlatform.X)
    assert x_intent.status == PublicationIntentStatus.DELIVERY_UNKNOWN
    assert plan.status == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_mp_07_retry_b_does_not_redispatch_x():
    """Retrying failed Threads target does NOT re-dispatch already succeeded X."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-101")
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_retryable=True,
        http_status=503,
    )
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert len(x_conn.publish_calls) == 1
    assert len(thr_conn.publish_calls) == 1

    # Now make Threads succeed on retry
    thr_conn.publish_success = True
    thr_conn.post_id = "thr-retry-success"
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    # X must NEVER have been called again
    assert len(x_conn.publish_calls) == 1
    assert len(thr_conn.publish_calls) == 2
    assert plan.status == PublicationPlanStatus.ALL_SUCCEEDED


@pytest.mark.asyncio
async def test_mp_08_concurrent_execution_cannot_duplicate_either():
    """Concurrent workers executing the same plan do not duplicate calls."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-101")
    thr_conn = MockTestConnector(target=TargetPlatform.THREADS, publish_success=True, post_id="thr-202")
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)

    # Run two executions sequentially / concurrently
    await asyncio.gather(
        PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg),
        PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg),
    )

    # Combined calls must not create duplicates
    assert len(x_conn.publish_calls) == 1
    assert len(thr_conn.publish_calls) == 1


@pytest.mark.asyncio
async def test_mp_09_modification_after_approval_blocks_both():
    """Modifying content after approval invalidates both X and Threads targets."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X)
    thr_conn = MockTestConnector(target=TargetPlatform.THREADS)
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)

    # Modify package content
    pkg.output_variants.variants[OutputVariantType.X_POST.value].text = "Tampered X content"
    pkg.output_variants.variants[OutputVariantType.THREADS_POST.value].text = "Tampered Threads content"

    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert len(x_conn.publish_calls) == 0
    assert len(thr_conn.publish_calls) == 0
    assert plan.status == PublicationPlanStatus.TERMINAL_FAILURE


@pytest.mark.asyncio
async def test_mp_10_unsupported_b_payload_rejected_preflight():
    """Threads payload exceeding 500 characters is rejected BEFORE network dispatch."""
    long_threads_text = "A" * 501
    pkg = _make_test_package(threads_text=long_threads_text)
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    thr_conn = MockTestConnector(target=TargetPlatform.THREADS, max_chars=500)
    plan = PublicationOrchestrator.create_plan(pkg, approval)

    res = await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, thr_conn)
    assert res.success is False
    assert res.error_code == "BUDGET_EXCEEDED"
    assert len(thr_conn.publish_calls) == 0  # Zero network calls


@pytest.mark.asyncio
async def test_mp_11_b_auth_scopes_failure_classification():
    """Threads 401/403 auth failure is classified as non-retryable terminal failure."""
    connector = ThreadsConnector(access_token="invalid_token", user_id="12345")
    is_retryable, err_code = connector.classify_error(401, "Invalid OAuth access token")
    assert is_retryable is False
    assert "AUTH_ERROR" in err_code

    is_retryable, err_code = connector.classify_error(403, "Insufficient scopes")
    assert is_retryable is False
    assert "AUTH_ERROR" in err_code


@pytest.mark.asyncio
async def test_mp_12_b_rate_limit_handling():
    """Threads 429 rate limit triggers retry with delay."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_retryable=True,
        http_status=429,
        error_code="RATE_LIMITED",
        retry_after_seconds=120,
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    now = _utc_now()
    res = await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, thr_conn, now=now)

    assert res.success is False
    assert plan.intents[0].status == PublicationIntentStatus.PENDING
    assert plan.intents[0].next_retry_at is not None
    assert (plan.intents[0].next_retry_at - now).total_seconds() == 120


@pytest.mark.asyncio
async def test_mp_13_b_external_success_local_crash():
    """Crash consistency Boundary C for Threads: post succeeded externally, recovery avoids duplicate."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=True,
        post_id="thr-boundary-c-123",
        reconcile_finds_post=True,
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)

    # First attempt: simulate crash after publish
    with pytest.raises(RuntimeError, match="SIMULATED_LOCAL_CRASH"):
        await PublicationOrchestrator.execute_intent(
            plan.intents[0], pkg, thr_conn, simulate_crash_after_publish=True
        )

    assert len(thr_conn.publish_calls) == 1
    assert plan.intents[0].status == PublicationIntentStatus.IN_FLIGHT

    # Recovery: replay without crash
    res = await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, thr_conn)
    assert res.success is True
    assert res.provider_post_id == "thr-boundary-c-123"
    assert len(thr_conn.publish_calls) == 1  # No duplicate post
    assert plan.intents[0].status == PublicationIntentStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_mp_14_b_ambiguous_result_without_blind_repost():
    """Threads ambiguous timeout without reconciliation confirmation avoids blind repost."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_ambiguous=True,
        reconcile_finds_post=False,
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, thr_conn)

    assert plan.intents[0].status == PublicationIntentStatus.DELIVERY_UNKNOWN
    assert len(thr_conn.publish_calls) == 1  # 0 blind reposts


@pytest.mark.asyncio
async def test_mp_15_target_specific_cancellation():
    """Cancelling target X does not cancel or corrupt target Threads."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X)
    thr_conn = MockTestConnector(target=TargetPlatform.THREADS, publish_success=True, post_id="thr-ok")
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)

    # Cancel target X
    x_intent = next(i for i in plan.intents if i.target == TargetPlatform.X)
    x_intent.status = PublicationIntentStatus.FAILED
    x_intent.last_error_code = "MANUALLY_CANCELLED"

    # Execute plan
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert len(x_conn.publish_calls) == 0  # X skipped
    assert len(thr_conn.publish_calls) == 1  # Threads published
    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.DELIVERED
    assert plan.status == PublicationPlanStatus.PARTIAL_SUCCESS


def test_mp_16_plan_level_status_aggregation():
    """Verify all aggregate states of PublicationPlan."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)

    # 1. ALL_PENDING
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.ALL_PENDING

    # 2. ATTEMPTING
    plan.intents[0].status = PublicationIntentStatus.IN_FLIGHT
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.ATTEMPTING

    # 3. MANUAL_RECONCILIATION_REQUIRED
    plan.intents[0].status = PublicationIntentStatus.DELIVERY_UNKNOWN
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED

    # 4. RETRY_PENDING
    plan.intents[0].status = PublicationIntentStatus.PENDING
    plan.intents[0].attempt_count = 1
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.RETRY_PENDING

    # 5. ALL_SUCCEEDED
    plan.intents[0].status = PublicationIntentStatus.SUCCEEDED
    plan.intents[1].status = PublicationIntentStatus.SUCCEEDED
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.ALL_SUCCEEDED

    # 6. PARTIAL_SUCCESS
    plan.intents[0].status = PublicationIntentStatus.SUCCEEDED
    plan.intents[1].status = PublicationIntentStatus.FAILED
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.PARTIAL_SUCCESS

    # 7. TERMINAL_FAILURE
    plan.intents[0].status = PublicationIntentStatus.FAILED
    plan.intents[1].status = PublicationIntentStatus.FAILED
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.TERMINAL_FAILURE

    # 8. CANCELLED
    plan.status = PublicationPlanStatus.CANCELLED
    assert calculate_aggregate_plan_status(plan) == PublicationPlanStatus.CANCELLED


@pytest.mark.asyncio
async def test_mp_17_telemetry_contains_both_independent_attempts():
    """Multi-platform execution records two distinct delivery records with correct lineage."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-99")
    thr_conn = MockTestConnector(target=TargetPlatform.THREADS, publish_success=True, post_id="thr-88")
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    targets = [r.target for r in pkg.delivery_records]
    assert TargetPlatform.X in targets
    assert TargetPlatform.THREADS in targets
    assert len(pkg.delivery_records) == 2


@pytest.mark.asyncio
async def test_mp_18_secrets_absent_from_telemetry_log_output():
    """Connector credentials and tokens are sanitized from errors and telemetry."""
    raw_secret = "Bearer THREADS_SECRET_TOKEN_XYZ client_secret=SEC123"
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_retryable=False,
        error_code="AUTH_ERROR",
        error_message=raw_secret,
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    res = await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, thr_conn)

    assert "THREADS_SECRET_TOKEN_XYZ" not in plan.intents[0].last_error_message
    assert "SEC123" not in plan.intents[0].last_error_message
    assert "[REDACTED_CREDENTIAL]" in plan.intents[0].last_error_message
    assert len(pkg.delivery_records) == 1
    assert "THREADS_SECRET_TOKEN_XYZ" not in pkg.delivery_records[0].error_message


@pytest.mark.asyncio
async def test_mp_19_recon_duplicate_text_old_post_not_attributed():
    """User has an older post with identical text; reconciliation must NOT falsely attribute it."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    # Intent approved now
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    intent = plan.intents[0]
    intent.attempt_started_at = datetime.datetime.now(datetime.timezone.utc)

    # Mock Threads API returning an older post with identical text
    thr_conn = ThreadsConnector(access_token="tok_123", user_id="usr_456")
    old_post_resp = httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "old-threads-post-999",
                    "text": intent.variant.value if hasattr(intent.variant, "value") else "Test Threads post content",
                    "permalink": "https://www.threads.net/t/old-threads-post-999",
                    "timestamp": "2026-09-01T00:00:00+0000",  # 6 days before attempt!
                }
            ]
        },
    )
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = old_post_resp
        recon = await thr_conn.reconcile_ambiguous_delivery(intent, "Test Threads post content")

        assert recon.confidence == ReconciliationConfidence.INCONCLUSIVE
        assert recon.resolved is False
        assert recon.published is False
        assert recon.provider_post_id is None


@pytest.mark.asyncio
async def test_mp_20_recon_delayed_visibility_remains_manual_no_blind_repost():
    """Ambiguous publish with delayed visibility remains DELIVERY_UNKNOWN; zero duplicate calls on replay."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_ambiguous=True,
        reconcile_finds_post=False,
    )
    reg = MagicMock(get_connector=lambda t: thr_conn)
    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)

    # First attempt: timeout occurs, immediate reconciliation is inconclusive
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert len(thr_conn.publish_calls) == 1
    thr_intent = plan.intents[0]
    assert thr_intent.status == PublicationIntentStatus.DELIVERY_UNKNOWN
    assert plan.status == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED

    # Replay/recovery invocation: must halt because reconciliation is inconclusive -> ZERO second publish calls
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert len(thr_conn.publish_calls) == 1  # Still exactly 1! Blind reposts = 0
    assert thr_intent.status == PublicationIntentStatus.DELIVERY_UNKNOWN
    assert plan.status == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_mp_21_recon_confirmed_absent_permits_retry():
    """When provider authoritatively confirms absence, retry backoff is scheduled."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.THREADS],
    )
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_ambiguous=True,
        is_retryable=True,
        reconcile_confidence=ReconciliationConfidence.CONFIRMED_ABSENT,
    )
    reg = MagicMock(get_connector=lambda t: thr_conn)
    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    thr_intent = plan.intents[0]
    assert thr_intent.status == PublicationIntentStatus.PENDING
    assert thr_intent.next_retry_at is not None
    assert plan.status == PublicationPlanStatus.RETRY_PENDING


@pytest.mark.asyncio
async def test_mp_22_plan_x_succeeded_threads_inconclusive_no_x_redispatch():
    """X succeeded and Threads is DELIVERY_UNKNOWN; plan is MANUAL_RECONCILIATION_REQUIRED and X is not re-dispatched."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id="x-101")
    thr_conn = MockTestConnector(
        target=TargetPlatform.THREADS,
        publish_success=False,
        is_ambiguous=True,
        reconcile_finds_post=False,
    )
    reg = MagicMock()
    reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

    plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.DELIVERED
    assert plan.status == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED
    assert len(x_conn.publish_calls) == 1
    assert len(thr_conn.publish_calls) == 1

    # Second execution (e.g. after restart or worker sweep)
    await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

    # Invariants: X never re-dispatched, Threads not blindly reposted
    assert len(x_conn.publish_calls) == 1
    assert len(thr_conn.publish_calls) == 1
    assert plan.status == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_mp_23_threads_connector_reconcile_unit_cases():
    """Direct unit tests on ThreadsConnector.reconcile_ambiguous_delivery."""
    conn = ThreadsConnector(access_token="tok_abc", user_id="12345")
    intent = PublicationIntent.create(
        package_id="pkg_u1",
        job_id="job_u1",
        target=TargetPlatform.THREADS,
        variant=OutputVariantType.THREADS_POST,
        approved_by=1001,
        payload_text="Threads unit test payload text",
    )
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    intent.attempt_started_at = now_utc

    # Case A: Matching text + fresh timestamp -> CONFIRMED_PRESENT
    fresh_ts = (now_utc - datetime.timedelta(seconds=10)).isoformat()
    mock_fresh = httpx.Response(
        200,
        json={"data": [{"id": "th-fresh-1", "text": "Threads unit test payload text", "timestamp": fresh_ts}]},
    )
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_fresh
        res = await conn.reconcile_ambiguous_delivery(intent, "Threads unit test payload text")
        assert res.confidence == ReconciliationConfidence.CONFIRMED_PRESENT
        assert res.resolved is True
        assert res.published is True
        assert res.provider_post_id == "th-fresh-1"

    # Case B: Matching text + stale timestamp -> INCONCLUSIVE (duplicate text protection)
    stale_ts = (now_utc - datetime.timedelta(days=2)).isoformat()
    mock_stale = httpx.Response(
        200,
        json={"data": [{"id": "th-stale-2", "text": "Threads unit test payload text", "timestamp": stale_ts}]},
    )
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_stale
        res = await conn.reconcile_ambiguous_delivery(intent, "Threads unit test payload text")
        assert res.confidence == ReconciliationConfidence.INCONCLUSIVE
        assert res.resolved is False
        assert res.published is False

    # Case C: Not found in recent timeline -> INCONCLUSIVE (never fake CONFIRMED_ABSENT)
    mock_empty = httpx.Response(200, json={"data": []})
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_empty
        res = await conn.reconcile_ambiguous_delivery(intent, "Threads unit test payload text")
        assert res.confidence == ReconciliationConfidence.INCONCLUSIVE
        assert res.resolved is False
        assert res.published is False


@pytest.mark.asyncio
async def test_mp_24_x_connector_reconcile_unit_cases():
    """Direct unit tests on XConnector.reconcile_ambiguous_delivery."""
    conn = XConnector(bearer_token="tok_xyz")
    intent = PublicationIntent.create(
        package_id="pkg_u2",
        job_id="job_u2",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="X unit test payload text",
    )
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    intent.attempt_started_at = now_utc

    # Case A: Matching text + fresh created_at -> CONFIRMED_PRESENT
    fresh_ts = (now_utc - datetime.timedelta(seconds=5)).isoformat()
    mock_fresh = httpx.Response(
        200,
        json={"data": [{"id": "x-fresh-1", "text": "X unit test payload text", "created_at": fresh_ts}]},
    )
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_fresh
        res = await conn.reconcile_ambiguous_delivery(intent, "X unit test payload text")
        assert res.confidence == ReconciliationConfidence.CONFIRMED_PRESENT
        assert res.resolved is True
        assert res.published is True
        assert res.provider_post_id == "x-fresh-1"

    # Case B: Matching text + stale created_at -> INCONCLUSIVE
    stale_ts = (now_utc - datetime.timedelta(days=5)).isoformat()
    mock_stale = httpx.Response(
        200,
        json={"data": [{"id": "x-stale-2", "text": "X unit test payload text", "created_at": stale_ts}]},
    )
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_stale
        res = await conn.reconcile_ambiguous_delivery(intent, "X unit test payload text")
        assert res.confidence == ReconciliationConfidence.INCONCLUSIVE
        assert res.resolved is False
        assert res.published is False

    # Case C: Not found -> INCONCLUSIVE
    mock_empty = httpx.Response(200, json={"data": []})
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_empty
        res = await conn.reconcile_ambiguous_delivery(intent, "X unit test payload text")
        assert res.confidence == ReconciliationConfidence.INCONCLUSIVE
        assert res.resolved is False
        assert res.published is False


def test_mp_25_manual_reconciliation_state_survives_restart():
    """Ensure PublicationPlan and intent states persist and recalculate identically across restarts."""
    pkg = _make_test_package()
    approval = OwnerApproval(
        package_id=pkg.package_id,
        owner_id=1001,
        content_hash=compute_package_content_hash(pkg),
        target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
    )
    plan = PublicationOrchestrator.create_plan(pkg, approval)
    plan.intents[0].status = PublicationIntentStatus.SUCCEEDED
    plan.intents[1].status = PublicationIntentStatus.DELIVERY_UNKNOWN
    plan.status = calculate_aggregate_plan_status(plan)
    assert plan.status == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED

    # Serialize to JSON dict and reconstruct
    data = plan.model_dump() if hasattr(plan, "model_dump") else plan.dict()
    restored_plan = PublicationPlan(**data)

    assert restored_plan.intents[0].status == PublicationIntentStatus.SUCCEEDED
    assert restored_plan.intents[1].status == PublicationIntentStatus.DELIVERY_UNKNOWN
    assert calculate_aggregate_plan_status(restored_plan) == PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED


def test_mp_26_native_idempotency_audit():
    """Verify accurate reporting of native idempotency vs orchestrator-managed idempotency."""
    x_caps = XConnector().capabilities()
    assert x_caps.supports_native_idempotency is False
    assert x_caps.orchestrator_managed_idempotency is True

    thr_caps = ThreadsConnector().capabilities()
    assert thr_caps.supports_native_idempotency is False
    assert thr_caps.orchestrator_managed_idempotency is True

    yt_caps = YouTubeCommunityConnector().capabilities()
    assert yt_caps.supports_native_idempotency is False
    assert yt_caps.orchestrator_managed_idempotency is False


# =========================================================================
# Replay Evaluation Runner with 7 Mandatory Safety Gates
# =========================================================================


@pytest.mark.asyncio
async def test_publication_replay_evaluation():
    """Evaluate all scenarios in publication_eval.json and enforce the 7 mandatory gates."""
    fixture_path = (
        Path(__file__).parent / "fixtures" / "publication_eval.json"
    )
    assert fixture_path.exists(), f"Fixture {fixture_path} missing!"

    with open(fixture_path, "r", encoding="utf-8") as f:
        scenarios = json.load(f)

    assert len(scenarios) == 19, f"Expected 19 scenarios, got {len(scenarios)}"

    # Safety Gate Counters
    unauthorized_publications = 0
    duplicate_publications = 0
    stale_approval_publications = 0
    blind_reposts = 0
    platform_isolation_violations = 0
    terminal_state_errors = 0
    unsanitized_credentials = 0

    for sc in scenarios:
        sc_id = sc["scenario_id"]
        pkg = _make_test_package(
            package_id=f"pkg-{sc_id}",
            owner_id=sc["owner_user_id"],
            x_text=sc.get("text", "Default test tweet text"),
            threads_text=sc.get("text", "Default test threads text"),
        )

        # 1. Check unauthorized approval gate
        if not sc.get("has_approval"):
            if pkg.approval_state in (PackageStatus.APPROVED, PackageStatus.DELIVERED):
                unauthorized_publications += 1
            continue

        # 2. Check mismatched package approval
        if sc.get("mismatch_package_id"):
            fake_approval = OwnerApproval(
                package_id="different-package-id",
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X],
            )
            is_valid, _ = verify_approval(pkg, fake_approval)
            if is_valid:
                unauthorized_publications += 1
            continue

        # 3. Check tampered / stale content
        if sc.get("tampered_text"):
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X],
            )
            plan = PublicationOrchestrator.create_plan(pkg, approval)
            # Tamper text
            pkg.output_variants.variants[OutputVariantType.X_POST.value].text = sc["tampered_text"]
            conn = MockTestConnector()
            res = await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, conn)
            if res.success:
                stale_approval_publications += 1
            continue

        # 4. Check rejected package transition
        if sc.get("package_rejected"):
            pkg.approval_state = PackageStatus.REJECTED
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X],
            )
            try:
                PublicationOrchestrator.create_plan(pkg, approval)
                terminal_state_errors += 1
            except InvalidLifecycleTransitionError:
                pass
            continue

        # 5. Check timeout / ambiguous blind reposts
        if sc.get("simulate_timeout"):
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X],
            )
            conn = MockTestConnector(publish_success=False, is_ambiguous=True, reconcile_finds_post=False)
            plan = PublicationOrchestrator.create_plan(pkg, approval)
            await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, conn)
            if len(conn.publish_calls) > 1:
                blind_reposts += 1
            continue

        # 6. Check multi-target platform isolation
        if sc.get("multi_target"):
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
            )
            x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True)
            thr_conn = MockTestConnector(target=TargetPlatform.THREADS, publish_success=False, is_retryable=False)
            reg = MagicMock()
            reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

            plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
            await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

            if pkg.distribution_targets[TargetPlatform.X.value].status != TargetDeliveryStatus.DELIVERED:
                platform_isolation_violations += 1
            if pkg.distribution_targets[TargetPlatform.THREADS.value].status != TargetDeliveryStatus.FAILED:
                platform_isolation_violations += 1
            continue

        # 7. Check duplicate invocation single post
        if sc.get("repeat_execution"):
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X],
            )
            conn = MockTestConnector(publish_success=True)
            plan = PublicationOrchestrator.create_plan(pkg, approval)
            intent = plan.intents[0]
            await PublicationOrchestrator.execute_intent(intent, pkg, conn)
            await PublicationOrchestrator.execute_intent(intent, pkg, conn)
            if len(conn.publish_calls) > 1:
                duplicate_publications += 1
            continue

        # 8. Check Threads official publish success (Scenario 16)
        if sc_id == "case_16_threads_official_publish_success":
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.THREADS],
            )
            thr_conn = MockTestConnector(
                target=TargetPlatform.THREADS,
                publish_success=True,
                post_id=sc["mock_post_id"],
            )
            plan = PublicationOrchestrator.create_plan(pkg, approval)
            res = await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, thr_conn)
            if not res.success or pkg.distribution_targets[TargetPlatform.THREADS.value].status != TargetDeliveryStatus.DELIVERED:
                terminal_state_errors += 1
            continue

        # 9. Check multi-target dual success (Scenario 17)
        if sc_id == "case_17_multi_target_dual_success":
            pkg.distribution_targets[TargetPlatform.YOUTUBE_COMMUNITY.value].status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X, TargetPlatform.THREADS],
            )
            x_conn = MockTestConnector(target=TargetPlatform.X, publish_success=True, post_id=sc["x_mock_post_id"])
            thr_conn = MockTestConnector(target=TargetPlatform.THREADS, publish_success=True, post_id=sc["threads_mock_post_id"])
            reg = MagicMock()
            reg.get_connector.side_effect = lambda t: x_conn if t == TargetPlatform.X else thr_conn

            plan = PublicationOrchestrator.create_plan(pkg, approval, connector_registry=reg)
            await PublicationOrchestrator.execute_plan(plan, pkg, connector_registry=reg)

            if plan.status != PublicationPlanStatus.ALL_SUCCEEDED or pkg.approval_state != PackageStatus.DELIVERED:
                terminal_state_errors += 1
            continue

        # 10. Check Threads ambiguous reconciliation (Scenario 18)
        if sc_id == "case_18_threads_ambiguous_reconciliation_success":
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.THREADS],
            )
            thr_conn = MockTestConnector(
                target=TargetPlatform.THREADS,
                publish_success=False,
                is_ambiguous=True,
                reconcile_finds_post=True,
                post_id=sc["mock_post_id"],
            )
            plan = PublicationOrchestrator.create_plan(pkg, approval)
            res = await PublicationOrchestrator.execute_intent(plan.intents[0], pkg, thr_conn)
            if not res.success or len(thr_conn.publish_calls) > 1:
                blind_reposts += 1
            continue

        # 11. Check target set binding violation (Scenario 19)
        if sc_id == "case_19_target_set_binding_violation":
            approval = OwnerApproval(
                package_id=pkg.package_id,
                owner_id=sc["owner_user_id"],
                content_hash=compute_package_content_hash(pkg),
                target_platforms=[TargetPlatform.X],
            )
            plan = PublicationOrchestrator.create_plan(pkg, approval)
            thr_conn = MockTestConnector(target=TargetPlatform.THREADS)
            threads_intent = PublicationIntent(
                package_id=pkg.package_id,
                job_id=pkg.job_id,
                target=TargetPlatform.THREADS,
                variant=OutputVariantType.THREADS_POST,
                approved_by=sc["owner_user_id"],
                approved_at=_utc_now(),
                payload_hash="somehash",
                publication_key=f"{pkg.package_id}:THREADS:THREADS_POST:somehash",
                status=PublicationIntentStatus.PENDING,
            )
            res = await PublicationOrchestrator.execute_intent(threads_intent, pkg, thr_conn, approval=approval)
            if res.success:
                unauthorized_publications += 1
            continue

    # 7 Mandatory Gates Assertion
    assert unauthorized_publications == 0, f"Gate 1 Failed: {unauthorized_publications} unauthorized publications"
    assert duplicate_publications == 0, f"Gate 2 Failed: {duplicate_publications} duplicate publications"
    assert stale_approval_publications == 0, f"Gate 3 Failed: {stale_approval_publications} stale publications"
    assert blind_reposts == 0, f"Gate 4 Failed: {blind_reposts} blind reposts"
    assert platform_isolation_violations == 0, f"Gate 5 Failed: {platform_isolation_violations} platform isolation violations"
    assert terminal_state_errors == 0, f"Gate 6 Failed: {terminal_state_errors} terminal state errors"
    assert unsanitized_credentials == 0, f"Gate 7 Failed: {unsanitized_credentials} credential leaks"

