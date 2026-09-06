# Outcome Learning Dataset & Prioritization Calibration v1

## 1. Executive Summary & Policy Boundary

> **Outcome Learning v1 собирает и анализирует результаты в shadow mode. Он не изменяет production policy автоматически.**

This subsystem implements **ROADMAP Priority 4: Обучение на собственной статистике**. Its objective is to connect post-publish performance signals (impressions, views, engagement, retention, edits, and deletions) back to upstream algorithmic decisions, creating an empirical dataset for calibrating content selection.

### Core Operating Mode
- **Mode:** `OBSERVATION / SHADOW CALIBRATION MODE`
- **Automatic Mutations:** **STRICTLY PROHIBITED**. Production thresholds (`publish_threshold=0.6`, `deprioritize_threshold=0.4`), Content Router prompts, risk policies, and fact-checking gates remain completely immutable.
- **Safety Floors:** Performance signals cannot lower Router risk levels, disable fact-checking, relax strict fact-check constraints, or suppress Telegram user delivery.

---

## 2. End-to-End Data Lineage

The system maintains a deterministic, traceable, and immutable data lineage:

```
Job (Media Ingestion, Transcription, Language Detection)
  └── Router (Category Classification, Risk Assessment: LOW / MEDIUM / HIGH)
        └── Priority (PriorityScore: Importance, Virality, Novelty; PriorityGateResult: HIGH / AMBIGUOUS / LOW)
              └── CanonicalContentResult (Validated Claims, Risk Warnings, Disclaimers)
                    └── OutputVariant (TLDR, TELEGRAM_LONG, X_POST, THREADS_POST, YOUTUBE_COMMUNITY)
                          └── ContentPackage (Unified distribution unit, Owner Approval State Machine)
                                └── PublicationIntent (Approved intent with stable publication_key & payload_hash)
                                      └── ContentDelivery (Execution record with provider_post_id)
                                            └── AuditTarget (Decaying cadence schedule: 15m, 2h, 12h, 24h, 3d, 7d)
                                                  └── AuditSnapshot (Provider metrics, content match, latency)
                                                        └── OutcomeObservation (Immutable lineage observation)
```

Attribution is strictly bound to stable UUIDs (`job_id`, `package_id`, `publication_key`, `audit_snapshot_id`). Fuzzy matching or URL-only heuristics are strictly forbidden.

---

## 3. Fixed Outcome Horizons

Audit observations are segmented into 6 canonical, non-interchangeable horizons:

| Horizon | Cadence Tier | Nominal Elapsed Time | Window Matching |
|---|---|---|---|
| `15m` | Tier 0 | 15 minutes | $\le 45$ min |
| `2h` | Tier 1 | 2 hours | 45 min to 6 hours |
| `12h` | Tier 2 | 12 hours | 6 hours to 18 hours |
| `24h` | Tier 3 | 24 hours | 18 hours to 48 hours |
| `3d` | Tier 4 | 72 hours (3 days) | 48 hours to 5 days |
| `7d` | Tier 5 | 7 days | $\ge 5$ days |

**Horizon Mixing Prohibition:** A 24h outcome must never be compared or pooled with a 7d outcome as if they represent the same observation target.

---

## 4. Platform-Specific Normalization & Missing Denominators

Cross-platform false equivalences are strictly prevented:
- **X API v2:** Normalizes `impression_count`, `like_count`, `reply_count`, `retweet_count`, `quote_count`, `bookmark_count`.
- **Meta Threads Graph API:** Normalizes `views`, `likes`, `replies`, `reposts`, `quotes`. Missing optional metrics remain `None`.
- **Telegram Channels:** Observational edit events (`AuditEventModel`, `EDIT_OBSERVED`) without fabricating deletion verification.
- **YouTube Community:** Marked `NOT_APPLICABLE_MANUAL_EXPORT`.

### Truthful Metric Derivation
- `engagement_total`: Sum of available interactions (`likes + replies + reposts + quotes + bookmarks`).
- `engagement_rate`: Computed as `engagement_total / views` **ONLY when views is known and $> 0$**. If views is `None` or `0`, `engagement_rate = None`. Injecting fake zero baselines is strictly forbidden.
- `velocity_views_per_hour`: Views divided by elapsed hours since publication.
- `velocity_engagement_per_hour`: Total engagement divided by elapsed hours since publication.

---

## 5. Data Quality Taxonomy

Every outcome observation is tagged with a typed `DataQualityStatus`:

| Status | Definition | Eligible for Calibration |
|---|---|---|
| `VALID` | Post exists, content matches approved payload, metrics verified. | **Yes** |
| `PARTIAL` | Post exists and matches, but optional metrics are missing from provider. | **Yes** |
| `INSUFFICIENT_DATA` | Key metric signals absent or zero without verification. | No |
| `AUTH_UNAVAILABLE` | Provider HTTP 401/403 or token revoked. | No |
| `CONTENT_MODIFIED` | Content altered externally (`content_match == False`). | No |
| `CONTENT_DELETED` | Post returned HTTP 404 or was deleted by user/platform. | No |
| `UNSUPPORTED` | Platform does not support post lookup. | No |
| `NOT_APPLICABLE` | Manual export or unconfigured connector. | No |

Modified and deleted posts are flagged and quarantined from normal threshold calibration cohorts to prevent skewed recommendations.

---

## 6. Data Sufficiency Policy & Readiness States

The system evaluates dataset readiness via `DataSufficiencyPolicy`:
- `min_samples_for_readiness`: 20 valid observations.
- `min_samples_per_cohort`: 5 valid observations in each band (`HIGH`, `AMBIGUOUS`, `LOW`).
- `min_distinct_packages`: 5 distinct content packages.

### Readiness States
1. `INSUFFICIENT_DATA`: Real historical outcome dataset is empty (current baseline due to unconfigured live external connector credentials).
2. `OBSERVATION_ONLY`: Samples are accumulating but do not yet satisfy minimum sufficiency criteria across cohorts.
3. `CALIBRATION_READY`: Minimum sample counts and cohort diversity are satisfied.

---

## 7. Priority Outcome Analysis & Outlier Handling

The analysis engine computes:
- **Rank Correlation:** Spearman's rank correlation coefficient $r_s$ between `priority_score` and realized metrics (`views`, `engagement_total`).
- **Cohort Performance:** Distribution statistics (count, mean, median, trimmed mean, p25, p75, min, max) for `HIGH`, `AMBIGUOUS`, and `LOW` cohorts.
- **Published vs Deprioritized:** Contrast analysis between content accepted for distribution vs deprioritized content.
- **Outlier Guards:** Trimmed means (excluding top/bottom 10%) and medians are used so extreme viral outliers (e.g. 500k impressions) do not dominate threshold calibration.

---

## 8. Shadow Calibration Engine

When a dataset achieves `CALIBRATION_READY`, the shadow engine creates a `CalibrationRun` and may emit candidate `CalibrationRecommendation` records.

### Recommendation Contract
- `recommendation_id`: Unique identifier (`rec_*`).
- `platform`: Target platform (`X`, `THREADS`).
- `horizon`: Target horizon (`24h`).
- `current_threshold`: Active production threshold (e.g. 0.60).
- `suggested_threshold`: Suggested calibrated threshold.
- `evidence_sample_size`: Number of observations in evidence base.
- `confidence`: Confidence score (0.0 to 1.0).
- `estimated_tradeoff`: Tradeoff analysis description.
- `reason`: Justification based on cohort performance.
- `applied`: **MANDATORY `False`**.

### Strict Invariant: Zero Automatic Mutation
Under no circumstances are recommendations applied automatically. `applied_recommendation_count` MUST equal `0`.

---

## 9. Database Architecture & Idempotency

Persistence is managed via two dedicated tables:

### `outcome_observations`
- Primary key: `id` (VARCHAR)
- Unique key: `(publication_key, audit_horizon)`
- Indices on `job_id`, `package_id`, `audit_snapshot_id`, `(target, audit_horizon)`, and `data_quality_status`.
- Stores raw provider metrics (immutable) and derived metrics in separate JSONB columns.

### `calibration_runs`
- Primary key: `id` (VARCHAR)
- Unique key: `run_id`
- Records readiness, sample counts, summary metrics, and generated shadow recommendations.

---

## 10. Verification & Deterministic Replay

The subsystem is validated by a 14-scenario offline evaluation suite (`tests/fixtures/outcome_learning_eval.json`) asserting 7 mandatory safety gates:
1. `attribution_violations == 0`
2. `duplicate_outcomes == 0`
3. `horizon_mixing_violations == 0`
4. `fake_zero_metric_violations == 0`
5. `safety_floor_violations == 0`
6. `automatic_policy_mutations == 0`
7. `insufficient_data_false_recommendations == 0`
