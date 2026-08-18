# Спецификация и Архитектура: Автономная система производства видео-критики и фактчекинга (Reels Critique Content Pipeline MVP)

**Версия:** 1.0.0  
**Дата:** 10 августа 2026 г.  
**Проект:** `reels_bot` (Instagram Reels Analyzer & Critique Factory)  
**Автор:** Hermes Agent  
**Целевой стек:** Python 3.11+, PostgreSQL 15, Redis + ARQ, Gemini 3.6 Flash (Vertex AI / REST API), Exa AI API, Jina AI, FFmpeg, Edge-TTS / OpenAI TTS.

---

## 1. Current Architecture (Существующая архитектура)

Текущая система `reels_bot` представляет собой реактивный пайплайн анализа видео, активируемый по ссылке на Instagram Reel из Telegram.

### Архитектурный поток (End-to-End):
```
[User Telegram]
       │ (отправка URL)
       ▼
[app.bot.handlers] ──► Создание записи Job(status="QUEUED") в PostgreSQL
       │
       ▼
[Redis / ARQ Queue] ──► Worker picks up task `process_video`
       │
       ├─► 1. download_video(url) [Cobalt API -> Fallback yt-dlp]
       ├─► 2. downscale_video() [FFmpeg -> 720p H264/AAC progressive]
       ├─► 3. get_raw_transcript() [Gemini File API (gemini-3.6-flash)]
       ├─► 4. extract_claims() [Gemini API -> List[Claim]]
       ├─► 5. search_exa_for_claim() [Exa AI Search]
       ├─► 6. validate_claims() [Gemini API -> VideoAnalysis]
       ├─► 7. qa_audit() [Jina AI r.jina.ai + Gemini QA Verification]
       │
       ├─► [IF QA REJECTED] ──► Status = REVIEW_REQUIRED, уведомление в TG
       │
       └─► [IF QA APPROVED]
               ├─► Сохранение брифа в `/plans/idea_{job_id}.md` и `BACKLOG.md`
               ├─► Сохранение записи Task в PostgreSQL
               ├─► Отправка видео + разбора личным сообщением юзеру
               └─► Публикация видео + краткого резюме в TG канал `@savemyreels`
```

### Основные технические особенности текущей реализации:
1. **База данных (`app/db/models.py`)**:
   - Таблица `jobs`: хранит `id`, `user_id`, `original_url`, `url_hash`, `status`, `analysis_text`, `qa_reasons`, `audit_scheduled_at`.
   - Таблица `tasks`: хранит инженерные задачи, извлеченные из видео.
2. **Асинхронные воркеры (`app/worker/tasks.py`)**:
   - Использование `ARQ` над `Redis` для очереди фоновых задач.
   - Использование `httpx` с обработкой таймаутов, потокового скачивания файлов и ротацией прокси/инстансов Cobalt.
3. **LLM и Фактчекинг (`app/worker/factcheck.py`)**:
   - Ротация API-ключей Gemini (`GEMINI_API_KEY_1..4`) с фоллбэком на Vertex AI (`aiplatform.googleapis.com`) через `google.auth`.
   - Алгоритм **Full Jitter Exponential Backoff** при вызовах Vertex AI.
   - Использование Exa AI для поиска первичных/официальных источников.
   - Двухэтапный контроль качества (**QA Audit**) через Jina AI (`https://r.jina.ai/`) и повторную проверку дословных цитат через Python + LLM.

---

## 2. Reusable Components (Переиспользуемые компоненты)

Все ключевые модули существующего `reels_bot` спроектированы автономно и на 100% переиспользуются в новом пайплайне производства контента:

| Компонент | Файл / Функция | Готовность | Интеграция в новый pipeline |
| :--- | :--- | :---: | :--- |
| **Downloader** | `app.worker.tasks.download_video` | ✅ 100% | Извлечение исходного видео по URL (Cobalt + yt-dlp fallback) |
| **Video Normalizer** | `app.worker.tasks.downscale_video`, `get_video_dimensions` | ✅ 100% | Приведение любого исходника к 720p 9:16 progressive H264/AAC |
| **Transcription & Vision** | `app.worker.tasks.get_raw_transcript` | ✅ 100% | Полная расшифровка речи и визуального ряда через Gemini File API |
| **Exa Search Engine** | `app.worker.factcheck.search_exa_for_claim` | ✅ 100% | Поиск улик, официальной документации и первоисточников |
| **Gemini API Client** | `app.worker.factcheck.call_gemini_api` | ✅ 100% | Ротация ключей, поддержка Vertex AI, Retry + Jitter |
| **Jina QA Audit** | `app.worker.factcheck.qa_audit` | ✅ 100% | Глубокая фетч-проверка цитат по прямым ссылкам |
| **PostgreSQL DB Layer** | `app.db.database`, `app.db.models` | ✅ 80% | Базовые сессии SQLAlchemy, требуются новые таблицы |
| **Redis / ARQ Worker** | `app.worker.settings.WorkerSettings` | ✅ 100% | Очереди очередей, менеджмент фоновых задач с авто-повторами |

---

## 3. New Architecture (Новая архитектура контент-фабрики)

Новая система превращает `reels_bot` из простого инспектора в **автономный генератор видео-разборов (Reels Critique Generator)**.

```
                  ┌──────────────────────────────────────────────┐
                  │                 SOURCE REEL                  │
                  └──────────────────────┬───────────────────────┘
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │          DOWNLOAD & TRANSCRIPTION            │
                  └──────────────────────┬───────────────────────┘
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │            CLAIM EXTRACTION & QA             │
                  └──────────────────────┬───────────────────────┘
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │          CONTENT-WORTHINESS SCORE            │
                  └───────┬──────────────────────────────┬───────┘
                          │ (Score < Threshold)          │ (Score >= Threshold)
                          ▼                              ▼
                 [SKIP / SAVE BRIEF]            ┌────────────────────────────────┐
                                                │      FACTCHECK & EVIDENCE      │
                                                └───────────────┬────────────────┘
                                                                ▼
                                                ┌────────────────────────────────┐
                                                │       CRITIQUE SCRIPT GEN      │
                                                └───────────────┬────────────────┘
                                                                ▼
                                                ┌────────────────────────────────┐
                                                │      MEDIA PLAN & TIMELINE     │
                                                └───────────────┬────────────────┘
                                                                ▼
                                                ┌────────────────────────────────┐
                                                │    TTS / AUDIO GENERATION      │
                                                └───────────────┬────────────────┘
                                                                ▼
                                                ┌────────────────────────────────┐
                                                │     FFMPEG VIDEO ASSEMBLY      │
                                                └───────────────┬────────────────┘
                                                                ▼
                                                ┌────────────────────────────────┐
                                                │      AUTOMATED QC GATES        │
                                                └───────┬────────────────┬───────┘
                                                        │ (FAILED)       │ (PASSED)
                                                        ▼                ▼
                                                [REVIEW_REQUIRED] [INSTAGRAM / TG]
```

---

## 4. Pipeline Stages (Подробное описание стадий)

Пайплайн состоит из 10 строго последовательных и восстанавливаемых шагов:

1. **DOWNLOAD & NORMALIZE**: Скачивание Reels, сохранение в локальное хранилище `/tmp/reels_bot/{content_id}/source.mp4`, стандартизация кодека через `ffmpeg`.
2. **TRANSCRIPTION & VISION**: Извлечение полного спич-транскрипта и описания экранных событий через `gemini-3.6-flash`.
3. **CLAIM EXTRACTION**: Извлечение всех ключевых утверждений автора (числовые показатели, заявления о заработке, технических возможностях, сроках).
4. **CONTENT-WORTHINESS SCORING**: Автоматическая оценка видео по 5 метрикам. Если итоговый сколл ниже порога (например, < 7.0/10), генерация видео прекращается, сохранен только текстовый разбор.
5. **FACT-CHECK & EVIDENCE COLLECTION**: Поиск через Exa AI + Jina AI. Присвоение юридически строгого статуса вердикта.
6. **SCRIPT GENERATION**: Генерация динамичного 30–60 сек текста роликов-ответа по структуре: `HOOK` -> `ORIGINAL CLAIM` -> `EVIDENCE` -> `VERDICT` -> `BETTER ALTERNATIVE` -> `CTA`.
7. **MEDIA PLAN & ASSET PREPARATION**:
   - Автоматическая нарезка 3–7 сек фрагмента оригинального видео.
   - Снятие скриншотов веб-страниц/улик через Playwright / HTML-to-Image.
   - Генерация закадрового голоса через TTS (Edge-TTS / OpenAI TTS).
   - Генерация файла субтитров SRT / ASS с пословным подсвечиванием.
