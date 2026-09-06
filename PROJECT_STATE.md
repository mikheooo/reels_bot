# REELS_BOT — canonical project state

Snapshot: 2026-09-06 22:50 ICT

Stage: **Multilingual Analysis Contract & Offline Evaluation — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `9c2b955eb2d7e297f64d7651fe2e61d022c5139b`.
- Documentation-only current HEAD: documentation-only handoff commit following release `9c2b955eb2d7e297f64d7651fe2e61d022c5139b`.
- Release commit: `9c2b955` — typed multilingual contract, deterministic
  detection, source/translation separation, multilingual search metadata,
  production integration and offline equivalence evaluation.
- Previous production release: `5c764ee49677d7970758728dae6d2b9a36c5715e`.
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- The release SHA was built from a clean Git archive, pushed to `origin/main`,
  passed hosted CI (run 34041920911) and was deployed through `scripts/release.ps1 -Deploy`.
- Production image tag: `reels_bot:9c2b955eb2d7e297f64d7651fe2e61d022c5139b`.
- Running image digest: `sha256:5294285a26f7c2f618ba3a6989d57b8c2dadeb98b69c2b7fb476d093334911f8`.
- OCI revision label, bot runtime identity and worker runtime identity equal the
  release SHA. Image build timestamp: `2026-09-06T15:20:12Z`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video` and `reap_stale_jobs`.
- Post-canary database: 86 jobs (`DONE=52`, `ERROR=28`,
  `REVIEW_REQUIRED=6`) and 21 tasks.
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## Original language behavior

The canonical `full_transcript` was already persisted verbatim immediately
after successful transcription and was not overwritten downstream. Gemini 3.5
transcription metadata contained model/fallback/status/duration/latency/character
data but no language. Router, priority, structured/specialized analysis,
fact-check, business-check and delivery largely relied on Russian prompt wording
without a typed language boundary. Mixed-language input had no explicit state,
claim source text and translated analysis were ambiguous, and Exa received one
untyped query. No multilingual equivalence replay existed.

## Multilingual contract and production flow

`app/worker/language.py` defines the versioned `LanguageContext` with detected
language/code/confidence, mixed-language flag, source languages, analysis/user/
channel output languages, translation requirement/mode, detection method,
fallback/failure data and canonical transcript SHA-256.

```text
immutable source transcript + provider metadata + secondary visible text
  -> bounded deterministic language detection
  -> typed LanguageContext persisted before Router
  -> Router + Priority with semantic-equivalence instructions
  -> routed analysis / claims / fact-check / delivery in explicit Russian
