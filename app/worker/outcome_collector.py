"""Outcome observation collector, normalization, and backfill engine."""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.db.models import (
    AuditSnapshotModel,
    AuditTargetModel,
    ContentDeliveryModel,
    ContentPackageModel,
    OutcomeObservationModel,
)
from app.worker.audit_schemas import AuditResultStatus, AuditTargetStatus
from app.worker.content_package import OutputVariantType, TargetPlatform
from app.worker.outcome_schemas import (
    AuditHorizon,
    ContentIntegrityStatus,
    DataQualityStatus,
    OutcomeDerivedMetrics,
    OutcomeObservation,
)
from app.worker.priority_policy import PriorityTier

logger = logging.getLogger(__name__)

TIER_TO_HORIZON: dict[int, AuditHorizon] = {
    0: AuditHorizon.H_15M,
    1: AuditHorizon.H_2H,
    2: AuditHorizon.H_12H,
    3: AuditHorizon.H_24H,
    4: AuditHorizon.H_3D,
    5: AuditHorizon.H_7D,
}


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def resolve_horizon(
    tier: int | None = None,
    scheduled_for: datetime.datetime | None = None,
    published_at: datetime.datetime | None = None,
    checked_at: datetime.datetime | None = None,
) -> AuditHorizon:
    """Deterministically map tier or elapsed duration to canonical AuditHorizon."""
    if tier is not None and tier in TIER_TO_HORIZON:
        return TIER_TO_HORIZON[tier]

    ref_time = scheduled_for or checked_at
    if published_at and ref_time:
        diff_sec = max(0.0, (ref_time - published_at).total_seconds())
        if diff_sec <= 2700:  # <= 45 min
            return AuditHorizon.H_15M
        if diff_sec <= 21600:  # <= 6 h
            return AuditHorizon.H_2H
        if diff_sec <= 64800:  # <= 18 h
            return AuditHorizon.H_12H
        if diff_sec <= 172800:  # <= 48 h
            return AuditHorizon.H_24H
        if diff_sec <= 432000:  # <= 5 days
            return AuditHorizon.H_3D
        return AuditHorizon.H_7D

    return AuditHorizon.H_15M


def classify_data_quality(
    target_status: str | AuditTargetStatus,
    snapshot_status: str | AuditResultStatus,
    object_exists: bool,
    content_match: bool | None,
    normalized_metrics: dict[str, Any] | None,
) -> DataQualityStatus:
    """Classify snapshot data quality for calibration eligibility."""
    t_status = target_status.value if isinstance(target_status, AuditTargetStatus) else target_status
    s_status = snapshot_status.value if isinstance(snapshot_status, AuditResultStatus) else snapshot_status

    if s_status == AuditResultStatus.AUTH_REQUIRED.value:
        return DataQualityStatus.AUTH_UNAVAILABLE

    if s_status == AuditResultStatus.DELETED_OR_NOT_FOUND.value or not object_exists:
        return DataQualityStatus.CONTENT_DELETED

    if s_status == AuditResultStatus.MODIFIED.value or content_match is False:
        return DataQualityStatus.CONTENT_MODIFIED

    if t_status == AuditTargetStatus.DELETION_VERIFICATION_UNSUPPORTED.value:
        return DataQualityStatus.UNSUPPORTED

    if t_status in (
        AuditTargetStatus.NOT_APPLICABLE_NOT_CONFIGURED.value,
        AuditTargetStatus.NOT_APPLICABLE_MANUAL_EXPORT.value,
        AuditTargetStatus.DEFERRED_LEGACY.value,
    ) or s_status in (
        AuditResultStatus.NOT_APPLICABLE.value,
        AuditResultStatus.FAILED.value,
    ):
        return DataQualityStatus.NOT_APPLICABLE

    # Check metrics
    norm = normalized_metrics or {}
    views = norm.get("views")
    likes = norm.get("likes")
    replies = norm.get("replies")
    reposts = norm.get("reposts")
    quotes = norm.get("quotes")
    bookmarks = norm.get("bookmarks")

    all_none = all(v is None for v in (views, likes, replies, reposts, quotes, bookmarks))
    if all_none:
        return DataQualityStatus.INSUFFICIENT_DATA

    # If views are provided or most interaction metrics are provided
    if views is not None:
        return DataQualityStatus.VALID

    # Partial metric set (e.g. likes present, views unknown)
    return DataQualityStatus.PARTIAL


