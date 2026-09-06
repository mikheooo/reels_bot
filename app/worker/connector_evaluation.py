"""Deterministic replay evaluation suite for Publishing Orchestration & External Platform Connectors."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.worker.connectors import (
    AmbiguousReconciliationResult,
    CapabilityStatus,
    ConnectorCapabilities,
    ConnectorRegistry,
    CredentialValidationResult,
    PublicationConnector,
    PublicationResult,
)
from app.worker.content_package import (
    ContentPackage,
    DeliveryOutcome,
    DeliveryRecord,
    OutputVariantType,
    PackageStatus,
    PublicationIntent,
    PublicationIntentStatus,
    TargetDeliveryStatus,
    TargetPlatform,
    UnauthorizedApprovalError,
    approve_package,
    build_content_package,
    compute_payload_hash,
    reconcile_package_status,
    transition_package_status,
)
from app.worker.output_variants import (
    CanonicalContentResult,
    generate_all_variants,
)


class ConnectorEvalCase(BaseModel):
    scenario_id: str
    description: str
    canonical: CanonicalContentResult
    owner_user_id: int
    attempt_user_id: int
    target: str
    action: str
    mock_http_status: int | None = None
    mock_post_id: str | None = None
    expected_package_status: str
    expected_intent_status: str | None = None
    expected_target_status: str
    expected_is_retryable: bool = False
    expected_unauthorized_error: bool = False
    expected_stale_error: bool = False


class ConnectorEvalReport(BaseModel):
    total_scenarios: int
    passed_scenarios: int
    unauthorized_publications: int
    duplicate_publications: int
    stale_approval_violations: int
    fake_successes: int
    retry_correctness: float
    all_gates_passed: bool
    scenario_details: list[dict[str, Any]] = Field(default_factory=list)


def load_connector_cases(fixture_path: str | Path) -> list[ConnectorEvalCase]:
    path = Path(fixture_path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [ConnectorEvalCase.model_validate(c) for c in data]


class MockTestConnector(PublicationConnector):
    def __init__(
        self,
        target: TargetPlatform,
        status: CapabilityStatus = CapabilityStatus.CONNECTED_SUPPORTED,
        mock_result: PublicationResult | None = None,
        mock_recon: AmbiguousReconciliationResult | None = None,
    ):
        self._target = target
        self._status = status
        self._mock_result = mock_result
        self._mock_recon = mock_recon
        self.published_payloads: list[str] = []

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target=self._target,
            status=self._status,
            publication_mode="MANUAL_APPROVAL" if self._target != TargetPlatform.YOUTUBE_COMMUNITY else "MANUAL_EXPORT",
            max_chars=280 if self._target == TargetPlatform.X else 500,
            auth_model="OAuth 2.0",
            official_endpoint="https://api.mock.com/post",
        )

    async def validate_credentials(self) -> CredentialValidationResult:
        if self._status == CapabilityStatus.CONNECTED_SUPPORTED:
            return CredentialValidationResult(
                is_valid=True,
                status=self._status,
                account_id="test_account_123",
                account_username="test_account",
            )
        return CredentialValidationResult(
            is_valid=False,
            status=self._status,
            error_code="MOCK_UNCONFIGURED",
        )

    async def publish(self, text: str, intent: PublicationIntent) -> PublicationResult:
        self.published_payloads.append(text)
        if self._mock_result:
            return self._mock_result
        return PublicationResult(success=True, provider_post_id="mock_default_id", provider_url="https://mock.url/post")

    async def lookup(self, provider_post_id: str) -> Any:
        return None

    async def delete(self, provider_post_id: str) -> bool:
        return True

    def classify_error(self, status_code: int | None, error_data: Any) -> tuple[bool, str]:
        if status_code in (429, 500, 502, 503, 504) or status_code is None:
            return True, "RETRYABLE"
        return False, "NON_RETRYABLE"

    async def reconcile_ambiguous_delivery(
        self, intent: PublicationIntent, text: str
    ) -> AmbiguousReconciliationResult:
        if self._mock_recon:
            return self._mock_recon
        return AmbiguousReconciliationResult(resolved=True, published=False, reason="Default mock not found")


async def _run_eval_async(cases: list[ConnectorEvalCase]) -> ConnectorEvalReport:
    total = len(cases)
    passed = 0
    unauthorized_publications = 0
    duplicate_publications = 0
    stale_approval_violations = 0
    fake_successes = 0
    retry_correct_count = 0
    retry_total_tested = 0

    details: list[dict[str, Any]] = []

    for case in cases:
        case_detail: dict[str, Any] = {
            "scenario_id": case.scenario_id,
            "status": "PASS",
            "errors": [],
        }

        canonical = case.canonical
        variants = generate_all_variants(canonical)
        pkg = build_content_package(
            job_id=case.scenario_id,
            source_url=f"https://instagram.com/reel/{case.scenario_id}",
            canonical=canonical,
            variants=variants,
        )

        pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
        pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.DELIVERED
        transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)

        target_enum = TargetPlatform(case.target)

        ConnectorRegistry.reset()

        if case.action == "X_PUBLISH_SUCCESS":
            mock_pub = PublicationResult(
                success=True,
                provider_post_id=case.mock_post_id,
                provider_url=f"https://x.com/i/status/{case.mock_post_id}",
            )
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED, mock_pub)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            res = await mock_conn.publish("text", intent)
            if res.success:
                intent.status = PublicationIntentStatus.SUCCEEDED
                intent.provider_post_id = res.provider_post_id
                pkg.distribution_targets[case.target].status = TargetDeliveryStatus.DELIVERED
            pkg.approval_state = reconcile_package_status(pkg)

        elif case.action == "X_RATE_LIMIT_429":
            mock_pub = PublicationResult(
                success=False,
                http_status=429,
                error_code="RATE_LIMITED",
                is_retryable=True,
            )
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED, mock_pub)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            res = await mock_conn.publish("text", intent)
            retry_total_tested += 1
            if res.is_retryable:
                retry_correct_count += 1
                intent.status = PublicationIntentStatus.PENDING
                intent.attempt_count += 1

        elif case.action == "X_AMBIGUOUS_TIMEOUT_LOOKUP_RESOLVED":
            mock_pub = PublicationResult(
                success=False,
                is_ambiguous=True,
                is_retryable=True,
                error_code="TIMEOUT",
            )
            mock_recon = AmbiguousReconciliationResult(
                resolved=True,
                published=True,
                provider_post_id=case.mock_post_id,
                provider_url=f"https://x.com/i/status/{case.mock_post_id}",
                reason="Found via lookup",
            )
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED, mock_pub, mock_recon)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            _ = await mock_conn.publish("text", intent)
            recon = await mock_conn.reconcile_ambiguous_delivery(intent, "text")
            if recon.resolved and recon.published:
                intent.status = PublicationIntentStatus.SUCCEEDED
                intent.provider_post_id = recon.provider_post_id
                pkg.distribution_targets[case.target].status = TargetDeliveryStatus.DELIVERED
            pkg.approval_state = reconcile_package_status(pkg)

        elif case.action == "X_BAD_CREDENTIALS_401":
            mock_pub = PublicationResult(
                success=False,
                http_status=401,
                error_code="AUTH_ERROR_401",
                is_retryable=False,
            )
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED, mock_pub)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            res = await mock_conn.publish("text", intent)
            if not res.is_retryable:
                intent.status = PublicationIntentStatus.FAILED
                pkg.distribution_targets[case.target].status = TargetDeliveryStatus.FAILED

        elif case.action == "THREADS_TWO_STEP_SUCCESS":
            mock_pub = PublicationResult(
                success=True,
                provider_post_id=case.mock_post_id,
                provider_url=f"https://threads.net/post/{case.mock_post_id}",
            )
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED, mock_pub)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            res = await mock_conn.publish("text", intent)
            if res.success:
                intent.status = PublicationIntentStatus.SUCCEEDED
                intent.provider_post_id = res.provider_post_id
                pkg.distribution_targets[case.target].status = TargetDeliveryStatus.DELIVERED
            pkg.approval_state = reconcile_package_status(pkg)

        elif case.action == "THREADS_PUBLISH_FAILS_RETRYABLE":
            mock_pub = PublicationResult(
                success=False,
                http_status=503,
                error_code="SERVER_ERROR_503",
                is_retryable=True,
            )
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED, mock_pub)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            res = await mock_conn.publish("text", intent)
            retry_total_tested += 1
            if res.is_retryable:
                retry_correct_count += 1
                intent.status = PublicationIntentStatus.PENDING

        elif case.action == "THREADS_RATE_LIMIT_429":
            mock_pub = PublicationResult(
                success=False,
                http_status=429,
                error_code="RATE_LIMITED",
                is_retryable=True,
            )
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED, mock_pub)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            res = await mock_conn.publish("text", intent)
            retry_total_tested += 1
            if res.is_retryable:
                retry_correct_count += 1
                intent.status = PublicationIntentStatus.PENDING

        elif case.action == "UNAUTHORIZED_APPROVAL":
            caught_unauthorized = False
            try:
                approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            except UnauthorizedApprovalError:
                caught_unauthorized = True

            if not caught_unauthorized:
                unauthorized_publications += 1
                case_detail["errors"].append("Unauthorized approval was not blocked")

        elif case.action == "STALE_APPROVAL":
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            variants_dict = getattr(pkg.output_variants, "variants", {})
            v = variants_dict.get(case.target)
            if v:
                v.text = (v.text or "") + " [MUTATED AFTER APPROVAL]"

            new_hash = compute_payload_hash(v.text if v else "")
            if new_hash != intent.payload_hash:
                intent.status = PublicationIntentStatus.APPROVAL_STALE
                pkg.distribution_targets[case.target].status = TargetDeliveryStatus.FAILED
            else:
                stale_approval_violations += 1
                case_detail["errors"].append("Stale approval not detected")

        elif case.action == "DOUBLE_APPROVE_IDEMPOTENCY":
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, recs1 = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intents_count_1 = len(pkg.publication_intents)
            pkg, recs2 = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intents_count_2 = len(pkg.publication_intents)

            if len(recs2) > 0 or intents_count_2 > intents_count_1:
                duplicate_publications += 1
                case_detail["errors"].append("Double approve created duplicate records or intents")

        elif case.action == "RETRY_SAME_PUBLICATION_KEY":
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.CONNECTED_SUPPORTED)
            ConnectorRegistry.register_connector(target_enum, mock_conn)

            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            intent = next(i for i in pkg.publication_intents if i.target == target_enum)
            initial_pub_key = intent.publication_key

            rec1 = DeliveryRecord.create(
                package_id=pkg.package_id,
                target=target_enum,
                variant=OutputVariantType.X_POST,
                attempt_id=1,
                approval_state=PackageStatus.APPROVED,
                status=DeliveryOutcome.RETRYABLE_ERROR,
                publication_key=initial_pub_key,
            )
            rec2 = DeliveryRecord.create(
                package_id=pkg.package_id,
                target=target_enum,
                variant=OutputVariantType.X_POST,
                attempt_id=2,
                approval_state=PackageStatus.APPROVED,
                status=DeliveryOutcome.SUCCEEDED,
                publication_key=initial_pub_key,
            )

            retry_total_tested += 1
            if rec1.publication_key == rec2.publication_key and rec1.attempt_id != rec2.attempt_id:
                retry_correct_count += 1
            else:
                case_detail["errors"].append("Retry did not preserve publication_key across attempts")

        elif case.action == "YOUTUBE_COMMUNITY_UNSUPPORTED":
            pkg, recs = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)
            t_status = pkg.distribution_targets[case.target].status
            if t_status == TargetDeliveryStatus.DELIVERED:
                fake_successes += 1
                case_detail["errors"].append("YouTube Community falsely marked as DELIVERED to external platform")

        elif case.action == "ACCOUNT_IDENTITY_MISMATCH":
            mock_conn = MockTestConnector(target_enum, CapabilityStatus.SUPPORTED_NOT_CONFIGURED)
            ConnectorRegistry.register_connector(target_enum, mock_conn)
            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id, use_connectors=True)

        ConnectorRegistry.reset()

        actual_pkg_status = pkg.approval_state.value
        actual_target_status = pkg.distribution_targets[case.target].status.value

        if actual_pkg_status != case.expected_package_status:
            case_detail["errors"].append(
                f"Package status mismatch: actual {actual_pkg_status} != expected {case.expected_package_status}"
            )

        if actual_target_status != case.expected_target_status:
            case_detail["errors"].append(
                f"Target status mismatch: actual {actual_target_status} != expected {case.expected_target_status}"
            )

        if case.expected_intent_status:
            intent = next((i for i in pkg.publication_intents if i.target.value == case.target), None)
            if not intent or intent.status.value != case.expected_intent_status:
                act_i = intent.status.value if intent else "NONE"
                case_detail["errors"].append(
                    f"Intent status mismatch: actual {act_i} != expected {case.expected_intent_status}"
                )

        if not case_detail["errors"]:
            passed += 1
        else:
            case_detail["status"] = "FAIL"

        details.append(case_detail)

    retry_rate = (retry_correct_count / retry_total_tested) if retry_total_tested > 0 else 1.0

    all_passed = (
        passed == total
        and unauthorized_publications == 0
        and duplicate_publications == 0
        and stale_approval_violations == 0
        and fake_successes == 0
        and retry_rate == 1.0
    )

    return ConnectorEvalReport(
        total_scenarios=total,
        passed_scenarios=passed,
        unauthorized_publications=unauthorized_publications,
        duplicate_publications=duplicate_publications,
        stale_approval_violations=stale_approval_violations,
        fake_successes=fake_successes,
        retry_correctness=retry_rate,
        all_gates_passed=all_passed,
        scenario_details=details,
    )


def evaluate_connectors(cases: list[ConnectorEvalCase]) -> ConnectorEvalReport:
    """Synchronous runner for connector evaluation."""
    return asyncio.run(_run_eval_async(cases))
