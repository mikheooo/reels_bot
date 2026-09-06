# REELS_BOT — canonical project state

Snapshot: 2026-09-07 02:35 ICT

Stage: **Post-Publish Audit & Telemetry v2 — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `26abd21f053d59e47798b55b979ab088a0902176`.
- Documentation-only current HEAD: documentation-only handoff commit following release `26abd21f053d59e47798b55b979ab088a0902176`.
- Release commit: `26abd21` — feat(audit): implement post-publish audit and telemetry v2.
- Previous accepted baseline: `29fefbcfe06ec49024758cd5d196ac0a6056fc99` (documentation HEAD `753b25ee3dc2db4c02f15b77b9d8b966a4c56347`).
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- Hosted CI verification:
  - Runtime commit `26abd21f053d59e47798b55b979ab088a0902176`: CI run `34054965964` -> **SUCCESS** (43s).
- Production image tag: `reels_bot:26abd21f053d59e47798b55b979ab088a0902176`.
- Running image digest: `sha256:d45b83436e474f5923b4d19f3e4a8c1fbe9fb257dc8dc29604ac7827ed24dde1`.
- Image build timestamp: `2026-09-06T19:28:46Z`.
- Runtime provenance: verified via `scripts/show_provenance.ps1`. OCI revision label, bot runtime identity, and worker runtime identity all match `26abd21f053d59e47798b55b979ab088a0902176`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot, and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video`, `cron:reap_stale_jobs`, and `cron:cron_audit_v2_jobs`.
- Post-canary database: `d8e70570-daa6-47eb-933d-b0ed79b243ab` verified (`status=DONE`, `user=SUCCEEDED`, `content_package=CREATED`, `output_variants=SUCCEEDED`), `ContentPackageModel` (`deebe693-109d-426f-bc8c-50388bda108b`, `DELIVERED`), 3 `PublicationIntentModel` rows (`SUPPORTED_NOT_CONFIGURED` for X and Threads, `MANUAL_EXPORT_READY` for YouTube Community).
- Post-publish audit tables: `audit_targets` and `audit_snapshots` initialized with schema indexes. 0 false/overdue audit records created during canary.
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## Post-Publish Audit & Telemetry v2 Architecture

This stage implemented a production-grade post-publish audit and telemetry system:

1. **Provider Lookup & Telemetry Contracts**:
   - **X API v2**: `GET https://api.x.com/2/tweets/:id?tweet.fields=text,public_metrics`. Normalizes `impression_count`, `like_count`, `reply_count`, `retweet_count`, `quote_count`, `bookmark_count`.
   - **Meta Threads Graph API**: `GET https://graph.threads.net/v1.0/:id?fields=id,text,permalink` and official Meta Insights endpoint `GET https://graph.threads.net/v1.0/:id/insights?metric=views,likes,replies,reposts,quotes`.
   - **YouTube Community**: Truthfully marked `NOT_APPLICABLE_MANUAL_EXPORT` with `next_audit_at = None`.
   - **Telegram Channels / Users**: Truthfully marked `DELETION_VERIFICATION_UNSUPPORTED` with `next_audit_at = None`.

2. **Core Safety Gates Enforced**:
   - **Gate 1: `auth_mistaken_for_deletion == 0`**: HTTP 401/403 (unauthorized/token revoked) is explicitly categorized as `AUTH_REQUIRED` and NEVER as `DELETED`.
   - **Gate 2: `duplicate_snapshots == 0`**: Occurrence key idempotency (`f"{target.audit_id}:{int(checked_at.timestamp())}"`) guarantees at most one snapshot per check window.
   - **Gate 3: `unsupported_fake_verification == 0`**: Telegram and YouTube Community are never reported as externally verified or checked for deletion.
   - **Gate 4: `overdue_pollution_for_unavailable_connectors == 0`**: Unconfigured or manual-only targets evaluate to `NOT_APPLICABLE_*` with `next_audit_at = None`.

3. **Cadence & Scheduler**:
   - Multi-phase decaying schedule: 15 min, 2 h, 12 h, 24 h (1 day), 72 h (3 days), 7 days.
   - Terminal cadence handling (`next_audit_at = None`).
   - Transient backoff for HTTP 429 (`Retry-After` header parsing).
   - Cron worker integration via ARQ (`cron_audit_v2_jobs` running every 15 min at `:05, :20, :35, :50`).
   - Concurrency locking via `FOR UPDATE SKIP LOCKED`.

4. **Technical Content Integrity & Safe Delta Computation**:
   - Technical whitespace/newline normalization for lossless SHA-256 hash comparison.
   - Actionable alerts for `DELETED_OR_NOT_FOUND`, `MODIFIED`, and `AUTH_REQUIRED`.
   - Safe zero baseline metric delta computation (prev=0 -> pct=None).

