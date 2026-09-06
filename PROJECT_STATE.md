# REELS_BOT — canonical project state

Snapshot: 2026-09-06 23:25 ICT

Stage: **Multiple Output Variants Contract & Offline Evaluation — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `3629485f5b3b12a3a312e78daed877919d8c8e6f`.
- Documentation-only current HEAD: documentation-only handoff commit following release `3629485f5b3b12a3a312e78daed877919d8c8e6f`.
- Release commit: `3629485` — multiple output variants contract, deterministic renderers, declarative platform constraints, canonical content result representation, budget overflow safety semantics, offline evaluation replay suite, and production integration.
- Feature commit: `dabfe75fbe4897a01b55679ece093d490434f604`.
- Previous production release: `9c2b955eb2d7e297f64d7651fe2e61d022c5139b`.
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- The release SHA was built from a clean Git archive, pushed to `origin/main`, passed hosted CI (run 34044870005) and was deployed through `scripts/release.ps1 -Deploy`.
- Production image tag: `reels_bot:3629485f5b3b12a3a312e78daed877919d8c8e6f`.
- Running image digest: `sha256:ce9cde7e9ab706d0064192fc623748eecd25d6845d833b2ad5e90e46260710f0`.
- OCI revision label, bot runtime identity and worker runtime identity equal the release SHA `3629485f5b3b12a3a312e78daed877919d8c8e6f`. Image build timestamp: `2026-09-06T16:15:35Z`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video` and `reap_stale_jobs`.
- Post-canary database: 87 jobs (`DONE=53`, `ERROR=28`, `REVIEW_REQUIRED=6`) and 21 tasks.
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## Output variants contract and canonical representation

Prior to this stage, generation was strictly coupled to Telegram summary and channel formats. Slicing text to meet character budgets (e.g. `text[:280]`) introduces semantic truncation and risks stripping mandatory disclaimers.

`app/worker/output_variants.py` introduces a typed, deterministic contract:

1. **`CanonicalContentResult`**:
   - Single verified semantic representation extracted from analysis text, structured JSON, fact-check, router, priority, and language context.
   - Preserves title, core takeaway, detailed points, actionable steps, primary category, risk level, epistemic status, verified claims, disputed claims, mandatory disclaimers, and source URL.

2. **5 Platform Variants**:
   - `TLDR`: Compact takeaway + 1 actionable step / disclaimer (<= 280 chars).
   - `TELEGRAM_LONG`: Rich HTML-formatted comprehensive breakdown (<= 4096 chars).
   - `X_POST`: High-density microblog post (<= 280 chars).
   - `THREADS_POST`: Narrative conversational post with hook and bullet points (<= 500 chars).
   - `YOUTUBE_COMMUNITY`: Audience engagement post with discussion prompt (<= 2000 chars).

3. **Declarative Constraints (`VARIANT_CONSTRAINTS`)**:
   - Explicit character ranges, markup allowances, hashtag limits, and required sections per platform.
   - **No Blind Slicing**: If an `X_POST` cannot fit its core takeaway and mandatory disclaimers, it returns `status="NOT_RENDERABLE"` (`BUDGET_EXCEEDED`) rather than slicing mid-thought or dropping disclaimers.

4. **Epistemic Certainty and Safety Floors**:
   - Disputed claims are never asserted as fact in any variant.
   - For `HIGH` and `CRITICAL` risk categories, disclaimers are unconditionally included.
   - All deterministic renderers preserve factuality and claim status.

5. **Persistence and Delivery Semantics**:
   - Output variants bundle is persisted in `jobs.qa_reasons["output_variants"]` with version `variants_v1`.
   - Recorded in `jobs.delivery_status["output_variants"] = "SUCCEEDED"` (or `"FAILED"`).
   - Generated for both normal completions and `REVIEW_REQUIRED` states.
   - User delivery and channel delivery continue unchanged.

6. **Publishing Boundary**:
   - NO external auto-publishing to X, Threads, or YouTube is implemented or enabled. Contract rendering, validation, and database persistence only.

## Test and evaluation baseline

- Clean Git archive, network-isolated verification:
  - `ruff check .` -> **all checks passed (0 errors)**.
  - `pytest -m "not integration" -q` -> **181 passed, 1 deselected, 5 warnings** (with `testpaths = tests`).
- Replay evaluation suites:
  - **Output Variants Replay**: **8 cases across 40 evaluations** (`tests/fixtures/output_variants_eval.json`); render success rate, constraint compliance rate, epistemic preservation score, and budget compliance rate all **1.0** (0 violations).
  - **Multilingual Replay**: **13/13 cases**, 1.0 accuracy across 7 semantic groups.
  - **Content Router Replay**: **20/20 cases**, 1.0 calibration/policy accuracy, HIGH risk recall 1.0.
  - **Combined Router+Priority Replay**: **6/6 cases**, 1.0 policy accuracy, 0 risk floor violations.
- Hosted CI:
  - Feature commit `dabfe75fbe4897a01b55679ece093d490434f604`: CI run `34044643604` -> **success**.
  - Release commit `3629485f5b3b12a3a312e78daed877919d8c8e6f`: CI run `34044870005` -> **success**.

## Production canary

- Deployment: 2026-09-06 23:17 ICT from clean release SHA `3629485f5b3b12a3a312e78daed877919d8c8e6f`.
- Canary job: `00b72622-f461-43e8-8772-43082828bf92`.
- Reel URL: `https://www.instagram.com/reel/Dc1oN9IuLys/` (user `392046103`).
- Worker execution duration: 199.19 seconds.
- Video validation: 720x1280 MP4, 12 keyframes visual evidence.
- Transcription: `OK` via `gemini-3.5-transcribe`, no fallback.
- Language: Russian (`ru`), confidence `0.9908`, mixed `false`, translation not required.
- Router: `HOW_TO`, risk `MEDIUM`.
- Priority: `overall_score=0.565`, decision `AMBIGUOUS_CONTINUE`.
- Output Variants: 5 rendered (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`), `contract_version="variants_v1"`, `rendered_count=5`, `all_valid=true`, `not_renderable_count=0`.
- User Delivery: `SUCCEEDED` (video + analysis message delivered to Telegram user `392046103`).
- Channel Delivery: `SKIPPED_DUPLICATE` (url_hash `8d383e84f469b86c409f5b04d0d2fa47d78f83c14cffdbae454e6d3bba68e626` already published in job `ff845d15-8504-4ac1-ab65-149b874683fb`).
- Delivery status: `{"plan": "NOT_APPLICABLE", "user": "SUCCEEDED", "channel": "SKIPPED_DUPLICATE", "task_db": "NOT_APPLICABLE", "language": "SUCCEEDED", "priority": "SUCCEEDED", "output_variants": "SUCCEEDED"}`.
- Terminal job status: `DONE`, `error_text=None`.

## Implemented production architecture

The active production capability is a reactive Telegram-to-ARQ pipeline featuring:
- PostgreSQL state and Redis queue;
- Media download, ffprobe validation, and frame extraction;
- Gemini 3.5 transcription with source preservation;
- Versioned multilingual context (`LanguageContext`);
- Calibrated Content Router and multi-factor Prioritization;
- Claim extraction, Exa search verification, and business logic analysis;
- Canonical Content Result extraction (`CanonicalContentResult`);
- Typed deterministic platform renderers producing 5 output variants (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`);
- Declarative platform constraint validation (`VARIANT_CONSTRAINTS`);
- Independent user delivery, idempotent channel delivery, progress notifications, and stale job reaping;
- Complete release provenance enforcement via OCI labels and runtime verification.

