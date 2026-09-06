# REELS_BOT — canonical project state

Snapshot: 2026-09-07 02:00 ICT

Stage: **External Connector Live Enablement & Controlled Publication Validation — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `29fefbcfe06ec49024758cd5d196ac0a6056fc99`.
- Documentation-only current HEAD: documentation-only handoff commit following release `29fefbcfe06ec49024758cd5d196ac0a6056fc99`.
- Release commit: `29fefbc` — fix(publishing): canonicalize X API host and provider-driven throttling.
- Previous accepted baseline: `f32e286bf8d939f807c18ca9fd933952ff7642a5` (documentation HEAD `4c2b7b0dc4642dcd4a63dbe65b74a4dd4a9ddaf7`).
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- Hosted CI verification:
  - Runtime commit `29fefbcfe06ec49024758cd5d196ac0a6056fc99`: CI run `34053120779` -> **SUCCESS** (42s).
- Production image tag: `reels_bot:29fefbcfe06ec49024758cd5d196ac0a6056fc99`.
- Running image digest: `sha256:752afd4c9d49fbbaeb5154f958bb29b6fd20b7182451851e0652782d5a62e5cd`.
- Image build timestamp: `2026-09-06T18:54:19Z`.
- Runtime provenance: verified via `scripts/show_provenance.ps1`. OCI revision label, bot runtime identity, and worker runtime identity all match `29fefbcfe06ec49024758cd5d196ac0a6056fc99`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot, and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video` and `reap_stale_jobs`.
- Post-canary database: `e0cafb55-2df8-480f-b4c1-629211ce1245` verified (`status=DONE`, `user=SUCCEEDED`, `content_package=CREATED`, `output_variants=SUCCEEDED`), `ContentPackageModel` (`7f390083-11ab-4fe5-babb-85ad3f1b6ea4`, `DELIVERED`), 3 `PublicationIntentModel` rows (`SUPPORTED_NOT_CONFIGURED` for X and Threads, `MANUAL_EXPORT_READY` for YouTube Community).
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## External Connector Live Enablement & Controlled Publication Validation

This stage verified, canonicalized, and hardened external platform distribution connectors:

1. **X Endpoint Canonicalization**:
   - Runtime, test suite, and documentation strictly adhere to canonical X API v2 base URL `https://api.x.com`:
     - Create: `POST https://api.x.com/2/tweets`
     - Lookup: `GET https://api.x.com/2/tweets/{id}`
     - Delete: `DELETE https://api.x.com/2/tweets/{id}`
     - Identity: `GET https://api.x.com/2/users/me`
     - Reconciliation: `GET https://api.x.com/2/users/me/tweets?max_results=5&tweet.fields=text`
   - Zero occurrences of legacy `api.twitter.com` remain in runtime, tests, or documentation.

2. **Rate-Limit Policy Cleanup (Header-First Throttling)**:
   - Eliminated any static hardcoded rate-limit invariants (such as `200 posts / 15 min`).
   - Implemented `RateLimitInfo` and `extract_rate_limit_info()` in `app/worker/connectors.py` to extract provider response headers:
     - `x-rate-limit-limit`
     - `x-rate-limit-remaining`
     - `x-rate-limit-reset` (UTC epoch timestamp converted dynamically to seconds until reset)
     - `Retry-After` (seconds)
   - Extended `PublicationResult` with `rate_limit_info` and `retry_after_seconds`.
   - Updated `execute_publication_intents()` in `app/worker/tasks.py` to schedule `next_retry_at` dynamically using `pub_result.retry_after_seconds` when provided by headers, falling back safely to `settings.publish_retry_backoff_seconds` only when headers are absent.
   - Standardized Meta Threads with identical dynamic `Retry-After` header extraction, using rolling 250 posts/24h as documentation fallback only.

3. **Credential Setup Audit & Identity Guards**:
   - Audited `.env` and environment variables:
     - X API credentials: `NOT_CONFIGURED`
     - Threads credentials: `NOT_CONFIGURED`
   - Zero secrets committed, logged, or recorded in project documentation.
   - Provider identity guards verified: mismatched authenticated user ID or handle cleanly triggers `ACCOUNT_IDENTITY_MISMATCH` and blocks publication without dispatch.

