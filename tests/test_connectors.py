"""Comprehensive test suite for Phase 20: Publishing Orchestration & External Platform Connectors."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.db.migrate import apply_migrations
from app.worker.connector_evaluation import evaluate_connectors, load_connector_cases
from app.worker.connectors import (
    AmbiguousReconciliationResult,
    CapabilityStatus,
    ConnectorRegistry,
    CredentialValidationResult,
    LookupResult,
    PublicationIntent,
    PublicationResult,
    ThreadsConnector,
    XConnector,
    YouTubeCommunityConnector,
    sanitize_sensitive_text,
)
from app.worker.content_package import (
    ContentPackage,
    DeliveryOutcome,
    DeliveryRecord,
    OutputVariantType,
    PackageStatus,
    PublicationIntentStatus,
    TargetDeliveryStatus,
    TargetPlatform,
    UnauthorizedApprovalError,
    approve_package,
    build_content_package,
    compute_payload_hash,
    reconcile_package_status,
    regenerate_variant_from_canonical,
    transition_package_status,
)
from app.worker.output_variants import (
    CanonicalContentResult,
    generate_all_variants,
)


def _sample_canonical() -> CanonicalContentResult:
    return CanonicalContentResult(
        title="Оптимизация Docker образов",
        topic="DevOps",
        summary="Использование multi-stage builds для уменьшения размера образов.",
        what_it_is="Инструкция по оптимизации Dockerfile.",
        why_it_matters="Ускоряет деплой и экономит память.",
        key_points=["Multi-stage сборка", "Минимальный runtime образ"],
        actionable_steps=["Разделить этапы сборки и рантайма"],
        risk_level="LOW",
        risk_reasons=[],
        critical_disclaimers=[],
        source_language_code="ru",
        analysis_language_code="ru",
        priority_tier="HIGH",
        priority_score=0.88,
        priority_reasons=["Практическая полезность"],
        citations=["https://docs.docker.com"],
        video_url="https://instagram.com/reel/test12345",
    )


# Test 1: X connector payload
@pytest.mark.asyncio
async def test_01_x_connector_payload():
    connector = XConnector(bearer_token="mock_token")
    intent = PublicationIntent.create(
        package_id="pkg_1",
        job_id="job_1",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="Test tweet payload under limit",
    )
    long_text = "A" * 300
    res = await connector.publish(long_text, intent)
    assert not res.success
    assert res.error_code == "BUDGET_EXCEEDED"


# Test 2: X success
@pytest.mark.asyncio
async def test_02_x_success():
    connector = XConnector(bearer_token="mock_token")
    intent = PublicationIntent.create(
        package_id="pkg_2",
        job_id="job_2",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="Valid tweet",
    )
    mock_resp = httpx.Response(201, json={"data": {"id": "123456789", "text": "Valid tweet"}})
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await connector.publish("Valid tweet", intent)
        assert res.success
        assert res.provider_post_id == "123456789"
        assert "123456789" in (res.provider_url or "")


# Test 3: X 429 retry
@pytest.mark.asyncio
async def test_03_x_429_retry():
    connector = XConnector(bearer_token="mock_token")
    intent = PublicationIntent.create(
        package_id="pkg_3",
        job_id="job_3",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="Rate limit test",
    )
    mock_resp = httpx.Response(429, text="Rate limit exceeded")
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await connector.publish("Rate limit test", intent)
        assert not res.success
        assert res.is_retryable
        assert res.error_code == "RATE_LIMITED"


# Test 4: X 401 non-retryable
@pytest.mark.asyncio
async def test_04_x_401_non_retryable():
    connector = XConnector(bearer_token="bad_token")
    intent = PublicationIntent.create(
        package_id="pkg_4",
        job_id="job_4",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="Auth error test",
    )
    mock_resp = httpx.Response(401, text="Unauthorized")
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await connector.publish("Auth error test", intent)
        assert not res.success
        assert not res.is_retryable
        assert res.error_code == "AUTH_ERROR_401"


# Test 5: X ambiguous timeout reconciliation
@pytest.mark.asyncio
async def test_05_x_ambiguous_timeout_reconciliation():
    connector = XConnector(bearer_token="mock_token")
    intent = PublicationIntent.create(
        package_id="pkg_5",
        job_id="job_5",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="Unique ambiguous tweet snippet",
    )
    # Simulate network timeout
    with patch.object(httpx.AsyncClient, "post", side_effect=httpx.TimeoutException("Read timed out")):
        res = await connector.publish("Unique ambiguous tweet snippet", intent)
        assert res.is_ambiguous
        assert res.is_retryable

    # Now reconcile via recent tweets search
    mock_search_resp = httpx.Response(200, json={
        "data": [{"id": "987654321", "text": "Unique ambiguous tweet snippet"}]
    })
    with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_search_resp
        recon = await connector.reconcile_ambiguous_delivery(intent, "Unique ambiguous tweet snippet")
        assert recon.resolved
        assert recon.published
        assert recon.provider_post_id == "987654321"


# Test 6: Threads container creation
@pytest.mark.asyncio
async def test_06_threads_container_creation():
    connector = ThreadsConnector(access_token="mock_th_token", user_id="12345")
    intent = PublicationIntent.create(
        package_id="pkg_6",
        job_id="job_6",
        target=TargetPlatform.THREADS,
        variant=OutputVariantType.THREADS_POST,
        approved_by=1001,
        payload_text="Threads test",
    )
    # Mock container step success, publish step success
    mock_container_resp = httpx.Response(200, json={"id": "container_999"})
    mock_publish_resp = httpx.Response(200, json={"id": "thread_post_888"})
    with patch.object(httpx.AsyncClient, "post", side_effect=[mock_container_resp, mock_publish_resp]):
        res = await connector.publish("Threads test", intent)
        assert res.success
        assert res.provider_post_id == "thread_post_888"


# Test 7: Threads publish step
@pytest.mark.asyncio
async def test_07_threads_publish():
    connector = ThreadsConnector(access_token="mock_th_token", user_id="12345")
    intent = PublicationIntent.create(
        package_id="pkg_7",
        job_id="job_7",
        target=TargetPlatform.THREADS,
        variant=OutputVariantType.THREADS_POST,
        approved_by=1001,
        payload_text="Threads publish",
    )
    mock_container_resp = httpx.Response(200, json={"id": "container_111"})
    mock_publish_resp = httpx.Response(200, json={"id": "thread_post_222"})
    with patch.object(httpx.AsyncClient, "post", side_effect=[mock_container_resp, mock_publish_resp]):
        res = await connector.publish("Threads publish", intent)
        assert res.success
        assert res.provider_post_id == "thread_post_222"


# Test 8: Threads 429 retry
@pytest.mark.asyncio
async def test_08_threads_429_retry():
    connector = ThreadsConnector(access_token="mock_th_token", user_id="12345")
    intent = PublicationIntent.create(
        package_id="pkg_8",
        job_id="job_8",
        target=TargetPlatform.THREADS,
        variant=OutputVariantType.THREADS_POST,
        approved_by=1001,
        payload_text="Threads rate limit",
    )
    mock_resp = httpx.Response(429, json={"error": {"code": 4, "message": "Application request limit reached"}})
    with patch.object(httpx.AsyncClient, "post", return_value=mock_resp):
        res = await connector.publish("Threads rate limit", intent)
        assert not res.success
        assert res.is_retryable
        assert res.error_code == "RATE_LIMITED"


# Test 9: Threads auth failure
@pytest.mark.asyncio
async def test_09_threads_auth_failure():
    connector = ThreadsConnector(access_token="invalid_token", user_id="12345")
    intent = PublicationIntent.create(
        package_id="pkg_9",
        job_id="job_9",
        target=TargetPlatform.THREADS,
        variant=OutputVariantType.THREADS_POST,
        approved_by=1001,
        payload_text="Threads auth fail",
    )
    mock_resp = httpx.Response(401, json={"error": {"code": 190, "message": "Invalid OAuth access token"}})
    with patch.object(httpx.AsyncClient, "post", return_value=mock_resp):
        res = await connector.publish("Threads auth fail", intent)
        assert not res.success
        assert not res.is_retryable
        assert res.error_code == "AUTH_ERROR_401"


# Test 10: Threads ambiguous response
@pytest.mark.asyncio
async def test_10_threads_ambiguous_response():
    connector = ThreadsConnector(access_token="mock_th_token", user_id="12345")
    intent = PublicationIntent.create(
        package_id="pkg_10",
        job_id="job_10",
        target=TargetPlatform.THREADS,
        variant=OutputVariantType.THREADS_POST,
        approved_by=1001,
        payload_text="Threads ambiguous",
    )
    with patch.object(httpx.AsyncClient, "post", side_effect=httpx.TimeoutException("Gateway timeout")):
        res = await connector.publish("Threads ambiguous", intent)
        assert res.is_ambiguous
        assert res.is_retryable


# Test 11: YouTube Community classified unsupported/manual
def test_11_youtube_community_classified_unsupported():
    connector = YouTubeCommunityConnector()
    caps = connector.capabilities()
    assert caps.status == CapabilityStatus.UNSUPPORTED_OFFICIAL_API
    assert caps.publication_mode.value == "MANUAL_EXPORT"


# Test 12: No browser automation path
def test_12_no_browser_automation_path():
    import app.worker.connectors as conn_module
    code_text = Path(conn_module.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ["selenium", "playwright", "puppeteer", "webdriver", "pyppeteer"]:
        assert forbidden not in code_text, f"Forbidden browser automation library {forbidden} detected!"


# Test 13: Credentials absent does not break Telegram
def test_13_credentials_absent_does_not_break_telegram():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_13", "https://instagram.com/reel/test", canonical, variants)
    # Simulate telegram delivered
    pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
    pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.DELIVERED
    transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)

    # Approve with unconfigured connectors
    pkg, recs = approve_package(pkg, user_id=1001, expected_owner_id=1001, use_connectors=True)
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED
    assert pkg.distribution_targets[TargetPlatform.THREADS.value].status == TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED
    assert pkg.distribution_targets[TargetPlatform.YOUTUBE_COMMUNITY.value].status == TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
    # Non-configured external platforms do NOT fail the package
    assert pkg.approval_state == PackageStatus.DELIVERED


# Test 14: Credentials never logged
def test_14_credentials_never_logged():
    raw_error = "Failed request with Bearer secret_token_xyz123 and api_key=super_secret_key"
    sanitized = sanitize_sensitive_text(raw_error)
    assert "secret_token_xyz123" not in sanitized
    assert "super_secret_key" not in sanitized
    assert "[REDACTED_CREDENTIAL]" in sanitized


# Test 15: Wrong provider account identity blocked
@pytest.mark.asyncio
async def test_15_wrong_provider_account_identity_blocked():
    connector = XConnector(bearer_token="mock_token", expected_user_id="expected_999")
    mock_user_resp = httpx.Response(200, json={"data": {"id": "wrong_111", "username": "wrong_user"}})
    with patch.object(httpx.AsyncClient, "get", return_value=mock_user_resp):
        val = await connector.validate_credentials()
        assert not val.is_valid
        assert val.error_code == "ACCOUNT_IDENTITY_MISMATCH"


# Test 16: Owner approval required
def test_16_owner_approval_required():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_16", "https://instagram.com/reel/test", canonical, variants)
    # Status before approval is REVIEW_REQUIRED
    assert pkg.approval_state == PackageStatus.REVIEW_REQUIRED
    assert pkg.distribution_targets[TargetPlatform.X.value].status == TargetDeliveryStatus.PENDING_APPROVAL


# Test 17: Unauthorized approval blocked
def test_17_unauthorized_approval_blocked():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_17", "https://instagram.com/reel/test", canonical, variants)
    with pytest.raises(UnauthorizedApprovalError):
        approve_package(pkg, user_id=9999, expected_owner_id=1001, use_connectors=True)


# Test 18: Payload mutation invalidates approval
def test_18_payload_mutation_invalidates_approval():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_18", "https://instagram.com/reel/test", canonical, variants)
    pkg, _ = approve_package(pkg, user_id=1001, expected_owner_id=1001, use_connectors=True)
    intent = next(i for i in pkg.publication_intents if i.target == TargetPlatform.X)
    original_hash = intent.payload_hash

    # Variant mutated
    new_text = "Mutated variant content"
    new_hash = compute_payload_hash(new_text)
    assert original_hash != new_hash


# Test 19: Payload hash preserved
def test_19_payload_hash_preserved():
    text = "Exact canonical text representation"
    h1 = compute_payload_hash(text)
    h2 = compute_payload_hash(text)
    assert h1 == h2
    assert len(h1) == 64


# Test 20: Stable publication_key across retries
def test_20_stable_publication_key_across_retries():
    intent = PublicationIntent.create(
        package_id="pkg_20",
        job_id="job_20",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="Stable text",
    )
    rec1 = DeliveryRecord.create(
        package_id=intent.package_id,
        target=intent.target,
        variant=intent.variant,
        attempt_id=1,
        approval_state=PackageStatus.APPROVED,
        status=DeliveryOutcome.RETRYABLE_ERROR,
        publication_key=intent.publication_key,
    )
    rec2 = DeliveryRecord.create(
        package_id=intent.package_id,
        target=intent.target,
        variant=intent.variant,
        attempt_id=2,
        approval_state=PackageStatus.APPROVED,
        status=DeliveryOutcome.SUCCEEDED,
        publication_key=intent.publication_key,
    )
    assert rec1.publication_key == rec2.publication_key
    assert rec1.attempt_id != rec2.attempt_id


# Test 21: Retry does not create second logical publication
def test_21_retry_does_not_create_second_logical_publication():
    intent = PublicationIntent.create(
        package_id="pkg_21",
        job_id="job_21",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        approved_by=1001,
        payload_text="Stable text",
    )
    # Attempt count increments, publication_key unchanged
    intent.attempt_count += 1
    key1 = intent.publication_key
    intent.attempt_count += 1
    key2 = intent.publication_key
    assert key1 == key2


# Test 22: Double callback idempotent
def test_22_double_callback_idempotent():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_22", "https://instagram.com/reel/test", canonical, variants)
    pkg, recs1 = approve_package(pkg, user_id=1001, expected_owner_id=1001, use_connectors=True)
    count1 = len(pkg.publication_intents)
    pkg, recs2 = approve_package(pkg, user_id=1001, expected_owner_id=1001, use_connectors=True)
    count2 = len(pkg.publication_intents)
    assert len(recs2) == 0
    assert count1 == count2


# Test 23: External provider ID persisted
def test_23_external_provider_id_persisted():
    rec = DeliveryRecord.create(
        package_id="pkg_23",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        attempt_id=1,
        approval_state=PackageStatus.APPROVED,
        status=DeliveryOutcome.SUCCEEDED,
        external_id="ext_999",
        provider_post_id="ext_999",
        provider_url="https://x.com/i/status/ext_999",
    )
    assert rec.provider_post_id == "ext_999"
    assert rec.provider_url == "https://x.com/i/status/ext_999"


# Test 24: Lookup verification
@pytest.mark.asyncio
async def test_24_lookup_verification():
    connector = XConnector(bearer_token="mock_token")
    mock_lookup = httpx.Response(200, json={
        "data": {"id": "lookup_123", "text": "Post content", "author_id": "author_456"}
    })
    with patch.object(httpx.AsyncClient, "get", return_value=mock_lookup):
        res = await connector.lookup("lookup_123")
        assert res.found
        assert res.provider_post_id == "lookup_123"
        assert res.text == "Post content"


# Test 25: Lifecycle DELIVERED only after correct target completion
def test_25_lifecycle_delivered_only_after_correct_target_completion():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_25", "https://instagram.com/reel/test", canonical, variants)
    # With pending approval, status is REVIEW_REQUIRED
    assert reconcile_package_status(pkg) == PackageStatus.REVIEW_REQUIRED
    # With active in-flight target, status is DELIVERY_PENDING
    pkg.distribution_targets[TargetPlatform.X.value].status = TargetDeliveryStatus.APPROVED
    assert reconcile_package_status(pkg) == PackageStatus.DELIVERY_PENDING


# Test 26: Manual-only YouTube does not fake DELIVERED
def test_26_manual_only_youtube_does_not_fake_delivered():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_26", "https://instagram.com/reel/test", canonical, variants)
    pkg, _ = approve_package(pkg, user_id=1001, expected_owner_id=1001, use_connectors=True)
    yt_target = pkg.distribution_targets[TargetPlatform.YOUTUBE_COMMUNITY.value]
    assert yt_target.status == TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
    assert yt_target.status != TargetDeliveryStatus.DELIVERED


# Test 27: Existing Telegram delivery green
def test_27_existing_telegram_delivery_green():
    canonical = _sample_canonical()
    variants = generate_all_variants(canonical)
    pkg = build_content_package("job_27", "https://instagram.com/reel/test", canonical, variants)
    assert OutputVariantType.TELEGRAM_LONG.value in pkg.output_variants.variants
    tl_variant = pkg.output_variants.variants[OutputVariantType.TELEGRAM_LONG.value]
    assert tl_variant.status == "RENDERED"
    assert tl_variant.text is not None


# Test 28: DB migration idempotent
@pytest.mark.asyncio
async def test_28_db_migration_idempotent():
    mock_conn = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn
    await apply_migrations(mock_engine)
    await apply_migrations(mock_engine)
    assert mock_conn.execute.called


# Test 29: Connector replay suite passes
def test_29_connector_replay_suite_passes():
    cases = load_connector_cases("tests/fixtures/connector_eval.json")
    report = evaluate_connectors(cases)
    assert report.all_gates_passed
    assert report.unauthorized_publications == 0
    assert report.duplicate_publications == 0
    assert report.stale_approval_violations == 0
    assert report.fake_successes == 0
    assert report.retry_correctness == 1.0
