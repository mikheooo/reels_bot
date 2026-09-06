# REELS_BOT — canonical project state

Snapshot: 2026-09-07 00:30 ICT

Stage: **Content Factory Delivery & Distribution MVP — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `9465d3beb25216dba7b33e5f3e2878d87339df51`.
- Documentation-only current HEAD: documentation-only handoff commit following release `9465d3beb25216dba7b33e5f3e2878d87339df51`.
- Release commit: `9465d3b` — fix(distribution): ensure naive UTC datetimes for PostgreSQL asyncpg storage.
- Preceding feature commit: `f1adecf` — feat(distribution): implement Content Factory Delivery & Distribution MVP.
- Previous accepted baseline: `1913afd1187c8a710131bd0715170cd4856cf8fe` (documentation HEAD `c867fa40995daab2bc30c8e89524f2b8cc13adba`).
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- Hosted CI verification:
  - Runtime commit `9465d3beb25216dba7b33e5f3e2878d87339df51`: CI run `34048388590` -> **SUCCESS** (45s).
  - Runtime commit `f1adecf2e08c98232098b0963bc00d7fd2c7bf7f`: CI run `34047947720` -> **SUCCESS** (41s).
- Production image tag: `reels_bot:9465d3beb25216dba7b33e5f3e2878d87339df51`.
- Running image digest: `sha256:a48f74bfa3306e5f2be4a4e33af74fb98eef4802f596e5dc0397d3dcee864e53`.
- Image build timestamp: `2026-09-06T17:23:09Z`.
- Runtime provenance: verified via `scripts/show_provenance.ps1`. OCI revision label, bot runtime identity, and worker runtime identity all match `9465d3beb25216dba7b33e5f3e2878d87339df51`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video` and `reap_stale_jobs`.
- Post-canary database: 90 jobs (`DONE=56`, `ERROR=28`, `REVIEW_REQUIRED=6`), 21 tasks, 1 content package, 5 delivery records.
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## Content Factory Delivery & Distribution MVP

Prior to this stage, output variants were render-only and not tracked as unified packages or gated by approval lifecycles.
`app/worker/content_package.py`, `app/bot/package_handlers.py`, and `app/db/` introduce typed, state-managed content delivery:

1. **`ContentPackage` Data Contract (`content_package_v1`)**:
   - Encapsulates `package_id`, `job_id`, `source_url`, `canonical_content`, `output_variants`, `distribution_targets`, `approval_state`, and `delivery_records`.
   - Explicit lifecycle state machine (`VALID_TRANSITIONS`): `GENERATED -> REVIEW_REQUIRED / PARTIALLY_DELIVERED -> APPROVED -> DELIVERED / REJECTED`.

2. **Core Architectural Principle (`generation != approval != publication`)**:
   - Generation produces candidate variants from `CanonicalContentResult`.
   - Internal Telegram delivery executes automatically, transitioning package to `PARTIALLY_DELIVERED`.
   - External platforms (`X`, `THREADS`, `YOUTUBE_COMMUNITY`) are marked `PENDING_APPROVAL` with `required_approval=True`.
   - Approval by the owner transitions external targets to `APPROVED_NOT_CONNECTED` (Level A publication boundary: zero mock or unauthorized HTTP calls).

3. **Zero Factual Mutation Invariant**:
   - Variant regeneration (`regenerate_variant_from_canonical`) reconstructs variants strictly from the immutable `CanonicalContentResult` without re-interpreting transcript or mutating facts.

4. **Bot Review & Approval Controls (`pkg:*`)**:
   - `pkg:menu`: Shows real-time target statuses and approval buttons.
   - `pkg:view`: Inspects formatted text of any variant.
   - `pkg:approve_all`: Approves all pending external targets.
   - `pkg:reject`: Rejects external publication.
   - `pkg:regen`: Re-renders specific variant from canonical facts.
   - Strict authorization: `callback.from_user.id == job.user_id` enforced on all actions.

5. **Database Storage & Idempotency**:
   - `content_packages`: Tracks package lifecycle, targets, and canonical JSON.
   - `content_deliveries`: Logs delivery attempts with unique constraint on `idempotency_key` (`{package_id}:{target}:{variant}:{attempt_id}`).

## Test and evaluation baseline

- Clean Git archive, network-isolated verification:
  - `ruff check .` -> **all checks passed (0 errors)**.
  - `pytest -m "not integration" -q` -> **283 passed, 1 skipped, 7 deselected, 17 warnings** (total 291 collected items).
  - 100% of previous test collection preserved (271 items) + 20 new comprehensive content package tests in `tests/test_content_package.py`.
- Replay evaluation suites (all 5 passing at 1.0):
  - **Distribution Replay**: **10 scenarios across 10 evaluations** (`tests/fixtures/distribution_eval.json`); lifecycle correctness: `1.0`, approval enforcement: `1.0`, target status accuracy: `1.0`, unauthorized approval violations: `0`, duplicate publication violations: `0`, factual mutation violations: `0`, risk warning violations: `0`, idempotency violations: `0`, all gates passed: `True`.
  - **Output Variants Replay**: **8 cases across 40 evaluations** (`tests/fixtures/output_variants_eval.json`); format validity: `1.0`, length compliance: `1.0`, mandatory fact recall: `1.0`, forbidden fact rate: `0.0`, uncertainty preservation: `1.0`, risk warning preservation: `1.0`, language policy accuracy: `1.0`, not renderable accuracy: `1.0`, platform limit violations: `0`, risk warning violations: `0`.
  - **Multilingual Replay**: **13/13 cases**, `1.0` accuracy across language detection, router equivalence, priority band equivalence, output contract accuracy, and 0 risk floor violations.
  - **Content Router Replay**: **20/20 cases**, `1.0` calibration/policy accuracy, HIGH risk recall `1.0`.
  - **Combined Router+Priority Replay**: **6/6 cases**, `1.0` policy accuracy, 0 risk floor violations.

## Verified live production canary

- Deployment: 2026-09-07 00:23 ICT from clean release SHA `9465d3beb25216dba7b33e5f3e2878d87339df51`.
- Canary job: `a7c9b360-7971-4f3c-b816-fe4ba4b79385`.
- Reel URL: `https://www.instagram.com/reel/Dc1oN9IuLys/` (user `392046103`).
- Video validation: 720x1280 MP4, 12 keyframes visual evidence.
- Transcription: `OK` via `gemini-3.5-transcribe` (2530 chars), no fallback.
- Language: Russian (`ru`), confidence `0.9908`, mixed `false`, translation not required.
- Router: `HOW_TO`, risk `MEDIUM`.
- Priority: `overall_score=0.522`, decision `AMBIGUOUS_CONTINUE`.
- Output Variants: 5 rendered (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`), `contract_version="variants_v1"`, `rendered_count=5`, `all_valid=true`, `not_renderable_count=0`.
- Delivery mode: `TELEGRAM_LONG` (worker log: `Sending to TG user 392046103 via TELEGRAM_LONG`).
- User Delivery: `SUCCEEDED` (external_id `648`).
- Channel Delivery: `SKIPPED_DUPLICATE` (dedup preserved, already published in job `ff845d15-8504-4ac1-ab65-149b874683fb`).
- Content Package Created: ID `b4f5a562-1cbc-4246-b2d0-71aeaa94df68`, contract version `content_package_v1`.
- Initial Package Status: `PARTIALLY_DELIVERED` (Telegram delivered, external platforms pending review).
- Review & Approval Test: Owner approval executed; external targets `X`, `THREADS`, `YOUTUBE_COMMUNITY` moved to `APPROVED_NOT_CONNECTED`.
- Delivery Records Created: 5 total records:
  - `TELEGRAM_USER` -> `SUCCEEDED` (external_id `648`)
  - `TELEGRAM_CHANNEL` -> `SKIPPED_DUPLICATE`
  - `X` -> `APPROVED_NOT_CONNECTED`
  - `THREADS` -> `APPROVED_NOT_CONNECTED`
  - `YOUTUBE_COMMUNITY` -> `APPROVED_NOT_CONNECTED`
- Terminal Package Status: `DELIVERED`.
- Database rows verified in `content_packages` and `content_deliveries`.

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
- Declarative platform constraint validation (`VARIANT_CONSTRAINTS`);
- Content Package management (`ContentPackage`, `DistributionTarget`, `DeliveryRecord`);
- Independent user delivery, idempotent channel delivery, progress notifications, and stale job reaping;
- Interactive Telegram bot package review menu with strict owner authorization;
- Level A publication boundary for external platforms (`APPROVED_NOT_CONNECTED`);
- Complete release provenance enforcement via OCI labels and runtime verification.

Platform publication to X, Threads, and YouTube remains strictly manual/deferred. Autonomous external publishing, TTS, automated video re-assembly, and autonomous learning remain out of scope.

## Known debt

- **Platform Publishing Boundary (Level A)**: Direct API integrations / auto-publishing for X, Threads, and YouTube Community are intentionally deferred. Variants transition to `APPROVED_NOT_CONNECTED` upon approval.
- **Multilingual Output Policy**: Output policy remains fixed to Russian (`analysis_language=ru`, `user_output_language=ru`, `channel_output_language=ru`); per-user language preference store remains deferred.
- **Provider Language Metadata**: Transcription currently supplies no provider language metadata; lexical/script detection operates as normal path.
- **Translation In-Prompt**: Translation occurs inside analysis prompts rather than an independently scored translation stage.
- **Claim Position Linking**: Claim metadata reattaches after validation by list index; requires stable UUIDs if validators reorder claims.
- **External Query Fan-out**: Multilingual search query fan-out needs telemetry on latency/cost.
- **Historical Calibration Drift**: Model score drift is evaluated on static replay sets but not monitored dynamically in production.
- **Deprecation Warnings**: Pytest emits deprecation warnings for Pydantic V1 compat and `datetime.utcnow()`.
- **Pre-migration Postgres Volume**: Retained for rollback safety.

## Next stage

Recommended next product stage: **Publishing Orchestration & External Platform Connectors** (Roadmap Priority 5 / Level B publication).
Connect real external APIs with token storage, credential encryption, rate limiting, and automated webhook/callback delivery confirmation.
