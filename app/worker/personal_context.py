"""Conservative, explicitly configured personal-context evidence collector."""

from __future__ import annotations

import json
import os
from pathlib import Path


def load_personal_context() -> dict:
    """Load only explicit context; never infer installations from model knowledge.

    REELS_PERSONAL_CONTEXT_JSON may contain a small evidence snapshot. Alternatively,
    REELS_PERSONAL_CONTEXT_FILE may point to a JSON file mounted read-only for the
    worker. Without either source the caller must report UNKNOWN.
    """
    raw = os.getenv("REELS_PERSONAL_CONTEXT_JSON", "").strip()
    source = "REELS_PERSONAL_CONTEXT_JSON"
    if not raw:
        path_value = os.getenv("REELS_PERSONAL_CONTEXT_FILE", "").strip()
        if not path_value:
            return {
                "status": "UNKNOWN",
                "evidence": [],
                "reason": "Локальный контекст не подключён.",
            }
        path = Path(path_value)
        source = str(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {
                "status": "UNKNOWN",
                "evidence": [],
                "reason": f"Контекст недоступен: {exc}",
            }
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        return {
            "status": "UNKNOWN",
            "evidence": [],
            "reason": f"Контекст некорректен: {exc}",
        }
    if not isinstance(data, dict):
        return {
            "status": "UNKNOWN",
            "evidence": [],
            "reason": "Контекст должен быть JSON-объектом.",
        }
    return {"status": "AVAILABLE", "source": source, "evidence": data}
