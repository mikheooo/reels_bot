import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
    PublicationConnector,
)
from app.worker.content_package import (
    DeliveryOutcome,
    DeliveryRecord,
    TargetPlatform,
    compute_payload_hash,
)

logger = logging.getLogger(__name__)


def evaluate_audit_eligibility(
    target: TargetPlatform,
    delivery_status: DeliveryOutcome,
    provider_post_id: str | None,
    connector_status: CapabilityStatus,
    policy: AuditPolicy | None = None,
    now: datetime | None = None,
) -> tuple[bool, AuditTargetStatus, datetime | None]:
    """Determine whether an external publication is eligible for automated post-publish audit."""
    current_time = now or datetime.now(timezone.utc)
    active_policy = policy or AuditPolicy()

    # Rule 1: YouTube Community is manual-export-only in official API
    if target == TargetPlatform.YOUTUBE_COMMUNITY:
        return False, AuditTargetStatus.NOT_APPLICABLE_MANUAL_EXPORT, None

    # Rule 2: Telegram Bot API does not support arbitrary post lookup / deletion verification
    if target in (TargetPlatform.TELEGRAM_CHANNEL, TargetPlatform.TELEGRAM_USER):
        return False, AuditTargetStatus.DELETION_VERIFICATION_UNSUPPORTED, None

    # Rule 3: External connectors (X, Threads) when unconfigured must never create an overdue schedule
    if connector_status != CapabilityStatus.CONNECTED_SUPPORTED:
        return False, AuditTargetStatus.NOT_APPLICABLE_NOT_CONFIGURED, None

    # Rule 4: Eligible only if delivery succeeded and provider returned an external post ID
    if delivery_status == DeliveryOutcome.SUCCEEDED and provider_post_id:
        initial_delay = active_policy.intervals_seconds[0] if active_policy.intervals_seconds else 900
        next_audit_at = current_time + timedelta(seconds=initial_delay)
        return True, AuditTargetStatus.SCHEDULED, next_audit_at

    return False, AuditTargetStatus.FAILED, None


def normalize_content_for_comparison(text: str) -> str:
    """Technical normalization for content integrity comparison.

    Normalizes newlines, strips line-trailing whitespace, and strips outer whitespace.
    Does NOT perform lossy fuzzy matching that could conceal external content mutations.
    """
    if not text:
        return ""
    # Standardize newlines
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip whitespace per line
    lines = [line.rstrip() for line in normalized.split("\n")]
    # Strip leading/trailing blank lines
    return "\n".join(lines).strip()


def compute_content_integrity(
    approved_text: str,
    provider_text: str | None,
) -> tuple[bool, str | None, str | None, AuditResultStatus]:
    """Compare provider-returned text with approved content payload.

    Returns (is_match, approved_hash, provider_hash, status).
    """
    norm_approved = normalize_content_for_comparison(approved_text)
    approved_hash = compute_payload_hash(norm_approved)

    if provider_text is None:
        return False, approved_hash, None, AuditResultStatus.REVIEW_REQUIRED

    norm_provider = normalize_content_for_comparison(provider_text)
    provider_hash = compute_payload_hash(norm_provider)

    if norm_approved == norm_provider:
        return True, approved_hash, provider_hash, AuditResultStatus.VERIFIED
    else:
        return False, approved_hash, provider_hash, AuditResultStatus.MODIFIED


