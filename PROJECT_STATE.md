# REELS_BOT — canonical project state

Snapshot: 2026-09-07 04:20 ICT

Stage: **Telegram Analysis UX Cleanup v1 — COMPLETE**

## Release identity

- Branch: `main`.
- Production release SHA: `75780f49fc41052f78c7c525a2fc90fb1e7f02f7`.
- Telegram UX feature commit: `6f39e99f5ea5e8d84fadf2acd3356d432a4b9141` — feat(telegram): simplify analysis presentation.
- Telegram UX follow-up commit: `75780f49fc41052f78c7c525a2fc90fb1e7f02f7` — fix(telegram): preserve meaning without business check.
- Key rotation commit: `7e7a156e16a2cb9dc750b7a815fc80e4bdf12836` — fix(gemini): unify key rotation priority.
- UX feature commit: `05fad1e4adc7156d674528c569a88376abc2e304` — feat(telegram): explain risk in user-friendly language.
- Release tooling fix: `02d8c3e77e4122abe897792d056e76ed467bd9b8` — fix(release): pass Docker label template on PowerShell.
- Previous production baseline: `7e7a156e16a2cb9dc750b7a815fc80e4bdf12836` (documentation HEAD `3004fe321a64594b559d3cd11fc10310602bf932`).
- Remote: `origin` = `https://github.com/mikheooo/reels_bot.git`.
- Production image tag: `reels_bot:75780f49fc41052f78c7c525a2fc90fb1e7f02f7`.
- Running image digest: `sha256:b14cb36d3a027b53ba7b66f2f0a9a305013a4b8e4f23d092e5d83f89c5324675`.
- Image build timestamp: `2026-09-06T21:15:14Z`.
- Runtime provenance: OCI revision label, bot runtime identity, and worker runtime identity all match `75780f49fc41052f78c7c525a2fc90fb1e7f02f7`.

## Runtime

- Canonical Python: `3.13`; running image: `3.13.15`.
- PostgreSQL 15 is healthy on named volume `reels_bot_postgres_data`.
- Redis 7, Telegram bot, and ARQ worker are running.
- Bot identity guard verified `@Reeelsanalyzerbot` before polling.
- Worker registers `process_video`, `cron:reap_stale_jobs`, and `cron:cron_audit_v2_jobs`.
- Post-canary database: `dcec0b41-dd28-404d-b818-adf78b025c77` verified (`status=DONE`, `user=SUCCEEDED`, `content_package=CREATED`, `output_variants=SUCCEEDED`), `ContentPackageModel` (`8fd21975-836a-42e3-a4aa-263f1f1bc7a7`, `PARTIALLY_DELIVERED`).
- Outcome learning tables: `outcome_observations` and `calibration_runs` initialized with schema indexes and unique constraints (`uq_outcome_observations_pub_horizon`).
- Shadow mode verified: zero threshold mutations, zero prompt changes, `applied_recommendation_count = 0`.
- Queue depth is zero; no `QUEUED` or `PROCESSING` job remains from the canary.

## Gemini Key Rotation Priority Consistency Patch

- Fact-check/analysis and transcription now use the same deterministic order: `GEMINI_API_KEY`, `GEMINI_API_KEY_1..9`, then `GEMINI_PAID_KEY` as the final fallback.
- Duplicate key values are removed while preserving the first configured position.
- The previous fact-check behavior that allowed `GEMINI_API_KEY_1` to shadow `GEMINI_API_KEY` is removed.
- `.env.example` and `README.md` now document the actual runtime order.
- Production worker inspection returned identical configured slot order for both pools and `orders_match=True`; no key values were printed or changed.

## Telegram Analysis UX Cleanup v1

- `TELEGRAM_LONG` now follows the user-facing order: short title, concise conclusion, risk, human reasons, action, factual check, business model/meaning, and next step.
- Titles are derived deterministically from existing canonical content, limited to 12 complete words, omit `Разбор:`, and require no additional LLM call.
- Primary Telegram output hides Router labels, intent enums, confidence percentages, priority bands/scores, language codes, and other pipeline metadata while retaining all raw values in canonical content, package persistence, and debug data.
- Business category enums are translated through a deterministic presentation map. If the business check is not run by the unchanged policy, the renderer shows the existing canonical meaning rather than inventing a business classification.
- MEDIUM/HIGH risk uses the non-accusatory disclaimer: `Это не означает, что ролик — обман. Это признаки того, что к его обещаниям стоит относиться критически.`
- Fact UX distinguishes confirmed, disputed, and independently unconfirmed claims. Confirmed sources are inline and not duplicated as raw URLs.
- Telegram link previews are disabled for initial delivery, cached repeat delivery, and regenerated package delivery.
- Harsh skip wording is removed from the next-step presentation; existing canonical recommendations remain unchanged in persistence.
- Content Router, Priority policy and thresholds, Fact Check policy, risk thresholds, Outcome Learning, Audit, contracts, and persistence schemas are unchanged.

