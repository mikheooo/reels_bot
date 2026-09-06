"""Offline multilingual replay for language, Router and priority invariants."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.worker.content_router import (
    IntentScore,
    LabelScore,
    RouterDecision,
    calibrate_decision,
)
from app.worker.language import detect_language_context
from app.worker.priority_policy import apply_priority_policy, evaluate_priority
from app.worker.schemas import PriorityScore


class MultilingualCase(BaseModel):
    id: str
    semantic_group: str
    transcript: str
    expected_language: str
    expected_mixed: bool = False
    expected_source_languages: list[str] = Field(default_factory=list)
    primary_type: str
    risk: str
    intent: str
    priority_overall: float = Field(ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class MultilingualReport(BaseModel):
    cases: int
    semantic_groups: int
    language_accuracy: float
    mixed_accuracy: float
    router_equivalence: float
    priority_band_equivalence: float
    policy_equivalence: float
    output_contract_accuracy: float
    max_score_drift: float
    risk_floor_violations: int
    failed_cases: list[dict]


def load_multilingual_cases(path: str | Path) -> list[MultilingualCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [MultilingualCase(**item) for item in data]


def _route(case: MultilingualCase) -> RouterDecision:
    return calibrate_decision(
        RouterDecision(
            primary_type=case.primary_type,
            labels=[LabelScore(label=case.primary_type, confidence=0.95)],
            intents=[IntentScore(intent=case.intent, confidence=0.9)],
            risk=case.risk,
            summary=case.semantic_group,
        )
    )


def _priority(case: MultilingualCase) -> PriorityScore:
    value = case.priority_overall
    return PriorityScore(
        importance=value,
        virality=value,
        novelty=value,
        views_potential=value,
        audience_value=value,
        overall=value,
        publish=value >= 0.6,
        reasons=["recorded multilingual replay"],
    )


def evaluate_multilingual_cases(
    cases: list[MultilingualCase],
) -> MultilingualReport:
    records = []
    failed: list[dict] = []
    language_hits = 0
    mixed_hits = 0
    output_hits = 0
    risk_floor_violations = 0

    for case in cases:
        language = detect_language_context(case.transcript)
        route = _route(case)
        priority = evaluate_priority(_priority(case), 0.6, 0.4)
        combined = apply_priority_policy(route, priority)
        language_ok = (
            language.detected_language_code == case.expected_language
            and all(code in language.source_languages for code in case.expected_source_languages)
        )
        mixed_ok = language.is_mixed_language == case.expected_mixed
        output_ok = (
            language.analysis_language_code
            == language.user_output_language_code
            == language.channel_output_language_code
            == "ru"
            and language.original_transcript_sha256
            == hashlib.sha256(case.transcript.encode("utf-8")).hexdigest()
        )
        safety_ok = (
            not combined.router_policy.run_fact_check
            or combined.effective_policy.run_fact_check
        ) and (
            not combined.router_policy.strict_fact_check
            or combined.effective_policy.strict_fact_check
        )
        language_hits += language_ok
        mixed_hits += mixed_ok
        output_hits += output_ok
        if not safety_ok:
            risk_floor_violations += 1
        if not (language_ok and mixed_ok and output_ok and safety_ok):
            failed.append(
                {
                    "id": case.id,
                    "language_ok": language_ok,
                    "mixed_ok": mixed_ok,
                    "output_ok": output_ok,
                    "safety_ok": safety_ok,
                }
            )
        records.append((case, route, priority, combined))

    groups: dict[str, list[tuple]] = {}
    for record in records:
        groups.setdefault(record[0].semantic_group, []).append(record)

    comparable = [items for items in groups.values() if len(items) > 1]
    router_hits = 0
    priority_hits = 0
    policy_hits = 0
    drifts = []
    for items in comparable:
        router_values = {(item[1].primary_type, item[1].risk) for item in items}
        priority_values = {item[2].tier for item in items}
        policy_values = {
            (
                json.dumps(item[3].effective_policy.model_dump(), sort_keys=True),
                item[3].user_delivery,
                item[3].channel_publication,
            )
            for item in items
        }
        router_hits += len(router_values) == 1
        priority_hits += len(priority_values) == 1
        policy_hits += len(policy_values) == 1
        scores = [item[0].priority_overall for item in items]
        drifts.append(max(scores) - min(scores))

    count = len(cases)
    group_count = len(comparable)
    return MultilingualReport(
        cases=count,
        semantic_groups=len(groups),
        language_accuracy=language_hits / count if count else 0.0,
        mixed_accuracy=mixed_hits / count if count else 0.0,
        router_equivalence=router_hits / group_count if group_count else 1.0,
        priority_band_equivalence=priority_hits / group_count if group_count else 1.0,
        policy_equivalence=policy_hits / group_count if group_count else 1.0,
        output_contract_accuracy=output_hits / count if count else 0.0,
        max_score_drift=max(drifts, default=0.0),
        risk_floor_violations=risk_floor_violations,
        failed_cases=failed,
    )
