"""Offline evaluation runner for multiple output variants contract and semantic invariants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.worker.content_router import (
    IntentScore,
    LabelScore,
    RouterDecision,
    calibrate_decision,
)
from app.worker.language import LanguageContext
from app.worker.output_variants import (
    VARIANT_CONSTRAINTS,
    CanonicalContentResult,
    ClaimSummary,
    OutputVariantsPayload,
    OutputVariantType,
    build_canonical_content_result,
    generate_all_variants,
)
from app.worker.priority_policy import PriorityGateResult
from app.worker.schemas import PriorityScore


class VariantTestCase(BaseModel):
    id: str
    scenario: str
    title: str
    primary_type: str
    risk: str
    what_it_is: str
    why_it_matters: str
    summary: str
    verified_claims: list[str] = Field(default_factory=list)
    disputed_claims: list[str] = Field(default_factory=list)
    actionable_steps: list[str] = Field(default_factory=list)
    source_language: str = "ru"
    expected_x_renderable: bool = True
    mandatory_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    expected_disclaimer: bool = False


class VariantEvaluationReport(BaseModel):
    evaluation_mode: str = "offline_variant_replay"
    contract_version: str = "variants_v1"
    total_cases: int
    total_evaluations: int
    format_validity: float
    length_compliance: float
    mandatory_fact_recall: float
    forbidden_fact_rate: float
    uncertainty_preservation: float
    risk_warning_preservation: float
    language_policy_accuracy: float
    not_renderable_accuracy: float
    risk_warning_violations: int
    invented_forbidden_facts: int
    platform_limit_violations: int
    failed_evaluations: list[dict[str, Any]] = Field(default_factory=list)


def load_variant_cases(path: str | Path) -> list[VariantTestCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [VariantTestCase(**item) for item in data]


def case_to_canonical(case: VariantTestCase) -> CanonicalContentResult:
    """Construct CanonicalContentResult directly from test case."""
    verified = [
        ClaimSummary(statement=stmt, status="подтверждено")
        for stmt in case.verified_claims
    ]
    disputed = [
        ClaimSummary(statement=stmt, status="опровергнуто")
        for stmt in case.disputed_claims
    ]

    critical_disclaimers: list[str] = []
    if case.risk == "HIGH":
        if case.primary_type == "HEALTH_MEDICAL":
            critical_disclaimers.append(
                "⚠️ Внимание: медицинские утверждения требуют консультации с квалифицированным врачом!"
            )
        elif case.primary_type == "FINANCE_INVESTMENT":
            critical_disclaimers.append(
                "⚠️ Внимание: финансовые решения сопряжены с риском потери капитала; не является индивидуальной инвестиционной рекомендацией!"
            )
        else:
            critical_disclaimers.append(
                "⚠️ Внимание: материал повышенного риска; требуется проверка фактов!"
            )

    return CanonicalContentResult(
        title=case.title,
        topic=case.primary_type.replace("_", " ").title(),
        summary=case.summary,
        what_it_is=case.what_it_is,
        why_it_matters=case.why_it_matters,
        key_points=[f"Вывод: {case.summary}"],
        verified_claims=verified,
        disputed_claims=disputed,
        actionable_steps=case.actionable_steps,
        risk_level=case.risk,  # type: ignore[arg-type]
        risk_reasons=[f"Risk category: {case.primary_type}"],
        critical_disclaimers=critical_disclaimers,
        source_language_code=case.source_language,
        analysis_language_code="ru",
        priority_tier="HIGH" if case.risk == "LOW" else "AMBIGUOUS",
        priority_score=0.65 if case.risk == "LOW" else 0.52,
    )


def evaluate_variants(cases: list[VariantTestCase]) -> VariantEvaluationReport:
    """Evaluate semantic invariants across all cases and output variants."""
    total_evals = 0
    valid_format_count = 0
    length_compliant_count = 0
    mandatory_recall_count = 0
    forbidden_absent_count = 0
    uncertainty_preserved_count = 0
    risk_warning_preserved_count = 0
    language_policy_count = 0
    not_renderable_correct_count = 0

    risk_warning_violations = 0
    invented_forbidden_facts = 0
    platform_limit_violations = 0
    failed_evaluations: list[dict[str, Any]] = []

    for case in cases:
        canonical = case_to_canonical(case)
        payload = generate_all_variants(canonical)

        # Evaluate X_POST not_renderable accuracy
        x_variant = payload.variants.get(OutputVariantType.X_POST.value)
        if x_variant:
            if case.expected_x_renderable:
                if x_variant.status == "RENDERED":
                    not_renderable_correct_count += 1
                else:
                    failed_evaluations.append({
                        "case_id": case.id,
                        "variant": "X_POST",
                        "error": f"Expected RENDERED but got {x_variant.status}: {x_variant.failure_reason}",
                    })
            else:
                if x_variant.status == "NOT_RENDERABLE":
                    not_renderable_correct_count += 1
                else:
                    failed_evaluations.append({
                        "case_id": case.id,
                        "variant": "X_POST",
                        "error": f"Expected NOT_RENDERABLE but got {x_variant.status}",
                    })
                    platform_limit_violations += 1

        for variant_type, variant in payload.variants.items():
            total_evals += 1
            constraints = VARIANT_CONSTRAINTS[OutputVariantType(variant_type)]

            # 1. Format validity
            if variant.validation_passed:
                valid_format_count += 1
            else:
                failed_evaluations.append({
                    "case_id": case.id,
                    "variant": variant_type,
                    "error": f"Validation failed: {variant.validation_errors}",
                })

            # 2. Length compliance
            if variant.status == "NOT_RENDERABLE" or (variant.text is not None) and len(variant.text) <= constraints.max_length:
                length_compliant_count += 1
            else:
                platform_limit_violations += 1
                failed_evaluations.append({
                    "case_id": case.id,
                    "variant": variant_type,
                    "error": f"Length {variant.character_count} > {constraints.max_length}",
                })

            # 3. Language policy accuracy
            if variant.language == "ru":
                language_policy_count += 1

            # Checks applicable when text is rendered
            if variant.status == "RENDERED" and variant.text:
                text = variant.text.lower()

                # 4. Mandatory facts
                if case.mandatory_facts:
                    recalled = any(mf.lower() in text for mf in case.mandatory_facts)
                    if recalled:
                        mandatory_recall_count += 1
                    else:
                        failed_evaluations.append({
                            "case_id": case.id,
                            "variant": variant_type,
                            "error": f"Mandatory facts not found: {case.mandatory_facts}",
                        })
                else:
                    mandatory_recall_count += 1

                # 5. Forbidden facts
                if case.forbidden_facts:
                    has_forbidden = any(ff.lower() in text for ff in case.forbidden_facts)
                    if not has_forbidden:
                        forbidden_absent_count += 1
                    else:
                        invented_forbidden_facts += 1
                        failed_evaluations.append({
                            "case_id": case.id,
                            "variant": variant_type,
                            "error": f"Forbidden fact present: {case.forbidden_facts}",
                        })
                else:
                    forbidden_absent_count += 1

                # 6. Uncertainty preservation
                if case.disputed_claims:
                    # Must not present disputed claims as confirmed
                    has_contradiction_signal = any(
                        w in text for w in ["опроверг", "ложно", "не подтвержд", "миф", "фейк", "внимание", "сомнит"]
                    )
                    if has_contradiction_signal:
                        uncertainty_preserved_count += 1
                    else:
                        failed_evaluations.append({
                            "case_id": case.id,
                            "variant": variant_type,
                            "error": "Disputed claim mentioned without contradiction context",
                        })
                else:
                    uncertainty_preserved_count += 1

                # 7. Risk warning preservation
                if case.expected_disclaimer:
                    warning_words = ["⚠️", "внимание", "риск", "врач", "консультац", "противопоказан", "потер", "не является"]
                    has_warning = any(w in text for w in warning_words)
                    if has_warning:
                        risk_warning_preserved_count += 1
                    else:
                        risk_warning_violations += 1
                        failed_evaluations.append({
                            "case_id": case.id,
                            "variant": variant_type,
                            "error": "Required high-risk disclaimer missing",
                        })
                else:
                    risk_warning_preserved_count += 1
            else:
                # When NOT_RENDERABLE, invariants are naturally preserved
                mandatory_recall_count += 1
                forbidden_absent_count += 1
                uncertainty_preserved_count += 1
                risk_warning_preserved_count += 1

    return VariantEvaluationReport(
        total_cases=len(cases),
        total_evaluations=total_evals,
        format_validity=valid_format_count / total_evals if total_evals else 0.0,
        length_compliance=length_compliant_count / total_evals if total_evals else 0.0,
        mandatory_fact_recall=mandatory_recall_count / total_evals if total_evals else 0.0,
        forbidden_fact_rate=invented_forbidden_facts / total_evals if total_evals else 0.0,
        uncertainty_preservation=uncertainty_preserved_count / total_evals if total_evals else 0.0,
        risk_warning_preservation=risk_warning_preserved_count / total_evals if total_evals else 0.0,
        language_policy_accuracy=language_policy_count / total_evals if total_evals else 0.0,
        not_renderable_accuracy=not_renderable_correct_count / len(cases) if cases else 0.0,
        risk_warning_violations=risk_warning_violations,
        invented_forbidden_facts=invented_forbidden_facts,
        platform_limit_violations=platform_limit_violations,
        failed_evaluations=failed_evaluations,
    )