8. **FFMPEG VIDEO ASSEMBLY**: Автоматический монтаж слоев: нарезка оригинала + оверлей скриншотов + TTS аудио + волновой график / прогресс-бар + субтитры в формате 9:16 (1080x1920).
9. **AUTOMATED QC GATES**: Комплексный прогон собранного файла через `ffprobe` и питоновские анализаторы.
10. **PUBLISHING & NOTIFICATION**: Публикация в Instagram (через HikerAPI / Playwright RPA) и Telegram с записью логов в БД.

---

## 5. PostgreSQL Schema (База данных)

Ниже представлена полная схема данных SQLAlchemy для хранения всех объектов контент-фабрики.

```python
# app/db/models_v2.py
import enum
from sqlalchemy import (
    JSON, BigInteger, Boolean, Column, DateTime, Enum, 
    Float, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.database import Base

class VerdictEnum(str, enum.Enum):
    FACTUALLY_SUPPORTED = "FACTUALLY_SUPPORTED"
    MISLEADING = "MISLEADING"
    UNSUPPORTED = "UNSUPPORTED"
    FALSE = "FALSE"
    OPINION = "OPINION"
    MARKETING_CLAIM = "MARKETING_CLAIM"
    NOT_ENOUGH_EVIDENCE = "NOT_ENOUGH_EVIDENCE"

class PipelineStateEnum(str, enum.Enum):
    DOWNLOADED = "DOWNLOADED"
    TRANSCRIBED = "TRANSCRIBED"
    ANALYZED = "ANALYZED"
    CLAIMS_EXTRACTED = "CLAIMS_EXTRACTED"
    FACT_CHECKED = "FACT_CHECKED"
    SCORED = "SCORED"
    SKIPPED_NOT_WORTHY = "SKIPPED_NOT_WORTHY"
    SCRIPT_READY = "SCRIPT_READY"
    ASSETS_READY = "ASSETS_READY"
    RENDERED = "RENDERED"
    QC_PASSED = "QC_PASSED"
    QC_FAILED = "QC_FAILED"
    PUBLISHED = "PUBLISHED"
    ERROR = "ERROR"

class SourceReel(Base):
    __tablename__ = "source_reels"
    
    id = Column(String, primary_key=True) # content_id (UUID)
    original_url = Column(String, nullable=False, index=True)
    url_hash = Column(String, nullable=False, index=True)
    author_handle = Column(String, nullable=True)
    duration_sec = Column(Float, nullable=True)
    local_path = Column(String, nullable=True)
    raw_transcript = Column(Text, nullable=True)
    state = Column(Enum(PipelineStateEnum), default=PipelineStateEnum.DOWNLOADED, nullable=False)
    
    # Worthiness metrics
    worthiness_score = Column(Float, nullable=True)
    worthiness_reasons = Column(JSON, nullable=True) # Dict of sub-scores
    worth_covering = Column(Boolean, default=False)
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    claims = relationship("ClaimEntity", back_populates="source_reel", cascade="all, delete-orphan")
    script = relationship("CritiqueScript", back_populates="source_reel", uselist=False)
    assets = relationship("MediaAsset", back_populates="source_reel")
    render_jobs = relationship("RenderJob", back_populates="source_reel")
    publication = relationship("PublishedPost", back_populates="source_reel", uselist=False)

class ClaimEntity(Base):
    __tablename__ = "claims"
    
    id = Column(String, primary_key=True)
    source_reel_id = Column(String, ForeignKey("source_reels.id"), nullable=False)
    statement = Column(Text, nullable=False)
    speaker_timestamp_start = Column(Float, nullable=True)
    speaker_timestamp_end = Column(Float, nullable=True)
    
    verdict = Column(Enum(VerdictEnum), default=VerdictEnum.NOT_ENOUGH_EVIDENCE, nullable=False)
    verdict_explanation = Column(Text, nullable=True)
    confidence_score = Column(Float, default=0.0) # 0.0 - 1.0
    
    source_reel = relationship("SourceReel", back_populates="claims")
    evidences = relationship("EvidenceEntity", back_populates="claim", cascade="all, delete-orphan")

class EvidenceEntity(Base):
    __tablename__ = "evidences"
    
    id = Column(String, primary_key=True)
    claim_id = Column(String, ForeignKey("claims.id"), nullable=False)
    source_title = Column(String, nullable=True)
    source_url = Column(String, nullable=False)
    source_type = Column(String, nullable=False) # official / secondary
    exact_quote = Column(Text, nullable=True)
    screenshot_path = Column(String, nullable=True)
    retrieved_at = Column(DateTime, server_default=func.now())
    
    claim = relationship("ClaimEntity", back_populates="evidences")

class CritiqueScript(Base):
    __tablename__ = "critique_scripts"
    
    id = Column(String, primary_key=True)
    source_reel_id = Column(String, ForeignKey("source_reels.id"), nullable=False)
    
    hook_text = Column(Text, nullable=False)
    original_claim_text = Column(Text, nullable=False)
    evidence_text = Column(Text, nullable=False)
    verdict_text = Column(Text, nullable=False)
    alternative_text = Column(Text, nullable=False)
    cta_text = Column(Text, nullable=True)
    
    full_narrator_script = Column(Text, nullable=False) # Готовый соединенный текст для TTS
    estimated_duration_sec = Column(Float, nullable=False)
    
    source_reel = relationship("SourceReel", back_populates="script")

class MediaAsset(Base):
    __tablename__ = "media_assets"
    
    id = Column(String, primary_key=True)
    source_reel_id = Column(String, ForeignKey("source_reels.id"), nullable=False)
    asset_type = Column(String, nullable=False) # source_clip / screenshot / tts_audio / srt_subtitles / b_roll
    file_path = Column(String, nullable=False)
    duration_sec = Column(Float, nullable=True)
    meta_info = Column(JSON, nullable=True) # extra params (e.g. resolution, crop coordinates)
    
    source_reel = relationship("SourceReel", back_populates="assets")

class RenderJob(Base):
    __tablename__ = "render_jobs"
    
    id = Column(String, primary_key=True)
    source_reel_id = Column(String, ForeignKey("source_reels.id"), nullable=False)
    output_video_path = Column(String, nullable=True)
    status = Column(String, default="PENDING") # PENDING / PROCESSING / COMPLETED / FAILED
    error_log = Column(Text, nullable=True)
    render_duration_sec = Column(Float, nullable=True)
    qc_passed = Column(Boolean, default=False)
    qc_details = Column(JSON, nullable=True)
    
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
    
    source_reel = relationship("SourceReel", back_populates="render_jobs")

class PublishedPost(Base):
    __tablename__ = "published_posts"
    
    id = Column(String, primary_key=True)
    source_reel_id = Column(String, ForeignKey("source_reels.id"), nullable=False)
    platform = Column(String, nullable=False) # instagram / telegram
    external_post_id = Column(String, nullable=True)
    post_url = Column(String, nullable=True)
    caption_text = Column(Text, nullable=True)
    published_at = Column(DateTime, server_default=func.now())
    
    source_reel = relationship("SourceReel", back_populates="publication")

class PipelineEvent(Base):
    __tablename__ = "pipeline_events"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_reel_id = Column(String, nullable=False, index=True)
    from_state = Column(String, nullable=True)
    to_state = Column(String, nullable=False)
    payload = Column(JSON, nullable=True)
    timestamp = Column(DateTime, server_default=func.now())
```