## Telegram Human-Friendly Risk Explanation UX Patch

- `TELEGRAM_LONG` remains the single production renderer used for Telegram user delivery.
- Canonical risk enums, Router taxonomy, confidence scores, thresholds, fact-check policy, safety floors, persistence schema, and output variant contracts are unchanged.
- User-facing risk levels are localized deterministically: `LOW` -> `Низкий`, `MEDIUM` -> `Средний`, `HIGH` -> `Высокий`; `CRITICAL` -> `Критический` is presentation-ready without widening the current Router enum.
- All 14 production content labels and all 8 production intent labels have centralized Russian explanations.
- Deterministic action guidance is defined for LOW, MEDIUM, HIGH, and forward-compatible CRITICAL.
- Primary `TELEGRAM_LONG` hides classifier confidence percentages and technical label names; canonical/persisted `risk_reasons` retain the raw label and score strings.
- MEDIUM/HIGH explanations explicitly state that the detected patterns are not a probability of fraud.
- Unknown future labels render a safe generic explanation instead of failing.

## Outcome Learning Dataset & Prioritization Calibration v1 Architecture

This stage implements ROADMAP Priority 4: Outcome Learning Dataset & Prioritization Calibration v1 operating strictly in **Observation / Shadow Calibration Mode**:

1. **Typed Data Lineage & Observation Schema**:
   - `OutcomeObservation` connects `publication_id`, `job_id`, `package_id`, `platform`, `variant`, `audit_horizon` (`15m`, `2h`, `12h`, `24h`, `3d`, `7d`), `router_classification`, `priority_score`, `raw_metrics`, `derived_metrics`, `content_integrity_status`, `data_quality_status`.
   - Immutable data lineage: links back to original jobs, Router classifications, priority scores, and output variants.
   - `AuditHorizon` enum with fixed non-overlapping evaluation windows (`15m`, `2h`, `12h`, `24h`, `3d`, `7d`). Cross-horizon pooling or direct comparisons are strictly prohibited.
   - Truthful platform normalization: missing view counts or zero impressions yield `engagement_rate = None` (never fake zero).
   - Data quality taxonomy: `VALID`, `INCOMPLETE_METRICS`, `INTEGRITY_COMPROMISED`, `OUTLIER_FLAGGED`, `STALE_TIMING`.
   - Integrity status: `VERIFIED_INTACT`, `CONTENT_MODIFIED`, `CONTENT_DELETED_OR_NOT_FOUND`, `AUTH_REVOKED`, `UNKNOWN`.

2. **Data Sufficiency & Calibration Readiness**:
   - `CalibrationReadiness` enum: `READY`, `INSUFFICIENT_DATA`, `LOW_CORRELATION`, `DISTORTED_OUTLIERS`, `SAFETY_FLOOR_VIOLATION`.
   - `DataSufficiencyPolicy`: minimum 30 observations per platform, minimum 10 observations per router category, observation window >= 24h.
   - Initial production database evaluation yields `INSUFFICIENT_DATA` truthfully (no simulated fake data).

3. **Outlier Guards & Robust Calibration**:
   - Interquartile range (IQR) and percentile thresholding (top 1% / bottom 1% flagged).
   - Trimmed means and medians for robust summary statistics.
   - Spearman rank correlation ($\rho$) to prevent high-leverage viral outliers from skewing threshold tuning.

4. **Immutable Safety Floor Protection**:
   - High-risk content gate (Router risk floor): threshold calibration cannot relax strict fact-checking or publish policies for high-risk categories (medical, financial, high-liability claims).
   - User delivery cannot be downgraded or disabled based on external performance stats.
   - Shadow calibration recommendations (`publish_threshold_delta`, `deprioritize_threshold_delta`, `suggested_weights`) logged strictly in shadow mode (`applied_recommendation_count = 0`).

5. **Replay Harness & 7 Mandatory Gates**:
   - Replay harness `app/worker/outcome_replay.py` validating 14 deterministic scenarios A through N (`tests/fixtures/outcome_learning_eval.json`).
   - Enforces 7 mandatory safety gates:
     1. Zero cross-horizon pooling (`gate_1_zero_cross_horizon_pooling = 0`)
     2. Zero fake zero denominators (`gate_2_zero_fake_zero_denominators = 0`)
     3. Data sufficiency enforcement (`gate_3_data_sufficiency_enforced = True`)
     4. Outlier isolation (`gate_4_outlier_isolation = True`)
     5. Zero automatic policy mutations (`gate_5_zero_automatic_policy_mutations = 0`)
     6. Immutable safety floor preservation (`gate_6_immutable_safety_floor_preservation = True`)
     7. Lineage integrity (`gate_7_lineage_integrity = True`)
   - All 7 gates passed (`all_gates_passed = True`).

## Automated test coverage

- Canonical Clean Git Archive / Hosted CI pytest run:
  - `391 collected`
  - `384 passed`
  - `7 deselected`
  - `0 failed`
