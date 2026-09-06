"""Publication Orchestrator Core for ROADMAP Priority 5: Unified Automated Multi-Platform Publication after Owner Approval.

Implements:
- Strict Owner Approval Binding (Package ID, Content Hash, Target Platform Set, Owner ID, Timestamp)
- Formal State Machine (PENDING_APPROVAL, APPROVED, READY, ATTEMPTING, PUBLISHED, RETRYABLE_FAILURE, AMBIGUOUS, PERMANENT_FAILURE, CANCELLED)
- Crash Consistency across Boundaries A, B, C, D, E
- Deterministic Idempotency (publication_key & attempt_key)
- Per-Platform Isolation
- Automatic Reconciliation of Ambiguous Outages (lookup / author timeline search before retry, zero blind reposts)
- Integration with Post-Publish Audit & Telemetry v2
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import settings
from app.worker.connectors import (
    CapabilityStatus,
    ConnectorRegistry,
    PublicationConnector,
    PublicationResult,
    ReconciliationConfidence,
    sanitize_sensitive_text,
)
from app.worker.content_package import (
    ContentPackage,
    DeliveryOutcome,
    DeliveryRecord,
    InvalidLifecycleTransitionError,
    OutputVariantType,
    PackageStatus,
    PublicationIntent,
    PublicationIntentStatus,
    TargetDeliveryStatus,
    TargetPlatform,
    UnauthorizedApprovalError,
    _utc_now,
    compute_payload_hash,
    reconcile_package_status,
)

logger = logging.getLogger(__name__)


class PublicationState(str, Enum):
    """Explicit terminal and intermediate states for the publication state machine."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    READY = "READY"
    ATTEMPTING = "ATTEMPTING"
    PUBLISHED = "PUBLISHED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    AMBIGUOUS = "AMBIGUOUS"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    CANCELLED = "CANCELLED"


# Mapping from PublicationState to internal PublicationIntentStatus
STATE_TO_INTENT_STATUS: dict[PublicationState, PublicationIntentStatus] = {
    PublicationState.PENDING_APPROVAL: PublicationIntentStatus.PENDING,
    PublicationState.APPROVED: PublicationIntentStatus.PENDING,
    PublicationState.READY: PublicationIntentStatus.PENDING,
    PublicationState.ATTEMPTING: PublicationIntentStatus.IN_FLIGHT,
    PublicationState.PUBLISHED: PublicationIntentStatus.SUCCEEDED,
    PublicationState.RETRYABLE_FAILURE: PublicationIntentStatus.PENDING,
    PublicationState.AMBIGUOUS: PublicationIntentStatus.DELIVERY_UNKNOWN,
    PublicationState.PERMANENT_FAILURE: PublicationIntentStatus.FAILED,
    PublicationState.CANCELLED: PublicationIntentStatus.FAILED,
}


def compute_package_content_hash(package: ContentPackage) -> str:
    """Compute deterministic SHA-256 hash binding all rendered variant payloads in the package."""
    parts = []
    variants_dict = (
        getattr(package.output_variants, "variants", {})
        if hasattr(package, "output_variants")
        else {}
    )
    if variants_dict:
        for k in sorted(variants_dict.keys()):
            v = variants_dict[k]
            text = v.text if hasattr(v, "text") else ""
            parts.append(f"{k}:{compute_payload_hash(text or '')}")
    elif package.distribution_targets:
        for k in sorted(package.distribution_targets.keys()):
            t = package.distribution_targets[k]
            text = t.rendered_payload or ""
            parts.append(f"{k}:{compute_payload_hash(text or '')}")
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


class PublicationPlanStatus(str, Enum):
    """Deterministic aggregate states for a multi-platform PublicationPlan."""

    ALL_PENDING = "ALL_PENDING"
    ATTEMPTING = "ATTEMPTING"
    ALL_SUCCEEDED = "ALL_SUCCEEDED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    RETRY_PENDING = "RETRY_PENDING"
    MANUAL_RECONCILIATION_REQUIRED = "MANUAL_RECONCILIATION_REQUIRED"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    CANCELLED = "CANCELLED"


