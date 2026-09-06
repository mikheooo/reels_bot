# REELS_BOT — canonical project state

Snapshot: 2026-09-06 21:58 ICT

Stage: **Prioritization Gate Integration with Router Policy — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `5c764ee49677d7970758728dae6d2b9a36c5715e`.
- Release commit: `5c764ee` — typed priority gate, production integration,
  observability, deterministic replay, regression tests and policy documentation.
- Previous production release: `c4e15cbf5a778deae09b2b0cf9cd41a1de72a8ad`.
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- The release SHA was built from a clean tree and pushed to `origin/main`.
  Expected final divergence after this handoff commit is pushed: `0 behind / 0 ahead`.
- Production image tag: `reels_bot:5c764ee49677d7970758728dae6d2b9a36c5715e`.
- Running image digest: `sha256:770fdcc8a89429cab67aea6e78e617506198adbe0c4cfe50939130e6287bd9a4`.
- OCI revision label and runtime `APP_GIT_SHA` equal the release SHA.
- Image build timestamp: `2026-09-06T14:53:17Z`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video` and `reap_stale_jobs`.
- Post-canary database: 85 jobs (`DONE=51`, `ERROR=28`,
  `REVIEW_REQUIRED=6`) and 21 tasks.
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## Prioritization audit and contract

The original module was `app/worker/prioritization.py`. It accepted transcript
plus an optional analysis summary and returned the typed `PriorityScore`: five
LLM-produced values from 0.0 to 1.0, their deterministic weighted sum,
`publish`, and reasons. The weights were importance `0.25`, virality `0.25`,
novelty `0.15`, views potential `0.15`, and audience value `0.20`. The only
original band was `publish >= 0.60`. Unit tests existed, but production had no
call site: `process_video` derived policy from Router alone.

The canonical integration contract is now in `app/worker/priority_policy.py`:

- `PriorityGateResult`: score, tier, decision, thresholds, reasons, fallback
  flag/code and policy version;
- `CombinedPolicyResult`: original Router policy, effective policy, independent
  user/channel decisions and explicit suppressed actions.

Production flow:

```text
transcript + visual evidence
  -> calibrated RouterDecision
  -> PriorityScore(transcript + Router summary)
  -> PriorityGateResult
  -> CombinedPolicyResult
  -> downstream analysis and delivery
```

This decision is made and persisted before technical analysis, fact-check,
business-check, task creation or publication.

## Priority bands and deterministic policy

- `HIGH / ACCEPTED`: score `>= 0.60`; Router policy remains intact and channel
  publication is allowed.
- `LOW / DEPRIORITIZED`: score `< 0.40`; optional technical details, personal
  relevance, task creation and channel publication are suppressed when applicable.
- `AMBIGUOUS / AMBIGUOUS_CONTINUE`: score from `0.40` to `< 0.60`; Router
  policy remains intact. Uncertainty does not silently discard user results.

Priority never disables Router-required fact-check, strict fact-check or
business-check. HIGH-risk evidence and `REVIEW_REQUIRED` semantics therefore
remain stronger than usefulness scoring. User delivery is always independent
of channel publication at the combined-policy boundary.

Task creation requires both Router eligibility and a non-low priority result.
No new Task types were added.

## Persistence and failure semantics

No schema migration was needed. Existing JSON fields are used:

- `qa_reasons.router` stores calibrated Router output;
- `qa_reasons.priority` stores the complete priority score/gate result;
- `qa_reasons.policy` stores Router policy, effective policy, user/channel
  decisions and suppressed actions;
- `delivery_status.priority` stores `SUCCEEDED` or
  `FAILED_FALLBACK:<exception type>`;
- channel suppression is stored as `SUPPRESSED_PRIORITY`.

The policy payload is written while the job is `PROCESSING` and included again
in its final payload. Historical rows without these keys remain compatible.

Missing transcript, timeout, exception, malformed/range-invalid output or an
inconsistent `publish` flag produces `AMBIGUOUS / FALLBACK_CONTINUE`. Router
safety work and user delivery continue, public channel publication is withheld,
and the priority delivery failure makes an otherwise completed job `PARTIAL`
rather than a false `DONE`.

## Test and evaluation baseline

- Exact clean Git archive, Python 3.13, no network:
  `pytest -m "not integration" -q` -> **221 passed, 1 skipped, 7 deselected**.
- `ruff check .` -> **all checks passed**.
- Focused Router/priority/completion/retry suite: **79 passed**.
- Combined deterministic replay: **6/6 cases**, policy accuracy `1.0`, zero
  risk-floor violations. Cases cover high/low HOW_TO, HIGH-risk medical claim,
  BUSINESS, low-actionability entertainment and ambiguous software content.
- Original Router replay remains green across 20 fixtures.
- GitHub Actions CI run `34040517226` for the exact release SHA: **success**.
- Live Gemini is not part of offline evaluation; replay uses recorded structured
  outputs and asserts policy outcomes/invariants rather than fragile exact scores.

## Production canary

- Deployment: 2026-09-06 21:53 ICT from the clean release SHA.
- Canary job: `8102780b-35e0-4ed2-9ad6-47449da8f619`.
- Worker elapsed time: 177.33 seconds.
- Download and 720x1280 media validation succeeded; downscale was not required.
- Transcription status: `OK`; visual evidence: 12 keyframes.
- Router: `HOW_TO`, risk `MEDIUM`.
- Priority signals: importance `0.65`, virality `0.85`, novelty `0.30`, views
  potential `0.80`, audience value `0.40`; overall `0.62`.
- Gate: `HIGH / ACCEPTED`, threshold `0.60`, no suppressed actions.
- Effective policy: fact-check true, strict false, business false, technical
  true, tasks true, user delivery true, channel publication true.
- Technical analysis and fact-check path completed. No routed actionable task
  was extracted, so plan/task delivery was correctly `NOT_APPLICABLE`.
- User delivery: `SUCCEEDED`.
- Channel decision: allowed by priority, then `SKIPPED_DUPLICATE` by the
  independent URL idempotency guard.
- Final state: `DONE`, priority delivery `SUCCEEDED`, audit `DEFERRED`, no
  `error_text` or unhandled exception.

## Implemented production architecture

The active capability is a reactive Telegram-to-ARQ analysis pipeline with
PostgreSQL state, Redis queue, media download/validation, hybrid transcription,
visual evidence, calibrated Content Router, typed prioritization gate,
deterministic combined policy, routed technical/specialized analysis,
evidence-oriented fact-checking, conditional business-checking, independent
user/channel delivery, optional plan/task persistence, progress reporting,
stale-job reaping and exact runtime provenance.

Post-Publish Audit remains deferred. Content Factory, TTS, video assembly,
automated QC, Trust Score, new social platforms/content types and autonomous
learning are not production capabilities.

## Known debt

- Priority signal generation is still one LLM call. Deterministic replay proves
  policy behavior, not live scoring quality or calibration drift.
- The three score bands are configuration-backed but have no historical outcome
  study yet; changing them requires a separately reviewed calibration stage.
- GitHub CI reports Node.js 20 deprecation for `actions/checkout@v4` and
  `actions/setup-python@v5` while the runner forces Node.js 24.
- Pytest emits 15 Pydantic/`datetime.utcnow()` deprecation warnings.
- Live integration tests are opt-in, and transitive dependencies are not
  hash-locked.
- The pre-migration anonymous PostgreSQL volume remains retained for rollback.

## Next stage

Recommended next stage: **Multilingual Analysis Contract & Offline Evaluation**
(the first deferred product priority in `ROADMAP.md`). It has not been started.
