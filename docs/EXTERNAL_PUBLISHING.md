# Publishing Orchestration & External Platform Connectors

## Overview

The **Publishing Orchestration & External Platform Connectors** subsystem advances the distribution layer from Level A (`APPROVED_NOT_CONNECTED`) into a production-grade **Level B Publication Boundary**. It connects approved `ContentPackage` variants to official third-party social APIs (X and Meta Threads) while maintaining a strict, non-compromising policy regarding unsupported endpoints (YouTube Community).

The pipeline enforces:
1. **Explicit Separation**: `Generation != Approval != Publication`.
2. **Official APIs Only**: Zero browser automation, scraping, or reverse-engineered endpoints.
3. **Owner Authorization**: Only verified job owners can approve or trigger publication intents.
4. **Stable Publication Idempotency**: Immutable `publication_key` preserved across retries; technical `attempt_id` incremented per attempt (`retry != new publication`).
5. **Stale Approval Protection**: Exact SHA-256 payload hash verification prevents publishing mutated content.
6. **Ambiguous Outcome Reconciliation**: Network timeouts transition to `DELIVERY_UNKNOWN` and require search/lookup verification before allowing safe retry.
7. **Connector-Disabled Default**: Missing credentials evaluate to `SUPPORTED_NOT_CONFIGURED` without failing jobs or impeding Telegram delivery.

---

## Provider Capability Audit & API Contracts

### 1. X (Twitter) API v2

| Attribute | Specification |
| :--- | :--- |
| **Official Publish Endpoint** | `POST https://api.x.com/2/tweets` |
| **Lookup Endpoint** | `GET https://api.x.com/2/tweets/{id}` |
| **Delete Endpoint** | `DELETE https://api.x.com/2/tweets/{id}` |
| **Reconciliation Endpoint** | `GET https://api.x.com/2/users/me/tweets?max_results=5&tweet.fields=text` |
| **Authentication Model** | OAuth 1.0a User Context (Consumer Key, Consumer Secret, Access Token, Access Token Secret) or OAuth 2.0 PKCE |
| **Required Scopes** | `tweet.read`, `tweet.write`, `users.read` |
| **Text Character Limit** | 280 standard characters (handled by `X_POST` constraint of <= 280 chars) |
| **Rate Limits** | Header-driven (`x-rate-limit-limit`, `x-rate-limit-remaining`, `x-rate-limit-reset`, `Retry-After`); tier defaults (17-200+ tweets/24h or 15-min window) as documented fallbacks |
| **Native Idempotency** | `False` (public `POST /2/tweets` endpoint provides no client-side idempotency parameter; idempotency is strictly orchestrator-managed via `publication_key`) |
| **Orchestrator-Managed Idempotency** | `True` (immutable `publication_key` prevents duplicate attempts) |
| **Capability Status** | `CONNECTED_SUPPORTED` (when credentials set) / `SUPPORTED_NOT_CONFIGURED` (default) |

### 2. Meta Threads Graph API

| Attribute | Specification |
| :--- | :--- |
| **Step 1: Media Container** | `POST https://graph.threads.net/v1.0/{user-id}/threads` (`media_type=TEXT`, `text=...`) |
| **Step 2: Publish Container** | `POST https://graph.threads.net/v1.0/{user-id}/threads_publish` (`creation_id={container-id}`) |
| **Lookup Endpoint** | `GET https://graph.threads.net/v1.0/{threads-media-id}?fields=id,text,permalink` |
| **Delete Endpoint** | Not supported via public Graph API for published posts |
| **Reconciliation Endpoint** | `GET https://graph.threads.net/v1.0/{user-id}/threads?limit=5` |
| **Authentication Model** | Long-lived User Access Token (OAuth 2.0 User Token) |
| **Required Scopes** | `threads_basic`, `threads_content_publish` |
| **Text Character Limit** | 500 characters (handled by `THREADS_POST` constraint of <= 500 chars) |
| **Rate Limits** | Header-driven (`Retry-After`, usage headers); 250 published posts per 24-hour rolling window as documented fallback |
| **Native Idempotency** | `False` (Meta Threads Graph API exposes no client-side idempotency parameter; idempotency is strictly orchestrator-managed via `publication_key`) |
| **Orchestrator-Managed Idempotency** | `True` (immutable `publication_key` prevents duplicate attempts) |
| **Capability Status** | `CONNECTED_SUPPORTED` (when credentials set) / `SUPPORTED_NOT_CONFIGURED` (default) |

### 3. YouTube Community Posts

