# REELS_BOT — canonical project state

Snapshot: 2026-09-06 23:55 ICT

Stage: **Multiple Output Variants Contract & Offline Evaluation — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `1913afd1187c8a710131bd0715170cd4856cf8fe`.
- Documentation-only current HEAD: documentation-only handoff commit following release `1913afd1187c8a710131bd0715170cd4856cf8fe`.
- Release commit: `1913afd` — fix(tasks): persist user_delivery_text as job analysis_text on completion.
- Preceding runtime commits:
  - `679a076` — fix(output_variants): accept video_url in CanonicalContentResult and build_canonical_content_result.
  - `af5071b` — feat(delivery): integrate TELEGRAM_LONG into production Telegram delivery (Option A) and restore full pytest collection.
- Feature commit: `dabfe75fbe4897a01b55679ece093d490434f604`.
- Previous accepted baseline: `6b8a94821eac884a9135426fc9b90c2748963f2a`.
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- Hosted CI verification:
  - Runtime commit `1913afd1187c8a710131bd0715170cd4856cf8fe`: CI run `34046513482` -> **SUCCESS** (39s).
  - Runtime commit `679a0763a4b3f05ef804ee2a32d3b27efca984c7`: CI run `34046230332` -> **SUCCESS** (41s).
  - Runtime commit `af5071b449257e66a5e15f70fa98d3ae8397f173`: CI run `34045914848` -> **SUCCESS** (44s).
