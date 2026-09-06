# REELS_BOT — canonical project state

Snapshot: 2026-09-06 21:35 ICT  
Stage: **Release Baseline Stabilization & Reproducible Deployment — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `c4e15cbf5a778deae09b2b0cf9cd41a1de72a8ad`.
- Release commits:
  - `03c27d3d5ed2414c98e5f6102dcb85b06811c974` — release gates, provenance,
    persistence migration, delivery semantics, audit decision, retry policy and
    operations documentation.
  - `c4e15cbf5a778deae09b2b0cf9cd41a1de72a8ad` — cache-safe release image build.
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- `origin/main` was fetched before changes. The release SHA was pushed and the
  final expected divergence after this handoff commit is pushed is `0 behind / 0 ahead`.
- The production build was made from a clean tree. Local-only/debug files and
  backups are ignored; generated `graphify-out/` files are no longer tracked.
- Production image tag: `reels_bot:c4e15cbf5a778deae09b2b0cf9cd41a1de72a8ad`.
- Running image digest: `sha256:5a1c7d52cd073505764c231ce212f9edfce1c8bb0583698e91e597851656c1ec`.
- OCI revision label and runtime `APP_GIT_SHA` both equal the release SHA.
- Image build timestamp: `2026-09-06T14:29:42Z`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- Docker services: PostgreSQL 15, Redis 7, Telegram bot, ARQ worker.
- PostgreSQL is healthy and mounted at `/var/lib/postgresql/data` from the
  explicit named volume `reels_bot_postgres_data`.
- Redis is up. The worker registers `process_video` and `reap_stale_jobs`.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Post-canary database: 84 jobs (`DONE=50`, `ERROR=28`,
  `REVIEW_REQUIRED=6`) and 21 tasks.
- Post-canary ARQ job completed normally; no stuck `PROCESSING` job or
  unhandled exception was observed.

## Test baseline

- Clean Linux source archive, Python 3.13, without `.env` and without network:
  `pytest -m "not integration" -q` -> **209 passed, 1 skipped, 7 deselected**.
- `ruff check .` -> **all checks passed**.
- Integration modules are marked at module level and perform platform/external
  setup only after pytest has selected them. They are opt-in and excluded from
  the offline release gate.
- Offline Content Router replay: 20 fixtures; all recorded evaluation metrics
  were `1.0`. This is deterministic regression evidence, not a live quality metric.
- GitHub Actions CI run `34039605723` for the exact release SHA: **success**;
  Python 3.13 setup, dependency install, Ruff and offline pytest all passed.

## Production verification

- Deployment: 2026-09-06 21:29 ICT from the clean release SHA.
- Canary job: `48ea43fa-a637-4a51-99a4-d2c38af38d39`.
- Canary runtime: release SHA above; elapsed worker time: 150.34 seconds.
- Intake/enqueue: accepted as a new production job.
- Download: succeeded through the configured download path.
- Media validation: 720x1280, 21,231,215 bytes; downscale was correctly not
  required, so the normalization branch was a no-op.
- Transcription: persisted with status `OK` before downstream processing.
- Visual evidence: 12 keyframes extracted and formatted.
- Router/policy: `HOW_TO`, `MEDIUM`; fact-check and technical analysis enabled,
  business-check disabled, task capability enabled by policy.
- Analysis: technical structured analysis and fact-check path completed.
- Plan and task persistence were `NOT_APPLICABLE` because no routed task was
  extracted; this is an explicit policy outcome, not a hidden delivery failure.
- Telegram user delivery: `SUCCEEDED`.
- Telegram channel: `SKIPPED_DUPLICATE`; the existing published `url_hash` was
  detected and no duplicate channel post was created.
- Final database state: `DONE`, `audit_state=DEFERRED`, no `error_text`, with
  structured delivery status `{user: SUCCEEDED, channel: SKIPPED_DUPLICATE,
  plan: NOT_APPLICABLE, task_db: NOT_APPLICABLE}`.

## PostgreSQL durability

- The previous anonymous-volume database was backed up before migration.
- Final pre-switch backup:
  `backups/reels_db-20260906-212429.dump`, 247,503 bytes, SHA-256
  `04E0DE9F5BF71E9BDB1669F52C9268A1AB0DA5B80DC25ADFDF504A9D8ACCE637`.
- `pg_restore --list` succeeded. A separate backup was restored into a
  disposable PostgreSQL 15 instance and aggregate counts matched the source:
  83 jobs, 21 tasks, 49 `DONE` at backup time.
- The final backup was restored into `reels_bot_postgres_data`; the same counts
  were verified before services resumed. The old anonymous volume was retained
  as a rollback safety copy and was not destroyed.
- Exact backup, restore and provenance commands are in
  `docs/RELEASE_OPERATIONS.md`.

## Completion, audit and retry policy

- `DONE`: required analysis and user delivery succeeded; every applicable
  downstream delivery step succeeded, or channel publish was an idempotent
  duplicate skip.
- `PARTIAL`: user received the analysis, but an applicable channel, plan or
  task-persistence step failed. `delivery_status` records the outcome.
- `ERROR`: required analysis or user delivery failed.
- `REVIEW_REQUIRED`: evidence policy stopped automatic publication/task side effects.
- Regression tests cover the completion decision and delivery persistence.
- Post-Publish Audit uses Option B: it is deferred, not an active production
  capability. New jobs receive no schedule and use `audit_state=DEFERRED`.
  The 16 historical overdue timestamps were preserved and marked
  `DEFERRED_LEGACY`; the other 67 historical jobs became `NOT_SCHEDULED`.
- ERROR retry is reachable: intake queries the newest matching job regardless
  of state, resets only transient fields on `ERROR`, reuses its job id and
  enqueues it. `REVIEW_REQUIRED` is not automatically retried. Regression tests
  cover this policy.

## Implemented architecture

The production capability is a reactive Telegram-to-ARQ pipeline: bot intake
and URL deduplication; PostgreSQL job/task state; Redis queue; media download
and dimension validation; hybrid Gemini transcription with canonical transcript
persistence; frame-based visual evidence; Content Router and explicit policy;
routed specialized/technical analysis; evidence-oriented fact-checking and
optional business-checking; compact Telegram result plus detail callbacks;
idempotent channel publication; optional plan/task persistence; progress
updates; stale-job reaping; API-key health and retry handling; and runtime
release provenance.

## Known debt

- Hosted CI reports that `actions/checkout@v4` and `actions/setup-python@v5`
  target deprecated Node.js 20 (the runner currently forces Node.js 24).
- Pytest is green but emits 15 deprecation warnings, mainly Pydantic v2
  class-based config and `datetime.utcnow()` usage.
- Integration tests remain an opt-in external-services gate; CI currently
  proves the offline suite, not every live provider path.
- Transitive dependencies are resolved at build time because the project has no
  locked, hash-verified dependency set; identical source SHA can therefore pick
  newer allowed versions such as `yt-dlp` and `exa-py`.
- The pre-migration anonymous PostgreSQL volume is deliberately retained for
  rollback and needs an explicit retention/removal decision later.

## Deferred roadmap

These are not production capabilities: Prioritization Gate integration,
Post-Publish Audit execution, Content Factory, TTS, video assembly, automated
QC, Trust Score, new social platforms/content types, and autonomous learning.

## Next stage

Recommended next product stage: **Prioritization Gate Integration with Router
Policy**. It was not started during release stabilization.
