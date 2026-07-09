import asyncio
import os
import shutil
import logging
import google.generativeai as genai
from aiogram import Bot
from aiogram.types import FSInputFile
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import Job

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

import httpx
import json as _json

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
                    logger.info(f"Downloaded via Cobalt ({base}).")
                    return output_path
                except Exception as e:
                    logger.warning(f"Cobalt download failed from {base}: {e}, trying next...")

    # Fallback to yt-dlp
    logger.info(f"All cobalt instances failed. Falling back to yt-dlp for {url}...")
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


async def analyze_video(file_path: str) -> str:
    def _upload_and_analyze():
        video_file = genai.upload_file(path=file_path)
        return video_file

    video_file = await asyncio.to_thread(_upload_and_analyze)
    
    while video_file.state.name == "PROCESSING":
        await asyncio.sleep(3)
        video_file = await asyncio.to_thread(genai.get_file, video_file.name)
        
    if video_file.state.name == "FAILED":
        raise Exception("Gemini video processing failed.")
        
    def _generate():
        model = genai.GenerativeModel(model_name="gemini-3.5-flash")
        prompt = """Проанализируй это короткое видео подробно:

1. О чём оно? Опиши содержание (что показано, что говорят, какая идея/концепция)
2. Что за техника/трюк/решение демонстрируется?
3. Насколько это реально реализовать? Оцени от 1 до 10 с обоснованием
4. Раздели: факт vs вымысел/преувеличение

5. Оцени реализуемость с учётом УЖЕ СУЩЕСТВУЮЩЕЙ инфраструктуры:

ТЕКУЩИЙ СТЕК (уже работает):
- Hermes Agent: AI-агент с инструментами (terminal, web, file, code, cron, skills), Telegram Gateway
- n8n: локально на http://localhost:5678, webhook-автоматизация
- Python: скрипты, парсинг, API-интеграции
- Telegram-боты: aiogram (этот reels-бот), Telethon (автопостинг)
- Telegram-каналы: @hermesaigm (AI-новости, cron 21:00), @remotejobd (вакансии, cron 10:00), @savemyreels (этот канал)
- Agent Reach: парсинг Twitter/X, YouTube, веб-контента
- Контент-пайплайн: GDrive → Whisper (транскрипция) → LLM → Telegram + YouTube (unlisted)
- LLM-провайдеры: Gemini, Claude, GPT, GLM-5.2 (z.ai), OpenRouter, ZenMux, GonkaGate
- Google Workspace: Gmail, Drive, Calendar через gws CLI
- Cobalt API: скачивание видео (reels/tiktok/youtube)
- computer_use: управление рабочим столом (Windows), browser CDP
- Docker: Postgres + Redis + ARQ воркеры для очередей
- Windows 10 хост (Паттайя, удалённая работа)

Можно ли повторить идею из видео? Что УЖЕ есть, а что нужно добавить? Оцени сложность.

6. Сформируй конкретную задачу для Hermes Agent в формате:
   ЗАДАЧА: [краткое название]
   ЦЕЛЬ: [что должно получиться]
   ИСПОЛЬЗУЕТ: [какие существующие компоненты задействовать]
   ДОБАВИТЬ: [что нового нужно создать]
   ШАГИ: [пронумерованный план]
   КРИТЕРИИ ГОТОВНОСТИ: [как проверим что работает]
   
   Если реализация невозможна или нерациональна — напиши почему и предложи альтернативу."""
        response = model.generate_content([video_file, prompt])
        return response.text
        
    result = await asyncio.to_thread(_generate)
    
    await asyncio.to_thread(genai.delete_file, video_file.name)
    return result

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
    """Extract content sections (1-4) for public channel — no stack/task info."""
    # Try to cut at section 5 (stack assessment)
    markers = [
        '\n5.', '\n5.', 'Оцени реализуемость в нашем стеке',
        'Оценка реализуемости в нашем стеке', 'реализуемость в нашем стеке'
    ]
    result = analysis
    for marker in markers:
        idx = result.find(marker)
        if idx > 100:
            result = result[:idx].strip()
            break
    return result


async def send_long_text(bot: Bot, chat_id: int, text: str):
    """Send text that may exceed Telegram's 4096 char limit."""
    for chunk in _split_text(text):
        await bot.send_message(chat_id=chat_id, text=chunk)
        await asyncio.sleep(0.3)


async def process_video(ctx, job_id: str, url: str, user_id: int):
    await update_job_status(job_id, 'PROCESSING')
    tmp_dir = f"/tmp/reels_bot/{job_id}"
    os.makedirs(tmp_dir, exist_ok=True)
    video_path = f"{tmp_dir}/video.mp4"
    
    try:
        logger.info(f"Downloading {url} for job {job_id}")
        await download_video(url, video_path)
        
        width, height = await get_video_dimensions(video_path)
        logger.info(f"Video dimensions: {width}x{height}")

        logger.info(f"Analyzing {video_path}")
        analysis = await analyze_video(video_path)

        # Send result to user
        logger.info(f"Sending to TG user {user_id}")
        bot = Bot(token=settings.bot_token)
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
        
        # Publish to channel @savemyreels
        try:
            logger.info(f"Publishing to channel {CHANNEL_CHAT_ID}")
            bot = Bot(token=settings.bot_token)
            channel_kwargs = {
                "chat_id": CHANNEL_CHAT_ID,
                "video": FSInputFile(video_path),
            }
            if width > 0 and height > 0:
                channel_kwargs["width"] = width
                channel_kwargs["height"] = height
            await bot.send_video(**channel_kwargs)
            summary = _extract_summary(analysis)
            await send_long_text(bot, CHANNEL_CHAT_ID, summary)
            await bot.session.close()
            logger.info("Published to channel successfully.")
        except Exception as e:
            logger.error(f"Channel publish failed: {e}")
        
        await update_job_status(job_id, 'DONE', tg_file_id=msg.video.file_id, analysis_text=analysis)
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