- Production image tag: `reels_bot:1913afd1187c8a710131bd0715170cd4856cf8fe`.
- Running image digest: `sha256:9d46b010c6a8784288fa1bad9ea6137c9c37b39972bd0140388a1e18461cf34f`.
- Image build timestamp: `2026-09-06T16:47:19Z`.
- Runtime provenance: verified via `scripts/show_provenance.ps1`. OCI revision label, bot runtime identity, and worker runtime identity all match `1913afd1187c8a710131bd0715170cd4856cf8fe`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video` and `reap_stale_jobs`.
- Post-canary database: 89 jobs (`DONE=55`, `ERROR=28`, `REVIEW_REQUIRED=6`) and 21 tasks.
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

5. **Persistence and Delivery Semantics (Option A)**:
   - Telegram user delivery is directly bound to `output_variants["TELEGRAM_LONG"].text`.
   - Output variants bundle is persisted in `jobs.qa_reasons["output_variants"]` with version `variants_v1`.
   - Recorded in `jobs.delivery_status["output_variants"] = "SUCCEEDED"` (or `"FAILED"`).
   - Generated for both normal completions and `REVIEW_REQUIRED` states.
   - User delivery text is persisted as `jobs.analysis_text` on completion.

6. **Publishing Boundary**:
   - NO external auto-publishing to X, Threads, or YouTube is implemented or enabled. Contract rendering, validation, and database persistence only.

## Closure verification gates

### BLOCKER 1 — Pytest collection regression diagnosis and resolution

- **Root Cause Diagnosis**: During initial setup of the output variants stage, `testpaths = tests` was placed into `pytest.ini`. This restricted pytest discovery strictly to the `tests/` directory, inadvertently omitting 82 tests residing in root test files (`test_visual_evidence.py` [35], `test_structured_analysis_contract.py` [12], `test_task_title_extraction.py` [9], `test_business_check.py` [9], `test_new_data_verification.py` [4], `test_phase1_extended.py` [2], `test_phase2_2.py` [1], `test_phase2_3.py` [1], `test_phase3.py` [1], `test_qa_audit.py` [1], `test_reels.py` [1], `test_relative_dates.py` [1], `test_validate.py` [1], `test_validate_direct.py` [1], `test_validate_task2.py` [1], `test_worker_keys.py` [1], `test_integration.py` [1]).
- **Resolution**: Removed `testpaths = tests` from `pytest.ini`, restoring discovery across the entire repository.
- **Collection Comparison**:
  - Previous accepted baseline (`6b8a948`): 245 collected (`237 passed, 1 skipped, 7 deselected`).
  - Current verified baseline (`1913afd`): **271 collected** (`263 passed, 1 skipped, 7 deselected, 17 warnings`).
  - 100% of previous tests preserved (237/237) plus 26 new contract/regression tests.

### BLOCKER 2 — Production Telegram delivery integration (Option A)

- **Architecture**: Option A implemented. Telegram user delivery is directly bound to `output_variants["TELEGRAM_LONG"].text`.
- **Integration Helper**: `resolve_telegram_delivery_payload(output_variants_payload, legacy_analysis)` in `app/worker/output_variants.py`:
  - When `TELEGRAM_LONG` is successfully rendered (`status == "RENDERED"`), its text is used for user delivery (`mode="TELEGRAM_LONG"`), and `jobs.analysis_text` is updated to this canonical rendered text.
  - When `TELEGRAM_LONG` fails to render (`FAILED` or `NOT_RENDERABLE`), the pipeline cleanly falls back to legacy analysis text (`mode="FALLBACK_ANALYSIS"`), marking `user: "SUCCEEDED_FALLBACK"` and `output_variants: "FAILED:TELEGRAM_LONG_..."`. The job terminal status becomes `PARTIAL` (never a false `DONE`).
  - Failures in optional variants (`X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`) do not block user delivery or prevent `DONE`.
- **Persistence**: `analysis_text` on completion is persisted as `user_delivery_text`, ensuring repeat user requests via `handlers.py` also return the canonical `TELEGRAM_LONG` representation.
- **Regression Suite**: 7 unit regression tests added in `tests/test_output_variants.py`:
  1. `test_telegram_user_delivery_bound_to_telegram_long`: Verifies `user_delivery_text == TELEGRAM_LONG.text`.
  2. `test_telegram_long_rendering_failure_has_correct_completion_semantics`: Verifies fallback and terminal `PARTIAL` (no false `DONE`).
  3. `test_optional_x_failure_does_not_break_user_delivery`: Optional X failure preserves `DONE`.
  4. `test_optional_threads_failure_does_not_break_user_delivery`: Optional Threads failure preserves `DONE`.
  5. `test_optional_youtube_community_failure_does_not_break_user_delivery`: Optional YouTube failure preserves `DONE`.
  6. `test_legacy_telegram_behavior_not_bypassing_output_variant`: Verifies canonical `TELEGRAM_LONG` content replaces legacy text.
  7. `test_build_canonical_content_result_accepts_video_url`: Verifies `video_url` propagation into `CanonicalContentResult`.

## Test and evaluation baseline

- Clean Git archive, network-isolated verification:
  - `ruff check .` -> **all checks passed (0 errors)**.
  - `pytest -m "not integration" -q` -> **263 passed, 1 skipped, 7 deselected, 17 warnings** (total 271 collected items).
- Replay evaluation suites (all 4 passing at 1.0):
  - **Output Variants Replay**: **8 cases across 40 evaluations** (`tests/fixtures/output_variants_eval.json`); format validity: `1.0`, length compliance: `1.0`, mandatory fact recall: `1.0`, forbidden fact rate: `0.0`, uncertainty preservation: `1.0`, risk warning preservation: `1.0`, language policy accuracy: `1.0`, not renderable accuracy: `1.0`, platform limit violations: `0`, risk warning violations: `0`.
  - **Multilingual Replay**: **13/13 cases**, `1.0` accuracy across language detection, router equivalence, priority band equivalence, output contract accuracy, and 0 risk floor violations.
  - **Content Router Replay**: **20/20 cases**, `1.0` calibration/policy accuracy, HIGH risk recall `1.0`.
  - **Combined Router+Priority Replay**: **6/6 cases**, `1.0` policy accuracy, 0 risk floor violations.

## Verified live production canary

- Deployment: 2026-09-06 23:47 ICT from clean release SHA `1913afd1187c8a710131bd0715170cd4856cf8fe`.
- Canary job: `6e73d06f-2a90-434b-8704-f1000063541c`.
- Reel URL: `https://www.instagram.com/reel/Dc1oN9IuLys/` (user `392046103`).
- Worker execution duration: 171.07 seconds.
- Video validation: 720x1280 MP4, 12 keyframes visual evidence.
- Transcription: `OK` via `gemini-3.5-transcribe` (2530 chars), no fallback.
- Language: Russian (`ru`), confidence `0.9908`, mixed `false`, translation not required.
- Router: `HOW_TO`, risk `MEDIUM`.
- Priority: `overall_score=0.560`, decision `AMBIGUOUS_CONTINUE`.
- Output Variants: 5 rendered (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`), `contract_version="variants_v1"`, `rendered_count=5`, `all_valid=true`, `not_renderable_count=0`.
- Delivery mode: `TELEGRAM_LONG` (worker log: `Sending to TG user 392046103 via TELEGRAM_LONG`).
- User Delivery: `SUCCEEDED` (video + canonical `TELEGRAM_LONG` analysis message delivered to Telegram user `392046103`).
- Channel Delivery: `SKIPPED_DUPLICATE` (url_hash `8d383e84f469b86c409f5b04d0d2fa47d78f83c14cffdbae454e6d3bba68e626` already published in job `ff845d15-8504-4ac1-ab65-149b874683fb`).
- Delivery status: `{"plan": "NOT_APPLICABLE", "user": "SUCCEEDED", "channel": "SKIPPED_DUPLICATE", "task_db": "NOT_APPLICABLE", "language": "SUCCEEDED", "priority": "SUCCEEDED", "output_variants": "SUCCEEDED"}`.
- Terminal job status: `DONE`, `error_text=None`.
- Database row `analysis_text` verified: contains the canonical rendered `TELEGRAM_LONG` Markdown.

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
- Option A Telegram user delivery bound to `TELEGRAM_LONG` with robust fallback semantics;
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
