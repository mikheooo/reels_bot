from pathlib import Path

from app.worker.content_router import ContentType
from app.worker.router_evaluation import (
    evaluate_cases,
    evaluate_combined_cases,
    load_cases,
    load_combined_cases,
)

FIXTURE = Path(__file__).parent / "fixtures" / "content_router_eval.jsonl"
COMBINED_FIXTURE = Path(__file__).parent / "fixtures" / "router_priority_eval.jsonl"


def test_offline_eval_covers_every_content_type_and_boundary_cases():
    cases = load_cases(FIXTURE)
    assert len(cases) >= 20
    assert {case.expected_primary for case in cases} == set(ContentType.__args__)
    assert sum("multi-label" in case.tags for case in cases) >= 4
    assert sum(case.expected_risk == "HIGH" for case in cases) >= 4


def test_offline_eval_passes_calibration_and_policy_gates():
    report = evaluate_cases(load_cases(FIXTURE))
    assert report.primary_accuracy >= 0.95
    assert report.label_f1 >= 0.90
    assert report.intent_f1 >= 0.90
    assert report.risk_accuracy >= 0.95
    assert report.high_risk_recall == 1.0
    assert report.policy_accuracy >= 0.95
    assert report.failed_cases == []


def test_invalid_jsonl_reports_line_number(tmp_path):
    fixture = tmp_path / "bad.jsonl"
    fixture.write_text("{}\nnot-json\n", encoding="utf-8")
    try:
        load_cases(fixture)
    except ValueError as exc:
        assert "line 1" in str(exc)
    else:
        raise AssertionError("Invalid fixture must fail closed")


def test_combined_router_priority_replay_passes_policy_gates():
    cases = load_combined_cases(COMBINED_FIXTURE)
    assert len(cases) >= 6
    assert {"high-value", "low-value", "high-risk", "business", "entertainment", "ambiguous"} <= {
        tag for case in cases for tag in case.tags
    }
    report = evaluate_combined_cases(cases)
    assert report.cases == len(cases)
    assert report.policy_accuracy == 1.0
    assert report.risk_floor_violations == 0
    assert report.failed_cases == []


test_content_router_offline_evaluation = test_offline_eval_passes_calibration_and_policy_gates
test_router_priority_offline_evaluation = test_combined_router_priority_replay_passes_policy_gates

