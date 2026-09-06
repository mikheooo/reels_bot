"""Typed contract, lifecycle state machine, DistributionTarget and DeliveryRecord models for Content Factory."""

from __future__ import annotations

import datetime
import hashlib
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.worker.output_variants import (
    CanonicalContentResult,
    OutputVariantsPayload,
    OutputVariantType,
    RenderedVariant,
    render_telegram_long,
    render_threads_post,
    render_tldr,
    render_x_post,
    render_youtube_community,
)

CONTRACT_VERSION = "content_package_v1"


class PackageStatus(str, Enum):
    GENERATED = "GENERATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    DELIVERY_PENDING = "DELIVERY_PENDING"
    PARTIALLY_DELIVERED = "PARTIALLY_DELIVERED"
    DELIVERED = "DELIVERED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class TargetPlatform(str, Enum):
    TELEGRAM_USER = "TELEGRAM_USER"
    TELEGRAM_CHANNEL = "TELEGRAM_CHANNEL"
    X = "X"
    THREADS = "THREADS"
    YOUTUBE_COMMUNITY = "YOUTUBE_COMMUNITY"


class PublicationMode(str, Enum):
    AUTOMATIC = "AUTOMATIC"
    MANUAL_APPROVAL = "MANUAL_APPROVAL"
    MANUAL_EXPORT = "MANUAL_EXPORT"


class TargetDeliveryStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    APPROVED_NOT_CONNECTED = "APPROVED_NOT_CONNECTED"
    SUPPORTED_NOT_CONFIGURED = "SUPPORTED_NOT_CONFIGURED"
    READY_FOR_MANUAL_PUBLISH = "READY_FOR_MANUAL_PUBLISH"
    MANUAL_EXPORT_ACKNOWLEDGED = "MANUAL_EXPORT_ACKNOWLEDGED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    DELIVERED = "DELIVERED"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    NOT_RENDERABLE = "NOT_RENDERABLE"


