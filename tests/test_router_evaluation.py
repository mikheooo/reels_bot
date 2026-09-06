from pathlib import Path

from app.worker.content_router import ContentType
from app.worker.router_evaluation import evaluate_cases, load_cases

FIXTURE = Path(__file__).parent / "fixtures" / "content_router_eval.jsonl"


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
