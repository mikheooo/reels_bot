import asyncio
import logging
import os
import random
import re
import shutil
import time
import uuid

import google.generativeai as genai
import googleapiclient.http
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import FSInputFile

from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import Job, Task
from app.worker.factcheck import (
    extract_claims,
    qa_audit,
    search_exa_for_claim,
    validate_claims,
)
from app.worker.business_check import run_business_check, format_business_check_markdown
from app.worker.structured_analysis import generate_structured_analysis
from app.worker.schemas import VideoAnalysis

_original_execute = googleapiclient.http.HttpRequest.execute

def _patched_execute(self, *args, **kwargs):
    if "$discovery" in self.uri and "key=AQ" in self.uri:
        self.uri = self.uri.split("&key=")[0]
    return _original_execute(self, *args, **kwargs)

googleapiclient.http.HttpRequest.execute = _patched_execute

logger = logging.getLogger(__name__)
genai.configure(api_key=settings.gemini_api_key)

async def update_job_status(job_id: str, status: str, **kwargs):
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if job:
            job.status = status
            for k, v in kwargs.items():
                setattr(job, k, v)
            await session.commit()

import json as _json

import httpx

COBALT_INSTANCES = [
    "https://co.eepy.today",
    "https://dwnld.nichind.dev",
]

async def _try_cobalt(client: httpx.AsyncClient, base: str, url: str) -> str | None:
    """Try one cobalt instance. Returns download URL or None."""
    try:
        resp = await client.post(
            f"{base}/",
            json={"url": url},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ReelsBot/1.0",
            },
        )
        if resp.status_code != 200:
            logger.warning(f"Cobalt {base} returned {resp.status_code}")
            return None
        data = resp.json()
        status = data.get("status")
        if status in ("tunnel", "redirect"):
            dl_url = data.get("url")
            if dl_url:
                logger.info(f"Cobalt {base} → {status}: {dl_url[:80]}...")
                return dl_url
        logger.warning(f"Cobalt {base} unexpected status: {status} ({data.get('error', {})})")
        return None
    except Exception as e:
        logger.warning(f"Cobalt {base} error: {e}")
        return None

async def download_video(url: str, output_path: str) -> str:
    # Try cobalt instances first
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for base in COBALT_INSTANCES:
            dl_url = await _try_cobalt(client, base, url)
            if dl_url:
                logger.info(f"Downloading from cobalt to {output_path}...")
                try:
                    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as dl_client:
                        async with dl_client.stream("GET", dl_url) as stream:
                            stream.raise_for_status()
                            with open(output_path, "wb") as f:
                                async for chunk in stream.aiter_bytes(chunk_size=65536):
                                    f.write(chunk)
                    
                    # Verify size - if < 1MB, it's likely a broken DASH fragment or thumbnail
                    fsize = os.path.getsize(output_path)
                    if fsize < 1_000_000:
                        logger.warning(f"Cobalt {base} returned suspiciously small file ({fsize} bytes). Rejecting.")
                        os.unlink(output_path)
                        continue # try next cobalt or fallback
                        
                    logger.info(f"Downloaded via Cobalt ({base}).")
                    return output_path
                except Exception as e:
                    logger.warning(f"Cobalt download failed from {base}: {e}, trying next...")

    # Fallback to yt-dlp
    logger.info(f"All cobalt instances failed or returned junk. Falling back to yt-dlp for {url}...")
    # Add fake user agent and cookies if needed, but basic usually works
    cmd = ["yt-dlp", "--max-filesize", "50M", "-o", output_path, url]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise Exception(f"Download failed (yt-dlp): {stderr.decode()}")
    logger.info("Downloaded via yt-dlp.")
    return output_path


