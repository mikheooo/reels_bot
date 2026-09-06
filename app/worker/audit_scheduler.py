import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import AuditSnapshotModel, AuditTargetModel
from app.worker.audit_schemas import (
    AuditPolicy,
    AuditResultStatus,
    AuditSnapshot,
    AuditTarget,
    AuditTargetStatus,
    NormalizedMetrics,
)
from app.worker.connectors import ConnectorRegistry, TargetPlatform
from app.worker.content_package import DeliveryOutcome, DeliveryRecord
from app.worker.post_publish_audit import (
    evaluate_audit_eligibility,
    perform_audit_check,
    should_send_alert,
)
from app.worker.tasks import _to_naive_utc

logger = logging.getLogger(__name__)


async def register_audit_target_if_eligible(
    session,
    delivery_record: DeliveryRecord,
    approved_text: str,
    package_id: str,
    policy: AuditPolicy | None = None,
    now: datetime | None = None,
) -> AuditTargetModel | None:
    """Inspect a delivery record and register an AuditTargetModel in the database.

    Enforces:
    - Truthful eligibility: unconfigured connectors resolve to NOT_APPLICABLE_NOT_CONFIGURED.
    - Zero overdue schedule pollution: next_audit_at is None for unconfigured/unsupported targets.
    """
    current_time = now or datetime.now(timezone.utc)
    target_platform = delivery_record.target

    try:
        connector = ConnectorRegistry.get_connector(target_platform)
        caps = connector.capabilities()
        connector_status = caps.status
    except Exception as e:
        logger.warning("Could not resolve connector for target %s: %s", target_platform, e)
        return None

    is_eligible, initial_status, next_audit = evaluate_audit_eligibility(
        target=target_platform,
        delivery_status=delivery_record.status,
        provider_post_id=delivery_record.provider_post_id,
        connector_status=connector_status,
        policy=policy,
        now=current_time,
    )

    audit_id = f"aud_{delivery_record.delivery_id}"
    existing = await session.get(AuditTargetModel, audit_id)

    if existing:
        existing.status = initial_status.value
        existing.next_audit_at = _to_naive_utc(next_audit)
        existing.updated_at = _to_naive_utc(current_time)
        return existing

    target_model = AuditTargetModel(
        id=audit_id,
        package_id=package_id,
        delivery_id=delivery_record.delivery_id,
        target=target_platform.value,
        variant=delivery_record.variant.value if delivery_record.variant else None,
        provider_post_id=delivery_record.provider_post_id or "",
        provider_url=delivery_record.provider_url,
        publication_key=delivery_record.publication_key or "",
        approved_payload_hash=delivery_record.payload_hash or "",
        approved_payload_text=approved_text,
        status=initial_status.value,
        tier=0,
        next_audit_at=_to_naive_utc(next_audit),
        attempt_count=0,
        created_at=_to_naive_utc(current_time),
        updated_at=_to_naive_utc(current_time),
    )
    session.add(target_model)
    return target_model


