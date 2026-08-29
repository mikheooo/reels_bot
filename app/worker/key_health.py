"""Detect placeholder / dummy API keys before they silently fail the pipeline.

Checks the credential env vars for values that look like stubs
(*_dumm*, *_test*, *_fake*, *_your_*, *_example*, *_placeholder*, too-short).
Logs a WARNING into the MAIN worker log (not only the raw gemini log) so a
fake key can never quietly degrade the pipeline to DONE without notice.

Runs automatically on worker startup; also importable / runnable manually:
  python -m app.worker.key_health
"""
import logging
import os

logger = logging.getLogger(__name__)

# values that are clearly placeholders (case-insensitive substring match)
PLACEHOLDER_SUBSTR = [
    "dumm", "test", "fake", "your_", "eg.", "example", "placeholder",
    "sample", "xxxx", "0000", "change_me", "insert_", "left_here",
]

# minimum plausible length per credential (provider format-agnostic lower bound)
MIN_LEN = {
    "GEMINI_API_KEY": 20,      # AIza... ~39, AQ... new format longer
    "GEMINI_PAID_KEY": 20,
    "GEMINI_API_KEY_1": 20,
    "GEMINI_API_KEY_2": 20,
    "GEMINI_API_KEY_3": 20,
    "GEMINI_API_KEY_4": 20,
    "EXA_API_KEY": 20,          # exa_ + ~30
    "JINA_API_KEY": 20,         # jina long token, no strict prefix
}

def check_keys(env=None) -> list[tuple[str, str]]:
    """Return list of (env_var, reason) for keys that look like placeholders.

    (var, reason) pairs; empty list means all configured keys look healthy.
    """
    env = env if env is not None else os.environ
    alerts = []
    for var, min_len in MIN_LEN.items():
        if var.startswith("GEMINI_API_KEY_") and var not in ("GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"):
            continue
        val = (env.get(var) or "").strip()
        if not val:
            alerts.append((var, "MISSING/empty"))
            continue
        low = val.lower()
        hit = [p for p in PLACEHOLDER_SUBSTR if p in low]
        if hit:
            alerts.append((var, f"looks like a placeholder ({hit})"))
        elif len(val) < min_len:
            alerts.append((var, f"suspiciously short ({len(val)} chars < {min_len})"))
    return alerts


def log_key_health(env=None) -> list[tuple[str, str]]:
    alerts = check_keys(env)
    if alerts:
        for var, reason in alerts:
            logger.warning("KEY_HEALTH: %s %s — check .env; fake key may silently degrade the pipeline", var, reason)
        logger.warning("KEY_HEALTH: %d suspicious API key(s) detected", len(alerts))
    else:
        logger.info("KEY_HEALTH: all API keys look healthy")
    return alerts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    alerts = log_key_health()
    print("ALERTS:", alerts if alerts else "NONE — all keys look healthy")