def normalize_provider_metrics(
    target: TargetPlatform,
    raw_metrics: dict[str, Any],
) -> NormalizedMetrics:
    """Normalize raw provider metrics dictionaries into typed NormalizedMetrics."""
    if not raw_metrics:
        return NormalizedMetrics()

    def _to_int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    if target == TargetPlatform.X:
        return NormalizedMetrics(
            views=_to_int(raw_metrics.get("impression_count")),
            likes=_to_int(raw_metrics.get("like_count")),
            replies=_to_int(raw_metrics.get("reply_count")),
            reposts=_to_int(raw_metrics.get("retweet_count")),
            quotes=_to_int(raw_metrics.get("quote_count")),
            bookmarks=_to_int(raw_metrics.get("bookmark_count")),
        )
    elif target == TargetPlatform.THREADS:
        return NormalizedMetrics(
            views=_to_int(raw_metrics.get("views")),
            likes=_to_int(raw_metrics.get("likes")),
            replies=_to_int(raw_metrics.get("replies")),
            reposts=_to_int(raw_metrics.get("reposts")),
            quotes=_to_int(raw_metrics.get("quotes")),
            bookmarks=_to_int(raw_metrics.get("bookmarks")),
        )
    return NormalizedMetrics()


def compute_metric_deltas(
    current_metrics: NormalizedMetrics,
    previous_metrics: NormalizedMetrics | None,
) -> dict[str, MetricDelta]:
    """Calculate absolute and percentage deltas between successive metric snapshots.

    Guards against division-by-zero when the baseline is zero.
    """
    deltas: dict[str, MetricDelta] = {}
    metric_fields = ["views", "likes", "replies", "reposts", "quotes", "bookmarks"]

    for field in metric_fields:
        curr_val = getattr(current_metrics, field)
        prev_val = getattr(previous_metrics, field) if previous_metrics else None

        if curr_val is None and prev_val is None:
            continue

        abs_delta: int | None = None
        pct_delta: float | None = None

        if curr_val is not None and prev_val is not None:
            abs_delta = curr_val - prev_val
            if prev_val > 0:
                pct_delta = round(((curr_val - prev_val) / prev_val) * 100.0, 2)
            else:
                pct_delta = None  # Safe zero baseline: avoid division by zero or deceptive 100%

        deltas[field] = MetricDelta(
            previous=prev_val,
            current=curr_val,
            absolute_delta=abs_delta,
            percentage_delta=pct_delta,
        )

    return deltas


