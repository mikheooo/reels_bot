from pathlib import Path

from app.worker.multilingual_evaluation import (
    evaluate_multilingual_cases,
    load_multilingual_cases,
)

FIXTURE = Path(__file__).parent / "fixtures" / "multilingual_eval.json"


def test_multilingual_replay_gates():
    cases = load_multilingual_cases(FIXTURE)
    assert len(cases) >= 13
    report = evaluate_multilingual_cases(cases)
    assert report.language_accuracy == 1.0
    assert report.mixed_accuracy == 1.0
    assert report.router_equivalence == 1.0
    assert report.priority_band_equivalence == 1.0
    assert report.policy_equivalence == 1.0
    assert report.output_contract_accuracy == 1.0
    assert report.max_score_drift <= 0.05
    assert report.risk_floor_violations == 0
    assert report.failed_cases == []