---

## 6. State Machine (Машина состояний и устойчивость)

### Переходы состояний:
```
[Start] ──► DOWNLOADED ──► TRANSCRIBED ──► CLAIMS_EXTRACTED
                                                  │
                                                  ▼
                                            FACT_CHECKED
                                                  │
                                                  ▼
                                               SCORED
                                       ┌──────────┴──────────┐
                                       │                     │
                     (worth_covering = False)           (worth_covering = True)
                                       │                     │
                                       ▼                     ▼
                             SKIPPED_NOT_WORTHY        SCRIPT_READY
                                                             │
                                                             ▼
                                                        ASSETS_READY
                                                             │
                                                             ▼
                                                          RENDERED
                                                             │
                                                             ▼
                                                         QC_PASSED ──► PUBLISHED
                                                             │
                                                     (QC Failed)
                                                             │
                                                             ▼
                                                         QC_FAILED / REVIEW_REQUIRED
```

### Принципы Идемпотентности и Восстановления (Recoverability):
1. **Каждый шаг сохраняет артефакты на диск**:
   Директория задачи `/tmp/reels_bot/{content_id}/`:
   - `source.mp4`
   - `transcript.json`
   - `claims.json`
   - `factcheck.json`
   - `script.json`
   - `assets/clip_cut.mp4`, `assets/evidence_1.png`, `assets/narrator.mp3`, `assets/subtitles.ass`
   - `output_final.mp4`