class PublicationIntentStatus(str, Enum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    APPROVAL_STALE = "APPROVAL_STALE"
    SUPPORTED_NOT_CONFIGURED = "SUPPORTED_NOT_CONFIGURED"
    MANUAL_EXPORT_READY = "MANUAL_EXPORT_READY"


def compute_payload_hash(text: str) -> str:
    """Compute deterministic SHA-256 hash of normalized variant text."""
    normalized = (text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()



class DeliveryOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    APPROVED_NOT_CONNECTED = "APPROVED_NOT_CONNECTED"
    FAILED = "FAILED"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    REJECTED = "REJECTED"
    NOT_RENDERABLE = "NOT_RENDERABLE"


class InvalidLifecycleTransitionError(Exception):
    """Raised when an illegal status transition is attempted on a ContentPackage."""


class UnauthorizedApprovalError(Exception):
    """Raised when a user who does not own the package attempts to approve or reject it."""


class DistributionTarget(BaseModel):
    target: TargetPlatform
    variant_type: OutputVariantType
    publication_mode: PublicationMode
    required_approval: bool
    status: TargetDeliveryStatus = TargetDeliveryStatus.PENDING_APPROVAL
    external_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempted_at: datetime.datetime | None = None
    delivered_at: datetime.datetime | None = None
    rendered_payload: str | None = None


def _utc_now() -> datetime.datetime:
    """Return timezone-naive UTC datetime for consistent asyncpg/PostgreSQL storage."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class DeliveryRecord(BaseModel):
    delivery_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    package_id: str
    target: TargetPlatform
    variant: OutputVariantType
    attempt_id: int = 1
    approval_state: PackageStatus
    status: DeliveryOutcome
    external_id: str | None = None
    started_at: datetime.datetime = Field(default_factory=_utc_now)
    finished_at: datetime.datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    idempotency_key: str
    publication_key: str | None = None
    payload_hash: str | None = None
    provider_post_id: str | None = None
    provider_url: str | None = None
    retry_count: int = 0
    next_retry_at: datetime.datetime | None = None

    @classmethod
    def create(
        cls,
        package_id: str,
        target: TargetPlatform,
        variant: OutputVariantType,
        attempt_id: int,
        approval_state: PackageStatus,
        status: DeliveryOutcome,
        external_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        finished_at: datetime.datetime | None = None,
        publication_key: str | None = None,
        payload_hash: str | None = None,
        provider_post_id: str | None = None,
        provider_url: str | None = None,
        retry_count: int = 0,
        next_retry_at: datetime.datetime | None = None,
    ) -> DeliveryRecord:
        key = f"{package_id}:{target.value}:{variant.value}:{attempt_id}"
        return cls(
            package_id=package_id,
            target=target,
            variant=variant,
            attempt_id=attempt_id,
            approval_state=approval_state,
            status=status,
            external_id=external_id,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished_at or _utc_now(),
            idempotency_key=key,
            publication_key=publication_key,
            payload_hash=payload_hash,
            provider_post_id=provider_post_id,
            provider_url=provider_url,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
        )


class PublicationIntent(BaseModel):
    intent_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    package_id: str
    job_id: str
    target: TargetPlatform
    variant: OutputVariantType
    approved_by: int
    approved_at: datetime.datetime = Field(default_factory=_utc_now)
    payload_hash: str
    publication_key: str
    status: PublicationIntentStatus = PublicationIntentStatus.PENDING
    attempt_count: int = 0
    next_retry_at: datetime.datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    provider_post_id: str | None = None
    provider_url: str | None = None
    plan_id: str | None = None
    scheduled_for: datetime.datetime | None = None
    attempt_started_at: datetime.datetime | None = None

    @classmethod
    def create(
        cls,
        package_id: str,
        job_id: str,
        target: TargetPlatform,
        variant: OutputVariantType,
        approved_by: int,
        payload_text: str,
        status: PublicationIntentStatus = PublicationIntentStatus.PENDING,
        plan_id: str | None = None,
        scheduled_for: datetime.datetime | None = None,
    ) -> PublicationIntent:
        p_hash = compute_payload_hash(payload_text)
        pub_key = f"{package_id}:{target.value}:{variant.value}:{p_hash[:16]}"
        return cls(
            package_id=package_id,
            job_id=job_id,
            target=target,
            variant=variant,
            approved_by=approved_by,
            payload_hash=p_hash,
            publication_key=pub_key,
            status=status,
            plan_id=plan_id,
            scheduled_for=scheduled_for,
        )


class ContentPackage(BaseModel):
    package_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str
    source_url: str
    contract_version: str = CONTRACT_VERSION
    created_at: datetime.datetime = Field(default_factory=_utc_now)
    language_context: dict[str, Any] = Field(default_factory=dict)

    router_result: dict[str, Any] = Field(default_factory=dict)
    priority_result: dict[str, Any] = Field(default_factory=dict)
    canonical_content: CanonicalContentResult
    output_variants: OutputVariantsPayload
    distribution_targets: dict[str, DistributionTarget] = Field(default_factory=dict)
    approval_state: PackageStatus = PackageStatus.GENERATED
    delivery_records: list[DeliveryRecord] = Field(default_factory=list)
    publication_intents: list[PublicationIntent] = Field(default_factory=list)
    owner_approval: Any | None = None


# Valid lifecycle state transitions map
VALID_TRANSITIONS: dict[PackageStatus, set[PackageStatus]] = {
    PackageStatus.GENERATED: {
        PackageStatus.REVIEW_REQUIRED,
        PackageStatus.APPROVED,
        PackageStatus.DELIVERY_PENDING,
        PackageStatus.PARTIALLY_DELIVERED,
        PackageStatus.FAILED,
    },
    PackageStatus.REVIEW_REQUIRED: {
        PackageStatus.APPROVED,
        PackageStatus.DELIVERY_PENDING,
        PackageStatus.PARTIALLY_DELIVERED,
        PackageStatus.DELIVERED,
        PackageStatus.REJECTED,
        PackageStatus.FAILED,
    },
    PackageStatus.APPROVED: {
        PackageStatus.DELIVERY_PENDING,
        PackageStatus.PARTIALLY_DELIVERED,
        PackageStatus.DELIVERED,
        PackageStatus.FAILED,
    },
    PackageStatus.DELIVERY_PENDING: {
        PackageStatus.PARTIALLY_DELIVERED,
        PackageStatus.DELIVERED,
        PackageStatus.FAILED,
    },
    PackageStatus.PARTIALLY_DELIVERED: {
        PackageStatus.APPROVED,
        PackageStatus.DELIVERY_PENDING,
        PackageStatus.DELIVERED,
        PackageStatus.REJECTED,
        PackageStatus.FAILED,
    },
    PackageStatus.DELIVERED: set(),  # Terminal
    PackageStatus.REJECTED: set(),  # Terminal
    PackageStatus.FAILED: set(),  # Terminal
}



def transition_package_status(
    package: ContentPackage, new_status: PackageStatus, reason: str = ""
) -> ContentPackage:
    """Validate and transition package lifecycle state."""
    current = package.approval_state
    if current == new_status:
        return package

    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise InvalidLifecycleTransitionError(
            f"Invalid package lifecycle transition: cannot move from {current.value} to {new_status.value} "
            f"(reason: {reason or 'none specified'}). Allowed transitions: {[s.value for s in allowed]}"
        )

    package.approval_state = new_status
    return package


def build_content_package(
    job_id: str,
    source_url: str,
    canonical: CanonicalContentResult,
    variants: OutputVariantsPayload,
    language_context: dict[str, Any] | None = None,
    router_decision: dict[str, Any] | None = None,
    priority_result: dict[str, Any] | None = None,
) -> ContentPackage:
    """Build a new ContentPackage with default distribution targets."""
    pkg_id = str(uuid.uuid4())

    # Map rendered variant texts
    variant_texts: dict[str, str | None] = {}
    variant_statuses: dict[str, str] = {}
    for v_key, v_val in variants.variants.items():
        variant_texts[v_key] = v_val.text if v_val.status == "RENDERED" else None
        variant_statuses[v_key] = v_val.status

    targets: dict[str, DistributionTarget] = {}

    # 1. Telegram User Target (Automatic, TELEGRAM_LONG)
    tg_user_status = (
        TargetDeliveryStatus.PENDING_APPROVAL
        if variant_statuses.get(OutputVariantType.TELEGRAM_LONG.value) == "RENDERED"
        else TargetDeliveryStatus.NOT_RENDERABLE
    )
    targets[TargetPlatform.TELEGRAM_USER.value] = DistributionTarget(
        target=TargetPlatform.TELEGRAM_USER,
        variant_type=OutputVariantType.TELEGRAM_LONG,
        publication_mode=PublicationMode.AUTOMATIC,
        required_approval=False,
        status=tg_user_status,
        rendered_payload=variant_texts.get(OutputVariantType.TELEGRAM_LONG.value),
    )

    # 2. Telegram Channel Target (Policy-driven automatic, TELEGRAM_LONG)
    targets[TargetPlatform.TELEGRAM_CHANNEL.value] = DistributionTarget(
        target=TargetPlatform.TELEGRAM_CHANNEL,
        variant_type=OutputVariantType.TELEGRAM_LONG,
        publication_mode=PublicationMode.AUTOMATIC,
        required_approval=False,
        status=tg_user_status,
        rendered_payload=variant_texts.get(OutputVariantType.TELEGRAM_LONG.value),
    )

    # 3. X Target (Manual approval required, strict budget)
    x_rendered = variant_statuses.get(OutputVariantType.X_POST.value) == "RENDERED"
    targets[TargetPlatform.X.value] = DistributionTarget(
        target=TargetPlatform.X,
        variant_type=OutputVariantType.X_POST,
        publication_mode=PublicationMode.MANUAL_APPROVAL,
        required_approval=True,
        status=(
            TargetDeliveryStatus.PENDING_APPROVAL
            if x_rendered
            else TargetDeliveryStatus.NOT_RENDERABLE
        ),
        rendered_payload=variant_texts.get(OutputVariantType.X_POST.value),
    )

    # 4. Threads Target (Manual approval required)
    thr_rendered = (
        variant_statuses.get(OutputVariantType.THREADS_POST.value) == "RENDERED"
    )
    targets[TargetPlatform.THREADS.value] = DistributionTarget(
        target=TargetPlatform.THREADS,
        variant_type=OutputVariantType.THREADS_POST,
        publication_mode=PublicationMode.MANUAL_APPROVAL,
        required_approval=True,
        status=(
            TargetDeliveryStatus.PENDING_APPROVAL
            if thr_rendered
            else TargetDeliveryStatus.NOT_RENDERABLE
        ),
        rendered_payload=variant_texts.get(OutputVariantType.THREADS_POST.value),
    )

    # 5. YouTube Community Target (Manual approval required)
    yt_rendered = (
        variant_statuses.get(OutputVariantType.YOUTUBE_COMMUNITY.value) == "RENDERED"
    )
    targets[TargetPlatform.YOUTUBE_COMMUNITY.value] = DistributionTarget(
        target=TargetPlatform.YOUTUBE_COMMUNITY,
        variant_type=OutputVariantType.YOUTUBE_COMMUNITY,
        publication_mode=PublicationMode.MANUAL_APPROVAL,
        required_approval=True,
        status=(
            TargetDeliveryStatus.PENDING_APPROVAL
            if yt_rendered
            else TargetDeliveryStatus.NOT_RENDERABLE
        ),
        rendered_payload=variant_texts.get(OutputVariantType.YOUTUBE_COMMUNITY.value),
    )

    # Invariant: Generation != Approval
    # The package is initially GENERATED, and moves to REVIEW_REQUIRED because external targets require manual approval
    package = ContentPackage(
        package_id=pkg_id,
        job_id=job_id,
        source_url=source_url,
        contract_version=CONTRACT_VERSION,
        language_context=language_context or {},
        router_result=router_decision or {},
        priority_result=priority_result or {},
        canonical_content=canonical,
        output_variants=variants,
        distribution_targets=targets,
        approval_state=PackageStatus.GENERATED,
    )

    transition_package_status(
        package, PackageStatus.REVIEW_REQUIRED, "External targets require review"
    )
    return package


def reconcile_package_status(package: ContentPackage) -> PackageStatus:
    """
    Reconcile package lifecycle status based on distribution target states.

    In Level B:
    - REVIEW_REQUIRED: if any target is PENDING_APPROVAL and package is in GENERATED.
    - DELIVERY_PENDING: if any connected target is APPROVED or DELIVERY_UNKNOWN.
    - DELIVERED: only when all required connected targets reached terminal outcome.
    - REJECTED / FAILED: preserved if explicitly transitioned.
    """
    if package.approval_state in (PackageStatus.REJECTED, PackageStatus.FAILED):
        return package.approval_state

    # Check terminal / resolved outcomes first
    resolved_statuses = (
        TargetDeliveryStatus.DELIVERED,
        TargetDeliveryStatus.APPROVED_NOT_CONNECTED,
        TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED,
        TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH,
        TargetDeliveryStatus.MANUAL_EXPORT_ACKNOWLEDGED,
        TargetDeliveryStatus.SKIPPED_DUPLICATE,
        TargetDeliveryStatus.NOT_RENDERABLE,
        TargetDeliveryStatus.REJECTED,
        TargetDeliveryStatus.FAILED,
    )
    all_resolved = all(
        t.status in resolved_statuses
        for t in package.distribution_targets.values()
    )

    if all_resolved:
        has_failed = any(
            t.status == TargetDeliveryStatus.FAILED
            for t in package.distribution_targets.values()
        )
        has_delivered = any(
            t.status == TargetDeliveryStatus.DELIVERED
            for t in package.distribution_targets.values()
        )
        if has_failed and has_delivered:
            return PackageStatus.PARTIALLY_DELIVERED
        elif has_failed and not has_delivered:
            return PackageStatus.FAILED
        else:
            return PackageStatus.DELIVERED

    # Check for in-flight / active approved deliveries
    has_in_flight = any(
        t.status in (TargetDeliveryStatus.APPROVED, TargetDeliveryStatus.DELIVERY_UNKNOWN)
        for t in package.distribution_targets.values()
    )
    if has_in_flight:
        tg_delivered = any(
            t.target in (TargetPlatform.TELEGRAM_USER, TargetPlatform.TELEGRAM_CHANNEL)
            and t.status == TargetDeliveryStatus.DELIVERED
            for t in package.distribution_targets.values()
        )
        if tg_delivered:
            return PackageStatus.PARTIALLY_DELIVERED
        return PackageStatus.DELIVERY_PENDING

    # Only transition to REVIEW_REQUIRED if we are in GENERATED
    if package.approval_state == PackageStatus.GENERATED:
        has_pending_approval = any(
            t.status == TargetDeliveryStatus.PENDING_APPROVAL
            for t in package.distribution_targets.values()
        )
        if has_pending_approval:
            return PackageStatus.REVIEW_REQUIRED

    return package.approval_state


def approve_package(
    package: ContentPackage,
    user_id: int,
    expected_owner_id: int,
    target_platform: TargetPlatform | None = None,
    use_connectors: bool = False,
    connector_registry: Any | None = None,
) -> tuple[ContentPackage, list[DeliveryRecord]]:
    """
    Approve external distribution targets in a ContentPackage.

    Security check: user_id must equal expected_owner_id.
    use_connectors=False: Level A boundary (APPROVED_NOT_CONNECTED).
    use_connectors=True: Level B boundary (persists PublicationIntent, inspects connector status).
    Idempotent: double approval returns existing records without re-publishing.
    """
    if user_id != expected_owner_id:
        raise UnauthorizedApprovalError(
            f"User {user_id} is not authorized to approve package {package.package_id} owned by {expected_owner_id}"
        )

    if package.approval_state == PackageStatus.REJECTED:
        raise InvalidLifecycleTransitionError(
            f"Cannot approve rejected package {package.package_id}"
        )

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    new_records: list[DeliveryRecord] = []

    # Determine which targets to approve
    target_keys: list[str]
    if target_platform:
        target_keys = [target_platform.value]
    else:
        # All external targets requiring approval
        target_keys = [
            TargetPlatform.X.value,
            TargetPlatform.THREADS.value,
            TargetPlatform.YOUTUBE_COMMUNITY.value,
        ]

    # Exact OwnerApproval binding
    from app.worker.publication_orchestrator import (
        OwnerApproval,
        compute_package_content_hash,
    )

    approved_platforms = [
        TargetPlatform(k) for k in target_keys if k in package.distribution_targets
    ]
    package.owner_approval = OwnerApproval(
        package_id=package.package_id,
        owner_id=user_id,
        content_hash=compute_package_content_hash(package),
        target_platforms=approved_platforms,
        approved_at=now,
    )

    any_updated = False
    for k in target_keys:
        t = package.distribution_targets.get(k)
        if not t:
            continue

        # If already approved or terminal, idempotent no-op
        if t.status in (
            TargetDeliveryStatus.APPROVED,
            TargetDeliveryStatus.APPROVED_NOT_CONNECTED,
            TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED,
            TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH,
            TargetDeliveryStatus.MANUAL_EXPORT_ACKNOWLEDGED,
            TargetDeliveryStatus.DELIVERED,
            TargetDeliveryStatus.NOT_RENDERABLE,
            TargetDeliveryStatus.REJECTED,
        ):
            continue

        # Count previous attempts for idempotency attempt_id
        attempt_id = (
            sum(
                1
                for r in package.delivery_records
                if r.target.value == t.target.value
            )
            + 1
        )

        # Get payload text and stable hash
        variants_dict = getattr(package.output_variants, "variants", {}) if hasattr(package, "output_variants") else {}
        v = variants_dict.get(t.variant_type.value)
        payload_text = (v.text if v else t.rendered_payload) or ""
        p_hash = compute_payload_hash(payload_text)
        pub_key = f"{package.package_id}:{t.target.value}:{t.variant_type.value}:{p_hash[:16]}"

        if use_connectors:
            # Level B boundary: connector status resolution
            if t.target == TargetPlatform.YOUTUBE_COMMUNITY:
                t.status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
                t.attempted_at = now
                intent = PublicationIntent(
                    package_id=package.package_id,
                    job_id=package.job_id,
                    target=t.target,
                    variant=t.variant_type,
                    approved_by=user_id,
                    approved_at=now,
                    payload_hash=p_hash,
                    publication_key=pub_key,
                    status=PublicationIntentStatus.MANUAL_EXPORT_READY,
                )
                if not any(i.publication_key == pub_key for i in package.publication_intents):
                    package.publication_intents.append(intent)

                record = DeliveryRecord.create(
                    package_id=package.package_id,
                    target=t.target,
                    variant=t.variant_type,
                    attempt_id=attempt_id,
                    approval_state=PackageStatus.APPROVED,
                    status=DeliveryOutcome.APPROVED_NOT_CONNECTED,
                    finished_at=now,
                    publication_key=pub_key,
                    payload_hash=p_hash,
                    error_code="MANUAL_EXPORT_ONLY",
                    error_message="YouTube Community Post publishing is unsupported by official API. Manual export required.",
                )
                package.delivery_records.append(record)
                new_records.append(record)
                any_updated = True
            else:
                # X or Threads connector
                reg = connector_registry
                if reg is None:
                    try:
                        from app.worker.connectors import ConnectorRegistry
                        reg = ConnectorRegistry
                    except ImportError:
                        reg = None

                connector = reg.get_connector(t.target) if reg else None
                caps = connector.capabilities() if connector else None

                if caps and caps.status.value == "CONNECTED_SUPPORTED":
                    t.status = TargetDeliveryStatus.APPROVED
                    t.attempted_at = now
                    intent = PublicationIntent(
                        package_id=package.package_id,
                        job_id=package.job_id,
                        target=t.target,
                        variant=t.variant_type,
                        approved_by=user_id,
                        approved_at=now,
                        payload_hash=p_hash,
                        publication_key=pub_key,
                        status=PublicationIntentStatus.PENDING,
                    )
                    if not any(i.publication_key == pub_key for i in package.publication_intents):
                        package.publication_intents.append(intent)
                    any_updated = True
                else:
                    # Supported not configured
                    t.status = TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED
                    t.attempted_at = now
                    intent = PublicationIntent(
                        package_id=package.package_id,
                        job_id=package.job_id,
                        target=t.target,
                        variant=t.variant_type,
                        approved_by=user_id,
                        approved_at=now,
                        payload_hash=p_hash,
                        publication_key=pub_key,
                        status=PublicationIntentStatus.SUPPORTED_NOT_CONFIGURED,
                    )
                    if not any(i.publication_key == pub_key for i in package.publication_intents):
                        package.publication_intents.append(intent)

                    record = DeliveryRecord.create(
                        package_id=package.package_id,
                        target=t.target,
                        variant=t.variant_type,
                        attempt_id=attempt_id,
                        approval_state=PackageStatus.APPROVED,
                        status=DeliveryOutcome.APPROVED_NOT_CONNECTED,
                        finished_at=now,
                        publication_key=pub_key,
                        payload_hash=p_hash,
                        error_code="SUPPORTED_NOT_CONFIGURED",
                        error_message="Connector credentials not configured in environment.",
                    )
                    package.delivery_records.append(record)
                    new_records.append(record)
                    any_updated = True
        else:
            # Level A Publication Boundary: transition to APPROVED_NOT_CONNECTED
            t.status = TargetDeliveryStatus.APPROVED_NOT_CONNECTED
            t.attempted_at = now
            t.delivered_at = now

            intent = PublicationIntent(
                package_id=package.package_id,
                job_id=package.job_id,
                target=t.target,
                variant=t.variant_type,
                approved_by=user_id,
                approved_at=now,
                payload_hash=p_hash,
                publication_key=pub_key,
                status=PublicationIntentStatus.SUPPORTED_NOT_CONFIGURED,
            )
            if not any(i.publication_key == pub_key for i in package.publication_intents):
                package.publication_intents.append(intent)

            record = DeliveryRecord.create(
                package_id=package.package_id,
                target=t.target,
                variant=t.variant_type,
                attempt_id=attempt_id,
                approval_state=PackageStatus.APPROVED,
                status=DeliveryOutcome.APPROVED_NOT_CONNECTED,
                finished_at=now,
                publication_key=pub_key,
                payload_hash=p_hash,
            )
            package.delivery_records.append(record)
            new_records.append(record)
            any_updated = True

    if not any_updated:
        return package, new_records

    # Reconcile package lifecycle status
    new_state = reconcile_package_status(package)
    if new_state != package.approval_state and new_state in VALID_TRANSITIONS.get(package.approval_state, set()):
        transition_package_status(
            package, new_state, f"Approved by user {user_id}"
        )
    elif any_updated and package.approval_state in (
        PackageStatus.GENERATED,
        PackageStatus.REVIEW_REQUIRED,
    ):
        transition_package_status(
            package, PackageStatus.APPROVED, f"Approved by user {user_id}"
        )

    return package, new_records


def reject_package(
    package: ContentPackage,
    user_id: int,
    expected_owner_id: int,
    reason: str = "Rejected by reviewer",
) -> ContentPackage:
    """Reject a package and block further distribution."""
    if user_id != expected_owner_id:
        raise UnauthorizedApprovalError(
            f"User {user_id} is not authorized to reject package {package.package_id} owned by {expected_owner_id}"
        )

    if package.approval_state == PackageStatus.DELIVERED:
        raise InvalidLifecycleTransitionError(
            f"Cannot reject already DELIVERED package {package.package_id}"
        )

    transition_package_status(package, PackageStatus.REJECTED, reason)

    # Mark non-delivered targets as REJECTED
    now = datetime.datetime.now(datetime.timezone.utc)
    for t in package.distribution_targets.values():
        if t.status in (
            TargetDeliveryStatus.PENDING_APPROVAL,
            TargetDeliveryStatus.APPROVED,
        ):
            t.status = TargetDeliveryStatus.REJECTED
            attempt_id = (
                sum(
                    1
                    for r in package.delivery_records
                    if r.target.value == t.target.value
                )
                + 1
            )
            record = DeliveryRecord.create(
                package_id=package.package_id,
                target=t.target,
                variant=t.variant_type,
                attempt_id=attempt_id,
                approval_state=PackageStatus.REJECTED,
                status=DeliveryOutcome.REJECTED,
                error_message=reason,
                finished_at=now,
            )
            package.delivery_records.append(record)

    return package


def regenerate_variant_from_canonical(
    canonical: CanonicalContentResult,
    variant_type: OutputVariantType,
) -> RenderedVariant:
    """
    Deterministic variant regeneration strictly using CanonicalContentResult.

    NEVER touches raw transcript or invents facts. Preserves epistemic status,
    risk level, critical disclaimers, and verified claims verbatim.
    """
    if variant_type == OutputVariantType.TLDR:
        return render_tldr(canonical)
    if variant_type == OutputVariantType.TELEGRAM_LONG:
        return render_telegram_long(canonical)
    if variant_type == OutputVariantType.X_POST:
        return render_x_post(canonical)
    if variant_type == OutputVariantType.THREADS_POST:
        return render_threads_post(canonical)
    if variant_type == OutputVariantType.YOUTUBE_COMMUNITY:
        return render_youtube_community(canonical)

    raise ValueError(f"Unknown variant type: {variant_type}")


def classify_delivery_error(exc: Exception | str) -> tuple[bool, str, str]:
    """
    Classify a delivery error into retryable vs non-retryable.

    Returns:
        (is_retryable, error_code, error_message)
    """
    err_str = str(exc).lower()

    # Retryable transient conditions
    retryable_patterns = [
        ("504", "GATEWAY_TIMEOUT_504"),
        ("503", "SERVICE_UNAVAILABLE_503"),
        ("502", "BAD_GATEWAY_502"),
        ("500", "SERVER_ERROR_500"),
        ("429", "RATE_LIMIT_429"),
        ("rate limit", "RATE_LIMIT_429"),
        ("timeout", "TIMEOUT"),
        ("timed out", "TIMEOUT"),
        ("connection reset", "CONNECTION_RESET"),
        ("connection refused", "CONNECTION_REFUSED"),
        ("temporarily unavailable", "TEMPORARILY_UNAVAILABLE"),
    ]

    for pattern, code in retryable_patterns:
        if pattern in err_str:
            return True, code, str(exc)

    # Non-retryable conditions
    non_retryable_patterns = [
        ("invalid credential", "AUTH_INVALID_CREDENTIALS"),
        ("unauthorized", "AUTH_UNAUTHORIZED"),
        ("401", "AUTH_UNAUTHORIZED"),
        ("403", "AUTH_FORBIDDEN"),
        ("not renderable", "NOT_RENDERABLE"),
        ("budget exceeded", "BUDGET_EXCEEDED"),
        ("length exceeded", "LENGTH_EXCEEDED"),
        ("character limit", "LENGTH_EXCEEDED"),
        ("invalid format", "INVALID_FORMAT"),
        ("rejected", "REJECTED"),
    ]

    for pattern, code in non_retryable_patterns:
        if pattern in err_str:
            return False, code, str(exc)

    return False, "UNKNOWN_ERROR", str(exc)
