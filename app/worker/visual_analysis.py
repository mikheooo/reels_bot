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

MAX_FRAMES = 8
FRAME_WIDTH = 640
JPEG_QUALITY = 2

VISUAL_ANALYSIS_PROMPT = """Ты — визуальный анализатор кадров из видео. Проанализируй предоставленные кадры из Reels/Shorts видео.

Кадры переданы в хронологическом порядке. Timestamps кадров: {timestamps}

Для каждого кадра определи:
1. timestamp: примерное время кадра из списка выше.
2. description: что показано на экране (интерфейс, код, терминал, GitHub, UI, человек, графика, и т.д.). Будь конкретен: если виден GitHub репозиторий, укажи это. Если виден терминал с командами, опиши. Если показан UI инструмента, опиши элементы.
3. text_read: любой читаемый текст — URL, название репозитория (owner/repo), название проекта, команды, заголовки, числа, промпты. Если текст не читается или отсутствует — null.
4. confidence: уверенность чтения текста:
   - "high": текст чётко читается, названия и URL разборчивы
   - "medium": часть текста читается, но есть неуверенность
   - "low": текст размыт, нечитаем или отсутствует

ПРАВИЛА:
- Не придумывай текст, которого нет на кадре.
- Если название репозитория читается частично, отметь confidence как "medium" и укажи только прочитанную часть.
- Если текст не читается — text_read: null, confidence: "low".
- Не добавляй информацию из внешних знаний — только то, что реально видно на кадре.
- Если на кадре показан GitHub, попытайся прочитать owner/repo, название проекта, звёзды, описание README.
- Если на кадре показан терминал, попытайся прочитать команды и вывод.
- Если на кадре показан UI инструмента, опиши элементы интерфейса и любые видимые настройки.
"""

VISUAL_ANALYSIS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "timestamp": {"type": "STRING"},
            "description": {"type": "STRING"},
            "text_read": {"type": "STRING", "nullable": True},
            "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
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


async def extract_keyframes(video_path: str, max_frames: int = MAX_FRAMES) -> list[dict]:
    """Extract keyframes from video as base64-encoded JPEGs.

    Uses uniform sampling: calculates an interval based on duration,
    then extracts up to ``max_frames`` frames at that interval.

    Returns:
        List of dicts with ``timestamp`` (str MM:SS) and ``jpeg_b64`` (str).
    """
    duration = await get_video_duration(video_path)
    if duration <= 0:
        logger.warning("Cannot extract frames: video duration unknown")
        return []

    # Skip first 0.5 s and last 0.5 s
    usable_duration = max(1.0, duration - 1.0)
    interval = max(2.0, usable_duration / max_frames)
    fps = 1.0 / interval

    tmp_dir = os.path.dirname(video_path) or os.getcwd()
    frame_pattern = os.path.join(tmp_dir, "vf_frame_%03d.jpg")

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", f"fps={fps:.4f},scale={FRAME_WIDTH}:-2",
        "-frames:v", str(max_frames),
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

            # Calculate timestamp for this frame
            ts = 0.5 + i * interval
            mm = int(ts // 60)
            ss = int(ts % 60)
            timestamp = f"{mm:02d}:{ss:02d}"

            frames.append({"timestamp": timestamp, "jpeg_b64": jpeg_b64})
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

    Returns:
        List of dicts with ``timestamp``, ``description``, ``text_read``,
        ``confidence``. Returns ``[]`` on any failure.
    """
    if not frames:
        return []

    timestamps = ", ".join(f["timestamp"] for f in frames)
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

    try:
        resp_json = await call_gemini_api(payload)
        text_resp = resp_json["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text_resp)

        # Ensure timestamps match our frame extraction order
        results: list[dict] = []
        for i, item in enumerate(data):
            if i < len(frames):
                item["timestamp"] = frames[i]["timestamp"]
            results.append(item)

        return results
    except Exception as e:
        logger.error(f"Vision analysis API call failed: {e}")
        return []


def format_visual_evidence(evidence_list: list[dict]) -> str:
    """Format visual evidence for inclusion in structured analysis prompt.

    Each item is rendered as:
        [MM:SS] description | Текст: <text_read> | Уверенность: <confidence>
    """
    if not evidence_list:
        return "VISUAL ANALYSIS UNAVAILABLE"

    parts: list[str] = []
    for item in evidence_list:
        ts = item.get("timestamp", "??:??")
        desc = item.get("description", "не описано")
        text_read = item.get("text_read")
        confidence = item.get("confidence", "medium")

        line = f"[{ts}] {desc}"
        if text_read:
            line += f" | Текст: {text_read}"
        line += f" | Уверенность: {confidence}"
        parts.append(line)

    return "\n".join(parts)


async def extract_visual_evidence(video_path: str) -> str | None:
    """Extract visual evidence from video frames.

    Main entry point. Returns a formatted string suitable for inclusion
    in the structured analysis prompt, or ``None`` if vision analysis is
    unavailable (caller falls back to transcript-only).
    """
    try:
        frames = await extract_keyframes(video_path)
        if not frames:
            logger.warning("No frames extracted from video")
            return None

        logger.info(f"Extracted {len(frames)} keyframes for vision analysis")

        evidence = await analyze_frames_with_vision(frames)
        if not evidence:
            logger.warning("Vision analysis returned no evidence")
            return None

        formatted = format_visual_evidence(evidence)
        logger.info(f"Visual evidence formatted: {len(evidence)} items")
        return formatted
    except Exception as e:
        logger.error(f"Visual evidence extraction failed: {e}")
        return None
