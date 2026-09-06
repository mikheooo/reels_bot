# Post-Publish Audit & Telemetry v2

## Overview

The **Post-Publish Audit & Telemetry v2** subsystem provides automated, scheduled verification and metrics tracking for content published across external social networks. Building on the publication boundary established in the External Platform Connectors stage, this system monitors external posts over their lifecycle, validates content integrity, collects engagement metrics, detects anomalies (deletion, unauthorized content modifications, token expirations), and isolates historical legacy records.

The pipeline enforces five core architectural principles:
1. **Accurate Provider Status Semantics**: Absolute distinction between HTTP 404 (post absent/deleted), HTTP 401/403 (authentication expired/revoked), and HTTP 429 (rate limited). Authentication failures are NEVER mistaken for content deletion.
2. **Zero Overdue Schedule Pollution**: Connectors in `SUPPORTED_NOT_CONFIGURED` or `UNSUPPORTED_OFFICIAL_API` evaluate immediately to non-scheduled terminal statuses (`NOT_APPLICABLE_NOT_CONFIGURED`, `NOT_APPLICABLE_MANUAL_EXPORT`) with `next_audit_at = None`, preventing spurious overdue backlog accumulation.
3. **Strict Legacy Isolation**: Pre-existing historical audit records (`DEFERRED_LEGACY`, `DEFERRED`) in the `jobs` table are strictly isolated. V2 audit operates entirely through dedicated `audit_targets` and `audit_snapshots` tables and never awakens legacy rows.
4. **Stable Idempotency & Occurrence Keys**: Immutable snapshot persistence keyed by deterministic `occurrence_key` strings (`target_id:timestamp`), preventing duplicate audit snapshots during worker retries.
5. **Technical Content Integrity**: Exact SHA-256 hash comparison between approved payloads and platform-returned text using lossless whitespace/newline normalization.

---

## Provider Audit & Lookup Contracts

### 1. X (Twitter) API v2

| Attribute | Specification |
| :--- | :--- |
| **Lookup Endpoint** | `GET https://api.x.com/2/tweets/:id?tweet.fields=text,created_at,author_id,public_metrics` |
| **Authentication Model** | OAuth 2.0 User Token / Bearer Token |
| **Public Metrics** | `impression_count`, `like_count`, `reply_count`, `retweet_count`, `quote_count`, `bookmark_count` |
| **Normalized Metrics Mapping** | `views` ← `impression_count`, `likes` ← `like_count`, `replies` ← `reply_count`, `reposts` ← `retweet_count`, `quotes` ← `quote_count`, `bookmarks` ← `bookmark_count` |
| **Status Handling** | HTTP 200: `VERIFIED` or `MODIFIED`; HTTP 404: `DELETED_OR_NOT_FOUND`; HTTP 401/403: `AUTH_REQUIRED`; HTTP 429: `TEMPORARILY_UNAVAILABLE` |
| **Rate Limit Headers** | `x-rate-limit-limit`, `x-rate-limit-remaining`, `x-rate-limit-reset`, `Retry-After` |

### 2. Meta Threads Graph API

| Attribute | Specification |
| :--- | :--- |
| **Lookup Endpoint** | `GET https://graph.threads.net/v1.0/:id?fields=id,text,permalink,timestamp` |
| **Insights Endpoint** | `GET https://graph.threads.net/v1.0/:id/insights?metric=views,likes,replies,reposts,quotes` |
| **Authentication Model** | Long-lived Meta Threads User Access Token |
| **Normalized Metrics Mapping** | `views` ← `views`, `likes` ← `likes`, `replies` ← `replies`, `reposts` ← `reposts`, `quotes` ← `quotes` |
| **Status Handling** | HTTP 200: `VERIFIED` or `MODIFIED`; HTTP 404: `DELETED_OR_NOT_FOUND`; HTTP 401/403: `AUTH_REQUIRED`; HTTP 429: `TEMPORARILY_UNAVAILABLE` |
| **Rate Limit Headers** | `Retry-After`, `x-business-use-case-usage` |

### 3. YouTube Community Posts

| Attribute | Specification |
| :--- | :--- |
| **Official Lookup API** | **None**. YouTube Data API v3 does not support reading or querying Community tab posts. |
| **Engineering Policy** | No scraping or reverse engineering. Truthfully marked `NOT_APPLICABLE_MANUAL_EXPORT`. |
| **Schedule Semantics** | `eligible = False`, `next_audit_at = None`. Zero fake verification checks. |

### 4. Telegram Channels & Users

| Attribute | Specification |
| :--- | :--- |
| **Official Lookup API** | Telegram Bot API does not provide an endpoint to look up arbitrary past messages or verify deletion status. |
| **Truthful Status** | `DELETION_VERIFICATION_UNSUPPORTED` |
| **Schedule Semantics** | `eligible = False`, `next_audit_at = None`. Zero fake verification checks. |

---

## Data Model & Relational Schema

### 1. `audit_targets` Table

Represents a trackable external publication target governed by an audit policy.

```sql
CREATE TABLE IF NOT EXISTS audit_targets (
    id VARCHAR(64) PRIMARY KEY,
    package_id VARCHAR(64) NOT NULL,
    delivery_id VARCHAR(64) NOT NULL,
    target VARCHAR(32) NOT NULL,
    variant VARCHAR(32),
    provider_post_id VARCHAR(128) NOT NULL,
    provider_url TEXT,
    publication_key VARCHAR(128) NOT NULL,
    approved_payload_hash VARCHAR(64) NOT NULL,
    approved_payload_text TEXT NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'SCHEDULED',
    tier INTEGER NOT NULL DEFAULT 0,
    next_audit_at TIMESTAMP WITHOUT TIME ZONE,
    last_checked_at TIMESTAMP WITHOUT TIME ZONE,
    last_result_status VARCHAR(64),
    last_error_code VARCHAR(64),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_audit_targets_next_audit
ON audit_targets (next_audit_at)
WHERE status IN ('SCHEDULED', 'ACTIVE');

CREATE INDEX IF NOT EXISTS ix_audit_targets_package_id
ON audit_targets (package_id);
```

