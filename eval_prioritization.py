"""Controlled evaluation harness for app.worker.prioritization.score_content.

Standalone script. NOT part of production: no DB, no tasks, no publishing.
Loads .env through app.core.config and runs live Gemini scoring on real
project materials. If credentials are missing or the API errors, the run is
aborted and failures are recorded — results are never fabricated.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from app.core.config import settings
from app.worker.factcheck import get_gemini_keys
from app.worker.prioritization import DEFAULT_PUBLISH_THRESHOLD, score_content

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

MATERIALS = [
    ("temp_transcript.txt (реальный транскрипт ролика)", Path("temp_transcript.txt"), None),
    (
        "tests/fixtures/structured_analysis_regression_input.txt (фикстура регрессии)",
        Path("tests/fixtures/structured_analysis_regression_input.txt"),
        None,
    ),
    ("README.md (заведомо низкоценный контент)", Path("README.md"), None),
    ("dummy.txt (пустой контент 'test')", Path("dummy.txt"), None),
]

SCORE_KEYS = ["importance", "virality", "novelty", "views_potential", "audience_value"]


def print_row(label: str, row: dict) -> None:
    vals = " | ".join(f"{row[k]:.3f}" for k in SCORE_KEYS + ["overall"])
    print(f"{label}\n    {vals} | publish={row['publish']}")


async def run_one(label: str, path: Path, analysis_summary: str | None) -> dict:
    text = path.read_text(encoding="utf-8")
    print(f"\n=== {label} ===")
    print(f"    source: {path} (chars={len(text)}, lines={len(text.splitlines())})")
    result = await score_content(text, analysis_summary=analysis_summary)
    row = {k: getattr(result, k) for k in SCORE_KEYS}
    row["overall"] = result.overall
    row["publish"] = result.publish
    row["reasons"] = list(result.reasons)
    print(f"    overall={row['overall']:.3f} publish={row['publish']}")
    for reason in result.reasons:
        print(f"    reason: {reason}")
    return row


async def main() -> int:
    threshold = getattr(settings, "publish_threshold", DEFAULT_PUBLISH_THRESHOLD)
    print(f"publish_threshold (settings): {threshold}")
    print(f"gemini keys available: {len(get_gemini_keys())}")
    if not get_gemini_keys():
        print("FATAL: no Gemini credentials available — aborting without live results.")
        return 2

    results: list[dict] = []
    for label, path, summary in MATERIALS:
        if not path.exists():
            print(f"\nSKIP {label}: file not found ({path})")
            continue
        try:
            row = await run_one(label, path, summary)
            results.append({"material": label, "source": str(path), **row})
        except Exception as exc:
            print(f"\nERROR on {label}: {type(exc).__name__}: {exc}")
            print("ABORT: live API failed — recording failure, results are NOT fabricated.")
            return 1

    print("\n" + "=" * 78)
    print(f"SUMMARY (threshold = {threshold:.2f})")
    for entry in results:
        print_row(entry["material"], entry)

    out = Path("eval_prioritization_live.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
