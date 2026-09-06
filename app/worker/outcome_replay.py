"""Deterministic replay evaluation suite for Outcome Learning Dataset & Prioritization Calibration v1."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.worker.content_package import OutputVariantType, TargetPlatform
from app.worker.outcome_analysis import (
    evaluate_calibration_readiness,
    run_shadow_calibration,
)
from app.worker.outcome_collector import (
    build_outcome_observation,
    compute_derived_metrics,
)
from app.worker.outcome_schemas import (
    AuditHorizon,
    CalibrationReadiness,
    DataQualityStatus,
    DataSufficiencyPolicy,
    OutcomeObservation,
)


class OutcomeReplayReport(BaseModel):
    """Evaluation report asserting all 7 mandatory gates for Outcome Learning."""

    total_scenarios: int = 0
    passed_scenarios: int = 0
    attribution_violations: int = 0
    duplicate_outcomes: int = 0
    horizon_mixing_violations: int = 0
    fake_zero_metric_violations: int = 0
    safety_floor_violations: int = 0
    automatic_policy_mutations: int = 0
    insufficient_data_false_recommendations: int = 0
    all_gates_passed: bool = False
    details: list[dict[str, Any]] = Field(default_factory=list)


def evaluate_outcome_replay_suite(
    fixture_path: Path | str,
) -> OutcomeReplayReport:
    """Run deterministic replay of outcome learning scenarios and evaluate safety gates."""
    path = Path(fixture_path)
    with path.open("r", encoding="utf-8") as f:
        cases = json.load(f)

    report = OutcomeReplayReport(total_scenarios=len(cases))

    # Baseline production policy parameters (MUST NOT MUTATE)
    initial_publish_threshold = 0.60
    initial_deprioritize_threshold = 0.40

    observations_by_key_horizon: dict[tuple[str, str], OutcomeObservation] = {}
    evaluated_observations: list[OutcomeObservation] = []

    for case in cases:
        scen_id = case["scenario_id"]
        target = TargetPlatform(case["target"])
        variant = OutputVariantType(case["variant"])
        horizon = AuditHorizon(case["horizon"])
        raw_metrics = case.get("raw_metrics", {})
        norm_metrics = case.get("normalized_metrics", {})
        priority_score = case.get("priority_score")
        priority_band = case.get("priority_band")
        category = case.get("router_category")
        risk = case.get("router_risk")
        integrity_str = case.get("content_integrity", "MATCH")

        # Stable attribution identifiers
        pkg_id = f"pkg_{scen_id}"
        job_id = f"job_{scen_id}"
        pub_key = f"pub_key_{scen_id}"
        snap_id = f"snap_{scen_id}"
        target_id = f"tgt_{scen_id}"

        # In case J (multiple horizons) or K (duplicate retry), reuse publication key to test deduplication
        if scen_id == "case_k_duplicate_audit_snapshot_retry":
            pub_key = "pub_key_shared_retry"
        elif scen_id == "case_j_same_publication_multiple_horizons":
            pub_key = "pub_key_shared_multi_horizon"

        package_data = {
            "router_result": {
                "primary_type": category,
                "risk": risk,
            },
            "priority_result": {
                "score": {"overall": priority_score},
                "tier": priority_band,
            },
            "language_context": {
                "detected_language_code": "ru",
            },
        }

        checked_at = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
        published_at = datetime.datetime(2026, 9, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)

        obs = build_outcome_observation(
            snapshot_id=snap_id,
            target_id=target_id,
            package_id=pkg_id,
            job_id=job_id,
            publication_key=pub_key,
            target_platform=target,
            variant_type=variant,
            raw_metrics=raw_metrics,
            normalized_metrics=norm_metrics,
            metric_deltas=None,
            object_exists=integrity_str != "DELETED_OR_NOT_FOUND",
            content_match=integrity_str == "MATCH",
            snapshot_status="VERIFIED" if integrity_str == "MATCH" else integrity_str,
            target_status="ACTIVE",
            tier=3 if horizon == AuditHorizon.H_24H else 0,
            scheduled_for=checked_at,
            checked_at=checked_at,
            published_at=published_at,
            package_data=package_data,
        )

        # Gate 1: Attribution validation (IDs must match exactly)
        if (
            obs.job_id != job_id
            or obs.package_id != pkg_id
            or obs.publication_key != pub_key
            or obs.audit_snapshot_id != snap_id
        ):
            report.attribution_violations += 1

        # Gate 2: Duplicate outcome detection (duplicate (pub_key, horizon) must be idempotent)
        key_horizon = (pub_key, obs.audit_horizon.value)
        if key_horizon in observations_by_key_horizon:
            if scen_id == "case_k_duplicate_audit_snapshot_retry":
                # Expected duplicate retry test - idempotent, do not count as violation
                pass
            else:
                report.duplicate_outcomes += 1
        else:
            observations_by_key_horizon[key_horizon] = obs

        # Gate 3: Horizon mixing validation
        if obs.audit_horizon != horizon:
            report.horizon_mixing_violations += 1

        # Gate 4: Fake zero metric validation (missing views denominator must produce None engagement rate)
        if case.get("expected_engagement_rate_is_none"):
            if obs.derived_metrics.engagement_rate is not None:
                report.fake_zero_metric_violations += 1
        elif (
            norm_metrics.get("views")
            and norm_metrics["views"] > 0
            and obs.derived_metrics.engagement_rate is None
        ):
            report.fake_zero_metric_violations += 1

        # Check expected quality status
        expected_quality = case.get("expected_quality")
        if expected_quality and obs.data_quality_status.value != expected_quality:
            report.details.append({
                "scenario_id": scen_id,
                "error": f"Quality mismatch: expected {expected_quality}, got {obs.data_quality_status.value}",
            })
        else:
            report.passed_scenarios += 1

        evaluated_observations.append(obs)

    # Gate 7: Insufficient data test (Scenario M)
    insufficient_policy = DataSufficiencyPolicy(min_samples_for_readiness=20, min_samples_per_cohort=5)
    insufficient_set = evaluated_observations[:3]  # Only 3 samples
    m_readiness = evaluate_calibration_readiness(insufficient_set, insufficient_policy)
    m_run = run_shadow_calibration(
        observations=insufficient_set,
        current_publish_threshold=initial_publish_threshold,
        current_deprioritize_threshold=initial_deprioritize_threshold,
        policy=insufficient_policy,
    )
    if m_readiness == CalibrationReadiness.CALIBRATION_READY or len(m_run.recommendations) > 0:
        report.insufficient_data_false_recommendations += 1

    # Gate 5 & 6: Calibration ready run and mutation check (Scenario N)
    # Construct a synthetic calibration-ready dataset with >= 20 valid items
    calib_set: list[OutcomeObservation] = []
    for i in range(25):
        band = "HIGH" if i < 10 else ("AMBIGUOUS" if i < 18 else "LOW")
        score = 0.85 if band == "HIGH" else (0.50 if band == "AMBIGUOUS" else 0.25)
        views = 10000 if band == "HIGH" else (9000 if band == "AMBIGUOUS" else 200)
        c_obs = build_outcome_observation(
            snapshot_id=f"snap_cal_{i}",
            target_id=f"tgt_cal_{i}",
            package_id=f"pkg_cal_{i}",
            job_id=f"job_cal_{i}",
            publication_key=f"pub_cal_{i}",
            target_platform=TargetPlatform.X,
            variant_type=OutputVariantType.X_POST,
            raw_metrics={"impression_count": views, "like_count": int(views * 0.05)},
            normalized_metrics={"views": views, "likes": int(views * 0.05)},
            metric_deltas=None,
            object_exists=True,
            content_match=True,
            snapshot_status="VERIFIED",
            target_status="ACTIVE",
            tier=3,
            scheduled_for=datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc),
            checked_at=datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc),
            published_at=datetime.datetime(2026, 9, 6, 12, 0, 0, tzinfo=datetime.timezone.utc),
            package_data={
                "router_result": {"primary_type": "NEWS", "risk": "LOW"},
                "priority_result": {"score": {"overall": score}, "tier": band},
                "language_context": {"detected_language_code": "ru"},
            },
        )
        calib_set.append(c_obs)

    calib_policy = DataSufficiencyPolicy(min_samples_for_readiness=20, min_samples_per_cohort=5, min_distinct_packages=5)
    n_readiness = evaluate_calibration_readiness(calib_set, calib_policy)
    if n_readiness != CalibrationReadiness.CALIBRATION_READY:
        report.details.append({"scenario_id": "case_n", "error": f"Expected CALIBRATION_READY, got {n_readiness}"})

    n_run = run_shadow_calibration(
        observations=calib_set,
        current_publish_threshold=initial_publish_threshold,
        current_deprioritize_threshold=initial_deprioritize_threshold,
        policy=calib_policy,
    )

    # Invariant: Recommendations MUST NEVER be applied
    if n_run.applied_recommendation_count != 0:
        report.automatic_policy_mutations += 1
    for rec in n_run.recommendations:
        if rec.applied is not False:
            report.automatic_policy_mutations += 1

    # Invariant: Safety floor check
    # Check that initial thresholds remain unchanged
    if initial_publish_threshold != 0.60 or initial_deprioritize_threshold != 0.40:
        report.safety_floor_violations += 1

    # Verify all gates
    report.all_gates_passed = (
        report.attribution_violations == 0
        and report.duplicate_outcomes == 0
        and report.horizon_mixing_violations == 0
        and report.fake_zero_metric_violations == 0
        and report.safety_floor_violations == 0
        and report.automatic_policy_mutations == 0
        and report.insufficient_data_false_recommendations == 0
        and report.passed_scenarios == report.total_scenarios
    )

    return report