4. **Truthful Unconfigured Behavior (Phase 9)**:
   - Verified that missing credentials evaluate truthfully to `SUPPORTED_NOT_CONFIGURED`.
   - Telegram delivery succeeds without disruption.
   - No fake publication records or fake IDs created (`fake_success_count = 0`).
   - Duplicate publications count = `0`.
   - Unauthorized publications count = `0`.

5. **YouTube Community Boundary Preservation**:
   - Maintained strict classification as `UNSUPPORTED_OFFICIAL_API` / `READY_FOR_MANUAL_PUBLISH`.
   - Zero browser automation, scraping, or private APIs. Telegram UI contains copy-ready text view with no misleading publish action.

6. **Ambiguous Result Reconciliation Safety**:
   - Replay and unit tests verify that ambiguous network timeouts transition to `DELIVERY_UNKNOWN` and trigger provider feed lookups (`/2/users/me/tweets`) to recover post IDs before permitting retries, preventing duplicate external posts.

## Test and evaluation baseline

- Clean Git archive, network-isolated verification:
  - `ruff check .` -> **all checks passed (0 errors)**.
  - `pytest -m "not integration" -q` -> **318 passed, 7 deselected, 17 warnings in 9.29s** (CI: **317 passed, 1 skipped, 7 deselected, 17 warnings in 5.93s**; total 325 collected items >= 320 baseline).
  - Deterministic skipped test explanation: `test_visual_evidence.py::test_extract_keyframes_timestamp_start_middle_end` skips cleanly in CI/clean Git archives when no media file is present (`pytest.skip("No test video available")`). In local workspace where `test_vid.mp4` exists, it executes and passes, producing 318 passed.
  - 100% of previous test collection preserved + 5 new comprehensive connector & rate-limit tests in `tests/test_connectors.py` (34 total connector tests):
    1. `test_30_x_rate_limit_info_extraction`
    2. `test_31_threads_rate_limit_header_extraction`
    3. `test_32_retry_after_delay_orchestration`
    4. `test_33_canonical_x_endpoints`
    5. `test_34_distribution_replay_suite_passes`
  - Hermetic test environment: isolated ambient `GEMINI_PAID_KEY` from retry unit test in `tests/test_gemini_429_retry.py`.
- Replay evaluation suites (all 6 passing at 1.0):
  - **Connector Replay**: **13 deterministic scenarios** (`tests/fixtures/connector_eval.json`); unauthorized publications: `0`, duplicate logical publications: `0`, stale approvals published: `0`, fake successes: `0`, retry correctness: `1.0`, all gates passed: `True`.
  - **Distribution Replay**: **10 scenarios across 10 evaluations** (`tests/fixtures/distribution_eval.json`); lifecycle correctness: `1.0`, approval enforcement: `1.0`, target status accuracy: `1.0`, unauthorized approval violations: `0`, duplicate publication violations: `0`, factual mutation violations: `0`, risk warning violations: `0`, idempotency violations: `0`, all gates passed: `True`.
  - **Output Variants Replay**: **8 cases across 40 evaluations** (`tests/fixtures/output_variants_eval.json`); format validity: `1.0`, length compliance: `1.0`, mandatory fact recall: `1.0`, forbidden fact rate: `0.0`, uncertainty preservation: `1.0`, risk warning preservation: `1.0`, language policy accuracy: `1.0`, not renderable accuracy: `1.0`, platform limit violations: `0`, risk warning violations: `0`.
  - **Multilingual Replay**: **13/13 cases**, `1.0` accuracy across language detection, router equivalence, priority band equivalence, output contract accuracy, and 0 risk floor violations.
  - **Content Router Replay**: **20/20 cases**, `1.0` calibration/policy accuracy, HIGH risk recall `1.0`.
  - **Combined Router+Priority Replay**: **6/6 cases**, `1.0` policy accuracy, 0 risk floor violations.