async def cron_audit_v2_jobs(ctx=None) -> int:
    """Scheduled cron task executing pending v2 audit targets.

    Concurrency & Invariants:
    - Uses FOR UPDATE SKIP LOCKED to prevent duplicate simultaneous worker execution.
    - Idempotency enforced via unique occurrence_key in audit_snapshots.
    - Strictly queries audit_targets table, ignoring legacy jobs table records.
    - Advances schedule tiers or halts polling upon terminal anomaly (DELETED, AUTH_REQUIRED).
    """
    now = datetime.now(timezone.utc)
    now_naive = _to_naive_utc(now)
    policy = AuditPolicy()
    processed_count = 0

    async with AsyncSessionLocal() as session:
        stmt = (
            select(AuditTargetModel)
            .where(
                AuditTargetModel.status.in_([
                    AuditTargetStatus.SCHEDULED.value,
                    AuditTargetStatus.ACTIVE.value,
                ]),
                AuditTargetModel.next_audit_at.is_not(None),
                AuditTargetModel.next_audit_at <= now_naive,
            )
            .with_for_update(skip_locked=True)
            .limit(10)
        )
        result = await session.execute(stmt)
        targets = result.scalars().all()

        for target_row in targets:
            try:
                target_enum = TargetPlatform(target_row.target)
                connector = ConnectorRegistry.get_connector(target_enum)
            except Exception as conn_err:
                logger.error("Could not obtain connector for audit target %s: %s", target_row.id, conn_err)
                continue

            target_pydantic = AuditTarget(
                audit_id=target_row.id,
                package_id=target_row.package_id,
                delivery_id=target_row.delivery_id,
                target=target_enum,
                provider_post_id=target_row.provider_post_id,
                provider_url=target_row.provider_url,
                publication_key=target_row.publication_key,
                approved_payload_hash=target_row.approved_payload_hash,
                approved_payload_text=target_row.approved_payload_text,
                status=AuditTargetStatus(target_row.status),
                tier=target_row.tier,
                next_audit_at=target_row.next_audit_at,
                attempt_count=target_row.attempt_count,
            )

            # Fetch latest snapshot to compute metric deltas
            prev_stmt = (
                select(AuditSnapshotModel)
                .where(AuditSnapshotModel.audit_id == target_row.id)
                .order_by(AuditSnapshotModel.checked_at.desc())
                .limit(1)
            )
            prev_row = (await session.execute(prev_stmt)).scalars().first()
            prev_snapshot = None
            if prev_row:
                prev_snapshot = AuditSnapshot(
                    snapshot_id=prev_row.id,
                    audit_id=prev_row.audit_id,
                    occurrence_key=prev_row.occurrence_key,
                    checked_at=prev_row.checked_at,
                    object_exists=prev_row.object_exists,
                    content_hash=prev_row.content_hash,
                    content_match=prev_row.content_match,
                    raw_metrics=prev_row.metrics or {},
                    normalized_metrics=NormalizedMetrics.model_validate(prev_row.normalized_metrics or {}),
                    status=AuditResultStatus(prev_row.status),
                )

            audit_res, audit_snap = await perform_audit_check(
                target=target_pydantic,
                connector=connector,
                previous_snapshot=prev_snapshot,
                policy=policy,
                now=now,
            )

            # Idempotency check: verify occurrence key does not already exist
            existing_snap = (
                await session.execute(
                    select(AuditSnapshotModel).where(
                        AuditSnapshotModel.occurrence_key == audit_snap.occurrence_key
                    )
                )
            ).scalars().first()

            if not existing_snap:
                snap_row = AuditSnapshotModel(
                    id=f"asnap_{uuid.uuid4().hex[:16]}",
                    audit_id=target_row.id,
                    occurrence_key=audit_snap.occurrence_key,
                    checked_at=_to_naive_utc(audit_snap.checked_at),
                    object_exists=audit_snap.object_exists,
                    content_hash=audit_snap.content_hash,
                    content_match=audit_snap.content_match,
                    metrics=audit_snap.raw_metrics,
                    normalized_metrics=audit_snap.normalized_metrics.model_dump(),
                    metric_deltas={k: v.model_dump() for k, v in audit_snap.metric_deltas.items()},
                    provider_http_status=audit_snap.provider_http_status,
                    latency_ms=audit_snap.latency_ms,
                    status=audit_snap.status.value,
                    created_at=now_naive,
                )
                session.add(snap_row)

            # Update target state
            target_row.last_checked_at = _to_naive_utc(audit_res.checked_at)
            target_row.last_result_status = audit_res.status.value
            target_row.last_error_code = audit_res.provider_error
            target_row.attempt_count += 1
            target_row.updated_at = now_naive

            if audit_res.status == AuditResultStatus.TEMPORARILY_UNAVAILABLE:
                target_row.status = AuditTargetStatus.ACTIVE.value
                target_row.next_audit_at = _to_naive_utc(audit_res.next_audit_at)
            elif audit_res.status in (
                AuditResultStatus.DELETED_OR_NOT_FOUND,
                AuditResultStatus.AUTH_REQUIRED,
                AuditResultStatus.FAILED,
            ):
                target_row.status = audit_res.status.value
                target_row.next_audit_at = None
            else:
                target_row.status = audit_res.status.value
                target_row.tier += 1
                target_row.next_audit_at = _to_naive_utc(audit_res.next_audit_at)

            alert_needed, alert_reason = should_send_alert(audit_res)
            if alert_needed:
                logger.warning(
                    "AUDIT ALERT on target %s (%s): %s",
                    target_row.id, target_row.target, alert_reason,
                )

            processed_count += 1

        await session.commit()

    return processed_count