5. **Legacy Audit Isolation**:
   - Historical records in `jobs` table (16 `DEFERRED_LEGACY`, 12 `DEFERRED`, 67 `NOT_SCHEDULED`) remain completely untouched and isolated.
   - V2 audit engine exclusively queries `audit_targets`.

## Automated test coverage

- Canonical pytest run:
  - `347 collected`
  - `340 passed`
  - `7 deselected`
  - `0 failed`
- Replay evaluation suites (all 7 passing at 1.0):
  - **Audit Replay**: **13 deterministic scenarios** (`tests/fixtures/audit_eval.json`); `auth_mistaken_for_deletion`: `0`, `duplicate_snapshots`: `0`, `unsupported_fake_verification`: `0`, `overdue_pollution_for_unavailable_connectors`: `0`, all gates passed: `True`.
  - **Connector Replay**: **13 deterministic scenarios** (`tests/fixtures/connector_eval.json`); unauthorized publications: `0`, duplicate logical publications: `0`, stale approvals published: `0`, fake successes: `0`, retry correctness: `1.0`, all gates passed: `True`.
  - **Distribution Replay**: **10 scenarios across 10 evaluations** (`tests/fixtures/distribution_eval.json`); lifecycle correctness: `1.0`, approval enforcement: `1.0`, target status accuracy: `1.0`, unauthorized approval violations: `0`, duplicate publication violations: `0`, factual mutation violations: `0`, risk warning violations: `0`, idempotency violations: `0`, all gates passed: `True`.
  - **Output Variants Replay**: **8 cases across 40 evaluations** (`tests/fixtures/output_variants_eval.json`); format validity: `1.0`, length compliance: `1.0`, mandatory fact recall: `1.0`, forbidden fact rate: `0.0`, uncertainty preservation: `1.0`, risk warning preservation: `1.0`, language policy accuracy: `1.0`, not renderable accuracy: `1.0`, platform limit violations: `0`, risk warning violations: `0`.
  - **Multilingual Replay**: **13/13 cases**, `1.0` accuracy across language detection, router equivalence, priority band equivalence, output contract accuracy, and 0 risk floor violations.
  - **Content Router Replay**: **20/20 cases**, `1.0` calibration/policy accuracy, HIGH risk recall `1.0`.
  - **Combined Router+Priority Replay**: **6/6 cases**, `1.0` policy accuracy, 0 risk floor violations.

## Verified live production canary

- Deployment: 2026-09-07 02:29 ICT from clean release SHA `26abd21f053d59e47798b55b979ab088a0902176`.
- Canary job: `d8e70570-daa6-47eb-933d-b0ed79b243ab`.
- Reel URL: `https://www.instagram.com/reel/Dc1oN9IuLys/` (user `392046103`).
- Video validation: 720x1280 MP4, 12 keyframes visual evidence.
- Transcription: `OK` via `gemini-3.5-transcribe` (2530 chars), no fallback.
- Language: Russian (`ru`), confidence `0.9908`, mixed `false`, translation not required.
- Router: `HOW_TO`, risk `MEDIUM`.
- Priority: `overall_score=0.560`, decision `AMBIGUOUS_CONTINUE`.
- Output Variants: 5 rendered (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`), `all_valid=true`.
- User Delivery: `SUCCEEDED` (`TELEGRAM_LONG`).
- Channel Delivery: `SKIPPED_DUPLICATE` (dedup preserved).
- Content Package Created: ID `deebe693-109d-426f-bc8c-50388bda108b`, contract version `content_package_v1`.
- Security Guard Verified: Unauthorized user `999999999` blocked with `UnauthorizedApprovalError`.
- Owner Approval & Intent Execution:
  - Owner `392046103` approved; `publication_intents` created with stable `publication_key` and `payload_hash`.
  - Connector-disabled default validated: X and Threads evaluated to `SUPPORTED_NOT_CONFIGURED` without failing the job.
  - YouTube Community evaluated to `READY_FOR_MANUAL_PUBLISH` (`MANUAL_EXPORT_READY`).
  - Terminal package status reached: `DELIVERED`.
  - Post-publish audit evaluation: targets for unconfigured connectors evaluated to `NOT_APPLICABLE_NOT_CONFIGURED` and YouTube to `NOT_APPLICABLE_MANUAL_EXPORT` with `next_audit_at = None`.
  - Zero overdue audit records created in `audit_targets` (`AUDIT_TARGETS_COUNT = 0`).
  - Redis queue depth: `0`.
  - Legacy isolation: 16 historical `DEFERRED_LEGACY` rows remain undisturbed.

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
- Post-Publish Audit & Telemetry v2 (`AuditTarget`, `AuditResult`, `AuditSnapshot`, `AuditPolicy`);
- Decaying multi-phase audit schedule (15m, 2h, 12h, 24h, 3d, 7d);
- Strict isolation of historical legacy audit records;
- Deterministic 13-scenario evaluation replay suite with 4 mandatory safety gates.