async def perform_audit_check(
    target: AuditTarget,
    connector: PublicationConnector,
    previous_snapshot: AuditSnapshot | None = None,
    policy: AuditPolicy | None = None,
    now: datetime | None = None,
) -> tuple[AuditResult, AuditSnapshot]:
    """Execute an external lookup audit against the provider API.

    Enforces:
    - Accurate distinction between 404 (absent) vs 401/403 (auth required) vs 429 (throttled).
    - Technical content integrity verification.
    - Immutable snapshot generation with occurrence key idempotency.
    - Declarative cadence tier advancement.
    """
    checked_at = now or datetime.now(timezone.utc)
    active_policy = policy or AuditPolicy()
    t0 = time.monotonic()

    lookup_res = await connector.lookup(target.provider_post_id)
    latency_ms = int((time.monotonic() - t0) * 1000)

    # Occurrence key for audit execution idempotency
    occurrence_key = f"{target.audit_id}:{int(checked_at.timestamp())}"

    # Case A: Object lookup failed or returned error
    if not lookup_res.found:
        status: AuditResultStatus
        if lookup_res.http_status == 404 or lookup_res.error_code == "POST_NOT_FOUND":
            status = AuditResultStatus.DELETED_OR_NOT_FOUND
        elif lookup_res.http_status in (401, 403) or lookup_res.error_code == "AUTH_REQUIRED":
            status = AuditResultStatus.AUTH_REQUIRED
        elif lookup_res.http_status == 429 or lookup_res.error_code == "RATE_LIMITED":
            status = AuditResultStatus.TEMPORARILY_UNAVAILABLE
        else:
            status = AuditResultStatus.FAILED

        # Terminal anomalies stop further routine scheduling
        next_audit: datetime | None = None
        if status == AuditResultStatus.TEMPORARILY_UNAVAILABLE:
            retry_delay = (
                lookup_res.rate_limit_info.retry_after_seconds
                if lookup_res.rate_limit_info and lookup_res.rate_limit_info.retry_after_seconds
                else active_policy.transient_retry_backoff_seconds
            )
            next_audit = checked_at + timedelta(seconds=retry_delay)

        result = AuditResult(
            status=status,
            checked_at=checked_at,
            object_exists=False,
            content_match=False,
            modified=False,
            provider_error=lookup_res.error_message or lookup_res.error_code,
            provider_http_status=lookup_res.http_status,
            raw_metrics=lookup_res.raw_metrics,
            normalized_metrics=NormalizedMetrics(),
            metrics_available=False,
            metric_deltas={},
            next_audit_at=next_audit,
            rate_limit_info=lookup_res.rate_limit_info,
            latency_ms=latency_ms,
        )
        snapshot = AuditSnapshot(
            snapshot_id=f"asnap_{occurrence_key}",
            audit_id=target.audit_id,
            occurrence_key=occurrence_key,
            checked_at=checked_at,
            object_exists=False,
            content_hash=None,
            content_match=False,
            raw_metrics=lookup_res.raw_metrics,
            normalized_metrics=NormalizedMetrics(),
            metric_deltas={},
            provider_http_status=lookup_res.http_status,
            latency_ms=latency_ms,
            status=status,
            created_at=checked_at,
        )
        return result, snapshot

    # Case B: Object exists on provider
    content_match, approved_hash, current_hash, integrity_status = compute_content_integrity(
        target.approved_payload_text,
        lookup_res.text,
    )
    norm_metrics = normalize_provider_metrics(target.target, lookup_res.raw_metrics)
    prev_metrics = previous_snapshot.normalized_metrics if previous_snapshot else None
    deltas = compute_metric_deltas(norm_metrics, prev_metrics)
    has_metrics = any(v is not None for v in norm_metrics.model_dump().values())

    status = integrity_status  # VERIFIED or MODIFIED
    modified = (status == AuditResultStatus.MODIFIED)

    # Advance tier for scheduling next audit
    next_tier = target.tier + 1
    next_audit = None
    if next_tier < len(active_policy.intervals_seconds):
        interval = active_policy.intervals_seconds[next_tier]
        next_audit = checked_at + timedelta(seconds=interval)

    result = AuditResult(
        status=status,
        checked_at=checked_at,
        object_exists=True,
        content_match=content_match,
        current_content_hash=current_hash,
        approved_content_hash=approved_hash,
        modified=modified,
        provider_http_status=lookup_res.http_status or 200,
        raw_metrics=lookup_res.raw_metrics,
        normalized_metrics=norm_metrics,
        metrics_available=has_metrics,
        metric_deltas=deltas,
        next_audit_at=next_audit,
        rate_limit_info=lookup_res.rate_limit_info,
        latency_ms=latency_ms,
    )
    snapshot = AuditSnapshot(
        snapshot_id=f"asnap_{occurrence_key}",
        audit_id=target.audit_id,
        occurrence_key=occurrence_key,
        checked_at=checked_at,
        object_exists=True,
        content_hash=current_hash,
        content_match=content_match,
        raw_metrics=lookup_res.raw_metrics,
        normalized_metrics=norm_metrics,
        metric_deltas=deltas,
        provider_http_status=lookup_res.http_status or 200,
        latency_ms=latency_ms,
        status=status,
        created_at=checked_at,
    )
    return result, snapshot


def should_send_alert(result: AuditResult) -> tuple[bool, str]:
    """Determine whether an audit result warrants an urgent administrator alert.

    Alerts only on actionable anomalies:
    - Post deleted or absent after confirmation
    - Content modified externally
    - Authentication revoked or expired
    Does NOT alert on normal metrics growth.
    """
    if result.status == AuditResultStatus.DELETED_OR_NOT_FOUND:
        return True, "External post was deleted or is no longer found on the provider."
    if result.status == AuditResultStatus.MODIFIED:
        return True, "External post text was modified and deviates from approved payload."
    if result.status == AuditResultStatus.AUTH_REQUIRED:
        return True, "External provider authentication revoked or expired (HTTP 401/403)."
    return False, ""
