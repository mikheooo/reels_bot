"""Raw diagnostic logging for Gemini generateContent calls.

Appends one self-contained record per attempt to
gemini_rotation_debug.<YYYYMMDD>.log (date-rotated) so future incidents can be
diagnosed from the RAW HTTP status, response/error body and timestamps instead
of a paraphrase. Used by get_raw_transcript and the standalone key probe.
"""
import os
import time
from datetime import datetime, timezone


def _log_path() -> str:
    datestr = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"gemini_rotation_debug.{datestr}.log"


def log_raw(
    alias: str,
    model: str,
    url: str,
    status,
    body,
    req_ts: float,
    end_ts: float,
    phase: str = "generate",
):
    """Append a raw record. status/body may be -1/None for transport failures.

    - alias: 'main' or 'key_N' — NEVER the secret itself.
    - status: HTTP status int, or -1 if the call never got an HTTP response
              (connect/read timeout), or 0 if an unexpected exception.
    - body: raw response body text (truncated to first 4096 chars) or the
            exception text for transport failures.
    - req_ts / end_ts: unix seconds for the exact request start and end.
    """
    if isinstance(body, (bytes, str)):
        body_s = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    else:
        body_s = str(body)
    if len(body_s) > 4096:
        body_s = body_s[:4096] + "...[TRUNCATED]"
    body_s = body_s.replace("\t", "  ").replace("\r", " ").replace("\n", "\n    ")

    req_iso = datetime.fromtimestamp(req_ts, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
    dur_ms = int((end_ts - req_ts) * 1000)

    rec = (
        "=" * 80 + "\n"
        f"{datetime.now(timezone.utc).isoformat()}  REQUEST_END  alias={alias}  "
        f"model={model}  phase={phase}\n"
        f"  url: {url}\n"
        f"  req_ts:  {req_iso}  ({req_ts:.3f})\n"
        f"  end_ts:  {end_iso}  ({end_ts:.3f})  ({end_ts - req_ts:.3f}s / {dur_ms}ms)\n"
        f"  status: {status}\n"
        f"  body:\n    {body_s}\n"
    )
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(rec)
            f.flush()
    except Exception as e:  # do not let logging break the pipeline
        print(f"[gemini_raw_log] write failed: {e}", flush=True)


def key_alias(key: str, main_key: str = "", paid_key: str = "") -> str:
    """Return the non-secret alias for a key: 'paid', 'main' or 'key_N'.

    Never returns the key secret. `paid_key` is detected first so a billing-
    enabled key is clearly labelled in logs to measure real spend.
    """
    if not key:
        return "none"
    if paid_key and key == paid_key.strip():
        return "paid"
    if main_key and key == main_key.strip():
        return "main"
    tail = key[-4:] if len(key) >= 4 else "****"
    return f"key_{tail}"