class OwnerApproval(BaseModel):
    """Immutable owner approval record bound via SHA-256 cryptographic integrity hash to exact rendered content."""

    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    package_id: str
    owner_id: int
    content_hash: str  # Cryptographic integrity hash used to bind owner approval to exact rendered content
    target_platforms: list[TargetPlatform]
    approved_at: datetime.datetime = Field(default_factory=_utc_now)


class PlatformPublicationAttempt(BaseModel):
    """Audit log of a single connector publication attempt."""

    attempt_id: int
    attempt_key: str
    target: TargetPlatform
    started_at: datetime.datetime = Field(default_factory=_utc_now)
    finished_at: datetime.datetime | None = None
    outcome: DeliveryOutcome
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    provider_post_id: str | None = None
    provider_url: str | None = None


class PublicationPlan(BaseModel):
    """Unified multi-platform publication plan created after valid owner approval."""

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    package_id: str
    job_id: str
    approval: OwnerApproval
    scheduled_for: datetime.datetime | None = None
    intents: list[PublicationIntent] = Field(default_factory=list)
    status: PublicationPlanStatus | PublicationState = PublicationPlanStatus.ALL_PENDING
    created_at: datetime.datetime = Field(default_factory=_utc_now)


def calculate_aggregate_plan_status(plan: PublicationPlan) -> PublicationPlanStatus:
    """Deterministically calculate aggregate PublicationPlan status from individual intent states.

    Semantics:
    1. CANCELLED: plan or package was explicitly cancelled.
    2. ATTEMPTING: one or more active targets currently in flight.
    3. MANUAL_RECONCILIATION_REQUIRED: one or more targets in ambiguous DELIVERY_UNKNOWN state.
    4. RETRY_PENDING: one or more targets waiting for scheduled retry backoff.
    5. ALL_SUCCEEDED: all active targets succeeded (or only manual export targets exist).
    6. TERMINAL_FAILURE: all active targets failed permanently (or staled) with no success.
    7. ALL_PENDING: all active targets are awaiting initial dispatch.
    8. PARTIAL_SUCCESS: at least one succeeded and at least one permanently failed (with no retries remaining).
    """
    status_val = getattr(plan.status, "value", plan.status)
    if status_val in (PublicationPlanStatus.CANCELLED.value, PublicationState.CANCELLED.value, "CANCELLED"):
        return PublicationPlanStatus.CANCELLED

    if not plan.intents:
        return PublicationPlanStatus.ALL_PENDING

    active_intents = [
        i for i in plan.intents if i.status != PublicationIntentStatus.MANUAL_EXPORT_READY
    ]
    if not active_intents:
        return PublicationPlanStatus.ALL_SUCCEEDED

    if any(i.status == PublicationIntentStatus.IN_FLIGHT for i in active_intents):
        return PublicationPlanStatus.ATTEMPTING

    if any(i.status == PublicationIntentStatus.DELIVERY_UNKNOWN for i in active_intents):
        return PublicationPlanStatus.MANUAL_RECONCILIATION_REQUIRED

    has_retry_pending = any(
        i.status == PublicationIntentStatus.PENDING and (i.attempt_count > 0 or i.next_retry_at is not None)
        for i in active_intents
    )

    succeeded = [i for i in active_intents if i.status == PublicationIntentStatus.SUCCEEDED]
    failed = [
        i
        for i in active_intents
        if i.status in (PublicationIntentStatus.FAILED, PublicationIntentStatus.APPROVAL_STALE)
    ]
    initial_pending = [
        i
        for i in active_intents
        if i.status == PublicationIntentStatus.PENDING
        and i.attempt_count == 0
        and i.next_retry_at is None
    ]

    if has_retry_pending:
        return PublicationPlanStatus.RETRY_PENDING

    if not succeeded and not failed:
        return PublicationPlanStatus.ALL_PENDING

    if len(succeeded) == len(active_intents):
        return PublicationPlanStatus.ALL_SUCCEEDED

    if len(failed) == len(active_intents):
        return PublicationPlanStatus.TERMINAL_FAILURE

    if succeeded and failed:
        return PublicationPlanStatus.PARTIAL_SUCCESS

    if succeeded and initial_pending:
        return PublicationPlanStatus.PARTIAL_SUCCESS

    if initial_pending and failed:
        return PublicationPlanStatus.PARTIAL_SUCCESS

    return PublicationPlanStatus.PARTIAL_SUCCESS