2. **Резюмируемость (Resume Capability)**: Если воркер уходит в перезагрузку на шаге `ASSETS_READY`, при повторном запуске он проверяет наличие `script.json` и готовых файлов ассетов, не тратя токены на повторные вызовы Gemini или Exa.
3. **ARQ Retries**: Для внешних сетевых вызовов (Exa, Cobalt, Gemini) используется механизм асинхронных повторов с экспоненциальной задержкой. При критической ошибке статус переходит в `ERROR` или `REVIEW_REQUIRED` с отправкой алерта в Telegram.

---

## 7. Fact-Checking & Classification Logic (Логика вердиктов)

Главное требование пользователя — **безопасность от иск/обвинений в клевете**. Система категорически избегает эмоциональных и оценочных ярлыков ("клише", "скамер", "инфоцыган", "мошенник").

### Классификация и матрица формулировок дискелеймеров:

| Категория вердикта | Значение | Разрешенная безопасная формулировка в видео |
| :--- | :--- | :--- |
| `FACTUALLY_SUPPORTED` | Заявление полностью подтверждено официальной документацией | *"Заявление подтверждается официальными данными [Источник]."* |
| `MISLEADING` | Частичная правда, но вырвана из контекста или умалчиваются расходы/сложности | *"Данные реальны, но упущена важная деталь: [Деталь]."* |
| `UNSUPPORTED` | Нет ни доказательств, ни опровержений. Сложно проверить | *"На данный момент публичных подтверждений этой цифре нет."* |
| `FALSE` | Заявление прямо противоречит фактам / законам физики / математике | *"Расчеты не сходятся. По официальной формуле получается иной результат."* |
| `OPINION` | Субъективное мнение автора видео | *"Это личное мнение автора, а не гарантированный факт."* |
| `MARKETING_CLAIM` | Рекламный преувеличенный слоган ("Лучший инструмент в мире") | *"Маркетинговое преувеличение, стандартное для промо-роликов."* |
| `NOT_ENOUGH_EVIDENCE` | Недостаточно открытых данных для однозначного вывода | *"Недостаточно данных в открытом доступе для полной проверки."* |

---

## 8. Script Generation (Генерация 30–60 секундного сценария)

Промпт генерации сценария строго ограничивает длительность (110–140 слов) и навязывает конструктивный тон.