```

Detection order is provider language metadata when present, then deterministic
script/lexical evidence, then `unknown`. The minimum evaluated set is English,
Russian, Thai, Ukrainian, Spanish and unknown. The detector is extensible and
also recognizes Chinese, Japanese and Korean scripts. Visible video text is a
secondary weighted signal. Mixed input is first-class: dominant language,
confidence and all material source languages are stored.

Analysis, user output and channel output are explicitly fixed to Russian for
this release. The original transcript and direct quotes remain verbatim.
Translation is model-prompted and belongs only in separate analysis fields; no
independent translation API or user-selectable output preference is claimed.

## Claims, search and safety

Claims now distinguish `original_statement`/original language from the Russian
analysis representation and translation status. Search queries are typed by
language and purpose. Fact-check can search the original-language claim plus an
English coverage query when useful, deduplicates URLs, records query language,
and still ranks evidence by provenance/quality rather than language.

Router labels, priority bands and policy thresholds were not changed. Language
uncertainty cannot lower the Router risk floor or suppress required fact-check,
strict fact-check, business-check or user delivery. Detection timeout/exception
produces observable `unknown` fallback and an otherwise successful job becomes
`PARTIAL`; ordinary weak/unsupported language evidence produces
`SUCCEEDED_UNKNOWN_FALLBACK` rather than `ERROR`.

No schema migration was needed. `qa_reasons.language` stores the full contract;
the existing Router/priority/policy JSON remains alongside it, and
`delivery_status.language` stores the terminal language outcome. Historical rows
without these keys remain compatible.

## Test and evaluation baseline

- Exact clean Git archive, Python 3.13, network disabled:
  `pytest -m "not integration" -q` -> **237 passed, 1 skipped, 7 deselected**.
- `ruff check .` -> **all checks passed**.
- Multilingual replay: **13/13 cases in 7 semantic groups**; language, mixed,
  Router, priority-band, policy and output-contract accuracy all `1.0`; maximum
  recorded score drift `0.04`; zero risk-floor violations.
- Fixtures cover parallel English/Russian/Thai HOW_TO, English/Russian/Ukrainian
  HIGH-risk medical claims, English/Thai business, English/Russian low-value
  entertainment, Russian+English and Thai+English mixed input, and weak unknown.
- Original Router replay: **20/20**, all calibration/policy metrics `1.0`, HIGH
  risk recall `1.0`.
- Combined Router+Priority replay: **6/6**, policy accuracy `1.0`, zero risk-floor
  violations.
- GitHub Actions CI run `34041920911` for the exact release SHA: **success**.
- CI emitted one infrastructure annotation: checkout/setup-python actions still
  target deprecated Node.js 20 while the runner forces Node.js 24.

## Production canary

- Deployment: 2026-09-06 22:20 ICT from the clean release SHA.
- Canary job: `4368c7bd-0dbc-4691-954b-4d449a42d6ef`.
- Worker elapsed time: 180.18 seconds.
- Source: previously successful Russian Reel; download and 720x1280 validation
  succeeded; transcription `OK` via `gemini-3.5-transcribe`, no fallback.
- Canonical transcript: 2,530 characters; persisted SHA-256
  `9c7c8ab3c96a0b2105986bb32bab147573993167415c4055756bfae5ef30ec97`.
- Language: Russian (`ru`), confidence `0.9908`, not mixed, translation not
  required, detection fallback/failure absent; analysis/user/channel policy `ru`.
- Router: `HOW_TO`, risk `MEDIUM`. Priority: `AMBIGUOUS`, score `0.54`, decision
  `AMBIGUOUS_CONTINUE`; safety-oriented fact-check remained enabled.
- User delivery: `SUCCEEDED`. Channel: `SKIPPED_DUPLICATE` by the independent
  URL idempotency guard. No applicable task was persisted.
- Final state: `DONE`; language and priority outcomes `SUCCEEDED`; no
  `error_text`. Temporary Gemini `429/503` responses were absorbed by existing
  key rotation/retry behavior.

## Implemented production architecture

The active capability is a reactive Telegram-to-ARQ analysis pipeline with
PostgreSQL state, Redis queue, media download/validation, hybrid transcription,
visual evidence, typed multilingual context, calibrated Content Router, typed
prioritization and combined policy, routed technical/specialized analysis,
source-preserving multilingual claims/search, evidence-oriented fact-checking,
conditional business checks, independent user/channel delivery, optional task
persistence, progress reporting, stale-job reaping and exact provenance.

Post-Publish Audit remains deferred. Content Factory, TTS, video assembly,
automated QC, Trust Score, new social platforms, automatic cross-platform
publishing and autonomous learning are not production capabilities.

## Known debt

- **Multilingual output policy**: Текущая multilingual output policy явно фиксирована как `analysis_language=ru`, `user_output_language=ru`, `channel_output_language=ru`. Это детерминированный fallback текущего продукта, а не реализованная система per-user language preferences.
- Production transcription currently supplies no provider language metadata, so
  the deterministic detector is the normal path; it is heuristic rather than a
  calibrated language-identification model.
- Live canary coverage is Russian only. Thai, Ukrainian, Spanish, mixed and
  unknown semantics are release-gated offline, not proven by live provider runs.
- Translation is produced inside analysis prompts, not by an independently
  scored translation stage.
- Claim metadata is reattached after validation by list position; a stable claim
  identifier should replace this if validators may reorder claims.
- Multilingual search can increase external query fan-out and needs cost/latency
  telemetry.
- Priority and Router live model calibration drift is not measured historically.
- GitHub Actions has the Node.js action-runtime deprecation annotation.
- Pytest emits 17 Pydantic/`datetime.utcnow()` deprecation warnings; live tests
  remain opt-in and transitive dependencies are not hash-locked.
- The pre-migration anonymous PostgreSQL volume remains retained for rollback.

## Next stage

Recommended next product stage: **Multiple Output Variants Contract & Offline
Evaluation** (`ROADMAP.md` Priority 3). Define typed variants for concise,
detailed, Telegram, X, Threads and YouTube Community outputs; preserve the
source/language/safety contracts; keep publication manual; add deterministic
channel-fit and no-fabrication fixtures before any production generation change.