async def get_video_dimensions(file_path: str) -> tuple[int, int]:
    """Get video width/height via ffprobe. Returns (0, 0) on failure."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-select_streams", "v:0",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        info = _json.loads(stdout.decode())
        stream = info["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except Exception as e:
        logger.warning(f"ffprobe failed: {e}, sending without dimensions")
        return 0, 0


async def downscale_video(input_path: str) -> str:
    """Downscale video to 720p if larger. Returns path (may be same or new)."""
    try:
        w, h = await get_video_dimensions(input_path)
        fsize = os.path.getsize(input_path)
        # only skip if truly small SD
        if w <= 720 and h <= 1280 and fsize < 15_000_000:
            # still force re-encode to progressive mp4 for Gemini compatibility
            if fsize < 2_000_000:
                logger.info(f"Video {w}x{h} {fsize}b small, will re-encode to progressive")
            else:
                logger.info(f"Video {w}x{h} already <=720p, no downscale needed")
                return input_path
        output_path = input_path.replace(".mp4", "_720.mp4")
        # Force progressive H264/AAC for Gemini File API
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", "scale=720:-2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "faststart",
            output_path
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"ffmpeg downscale failed: {stderr.decode()[:800]}, trying alternative")
            # fallback: try without scale, just remux to progressive
            cmd2 = [
                "ffmpeg", "-y", "-i", input_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac",
                "-movflags", "faststart",
                output_path
            ]
            proc2 = await asyncio.create_subprocess_exec(*cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr2 = await proc2.communicate()
            if proc2.returncode != 0:
                logger.warning(f"ffmpeg fallback also failed: {stderr2.decode()[:500]}, using original")
                return input_path
        new_w, new_h = await get_video_dimensions(output_path)
        new_sz = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        logger.info(f"Downscaled {w}x{h} {fsize}b -> {new_w}x{new_h} {new_sz}b: {output_path}")
        return output_path
    except Exception as e:
        logger.warning(f"downscale_video error: {e}, using original")
        return input_path



async def get_raw_transcript(file_path: str, max_rounds: int = 5, base_delay: float = 3.0, cap_delay: float = 60.0) -> str:
    """Extract raw transcript and visual descriptions with multi-round Gemini key rotation, 429/quota retry with exponential backoff."""
    keys = []
    for i in range(1, 10):
        k = os.getenv(f"GEMINI_API_KEY_{i}")
        if k:
            keys.append(k.strip())
    if not keys:
        if getattr(settings, "gemini_api_key", None):
            keys.append(settings.gemini_api_key.strip())
        
    last_error = None
    for round_idx in range(1, max_rounds + 1):
        for key in keys:
            genai.configure(api_key=key, transport="rest")
            logger.info(f"Trying Gemini API key ending in ...{key[-4:] if key else 'None'} (Round {round_idx}/{max_rounds})")
            
            video_file = None
            try:
                def _upload_and_analyze():
                    return genai.upload_file(path=file_path)

                video_file = await asyncio.to_thread(_upload_and_analyze)
                
                while video_file.state.name == "PROCESSING":
                    await asyncio.sleep(3)
                    video_file = await asyncio.to_thread(genai.get_file, video_file.name)
                    
                if video_file.state.name == "FAILED":
                    err_detail = getattr(video_file, 'error', None)
                    raise Exception(f"Gemini video processing failed: {err_detail}")
                    
                def _generate():
                    flash_model = genai.GenerativeModel(model_name="gemini-3.7-flash")
                    extraction_prompt = "Сделай полную подробную транскрипцию всего, что говорят в этом видео. Также детально опиши всё, что происходит на экране."
                    extraction_response = flash_model.generate_content([video_file, extraction_prompt])
                    return extraction_response.text

                result = await asyncio.to_thread(_generate)
                return result
                
            except Exception as e:
                error_msg = str(e).lower()
                if any(err_kw in error_msg for err_kw in ("429", "quota", "exhausted", "503", "high demand", "unavailable", "rate limit")):
                    logger.warning(f"Gemini API key (...{key[-4:] if key else 'None'}) rate limited / busy: {e}. Rotating...")
                    last_error = e
                    continue
                else:
                    raise e
            finally:
                if video_file is not None:
                    try:
                        await asyncio.to_thread(genai.delete_file, video_file.name)
                    except Exception as del_err:
                        logger.debug(f"Failed to delete uploaded video file {getattr(video_file, 'name', 'unknown')}: {del_err}")

        # If all keys were rate-limited in this round
        if round_idx < max_rounds:
            temp = min(cap_delay, base_delay * (2 ** (round_idx - 1)))
            wait_time = random.uniform(temp * 0.5, temp)
            logger.warning(f"All Gemini API keys rate limited/busy in get_raw_transcript. Sleeping {wait_time:.2f}s before retry round {round_idx + 1}/{max_rounds}...")
            await asyncio.sleep(wait_time)

    raise last_error or Exception("All API keys failed in get_raw_transcript")


def format_analysis_markdown(analysis: VideoAnalysis, mechanics_text: str) -> str:
    parts = []
    if mechanics_text:
        parts.append(mechanics_text)
    
    parts.append("🔎 **Проверка фактов:**")
    for c in analysis.claims:
        if c.status == "подтверждено":
            parts.append(f"- ✅ [Подтверждено] {c.statement}\n  (Источник: [{c.source_type}] {c.source_url})")
        elif c.status == "опровергнуто":
            parts.append(f"- ❌ [Опровергнуто] {c.statement}\n  (Источник: {c.source_url})")
        elif c.status == "не проверено":
            parts.append(f"- 🟡 [Не проверено] {c.statement} ({c.unverified_reason or 'Нет надежных источников'})")
    
    if getattr(analysis, 'business_check', None):
        parts.append(format_business_check_markdown(analysis.business_check))

    if analysis.task_description:
        parts.append(f"\nЗАДАЧА:\n{analysis.task_description}")
        
    return "\n\n".join(parts)

def _split_text(text: str, limit: int = 4096) -> list[str]:
    """Split long text into chunks under Telegram's message limit."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind('\n\n', 0, limit)
        if split_at < limit // 2:
            split_at = text.rfind('\n', 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


def _extract_summary(analysis: str) -> str:
    """Extract only the short 1-2 sentence summary for the public channel."""
    start_marker = "КРАТКО ДЛЯ КАНАЛА:"
    start_idx = analysis.find(start_marker)

    if start_idx != -1:
        text_after = analysis[start_idx + len(start_marker):].strip()
        for sep in ['\n---', '\n\n---', '\n1.', '\n**1.', '\n### 1.', '\n\n1.']:
            idx = text_after.find(sep)
            if 0 < idx < len(text_after):
                text_after = text_after[:idx].strip()
                break
        text_after = text_after.strip('-').strip()
        if text_after:
            return text_after

    first = analysis.split('\n\n')[0][:500].strip()
    return first.strip('-').strip()


GENERIC_TITLES = {
    "идея из видео", "идея из видео (reels)", "идея из текста", "idea from video", "reel idea", "новая задача",
    "разбор видео", "видео разбор", "задача из рилз", "идея", "задача", "видео", "анализ видео"
}


def clean_title_str(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r"^[#\*\-\s\d\.\:\(\)\[\]]+", "", t).strip()
    
    prefixes = [
        r"^идея\s+из\s+видео\s*[:\-–—]?",
        r"^идея\s+из\s+текста\s*[:\-–—]?",
        r"^idea\s+brief\s*[:\-–—]?",
        r"^idea\s+from\s+video\s*[:\-–—]?",
        r"^reel\s+idea\s*[:\-–—]?",
        r"^новая\s+задача\s*[:\-–—]?",
        r"^задача\s+для\s+михаила\s*[:\-–—\(\)]*",
        r"^задача\s*[:\-–—]?",
        r"^концепция\s*[:\-–—]?",
        r"^концепт\s*[:\-–—]?",
        r"^дельта\s+механики\s*[:\-–—]?",
        r"^разбор\s+видео\s*[:\-–—]?",
        r"^разбор\s*[:\-–—]?",
        r"^обзор\s*[:\-–—]?",
        r"^суть\s*[:\-–—]?",
        r"^цель\s*[:\-–—]?",
        r"^идея\s*[:\-–—]?",
        r"^как\s+применить\s*[:\-–—\(\)]*",
        r"^как\s+михаил\s+может\s+это\s+применить\s*[:\-–—\(\)]*",
        r"^применение\s+в\s+стеке\s*[:\-–—\(\)]*",
        r"^применение\s+в\s+работе\s+и\s+жизни\s*[:\-–—\(\)]*",
        r"^применение\s+в\s+работе\s*[:\-–—\(\)]*",
        r"^применение\s+и\s+интеграция\s*[:\-–—\(\)]*",
        r"^применение\s+идеи\s*[:\-–—\(\)]*",
        r"^применение\s*[:\-–—\(\)]*",
        r"^интеграция\s+в\s+стек\s*[:\-–—\(\)]*",
        r"^чистый\s+концентрат\s+для\s+инженера\s*[:\-–—\(\)]*",
        r"^белая\s+авто-система\s+ревью\s+фильмов\s*[:\-–—\(\)]*",
        r"^видео\s+демонстрирует\s+(?:три|3|две|2|четыре|4|пять|5)?\s*",
        r"^видео\s+показывает\s+",
        r"^видео\s+разбирает\s+",
        r"^автор\s+демонстрирует\s+",
        r"^автор\s+показывает\s+",
        r"^автор\s+презентует\s+",
        r"^автор\s+делится\s+",
        r"^автор\s+видео\s+развенчивает\s+миф\s+[^,]+,\s*(?:предлагая\s+)?",
        r"^схема\s+описывает\s+",
        r"^в\s+видео\s+демонстрируется\s+",
        r"^в\s+видео\s+показывается\s+",
        r"^в\s+видео\s+представлен\s+разбор\s+",
        r"^разбор\s+репозитория-агрегатора\s*\(?",
        r"^разбор\s+одного\s+из\s+лучших\s+github-репозиториев\s*",
        r"^разбор\s+классической\s+инфобизнесовой\s+воронки\s*[:\-]?\s*",
        r"^разбор\s+классического\s+эстетического\s+лайфстайл-видео\s*\(?[^\)]*\)?\s*,?\s*",
        r"^разбор\s+",
        r"^обзор\s+",
        r"^способ\s+",
        r"^пошаговая\s+методология\s+",
        r"^пошаговый\s+сборка\s+",
        r"^пошаговая\s+сборка\s+",
        r"^михаил,\s+эти\s+три\s+концепта\s+идеально\s+ложатся\s+на\s+твои\s+рабочие\s+процессы\s*[^:]*:\s*",
        r"^михаил,\s+тебе\s+не\s+нужно\s+[^.]*\.\s*(?:твоя\s+ценность\s*—\s*)?",
        r"^михаил,\s+для\s+тебя\s+это\s+[^,]*,\s*а\s*",
        r"^михаил,\s+не\s+трать\s+время\s+[^.]*\.\s*(?:но\s+сам\s+)?",
        r"^михаил\s+может\s+превратить\s+эту\s+ручную\s+рутину\s+из\s+видео\s+в\s*",
        r"^этот\s+ручной\s+процесс\s*—\s*идеальный\s+кандидат\s+на\s*",
        r"^тебе,\s*как\s+[^.]*,\s*",
        r"^тебе\s+не\s+нужен\s+[^.]*\.\s*",
    ]
    
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            new_t = re.sub(p, "", t, flags=re.IGNORECASE).strip()
            if new_t != t:
                t = new_t
                changed = True
                t = re.sub(r"^[\*\-\s\d\.\:\(\)\[\]\"'«»—–]+", "", t).strip()

    t = t.replace("**", "").replace("*", "").replace("`", "").replace("«", "").replace("»", "").replace('"', '').strip()
    t = re.sub(r"^\((?:как\s+)?михаил\s+может\s+это\s+применить\)", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^\((?:применение\s+в\s+работе|применение\s+в\s+стеке|применение|интеграция\s+в\s+стек|применение\s+идеи)\)", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^[\:\-\–—\s]+", "", t).strip()
    t = re.sub(r"https?://\S+", "", t).strip()
    t = t.rstrip(" :.-")
    
    if t:
        t = t[0].upper() + t[1:]
        
    return t


def is_valid_title(title: str) -> bool:
    if not title:
        return False
    t_clean = title.lower().strip()
    if t_clean in GENERIC_TITLES:
        return False
    if len(t_clean) < 8:
        return False
    words = title.split()
    if len(words) < 3:
        return False
    banned_starts = ["михаил,", "михаил ", "тебе, как", "тебе не нужен", "этот ручной", "видео интересно", "поскольку видео"]
    if any(t_clean.startswith(b) for b in banned_starts):
        return False
    return True


def truncate_to_words(title: str, min_words: int = 4, max_words: int = 10) -> str:
    words = title.split()
    if len(words) <= max_words:
        return title
    
    for sep in [":", " — ", " – ", " - ", ",", ";", "."]:
        idx = title.find(sep)
        if idx > 15:
            cand = title[:idx].strip()
            if min_words <= len(cand.split()) <= max_words:
                return cand.rstrip(" ,.-")
                
    return " ".join(words[:max_words]).rstrip(" ,.-")


def extract_tasks_from_analysis(analysis: str, url: str = "") -> list[dict]:
    """Parse analysis and return list of actionable tasks with concise, meaningful titles (4-10 words)."""
    # 1. Check if multiple explicit ЗАДАЧА blocks exist
    z_matches = list(re.finditer(r"(?:^|\n)(?:ЗАДАЧА(?:\s+\d+)?|###\s*6(?:\.\d+)?\.?\s*ЗАДАЧА.*?):\s*", analysis, re.IGNORECASE))
    
    if len(z_matches) > 1:
        tasks = []
        for i, match in enumerate(z_matches):
            start = match.end()
            end = z_matches[i + 1].start() if i + 1 < len(z_matches) else len(analysis)
            section_text = analysis[start:end].strip()
            first_line = section_text.splitlines()[0].strip() if section_text.splitlines() else ""
            title = clean_title_str(first_line)
            if not is_valid_title(title):
                title = clean_title_str(section_text[:120])
            title = truncate_to_words(title)
            tasks.append({
                "title": title,
                "description": section_text,
                "source_type": "reel",
                "source_url": url
            })
        return tasks

    # 2. Check Section 6
    m_task = re.search(r"(?:###\s*6\.?\s*ЗАДАЧА[^\n]*\n|ЗАДАЧА:[^\n]*\n)(.*?)(?=(?:\n###\s+[0-9]|\n---\s*\n###|\Z))", analysis, re.DOTALL | re.IGNORECASE)
    if m_task:
        task_block = m_task.group(1).strip()
        # Look for explicit Concept / Idea line
        m_c = re.search(r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:\*\*)?(?:концепция|концепт|идея)(?:\*\*)?\s*[:\*\#\s]*([^\n]+)", task_block, re.IGNORECASE)
        if m_c:
            cand = clean_title_str(m_c.group(1))
            if is_valid_title(cand):
                return [{
                    "title": truncate_to_words(cand),
                    "description": task_block,
                    "source_type": "reel",
                    "source_url": url
                }]

        # Look for numbered sub-tasks that represent distinct skills/tools
        numbered_items = list(re.finditer(r"(?:^|\n)\s*(\d+)\.\s+\*\*([^\*]+)\*\*(.*?)(?=(?:\n\s*\d+\.\s+\*\*|\n####|\n---|\Z))", task_block, re.DOTALL))
        if len(numbered_items) >= 2:
            sub_titles = [clean_title_str(m.group(2)) for m in numbered_items]
            if all(is_valid_title(st) for st in sub_titles):
                tasks = []
                for m in numbered_items:
                    t_title = truncate_to_words(clean_title_str(m.group(2)))
                    t_desc = m.group(0).strip()
                    tasks.append({
                        "title": t_title,
                        "description": t_desc,
                        "source_type": "reel",
                        "source_url": url
                    })
                return tasks
                
        # Check first actionable line
        for line in task_block.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("---") or line_str.startswith("#"):
                continue
            cand = clean_title_str(line_str)
            if is_valid_title(cand):
                return [{
                    "title": truncate_to_words(cand),
                    "description": task_block,
                    "source_type": "reel",
                    "source_url": url
                }]

    # 3. Look for Summary (КРАТКО ДЛЯ КАНАЛА:)
    m_sum = re.search(r"(?:КРАТКО|СУТЬ|ОПИСАНИЕ)(?:\s*ДЛЯ\s*КАНАЛА)?\s*\*?\*?:\s*\*?\*?\s*(.*?)(?:\n\n|\n#|\n---)", analysis, re.DOTALL | re.IGNORECASE)
    if m_sum:
        sum_text = m_sum.group(1).strip().replace("\n", " ")
        first_sent = re.split(r"[\.\!\?]\s+", sum_text)[0].strip()
        cand = clean_title_str(first_sent)
        if is_valid_title(cand):
            return [{
                "title": truncate_to_words(cand),
                "description": analysis,
                "source_type": "reel",
                "source_url": url
            }]

    # 4. Look for Section 1 (О ЧЁМ ВИДЕО)
    m_about = re.search(r"###\s*1\.?\s*О\s*ЧЁМ\s*ВИДЕО[^\n]*\n(.*?)(?:\n###|\Z)", analysis, re.DOTALL | re.IGNORECASE)
    if m_about:
        about_text = m_about.group(1).strip()
        m_idea = re.search(r"(?:Идея\s*/\s*Концепция|Концепция|Проблема\s*/\s*Возможность|Содержание)\s*[:\*\#]*\s*([^\n\*\#]+)", about_text, re.IGNORECASE)
        if m_idea:
            cand = clean_title_str(m_idea.group(1))
            if is_valid_title(cand):
                return [{
                    "title": truncate_to_words(cand),
                    "description": analysis,
                    "source_type": "reel",
                    "source_url": url
                }]

    return [{
        "title": "Интеграция решения из видео",
        "description": analysis,
        "source_type": "reel",
        "source_url": url
    }]


def _extract_task(analysis: str) -> dict | None:
    """Backward-compatible helper returning single task or None."""
    tasks = extract_tasks_from_analysis(analysis)
    return tasks[0] if tasks else None


async def send_long_text(bot: Bot, chat_id: int, text: str):
    """Send text that may exceed Telegram's 4096 char limit."""
    for chunk in _split_text(text):
        await bot.send_message(chat_id=chat_id, text=chunk)
        await asyncio.sleep(0.3)


async def process_video(ctx, job_id: str, url: str, user_id: int):
    if not getattr(settings, 'exa_api_key', None) or not getattr(settings, 'jina_api_key', None):
        msg = "⚠️ Ошибка пайплайна: отсутствуют ключи EXA_API_KEY или JINA_API_KEY. Проверка фактов и автоматическая публикация заблокированы."
        logger.error(f"Job {job_id} REVIEW_REQUIRED: {msg}")
        await update_job_status(job_id, 'REVIEW_REQUIRED', error_text=msg)
        bot = Bot(token=settings.bot_token)
        try:
            await bot.send_message(chat_id=user_id, text=msg)
        except Exception:
            pass
        finally:
            await bot.session.close()
        return

    await update_job_status(job_id, 'PROCESSING')
    tmp_dir = f"/tmp/reels_bot/{job_id}"
    os.makedirs(tmp_dir, exist_ok=True)
    video_path = f"{tmp_dir}/video.mp4"
    
    try:
        logger.info(f"Downloading {url} for job {job_id}")
        await download_video(url, video_path)
        
        width, height = await get_video_dimensions(video_path)
        logger.info(f"Video dimensions: {width}x{height}, size: {os.path.getsize(video_path)} bytes")

        # Auto-downscale if >720p to avoid Gemini FAILED on high-res reels
        if width > 720 or height > 1280:
            video_path = await downscale_video(video_path)
            width, height = await get_video_dimensions(video_path)
            logger.info(f"After downscale: {width}x{height}, size: {os.path.getsize(video_path)} bytes")

        logger.info(f"Analyzing {video_path}")
        
        logger.info(f"Extracting raw transcript for {video_path}")
        raw_video_text = await get_raw_transcript(video_path)

        # Restore the Reels Analyzer report contract. The legacy reels_bot
        # formatter below is retained only as a fallback if report generation fails.
        try:
            structured_analysis = await generate_structured_analysis(raw_video_text)
            logger.info("Structured Reels Analyzer report generated.")
        except Exception as structured_err:
            logger.error(f"Structured Reels Analyzer report failed: {structured_err}")
            structured_analysis = None
        
        logger.info("Executing Phase 2 Pipeline...")
        claims = await extract_claims(raw_video_text)
        search_data = {}
        for c in claims:
            if c.claim_type == "fact":
                search_data[c.statement] = await search_exa_for_claim(c)
        
        logger.info("Validating claims...")
        analysis_obj = await validate_claims(claims, search_data)
        
        logger.info("Executing Business Check...")
        try:
            bc_res = await run_business_check(
                transcript=raw_video_text,
                claims=claims,
                factcheck_analysis=analysis_obj
            )
            analysis_obj.business_check = bc_res
        except Exception as bc_err:
            logger.error(f"Business Check failed (non-blocking): {bc_err}")

        logger.info("Running QA Audit...")
        qa_res = await qa_audit(analysis_obj)
        
        # Prefer the restored structured report. Keep the legacy formatter as a
        # safe fallback so a temporary model/API failure does not lose the job.
        analysis = structured_analysis or format_analysis_markdown(
            analysis_obj,
            "📝 **Сырой Транскрипт (Механика):**\n" + raw_video_text[:1000] + "..."
        )

        if not qa_res.approved:
            msg = "⚠️ Пост заблокирован QA-контроллером.\nПричины:\n- " + "\n- ".join(qa_res.reasons)
            logger.warning(msg)
            await update_job_status(job_id, 'REVIEW_REQUIRED', error_text=msg, qa_reasons=qa_res.reasons)
            bot = Bot(token=settings.bot_token)
            try:
                await bot.send_message(chat_id=user_id, text=msg)
                await send_long_text(bot, user_id, analysis)
            except Exception:
                pass
            finally:
                await bot.session.close()
            return


        # Извлекаем задачи из анализа с содержательными заголовками
        extracted_tasks = extract_tasks_from_analysis(analysis, url=url)
        primary_title = extracted_tasks[0]['title'] if extracted_tasks else 'Интеграция решения из видео'

        # Сохраняем разбор видео в Hermes plans (чистый бриф для архитектора)
        plan_file = f"/plans/idea_{job_id}.md"
        try:
            with open(plan_file, "w", encoding="utf-8") as f:
                f.write(f"# {primary_title}\n\n")
                f.write(analysis)
            logger.info(f"Video idea brief saved to {plan_file} with title: {primary_title}")
            
            # Добавляем в общий список (Backlog)
            backlog_file = "/plans/BACKLOG.md"
            with open(backlog_file, "a", encoding="utf-8") as bf:
                if os.path.getsize(backlog_file) == 0 if os.path.exists(backlog_file) else True:
                    bf.write("# База Идей (Backlog)\n\n")
                for t in extracted_tasks:
                    bf.write(f"- [ ] [{t['title']}]({plan_file.split('/')[-1]}) - {url}\n")
                
        except Exception as e:
            logger.error(f"Failed to save idea brief to {plan_file}: {e}")

        # Send result to user
        logger.info(f"Sending to TG user {user_id}")
        from aiohttp import ClientTimeout
        session = AiohttpSession(timeout=ClientTimeout(total=900))
        bot = Bot(token=settings.bot_token, session=session)
        send_kwargs = {
            "chat_id": user_id,
            "video": FSInputFile(video_path),
        }
        if width > 0 and height > 0:
            send_kwargs["width"] = width
            send_kwargs["height"] = height
        msg = await bot.send_video(**send_kwargs)
        # Analysis as separate message(s) — full text, split if needed
        await send_long_text(bot, user_id, analysis)
        await bot.session.close()
        
        # Publish to channel
        try:
            channel_id = settings.channel_chat_id or "@savemyreels"
            logger.info(f"Publishing to channel {channel_id}")
            channel_session = AiohttpSession(timeout=ClientTimeout(total=900))
            bot = Bot(token=settings.bot_token, session=channel_session)
            channel_kwargs = {
                "chat_id": channel_id,
                "video": FSInputFile(video_path),
            }
            if width > 0 and height > 0:
                channel_kwargs["width"] = width
                channel_kwargs["height"] = height
            await bot.send_video(**channel_kwargs)
            summary = _extract_summary(analysis)
            await send_long_text(bot, channel_id, summary)
            await bot.session.close()
            logger.info("Published to channel successfully.")
        except Exception as e:
            logger.error(f"Channel publish failed: {e}")
        
        from datetime import datetime, timedelta
        qa_reasons_data = {
            "analysis_json": analysis_obj.model_dump(),
            "mechanics_text": "📝 **Сырой Транскрипт (Механика):**\n" + raw_video_text[:1000] + "...",
            "audit_history": []
        }
        await update_job_status(job_id, 'DONE', tg_file_id=msg.video.file_id, analysis_text=analysis, qa_reasons=qa_reasons_data, audit_scheduled_at=datetime.utcnow() + timedelta(hours=24))

        # Сохраняем задачи в базу данных (PostgreSQL)
        try:
            if extracted_tasks:
                async with AsyncSessionLocal() as session:
                    for t in extracted_tasks:
                        new_task = Task(
                            id=str(uuid.uuid4()),
                            job_id=job_id,
                            user_id=user_id,
                            title=t['title'],
                            description=t.get('description'),
                            status='PENDING',
                        )
                        session.add(new_task)
                    await session.commit()
                logger.info(f"Tasks saved to DB: {[t['title'] for t in extracted_tasks]}")
        except Exception as e:
            logger.error(f"Failed to save task to DB: {e}")

        logger.info(f"Job {job_id} completed successfully.")
        
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        await update_job_status(job_id, 'ERROR', error_text=str(e))
        bot = Bot(token=settings.bot_token)
        try:
            await bot.send_message(chat_id=user_id, text=f"Произошла ошибка при обработке видео: {e}")
        except Exception:
            pass
        finally:
            await bot.session.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
