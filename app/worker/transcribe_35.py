"""Primary transcription path: Gemini 3.5 Transcribe (verbatim).

Dedicated speech-to-text model. Verbatim output preserves repetitions and
false starts; the result is the canonical transcript. Smart cleanup is
deliberately NOT used here — readability transforms belong to reporting,
never to the source artifact.

Deliberately uses the SAME stack as the legacy path (google-generativeai
0.8.3 for the Files API + raw httpx generateContent), so no dependency
upgrade is required. google-genai>=2.22 would satisfy pydantic>=2.12,
which conflicts with the pinned pydantic 2.9.2 — hence raw HTTP.

Separation of concerns:
- ffmpeg audio extraction lives here, not in the orchestrator;
- request/response handling lives here, not in the fallback;
- completeness policy lives here as a pure function.
"""

import asyncio
import logging
import os
import random
import re

import httpx

logger = logging.getLogger(__name__)

MODEL_35 = "gemini-3.5-transcribe"

VERBATIM_PROMPT = (
    "Transcribe this audio verbatim. Preserve every spoken word, "
    "repetition and false start. Do not summarize."
)

MIN_CHARS_PER_MINUTE = 400
RETRY_SLEEP_SECONDS = 45
GEN_TIMEOUT = 600.0


async def extract_audio_mp3(video_path: str, mp3_path: str) -> None:
    """Video -> 16kHz mono mp3 suitable for the transcription model."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error",
        "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
        mp3_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {stderr.decode()[:300]}")


def assess_transcript_completeness(text: str, duration_s: float) -> str:
    """Return OK / SUSPICIOUS / FAILED.

    FAILED  = empty / whitespace / clearly malformed.
    SUSPICIOUS = unusually short for the audio duration or abruptly cut.
    OK = no sign of a problem.  Duration 0 (unknown) trusts non-empty text.
    """
    if not text or not text.strip():
        return "FAILED"
    stripped = text.strip()
    if len(stripped) < 5:
        return "FAILED"
    if not duration_s or duration_s <= 0:
        return "OK"  # unknown duration: trust non-empty
    expected_min = MIN_CHARS_PER_MINUTE * (duration_s / 60.0)
    if len(stripped) < expected_min * 0.6:
        return "SUSPICIOUS" if len(stripped) >= 20 else "FAILED"
    if not re.search(r"[.!?\"»…)\]]\s*$", stripped) and len(stripped) < expected_min:
        return "SUSPICIOUS"
    if len(stripped) < 20:
        return "FAILED"
    return "OK"


def is_transcript_complete(text: str, duration_s: float) -> bool:
    """Legacy boolean gate: True only on OK (strict, for old callers)."""
    return assess_transcript_completeness(text, duration_s) == "OK"


def extract_transcription_text(data: dict) -> str:
    """Join audioTranscription parts of a generateContent JSON response."""
    texts: list[str] = []
    for cand in data.get("candidates", []):
        parts = (cand.get("content") or {}).get("parts", [])
        for part in parts:
            at = part.get("audioTranscription") or {}
            if at.get("text"):
                texts.append(at["text"])
    return "".join(texts)


def _transcribe_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    """Honour Retry-After if present, else exponential backoff + jitter, bounded."""
    if resp is not None:
        ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        if ra:
            try:
                return min(float(ra.strip().split()[0]), 120.0)
            except Exception:
                pass
    base = RETRY_SLEEP_SECONDS * (1.5 ** attempt)
    return min(base + random.uniform(0, 10), 120.0)


async def transcribe_with_gemini_35(
    video_path: str,
    api_keys: list[str],
    tmp_dir: str = "/tmp",
) -> str:
    """Verbatim transcription. Raises on total failure (caller falls back)."""
    import google.generativeai as genai

    mp3_path = os.path.join(tmp_dir, f"t35_{os.path.basename(video_path)}.mp3")
    await extract_audio_mp3(video_path, mp3_path)
    last_error: Exception | None = None
    try:
        for idx, key in enumerate(api_keys):
            if not key:
                continue
            genai.configure(api_key=key, transport="rest")
            audio_file = None
            try:
                audio_file = await asyncio.to_thread(genai.upload_file, path=mp3_path)
                mime = getattr(audio_file, "mime_type", None) or "audio/mpeg"
                uri = audio_file.uri or audio_file.name
                payload = {
                    "contents": [{
                        "role": "user",
                        "parts": [
                            {"file_data": {"file_uri": uri, "mime_type": mime}},
                            {"text": VERBATIM_PROMPT},
                        ],
                    }]
                }
                async with httpx.AsyncClient(timeout=GEN_TIMEOUT) as client:
                    resp = await client.post(
                        _transcribe_url(MODEL_35),
                        headers={"x-goog-api-key": key, "Accept": "application/json"},
                        json=payload,
                    )
                if resp.status_code == 200:
                    text = extract_transcription_text(resp.json())
                    if text and text.strip():
                        return text
                    last_error = RuntimeError("3.5 Transcribe returned empty text")
                    continue
                if resp.status_code in (429, 500, 502, 503, 504):
                    delay = _retry_delay(resp, idx)
                    logger.warning(f"3.5 Transcribe {resp.status_code}, retry in {delay:.0f}s, next key.")
                    last_error = RuntimeError(f"3.5 HTTP {resp.status_code}")
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"3.5 HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                if "429" in msg or "quota" in msg or "503" in msg or "unavailable" in msg:
                    delay = RETRY_SLEEP_SECONDS + random.uniform(0, 10)
                    logger.warning(f"3.5 Transcribe busy/rate-limited, next key in {delay:.0f}s: {e}")
                    await asyncio.sleep(delay)
                    continue
                logger.warning(f"3.5 Transcribe attempt failed: {e}")
                continue
            finally:
                try:
                    if audio_file is not None:
                        await asyncio.to_thread(genai.delete_file, audio_file.name)
                except Exception:
                    pass
    finally:
        try:
            os.remove(mp3_path)
        except Exception:
            pass
    raise last_error or RuntimeError("3.5 Transcribe failed with no keys tried")