def compute_derived_metrics(
    target_platform: TargetPlatform,
    raw_metrics: dict[str, Any],
    normalized_metrics: dict[str, Any],
    metric_deltas: dict[str, Any] | None = None,
    elapsed_seconds: float | None = None,
) -> OutcomeDerivedMetrics:
    """Compute truthful derived metrics without fabricating zeros or cross-platform mixing."""
    views = normalized_metrics.get("views")
    likes = normalized_metrics.get("likes")
    replies = normalized_metrics.get("replies")
    reposts = normalized_metrics.get("reposts")
    quotes = normalized_metrics.get("quotes")
    bookmarks = normalized_metrics.get("bookmarks")

    interaction_values = [v for v in (likes, replies, reposts, quotes, bookmarks) if v is not None]
    if interaction_values:
        engagement_total: int | None = sum(interaction_values)
    else:
        engagement_total = None

    # CRITICAL: engagement_rate ONLY when denominator (views) is known and > 0
    engagement_rate: float | None = None
    if views is not None and views > 0 and engagement_total is not None:
        engagement_rate = round(float(engagement_total) / float(views), 6)
    else:
        engagement_rate = None

    elapsed_hours: float | None = None
    if elapsed_seconds is not None and elapsed_seconds > 0:
        elapsed_hours = elapsed_seconds / 3600.0

    velocity_views: float | None = None
    if views is not None and elapsed_hours and elapsed_hours > 0:
        velocity_views = round(views / elapsed_hours, 2)

    velocity_engagement: float | None = None
    if engagement_total is not None and elapsed_hours and elapsed_hours > 0:
        velocity_engagement = round(engagement_total / elapsed_hours, 2)

    return OutcomeDerivedMetrics(
        views=views,
        likes=likes,
        replies=replies,
        reposts=reposts,
        quotes=quotes,
        bookmarks=bookmarks,
        engagement_total=engagement_total,
        engagement_rate=engagement_rate,
        delta_from_previous=metric_deltas,
        velocity_views_per_hour=velocity_views,
        velocity_engagement_per_hour=velocity_engagement,
    )


def build_outcome_observation(
    snapshot_id: str,
    target_id: str,
    package_id: str,
    job_id: str,
    publication_key: str,
    target_platform: TargetPlatform,
    variant_type: OutputVariantType,
    raw_metrics: dict[str, Any],
    normalized_metrics: dict[str, Any],
    metric_deltas: dict[str, Any] | None,
    object_exists: bool,
    content_match: bool | None,
    snapshot_status: str,
    target_status: str,
    tier: int,
    scheduled_for: datetime.datetime | None,
    checked_at: datetime.datetime,
    published_at: datetime.datetime | None,
    package_data: dict[str, Any],
) -> OutcomeObservation:
    """Construct an immutable OutcomeObservation tying snapshot to lineage."""
    horizon = resolve_horizon(
        tier=tier,
        scheduled_for=scheduled_for,
        published_at=published_at,
        checked_at=checked_at,
    )

    elapsed_sec = None
    if published_at and checked_at:
        elapsed_sec = max(0.0, (checked_at - published_at).total_seconds())

    derived = compute_derived_metrics(
        target_platform=target_platform,
        raw_metrics=raw_metrics,
        normalized_metrics=normalized_metrics,
        metric_deltas=metric_deltas,
        elapsed_seconds=elapsed_sec,
    )

    quality = classify_data_quality(
        target_status=target_status,
        snapshot_status=snapshot_status,
        object_exists=object_exists,
        content_match=content_match,
        normalized_metrics=normalized_metrics,
    )

    if snapshot_status == AuditResultStatus.MODIFIED.value or content_match is False:
        integrity = ContentIntegrityStatus.MODIFIED
    elif snapshot_status == AuditResultStatus.DELETED_OR_NOT_FOUND.value or not object_exists:
        integrity = ContentIntegrityStatus.DELETED_OR_NOT_FOUND
    elif content_match is True:
        integrity = ContentIntegrityStatus.MATCH
    else:
        integrity = ContentIntegrityStatus.UNKNOWN

    # Extract router context
    router_res = package_data.get("router_result") or {}
    router_category = router_res.get("primary_type") or router_res.get("primary_category")
    router_risk = router_res.get("risk") or router_res.get("risk_level")
    content_type = router_category

    # Extract priority context
    priority_res = package_data.get("priority_result") or {}
    score_data = priority_res.get("score") or {}
    priority_score = score_data.get("overall") if isinstance(score_data, dict) else None
    priority_band = priority_res.get("tier")

    # Extract language context
    lang_ctx = package_data.get("language_context") or {}
    language_code = lang_ctx.get("detected_language_code") or lang_ctx.get("user_output_language_code")

    outcome_id = f"out_{uuid.uuid4().hex[:16]}"

    return OutcomeObservation(
        outcome_id=outcome_id,
        job_id=job_id,
        package_id=package_id,
        publication_key=publication_key,
        target=target_platform,
        variant_type=variant_type,
        content_type=content_type,
        router_primary_category=router_category,
        router_risk_level=router_risk,
        priority_score=priority_score,
        priority_band=priority_band,
        language_code=language_code,
        published_at=published_at,
        audit_horizon=horizon,
        audit_snapshot_id=snapshot_id,
        metrics=raw_metrics,
        derived_metrics=derived,
        content_integrity_status=integrity,
        data_quality_status=quality,
        created_at=checked_at or _utc_now(),
    )