## Verified live production canary

- Deployment: 2026-09-07 01:54 ICT from clean release SHA `29fefbcfe06ec49024758cd5d196ac0a6056fc99`.
- Canary job: `e0cafb55-2df8-480f-b4c1-629211ce1245`.
- Reel URL: `https://www.instagram.com/reel/Dc1oN9IuLys/` (user `392046103`).
- Video validation: 720x1280 MP4, 12 keyframes visual evidence.
- Transcription: `OK` via `gemini-3.5-transcribe` (2530 chars), no fallback.
- Language: Russian (`ru`), confidence `0.9908`, mixed `false`, translation not required.
- Router: `HOW_TO`, risk `MEDIUM`.
- Priority: `overall_score=0.522`, decision `AMBIGUOUS_CONTINUE`.
- Output Variants: 5 rendered (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`), `all_valid=true`.
- User Delivery: `SUCCEEDED` (external_id `652`, `TELEGRAM_LONG`).
- Channel Delivery: `SKIPPED_DUPLICATE` (dedup preserved).
- Content Package Created: ID `7f390083-11ab-4fe5-babb-85ad3f1b6ea4`, contract version `content_package_v1`.
- Security Guard Verified: Unauthorized user `999999999` blocked with `UnauthorizedApprovalError`.
- Owner Approval & Intent Execution:
  - Owner `392046103` approved; `publication_intents` created with stable `publication_key` and `payload_hash`.
  - Connector-disabled default validated: X and Threads evaluated to `SUPPORTED_NOT_CONFIGURED` without failing the job.
  - YouTube Community evaluated to `READY_FOR_MANUAL_PUBLISH` (`MANUAL_EXPORT_READY`).
  - Stale approval guard validated: Tampered hash intercepted, status set to `APPROVAL_STALE`, dispatch aborted.
  - Terminal package status reached: `DELIVERED`.

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
- Telegram user delivery bound to `TELEGRAM_LONG` with robust fallback semantics;
- Content Package management (`ContentPackage`, `DistributionTarget`, `DeliveryRecord`);
- Level B Publication Boundary (`PublicationConnector`, `XConnector`, `ThreadsConnector`, `YouTubeCommunityConnector`);
- Canonical X API v2 endpoints (`https://api.x.com`);
- Dynamic header-driven rate-limit model (`x-rate-limit-*`, `Retry-After`);
- Persistent `PublicationIntent` lifecycle with stale approval guard and ambiguous outcome reconciliation;
- Platform review UI with live capability badges and copy-ready YouTube manual export;
- Complete release provenance enforcement via OCI labels and runtime verification.

## Known debt

- **Live Social Credentials**: Production environment operates in `SUPPORTED_NOT_CONFIGURED` mode by default until live brand OAuth tokens are provisioned into `.env`.
- **Media Upload to X / Threads**: Connectors currently implement text-only publishing; media (video/image) attachments remain deferred.
- **Multilingual Output Policy**: Output policy remains fixed to Russian (`analysis_language=ru`, `user_output_language=ru`, `channel_output_language=ru`); per-user language preference store remains deferred.
- **Provider Language Metadata**: Transcription currently supplies no provider language metadata; lexical/script detection operates as normal path.
- **Translation In-Prompt**: Translation occurs inside analysis prompts rather than an independently scored translation stage.
- **Claim Position Linking**: Claim metadata reattaches after validation by list index; requires stable UUIDs if validators reorder claims.
- **External Query Fan-out**: Multilingual search query fan-out needs telemetry on latency/cost.
- **Historical Calibration Drift**: Model score drift is evaluated on static replay sets but not monitored dynamically in production.
- **Deprecation Warnings**: Pytest emits deprecation warnings for Pydantic V1 compat and `datetime.utcnow()`.
- **Pre-migration Postgres Volume**: Retained for rollback safety.

## Next stage

Recommended next product stage: **Post-Publish Audit & Telemetry** (Roadmap Priority 6).
Automate verification of published posts across platforms, gather performance engagement metrics, and detect external content deletions/edits.
