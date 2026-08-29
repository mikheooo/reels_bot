# Reels Analyzer (reels_bot)

A Telegram bot and asynchronous worker pipeline that analyzes short-form video
(Instagram Reels, TikTok, YouTube Shorts): extracts transcripts and visual
evidence, checks claims against sources, and produces structured, evidence-
backed analysis results.

## What it does

- Accepts video URLs via a Telegram bot (aiogram) and queues them for
  processing (Redis + ARQ async task queue, PostgreSQL job/task store).
- Downloads the video, extracts the transcript, and runs structured LLM
  analysis (Gemini) over text and visual frames.
- Extracts claims, offers, monetization hypotheses, CTAs, promises, and
  reproducibility information into typed schemas (Pydantic).
- Fact-checks claims against web sources (Exa) with QA controls.
- Applies content scoring and publish-threshold prioritization
  (`app/worker/prioritization.py`).

## Architecture

```
app/
  bot/      aiogram Telegram bot: accepts URLs, reports job status
  core/     settings + URL normalization
  db/       SQLAlchemy async models (Job, Task) on PostgreSQL
  worker/   ARQ workers: download, transcript, visual analysis,
            structured analysis, fact-check, prioritization
tests/      pytest suite (mocked Gemini/Exa; no live network)
```

## Tech stack

Python, aiogram, ARQ + Redis, PostgreSQL (asyncpg/SQLAlchemy), Pydantic,
Google Gemini API, Exa, Docker / docker-compose.

## Run locally

```bash
cp .env.example .env        # fill BOT_TOKEN, GEMINI_API_KEY, DB_URL, EXA_API_KEY
docker-compose up --build -d
```

Or without Docker: install `requirements.txt`, provide the same `.env`,
run Redis and PostgreSQL, then start the bot and the ARQ worker.

## Tests

```bash
pytest tests -q
```

All external services (Gemini, Exa, Telegram, DB) are mocked in tests.

## Limitations / notes

- `.env` holds real keys and is git-ignored; `.env.example` documents the
  required variables. Multi-key rotation (`GEMINI_API_KEY_1..N`) is supported
  for free-tier quota spreading; an optional paid key can be set via
  `GEMINI_PAID_KEY`.
- Requires Docker or locally running Redis + PostgreSQL.
- Designed for personal single-user operation (admin user id comes from
  environment/configuration, not hard-coded).