def verify_approval(
    package: ContentPackage,
    approval: OwnerApproval,
    expected_owner_id: int | None = None,
    target_platform: TargetPlatform | None = None,
) -> tuple[bool, str]:
    """Verify validity of OwnerApproval against the ContentPackage.

    Invariants:
    1. Package ID must match.
    2. Owner ID must match expected authorized owner.
    3. Package must not be in REJECTED or FAILED terminal states.
    4. Content hash (cryptographic integrity hash) must match current rendered content (staleness check).
    5. Target platform set cannot be empty; if target_platform is passed, it must be in approved targets.
    """
    if approval.package_id != package.package_id:
        return False, f"Approval package_id {approval.package_id} != package {package.package_id}"

    if expected_owner_id is not None and approval.owner_id != expected_owner_id:
        return False, f"Approval owner_id {approval.owner_id} != expected {expected_owner_id}"

    if package.approval_state in (PackageStatus.REJECTED, PackageStatus.FAILED):
        return False, f"Cannot execute publication for package in terminal state {package.approval_state.value}"

    if not approval.target_platforms:
        return False, "TARGET_SET_EMPTY: OwnerApproval has no target platforms specified"

    if target_platform is not None and target_platform not in approval.target_platforms:
        return False, f"TARGET_NOT_APPROVED: Platform {target_platform.value} was not authorized in owner approval target set"

    current_hash = compute_package_content_hash(package)
    if approval.content_hash != current_hash:
        return False, "APPROVAL_STALE: Package content was modified after owner approval"

    return True, "VALID"


