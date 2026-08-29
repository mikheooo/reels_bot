"""Visual evidence extraction from video frames.

Provides frame extraction and vision analysis to produce structured visual
evidence that complements the transcript in structured analysis.

Pipeline:
    video → ffprobe duration → ffmpeg uniform frame sampling → base64 JPEG
    → Gemini vision (inline_data) → structured VisualEvidence list

Fallback:
    If any step fails, returns None — caller falls back to transcript-only.
"""
import asyncio
import base64
import json
import logging
import os

from app.worker.factcheck import call_gemini_api

logger = logging.getLogger(__name__)

MAX_VISUAL_FRAMES = 12
HARD_FRAME_CAP = 16
VISUAL_MAX_RETRIES = 3
FRAME_WIDTH = 640
JPEG_QUALITY = 2


class RateLimitError(Exception):
    """Raised when all Gemini API keys are rate-limited for visual analysis."""
    pass

VISUAL_ANALYSIS_PROMPT = """Ты — визуальный анализатор кадров из видео. Проанализируй предоставленные кадры из Reels/Shorts видео.

Кадры переданы в хронологическом порядке. Timestamps кадров: {timestamps}

Для каждого кадра определи:
1. timestamp: примерное время кадра (вещественное число в секундах).
2. description: что показано на экране (интерфейс, код, терминал, GitHub, UI, человек, графика, и т.д.). Будь конкретен: если виден GitHub репозиторий, укажи это. Если виден терминал с командами, опиши. Если показан UI инструмента, опиши элементы.
3. text_read: любой читаемый текст — URL, название репозитория (owner/repo), название проекта, команды, заголовки, числа, промпты. Если текст не читается или отсутствует — null.
4. confidence: уверенность чтения текста:
   - "high": текст чётко читается, названия и URL разборчивы
   - "medium": часть текста читается, но есть неуверенность
   - "low": текст размыт, нечитаем или отсутствует
5. tool_name: название конкретного инструмента/сервиса, если оно читается на экране. Если не читается — null.
6. project_name: название проекта/репозитория (owner/repo), если видно. Если не читается — null.
7. repository: полный путь к репозиторию (owner/repo), если виден GitHub/GitLab. Не угадывай — только если явно читается. Если не видно — null.
8. visible_url: любой видимый URL (https://...). Не угадывай URL — только если явно читается на экране. Если не видно — null.

ПРАВИЛА:
- Не придумывай текст, которого нет на кадре.
- Если название репозитория читается частично, отметь confidence как "medium" и укажи только прочитанную часть.
- Если текст не читается — text_read: null, confidence: "low".
- Не добавляй информацию из внешних знаний — только то, что реально видно на кадре.
- Если на кадре показан GitHub, попытайся прочитать owner/repo, название проекта, звёзды, описание README.
- Если на кадре показан терминал, попытайся прочитать команды и вывод.
- Если на кадре показан UI инструмента, опиши элементы интерфейса и любые видимые настройки.
- tool_name, project_name, repository, visible_url — только если явно читается. Не угадывай. Если не читается или отсутствует — null.
"""

VISUAL_ANALYSIS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "timestamp": {"type": "NUMBER"},
            "description": {"type": "STRING"},
            "text_read": {"type": "STRING", "nullable": True},
            "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
            "tool_name": {"type": "STRING", "nullable": True},
            "project_name": {"type": "STRING", "nullable": True},
            "repository": {"type": "STRING", "nullable": True},
            "visible_url": {"type": "STRING", "nullable": True},
        },
        "required": ["timestamp", "description", "confidence"],
    },
}