| Attribute | Specification |
| :--- | :--- |
| **Official Publishing API** | **None**. The legacy YouTube Data API `activities.insert` channel bulletin mechanism was deprecated and retired by Google. YouTube Data API v3 does not expose an endpoint to create Community posts. |
| **Engineering Policy** | **Strictly prohibited**: Browser automation, headless browsers (Playwright/Puppeteer/Selenium), UI clicking, session scraping, or private internal Google APIs. |
| **Truthful Status** | `UNSUPPORTED_OFFICIAL_API` |
| **Runtime Flow** | `READY_FOR_MANUAL_PUBLISH` (`MANUAL_EXPORT_READY`). The Telegram bot UI provides a copy-ready formatted text view for manual pasting by the channel manager, with no fake publish action. |

---

## Connector Interface (`app/worker/connectors.py`)

All platform integrations adhere to the typed `PublicationConnector` abstract base class:

```python
class CapabilityStatus(str, Enum):
    CONNECTED_SUPPORTED = "CONNECTED_SUPPORTED"
    SUPPORTED_NOT_CONFIGURED = "SUPPORTED_NOT_CONFIGURED"
    UNSUPPORTED_OFFICIAL_API = "UNSUPPORTED_OFFICIAL_API"
    DISABLED = "DISABLED"

class PublicationConnector(ABC):
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    async def validate_credentials(self) -> CredentialHealth: ...

    @abstractmethod
    async def publish(self, text: str, intent: PublicationIntent) -> PublishResult: ...

    @abstractmethod
    async def lookup(self, provider_post_id: str) -> LookupResult: ...

    @abstractmethod
    async def delete(self, provider_post_id: str) -> bool: ...

    @abstractmethod
    def classify_error(self, status_code: int, response_body: str) -> tuple[bool, str]: ...
```

### Connector Implementations
- **`XConnector`**: Handles OAuth 1.0a signing via `authlib`, executes `POST /2/tweets`, validates account identity against `settings.x_expected_user_id` / handle, and performs ambiguous timeout reconciliation via user timeline lookup.
- **`ThreadsConnector`**: Manages the two-stage container creation and publish flow, validates `settings.threads_expected_user_id`, and reconciles timeouts via recent threads search.
- **`YouTubeCommunityConnector`**: Returns `UNSUPPORTED_OFFICIAL_API` with clear manual export documentation.
- **`ConnectorRegistry`**: Global registry providing connector retrieval, runtime configuration, and mocking hooks for deterministic evaluation suites.

---

## PublicationIntent Data Contract

Stored in the relational `publication_intents` table:

```python
class PublicationIntent(BaseModel):
    intent_id: str
    package_id: str
    job_id: str
    target: TargetPlatform
    variant: OutputVariantType
    approved_by: int
    approved_at: datetime
    payload_hash: str              # Full SHA-256 of approved text
    publication_key: str           # {package_id}:{target}:{variant}:{payload_hash[:16]}
    status: PublicationIntentStatus
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    provider_post_id: str | None = None
    provider_url: str | None = None
```

### PublicationIntent Lifecycle States
- **`PENDING`**: Intent created upon owner approval, waiting for worker execution.
- **`IN_FLIGHT`**: Network request currently executing against platform API.
- **`SUCCEEDED`**: Platform acknowledged creation; `provider_post_id` persisted.
- **`FAILED`**: Terminal failure or retries exhausted.
- **`DELIVERY_UNKNOWN`**: Network timeout occurred; awaiting reconciliation lookup.
- **`APPROVAL_STALE`**: Text content modified after owner approved; publishing blocked.
- **`SUPPORTED_NOT_CONFIGURED`**: Connector supported by code, but credentials absent in environment.
- **`MANUAL_EXPORT_READY`**: Manual-only platform (YouTube Community); text ready for manual copying.

---

## Core Safety Invariants

### 1. Invariant: Stable Idempotency (`retry != new publication`)
- `publication_key` is calculated as:
  publication_key = package_id : target : variant : payload_hash[:16]
- This key remains **completely immutable** across all retry attempts.
- Technical execution attempts increment `attempt_id` on new `content_deliveries` rows, but share the identical `publication_key`.

### 2. Invariant: Stale Approval Guard
- Any re-render, regeneration, or manual edit of an output variant alters its SHA-256 payload hash.
- Before executing a network request, `execute_publication_intents` re-computes `compute_payload_hash(current_variant_text)`.
- If `current_hash != intent.payload_hash`, the intent transitions to `APPROVAL_STALE` and network dispatch is aborted.