class PublicationOrchestrator:
    """Production orchestration layer for multi-platform automated publication.

    Enforces:
    - Zero autonomous publication without valid owner approval
    - Per-platform isolation
    - Crash consistency across Boundaries A, B, C, D, E
    - Deterministic idempotency and duplicate prevention
    - Ambiguous outcome reconciliation (never blind repost)
    - Post-publish audit target registration
    """

    @classmethod
    def create_plan(
        cls,
        package: ContentPackage,
        approval: OwnerApproval,
        expected_owner_id: int | None = None,
        scheduled_for: datetime.datetime | None = None,
        connector_registry: Any | None = None,
    ) -> PublicationPlan:
        """Construct a PublicationPlan from a valid OwnerApproval.

        Validates approval binding, creates PublicationIntents with stable publication_keys,
        and inspects connector readiness.
        """
        is_valid, reason = verify_approval(package, approval, expected_owner_id)
        if not is_valid:
            if "owner_id" in reason or "TARGET_NOT_APPROVED" in reason or "TARGET_SET_EMPTY" in reason:
                raise UnauthorizedApprovalError(reason)
            elif "terminal state" in reason:
                raise InvalidLifecycleTransitionError(reason)
            else:
                raise ValueError(reason)

        now = _utc_now()
        plan_id = str(uuid.uuid4())
        intents: list[PublicationIntent] = []
        reg = connector_registry or ConnectorRegistry

        for target_plat in approval.target_platforms:
            t = package.distribution_targets.get(target_plat.value)
            if not t:
                continue

            # Extract payload text
            variants_dict = (
                getattr(package.output_variants, "variants", {})
                if hasattr(package, "output_variants")
                else {}
            )
            v = variants_dict.get(t.variant_type.value)
            payload_text = (v.text if v else t.rendered_payload) or ""
            p_hash = compute_payload_hash(payload_text)
            pub_key = f"{package.package_id}:{t.target.value}:{t.variant_type.value}:{p_hash[:16]}"

            # Determine initial intent status based on connector capabilities
            if target_plat == TargetPlatform.YOUTUBE_COMMUNITY:
                initial_status = PublicationIntentStatus.MANUAL_EXPORT_READY
                t.status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
            else:
                try:
                    connector = reg.get_connector(target_plat) if reg else None
                    caps = connector.capabilities() if connector else None
                except Exception:
                    caps = None

                if caps and caps.status == CapabilityStatus.CONNECTED_SUPPORTED:
                    initial_status = PublicationIntentStatus.PENDING
                    t.status = TargetDeliveryStatus.APPROVED
                else:
                    initial_status = PublicationIntentStatus.SUPPORTED_NOT_CONFIGURED
                    t.status = TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED

            t.attempted_at = now

            intent = PublicationIntent(
                package_id=package.package_id,
                job_id=package.job_id,
                target=t.target,
                variant=t.variant_type,
                approved_by=approval.owner_id,
                approved_at=approval.approved_at,
                payload_hash=p_hash,
                publication_key=pub_key,
                status=initial_status,
                plan_id=plan_id,
                scheduled_for=scheduled_for,
            )

            # Avoid duplicate intents in package
            if not any(i.publication_key == pub_key for i in package.publication_intents):
                package.publication_intents.append(intent)
            intents.append(intent)

        # Transition package status to APPROVED or DELIVERY_PENDING
        new_status = reconcile_package_status(package)
        if (
            new_status != package.approval_state
            and new_status in {PackageStatus.APPROVED, PackageStatus.DELIVERY_PENDING}
        ):
            package.approval_state = new_status

        plan = PublicationPlan(
            plan_id=plan_id,
            package_id=package.package_id,
            job_id=package.job_id,
            approval=approval,
            scheduled_for=scheduled_for,
            intents=intents,
            status=PublicationPlanStatus.ALL_PENDING,
            created_at=now,
        )
        return plan

    @classmethod
    async def execute_intent(
        cls,
        intent: PublicationIntent,
        package: ContentPackage,
        connector: PublicationConnector,
        session: Any | None = None,
        simulate_crash_after_publish: bool = False,
        now: datetime.datetime | None = None,
        approval: OwnerApproval | None = None,
    ) -> PublicationResult:
        """Execute a single PublicationIntent with strict crash consistency and idempotency.

        Crash Boundaries:
        A. Pre-dispatch: marked IN_FLIGHT before external call.
        B. In-flight / Timeout: marked DELIVERY_UNKNOWN, immediately reconciled.
        C. External Success + Local Crash: on replay, reconciliation detects post and marks SUCCEEDED.
        D. Committed Success: already SUCCEEDED -> returns immediately without duplicate call.
        E. Retry after Ambiguity: checks reconciliation first; never blind reposts.
        """
        now_naive = now or _utc_now()
        target_obj = package.distribution_targets.get(intent.target.value)
        if not target_obj:
            return PublicationResult(
                success=False,
                error_code="TARGET_NOT_FOUND",
                error_message=f"Distribution target {intent.target.value} not found in package",
            )

        # Target set authorization guard
        if approval is not None:
            is_valid, reason = verify_approval(package, approval, target_platform=intent.target)
            if not is_valid:
                target_obj.status = TargetDeliveryStatus.FAILED
                err_code = "TARGET_NOT_APPROVED" if "TARGET_NOT_APPROVED" in reason else "UNAUTHORIZED_APPROVAL"
                target_obj.error_code = err_code
                target_obj.error_message = reason
                intent.status = PublicationIntentStatus.FAILED
                intent.last_error_code = err_code
                intent.last_error_message = reason
                return PublicationResult(
                    success=False,
                    error_code=err_code,
                    error_message=reason,
                    is_retryable=False,
                )

        # Crash Boundary D: Already published -> idempotent no-op
        if intent.status == PublicationIntentStatus.SUCCEEDED:
            logger.info("Intent %s already SUCCEEDED. Idempotent skip.", intent.publication_key)
            return PublicationResult(
                success=True,
                provider_post_id=intent.provider_post_id,
                provider_url=intent.provider_url,
                error_code="ALREADY_PUBLISHED",
            )

        # Content staleness check: ensure current variant text matches approved payload hash
        variants_dict = (
            getattr(package.output_variants, "variants", {})
            if hasattr(package, "output_variants")
            else {}
        )
        current_v = variants_dict.get(intent.variant.value)
        current_text = (current_v.text if current_v else target_obj.rendered_payload) or ""
        current_hash = compute_payload_hash(current_text)

        if current_hash != intent.payload_hash:
            logger.warning(
                "STALE APPROVAL: Variant %s in package %s has hash %s != approved %s",
                intent.variant.value,
                package.package_id,
                current_hash[:16],
                intent.payload_hash[:16],
            )
            intent.status = PublicationIntentStatus.APPROVAL_STALE
            intent.last_error_code = "APPROVAL_STALE"
            intent.last_error_message = (
                "Variant text modified after owner approval. Re-approval required."
            )
            target_obj.status = TargetDeliveryStatus.FAILED
            target_obj.error_code = "APPROVAL_STALE"
            target_obj.error_message = intent.last_error_message
            return PublicationResult(
                success=False,
                error_code="APPROVAL_STALE",
                error_message=intent.last_error_message,
            )

        # Check connector capability status
        caps = connector.capabilities()
        if caps.status != CapabilityStatus.CONNECTED_SUPPORTED:
            if caps.status == CapabilityStatus.UNSUPPORTED_OFFICIAL_API:
                intent.status = PublicationIntentStatus.MANUAL_EXPORT_READY
                target_obj.status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
                return PublicationResult(
                    success=False,
                    error_code="MANUAL_EXPORT_ONLY",
                    error_message=f"{intent.target.value} requires manual export.",
                )
            else:
                intent.status = PublicationIntentStatus.SUPPORTED_NOT_CONFIGURED
                target_obj.status = TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED
                return PublicationResult(
                    success=False,
                    error_code="SUPPORTED_NOT_CONFIGURED",
                    error_message=f"{intent.target.value} credentials not configured.",
                )

        # Preflight payload validation against declared connector capabilities
        if not caps.supports_text:
            intent.status = PublicationIntentStatus.FAILED
            target_obj.status = TargetDeliveryStatus.FAILED
            err = f"Connector {intent.target.value} does not support text publishing"
            intent.last_error_code = "CAPABILITY_UNSUPPORTED"
            intent.last_error_message = err
            return PublicationResult(
                success=False,
                error_code="CAPABILITY_UNSUPPORTED",
                error_message=err,
                is_retryable=False,
            )

        if len(current_text) > caps.max_chars:
            intent.status = PublicationIntentStatus.FAILED
            target_obj.status = TargetDeliveryStatus.FAILED
            err = f"{intent.target.value} payload length {len(current_text)} exceeds maximum limit of {caps.max_chars} characters"
            intent.last_error_code = "BUDGET_EXCEEDED"
            intent.last_error_message = err
            return PublicationResult(
                success=False,
                error_code="BUDGET_EXCEEDED",
                error_message=err,
                is_retryable=False,
            )

        attempt_id = (
            sum(1 for r in package.delivery_records if r.target == intent.target) + 1
        )

        # Crash Boundary C & E: If intent was previously in-flight or ambiguous, reconcile first!
        if (
            intent.status
            in (PublicationIntentStatus.IN_FLIGHT, PublicationIntentStatus.DELIVERY_UNKNOWN)
            or intent.attempt_count > 0
        ):
            logger.info(
                "Intent %s in status %s with %d attempts. Reconciling with provider before any retry.",
                intent.publication_key,
                intent.status.value,
                intent.attempt_count,
            )
            recon = await connector.reconcile_ambiguous_delivery(intent, current_text)
            if recon.confidence == ReconciliationConfidence.CONFIRMED_PRESENT or (
                recon.resolved and recon.published
            ):
                logger.info(
                    "Reconciliation recovered existing post %s for intent %s",
                    recon.provider_post_id,
                    intent.publication_key,
                )
                intent.status = PublicationIntentStatus.SUCCEEDED
                intent.provider_post_id = recon.provider_post_id
                intent.provider_url = recon.provider_url
                intent.last_error_code = None
                intent.last_error_message = None

                target_obj.status = TargetDeliveryStatus.DELIVERED
                target_obj.external_id = recon.provider_post_id
                target_obj.delivered_at = now_naive

                rec = DeliveryRecord.create(
                    package_id=package.package_id,
                    target=intent.target,
                    variant=intent.variant,
                    attempt_id=attempt_id,
                    approval_state=PackageStatus.APPROVED,
                    status=DeliveryOutcome.SUCCEEDED,
                    external_id=recon.provider_post_id,
                    finished_at=now_naive,
                    publication_key=intent.publication_key,
                    payload_hash=intent.payload_hash,
                    provider_post_id=recon.provider_post_id,
                    provider_url=recon.provider_url,
                    retry_count=intent.attempt_count,
                )
                package.delivery_records.append(rec)

                # Post-publish audit target registration
                if session is not None:
                    try:
                        from app.worker.audit_scheduler import register_audit_target

                        await register_audit_target(
                            session=session,
                            delivery_record=rec,
                            approved_text=current_text,
                            package_id=package.package_id,
                            now=now_naive,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to register audit target for recovered intent %s: %s",
                            intent.publication_key,
                            e,
                        )

                return PublicationResult(
                    success=True,
                    provider_post_id=recon.provider_post_id,
                    provider_url=recon.provider_url,
                )
            elif recon.confidence == ReconciliationConfidence.CONFIRMED_ABSENT:
                logger.info(
                    "Reconciliation confirmed absence of post for intent %s; proceeding.",
                    intent.publication_key,
                )
            elif intent.status in (
                PublicationIntentStatus.IN_FLIGHT,
                PublicationIntentStatus.DELIVERY_UNKNOWN,
            ):
                logger.warning(
                    "Reconciliation for intent %s in %s is INCONCLUSIVE. Halting to prevent blind repost.",
                    intent.publication_key,
                    intent.status.value,
                )
                intent.status = PublicationIntentStatus.DELIVERY_UNKNOWN
                target_obj.status = TargetDeliveryStatus.DELIVERY_UNKNOWN
                intent.last_error_code = "RECONCILIATION_INCONCLUSIVE"
                intent.last_error_message = (
                    recon.reason
                    or "Delivery outcome remains ambiguous; manual reconciliation required"
                )
                return PublicationResult(
                    success=False,
                    is_ambiguous=True,
                    is_retryable=False,
                    error_code="RECONCILIATION_INCONCLUSIVE",
                    error_message=intent.last_error_message,
                )

        # Boundary A: Mark IN_FLIGHT before network dispatch
        intent.attempt_count += 1
        intent.status = PublicationIntentStatus.IN_FLIGHT
        intent.attempt_started_at = now_naive

        # Execute publish via connector
        pub_result = await connector.publish(current_text, intent)

        # Crash Boundary C simulation hook for testing
        if simulate_crash_after_publish and pub_result.success:
            logger.warning(
                "SIMULATED CRASH after external post creation (post_id=%s) before local persistence!",
                pub_result.provider_post_id,
            )
            raise RuntimeError("SIMULATED_LOCAL_CRASH_AFTER_EXTERNAL_SUCCESS")

        if pub_result.success:
            intent.status = PublicationIntentStatus.SUCCEEDED
            intent.provider_post_id = pub_result.provider_post_id
            intent.provider_url = pub_result.provider_url
            intent.last_error_code = None
            intent.last_error_message = None

            target_obj.status = TargetDeliveryStatus.DELIVERED
            target_obj.external_id = pub_result.provider_post_id
            target_obj.delivered_at = now_naive

            rec = DeliveryRecord.create(
                package_id=package.package_id,
                target=intent.target,
                variant=intent.variant,
                attempt_id=attempt_id,
                approval_state=PackageStatus.APPROVED,
                status=DeliveryOutcome.SUCCEEDED,
                external_id=pub_result.provider_post_id,
                finished_at=now_naive,
                publication_key=intent.publication_key,
                payload_hash=intent.payload_hash,
                provider_post_id=pub_result.provider_post_id,
                provider_url=pub_result.provider_url,
                retry_count=intent.attempt_count - 1,
            )
            package.delivery_records.append(rec)

            # Schedule Post-Publish Audit & Telemetry v2 immediately
            if session is not None:
                try:
                    from app.worker.audit_scheduler import register_audit_target

                    await register_audit_target(
                        session=session,
                        delivery_record=rec,
                        approved_text=current_text,
                        package_id=package.package_id,
                        now=now_naive,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to register audit target for intent %s: %s",
                        intent.publication_key,
                        e,
                    )

        elif pub_result.is_ambiguous:
            # Boundary B: Network timeout or ambiguous outcome
            intent.status = PublicationIntentStatus.DELIVERY_UNKNOWN
            intent.last_error_code = pub_result.error_code or "TIMEOUT"
            intent.last_error_message = pub_result.error_message
            target_obj.status = TargetDeliveryStatus.DELIVERY_UNKNOWN

            # Immediate ambiguous reconciliation
            recon = await connector.reconcile_ambiguous_delivery(intent, current_text)
            if recon.confidence == ReconciliationConfidence.CONFIRMED_PRESENT or (
                recon.resolved and recon.published
            ):
                intent.status = PublicationIntentStatus.SUCCEEDED
                intent.provider_post_id = recon.provider_post_id
                intent.provider_url = recon.provider_url
                target_obj.status = TargetDeliveryStatus.DELIVERED
                target_obj.external_id = recon.provider_post_id
                target_obj.delivered_at = now_naive

                pub_result = PublicationResult(
                    success=True,
                    provider_post_id=recon.provider_post_id,
                    provider_url=recon.provider_url,
                    http_status=200,
                )

                rec = DeliveryRecord.create(
                    package_id=package.package_id,
                    target=intent.target,
                    variant=intent.variant,
                    attempt_id=attempt_id,
                    approval_state=PackageStatus.APPROVED,
                    status=DeliveryOutcome.SUCCEEDED,
                    external_id=recon.provider_post_id,
                    finished_at=now_naive,
                    publication_key=intent.publication_key,
                    payload_hash=intent.payload_hash,
                    provider_post_id=recon.provider_post_id,
                    provider_url=recon.provider_url,
                    retry_count=intent.attempt_count - 1,
                )
                package.delivery_records.append(rec)
            elif recon.confidence == ReconciliationConfidence.CONFIRMED_ABSENT:
                # Confirmed absent -> retry permitted if retryable and attempts remain
                if pub_result.is_retryable and intent.attempt_count < settings.publish_max_retries:
                    intent.status = PublicationIntentStatus.PENDING
                    if pub_result.retry_after_seconds and pub_result.retry_after_seconds > 0:
                        delay_sec = pub_result.retry_after_seconds
                    else:
                        backoff_idx = min(
                            intent.attempt_count - 1,
                            len(settings.publish_retry_backoff_seconds) - 1,
                        )
                        delay_sec = settings.publish_retry_backoff_seconds[backoff_idx]
                    intent.next_retry_at = now_naive + datetime.timedelta(seconds=delay_sec)
                else:
                    intent.status = PublicationIntentStatus.FAILED
                    target_obj.status = TargetDeliveryStatus.FAILED

                rec = DeliveryRecord.create(
                    package_id=package.package_id,
                    target=intent.target,
                    variant=intent.variant,
                    attempt_id=attempt_id,
                    approval_state=PackageStatus.APPROVED,
                    status=(
                        DeliveryOutcome.RETRYABLE_ERROR
                        if intent.status == PublicationIntentStatus.PENDING
                        else DeliveryOutcome.FAILED
                    ),
                    error_code=pub_result.error_code,
                    error_message=sanitize_sensitive_text(pub_result.error_message or ""),
                    finished_at=now_naive,
                    publication_key=intent.publication_key,
                    payload_hash=intent.payload_hash,
                    retry_count=intent.attempt_count - 1,
                )
                package.delivery_records.append(rec)
            else:
                # INCONCLUSIVE:
                # Ambiguous outcome inconclusive -> never blind retry, remain DELIVERY_UNKNOWN
                intent.status = PublicationIntentStatus.DELIVERY_UNKNOWN
                target_obj.status = TargetDeliveryStatus.DELIVERY_UNKNOWN
                pub_result.is_retryable = False
                intent.last_error_code = "RECONCILIATION_INCONCLUSIVE"
                intent.last_error_message = (
                    recon.reason
                    or "Delivery outcome remains ambiguous; manual reconciliation required"
                )
                rec = DeliveryRecord.create(
                    package_id=package.package_id,
                    target=intent.target,
                    variant=intent.variant,
                    attempt_id=attempt_id,
                    approval_state=PackageStatus.APPROVED,
                    status=DeliveryOutcome.FAILED,
                    error_code="RECONCILIATION_INCONCLUSIVE",
                    error_message=sanitize_sensitive_text(intent.last_error_message),
                    finished_at=now_naive,
                    publication_key=intent.publication_key,
                    payload_hash=intent.payload_hash,
                    retry_count=intent.attempt_count - 1,
                )
                package.delivery_records.append(rec)

        else:
            # Failure case (retryable vs permanent)
            intent.last_error_code = pub_result.error_code
            intent.last_error_message = sanitize_sensitive_text(
                pub_result.error_message or ""
            )

            if (
                pub_result.is_retryable
                and intent.attempt_count < settings.publish_max_retries
            ):
                intent.status = PublicationIntentStatus.PENDING
                if pub_result.retry_after_seconds and pub_result.retry_after_seconds > 0:
                    delay_sec = pub_result.retry_after_seconds
                else:
                    backoff_idx = min(
                        intent.attempt_count - 1,
                        len(settings.publish_retry_backoff_seconds) - 1,
                    )
                    delay_sec = settings.publish_retry_backoff_seconds[backoff_idx]
                intent.next_retry_at = now_naive + datetime.timedelta(seconds=delay_sec)
                outcome = DeliveryOutcome.RETRYABLE_ERROR
            else:
                intent.status = PublicationIntentStatus.FAILED
                target_obj.status = TargetDeliveryStatus.FAILED
                target_obj.error_code = pub_result.error_code
                target_obj.error_message = intent.last_error_message
                outcome = DeliveryOutcome.FAILED

            rec = DeliveryRecord.create(
                package_id=package.package_id,
                target=intent.target,
                variant=intent.variant,
                attempt_id=attempt_id,
                approval_state=PackageStatus.APPROVED,
                status=outcome,
                error_code=pub_result.error_code,
                error_message=intent.last_error_message,
                finished_at=now_naive,
                publication_key=intent.publication_key,
                payload_hash=intent.payload_hash,
                retry_count=intent.attempt_count - 1,
                next_retry_at=intent.next_retry_at,
            )
            package.delivery_records.append(rec)

        # Reconcile package lifecycle state
        new_state = reconcile_package_status(package)
        if (
            new_state != package.approval_state
            and new_state in {PackageStatus.DELIVERED, PackageStatus.PARTIALLY_DELIVERED, PackageStatus.FAILED}
        ):
            package.approval_state = new_state

        return pub_result

    @classmethod
    async def execute_plan(
        cls,
        plan: PublicationPlan,
        package: ContentPackage,
        session: Any | None = None,
        connector_registry: Any | None = None,
        target_filter: TargetPlatform | None = None,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """Execute all intents in a PublicationPlan with strict per-platform isolation.

        Failure on Platform A does not invalidate or re-attempt Platform B.
        """
        if target_filter and target_filter not in plan.approval.target_platforms:
            raise UnauthorizedApprovalError(
                f"Target platform {target_filter.value} was not authorized in owner approval target set {plan.approval.target_platforms}"
            )

        reg = connector_registry or ConnectorRegistry
        results: dict[str, Any] = {}

        for intent in plan.intents:
            if target_filter and intent.target != target_filter:
                continue

            # Only execute intents that are pending or need ambiguous reconciliation
            if intent.status not in (
                PublicationIntentStatus.PENDING,
                PublicationIntentStatus.DELIVERY_UNKNOWN,
                PublicationIntentStatus.IN_FLIGHT,
            ):
                results[intent.target.value] = {
                    "status": intent.status.value,
                    "action": "SKIPPED",
                }
                continue

            try:
                connector = reg.get_connector(intent.target)
            except Exception as e:
                logger.error("No connector for target %s: %s", intent.target, e)
                results[intent.target.value] = {"status": "NO_CONNECTOR", "error": str(e)}
                continue

            try:
                pub_result = await cls.execute_intent(
                    intent=intent,
                    package=package,
                    connector=connector,
                    session=session,
                    now=now,
                    approval=plan.approval,
                )
                results[intent.target.value] = {
                    "status": intent.status.value,
                    "success": pub_result.success,
                    "post_id": pub_result.provider_post_id,
                    "error_code": pub_result.error_code,
                }
            except Exception as e:
                logger.error("Unexpected error executing target %s: %s", intent.target, e)
                results[intent.target.value] = {
                    "status": "UNEXPECTED_ERROR",
                    "error": sanitize_sensitive_text(str(e)),
                }

        # Update plan status deterministically using aggregate semantics
        plan.status = calculate_aggregate_plan_status(plan)

        return results
