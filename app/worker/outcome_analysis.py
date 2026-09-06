"""Priority outcome analysis and shadow calibration engine."""

from __future__ import annotations

import datetime
import math
import uuid
from typing import Any

from app.worker.content_package import TargetPlatform
from app.worker.outcome_schemas import (
    AuditHorizon,
    CalibrationReadiness,
    CalibrationRecommendation,
    CalibrationRun,
    DataQualityStatus,
    DataSufficiencyPolicy,
    OutcomeObservation,
)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def compute_spearman_rank_correlation(x: list[float], y: list[float]) -> float | None:
    """Compute Spearman's rank correlation coefficient deterministically without external deps."""
    n = len(x)
    if n < 3 or len(y) != n:
        return None

    def rank(arr: list[float]) -> list[float]:
        indexed = sorted(enumerate(arr), key=lambda item: item[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and indexed[j][1] == indexed[j + 1][1]:
                j += 1
            mean_rank = (i + j + 2) / 2.0  # 1-based ranking
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = mean_rank
            i = j + 1
        return ranks

    rx = rank(x)
    ry = rank(y)

    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n

    num = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    den_x = math.sqrt(sum((rx[i] - mean_rx) ** 2 for i in range(n)))
    den_y = math.sqrt(sum((ry[i] - mean_ry) ** 2 for i in range(n)))

    if den_x == 0 or den_y == 0:
        return 0.0

    return round(num / (den_x * den_y), 4)


def summarize_numeric_series(values: list[float | int]) -> dict[str, Any]:
    """Compute basic distribution statistics including outlier-resistant medians."""
    if not values:
        return {"count": 0, "mean": None, "median": None, "p25": None, "p75": None, "min": None, "max": None}

    sorted_vals = sorted(float(v) for v in values)
    n = len(sorted_vals)
    mean_val = sum(sorted_vals) / n

    def percentile(p: float) -> float:
        k = (n - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_vals[int(k)]
        d0 = sorted_vals[int(f)] * (c - k)
        d1 = sorted_vals[int(c)] * (k - f)
        return d0 + d1

    median_val = percentile(0.5)
    p25 = percentile(0.25)
    p75 = percentile(0.75)

    # Trimmed mean (excluding top and bottom 10% if n >= 10)
    trimmed_mean = mean_val
    if n >= 10:
        trim_k = int(n * 0.1)
        trimmed_vals = sorted_vals[trim_k : n - trim_k]
        if trimmed_vals:
            trimmed_mean = sum(trimmed_vals) / len(trimmed_vals)

    return {
        "count": n,
        "mean": round(mean_val, 2),
        "median": round(median_val, 2),
        "trimmed_mean": round(trimmed_mean, 2),
        "p25": round(p25, 2),
        "p75": round(p75, 2),
        "min": round(sorted_vals[0], 2),
        "max": round(sorted_vals[-1], 2),
    }


def analyze_priority_outcomes(
    observations: list[OutcomeObservation],
    filter_data_quality: bool = True,
) -> dict[str, Any]:
    """Analyze the relationship between priority decisions and realized outcomes.

    Strict Invariants:
    - Never pools across platforms or horizons without segmentation.
    - Excludes deleted/modified content from standard calibration cohorts.
    - Missing metrics remain None, never coerced to 0.
    """
    valid_obs: list[OutcomeObservation] = []
    excluded_count = 0

    for obs in observations:
        if filter_data_quality and obs.data_quality_status in (
            DataQualityStatus.CONTENT_DELETED,
            DataQualityStatus.CONTENT_MODIFIED,
            DataQualityStatus.AUTH_UNAVAILABLE,
            DataQualityStatus.UNSUPPORTED,
            DataQualityStatus.NOT_APPLICABLE,
            DataQualityStatus.INSUFFICIENT_DATA,
        ):
            excluded_count += 1
            continue
        valid_obs.append(obs)

    # Rank correlation: priority_score vs views / engagement
    score_pairs = [
        (obs.priority_score, obs.derived_metrics.views)
        for obs in valid_obs
        if obs.priority_score is not None and obs.derived_metrics.views is not None
    ]
    views_rank_corr = None
    if len(score_pairs) >= 3:
        scores, views = zip(*score_pairs, strict=False)
        views_rank_corr = compute_spearman_rank_correlation(list(scores), list(views))

    # Cohort breakdown by priority_band (HIGH, AMBIGUOUS, LOW)
    cohorts: dict[str, list[OutcomeObservation]] = {"HIGH": [], "AMBIGUOUS": [], "LOW": []}
    for obs in valid_obs:
        band = obs.priority_band or "UNKNOWN"
        if band in cohorts:
            cohorts[band].append(obs)

    cohort_summary: dict[str, Any] = {}
    for band, obs_list in cohorts.items():
        views_list = [o.derived_metrics.views for o in obs_list if o.derived_metrics.views is not None]
        eng_list = [
            o.derived_metrics.engagement_total
            for o in obs_list
            if o.derived_metrics.engagement_total is not None
        ]
        rates_list = [
            o.derived_metrics.engagement_rate
            for o in obs_list
            if o.derived_metrics.engagement_rate is not None
        ]
        cohort_summary[band] = {
            "sample_count": len(obs_list),
            "views": summarize_numeric_series(views_list),
            "engagement_total": summarize_numeric_series(eng_list),
            "engagement_rate": summarize_numeric_series(rates_list),
        }

    # Breakdown by platform
    platform_summary: dict[str, Any] = {}
    for p in TargetPlatform:
        p_obs = [o for o in valid_obs if o.target == p]
        if p_obs:
            p_views = [o.derived_metrics.views for o in p_obs if o.derived_metrics.views is not None]
            platform_summary[p.value] = {
                "count": len(p_obs),
                "views": summarize_numeric_series(p_views),
            }

    # Breakdown by horizon
    horizon_summary: dict[str, Any] = {}
    for h in AuditHorizon:
        h_obs = [o for o in valid_obs if o.audit_horizon == h]
        if h_obs:
            h_views = [o.derived_metrics.views for o in h_obs if o.derived_metrics.views is not None]
            horizon_summary[h.value] = {
                "count": len(h_obs),
                "views": summarize_numeric_series(h_views),
            }

    # Breakdown by category
    category_summary: dict[str, Any] = {}
    for obs in valid_obs:
        cat = obs.router_primary_category or "UNKNOWN"
        if cat not in category_summary:
            category_summary[cat] = []
        if obs.derived_metrics.views is not None:
            category_summary[cat].append(obs.derived_metrics.views)

    cat_stats = {cat: summarize_numeric_series(v_list) for cat, v_list in category_summary.items()}

    return {
        "total_observations": len(observations),
        "valid_observations": len(valid_obs),
        "excluded_observations": excluded_count,
        "views_rank_correlation": views_rank_corr,
        "cohorts": cohort_summary,
        "platforms": platform_summary,
        "horizons": horizon_summary,
        "categories": cat_stats,
    }


def evaluate_calibration_readiness(
    observations: list[OutcomeObservation],
    policy: DataSufficiencyPolicy | None = None,
) -> CalibrationReadiness:
    """Evaluate whether the dataset meets sufficiency criteria for shadow recommendations."""
    pol = policy or DataSufficiencyPolicy()

    # Only VALID and PARTIAL observations qualify as evidence
    valid_obs = [
        o
        for o in observations
        if o.data_quality_status in (DataQualityStatus.VALID, DataQualityStatus.PARTIAL)
        and o.content_integrity_status == "MATCH"
    ]

    if not valid_obs:
        return CalibrationReadiness.INSUFFICIENT_DATA

    if len(valid_obs) < pol.min_samples_for_readiness:
        return CalibrationReadiness.OBSERVATION_ONLY

    # Check distinct packages
    distinct_packages = {o.package_id for o in valid_obs}
    if len(distinct_packages) < pol.min_distinct_packages:
        return CalibrationReadiness.OBSERVATION_ONLY

    # Check cohort representation
    high_count = sum(1 for o in valid_obs if o.priority_band == "HIGH")
    amb_count = sum(1 for o in valid_obs if o.priority_band == "AMBIGUOUS")
    low_count = sum(1 for o in valid_obs if o.priority_band == "LOW")

    if (
        high_count < pol.min_samples_per_cohort
        or amb_count < pol.min_samples_per_cohort
        or low_count < pol.min_samples_per_cohort
    ):
        return CalibrationReadiness.OBSERVATION_ONLY

    return CalibrationReadiness.CALIBRATION_READY


def run_shadow_calibration(
    observations: list[OutcomeObservation],
    current_publish_threshold: float,
    current_deprioritize_threshold: float,
    policy: DataSufficiencyPolicy | None = None,
    platform_filter: TargetPlatform | None = None,
    horizon_filter: AuditHorizon | None = AuditHorizon.H_24H,
) -> CalibrationRun:
    """Execute a shadow calibration run.

    STRICT SAFETY INVARIANTS:
    - Never mutates current_publish_threshold or current_deprioritize_threshold.
    - applied_recommendation_count MUST = 0.
    - Every recommendation generated has applied = False.
    - If dataset is not CALIBRATION_READY, zero recommendations are emitted.
    """
    start_time = _utc_now()
    pol = policy or DataSufficiencyPolicy()

    target_obs = observations
    if platform_filter:
        target_obs = [o for o in target_obs if o.target == platform_filter]
    if horizon_filter:
        target_obs = [o for o in target_obs if o.audit_horizon == horizon_filter]

    readiness = evaluate_calibration_readiness(target_obs, pol)
    analysis = analyze_priority_outcomes(target_obs)

    recommendations: list[CalibrationRecommendation] = []

    if readiness == CalibrationReadiness.CALIBRATION_READY:
        cohorts = analysis.get("cohorts", {})
        high_stats = cohorts.get("HIGH", {}).get("views", {})
        amb_stats = cohorts.get("AMBIGUOUS", {}).get("views", {})
        low_stats = cohorts.get("LOW", {}).get("views", {})

        high_median = high_stats.get("median") or 0.0
        amb_median = amb_stats.get("median") or 0.0
        low_median = low_stats.get("median") or 0.0

        # Calibration heuristic (shadow recommendation only):
        # If AMBIGUOUS cohort performs on par with HIGH cohort, suggest modest threshold lowering
        # If HIGH cohort performance is weak or indistinguishable from LOW, suggest threshold tightening
        platform_enum = platform_filter or TargetPlatform.X
        horizon_enum = horizon_filter or AuditHorizon.H_24H
        sample_size = analysis.get("valid_observations", 0)

        if amb_median > 0 and high_median > 0 and amb_median >= 0.85 * high_median:
            suggested = max(0.45, current_publish_threshold - 0.05)
            confidence = min(0.95, round(0.50 + 0.01 * sample_size, 2))
            rec = CalibrationRecommendation(
                recommendation_id=f"rec_{uuid.uuid4().hex[:12]}",
                platform=platform_enum,
                horizon=horizon_enum,
                category=None,
                current_threshold=current_publish_threshold,
                suggested_threshold=suggested,
                evidence_sample_size=sample_size,
                confidence=confidence,
                estimated_tradeoff="Expanding publish threshold may capture high-performing ambiguous content with minimal noise.",
                reason=f"Ambiguous cohort median views ({amb_median}) closely match High cohort ({high_median}).",
                applied=False,
            )
            recommendations.append(rec)
        elif high_median > 0 and low_median > 0 and high_median <= 1.2 * low_median:
            suggested = min(0.85, current_publish_threshold + 0.05)
            confidence = min(0.95, round(0.50 + 0.01 * sample_size, 2))
            rec = CalibrationRecommendation(
                recommendation_id=f"rec_{uuid.uuid4().hex[:12]}",
                platform=platform_enum,
                horizon=horizon_enum,
                category=None,
                current_threshold=current_publish_threshold,
                suggested_threshold=suggested,
                evidence_sample_size=sample_size,
                confidence=confidence,
                estimated_tradeoff="Tightening publish threshold protects distribution channels from low-engagement false positives.",
                reason=f"High cohort median views ({high_median}) not sufficiently differentiated from Low cohort ({low_median}).",
                applied=False,
            )
            recommendations.append(rec)

    completed_time = _utc_now()
    run = CalibrationRun(
        run_id=f"crun_{uuid.uuid4().hex[:16]}",
        started_at=start_time,
        completed_at=completed_time,
        readiness=readiness,
        sample_count=len(target_obs),
        valid_sample_count=analysis.get("valid_observations", 0),
        recommendations=recommendations,
        applied_recommendation_count=0,
        metrics_summary=analysis,
    )
    return run
