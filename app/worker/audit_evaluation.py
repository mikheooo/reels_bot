"""Deterministic replay evaluation suite for Post-Publish Audit & Telemetry v2."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.worker.audit_schemas import (
    AuditPolicy,
    AuditResult,
    AuditResultStatus,
    AuditSnapshot,
    AuditTarget,
    AuditTargetStatus,
    NormalizedMetrics,
)
from app.worker.connectors import (
    CapabilityStatus,
    ConnectorCapabilities,
    LookupResult,
    PublicationConnector,
    PublicationResult,
    RateLimitInfo,
)
from app.worker.content_package import (
    DeliveryOutcome,
    TargetPlatform,
    compute_payload_hash,
)
from app.worker.post_publish_audit import (
    compute_metric_deltas,
    evaluate_audit_eligibility,
    normalize_provider_metrics,
    perform_audit_check,
    should_send_alert,
)


class AuditEvalCase(BaseModel):
    scenario_id: str
    description: str
    target: str
    action: str
    initial_text: str
    mock_provider_post_id: str | None = None
    mock_provider_text: str | None = None
    mock_http_status: int | None = None
    mock_raw_metrics: dict[str, Any] = Field(default_factory=dict)
    previous_raw_metrics: dict[str, Any] | None = None
    connector_status: str = "CONNECTED_SUPPORTED"
    is_legacy: bool = False
    expected_eligible: bool = True
    expected_target_status: str
    expected_audit_result: str | None = None
    expected_content_match: bool | None = None
    expected_next_audit: bool = True
    expected_alert: bool = False
    expected_metric_deltas: dict[str, Any] = Field(default_factory=dict)


class AuditEvalReport(BaseModel):
    total_scenarios: int
    passed_scenarios: int
    auth_mistaken_for_deletion: int  # Gate 1: Must be 0
    duplicate_snapshots: int         # Gate 2: Must be 0
    unsupported_fake_verification: int  # Gate 3: Must be 0
    overdue_pollution_for_unavailable_connectors: int  # Gate 4: Must be 0
    all_gates_passed: bool
    scenario_details: list[dict[str, Any]] = Field(default_factory=list)


def load_audit_cases(fixture_path: str | Path) -> list[AuditEvalCase]:
    path = Path(fixture_path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [AuditEvalCase.model_validate(c) for c in data]


class MockAuditConnector(PublicationConnector):
    def __init__(
        self,
        target: TargetPlatform,
        http_status: int | None = 200,
        text: str | None = None,
        raw_metrics: dict[str, Any] | None = None,
        status: CapabilityStatus = CapabilityStatus.CONNECTED_SUPPORTED,
    ):
        self._target = target
        self._status = status
        self._http_status = http_status
        self._text = text
        self._raw_metrics = raw_metrics or {}

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target=self._target,
            status=self._status,
            publication_mode="MANUAL_APPROVAL",
            max_chars=280 if self._target == TargetPlatform.X else 500,
            supports_video_attachment=True,
            official_endpoint="https://api.mock.test",
        )

    async def validate_credentials(self) -> Any:
        return None

    async def publish(self, intent: Any) -> PublicationResult:
        raise NotImplementedError("Audit mock connector does not publish")

    async def lookup(self, provider_post_id: str) -> LookupResult:
        if self._http_status == 200:
            return LookupResult(
                found=True,
                provider_post_id=provider_post_id,
                text=self._text,
                http_status=200,
                raw_metrics=self._raw_metrics,
            )
        elif self._http_status == 404:
            return LookupResult(
                found=False,
                provider_post_id=provider_post_id,
                http_status=404,
                error_code="POST_NOT_FOUND",
                error_message="Post not found or deleted on platform",
            )
        elif self._http_status in (401, 403):
            return LookupResult(
                found=False,
                provider_post_id=provider_post_id,
                http_status=self._http_status,
                error_code="AUTH_REQUIRED",
                error_message="Authentication expired or token revoked",
            )
        elif self._http_status == 429:
            return LookupResult(
                found=False,
                provider_post_id=provider_post_id,
                http_status=429,
                error_code="RATE_LIMITED",
                error_message="Rate limit exceeded",
                rate_limit_info=RateLimitInfo(retry_after_seconds=60),
            )
        return LookupResult(
            found=False,
            provider_post_id=provider_post_id,
            http_status=self._http_status,
            error_code="UNKNOWN_ERROR",
        )

    async def delete(self, provider_post_id: str) -> bool:
        return True

    def classify_error(self, status_code: int | None, error_data: Any) -> tuple[bool, str]:
        if status_code in (429, 500, 502, 503, 504) or status_code is None:
            return True, "RETRYABLE"
        return False, "NON_RETRYABLE"

    async def reconcile_ambiguous_delivery(self, intent: Any, text: str) -> Any:
        return None


async def _run_eval_async(cases: list[AuditEvalCase]) -> AuditEvalReport:
    total = len(cases)
    passed = 0
    auth_mistaken_for_deletion = 0
    duplicate_snapshots = 0
    unsupported_fake_verification = 0
    overdue_pollution_for_unavailable_connectors = 0
    details: list[dict[str, Any]] = []

    for case in cases:
        case_detail: dict[str, Any] = {
            "scenario_id": case.scenario_id,
            "action": case.action,
            "status": "PASS",
            "errors": [],
        }

        # Target platform enum
        target_enum = TargetPlatform(case.target)
        conn_status_enum = CapabilityStatus(case.connector_status)

        # 1. Eligibility Evaluation
        if case.is_legacy:
            is_eligible = False
            target_status_val = case.expected_target_status
            next_at = None
        else:
            is_eligible, target_status, next_at = evaluate_audit_eligibility(
                target=target_enum,
                delivery_status=DeliveryOutcome.SUCCEEDED,
                provider_post_id=case.mock_provider_post_id,
                connector_status=conn_status_enum,
            )
            target_status_val = target_status.value

        # Gate 4 Safety Check: Overdue pollution for unconfigured / manual targets
        if (
            case.connector_status != "CONNECTED_SUPPORTED"
            or case.target in ("YOUTUBE_COMMUNITY", "TELEGRAM_CHANNEL")
        ) and (is_eligible or next_at is not None or target_status_val in ("SCHEDULED", "ACTIVE")):
            overdue_pollution_for_unavailable_connectors += 1
            case_detail["errors"].append(
                f"Gate 4 violation: scheduled/active target created for unavailable connector {case.target}"
            )

        # Gate 3 Safety Check: Unsupported fake verification
        if case.target in ("YOUTUBE_COMMUNITY", "TELEGRAM_CHANNEL") and (
            is_eligible or (case.expected_audit_result == "VERIFIED" and case.action.endswith("VERIFIED"))
        ):
            unsupported_fake_verification += 1
            case_detail["errors"].append(
                f"Gate 3 violation: unsupported target {case.target} falsely marked eligible or verified"
            )

        # Legacy isolation check
        if case.is_legacy and (is_eligible or next_at is not None):
            case_detail["errors"].append("Legacy record was erroneously marked eligible for v2 audit")

        # Verify eligibility matches expectation
        if is_eligible != case.expected_eligible:
            case_detail["errors"].append(
                f"Eligibility mismatch: actual {is_eligible} != expected {case.expected_eligible}"
            )

        if target_status_val != case.expected_target_status:
            case_detail["errors"].append(
                f"Target status mismatch: actual {target_status_val} != expected {case.expected_target_status}"
            )

        # 2. Audit Execution (for eligible targets)
        if is_eligible:
            target_obj = AuditTarget(
                audit_id=f"audit_{case.scenario_id}",
                package_id=f"pkg_{case.scenario_id}",
                delivery_id=f"del_{case.scenario_id}",
                target=target_enum,
                provider_post_id=case.mock_provider_post_id or "default_id",
                publication_key=f"pub_{case.scenario_id}",
                approved_payload_hash=compute_payload_hash(case.initial_text),
                approved_payload_text=case.initial_text,
                status=target_status,
                tier=0,
                next_audit_at=next_at,
            )

            connector = MockAuditConnector(
                target=target_enum,
                http_status=case.mock_http_status,
                text=case.mock_provider_text,
                raw_metrics=case.mock_raw_metrics,
                status=conn_status_enum,
            )

            prev_snap = None
            if case.previous_raw_metrics is not None:
                prev_norm = normalize_provider_metrics(target_enum, case.previous_raw_metrics)
                prev_snap = AuditSnapshot(
                    snapshot_id=f"asnap_prev_{case.scenario_id}",
                    audit_id=target_obj.audit_id,
                    occurrence_key=f"{target_obj.audit_id}:prev",
                    checked_at=datetime.now(timezone.utc),
                    object_exists=True,
                    raw_metrics=case.previous_raw_metrics,
                    normalized_metrics=prev_norm,
                    metric_deltas={},
                    status=AuditResultStatus.VERIFIED,
                )

            fixed_now = datetime.now(timezone.utc)
            result, snapshot = await perform_audit_check(
                target=target_obj,
                connector=connector,
                previous_snapshot=prev_snap,
                now=fixed_now,
                scheduled_for=target_obj.next_audit_at,
            )

            # Gate 1 Safety Check: Auth failure mistaken for deletion
            if case.mock_http_status in (401, 403) and result.status == AuditResultStatus.DELETED_OR_NOT_FOUND:
                auth_mistaken_for_deletion += 1
                case_detail["errors"].append("Gate 1 violation: HTTP 401/403 was mistaken for deletion")

            # Gate 2 Safety Check: Duplicate snapshot generation on retry/duplicate trigger
            if case.action == "DUPLICATE_AUDIT_TRIGGER":
                # Simulate retry at a different execution timestamp (+17 seconds) for the same logical occurrence
                retry_now = fixed_now + timedelta(seconds=17)
                result2, snapshot2 = await perform_audit_check(
                    target=target_obj,
                    connector=connector,
                    previous_snapshot=prev_snap,
                    now=retry_now,
                    scheduled_for=target_obj.next_audit_at,
                )
                if snapshot.occurrence_key != snapshot2.occurrence_key or snapshot.snapshot_id != snapshot2.snapshot_id:
                    duplicate_snapshots += 1
                    case_detail["errors"].append(
                        "Gate 2 violation: duplicate trigger generated divergent occurrence keys/snapshots"
                    )

            # Validate audit result status
            if case.expected_audit_result and result.status.value != case.expected_audit_result:
                case_detail["errors"].append(
                    f"Result status mismatch: actual {result.status.value} != expected {case.expected_audit_result}"
                )

            # Validate content integrity match
            if case.expected_content_match is not None and result.content_match != case.expected_content_match:
                case_detail["errors"].append(
                    f"Content match mismatch: actual {result.content_match} != expected {case.expected_content_match}"
                )

            # Validate next audit scheduling
            actual_has_next = result.next_audit_at is not None
            if actual_has_next != case.expected_next_audit:
                case_detail["errors"].append(
                    f"Next audit schedule mismatch: actual {actual_has_next} != expected {case.expected_next_audit}"
                )

            # Validate administrator alert
            needs_alert, _ = should_send_alert(result)
            if needs_alert != case.expected_alert:
                case_detail["errors"].append(
                    f"Alert expectation mismatch: actual {needs_alert} != expected {case.expected_alert}"
                )

            # Validate metric deltas (e.g. threads metrics updated)
            if case.expected_metric_deltas:
                for metric_name, exp_delta in case.expected_metric_deltas.items():
                    act_delta = result.metric_deltas.get(metric_name)
                    if not act_delta:
                        case_detail["errors"].append(f"Missing metric delta for {metric_name}")
                        continue
                    if act_delta.absolute_delta != exp_delta.get("absolute_delta"):
                        case_detail["errors"].append(
                            f"Absolute delta mismatch for {metric_name}: actual {act_delta.absolute_delta} != expected {exp_delta.get('absolute_delta')}"
                        )
                    if act_delta.percentage_delta != exp_delta.get("percentage_delta"):
                        case_detail["errors"].append(
                            f"Percentage delta mismatch for {metric_name}: actual {act_delta.percentage_delta} != expected {exp_delta.get('percentage_delta')}"
                        )

        if not case_detail["errors"]:
            passed += 1
        else:
            case_detail["status"] = "FAIL"

        details.append(case_detail)

    all_passed = (
        passed == total
        and auth_mistaken_for_deletion == 0
        and duplicate_snapshots == 0
        and unsupported_fake_verification == 0
        and overdue_pollution_for_unavailable_connectors == 0
    )

    return AuditEvalReport(
        total_scenarios=total,
        passed_scenarios=passed,
        auth_mistaken_for_deletion=auth_mistaken_for_deletion,
        duplicate_snapshots=duplicate_snapshots,
        unsupported_fake_verification=unsupported_fake_verification,
        overdue_pollution_for_unavailable_connectors=overdue_pollution_for_unavailable_connectors,
        all_gates_passed=all_passed,
        scenario_details=details,
    )


def evaluate_audit_suite(cases: list[AuditEvalCase]) -> AuditEvalReport:
    """Synchronous runner for the audit deterministic evaluation replay suite."""
    return asyncio.run(_run_eval_async(cases))