async def get_video_duration(file_path: str) -> float:
    """Get video duration in seconds via ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        info = json.loads(stdout.decode())
        return float(info["format"]["duration"])
    except Exception as e:
        logger.warning(f"ffprobe duration failed: {e}")
        return 0.0


async def extract_keyframes(video_path: str, max_frames: int = MAX_VISUAL_FRAMES) -> list[dict]:
    """Extract keyframes from video as base64-encoded JPEGs.

    Uses uniform sampling: calculates an interval based on duration,
    then extracts up to ``max_frames`` frames at that interval.
    Enforces ``HARD_FRAME_CAP`` as an absolute upper bound.

    Returns:
        List of dicts with ``timestamp`` (float seconds) and ``jpeg_b64`` (str).
    """
    effective_cap = min(max_frames, HARD_FRAME_CAP)

    duration = await get_video_duration(video_path)
    if duration <= 0:
        logger.warning("Cannot extract frames: video duration unknown")
        return []

    usable_duration = max(1.0, duration - 0.5)
    interval = max(1.0, usable_duration / effective_cap)
    fps = 1.0 / interval

    tmp_dir = os.path.dirname(video_path) or os.getcwd()
    frame_pattern = os.path.join(tmp_dir, "vf_frame_%03d.jpg")

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps={fps:.4f},scale={FRAME_WIDTH}:-2",
        "-frames:v", str(effective_cap),
        "-q:v", str(JPEG_QUALITY),
        frame_pattern,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(f"ffmpeg frame extraction failed: {stderr.decode()[:500]}")
        return []

    frames: list[dict] = []
    frame_files = sorted(
        f for f in os.listdir(tmp_dir)
        if f.startswith("vf_frame_") and f.endswith(".jpg")
    )

    for i, fname in enumerate(frame_files):
        fpath = os.path.join(tmp_dir, fname)
        try:
            with open(fpath, "rb") as f:
                jpeg_bytes = f.read()
            jpeg_b64 = base64.b64encode(jpeg_bytes).decode("ascii")

            ts = i * interval
            frames.append({"timestamp": ts, "jpeg_b64": jpeg_b64})
        except Exception as e:
            logger.warning(f"Failed to read frame {fname}: {e}")
        finally:
            try:
                os.unlink(fpath)
            except OSError:
                pass

    logger.info(
        f"Extracted {len(frames)} keyframes "
        f"(duration={duration:.1f}s, interval={interval:.1f}s)"
    )
    return frames


async def analyze_frames_with_vision(frames: list[dict]) -> list[dict]:
    """Send frames to Gemini vision for structured analysis.

    Constructs a single ``generateContent`` call with ``inline_data`` image
    parts (one per frame) alongside the analysis prompt.

    Retries up to ``VISUAL_MAX_RETRIES`` times on rate-limit errors, without
    modifying the shared ``call_gemini_api`` (which handles key rotation
    internally).  Non-rate-limit errors are not retried.

    Returns:
        List of dicts with ``timestamp`` (float), ``description``, ``text_read``,
        ``confidence``, and nullable ``tool_name``, ``project_name``,
        ``repository``, ``visible_url``. Returns ``[]`` on any failure.
    """
    if not frames:
        return []

    def _fmt_ts(seconds: float) -> str:
        mm = int(seconds) // 60
        ss = seconds - mm * 60
        return f"{mm:02d}:{ss:05.2f}"

    timestamps = ", ".join(_fmt_ts(f["timestamp"]) for f in frames)
    prompt = VISUAL_ANALYSIS_PROMPT.format(timestamps=timestamps)

    parts: list[dict] = [{"text": prompt}]
    for f in frames:
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": f["jpeg_b64"],
                }
            }
        )

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": VISUAL_ANALYSIS_SCHEMA,
        },
    }

    last_exc: Exception | None = None
    for attempt in range(VISUAL_MAX_RETRIES):
        try:
            # Keep Vision's own retry budget bounded. One shared API call
            # still rotates through configured Gemini keys, but it must not
            # enter the shared multi-round backoff loop for this payload.
            resp_json = await call_gemini_api(payload, max_rounds=1)
            candidates = resp_json.get("candidates", [])
            if not candidates:
                return []
            text_resp = candidates[0]["content"]["parts"][0]["text"]
            data = json.loads(text_resp)

            results: list[dict] = []
            for i, item in enumerate(data):
                if i < len(frames):
                    item["timestamp"] = frames[i]["timestamp"]
                results.append(item)

            return results
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            is_rate_limit = any(kw in err_str for kw in (
                "429", "quota", "exhausted", "rate limit",
                "resource_exhausted", "503", "high demand", "unavailable",
            ))
            if is_rate_limit:
                if attempt < VISUAL_MAX_RETRIES - 1:
                    logger.warning(
                        f"Vision API rate limited (attempt {attempt + 1}/{VISUAL_MAX_RETRIES}): {e}"
                    )
                    continue
                raise RateLimitError(f"visual retries exhausted: {e}") from e
            logger.error(f"Vision analysis API call failed: {e}")
            return []

    raise last_exc or RateLimitError("visual retries exhausted")


def format_visual_evidence(evidence_list: list[dict]) -> str | dict:
    """Format visual evidence for inclusion in structured analysis prompt.

    Returns a dict with ``status`` and ``evidence`` keys, or a plain string
    for backward compatibility with the prompt template.

    Each evidence item is rendered as:
        [MM:SS] description | Текст: <text_read> | Уверенность: <confidence>
    """
    if not evidence_list:
        return {"status": "unavailable", "evidence": []}

    parts: list[str] = []
    evidence_items: list[dict] = []
    for item in evidence_list:
        ts = item.get("timestamp", 0.0)
        if isinstance(ts, (int, float)):
            mm = int(ts) // 60
            ss = ts - mm * 60
            ts_str = f"{mm:02d}:{ss:05.2f}"
        else:
            ts_str = str(ts)
        desc = item.get("description", "не описано")
        text_read = item.get("text_read")
        confidence = item.get("confidence", "medium")

        line = f"[{ts_str}] {desc}"
        if text_read:
            line += f" | Текст: {text_read}"
        line += f" | Уверенность: {confidence}"
        parts.append(line)
        evidence_items.append(item)

    return {
        "status": "available",
        "evidence": evidence_items,
        "formatted": "\n".join(parts),
    }


async def extract_visual_evidence(video_path: str) -> dict | None:
    """Extract visual evidence from video frames.

    Main entry point. Returns a dict with ``status`` and ``formatted`` keys,
    or ``None`` if visual analysis is unavailable (caller falls back to
    transcript-only).

    Application-level status values (not from the LLM):
        - "available": frames extracted and evidence produced
        - "frame_extraction_failed": no frames could be extracted
        - "empty_response": API returned no usable evidence
        - "api_rate_limited": all retry attempts exhausted due to rate limits
    """
    try:
        frames = await extract_keyframes(video_path)
        if not frames:
            logger.warning("No frames extracted from video")
            return {"status": "frame_extraction_failed", "formatted": None}

        logger.info(f"Extracted {len(frames)} keyframes for vision analysis")

        evidence = await analyze_frames_with_vision(frames)
        if not evidence:
            logger.warning("Vision analysis returned no evidence")
            return {"status": "empty_response", "formatted": None}

        formatted = format_visual_evidence(evidence)
        if isinstance(formatted, dict):
            result_str = formatted.get("formatted", "")
        else:
            result_str = formatted
        logger.info(f"Visual evidence formatted: {len(evidence)} items")
        if result_str:
            return {"status": "available", "formatted": result_str}
        return {"status": "empty_response", "formatted": None}
    except RateLimitError:
        logger.warning("Visual evidence extraction: all retries rate-limited")
        return {"status": "api_rate_limited", "formatted": None}
    except Exception as e:
        logger.error(f"Visual evidence extraction failed: {e}")
        return {"status": "unavailable", "formatted": None}