### 3. Invariant: Ambiguous Result Reconciliation & Confidence Model
- When a POST request to an external provider times out or drops the connection after sending the payload, the publication outcome is ambiguous (the post may have been created on the server before the connection dropped).
- The connector enters reconciliation mode using the typed `ReconciliationConfidence` model:
  - `CONFIRMED_PRESENT`: Text match verified within the attempt window (`created_at >= attempt_started_at - 120s`). Recovers `provider_post_id`, marks intent `SUCCEEDED`, and eliminates duplicate posts.
  - `CONFIRMED_ABSENT`: Definitive authoritative negative assertion from the provider (e.g. manual-only connectors or guaranteed authoritative lookups). Permits scheduled retry backoff if retry attempts remain.
  - `INCONCLUSIVE`: Bounded query (e.g. recent 5 posts) did NOT find the post, or only matched older posts created prior to the attempt window. Because eventual consistency, replication lag, or timeline indexing delays can cause newly created posts to be temporarily invisible, absence cannot be proven.
- **Strict Inconclusive Safety**: `INCONCLUSIVE` results NEVER trigger automatic retry. The intent remains frozen in `DELIVERY_UNKNOWN` and the overall plan transitions to `MANUAL_RECONCILIATION_REQUIRED`.
- **Duplicate-Text Protection**: Older posts with identical text are excluded via timestamp windowing (`post_ts < attempt_started_at - 120s`), preventing accidental attribution to previous publications. Zero false-positive attributions.

### 4. Invariant: Provider Account Identity Guard
- Credentials might point to an incorrect personal account instead of the authorized brand handle.
- Before publishing, connectors verify the authenticated account identifier (`/2/users/me` or `/v1.0/me`) against `settings.x_expected_user_id` / `settings.threads_expected_user_id`.
- Mismatched identities trigger `ACCOUNT_MISMATCH` and abort publication.

### 5. Invariant: Package Lifecycle Truthfulness
- In Level A, a package reached `DELIVERED` even though external platforms were merely `APPROVED_NOT_CONNECTED`.
- In Level B, `reconcile_package_status` requires all **configured, required targets** in the plan to reach `DELIVERED`.
- Unconfigured targets (`SUPPORTED_NOT_CONFIGURED`) and manual targets (`READY_FOR_MANUAL_PUBLISH`) do not block package closure once primary Telegram user delivery succeeds.

---

## Retry Policies & Error Classification

| HTTP Status | Category | Connector Action | Next Step |
| :--- | :--- | :--- | :--- |
| **200 / 201** | Success | Parse `provider_post_id` | Status -> `SUCCEEDED` |
| **429** | Rate Limited | Inspect `Retry-After` header | Status -> `PENDING`, exponential backoff |
| **500 / 502 / 503 / 504** | Transient Server Error | Mark retryable | Status -> `PENDING`, exponential backoff |
| **Timeout / ConnectError** | Ambiguous Network Error | Run Reconciliation Lookup | Lookup success -> `SUCCEEDED`; absent -> Retry |
| **400 Bad Request** | Terminal Payload Error | Mark non-retryable | Status -> `FAILED` |
| **401 / 403** | Auth Failure | Invalidate credentials | Status -> `FAILED` |
| **422 Unprocessable** | Semantic Error | Mark non-retryable | Status -> `FAILED` |

### Backoff Schedule (`settings.publish_retry_backoff_seconds`)
- Attempt 1: 30 seconds
- Attempt 2: 120 seconds (2 minutes)
- Attempt 3: 300 seconds (5 minutes)
- Max attempts: 3 (`settings.publish_max_retries`)

---

## Security & Secrets Management

1. **Zero Secret Logging**: All connector logs and exception handlers pipe messages through `sanitize_sensitive_text()`. Authorization headers, Bearer tokens, OAuth signature strings, and client secrets are stripped via regex before logging.
2. **Environment Isolation**: API credentials reside exclusively in environment variables (`.env`), never checked into Git.
3. **Telegram Owner Verification**: Callback handlers verify `callback.from_user.id == job.user_id` on every approval action.

---

## Publication Orchestrator Core (ROADMAP Priority 5)

The **Publication Orchestrator Core** (`app/worker/publication_orchestrator.py`) provides the production-grade orchestration engine that safely converts owner approval into verified external publications.

### 1. Publication State Machine

The state machine explicitly differentiates terminal from intermediate states:

- `PENDING_APPROVAL`: Content package is generated; awaiting owner review.
- `APPROVED`: Owner approval cryptographically verified and bound; plan created.
- `READY`: Connector credentials verified, no active rate limits, eligible for worker dispatch.
- `ATTEMPTING`: Network request in-flight to external provider endpoint.
- `PUBLISHED`: Confirmed published with verified external post ID and canonical URL.
- `RETRYABLE_FAILURE`: Transient transport failure or 429 rate limit with scheduled backoff timestamp (`next_retry_at`).
- `AMBIGUOUS`: Network timeout or disconnection; requires reconciliation lookup before retry.
- `PERMANENT_FAILURE`: Terminal rejection (auth error 401/403, character limit breach, stale approval, or max retries exceeded).
- `CANCELLED`: Explicitly revoked by owner or system.

### 2. Exact Approval Binding (`OwnerApproval`)

No external publication can proceed without a cryptographically bound `OwnerApproval`:
- `approval_id`: Unique UUID.
- `package_id`: Immutable link to the target `ContentPackage`.
- `owner_id`: Telegram user ID of authorized owner (rejects unauthorized approvals).
- `content_hash`: Deterministic SHA-256 hash binding all rendered variant payloads (`compute_package_content_hash`).
- `target_platforms`: Exact list of approved destination platforms.
- `approved_at`: UTC timestamp.

**Staleness Invariant**: Any modification to approved variant text or package contents causes a hash mismatch, immediately marking the intent `APPROVAL_STALE` and blocking publication until fresh owner approval is granted.

### 3. Crash Consistency Boundaries

The orchestrator explicitly handles 5 crash boundaries:

- **Boundary A (Pre-dispatch Crash)**: Intent is marked `IN_FLIGHT` (`ATTEMPTING`) before network dispatch. If worker crashes, recovery checks provider timeline before attempting repost.
- **Boundary B (In-flight Timeout)**: Network disconnection or HTTP timeout transitions intent to `DELIVERY_UNKNOWN` (`AMBIGUOUS`). The orchestrator triggers ambiguous reconciliation; never blind-reposts!
- **Boundary C (External Success + Local Crash)**: Post succeeded on external platform, but local worker died before committing to PostgreSQL. On recovery, `reconcile_ambiguous_delivery` discovers the existing post via timeline snippet/ID lookup and marks `SUCCEEDED` without sending a duplicate `POST`.
- **Boundary D (Committed Success Replay)**: Already `SUCCEEDED` -> returns immediately with `ALREADY_PUBLISHED` no-op.
- **Boundary E (Retry after Ambiguity)**: Reconciliation confirms post does not exist before scheduling retry; prevents duplicate posts.

### 4. Per-Platform Isolation

Each target platform maintains an independent lifecycle. If Platform A (X) succeeds and Platform B (Threads) fails:
- Platform A is marked `DELIVERED`.
- Platform B is marked `FAILED` / `RETRYABLE_FAILURE`.
- Package status resolves to `PARTIALLY_DELIVERED`.
- Retrying the package only executes Platform B. Platform A is completely untouched and never reposted.

### 5. Idempotency Keys

- `publication_key`: Deterministic across all retries of the same content:
  `{package_id}:{target}:{variant}:{payload_hash[:16]}`
- `attempt_key`: Unique per network attempt:
  `{publication_key}:attempt_{attempt_id}`

---

## Multi-Platform Publication Expansion & Connector Parity (Priority 5 Slice 2)

Priority 5 Slice 2 elevates **Meta Threads** (`TargetPlatform.THREADS`) to full official API parity alongside X API v2, establishing a true multi-platform, owner-approved publication pipeline.

### 1. Platform Selection Rationale
- **Meta Threads**: Selected as Platform B because Meta exposes an official, documented Graph API (`POST /{user-id}/threads` followed by `POST /{user-id}/threads_publish`) with OAuth 2.0 User Access Tokens, compliant text limits (500 chars), and timeline lookup (`GET /{user-id}/threads?limit=5`).
- **YouTube Community**: Maintained strictly at `UNSUPPORTED_OFFICIAL_API` / `MANUAL_EXPORT_READY` because Google provides no official public API endpoint for Community posts. Automated browser hacks remain strictly forbidden.

### 2. Connector Capability Matrix

