"""Offline replay evaluation for Content Router calibration and policy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.worker.content_router import (
    INTENT_POLICY_THRESHOLD,
    PRIMARY_OVERRIDE_MARGIN,
    RISK_INTENT_THRESHOLD,
    SECONDARY_LABEL_THRESHOLD,
    AuthorIntent,
    ContentType,
    RiskLevel,
    RouterDecision,
    calibrate_decision,
    policy_for,
)


class ExpectedPolicy(BaseModel):
    run_fact_check: bool
    strict_fact_check: bool
    run_business_check: bool
    include_technical_details: bool
    run_personal_relevance: bool
    create_tasks: bool


class RouterEvalCase(BaseModel):
    id: str
    transcript: str
    expected_primary: ContentType
    expected_labels: list[ContentType]
    expected_intents: list[AuthorIntent]
    expected_risk: RiskLevel
    expected_policy: ExpectedPolicy
    recorded_output: dict = Field(alias="model_output")
    tags: list[str] = Field(default_factory=list)


class RouterEvalReport(BaseModel):
    evaluation_mode: Literal["offline_replay"] = "offline_replay"
    calibration_version: Literal["v1"] = "v1"
    thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "secondary_label": SECONDARY_LABEL_THRESHOLD,
            "primary_override_margin": PRIMARY_OVERRIDE_MARGIN,
            "intent_policy": INTENT_POLICY_THRESHOLD,
            "risk_intent": RISK_INTENT_THRESHOLD,
        }
    )
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Measures deterministic schema, calibration and policy replay; not live model classification quality."
        ]
    )
    cases: int
    type_coverage: list[str]
    primary_accuracy: float
    label_precision: float
    label_recall: float
    label_f1: float
    intent_precision: float
    intent_recall: float
    intent_f1: float
    risk_accuracy: float
    high_risk_recall: float
    policy_accuracy: float
    failed_cases: list[dict]


def load_cases(path: str | Path) -> list[RouterEvalCase]:
    rows = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            rows.append(RouterEvalCase(**json.loads(line)))
        except Exception as exc:
            raise ValueError(
                f"Invalid router fixture at line {line_number}: {exc}"
            ) from exc
    return rows


def _prf(
    true_sets: list[set[str]], predicted_sets: list[set[str]]
) -> tuple[float, float, float]:
    true_positive = sum(
        len(expected & predicted)
        for expected, predicted in zip(true_sets, predicted_sets)
    )
    false_positive = sum(
        len(predicted - expected)
        for expected, predicted in zip(true_sets, predicted_sets)
    )
    false_negative = sum(
        len(expected - predicted)
        for expected, predicted in zip(true_sets, predicted_sets)
    )
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 1.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 1.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def evaluate_cases(cases: list[RouterEvalCase]) -> RouterEvalReport:
    label_expected: list[set[str]] = []
    label_actual: list[set[str]] = []
    intent_expected: list[set[str]] = []
    intent_actual: list[set[str]] = []
    primary_hits = 0
    risk_hits = 0
    policy_hits = 0
    high_expected = 0
    high_hits = 0
    failed_cases: list[dict] = []

    for case in cases:
        decision = calibrate_decision(RouterDecision(**case.recorded_output))
        policy = policy_for(decision)
        actual_labels = {item.label for item in decision.labels}
        actual_intents = {item.intent for item in decision.intents}
        expected_labels = set(case.expected_labels)
        expected_intents = set(case.expected_intents)
        policy_matches = policy.model_dump() == case.expected_policy.model_dump()

        primary_hits += decision.primary_type == case.expected_primary
        risk_hits += decision.risk == case.expected_risk
        policy_hits += policy_matches
        if case.expected_risk == "HIGH":
            high_expected += 1
            high_hits += decision.risk == "HIGH"
        label_expected.append(expected_labels)
        label_actual.append(actual_labels)
        intent_expected.append(expected_intents)
        intent_actual.append(actual_intents)

        if not (
            decision.primary_type == case.expected_primary
            and actual_labels == expected_labels
            and actual_intents == expected_intents
            and decision.risk == case.expected_risk
            and policy_matches
        ):
            failed_cases.append(
                {
                    "id": case.id,
                    "primary": [case.expected_primary, decision.primary_type],
                    "labels": [sorted(expected_labels), sorted(actual_labels)],
                    "intents": [sorted(expected_intents), sorted(actual_intents)],
                    "risk": [case.expected_risk, decision.risk],
                    "policy_match": policy_matches,
                    "calibration_notes": decision.calibration_notes,
                }
            )

    count = len(cases)
    label_precision, label_recall, label_f1 = _prf(label_expected, label_actual)
    intent_precision, intent_recall, intent_f1 = _prf(intent_expected, intent_actual)
    return RouterEvalReport(
        cases=count,
        type_coverage=sorted({case.expected_primary for case in cases}),
        primary_accuracy=primary_hits / count if count else 0.0,
        label_precision=label_precision,
        label_recall=label_recall,
        label_f1=label_f1,
        intent_precision=intent_precision,
        intent_recall=intent_recall,
        intent_f1=intent_f1,
        risk_accuracy=risk_hits / count if count else 0.0,
        high_risk_recall=high_hits / high_expected if high_expected else 1.0,
        policy_accuracy=policy_hits / count if count else 0.0,
        failed_cases=failed_cases,
    )