async def record_outcome_observation(
    session,
    snapshot: AuditSnapshotModel,
    target: AuditTargetModel,
    package: ContentPackageModel,
    delivery: ContentDeliveryModel | None = None,
) -> OutcomeObservationModel | None:
    """Record an OutcomeObservationModel idempotently in shadow mode.

    Guarantees:
    - Never throws an uncaught error to break calling scheduler.
    - Zero duplicate outcome per (publication_key, audit_horizon).
    - Source metrics remain immutable.
    """
    try:
        target_platform = TargetPlatform(target.target)
        variant_type = OutputVariantType(target.variant) if target.variant else OutputVariantType.X_POST

        published_at = delivery.finished_at if delivery else None
        if not published_at:
            published_at = target.created_at

        horizon = resolve_horizon(
            tier=target.tier,
            scheduled_for=snapshot.scheduled_for,
            published_at=published_at,
            checked_at=snapshot.checked_at,
        )

        # Uniqueness invariant check: (publication_key, audit_horizon)
        existing_stmt = select(OutcomeObservationModel).where(
            OutcomeObservationModel.publication_key == target.publication_key,
            OutcomeObservationModel.audit_horizon == horizon.value,
        )
        existing = (await session.execute(existing_stmt)).scalars().first()
        if existing:
            logger.info(
                "Outcome observation already exists for publication_key=%s horizon=%s (idempotent no-op)",
                target.publication_key,
                horizon.value,
            )
            return existing

        package_data = {
            "router_result": package.router_result,
            "priority_result": package.priority_result,
            "language_context": package.language_context,
        }

        observation = build_outcome_observation(
            snapshot_id=snapshot.id,
            target_id=target.id,
            package_id=package.id,
            job_id=package.job_id,
            publication_key=target.publication_key,
            target_platform=target_platform,
            variant_type=variant_type,
            raw_metrics=snapshot.metrics or {},
            normalized_metrics=snapshot.normalized_metrics or {},
            metric_deltas=snapshot.metric_deltas,
            object_exists=snapshot.object_exists,
            content_match=snapshot.content_match,
            snapshot_status=snapshot.status,
            target_status=target.status,
            tier=target.tier,
            scheduled_for=snapshot.scheduled_for,
            checked_at=snapshot.checked_at,
            published_at=published_at,
            package_data=package_data,
        )

        model = OutcomeObservationModel(
            id=observation.outcome_id,
            outcome_id=observation.outcome_id,
            job_id=observation.job_id,
            package_id=observation.package_id,
            publication_key=observation.publication_key,
            target=observation.target.value,
            variant_type=observation.variant_type.value,
            content_type=observation.content_type,
            router_primary_category=observation.router_primary_category,
            router_risk_level=observation.router_risk_level,
            priority_score=observation.priority_score,
            priority_band=observation.priority_band,
            language_code=observation.language_code,
            published_at=observation.published_at,
            audit_horizon=observation.audit_horizon.value,
            audit_snapshot_id=observation.audit_snapshot_id,
            metrics=observation.metrics,
            derived_metrics=observation.derived_metrics.model_dump(),
            content_integrity_status=observation.content_integrity_status.value,
            data_quality_status=observation.data_quality_status.value,
            created_at=observation.created_at,
        )
        session.add(model)
        logger.info(
            "Recorded outcome observation %s (pub=%s horizon=%s quality=%s)",
            model.id,
            model.publication_key,
            model.audit_horizon,
            model.data_quality_status,
        )
        return model

    except Exception:
        logger.exception("Failed to record outcome observation")
        return None


async def backfill_historical_outcomes(session) -> tuple[int, list[str]]:
    """Idempotently backfill outcome observations from historical audit snapshots.

    Returns:
        tuple[int, list[str]]: (number of backfilled observations, list of outcome_ids created)
    """
    stmt = (
        select(AuditSnapshotModel, AuditTargetModel, ContentPackageModel)
        .join(AuditTargetModel, AuditSnapshotModel.audit_id == AuditTargetModel.id)
        .join(ContentPackageModel, AuditTargetModel.package_id == ContentPackageModel.id)
    )
    results = (await session.execute(stmt)).all()

    created_ids: list[str] = []
    for snapshot_row, target_row, package_row in results:
        # Check delivery if available
        del_stmt = select(ContentDeliveryModel).where(
            ContentDeliveryModel.id == target_row.delivery_id
        )
        delivery_row = (await session.execute(del_stmt)).scalars().first()

        model = await record_outcome_observation(
            session=session,
            snapshot=snapshot_row,
            target=target_row,
            package=package_row,
            delivery=delivery_row,
        )
        if model and model.id not in created_ids:
            created_ids.append(model.id)

    if created_ids:
        await session.commit()

    return len(created_ids), created_ids
