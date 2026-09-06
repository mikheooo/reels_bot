# REELS_BOT — canonical project state

Snapshot: 2026-09-07 01:25 ICT

Stage: **Publishing Orchestration & External Platform Connectors — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `f32e286bf8d939f807c18ca9fd933952ff7642a5`.
- Documentation-only current HEAD: documentation-only handoff commit following release `f32e286bf8d939f807c18ca9fd933952ff7642a5`.
- Release commit: `f32e286` — feat(publish): implement publishing orchestration and external platform connectors.
- Previous accepted baseline: `9465d3beb25216dba7b33e5f3e2878d87339df51` (documentation HEAD `67f3a5fa595efa91410217c45a5400a4963575c0`).
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- Hosted CI verification:
  - Runtime commit `f32e286bf8d939f807c18ca9fd933952ff7642a5`: CI run `34051093765` -> **SUCCESS** (37s).
- Production image tag: `reels_bot:f32e286bf8d939f807c18ca9fd933952ff7642a5`.
- Running image digest: `sha256:fba440cf3c093099258080b566c0f0c43e94dcbea1dd898613a7c1881705e236`.
- Image build timestamp: `2026-09-06T18:15:38Z`.
- Runtime provenance: verified via `scripts/show_provenance.ps1`. OCI revision label, bot runtime identity, and worker runtime identity all match `f32e286bf8d939f807c18ca9fd933952ff7642a5`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot, and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video` and `reap_stale_jobs`.
- Post-canary database: `c494df78-1926-4c24-9de9-237dbaaf67a9` verified (`status=DONE`, `user=SUCCEEDED`, `content_package=CREATED`, `output_variants=SUCCEEDED`), `ContentPackageModel` (`f837d441-4c40-4be7-8a0b-e8df2ff4abb4`, `DELIVERED`), 3 `PublicationIntentModel` rows (`SUPPORTED_NOT_CONFIGURED` for X and Threads, `MANUAL_EXPORT_READY` for YouTube Community).
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## Publishing Orchestration & External Platform Connectors

Prior to this stage, external distribution targets were Level A (`APPROVED_NOT_CONNECTED`) and had no official API integrations, intent tracking, or retry capabilities.
`app/worker/connectors.py`, `app/worker/content_package.py`, `app/worker/tasks.py`, `app/bot/package_handlers.py`, and `app/db/` advance the pipeline to **Level B Publication Boundary**:

1. **Official Provider API Integrations**:
   - **X API v2**: `POST /2/tweets` with OAuth 1.0a User Context, `GET /2/tweets/:id` lookup, `DELETE /2/tweets/:id` cleanup, and ambiguous timeout reconciliation via recent user timeline search.
   - **Meta Threads Graph API**: Two-step flow (`POST /{user-id}/threads` media container creation followed by `POST /{user-id}/threads_publish`), lookup via `GET /{threads-media-id}`, and ambiguous timeout reconciliation via recent threads lookup.
   - **YouTube Community Truthful Classification**: `UNSUPPORTED_OFFICIAL_API`. In accordance with official Google API documentation, legacy channel bulletins are retired and no public Community Post publishing endpoint exists. Zero browser automation or scraping is used; content is rendered copy-ready as `READY_FOR_MANUAL_PUBLISH`.

2. **Persistent `PublicationIntent` Contract & Table**:
   - Relational `publication_intents` table tracks publication lifecycle: `PENDING -> IN_FLIGHT -> SUCCEEDED / FAILED / DELIVERY_UNKNOWN / APPROVAL_STALE / SUPPORTED_NOT_CONFIGURED / MANUAL_EXPORT_READY`.
   - Separates technical callback execution from asynchronous worker publication.

3. **Core Safety Invariants**:
   - **Stable Publication Idempotency**: `publication_key` (`{package_id}:{target}:{variant}:{payload_hash[:16]}`) is immutable across retries. Technical attempts increment `attempt_id` on `content_deliveries` (`retry != new publication`).
   - **Stale Approval Protection**: Exact SHA-256 payload hash verification prevents publishing variants that were modified or regenerated after owner approval (`APPROVAL_STALE`).
   - **Ambiguous Result Reconciliation**: Network timeouts transition to `DELIVERY_UNKNOWN` and trigger provider feed lookups to recover post IDs before permitting retries.
   - **Provider Identity Guard**: Validates authenticated account ID against expected configuration before publishing.
   - **Connector-Disabled Default**: Production operates smoothly without credentials; missing keys yield `SUPPORTED_NOT_CONFIGURED` without failing jobs or blocking Telegram user delivery.
   - **Zero Secret Logging**: All logs and exceptions sanitize Authorization headers, tokens, and secrets via `sanitize_sensitive_text()`.

4. **Bot Review UI Evolution (`app/bot/package_handlers.py`)**:
   - Menu presents real-time platform capability badges: `[CONNECTED]`, `[НЕ НАСТРОЕН]`, `[РУЧНОЙ ЭКСПОРТ]`.
   - YouTube Community provides a copy-ready text view without any misleading publish action.
   - Strict owner authorization enforced: non-owners receive permission denials on all callbacks.

## Test and evaluation baseline

- Clean Git archive, network-isolated verification:
  - `ruff check .` -> **all checks passed (0 errors)**.
  - `pytest -m "not integration" -q` -> **312 passed, 1 skipped, 7 deselected, 17 warnings** (total 320 collected items).
  - 100% of previous test collection preserved (291 items) + 29 new comprehensive connector tests in `tests/test_connectors.py`.
- Replay evaluation suites (all 6 passing at 1.0):
  - **Connector Replay**: **13 deterministic scenarios** (`tests/fixtures/connector_eval.json`); unauthorized publications: `0`, duplicate logical publications: `0`, stale approvals published: `0`, fake successes: `0`, retry correctness: `1.0`, all gates passed: `True`.
  - **Distribution Replay**: **10 scenarios across 10 evaluations** (`tests/fixtures/distribution_eval.json`); lifecycle correctness: `1.0`, approval enforcement: `1.0`, target status accuracy: `1.0`, unauthorized approval violations: `0`, duplicate publication violations: `0`, factual mutation violations: `0`, risk warning violations: `0`, idempotency violations: `0`, all gates passed: `True`.
  - **Output Variants Replay**: **8 cases across 40 evaluations** (`tests/fixtures/output_variants_eval.json`); format validity: `1.0`, length compliance: `1.0`, mandatory fact recall: `1.0`, forbidden fact rate: `0.0`, uncertainty preservation: `1.0`, risk warning preservation: `1.0`, language policy accuracy: `1.0`, not renderable accuracy: `1.0`, platform limit violations: `0`, risk warning violations: `0`.
  - **Multilingual Replay**: **13/13 cases**, `1.0` accuracy across language detection, router equivalence, priority band equivalence, output contract accuracy, and 0 risk floor violations.
  - **Content Router Replay**: **20/20 cases**, `1.0` calibration/policy accuracy, HIGH risk recall `1.0`.
  - **Combined Router+Priority Replay**: **6/6 cases**, `1.0` policy accuracy, 0 risk floor violations.

## Verified live production canary

- Deployment: 2026-09-07 01:15 ICT from clean release SHA `f32e286bf8d939f807c18ca9fd933952ff7642a5`.
- Canary job: `c494df78-1926-4c24-9de9-237dbaaf67a9`.
- Reel URL: `https://www.instagram.com/reel/Dc1oN9IuLys/` (user `392046103`).
- Video validation: 720x1280 MP4, 12 keyframes visual evidence.
- Transcription: `OK` via `gemini-3.5-transcribe` (2530 chars), no fallback.
- Language: Russian (`ru`), confidence `0.9908`, mixed `false`, translation not required.
- Router: `HOW_TO`, risk `MEDIUM`.
- Priority: `overall_score=0.522`, decision `AMBIGUOUS_CONTINUE`.
- Output Variants: 5 rendered (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`), `all_valid=true`.
- User Delivery: `SUCCEEDED` (external_id `652`, `TELEGRAM_LONG`).
- Channel Delivery: `SKIPPED_DUPLICATE` (dedup preserved).
- Content Package Created: ID `f837d441-4c40-4be7-8a0b-e8df2ff4abb4`, contract version `content_package_v1`.
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