### Шаблон структуры сценария (JSON Output Schema):
```json
{
  "hook": "Он утверждает, что этот метод приносит $500 в день с помощью ChatGPT. Проверим, так ли это.",
  "original_claim": "В видео говорится: достаточно запускать один скрипт и получать авто-выплаты.",
  "evidence": "Однако в официальной документации OpenAI указаны суточные лимиты API, а комиссия платформы съедает до 40% дохода.",
  "verdict": "Это классический MARKETING_CLAIM: реальный чистый доход в 5 раз ниже заявленного.",
  "better_alternative": "Если вы хотите автоматизировать работу, используйте официальный API с кешированием запросов — это снизит расходы на 70%.",
  "cta": "Подписывайтесь, здесь мы проверяем факты без воды.",
  "estimated_speaking_time_sec": 42.5
}
```

---

## 9. Voice-First vs Avatar-First Comparison (Сравнение подходов)

| Критерий | 🎙️ Voice-First (Выбран для MVP) | 🤖 Avatar-First |
| :--- | :--- | :--- |
| **Реалистичность (Realism)** | **10/10** — Естественный закадровый голос + реальные доказательства (скриншоты, документация). Нет фальши. | **6-7/10** — Зловещая долина (Uncanny Valley), глитчи синхронизации губ, неживая мимика. |
| **Себестоимость (Production Cost)** | **~$0.001 - $0.01 / ролик** (Edge-TTS бесплатный, OpenAI TTS цент за ролик). | **$0.20 - $0.80 / ролик** (HeyGen / Hedra / D-ID API) + подписки. |
| **Скорость генерации** | **3–10 секунд** на весь рендеринг через FFmpeg. | **1–5 минут** ожидания рендеринга аватара в облаке. |
| **Сложность автоматизации** | **Низкая**: Локальный 0-token FFmpeg пайплайн. | **Высокая**: Зависимость от сторонних внешних API, сбои очередей аватаров. |
| **Масштабируемость** | **Неограниченная** (можно генерировать хоть 100 роликов в час на одном CPU/GPU). | **Ограничена** лимитами API и бюджетом. |
| **Органичность в Instagram** | **Высокая**: Формат "аналитика со скриншотами и подсвеченным пруфом" органичен для Reels. | **Средняя/Низкая**: Пользователи распознают AI-аватары и пролистывают как спам. |

### Обоснование выбора Voice-First для MVP:
1. **Максимальное соответствие предпочтению пользователя** (Voice-first с опцией аватара позже).
2. **0-Token / Детерминированная сборка**: Не зависит от нестабильных видео-генераторов.
3. **Скорость и надежность**: Монтаж за секунды на текущем сервере.
4. **Легкое добавление аватара в будущем**: Абстрактный слой `MediaAsset` позволит просто подставлять видео аватара на задний план вместо B-roll / нарезки оригинала.

---

## 10. FFmpeg Assembly Architecture (Схема сборки 9:16)

Сборка производится через один высокооптимизированный запуск `ffmpeg` с использованием `filter_complex`.

```
[Input 0: Original Clip (cut 0-5s)] ──► Scale & Crop (1080x1920) ──┐
[Input 1: Evidence Screenshot.png]  ──► Overlay (Centered)      ├──► [Filter Complex] ──► Output 1080x1920 MP4
[Input 2: TTS Audio.mp3]          ──► Audio Track               │
[Input 3: Subtitles.ass]          ──► Burn-in Captions          ┘
```

### Точная FFmpeg команда сборки:
```bash
ffmpeg -y \
  -ss 00:00:01 -t 00:00:05 -i source_clip.mp4 \
  -loop 1 -t 00:00:35 -i evidence_screenshot.png \
  -i tts_narrator.mp3 \
  -filter_complex "
    [0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v0];
    [1:v]scale=900:-1[v1];
    [v0][v1]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2:enable='between(t,5,15)'[v_overlay];
    [v_overlay]subtitles=subtitles.ass[v_out]
  " \
  -map "[v_out]" -map 2:a \
  -c:v libx264 -preset fast -crf 22 -pix_fmt yuv420p \
  -c:a aac -b:a 192000 -ar 44100 \
  -shortest output_critique.mp4
```

---

## 11. QC Gates (Автоматический контроль качества)

Каждое сгенерированное видео проходит обязательную автоматическую инспекцию перед публикацией:

```python
# app/worker/qc.py
import json
import subprocess
import os

async def verify_rendered_video(video_path: str, expected_min_duration: float = 20.0) -> dict:
    reasons = []
    
    # 1. FFprobe metadata check
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", video_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"passed": False, "reasons": ["Corrupted MP4 container"]}
        
    data = json.loads(res.stdout)
    format_info = data.get("format", {})
    streams = data.get("streams", [])
    
    duration = float(format_info.get("duration", 0))
    if duration < expected_min_duration:
        reasons.append(f"Duration too short ({duration:.1f}s < {expected_min_duration}s)")
        
    v_stream = next((s for s in streams if s["codec_type"] == "video"), None)
    a_stream = next((s for s in streams if s["codec_type"] == "audio"), None)
    
    if not v_stream:
        reasons.append("Missing video stream")
    else:
        w, h = int(v_stream.get("width", 0)), int(v_stream.get("height", 0))
        if w != 1080 or h != 1920:
            reasons.append(f"Invalid resolution ({w}x{h}, expected 1080x1920)")
            
    if not a_stream:
        reasons.append("Missing audio stream (silent video)")
        
    # 2. Check for black / frozen frames via FFmpeg blackdetect
    cmd_black = [
        "ffmpeg", "-i", video_path,
        "-vf", "blackdetect=d=2:pix_th=0.10",
        "-f", "null", "-"
    ]
    res_black = subprocess.run(cmd_black, capture_output=True, text=True)
    if "blackdetect" in res_black.stderr:
        reasons.append("Black frames detected (>2 sec continuous black)")

    return {
        "passed": len(reasons) == 0,
        "duration": duration,
        "resolution": f"{w}x{h}" if v_stream else "N/A",
        "reasons": reasons
    }
```

---

## 12. Instagram Publishing Stage (Публикация)

### Выбранный вариант интеграции:
1. **Основной канал**: HikerAPI / Instagrapi (Python wrapper).
2. **Резервный канал (RPA)**: Playwright Chromium script с сохраненными куки (`CDP Profile` на портах Chrome).

### Параметры публикации:
- **Aspect Ratio**: 9:16 (1080x1920 MP4).
- **Cover Frame**: Автоматический выбор кадра на 3-й секунде (там, где появляется скриншот доказательства и яркий заголовок).
- **Caption Structure**:
  - Короткий тизер ситуации (2 предложения).
  - Вердикт и статус проверки.
  - Дисклеймер о нейтральности и фактах.
  - Хэштеги: `#фактчекинг #разбор #новоститехнологий #нейросети #новостиai`.

---

## 13. MVP Scope (Минимальный жизнеспособный продукт)

В рамках MVP первого релиза реализуется:
1. Расширение таблицы БД `jobs` и добавление новых моделей (`source_reels`, `claims`, `critique_scripts`, `render_jobs`).
2. Оценка `Worthiness Score` на шаге анализа.
3. Генерация текста критики через `gemini-3.6-flash` в формате JSON.
4. Синтез речи через `edge-tts` (бесплатный высококачественный русский/английский голос).
5. Детерминированный сборщик `ffmpeg` (нарезка оригинала + скриншот + TTS + субтитры).
6. QC модуль на базе `ffprobe`.
7. Отправка готового собранного ролика администратору в Telegram на утверждение перед постингом.

---

## 14. Future Extensions (Будущее развитие)

1. **Avatar-First Integration**: Добавление провайдеров Hedra / HeyGen через единый интерфейс `VideoGeneratorProvider`.
2. **A/B Тестирование Хуков**: Генерация 3 различных заставок/хуков и автоматический выбор на основе CTR первого часа.
3. **Авто-выбор B-roll**: Подтягивание релевантных видео-футажей из Pexels / Pixabay API под контекст текста.
4. **Multi-language Auto-dubbing**: Автоматический перевод русской аналитики на английский язык и постинг в англоязычный аккаунт Reels.

---

## 15. Exact Files / Modules to Modify or Add

