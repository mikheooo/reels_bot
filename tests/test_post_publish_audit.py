"""Comprehensive test suite for Stage: Post-Publish Audit & Telemetry v2."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.audit_evaluation import (
    AuditEvalCase,
    AuditEvalReport,
    MockAuditConnector,
    evaluate_audit_suite,
    load_audit_cases,
)
from app.worker.audit_scheduler import (
    cron_audit_v2_jobs,
    register_audit_target_if_eligible,
)
from app.worker.audit_schemas import (
    AuditPolicy,
    AuditResult,
    AuditResultStatus,
    AuditSnapshot,
    AuditTarget,
    AuditTargetStatus,
    MetricDelta,
    NormalizedMetrics,
)
from app.worker.connectors import (
    CapabilityStatus,
    LookupResult,
    RateLimitInfo,
)
from app.worker.content_package import (
    DeliveryOutcome,
    DeliveryRecord,
    OutputVariantType,
    PackageStatus,
    TargetPlatform,
    compute_payload_hash,
)
from app.worker.post_publish_audit import (
    compute_content_integrity,
    compute_metric_deltas,
    evaluate_audit_eligibility,
    normalize_content_for_comparison,
    normalize_provider_metrics,
    perform_audit_check,
    should_send_alert,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "audit_eval.json"


# Test 01: Audit Target and Snapshot schema validation
def test_01_audit_schemas_validation():
    now = datetime.now(timezone.utc)
    target = AuditTarget(
        audit_id="audit_test_01",
        package_id="pkg_test_01",
        delivery_id="del_test_01",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        provider_post_id="post_12345",
        provider_url="https://x.com/user/status/post_12345",
        publication_key="key_12345",
        approved_payload_hash="hash123",
        approved_payload_text="Text content",
        status=AuditTargetStatus.SCHEDULED,
        tier=0,
        next_audit_at=now,
    )
    assert target.audit_id == "audit_test_01"
    assert target.status == AuditTargetStatus.SCHEDULED
    assert target.tier == 0

    snapshot = AuditSnapshot(
        snapshot_id="snap_123",
        audit_id=target.audit_id,
        occurrence_key=f"{target.audit_id}:{int(now.timestamp())}",
        checked_at=now,
        object_exists=True,
        content_hash="hash123",
        content_match=True,
        raw_metrics={"views": 10},
        normalized_metrics=NormalizedMetrics(views=10),
        metric_deltas={},
        status=AuditResultStatus.VERIFIED,
    )
    assert snapshot.snapshot_id == "snap_123"
    assert snapshot.normalized_metrics.views == 10
    assert snapshot.status == AuditResultStatus.VERIFIED


# Test 02: Eligibility for unconfigured connectors
def test_02_eligibility_unconfigured_connectors():
    is_eligible, status, next_at = evaluate_audit_eligibility(
        target=TargetPlatform.X,
        delivery_status=DeliveryOutcome.SUCCEEDED,
        provider_post_id="post_123",
        connector_status=CapabilityStatus.SUPPORTED_NOT_CONFIGURED,
    )
    assert is_eligible is False
    assert status == AuditTargetStatus.NOT_APPLICABLE_NOT_CONFIGURED
    assert next_at is None


# Test 03: Eligibility for YouTube Community manual export
def test_03_eligibility_youtube_manual_export():
    is_eligible, status, next_at = evaluate_audit_eligibility(
        target=TargetPlatform.YOUTUBE_COMMUNITY,
        delivery_status=DeliveryOutcome.SUCCEEDED,
        provider_post_id="draft_123",
        connector_status=CapabilityStatus.UNSUPPORTED_OFFICIAL_API,
    )
    assert is_eligible is False
    assert status == AuditTargetStatus.NOT_APPLICABLE_MANUAL_EXPORT
    assert next_at is None


# Test 04: Eligibility for Telegram Bot API unsupported lookup
def test_04_eligibility_telegram_unsupported_lookup():
    for tg_target in (TargetPlatform.TELEGRAM_CHANNEL, TargetPlatform.TELEGRAM_USER):
        is_eligible, status, next_at = evaluate_audit_eligibility(
            target=tg_target,
            delivery_status=DeliveryOutcome.SUCCEEDED,
            provider_post_id="tg_msg_123",
            connector_status=CapabilityStatus.CONNECTED_SUPPORTED,
        )
        assert is_eligible is False
        assert status == AuditTargetStatus.DELETION_VERIFICATION_UNSUPPORTED
        assert next_at is None


# Test 05: Eligibility for connected external success
def test_05_eligibility_connected_external_success():
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    policy = AuditPolicy(intervals_seconds=[900, 7200])
    is_eligible, status, next_at = evaluate_audit_eligibility(
        target=TargetPlatform.X,
        delivery_status=DeliveryOutcome.SUCCEEDED,
        provider_post_id="1892837465912384",
        connector_status=CapabilityStatus.CONNECTED_SUPPORTED,
        policy=policy,
        now=now,
    )
    assert is_eligible is True
    assert status == AuditTargetStatus.SCHEDULED
    assert next_at == now + timedelta(seconds=900)


# Test 06: Eligibility when delivery failed
def test_06_eligibility_when_delivery_failed():
    is_eligible, status, next_at = evaluate_audit_eligibility(
        target=TargetPlatform.X,
        delivery_status=DeliveryOutcome.FAILED,
        provider_post_id=None,
        connector_status=CapabilityStatus.CONNECTED_SUPPORTED,
    )
    assert is_eligible is False
    assert status == AuditTargetStatus.FAILED
    assert next_at is None


# Test 07: Technical content integrity exact match
def test_07_content_integrity_exact_match():
    approved = "Первая строка поста.\r\nВторая строка поста.   "
    provider = "Первая строка поста.\nВторая строка поста."
    is_match, app_hash, prov_hash, status = compute_content_integrity(approved, provider)
    assert is_match is True
    assert app_hash == prov_hash
    assert status == AuditResultStatus.VERIFIED


# Test 08: Content integrity detects modification
def test_08_content_integrity_modification_detected():
    approved = "Первая строка оригинального поста."
    provider = "Первая строка оригинального поста (отредактировано)."
    is_match, app_hash, prov_hash, status = compute_content_integrity(approved, provider)
    assert is_match is False
    assert app_hash != prov_hash
    assert status == AuditResultStatus.MODIFIED


# Test 09: Metrics normalization for X
def test_09_metrics_normalization_x():
    raw = {
        "impression_count": "1500",
        "like_count": 42,
        "reply_count": 8,
        "retweet_count": 12,
        "quote_count": 2,
        "bookmark_count": 5,
    }
    norm = normalize_provider_metrics(TargetPlatform.X, raw)
    assert norm.views == 1500
    assert norm.likes == 42
    assert norm.replies == 8
    assert norm.reposts == 12
    assert norm.quotes == 2
    assert norm.bookmarks == 5


# Test 10: Metrics normalization for Threads
def test_10_metrics_normalization_threads():
    raw = {
        "views": 2500,
        "likes": 88,
        "replies": 14,
        "reposts": 6,
        "quotes": 4,
    }
    norm = normalize_provider_metrics(TargetPlatform.THREADS, raw)
    assert norm.views == 2500
    assert norm.likes == 88
    assert norm.replies == 14
    assert norm.reposts == 6
    assert norm.quotes == 4


# Test 11: Metric deltas calculation
def test_11_metric_deltas_calculation():
    prev = NormalizedMetrics(views=1000, likes=50)
    curr = NormalizedMetrics(views=1500, likes=75)
    deltas = compute_metric_deltas(curr, prev)
    assert deltas["views"].previous == 1000
    assert deltas["views"].current == 1500
    assert deltas["views"].absolute_delta == 500
    assert deltas["views"].percentage_delta == 50.0

    assert deltas["likes"].previous == 50
    assert deltas["likes"].current == 75
    assert deltas["likes"].absolute_delta == 25
    assert deltas["likes"].percentage_delta == 50.0


# Test 12: Metric deltas safe zero baseline
def test_12_metric_deltas_safe_zero_baseline():
    prev = NormalizedMetrics(views=0, quotes=0)
    curr = NormalizedMetrics(views=100, quotes=5)
    deltas = compute_metric_deltas(curr, prev)
    assert deltas["views"].absolute_delta == 100
    assert deltas["views"].percentage_delta is None  # Safe zero baseline, no div by 0

    assert deltas["quotes"].absolute_delta == 5
    assert deltas["quotes"].percentage_delta is None


# Test 13: Audit lookup HTTP 404 (deleted/absent)
@pytest.mark.asyncio
async def test_13_audit_lookup_404_deleted():
    target = AuditTarget(
        audit_id="audit_404",
        package_id="pkg_404",
        delivery_id="del_404",
        target=TargetPlatform.X,
        provider_post_id="deleted_post",
        publication_key="pk_404",
        approved_payload_hash="h404",
        approved_payload_text="text",
        status=AuditTargetStatus.SCHEDULED,
        tier=0,
    )
    connector = MockAuditConnector(target=TargetPlatform.X, http_status=404)
    result, snapshot = await perform_audit_check(target, connector)
    assert result.status == AuditResultStatus.DELETED_OR_NOT_FOUND
    assert result.object_exists is False
    assert result.next_audit_at is None
    assert snapshot.object_exists is False
    assert snapshot.status == AuditResultStatus.DELETED_OR_NOT_FOUND


# Test 14: Audit lookup HTTP 401/403 (auth required, NEVER deleted)
@pytest.mark.asyncio
async def test_14_audit_lookup_401_auth_required():
    target = AuditTarget(
        audit_id="audit_401",
        package_id="pkg_401",
        delivery_id="del_401",
        target=TargetPlatform.X,
        provider_post_id="auth_post",
        publication_key="pk_401",
        approved_payload_hash="h401",
        approved_payload_text="text",
        status=AuditTargetStatus.SCHEDULED,
        tier=0,
    )
    connector = MockAuditConnector(target=TargetPlatform.X, http_status=401)
    result, snapshot = await perform_audit_check(target, connector)
    assert result.status == AuditResultStatus.AUTH_REQUIRED
    assert result.status != AuditResultStatus.DELETED_OR_NOT_FOUND  # Critical Gate 1
    assert result.object_exists is False
    assert result.next_audit_at is None
    assert snapshot.status == AuditResultStatus.AUTH_REQUIRED


# Test 15: Audit lookup HTTP 429 (rate limited backoff)
@pytest.mark.asyncio
async def test_15_audit_lookup_429_rate_limited():
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    target = AuditTarget(
        audit_id="audit_429",
        package_id="pkg_429",
        delivery_id="del_429",
        target=TargetPlatform.X,
        provider_post_id="rate_post",
        publication_key="pk_429",
        approved_payload_hash="h429",
        approved_payload_text="text",
        status=AuditTargetStatus.SCHEDULED,
        tier=0,
    )
    connector = MockAuditConnector(target=TargetPlatform.X, http_status=429)
    result, snapshot = await perform_audit_check(target, connector, now=now)
    assert result.status == AuditResultStatus.TEMPORARILY_UNAVAILABLE
    assert result.object_exists is False
    assert result.next_audit_at == now + timedelta(seconds=60)
    assert snapshot.status == AuditResultStatus.TEMPORARILY_UNAVAILABLE


# Test 16: Alerting criteria
def test_16_alerting_criteria():
    res_deleted = AuditResult(
        status=AuditResultStatus.DELETED_OR_NOT_FOUND,
        checked_at=datetime.now(timezone.utc),
        object_exists=False,
    )
    alert_del, msg_del = should_send_alert(res_deleted)
    assert alert_del is True
    assert "deleted" in msg_del.lower()

    res_modified = AuditResult(
        status=AuditResultStatus.MODIFIED,
        checked_at=datetime.now(timezone.utc),
        object_exists=True,
        modified=True,
    )
    alert_mod, msg_mod = should_send_alert(res_modified)
    assert alert_mod is True
    assert "modified" in msg_mod.lower()

    res_auth = AuditResult(
        status=AuditResultStatus.AUTH_REQUIRED,
        checked_at=datetime.now(timezone.utc),
        object_exists=False,
    )
    alert_auth, msg_auth = should_send_alert(res_auth)
    assert alert_auth is True
    assert "authentication" in msg_auth.lower()

    res_verified = AuditResult(
        status=AuditResultStatus.VERIFIED,
        checked_at=datetime.now(timezone.utc),
        object_exists=True,
    )
    alert_ver, _ = should_send_alert(res_verified)
    assert alert_ver is False


# Test 17: Cadence tier advancement
@pytest.mark.asyncio
async def test_17_cadence_tier_advancement():
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    policy = AuditPolicy(intervals_seconds=[900, 7200, 86400])
    target = AuditTarget(
        audit_id="audit_tier",
        package_id="pkg_tier",
        delivery_id="del_tier",
        target=TargetPlatform.X,
        provider_post_id="post_tier",
        publication_key="pk_tier",
        approved_payload_hash=compute_payload_hash("text"),
        approved_payload_text="text",
        status=AuditTargetStatus.SCHEDULED,
        tier=0,
    )
    connector = MockAuditConnector(target=TargetPlatform.X, http_status=200, text="text")
    result, snapshot = await perform_audit_check(target, connector, policy=policy, now=now)
    assert result.status == AuditResultStatus.VERIFIED
    # Next tier is tier 1 (7200 seconds)
    assert result.next_audit_at == now + timedelta(seconds=7200)


# Test 18: Terminal cadence tier
@pytest.mark.asyncio
async def test_18_terminal_cadence_tier():
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    policy = AuditPolicy(intervals_seconds=[900, 7200])
    target = AuditTarget(
        audit_id="audit_terminal",
        package_id="pkg_term",
        delivery_id="del_term",
        target=TargetPlatform.X,
        provider_post_id="post_term",
        publication_key="pk_term",
        approved_payload_hash=compute_payload_hash("text"),
        approved_payload_text="text",
        status=AuditTargetStatus.SCHEDULED,
        tier=1,  # Last tier
    )
    connector = MockAuditConnector(target=TargetPlatform.X, http_status=200, text="text")
    result, snapshot = await perform_audit_check(target, connector, policy=policy, now=now)
    assert result.status == AuditResultStatus.VERIFIED
    assert result.next_audit_at is None


# Test 19: Occurrence key idempotency
@pytest.mark.asyncio
async def test_19_occurrence_key_idempotency():
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    target = AuditTarget(
        audit_id="audit_idemp",
        package_id="pkg_idemp",
        delivery_id="del_idemp",
        target=TargetPlatform.X,
        provider_post_id="post_idemp",
        publication_key="pk_idemp",
        approved_payload_hash=compute_payload_hash("text"),
        approved_payload_text="text",
        status=AuditTargetStatus.SCHEDULED,
        tier=0,
    )
    connector = MockAuditConnector(target=TargetPlatform.X, http_status=200, text="text")
    _, snap1 = await perform_audit_check(target, connector, now=now)
    _, snap2 = await perform_audit_check(target, connector, now=now)
    assert snap1.occurrence_key == snap2.occurrence_key
    assert snap1.snapshot_id == snap2.snapshot_id


# Test 20: Scheduler registration wiring
@pytest.mark.asyncio
async def test_20_scheduler_registration_wiring():
    delivery = DeliveryRecord(
        delivery_id="del_reg_1",
        package_id="pkg_reg_1",
        target=TargetPlatform.X,
        variant=OutputVariantType.X_POST,
        status=DeliveryOutcome.SUCCEEDED,
        approval_state=PackageStatus.APPROVED,
        idempotency_key="idemp_reg_1",
        publication_key="pubkey_reg",
        approved_by=1001,
        approved_at=datetime.now(timezone.utc),
        payload_hash="hash_reg",
        payload_text="Approved text for X",
        provider_post_id="1892837465912384",
    )
    mock_session = AsyncMock()
    mock_session.get.return_value = None
    mock_session.add = MagicMock()
    mock_connector = MagicMock()
    mock_connector.capabilities.return_value.status = CapabilityStatus.CONNECTED_SUPPORTED

    with patch("app.worker.connectors.ConnectorRegistry.get_connector", return_value=mock_connector):
        target = await register_audit_target_if_eligible(
            mock_session,
            delivery,
            approved_text="Approved text for X",
            package_id="pkg_reg_1",
        )
        assert target is not None
        assert target.target == "X"
        assert target.status == "SCHEDULED"
        assert target.provider_post_id == "1892837465912384"
        assert mock_session.add.called


# Test 21: Legacy audit isolation
@pytest.mark.asyncio
async def test_21_legacy_audit_isolation():
    # Verify that cron_audit_v2_jobs queries only AuditTargetModel and NEVER touches jobs table
    mock_session = AsyncMock()
    # Mock query result returning no active audit targets
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    with patch("app.worker.audit_scheduler.AsyncSessionLocal") as mock_factory:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        mock_factory.return_value = mock_ctx

        count = await cron_audit_v2_jobs({})
        assert count == 0
        # Check that executed query targeted AuditTargetModel
        assert mock_session.execute.called
        query_arg = mock_session.execute.call_args[0][0]
        query_str = str(query_arg)
        assert "audit_targets" in query_str
        assert "jobs" not in query_str


# Test 22: Audit Replay Suite (All 13 deterministic scenarios & 4 safety gates)
def test_22_audit_replay_suite_all_gates_passed():
    assert FIXTURE_PATH.exists(), f"Fixture {FIXTURE_PATH} not found"
    cases = load_audit_cases(FIXTURE_PATH)
    assert len(cases) == 13
    report = evaluate_audit_suite(cases)
    assert report.total_scenarios == 13
    assert report.passed_scenarios == 13
    assert report.auth_mistaken_for_deletion == 0          # Gate 1
    assert report.duplicate_snapshots == 0                 # Gate 2
    assert report.unsupported_fake_verification == 0       # Gate 3
    assert report.overdue_pollution_for_unavailable_connectors == 0  # Gate 4
    assert report.all_gates_passed is True