Platform publication to X, Threads, and YouTube remains strictly manual/deferred. Content Factory, TTS, automated video re-assembly, and autonomous learning remain out of scope.

## Known debt

- **Platform Publishing Boundary**: Direct API integrations / auto-publishing for X, Threads, and YouTube Community are intentionally deferred. Variants are generated, validated, and stored in `qa_reasons["output_variants"]`.
- **Multilingual Output Policy**: Output policy remains fixed to Russian (`analysis_language=ru`, `user_output_language=ru`, `channel_output_language=ru`); per-user language preference store remains deferred.
- **Provider Language Metadata**: Transcription currently supplies no provider language metadata; lexical/script detection operates as normal path.
- **Translation In-Prompt**: Translation occurs inside analysis prompts rather than an independently scored translation stage.
- **Claim Position Linking**: Claim metadata reattaches after validation by list index; requires stable UUIDs if validators reorder claims.
- **External Query Fan-out**: Multilingual search query fan-out needs telemetry on latency/cost.
- **Historical Calibration Drift**: Model score drift is evaluated on static replay sets but not monitored dynamically in production.
- **Deprecation Warnings**: Pytest emits deprecation warnings for Pydantic V1 compat and `datetime.utcnow()`.
- **Pre-migration Postgres Volume**: Retained for rollback safety.

## Next stage

Recommended next product stage: **Content Factory Delivery & Distribution MVP** (`ROADMAP.md` Priority 4).
Implement distribution pipelines / adapters for selected platforms using the verified output variants contract, establish rate limits and credential management, and maintain human-in-the-loop review boundaries.
