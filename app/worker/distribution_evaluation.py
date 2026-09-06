"""Offline replay suite and metric evaluation for Content Factory Delivery & Distribution MVP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.worker.content_package import (
    ContentPackage,
    DeliveryOutcome,
    DeliveryRecord,
    OutputVariantType,
    PackageStatus,
    TargetDeliveryStatus,
    TargetPlatform,
    UnauthorizedApprovalError,
    approve_package,
    build_content_package,
    classify_delivery_error,
    regenerate_variant_from_canonical,
    reject_package,
    transition_package_status,
)
from app.worker.output_variants import (
    CanonicalContentResult,
    generate_all_variants,
)


class DistributionEvalCase(BaseModel):
    scenario_id: str
    description: str
    canonical: CanonicalContentResult
    owner_user_id: int
    attempt_user_id: int
    action: str
    expected_package_status: str
    expected_target_statuses: dict[str, str] = Field(default_factory=dict)
    expected_unauthorized_error: bool = False
    expected_duplicate_call_safe: bool = True


class DistributionEvalReport(BaseModel):
    total_scenarios: int
    passed_scenarios: int
    lifecycle_correctness: float
    approval_enforcement: float
    target_status_accuracy: float
    unauthorized_approval_violations: int
    duplicate_publication_violations: int
    factual_mutation_violations: int
    risk_warning_violations: int
    idempotency_violations: int
    all_gates_passed: bool
    scenario_details: list[dict[str, Any]] = Field(default_factory=list)


def load_distribution_cases(fixture_path: str | Path) -> list[DistributionEvalCase]:
    """Load evaluation cases from JSON fixture file."""
    path = Path(fixture_path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [DistributionEvalCase.model_validate(c) for c in data]


def evaluate_distribution(cases: list[DistributionEvalCase]) -> DistributionEvalReport:
    """
    Execute offline distribution evaluation replay against deterministic gates.
    """
    total = len(cases)
    passed = 0
    unauthorized_violations = 0
    duplicate_violations = 0
    factual_violations = 0
    risk_violations = 0
    idempotency_violations = 0
    correct_lifecycles = 0
    correct_approvals = 0
    correct_targets = 0

    scenario_details: list[dict[str, Any]] = []

    for case in cases:
        canonical = case.canonical
        variants = generate_all_variants(canonical)
        pkg = build_content_package(
            job_id=case.scenario_id,
            source_url=f"https://instagram.com/reel/{case.scenario_id}",
            canonical=canonical,
            variants=variants,
        )

        detail: dict[str, Any] = {
            "scenario_id": case.scenario_id,
            "status": "PASS",
            "errors": [],
        }

        # Check invariant: Generation != Approval
        if pkg.approval_state == PackageStatus.APPROVED:
            unauthorized_violations += 1
            detail["errors"].append("Generation violated approval invariant: package was born APPROVED")

        # Execute Scenario Actions
        action = case.action
        unauthorized_caught = False

        if action == "CREATE_AND_DELIVER_TELEGRAM":
            # Simulate worker delivering Telegram user and channel
            pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
            pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.DELIVERED
            rec_user = DeliveryRecord.create(
                package_id=pkg.package_id,
                target=TargetPlatform.TELEGRAM_USER,
                variant=OutputVariantType.TELEGRAM_LONG,
                attempt_id=1,
                approval_state=pkg.approval_state,
                status=DeliveryOutcome.SUCCEEDED,
            )
            rec_ch = DeliveryRecord.create(
                package_id=pkg.package_id,
                target=TargetPlatform.TELEGRAM_CHANNEL,
                variant=OutputVariantType.TELEGRAM_LONG,
                attempt_id=1,
                approval_state=pkg.approval_state,
                status=DeliveryOutcome.SUCCEEDED,
            )
            pkg.delivery_records.extend([rec_user, rec_ch])
            transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)

        elif action == "CREATE_LOW_PRIORITY":
            pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
            pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.NOT_RENDERABLE
            transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)

        elif action == "DEDUP_CHANNEL_SKIP":
            pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
            pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.SKIPPED_DUPLICATE
            transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)

        elif action == "APPROVE_EXTERNAL":
            pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
            pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.DELIVERED
            transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)
            pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id)

        elif action == "REJECT_PACKAGE":
            pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
            transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)
            pkg = reject_package(pkg, case.attempt_user_id, case.owner_user_id, "Rejected in eval")

        elif action == "RETRYABLE_ERROR_CHECK":
            pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
            pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.DELIVERED
            transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)
            is_ret, err_code, _ = classify_delivery_error("Connection timed out after 30s")
            if not is_ret or err_code != "TIMEOUT":
                detail["errors"].append("Retryable error misclassified")

        elif action == "DOUBLE_APPROVE_IDEMPOTENCY":
            pkg.distribution_targets[TargetPlatform.TELEGRAM_USER.value].status = TargetDeliveryStatus.DELIVERED
            pkg.distribution_targets[TargetPlatform.TELEGRAM_CHANNEL.value].status = TargetDeliveryStatus.DELIVERED
            transition_package_status(pkg, PackageStatus.PARTIALLY_DELIVERED)
            pkg, recs1 = approve_package(pkg, case.attempt_user_id, case.owner_user_id)
            pkg, recs2 = approve_package(pkg, case.attempt_user_id, case.owner_user_id)
            if len(recs2) != 0:
                idempotency_violations += 1
                duplicate_violations += 1
                detail["errors"].append(f"Double approve created {len(recs2)} duplicate delivery records")

        elif action == "UNAUTHORIZED_APPROVAL":
            try:
                pkg, _ = approve_package(pkg, case.attempt_user_id, case.owner_user_id)
                unauthorized_violations += 1
                detail["errors"].append("Unauthorized approval was not blocked")
            except UnauthorizedApprovalError:
                unauthorized_caught = True

        # Check Invariant: Fact-preservation on regeneration
        for vt in OutputVariantType:
            regen = regenerate_variant_from_canonical(canonical, vt)
            if canonical.critical_disclaimers:
                for d in canonical.critical_disclaimers:
                    if regen.status == "RENDERED" and d not in (regen.text or ""):
                        risk_violations += 1
                        detail["errors"].append(f"Regenerated variant {vt.value} lost critical disclaimer")
            title_prefix = canonical.title[:40]
            if (
                title_prefix not in (regen.text or "")
                and regen.status == "RENDERED"
                and vt in (OutputVariantType.TLDR, OutputVariantType.TELEGRAM_LONG, OutputVariantType.YOUTUBE_COMMUNITY)
            ):
                factual_violations += 1
                detail["errors"].append(f"Regenerated variant {vt.value} lost canonical title")

        # Verify expected statuses
        if unauthorized_caught:
            if not case.expected_unauthorized_error:
                detail["errors"].append("Unexpected unauthorized error caught")
            else:
                correct_approvals += 1
        else:
            if case.expected_unauthorized_error:
                detail["errors"].append("Expected unauthorized error was not raised")
            else:
                correct_approvals += 1

        if pkg.approval_state.value == case.expected_package_status:
            correct_lifecycles += 1
        else:
            detail["errors"].append(
                f"Package status mismatch: got {pkg.approval_state.value}, expected {case.expected_package_status}"
            )

        target_match = True
        for tgt_k, expected_st in case.expected_target_statuses.items():
            actual_st = pkg.distribution_targets.get(tgt_k)
            actual_st_val = actual_st.status.value if actual_st else "MISSING"
            if actual_st_val != expected_st:
                target_match = False
                detail["errors"].append(f"Target {tgt_k} status mismatch: got {actual_st_val}, expected {expected_st}")

        if target_match:
            correct_targets += 1

        if not detail["errors"]:
            passed += 1
        else:
            detail["status"] = "FAIL"

        scenario_details.append(detail)

    all_gates = (
        passed == total
        and unauthorized_violations == 0
        and duplicate_violations == 0
        and factual_violations == 0
        and risk_violations == 0
        and idempotency_violations == 0
    )

    return DistributionEvalReport(
        total_scenarios=total,
        passed_scenarios=passed,
        lifecycle_correctness=correct_lifecycles / total if total else 0.0,
        approval_enforcement=correct_approvals / total if total else 0.0,
        target_status_accuracy=correct_targets / total if total else 0.0,
        unauthorized_approval_violations=unauthorized_violations,
        duplicate_publication_violations=duplicate_violations,
        factual_mutation_violations=factual_violations,
        risk_warning_violations=risk_violations,
        idempotency_violations=idempotency_violations,
        all_gates_passed=all_gates,
        scenario_details=scenario_details,
    )
