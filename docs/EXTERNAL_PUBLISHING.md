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
| **Native Idempotency** | Supported via client request or application-level `publication_key` |
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

### 3. Invariant: Ambiguous Result Reconciliation
- When a POST request to an external provider times out or drops the connection after sending the payload, the publication outcome is ambiguous (the post may have been created on the server before the connection dropped).
- The connector catches timeout exceptions and enters reconciliation mode:
  - For X: Queries `GET https://api.x.com/2/users/me/tweets?max_results=5&tweet.fields=text` for recent user tweets matching payload.
  - For Threads: Queries `GET /{user-id}/threads?limit=5` for recent media.
- If the post is confirmed present: recovers `provider_post_id` and marks `SUCCEEDED` without re-posting.
- If confirmed absent: marks error as retryable for scheduled backoff.

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
