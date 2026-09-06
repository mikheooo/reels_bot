"""Comprehensive test suite for Stage: Outcome Learning Dataset & Prioritization Calibration v1."""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.content_package import OutputVariantType, TargetPlatform
from app.worker.outcome_analysis import (
    analyze_priority_outcomes,
    compute_spearman_rank_correlation,
    evaluate_calibration_readiness,
    run_shadow_calibration,
    summarize_numeric_series,
)
from app.worker.outcome_collector import (
    backfill_historical_outcomes,
    build_outcome_observation,
    classify_data_quality,
    compute_derived_metrics,
    record_outcome_observation,
    resolve_horizon,
)
from app.worker.outcome_replay import evaluate_outcome_replay_suite
from app.worker.outcome_schemas import (
    AuditHorizon,
    CalibrationReadiness,
    CalibrationRecommendation,
    CalibrationRun,
    ContentIntegrityStatus,
    DataQualityStatus,
    DataSufficiencyPolicy,
    OutcomeDerivedMetrics,
    OutcomeObservation,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "outcome_learning_eval.json"


# Test 01: Outcome typed schema validation
def test_01_outcome_typed_schema_validation():
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    obs = OutcomeObservation(
        outcome_id="out_test_01",
        job_id="job_test_01",
        package_id="pkg_test_01",
        publication_key="pub_key_01",
        target=TargetPlatform.X,
        variant_type=OutputVariantType.X_POST,
        content_type="NEWS",
        router_primary_category="NEWS",
        router_risk_level="LOW",
        priority_score=0.75,
        priority_band="HIGH",
        language_code="ru",
        published_at=now,
        audit_horizon=AuditHorizon.H_24H,
        audit_snapshot_id="snap_test_01",
        metrics={"impression_count": 1000},
        derived_metrics=OutcomeDerivedMetrics(views=1000, likes=50),
        content_integrity_status=ContentIntegrityStatus.MATCH,
        data_quality_status=DataQualityStatus.VALID,
    )
    assert obs.outcome_id == "out_test_01"
    assert obs.target == TargetPlatform.X
    assert obs.audit_horizon == AuditHorizon.H_24H
    assert obs.metrics["impression_count"] == 1000
    assert obs.derived_metrics.views == 1000
    assert obs.derived_metrics.likes == 50


# Test 02: Attribution immutability
def test_02_attribution_immutability():
    obs = build_outcome_observation(
        snapshot_id="snap_attr_123",
        target_id="tgt_attr_123",
        package_id="pkg_attr_123",
        job_id="job_attr_123",
        publication_key="pkg_attr_123:X:X_POST:abc123456789",
        target_platform=TargetPlatform.X,
        variant_type=OutputVariantType.X_POST,
        raw_metrics={"impression_count": 200},
        normalized_metrics={"views": 200},
        metric_deltas=None,
        object_exists=True,
        content_match=True,
        snapshot_status="VERIFIED",
        target_status="ACTIVE",
        tier=0,
        scheduled_for=None,
        checked_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        published_at=None,
        package_data={
            "router_result": {"primary_type": "HOW_TO", "risk": "MEDIUM"},
            "priority_result": {"score": {"overall": 0.65}, "tier": "HIGH"},
            "language_context": {"detected_language_code": "ru"},
        },
    )
    assert obs.job_id == "job_attr_123"
    assert obs.package_id == "pkg_attr_123"
    assert obs.publication_key == "pkg_attr_123:X:X_POST:abc123456789"
    assert obs.audit_snapshot_id == "snap_attr_123"
    assert obs.target == TargetPlatform.X
    assert obs.variant_type == OutputVariantType.X_POST
    assert obs.router_primary_category == "HOW_TO"
    assert obs.router_risk_level == "MEDIUM"
    assert obs.priority_score == 0.65
    assert obs.priority_band == "HIGH"


# Test 03: Raw source metrics preserved
def test_03_raw_source_metrics_preserved():
    raw = {"impression_count": 5500, "like_count": 220, "custom_provider_field": "preserved"}
    derived = compute_derived_metrics(
        target_platform=TargetPlatform.X,
        raw_metrics=raw,
        normalized_metrics={"views": 5500, "likes": 220},
    )
    # Raw dictionary is not modified
    assert raw["impression_count"] == 5500
    assert raw["custom_provider_field"] == "preserved"
    assert "views" not in raw


# Test 04: Derived metrics separate
def test_04_derived_metrics_separate():
    raw = {"impression_count": 10000, "like_count": 500}
    norm = {"views": 10000, "likes": 500, "replies": 50, "reposts": 50}
    derived = compute_derived_metrics(
        target_platform=TargetPlatform.X,
        raw_metrics=raw,
        normalized_metrics=norm,
        elapsed_seconds=7200.0,
    )
    assert derived.views == 10000
    assert derived.likes == 500
    assert derived.engagement_total == 600
    assert derived.engagement_rate == 0.06
    assert derived.velocity_views_per_hour == 5000.0
    assert derived.velocity_engagement_per_hour == 300.0
    # Raw remains unaffected
    assert "engagement_rate" not in raw


# Test 05: Missing denominator -> None (Never fake zero)
def test_05_missing_denominator_results_in_none():
    norm_no_views = {"views": None, "likes": 50, "replies": 10}
    derived = compute_derived_metrics(
        target_platform=TargetPlatform.THREADS,
        raw_metrics={},
        normalized_metrics=norm_no_views,
    )
    # Critical invariant: engagement_rate MUST be None, never 0.0
    assert derived.engagement_rate is None

    norm_zero_views = {"views": 0, "likes": 50}
    derived_zero = compute_derived_metrics(
        target_platform=TargetPlatform.THREADS,
        raw_metrics={},
        normalized_metrics=norm_zero_views,
    )
    assert derived_zero.engagement_rate is None


# Test 06: Multiple horizons distinct
def test_06_multiple_horizons_distinct():
    pub_time = datetime.datetime(2026, 9, 7, 0, 0, 0, tzinfo=datetime.timezone.utc)
    h_15m = resolve_horizon(tier=0, published_at=pub_time, checked_at=pub_time + datetime.timedelta(minutes=15))
    h_2h = resolve_horizon(tier=1, published_at=pub_time, checked_at=pub_time + datetime.timedelta(hours=2))
    h_12h = resolve_horizon(tier=2, published_at=pub_time, checked_at=pub_time + datetime.timedelta(hours=12))
    h_24h = resolve_horizon(tier=3, published_at=pub_time, checked_at=pub_time + datetime.timedelta(hours=24))
    h_3d = resolve_horizon(tier=4, published_at=pub_time, checked_at=pub_time + datetime.timedelta(days=3))
    h_7d = resolve_horizon(tier=5, published_at=pub_time, checked_at=pub_time + datetime.timedelta(days=7))

    assert h_15m == AuditHorizon.H_15M
    assert h_2h == AuditHorizon.H_2H
    assert h_12h == AuditHorizon.H_12H
    assert h_24h == AuditHorizon.H_24H
    assert h_3d == AuditHorizon.H_3D
    assert h_7d == AuditHorizon.H_7D
    # None of them are equal
    assert len({h_15m, h_2h, h_12h, h_24h, h_3d, h_7d}) == 6


# Test 07: Duplicate horizon insert idempotent
@pytest.mark.asyncio
async def test_07_duplicate_horizon_insert_idempotent():
    mock_session = AsyncMock()
    # First query: existing is returned
    existing_mock = MagicMock()
    existing_mock.id = "out_existing_123"
    result_mock = MagicMock()
    result_mock.scalars.return_value.first.return_value = existing_mock
    mock_session.execute.return_value = result_mock

    snap = MagicMock()
    snap.id = "snap_dup_1"
    snap.scheduled_for = None
    snap.checked_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    snap.metrics = {}
    snap.normalized_metrics = {}
    snap.metric_deltas = None
    snap.status = "VERIFIED"
    snap.object_exists = True
    snap.content_match = True

    target = MagicMock()
    target.id = "tgt_dup_1"
    target.target = "X"
    target.variant = "X_POST"
    target.publication_key = "pub_dup_123"
    target.tier = 3
    target.status = "SCHEDULED"
    target.created_at = None

    pkg = MagicMock()
    pkg.id = "pkg_dup_1"
    pkg.job_id = "job_dup_1"
    pkg.router_result = {}
    pkg.priority_result = {}
    pkg.language_context = {}

    model = await record_outcome_observation(
        session=mock_session,
        snapshot=snap,
        target=target,
        package=pkg,
    )
    assert model.id == "out_existing_123"
    assert not mock_session.add.called


# Test 08: Modified content excluded/flagged
def test_08_modified_content_excluded_flagged():
    quality = classify_data_quality(
        target_status="ACTIVE",
        snapshot_status="MODIFIED",
        object_exists=True,
        content_match=False,
        normalized_metrics={"views": 5000},
    )
    assert quality == DataQualityStatus.CONTENT_MODIFIED

    obs = OutcomeObservation(
        outcome_id="out_mod",
        job_id="j_mod",
        package_id="p_mod",
        publication_key="k_mod",
        target=TargetPlatform.X,
        variant_type=OutputVariantType.X_POST,
        audit_horizon=AuditHorizon.H_24H,
        audit_snapshot_id="s_mod",
        derived_metrics=OutcomeDerivedMetrics(views=5000),
        content_integrity_status=ContentIntegrityStatus.MODIFIED,
        data_quality_status=DataQualityStatus.CONTENT_MODIFIED,
    )
    analysis = analyze_priority_outcomes([obs], filter_data_quality=True)
    assert analysis["valid_observations"] == 0
    assert analysis["excluded_observations"] == 1


# Test 09: Deleted content excluded/flagged
def test_09_deleted_content_excluded_flagged():
    quality = classify_data_quality(
        target_status="ACTIVE",
        snapshot_status="DELETED_OR_NOT_FOUND",
        object_exists=False,
        content_match=None,
        normalized_metrics={},
    )
    assert quality == DataQualityStatus.CONTENT_DELETED

    obs = OutcomeObservation(
        outcome_id="out_del",
        job_id="j_del",
        package_id="p_del",
        publication_key="k_del",
        target=TargetPlatform.X,
        variant_type=OutputVariantType.X_POST,
        audit_horizon=AuditHorizon.H_24H,
        audit_snapshot_id="s_del",
        content_integrity_status=ContentIntegrityStatus.DELETED_OR_NOT_FOUND,
        data_quality_status=DataQualityStatus.CONTENT_DELETED,
    )
    analysis = analyze_priority_outcomes([obs], filter_data_quality=True)
    assert analysis["valid_observations"] == 0
    assert analysis["excluded_observations"] == 1


# Test 10: Unsupported provider safe
def test_10_unsupported_provider_safe():
    quality = classify_data_quality(
        target_status="DELETION_VERIFICATION_UNSUPPORTED",
        snapshot_status="NOT_APPLICABLE",
        object_exists=True,
        content_match=None,
        normalized_metrics={},
    )
    assert quality == DataQualityStatus.UNSUPPORTED


# Test 11: Insufficient data state
def test_11_insufficient_data_state():
    readiness = evaluate_calibration_readiness([])
    assert readiness == CalibrationReadiness.INSUFFICIENT_DATA


# Test 12: Calibration-ready state
def test_12_calibration_ready_state():
    obs_list: list[OutcomeObservation] = []
    for i in range(25):
        band = "HIGH" if i < 10 else ("AMBIGUOUS" if i < 18 else "LOW")
        obs_list.append(
            OutcomeObservation(
                outcome_id=f"out_{i}",
                job_id=f"j_{i}",
                package_id=f"p_{i}",
                publication_key=f"k_{i}",
                target=TargetPlatform.X,
                variant_type=OutputVariantType.X_POST,
                priority_band=band,
                audit_horizon=AuditHorizon.H_24H,
                audit_snapshot_id=f"s_{i}",
                content_integrity_status=ContentIntegrityStatus.MATCH,
                data_quality_status=DataQualityStatus.VALID,
                derived_metrics=OutcomeDerivedMetrics(views=1000 + i),
            )
        )
    pol = DataSufficiencyPolicy(min_samples_for_readiness=20, min_samples_per_cohort=5, min_distinct_packages=5)
    readiness = evaluate_calibration_readiness(obs_list, pol)
    assert readiness == CalibrationReadiness.CALIBRATION_READY


# Test 13: No recommendation below minimum N
def test_13_no_recommendation_below_minimum_n():
    obs_list = [
        OutcomeObservation(
            outcome_id="out_few_1",
            job_id="j_few_1",
            package_id="p_few_1",
            publication_key="k_few_1",
            target=TargetPlatform.X,
            variant_type=OutputVariantType.X_POST,
            priority_band="HIGH",
            audit_horizon=AuditHorizon.H_24H,
            audit_snapshot_id="s_few_1",
            content_integrity_status=ContentIntegrityStatus.MATCH,
            data_quality_status=DataQualityStatus.VALID,
            derived_metrics=OutcomeDerivedMetrics(views=5000),
        )
    ]
    run = run_shadow_calibration(
        observations=obs_list,
        current_publish_threshold=0.60,
        current_deprioritize_threshold=0.40,
        policy=DataSufficiencyPolicy(min_samples_for_readiness=20),
    )
    assert run.readiness == CalibrationReadiness.OBSERVATION_ONLY
    assert len(run.recommendations) == 0
    assert run.applied_recommendation_count == 0


# Test 14: Rank/calibration analysis deterministic
def test_14_rank_calibration_analysis_deterministic():
    scores = [0.9, 0.8, 0.7, 0.6, 0.5]
    views = [10000, 8000, 6000, 4000, 2000]
    corr = compute_spearman_rank_correlation(scores, views)
    # Perfect monotonic rank correlation
    assert corr == 1.0


# Test 15: Outlier does not dominate improperly
def test_15_outlier_does_not_dominate_improperly():
    # 9 normal posts around 100 views, 1 viral post with 1,000,000 views
    views = [100, 110, 95, 105, 100, 90, 115, 105, 95, 1000000]
    summary = summarize_numeric_series(views)
    # Mean is heavily skewed by the outlier
    assert summary["mean"] > 90000
    # Median and trimmed mean remain resilient
    assert summary["median"] <= 110
    assert summary["trimmed_mean"] <= 120


# Test 16: Cross-platform separation
def test_16_cross_platform_separation():
    obs_x = OutcomeObservation(
        outcome_id="out_x",
        job_id="j_x",
        package_id="p_x",
        publication_key="k_x",
        target=TargetPlatform.X,
        variant_type=OutputVariantType.X_POST,
        audit_horizon=AuditHorizon.H_24H,
        audit_snapshot_id="s_x",
        derived_metrics=OutcomeDerivedMetrics(views=50000),
        content_integrity_status=ContentIntegrityStatus.MATCH,
        data_quality_status=DataQualityStatus.VALID,
    )
    obs_threads = OutcomeObservation(
        outcome_id="out_th",
        job_id="j_th",
        package_id="p_th",
        publication_key="k_th",
        target=TargetPlatform.THREADS,
        variant_type=OutputVariantType.THREADS_POST,
        audit_horizon=AuditHorizon.H_24H,
        audit_snapshot_id="s_th",
        derived_metrics=OutcomeDerivedMetrics(views=3000),
        content_integrity_status=ContentIntegrityStatus.MATCH,
        data_quality_status=DataQualityStatus.VALID,
    )
    analysis = analyze_priority_outcomes([obs_x, obs_threads])
    assert "X" in analysis["platforms"]
    assert "THREADS" in analysis["platforms"]
    assert analysis["platforms"]["X"]["views"]["mean"] == 50000.0
    assert analysis["platforms"]["THREADS"]["views"]["mean"] == 3000.0


# Test 17: Calibration recommendation persistence
def test_17_calibration_recommendation_persistence():
    rec = CalibrationRecommendation(
        recommendation_id="rec_pers_1",
        platform=TargetPlatform.X,
        horizon=AuditHorizon.H_24H,
        category=None,
        current_threshold=0.60,
        suggested_threshold=0.55,
        evidence_sample_size=35,
        confidence=0.85,
        estimated_tradeoff="Capture more high-performing ambiguous content.",
        reason="Ambiguous cohort views equal to high cohort.",
        applied=False,
    )
    run = CalibrationRun(
        run_id="crun_pers_1",
        started_at=datetime.datetime(2026, 9, 7, 10, 0, 0, tzinfo=datetime.timezone.utc),
        completed_at=datetime.datetime(2026, 9, 7, 10, 0, 5, tzinfo=datetime.timezone.utc),
        readiness=CalibrationReadiness.CALIBRATION_READY,
        sample_count=35,
        valid_sample_count=35,
        recommendations=[rec],
        applied_recommendation_count=0,
    )
    dump = run.model_dump()
    assert dump["run_id"] == "crun_pers_1"
    assert dump["applied_recommendation_count"] == 0
    assert dump["recommendations"][0]["applied"] is False


# Test 18: Recommendation does not change production thresholds
def test_18_recommendation_does_not_change_production_thresholds():
    obs_list: list[OutcomeObservation] = []
    for i in range(25):
        band = "HIGH" if i < 10 else ("AMBIGUOUS" if i < 18 else "LOW")
        score = 0.85 if band == "HIGH" else (0.50 if band == "AMBIGUOUS" else 0.25)
        views = 10000 if band == "HIGH" else (9500 if band == "AMBIGUOUS" else 200)
        obs_list.append(
            OutcomeObservation(
                outcome_id=f"out_th_{i}",
                job_id=f"j_th_{i}",
                package_id=f"p_th_{i}",
                publication_key=f"k_th_{i}",
                target=TargetPlatform.X,
                variant_type=OutputVariantType.X_POST,
                priority_band=band,
                priority_score=score,
                audit_horizon=AuditHorizon.H_24H,
                audit_snapshot_id=f"s_th_{i}",
                content_integrity_status=ContentIntegrityStatus.MATCH,
                data_quality_status=DataQualityStatus.VALID,
                derived_metrics=OutcomeDerivedMetrics(views=views),
            )
        )

    prod_publish_threshold = 0.60
    prod_deprioritize_threshold = 0.40

    run = run_shadow_calibration(
        observations=obs_list,
        current_publish_threshold=prod_publish_threshold,
        current_deprioritize_threshold=prod_deprioritize_threshold,
    )

    # Suggestions may exist in shadow mode
    assert len(run.recommendations) > 0
    # Invariant: Production variables MUST NOT be mutated
    assert prod_publish_threshold == 0.60
    assert prod_deprioritize_threshold == 0.40
    # Invariant: Recommendations MUST have applied = False
    assert run.applied_recommendation_count == 0
    for rec in run.recommendations:
        assert rec.applied is False


# Test 19: Risk floors immutable
def test_19_risk_floors_immutable():
    # Outcome learning does not allow high-risk content to bypass fact check
    from app.worker.content_router import (
        IntentScore,
        LabelScore,
        RouterDecision,
        policy_for,
    )
    from app.worker.priority_policy import PriorityGateResult, apply_priority_policy

    route_high_risk = RouterDecision(
        primary_type="HOW_TO",
        labels=[LabelScore(label="HOW_TO", confidence=0.9)],
        intents=[IntentScore(intent="TEACH", confidence=0.8)],
        risk="HIGH",
        risk_reasons=["High financial risk claim"],
        summary="High risk content",
    )
    router_pol = policy_for(route_high_risk)
    assert router_pol.run_fact_check is True
    assert router_pol.strict_fact_check is True

    # Even with high priority and strong outcomes, apply_priority_policy must preserve safety
    high_gate = PriorityGateResult(
        tier="HIGH",
        decision="ACCEPTED",
        publish_threshold=0.60,
        deprioritize_below=0.40,
    )
    comb = apply_priority_policy(route_high_risk, high_gate)
    assert comb.effective_policy.run_fact_check is True
    assert comb.effective_policy.strict_fact_check is True


# Test 20: Historical backfill idempotent
@pytest.mark.asyncio
async def test_20_historical_backfill_idempotent():
    mock_session = AsyncMock()
    # Mock returning 0 snapshots (current production baseline)
    mock_res = MagicMock()
    mock_res.all.return_value = []
    mock_session.execute.return_value = mock_res

    count1, ids1 = await backfill_historical_outcomes(mock_session)
    assert count1 == 0
    assert ids1 == []

    count2, ids2 = await backfill_historical_outcomes(mock_session)
    assert count2 == 0
    assert ids2 == []


# Test 21: Outcome replay all gates green
def test_21_outcome_replay_all_gates_green():
    assert FIXTURE_PATH.exists(), f"Fixture {FIXTURE_PATH} not found"
    report = evaluate_outcome_replay_suite(FIXTURE_PATH)
    assert report.total_scenarios == 14
    assert report.passed_scenarios == 14
    assert report.attribution_violations == 0
    assert report.duplicate_outcomes == 0
    assert report.horizon_mixing_violations == 0
    assert report.fake_zero_metric_violations == 0
    assert report.safety_floor_violations == 0
    assert report.automatic_policy_mutations == 0
    assert report.insufficient_data_false_recommendations == 0
    assert report.all_gates_passed is True