- Replay evaluation suites (all 8 passing at 1.0, 131 tests passed):
  - **Outcome Learning Replay**: **14 deterministic scenarios** (`tests/fixtures/outcome_learning_eval.json`); zero cross-horizon pooling: `0`, zero fake zero denominators: `0`, data sufficiency enforced: `True`, outlier isolation: `True`, zero automatic policy mutations: `0`, immutable safety floor preserved: `True`, lineage integrity: `True`, all 7 gates passed: `True`.
  - **Audit Replay**: **13 deterministic scenarios** (`tests/fixtures/audit_eval.json`); `auth_mistaken_for_deletion`: `0`, `duplicate_snapshots`: `0`, `unsupported_fake_verification`: `0`, `overdue_pollution_for_unavailable_connectors`: `0`, all gates passed: `True`.
  - **Connector Replay**: **13 deterministic scenarios** (`tests/fixtures/connector_eval.json`); unauthorized publications: `0`, duplicate logical publications: `0`, stale approvals published: `0`, fake successes: `0`, retry correctness: `1.0`, all gates passed: `True`.
  - **Distribution Replay**: **10 scenarios across 10 evaluations** (`tests/fixtures/distribution_eval.json`); lifecycle correctness: `1.0`, approval enforcement: `1.0`, target status accuracy: `1.0`, unauthorized approval violations: `0`, duplicate publication violations: `0`, factual mutation violations: `0`, risk warning violations: `0`, idempotency violations: `0`, all gates passed: `True`.
  - **Output Variants Replay**: **8 cases across 40 evaluations** (`tests/fixtures/output_variants_eval.json`); format validity: `1.0`, length compliance: `1.0`, mandatory fact recall: `1.0`, forbidden fact rate: `0.0`, uncertainty preservation: `1.0`, risk warning preservation: `1.0`, language policy accuracy: `1.0`, not renderable accuracy: `1.0`, platform limit violations: `0`, risk warning violations: `0`.
  - **Multilingual Replay**: **13/13 cases**, `1.0` accuracy across language detection, router equivalence, priority band equivalence, output contract accuracy, and 0 risk floor violations.
  - **Content Router Replay**: **20/20 cases**, `1.0` calibration/policy accuracy, HIGH risk recall `1.0`.
  - **Combined Router+Priority Replay**: **6/6 cases**, `1.0` policy accuracy, 0 risk floor violations.

## Verified live production canary

- Deployment: 2026-09-07 04:15 ICT from clean release SHA `75780f49fc41052f78c7c525a2fc90fb1e7f02f7`.
- Canary job: `dcec0b41-dd28-404d-b818-adf78b025c77`.
- Reel URL: `https://www.instagram.com/reel/Dc1oN9IuLys/` (user `392046103`).
- Video validation: 720x1280 MP4, 12 keyframes visual evidence.
- Transcription: `OK`; upstream Gemini retries recovered from transient `429`/`503` responses.
- Language: Russian (`ru`), translation not required.
- Router: `BUSINESS_IDEA`, risk `MEDIUM`; intents `PROMISE_RESULT`, `TEACH`, `PERSUADE`, `RECOMMEND`.
- Priority: `overall_score=0.5245`, decision `AMBIGUOUS_CONTINUE` (publish threshold 0.6 intact).
- Output Variants: 5 rendered (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`), `all_valid=true`.
- User Delivery: `SUCCEEDED` (`TELEGRAM_LONG`).
- Channel Delivery: `SKIPPED_DUPLICATE` (dedup preserved).
- Content Package Created: ID `8fd21975-836a-42e3-a4aa-263f1f1bc7a7`, contract version `content_package_v1`.
- Delivered title contains 12 complete words; the message includes the new conclusion, risk, fact, business-model, and next-step sections.
- Delivered output contains none of the banned internal labels/scores and only one inline source URL; link previews are disabled by the delivery call.
- Persisted canonical data retains raw `LEAD_GENERATION`, `AMBIGUOUS`, priority `0.5245`, and risk reasons `PROMISE_RESULT (95%)`, `PERSUADE (90%)`, `TEACH (85%)`, `RECOMMEND (75%)`.
- Outcome Learning & Shadow Calibration:
  - Database schema initialized: `outcome_observations` and `calibration_runs` tables verified.
  - Unique constraint `uq_outcome_observations_pub_horizon` enforced.
  - Shadow Calibration mode confirmed: 0 automatic mutations to production thresholds (`publish=0.6`, `deprioritize=0.4`) or Router policies.
  - Zero overdue audit records created (`AUDIT_TARGETS_COUNT = 0`).
  - Redis queue depth: `0`.
  - Legacy isolation: historical rows remain undisturbed.

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
- Outcome Learning Dataset & Shadow Prioritization Calibration v1 (`OutcomeObservation`, `CalibrationRun`, `DataSufficiencyPolicy`, `CalibrationRecommendation`);
- 8 Deterministic evaluation replay suites with comprehensive mandatory safety gates.
