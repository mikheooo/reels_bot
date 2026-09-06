"""Typed, deterministic bridge from Router and priority scoring to policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.worker.content_router import AnalysisPolicy, RouterDecision, policy_for
from app.worker.schemas import PriorityScore

PriorityTier = Literal["HIGH", "LOW", "AMBIGUOUS"]
PriorityGateDecision = Literal[
    "ACCEPTED",
    "DEPRIORITIZED",
    "AMBIGUOUS_CONTINUE",
    "FALLBACK_CONTINUE",
]

DEFAULT_DEPRIORITIZE_THRESHOLD = 0.40
POLICY_NAME = "router_priority_v1"


class PriorityGateResult(BaseModel):
    """Observable result of converting a score into a policy-relevant tier."""

    score: PriorityScore | None = None
    tier: PriorityTier
    decision: PriorityGateDecision
    publish_threshold: float = Field(ge=0.0, le=1.0)
    deprioritize_below: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    failure_code: str | None = None
    policy_name: Literal["router_priority_v1"] = POLICY_NAME


class CombinedPolicyResult(BaseModel):
    """Router safety policy plus priority-controlled optional decisions."""

    router_policy: AnalysisPolicy
    effective_policy: AnalysisPolicy
    priority: PriorityGateResult
    user_delivery: Literal[True] = True
    channel_publication: bool
    suppressed_actions: list[str] = Field(default_factory=list)
    policy_name: Literal["router_priority_v1"] = POLICY_NAME


def evaluate_priority(
    score: PriorityScore,
    publish_threshold: float,
    deprioritize_below: float = DEFAULT_DEPRIORITIZE_THRESHOLD,
) -> PriorityGateResult:
    """Classify a validated score without another model call."""
    if not 0.0 <= deprioritize_below < publish_threshold <= 1.0:
        raise ValueError("Priority thresholds must satisfy 0 <= low < publish <= 1")

    expected_publish = score.overall >= publish_threshold
    if score.publish != expected_publish:
        raise ValueError("PriorityScore.publish is inconsistent with overall/threshold")

    if expected_publish:
        tier: PriorityTier = "HIGH"
        decision: PriorityGateDecision = "ACCEPTED"
        computed_reason = f"overall >= publish threshold ({publish_threshold:.2f})"
    elif score.overall < deprioritize_below:
        tier = "LOW"
        decision = "DEPRIORITIZED"
        computed_reason = f"overall < deprioritize threshold ({deprioritize_below:.2f})"
    else:
        tier = "AMBIGUOUS"
        decision = "AMBIGUOUS_CONTINUE"
        computed_reason = (
            f"overall is between {deprioritize_below:.2f} and "
            f"{publish_threshold:.2f}; preserve router policy"
        )

    return PriorityGateResult(
        score=score,
        tier=tier,
        decision=decision,
        publish_threshold=publish_threshold,
        deprioritize_below=deprioritize_below,
        reasons=[*score.reasons, computed_reason],
    )


def fallback_priority(
    failure: BaseException,
    publish_threshold: float,
    deprioritize_below: float = DEFAULT_DEPRIORITIZE_THRESHOLD,
) -> PriorityGateResult:
    """Preserve safety work and user delivery, but withhold public publication."""
    return PriorityGateResult(
        tier="AMBIGUOUS",
        decision="FALLBACK_CONTINUE",
        publish_threshold=publish_threshold,
        deprioritize_below=deprioritize_below,
        reasons=["Priority evaluation unavailable; router safety policy preserved."],
        fallback_used=True,
        failure_code=type(failure).__name__,
    )


def apply_priority_policy(
    route: RouterDecision, priority: PriorityGateResult
) -> CombinedPolicyResult:
    """Suppress only usefulness actions; never relax Router safety requirements."""
    router_policy = policy_for(route)
    effective_policy = router_policy.model_copy(deep=True)
    suppressed: list[str] = []
    channel_publication = True

    if priority.decision == "DEPRIORITIZED":
        for field in (
            "include_technical_details",
            "run_personal_relevance",
            "create_tasks",
        ):
            if getattr(effective_policy, field):
                setattr(effective_policy, field, False)
                suppressed.append(field)
        channel_publication = False
        suppressed.append("channel_publication")
    elif priority.decision == "FALLBACK_CONTINUE":
        # Unknown priority cannot relax safety work or hide the user result, but
        # it is not enough evidence for a public-channel side effect.
        channel_publication = False
        suppressed.append("channel_publication")

    if router_policy.run_fact_check and not effective_policy.run_fact_check:
        raise AssertionError("priority policy relaxed Router fact-check floor")
    if router_policy.strict_fact_check and not effective_policy.strict_fact_check:
        raise AssertionError("priority policy relaxed Router strict risk floor")
    if router_policy.run_business_check and not effective_policy.run_business_check:
        raise AssertionError("priority policy relaxed Router business-check floor")

    return CombinedPolicyResult(
        router_policy=router_policy,
        effective_policy=effective_policy,
        priority=priority,
        channel_publication=channel_publication,
        suppressed_actions=suppressed,
    )


def priority_delivery_outcome(priority: PriorityGateResult) -> str:
    if priority.fallback_used:
        return f"FAILED_FALLBACK:{priority.failure_code or 'UnknownError'}"
    return "SUCCEEDED"


def policy_observability_payload(
    route: RouterDecision, combined: CombinedPolicyResult
) -> dict:
    """Canonical JSON payload persisted both before and after downstream work."""
    return {
        "router": route.model_dump(),
        "priority": combined.priority.model_dump(),
        "policy": combined.model_dump(),
    }


def format_priority_summary(priority: PriorityGateResult) -> str:
    score = f"{priority.score.overall:.2f}" if priority.score else "недоступна"
    labels = {
        "HIGH": "высокий",
        "LOW": "низкий",
        "AMBIGUOUS": "неопределённый",
    }
    reason = priority.reasons[0] if priority.reasons else "Без дополнительного пояснения."
    return f"🎯 **Приоритет:** {labels[priority.tier]} (оценка: {score}). {reason}"