### 2. `audit_snapshots` Table

Stores immutable audit event records, content hashes, and normalized engagement telemetry.

```sql
CREATE TABLE IF NOT EXISTS audit_snapshots (
    id VARCHAR(64) PRIMARY KEY,
    audit_id VARCHAR(64) NOT NULL REFERENCES audit_targets(id) ON DELETE CASCADE,
    occurrence_key VARCHAR(128) NOT NULL,
    checked_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    object_exists BOOLEAN NOT NULL,
    content_hash VARCHAR(64),
    content_match BOOLEAN,
    raw_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    normalized_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    metric_deltas JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_http_status INTEGER,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_snapshots_occurrence
ON audit_snapshots (occurrence_key);

CREATE INDEX IF NOT EXISTS ix_audit_snapshots_audit_id
ON audit_snapshots (audit_id);
```

---

## Cadence & Scheduling Architecture

### Cadence Schedule

The default `AuditPolicy` defines a declarative, non-linear decaying schedule covering 7 days:
- **Tier 0**: 15 minutes (`900s`)
- **Tier 1**: 2 hours (`7200s`)
- **Tier 2**: 12 hours (`43200s`)
- **Tier 3**: 24 hours / 1 day (`86400s`)
- **Tier 4**: 72 hours / 3 days (`259200s`)
- **Tier 5**: 7 days (`604800s`)
- **Tier 6+**: Terminal state reached; `next_audit_at = None`.

### Execution Worker & Concurrency

- **Worker cron task**: `cron_audit_v2_jobs` registered in `WorkerSettings.cron_jobs` running every 15 minutes (`minute={5, 20, 35, 50}`).
- **Concurrency control**: PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` prevents double-auditing across distributed worker nodes.
- **Occurrence key idempotency**: `occurrence_key = f"{target.audit_id}:{int(checked_at.timestamp())}"` ensures duplicate triggers do not insert duplicate database snapshots.

---

## Metric Delta Computation & Safe Zero Baseline

Metric deltas compare current normalized metrics against the previous audit snapshot:
$$\Delta_{	ext{abs}} = M_{	ext{current}} - M_{	ext{previous}}$$
$$\Delta_{	ext{pct}} = rac{M_{	ext{current}} - M_{	ext{previous}}}{M_{	ext{previous}}} 	imes 100\% \quad (M_{	ext{previous}} > 0)$$

If $M_{	ext{previous}} = 0$, $\Delta_{	ext{pct}}$ is set to `None` to prevent division by zero or misleading infinite growth percentages.

---

## Actionable Anomaly Alerting

Alerts are sent to administrators (`should_send_alert`) exclusively for actionable incidents:
1. `DELETED_OR_NOT_FOUND`: Post was removed by the author or platform moderation.
2. `MODIFIED`: External post text was edited and deviates from the approved payload.
3. `AUTH_REQUIRED`: Provider access token expired or credentials revoked (HTTP 401/403).

Routine metric increases and transient rate-limits never trigger urgent administrative alerts.

---

## Deterministic Evaluation Replay Suite

The 13 evaluation scenarios in `tests/fixtures/audit_eval.json` validate every edge case against 4 safety gates:

| # | Scenario ID | Target | Provider Status | Expected Result | Gate Enforced |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `case_01_x_verified_unchanged` | X | HTTP 200 (Exact text) | `VERIFIED` | Exact content integrity |
| 2 | `case_02_x_modified` | X | HTTP 200 (Edited text) | `MODIFIED` | Content mutation detection |
| 3 | `case_03_x_deleted_404` | X | HTTP 404 (Not found) | `DELETED_OR_NOT_FOUND` | Terminal deletion state |
| 4 | `case_04_x_auth_failure_401` | X | HTTP 401 (Unauthorized) | `AUTH_REQUIRED` | **Gate 1**: `auth_mistaken_for_deletion == 0` |
| 5 | `case_05_x_rate_limited_429` | X | HTTP 429 (Throttled) | `TEMPORARILY_UNAVAILABLE` | Transient retry backoff |
| 6 | `case_06_threads_verified` | Threads | HTTP 200 (Exact text) | `VERIFIED` | Meta Insights normalization |
| 7 | `case_07_threads_metrics_updated` | Threads | HTTP 200 (Growing metrics) | `VERIFIED` | Safe zero baseline deltas |
| 8 | `case_08_threads_deleted_404` | Threads | HTTP 404 (Object absent) | `DELETED_OR_NOT_FOUND` | Terminal deletion state |
| 9 | `case_09_connector_not_configured` | X | Unconfigured | `NOT_APPLICABLE_NOT_CONFIGURED` | **Gate 4**: `overdue_pollution == 0` |
| 10 | `case_10_youtube_manual_only` | YouTube | Manual Export Only | `NOT_APPLICABLE_MANUAL_EXPORT` | **Gate 3 & 4**: `unsupported_fake == 0` |
| 11 | `case_11_legacy_deferred_record` | Legacy | Legacy DB record | `DEFERRED_LEGACY` | Strict legacy isolation |
| 12 | `case_12_duplicate_audit_trigger` | X | HTTP 200 | `VERIFIED` | **Gate 2**: `duplicate_snapshots == 0` |
| 13 | `case_13_telegram_known_edit` | Telegram | Bot API Delivery | `DELETION_VERIFICATION_UNSUPPORTED` | **Gate 3**: `unsupported_fake == 0` |