### Новые создаваемые файлы:
- `C:\Users\Misha\reels_bot\app\db\models_v2.py` — новые SQLAlchemy таблицы.
- `C:\Users\Misha\reels_bot\app\worker\scoring.py` — оценка привлекательности контента (`worthiness_score`).
- `C:\Users\Misha\reels_bot\app\worker\script_gen.py` — промпты и генератор сценариев критики.
- `C:\Users\Misha\reels_bot\app\worker\tts.py` — синтез речи через Edge-TTS / OpenAI.
- `C:\Users\Misha\reels_bot\app\worker\ffmpeg_builder.py` — модуль сборки видео.
- `C:\Users\Misha\reels_bot\app\worker\qc_checker.py` — проверки готового файла.
- `C:\Users\Misha\reels_bot\docs\CRITIQUE_CONTENT_PIPELINE_MVP.md` — этот документ.

### Файлы для модификации (без нарушения обратной совместимости):
- `C:\Users\Misha\reels_bot\app\worker\schemas.py` — добавление Pydantic моделей для сценариев, оценок и вердиктов.
- `C:\Users\Misha\reels_bot\app\worker\factcheck.py` — расширение вердиктов (7 категорий) и матрица формулировок.
- `C:\Users\Misha\reels_bot\app\worker\tasks.py` — добавление шагов вызова генерации сценария, TTS, сборки и QC в основной workflow ARQ.

---

## Итоговое резюме (Summary for User)

### 1. Что уже есть:
* Стабильный скачиватель Reels (Cobalt + yt-dlp fallback) с нормализацией в 720p H264.
* Извлечение транскрипта и визуальных событий через `gemini-3.6-flash` (File API).
* Поисковый движок улик через Exa AI API.
* Двухэтапный контроль качества фактчекинга (Jina AI `r.jina.ai` + Gemini QA).
* Асинхронный воркер ARQ + Redis + PostgreSQL.
* Ротация ключей Gemini + Vertex AI с Retry & Jitter.

### 2. Что отсутствует:
* Оценка привлекательности/целесообразности разбора (`Worthiness Score`).
* Расширенная безопасная сетка вердиктов (7 категорий вместо бинарного подтверждено/опровергнуто).
* Модуль генерации короткого сценария разбора (HOOK -> CLAIM -> EVIDENCE -> VERDICT -> ALTERNATIVE).
* Синтезатор речи (TTS) и генератор субтитров с пословной тайминг-разметкой.
* Автоматический FFmpeg-сборщик видео с наложением слоев (скриншоты, нарезка, аудио, субтитры).
* Модуль автоматического технического QC готового ролика (ffprobe, битые кадры, тишина).

### 3. Какой MVP можно собрать из существующего кода:
Можно собрать **100% автономную фабрику контента на базе Voice-First**:
Исходный Reel -> Извлечение фактов -> Оценка пользы -> Поиск улик в Exa -> Безопасный вердикт -> Сценарий на 30-40 сек -> TTS (Edge-TTS) -> FFmpeg монтаж (5 сек оригинал + скриншот пруфа + голос + субтитры 9:16) -> QC проверка -> Готовое видео в Telegram/Instagram.
*Затраты на 1 ролик:* ~$0.005 (практически бесплатно), время сборки: 15-20 секунд.

### 4. три следующие задачи для реализации агенту:
1. **Задача 1 (БД и Схемы)**: Создать миграцию БД (`app/db/models_v2.py`) и обновить `app/worker/schemas.py` для поддержки новых сущностей (`SourceReel`, `Claim`, `CritiqueScript`, 7 типов вердиктов, `WorthinessScore`).
2. **Задача 2 (Сценарий & Вердикты)**: Реализовать модули `scoring.py` (оценка интересности) и `script_gen.py` (генерация 30-60 сек сценария с юридически безопасными формулировками вердикта).
3. **Задача 3 (TTS & FFmpeg Assembly & QC)**: Реализовать модули `tts.py`, `ffmpeg_builder.py` и `qc_checker.py` для авто-монтажа 9:16 ролика с закадровым голосом, субтитрами, скриншотами улик и авто-валидацией готового MP4.
