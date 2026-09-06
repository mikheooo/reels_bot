import json

import pytest

from app.worker.content_router import (
    RouterDecision,
    fallback_route,
    policy_for,
    route_content,
)


def decision(primary, risk="MEDIUM", intents=None):
    return RouterDecision(
        primary_type=primary,
        labels=[{"label": primary, "confidence": 0.91}],
        intents=[
            {"intent": value, "confidence": 0.8} for value in (intents or ["INFORM"])
        ],
        risk=risk,
        risk_reasons=[],
        summary="fixture",
    )


@pytest.mark.parametrize(
    "primary",
    [
        "SOFTWARE_TOOL",
        "AI_SKILL_PLUGIN",
        "JOB_OPPORTUNITY",
        "HEALTH_MEDICAL",
        "FITNESS",
        "FINANCE_INVESTMENT",
        "BUSINESS_IDEA",
        "PRODUCT",
        "TRAVEL_PLACE",
        "HOW_TO",
        "NEWS_CLAIM",
        "SCIENCE_EDUCATION",
        "OPINION",
        "ENTERTAINMENT",
    ],
)
def test_all_required_content_types_are_accepted(primary):
    assert decision(primary).primary_type == primary


def test_multilabel_scores_are_sorted_and_primary_is_present():
    routed = RouterDecision(
        primary_type="AI_SKILL_PLUGIN",
        labels=[
            {"label": "SOFTWARE_TOOL", "confidence": 0.7},
            {"label": "AI_SKILL_PLUGIN", "confidence": 0.95},
        ],
        intents=[
            {"intent": "SELL", "confidence": 0.4},
            {"intent": "TEACH", "confidence": 0.9},
        ],
        risk="LOW",
        summary="skills",
    )
    assert [item.label for item in routed.labels] == [
        "AI_SKILL_PLUGIN",
        "SOFTWARE_TOOL",
    ]
    assert [item.intent for item in routed.intents] == ["TEACH", "SELL"]


def test_low_risk_entertainment_skips_expensive_downstream_checks():
    policy = policy_for(decision("ENTERTAINMENT", risk="LOW", intents=["ENTERTAIN"]))
    assert policy.run_fact_check is False
    assert policy.run_business_check is False
    assert policy.create_tasks is False


def test_high_risk_health_requires_strict_fact_check_and_no_task():
    policy = policy_for(
        decision("HEALTH_MEDICAL", risk="HIGH", intents=["PROMISE_RESULT"])
    )
    assert policy.run_fact_check is True
    assert policy.strict_fact_check is True
    assert policy.run_business_check is False
    assert policy.create_tasks is False


def test_job_route_enables_personal_relevance_and_fact_check():
    policy = policy_for(
        decision("JOB_OPPORTUNITY", risk="MEDIUM", intents=["RECOMMEND"])
    )
    assert policy.run_personal_relevance is True
    assert policy.run_fact_check is True
    assert policy.run_business_check is False


def test_finance_enables_business_and_fact_layers():
    policy = policy_for(
        decision("FINANCE_INVESTMENT", risk="HIGH", intents=["PROMISE_RESULT"])
    )
    assert policy.run_business_check is True
    assert policy.run_fact_check is True


def test_router_failure_preserves_legacy_downstream_capabilities():
    policy = policy_for(fallback_route("offline"))
    assert policy.run_fact_check is True
    assert policy.run_business_check is True
    assert policy.include_technical_details is True


@pytest.mark.asyncio
async def test_route_content_parses_multilabel_json(monkeypatch):
    response = {
        "primary_type": "AI_SKILL_PLUGIN",
        "labels": [
            {"label": "AI_SKILL_PLUGIN", "confidence": 0.97},
            {"label": "SOFTWARE_TOOL", "confidence": 0.72},
        ],
        "intents": [{"intent": "RECOMMEND", "confidence": 0.84}],
        "risk": "LOW",
        "risk_reasons": [],
        "summary": "Набор методик для coding-agent.",
    }

    async def fake_call(payload):
        prompt = payload["contents"][0]["parts"][0]["text"]
        assert "Superpowers" in prompt
        assert "не новая AI-модель" in prompt
        return {
            "candidates": [{"content": {"parts": [{"text": json.dumps(response)}]}}]
        }

    monkeypatch.setattr("app.worker.content_router.call_gemini_api", fake_call)
    routed = await route_content("Superpowers provides skills for coding agents")
    assert routed.primary_type == "AI_SKILL_PLUGIN"
    assert len(routed.labels) == 2
