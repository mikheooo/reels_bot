import pytest

from app.worker.content_router import IntentScore, LabelScore, RouterDecision
from app.worker.priority_policy import (
    apply_priority_policy,
    evaluate_priority,
    fallback_priority,
    format_priority_summary,
    policy_observability_payload,
    priority_delivery_outcome,
)
from app.worker.schemas import PriorityScore
from app.worker.tasks import determine_completion_status


def route(primary="HOW_TO", risk="LOW", intents=None):
    return RouterDecision(
        primary_type=primary,
        labels=[LabelScore(label=primary, confidence=0.95)],
        intents=[
            IntentScore(intent=intent, confidence=confidence)
            for intent, confidence in (intents or [("TEACH", 0.9)])
        ],
        risk=risk,
        summary="fixture",
    )


def score(overall: float, *, publish: bool | None = None) -> PriorityScore:
    return PriorityScore(
        importance=overall,
        virality=overall,
        novelty=overall,
        views_potential=overall,
        audience_value=overall,
        overall=overall,
        publish=overall >= 0.6 if publish is None else publish,
        reasons=["fixture signal"],
    )


def gate(overall: float):
    return evaluate_priority(score(overall), 0.6, 0.4)


def test_high_priority_passes_router_policy_unchanged():
    result = apply_priority_policy(route(), gate(0.8))
    assert result.priority.tier == "HIGH"
    assert result.priority.decision == "ACCEPTED"
    assert result.effective_policy == result.router_policy
    assert result.channel_publication is True


def test_low_priority_suppresses_only_usefulness_actions():
    result = apply_priority_policy(route(), gate(0.2))
    assert result.priority.tier == "LOW"
    assert result.effective_policy.include_technical_details is False
    assert result.effective_policy.create_tasks is False
    assert result.channel_publication is False
    assert result.user_delivery is True
    assert result.effective_policy.run_fact_check == result.router_policy.run_fact_check


def test_low_priority_cannot_relax_high_risk_fact_check_floor():
    risky = route(
        primary="HEALTH_MEDICAL",
        risk="HIGH",
        intents=[("PROMISE_RESULT", 0.95)],
    )
    result = apply_priority_policy(risky, gate(0.1))
    assert result.router_policy.run_fact_check is True
    assert result.router_policy.strict_fact_check is True
    assert result.effective_policy.run_fact_check is True
    assert result.effective_policy.strict_fact_check is True


def test_ambiguous_priority_preserves_policy_and_user_delivery():
    result = apply_priority_policy(route(), gate(0.5))
    assert result.priority.decision == "AMBIGUOUS_CONTINUE"
    assert result.effective_policy == result.router_policy
    assert result.user_delivery is True
    assert result.channel_publication is True


def test_low_priority_suppresses_task_creation():
    result = apply_priority_policy(route(primary="SOFTWARE_TOOL"), gate(0.1))
    assert result.router_policy.create_tasks is True
    assert result.effective_policy.create_tasks is False
    assert "create_tasks" in result.suppressed_actions


def test_channel_and_user_decisions_are_independent():
    result = apply_priority_policy(route(), gate(0.1))
    assert result.user_delivery is True
    assert result.channel_publication is False
    assert "channel_publication" in result.suppressed_actions


def test_priority_result_is_structured_and_explainable():
    result = apply_priority_policy(route(), gate(0.8))
    payload = result.model_dump()
    assert payload["priority"]["score"]["overall"] == pytest.approx(0.8)
    assert payload["priority"]["reasons"]
    assert payload["priority"]["publish_threshold"] == pytest.approx(0.6)
    assert payload["policy_name"] == "router_priority_v1"

    persisted = policy_observability_payload(route(), result)
    assert persisted["router"]["primary_type"] == "HOW_TO"
    assert persisted["priority"]["tier"] == "HIGH"
    assert persisted["policy"]["effective_policy"]["create_tasks"] is True


def test_failure_fallback_preserves_safety_but_is_not_false_done():
    priority = fallback_priority(TimeoutError("slow"), 0.6, 0.4)
    result = apply_priority_policy(
        route(primary="HEALTH_MEDICAL", risk="HIGH", intents=[("PROMISE_RESULT", 0.9)]),
        priority,
    )
    assert result.effective_policy.run_fact_check is True
    assert result.effective_policy.strict_fact_check is True
    assert result.user_delivery is True
    assert result.channel_publication is False
    delivery = {
        "priority": priority_delivery_outcome(priority),
        "user": "SUCCEEDED",
        "channel": "SUPPRESSED_PRIORITY",
    }
    assert determine_completion_status(delivery) == "PARTIAL"


def test_malformed_publish_flag_uses_failure_path():
    with pytest.raises(ValueError, match="inconsistent"):
        evaluate_priority(score(0.8, publish=False), 0.6, 0.4)


def test_priority_summary_exposes_tier_and_reason():
    text = format_priority_summary(gate(0.2))
    assert "низкий" in text
    assert "fixture signal" in text
