import asyncio
import logging
import os
import shutil
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



async def get_raw_transcript(file_path: str) -> str:
    keys = []
    for i in range(1, 10):
        k = os.getenv(f"GEMINI_API_KEY_{i}")
        if k: keys.append(k)
    if not keys:
        keys.append(settings.gemini_api_key)
        
    last_error = None
    for key in keys:
        genai.configure(api_key=key, transport="rest")
        logger.info(f"Trying Gemini API key ending in ...{key[-4:] if key else 'None'}")
        
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
                flash_model = genai.GenerativeModel(model_name="gemini-3.6-flash")
                extraction_prompt = "Сделай полную подробную транскрипцию всего, что говорят в этом видео. Также детально опиши всё, что происходит на экране."
                extraction_response = flash_model.generate_content([video_file, extraction_prompt])
                return extraction_response.text

            result = await asyncio.to_thread(_generate)
            await asyncio.to_thread(genai.delete_file, video_file.name)
            return result
            
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                last_error = e
                continue
            else:
                raise e
                
    raise last_error or Exception("All API keys failed")


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


def _extract_task(analysis: str) -> dict | None:
    """Parse ЗАДАЧА block from analysis. Returns {title, goal, uses, adds, steps, criteria} or None."""
    # Ищем блок ЗАДАЧА
    task_start = analysis.find('ЗАДАЧА:')
    if task_start == -1:
        return None

    task_block = analysis[task_start:]

    # Обрезаем хвост после КРИТЕРИИ ГОТОВНОСТИ
    crit_start = task_block.find('КРИТЕРИИ ГОТОВНОСТИ:')
    if crit_start != -1:
        # Найдём конец списка критериев (два переноса строки после начала)
        after_crit = task_block[crit_start:]
        # Ищем конец — либо конец текста, либо разделитель типа "Почему реализация"
        end_markers = ['\n\n**Почему', '\n\nПочему реализация', '\n\n---', '\n\n###']
        cut = len(task_block)
        for em in end_markers:
            idx = task_block.find(em, crit_start)
            if idx != -1 and idx < cut:
                cut = idx
        task_block = task_block[:cut]

    result = {}
    # ЗАДАЧА: [название]
    title_match = task_block.find('ЗАДАЧА:')
    if title_match != -1:
        after = task_block[title_match + len('ЗАДАЧА:'):].lstrip()
        end = after.find('\n')
        if end != -1:
            result['title'] = after[:end].strip('[]*\n ').strip()
        else:
            result['title'] = after.strip('[]*\n ').strip()

    # ЦЕЛЬ:
    for field, label in [('goal', 'ЦЕЛЬ:'), ('uses', 'ИСПОЛЬЗУЕТ:'), ('adds', 'ДОБАВИТЬ:'), ('steps', 'ШАГИ:'), ('criteria', 'КРИТЕРИИ ГОТОВНОСТИ:')]:
        idx = task_block.find(label)
        if idx != -1:
            after = task_block[idx + len(label):].lstrip()
            # Конец поля — следующая метка или конец блока
            next_labels = ['ЦЕЛЬ:', 'ИСПОЛЬЗУЕТ:', 'ДОБАВИТЬ:', 'ШАГИ:', 'КРИТЕРИИ ГОТОВНОСТИ:']
            end = len(after)
            for nl in next_labels:
                ni = after.find(nl)
                if ni != -1 and ni < end:
                    end = ni
            result[field] = after[:end].strip()

    if not result.get('title'):
        return None

    desc_parts = []
    if result.get('goal'):
        desc_parts.append(f"🎯 {result['goal']}")
    if result.get('steps'):
        desc_parts.append(f"📋 {result['steps']}")

    return {
        'title': result['title'],
        'description': '\n\n'.join(desc_parts) if desc_parts else None,
    }


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
        
        logger.info("Executing Phase 2 Pipeline...")
        claims = await extract_claims(raw_video_text)
        search_data = {}
        for c in claims:
            if c.claim_type == "fact":
                search_data[c.statement] = await search_exa_for_claim(c)
        
        logger.info("Validating claims...")
        analysis_obj = await validate_claims(claims, search_data)
        
        logger.info("Running QA Audit...")
        qa_res = await qa_audit(analysis_obj)
        
        # Format the final text
        # Since mechanics is not part of validate_claims anymore, we just use the raw output or a basic summary
        # For this integration, I'll pass raw text as mechanics for now so it's not empty
        analysis = format_analysis_markdown(analysis_obj, "📝 **Сырой Транскрипт (Механика):**\n" + raw_video_text[:1000] + "...")

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


        # Сохраняем разбор видео в Hermes plans (чистый бриф для архитектора)
        plan_file = f"/plans/idea_{job_id}.md"
        try:
            with open(plan_file, "w", encoding="utf-8") as f:
                f.write(f"# Идея из видео: {url}\n\n")
                f.write(analysis)
            logger.info(f"Video idea brief saved to {plan_file}")
            
            # Добавляем в общий список (Backlog)
            backlog_file = "/plans/BACKLOG.md"
            with open(backlog_file, "a", encoding="utf-8") as bf:
                if os.path.getsize(backlog_file) == 0 if os.path.exists(backlog_file) else True:
                    bf.write("# База Идей (Backlog)\n\n")
                bf.write(f"- [ ] [Идея из видео (Reels)]({plan_file.split('/')[-1]}) - {url}\n")
                
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

        # Извлекаем и сохраняем задачу
        try:
            task_data = _extract_task(analysis)
            if task_data:
                async with AsyncSessionLocal() as session:
                    new_task = Task(
                        id=str(uuid.uuid4()),
                        job_id=job_id,
                        user_id=user_id,
                        title=task_data['title'],
                        description=task_data.get('description'),
                        status='PENDING',
                    )
                    session.add(new_task)
                    await session.commit()
                logger.info(f"Task saved: {task_data['title']}")
        except Exception as e:
            logger.error(f"Failed to save task: {e}")

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
