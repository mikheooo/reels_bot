"""Run the Content Router replay gate without APIs, DB, Telegram, or workers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.worker.content_router import ContentType
from app.worker.router_evaluation import evaluate_cases, load_cases

DEFAULT_FIXTURE = Path("tests/fixtures/content_router_eval.jsonl")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    report = evaluate_cases(load_cases(args.fixture))
    payload = report.model_dump()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.json_output:
        args.json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    expected_types = set(ContentType.__args__)
    gates_pass = (
        set(report.type_coverage) == expected_types
        and report.primary_accuracy >= 0.95
        and report.label_f1 >= 0.90
        and report.intent_f1 >= 0.90
        and report.risk_accuracy >= 0.95
        and report.high_risk_recall == 1.0
        and report.policy_accuracy >= 0.95
    )
    return 0 if gates_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