| Capability Field | X (Twitter) API v2 | Meta Threads Graph API | YouTube Community |
| :--- | :--- | :--- | :--- |
| **Status** | `CONNECTED_SUPPORTED` / `SUPPORTED_NOT_CONFIGURED` | `CONNECTED_SUPPORTED` / `SUPPORTED_NOT_CONFIGURED` | `UNSUPPORTED_OFFICIAL_API` |
| **Publication Mode** | `MANUAL_APPROVAL` | `MANUAL_APPROVAL` | `MANUAL_EXPORT` |
| **Max Text Characters** | 280 | 500 | 2000 |
| **Supports Text** | `True` | `True` | `False` |
| **Supports Image** | `False` (v1 text-focused) | `False` (v1 text-focused) | `False` |
| **Supports Video** | `False` (v1 text-focused) | `False` (v1 text-focused) | `False` |
| **Supports Edit** | `False` | `False` | `False` |
| **Supports Delete** | `True` (`DELETE /2/tweets/{id}`) | `False` (not in public Graph API) | `False` |
| **Supports Lookup** | `True` (`GET /2/tweets/{id}`) | `True` (`GET /{threads-media-id}`) | `False` |
| **Supports Timeline Reconciliation** | `True` (`GET /2/users/me/tweets?max_results=5`) | `True` (`GET /{user-id}/threads?limit=5`) | `False` |
| **Supports Native Idempotency** | `False` | `False` | `False` |
| **Orchestrator Managed Idempotency** | `True` | `True` | `False` |
| **Max Media Count** | 0 | 0 | 0 |
| **Supported MIME Types** | `["text/plain"]` | `["text/plain"]` | `["text/plain"]` |
| **Rate Limit Model** | `header_driven_x_rate_limit_or_tier_fallback` | `header_driven_threads_250_rolling_24h` | `none_manual_export` |

### 3. Approval Target-Set Binding Semantics
- `OwnerApproval` contains `target_platforms: list[TargetPlatform]`.
- Content integrity is protected by `content_hash`: a cryptographic integrity hash used to bind owner approval to exact rendered content (not a digital signature).
- **Target Authorization Guard**: An approval is valid ONLY for its explicitly designated targets. Attempting to execute an intent for a target outside `approval.target_platforms` immediately fails with `TARGET_NOT_APPROVED` without making any network call.
- Empty target platform lists are rejected with `TARGET_SET_EMPTY`.

### 4. PublicationPlan Status Aggregation Semantics
A multi-platform plan is never reduced to a simplistic binary boolean. The aggregate status is deterministically derived from all active intents:
1. **`ALL_PENDING`**: All active targets are awaiting initial dispatch.
2. **`ATTEMPTING`**: One or more active targets are currently in-flight.
3. **`MANUAL_RECONCILIATION_REQUIRED`**: One or more targets are in ambiguous `DELIVERY_UNKNOWN` state requiring manual inspection.
4. **`RETRY_PENDING`**: One or more targets encountered transient failures and are waiting for scheduled exponential backoff retry.
5. **`ALL_SUCCEEDED`**: All active targets confirmed published successfully.
6. **`PARTIAL_SUCCESS`**: At least one target succeeded and at least one target permanently failed (with no retries remaining).
7. **`TERMINAL_FAILURE`**: All active targets permanently failed with zero successes.
8. **`CANCELLED`**: The plan or package was explicitly revoked/cancelled.

### 5. Threads Ambiguity Reconciliation & Confidence Protocol
When a Threads publication attempt experiences a network timeout or connection reset:
1. Transition intent to `DELIVERY_UNKNOWN`.
2. Inspect `reconcile_ambiguous_delivery()`:
   - If `provider_post_id` is present, look up the media container via `GET /{threads-media-id}`.
   - If missing `provider_post_id`, query recent threads via `GET /{user-id}/threads?limit=5&fields=id,text,permalink,timestamp`.
   - **Timestamp Window Filtering**: Posts with timestamps older than `attempt_started_at - 120s` are discarded as prior identical publications.
   - If matching text is found within the valid timestamp window: evaluates to `ReconciliationConfidence.CONFIRMED_PRESENT` (`resolved=True, published=True`), resolves `provider_post_id`, and transitions to `SUCCEEDED` without re-posting.
   - If definitively absent via authoritative provider confirmation: evaluates to `ReconciliationConfidence.CONFIRMED_ABSENT` and schedules retry.
   - If not found in the bounded 5-post query or only matched older duplicate posts: evaluates strictly to `ReconciliationConfidence.INCONCLUSIVE` (`resolved=False, published=False`). Because timeline indexing delays or replication lag cannot definitively prove absence, automatic retry is prohibited. The intent remains frozen in `DELIVERY_UNKNOWN` and the overall plan status transitions to `MANUAL_RECONCILIATION_REQUIRED`.
   - Zero blind reposts, zero duplicate publications, and zero false-positive duplicate text attributions.


