"""External platform publishing connectors for Level B distribution.

Implements official API contracts for X (Twitter) and Meta Threads,
with formal classification of YouTube Community as unsupported official API (manual export).
Strictly adheres to:
- No browser automation / unofficial APIs
- Token masking & credential sanitizer (never log secrets)
- Stable publication idempotency (retry != new publication)
- Ambiguous timeout reconciliation
- Account identity guards
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.config import settings
from app.worker.content_package import (
    DeliveryOutcome,
    OutputVariantType,
    PublicationIntent,
    PublicationMode,
    TargetPlatform,
)

logger = logging.getLogger(__name__)

# Sanitizer pattern to prevent logging sensitive auth tokens or header values
TOKEN_SANITIZER_RE = re.compile(
    r"(bearer\s+[\w\-\.]+|(?:api[_-]?key|secret|token|password)[\"'\s:=]+[\w\-\.]+)",
    re.IGNORECASE,
)


def sanitize_sensitive_text(text: str) -> str:
    """Mask credentials and tokens in log strings and exceptions."""
    if not text:
        return ""
    return TOKEN_SANITIZER_RE.sub("[REDACTED_CREDENTIAL]", text)


class CapabilityStatus(str, Enum):
    CONNECTED_SUPPORTED = "CONNECTED_SUPPORTED"
    SUPPORTED_NOT_CONFIGURED = "SUPPORTED_NOT_CONFIGURED"
    UNSUPPORTED_OFFICIAL_API = "UNSUPPORTED_OFFICIAL_API"
    DISABLED = "DISABLED"


class ConnectorCapabilities(BaseModel):
    target: TargetPlatform
    status: CapabilityStatus
    publication_mode: PublicationMode
    max_chars: int
    supports_text: bool = True
    supports_image: bool = False
    supports_video: bool = False
    supports_edit: bool = False
    supports_delete: bool = False
    supports_lookup: bool = False
    supports_timeline_reconciliation: bool = False
    supports_native_idempotency: bool = False
    max_media_count: int = 0
    supported_mime_types: list[str] = Field(default_factory=lambda: ["text/plain"])
    rate_limit_model: str = ""
    auth_model: str
    official_endpoint: str
    known_restrictions: list[str] = Field(default_factory=list)


class CredentialValidationResult(BaseModel):
    is_valid: bool
    status: CapabilityStatus
    account_id: str | None = None
    account_username: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class RateLimitInfo(BaseModel):
    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None
    retry_after_seconds: int | None = None


def extract_rate_limit_info(headers: httpx.Headers | dict[str, str] | None) -> RateLimitInfo:
    """Extract standard rate limit metrics from provider HTTP response headers.

    Extracts:
    - x-rate-limit-limit (X / Twitter standard)
    - x-rate-limit-remaining (X / Twitter standard)
    - x-rate-limit-reset (X / Twitter standard, UTC epoch seconds)
    - retry-after (HTTP standard, seconds)
    """
    if not headers:
        return RateLimitInfo()

    hdr = {k.lower(): v for k, v in headers.items()}
    limit_val: int | None = None
    remaining_val: int | None = None
    reset_epoch_val: int | None = None
    retry_after_val: int | None = None

    if "x-rate-limit-limit" in hdr:
        try:
            limit_val = int(hdr["x-rate-limit-limit"])
        except (ValueError, TypeError):
            pass

    if "x-rate-limit-remaining" in hdr:
        try:
            remaining_val = int(hdr["x-rate-limit-remaining"])
        except (ValueError, TypeError):
            pass

    if "x-rate-limit-reset" in hdr:
        try:
            reset_epoch_val = int(hdr["x-rate-limit-reset"])
            now_ts = int(datetime.now(timezone.utc).timestamp())
            if reset_epoch_val > now_ts:
                retry_after_val = reset_epoch_val - now_ts
        except (ValueError, TypeError):
            pass

    if "retry-after" in hdr:
        try:
            retry_after_val = int(hdr["retry-after"])
        except (ValueError, TypeError):
            pass

    return RateLimitInfo(
        limit=limit_val,
        remaining=remaining_val,
        reset_epoch=reset_epoch_val,
        retry_after_seconds=retry_after_val,
    )


class PublicationResult(BaseModel):
    success: bool
    provider_post_id: str | None = None
    provider_url: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    is_ambiguous: bool = False
    is_retryable: bool = False
    response_payload: dict[str, Any] = Field(default_factory=dict)
    rate_limit_info: RateLimitInfo | None = None
    retry_after_seconds: int | None = None


class LookupResult(BaseModel):
    found: bool
    provider_post_id: str | None = None
    text: str | None = None
    author_id: str | None = None
    author_username: str | None = None
    created_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    raw_metrics: dict[str, Any] = Field(default_factory=dict)
    permalink: str | None = None
    rate_limit_info: RateLimitInfo | None = None


class AmbiguousReconciliationResult(BaseModel):
    resolved: bool
    published: bool
    provider_post_id: str | None = None
    provider_url: str | None = None
    reason: str | None = None


class PublicationConnector(ABC):
    """Abstract interface for all external platform distribution connectors."""

    @abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        """Return provider capabilities and official API metadata."""

    @abstractmethod
    async def validate_credentials(self) -> CredentialValidationResult:
        """Validate configured credentials and check expected account identity."""

    @abstractmethod
    async def publish(self, text: str, intent: PublicationIntent) -> PublicationResult:
        """Publish text payload to the external platform."""

    @abstractmethod
    async def lookup(self, provider_post_id: str) -> LookupResult:
        """Look up existing post by provider post ID."""

    @abstractmethod
    async def delete(self, provider_post_id: str) -> bool:
        """Delete post if supported by the provider API."""

    @abstractmethod
    def classify_error(self, status_code: int | None, error_data: Any) -> tuple[bool, str]:
        """Return (is_retryable, error_code)."""

    @abstractmethod
    async def reconcile_ambiguous_delivery(
        self, intent: PublicationIntent, text: str
    ) -> AmbiguousReconciliationResult:
        """Reconcile post status when request timed out or returned ambiguous response."""


class XConnector(PublicationConnector):
    """Official X (Twitter) API v2 text tweet publication connector."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        access_token_secret: str | None = None,
        bearer_token: str | None = None,
        expected_user_id: str | None = None,
        expected_username: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or getattr(settings, "x_api_key", None)
        self.api_secret = api_secret or getattr(settings, "x_api_secret", None)
        self.access_token = access_token or getattr(settings, "x_access_token", None)
        self.access_token_secret = access_token_secret or getattr(
            settings, "x_access_token_secret", None
        )
        self.bearer_token = bearer_token or getattr(settings, "x_bearer_token", None)
        self.expected_user_id = expected_user_id or getattr(
            settings, "expected_x_user_id", None
        )
        self.expected_username = expected_username or getattr(
            settings, "expected_x_username", None
        )
        self._client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=30.0)

    def _has_credentials(self) -> bool:
        return bool(self.bearer_token or (self.access_token and self.api_key))

    def _get_auth_headers(self) -> dict[str, str]:
        if self.bearer_token:
            return {"Authorization": f"Bearer {self.bearer_token}"}
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {}

    def capabilities(self) -> ConnectorCapabilities:
        status = (
            CapabilityStatus.CONNECTED_SUPPORTED
            if self._has_credentials()
            else CapabilityStatus.SUPPORTED_NOT_CONFIGURED
        )
        return ConnectorCapabilities(
            target=TargetPlatform.X,
            status=status,
            publication_mode=PublicationMode.MANUAL_APPROVAL,
            max_chars=280,
            supports_text=True,
            supports_image=False,
            supports_video=False,
            supports_edit=False,
            supports_delete=True,
            supports_lookup=True,
            supports_timeline_reconciliation=True,
            supports_native_idempotency=True,
            max_media_count=0,
            supported_mime_types=["text/plain"],
            rate_limit_model="header_driven_x_rate_limit_or_tier_fallback",
            auth_model="OAuth 2.0 User Context / OAuth 1.0a",
            official_endpoint="POST https://api.x.com/2/tweets",
            known_restrictions=[
                "Text must be <= 280 characters",
                "Rate limit: dynamically governed by x-rate-limit-* and Retry-After headers (tier defaults 17-200+ posts/24h)",
                "Media upload requires separate v1.1 endpoint (deferred)",
            ],
        )

    async def validate_credentials(self) -> CredentialValidationResult:
        if not self._has_credentials():
            return CredentialValidationResult(
                is_valid=False,
                status=CapabilityStatus.SUPPORTED_NOT_CONFIGURED,
                error_code="CREDENTIALS_MISSING",
                error_message="X API credentials not configured in environment.",
            )

        client = self._get_client()
        headers = self._get_auth_headers()
        try:
            resp = await client.get("https://api.x.com/2/users/me", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                user_id = str(data.get("id", ""))
                username = data.get("username", "")

                # Provider account identity guard
                if self.expected_user_id and user_id != str(self.expected_user_id):
                    return CredentialValidationResult(
                        is_valid=False,
                        status=CapabilityStatus.CONNECTED_SUPPORTED,
                        error_code="ACCOUNT_IDENTITY_MISMATCH",
                        error_message=f"X authenticated user_id {user_id} does not match expected {self.expected_user_id}",
                    )
                if self.expected_username and username.lower() != self.expected_username.lower():
                    return CredentialValidationResult(
                        is_valid=False,
                        status=CapabilityStatus.CONNECTED_SUPPORTED,
                        error_code="ACCOUNT_IDENTITY_MISMATCH",
                        error_message=f"X authenticated username @{username} does not match expected @{self.expected_username}",
                    )

                return CredentialValidationResult(
                    is_valid=True,
                    status=CapabilityStatus.CONNECTED_SUPPORTED,
                    account_id=user_id,
                    account_username=username,
                )
            else:
                retryable, err_code = self.classify_error(resp.status_code, resp.text)
                return CredentialValidationResult(
                    is_valid=False,
                    status=CapabilityStatus.CONNECTED_SUPPORTED,
                    error_code=err_code,
                    error_message=sanitize_sensitive_text(resp.text),
                )
        except Exception as e:
            return CredentialValidationResult(
                is_valid=False,
                status=CapabilityStatus.CONNECTED_SUPPORTED,
                error_code="NETWORK_ERROR",
                error_message=sanitize_sensitive_text(str(e)),
            )

    async def publish(self, text: str, intent: PublicationIntent) -> PublicationResult:
        if not self._has_credentials():
            return PublicationResult(
                success=False,
                error_code="SUPPORTED_NOT_CONFIGURED",
                error_message="Cannot publish to X: credentials not configured.",
                is_retryable=False,
            )

        if len(text) > 280:
            return PublicationResult(
                success=False,
                error_code="BUDGET_EXCEEDED",
                error_message=f"X_POST length {len(text)} exceeds limit of 280 characters.",
                is_retryable=False,
            )

        client = self._get_client()
        headers = {**self._get_auth_headers(), "Content-Type": "application/json"}
        payload = {"text": text}

        try:
            resp = await client.post(
                "https://api.x.com/2/tweets",
                headers=headers,
                json=payload,
            )
            rate_info = extract_rate_limit_info(resp.headers)
            if resp.status_code in (200, 201):
                data = resp.json().get("data", {})
                tweet_id = str(data.get("id", ""))
                url = f"https://x.com/i/status/{tweet_id}" if tweet_id else None
                return PublicationResult(
                    success=True,
                    provider_post_id=tweet_id,
                    provider_url=url,
                    http_status=resp.status_code,
                    response_payload=resp.json(),
                    rate_limit_info=rate_info,
                    retry_after_seconds=rate_info.retry_after_seconds,
                )
            else:
                is_retryable, err_code = self.classify_error(resp.status_code, resp.text)
                return PublicationResult(
                    success=False,
                    http_status=resp.status_code,
                    error_code=err_code,
                    error_message=sanitize_sensitive_text(resp.text),
                    is_retryable=is_retryable,
                    response_payload=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {},
                    rate_limit_info=rate_info,
                    retry_after_seconds=rate_info.retry_after_seconds,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as net_err:
            logger.warning("X publish ambiguous timeout: %s", sanitize_sensitive_text(str(net_err)))
            return PublicationResult(
                success=False,
                error_code="DELIVERY_UNKNOWN",
                error_message=sanitize_sensitive_text(str(net_err)),
                is_ambiguous=True,
                is_retryable=True,
            )
        except Exception as e:
            return PublicationResult(
                success=False,
                error_code="UNKNOWN_ERROR",
                error_message=sanitize_sensitive_text(str(e)),
                is_retryable=False,
            )

    async def lookup(self, provider_post_id: str) -> LookupResult:
        if not self._has_credentials() or not provider_post_id:
            return LookupResult(found=False, error_code="NOT_CONFIGURED_OR_EMPTY_ID")

        client = self._get_client()
        headers = self._get_auth_headers()
        url = f"https://api.x.com/2/tweets/{provider_post_id}?tweet.fields=text,created_at,author_id,public_metrics"

        try:
            resp = await client.get(url, headers=headers)
            rate_info = extract_rate_limit_info(resp.headers)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                raw_metrics = data.get("public_metrics", {}) or {}
                permalink = f"https://x.com/i/status/{provider_post_id}"
                return LookupResult(
                    found=True,
                    provider_post_id=str(data.get("id", "") or provider_post_id),
                    text=data.get("text"),
                    author_id=str(data.get("author_id", "")),
                    created_at=data.get("created_at"),
                    http_status=200,
                    raw_metrics=raw_metrics,
                    permalink=permalink,
                    rate_limit_info=rate_info,
                )
            elif resp.status_code == 404:
                return LookupResult(
                    found=False,
                    http_status=404,
                    error_code="POST_NOT_FOUND",
                    error_message="Post not found on X.",
                    rate_limit_info=rate_info,
                )
            elif resp.status_code in (401, 403):
                return LookupResult(
                    found=False,
                    http_status=resp.status_code,
                    error_code="AUTH_REQUIRED",
                    error_message=sanitize_sensitive_text(resp.text),
                    rate_limit_info=rate_info,
                )
            elif resp.status_code == 429:
                return LookupResult(
                    found=False,
                    http_status=429,
                    error_code="RATE_LIMITED",
                    error_message=sanitize_sensitive_text(resp.text),
                    rate_limit_info=rate_info,
                )
            else:
                _, err_code = self.classify_error(resp.status_code, resp.text)
                return LookupResult(
                    found=False,
                    http_status=resp.status_code,
                    error_code=err_code,
                    error_message=sanitize_sensitive_text(resp.text),
                    rate_limit_info=rate_info,
                )
        except Exception as e:
            return LookupResult(
                found=False,
                error_code="NETWORK_ERROR",
                error_message=sanitize_sensitive_text(str(e)),
            )

    async def delete(self, provider_post_id: str) -> bool:
        if not self._has_credentials() or not provider_post_id:
            return False

        client = self._get_client()
        headers = self._get_auth_headers()
        url = f"https://api.x.com/2/tweets/{provider_post_id}"
        try:
            resp = await client.delete(url, headers=headers)
            return resp.status_code in (200, 204)
        except Exception:
            return False

    def classify_error(self, status_code: int | None, error_data: Any) -> tuple[bool, str]:
        if status_code is None:
            return True, "NETWORK_TIMEOUT"
        if status_code == 429:
            return True, "RATE_LIMITED"
        if status_code in (500, 502, 503, 504):
            return True, f"SERVER_ERROR_{status_code}"
        if status_code in (401, 403):
            return False, f"AUTH_ERROR_{status_code}"
        if status_code == 400:
            return False, "INVALID_PAYLOAD"
        return False, f"HTTP_{status_code}"

    async def reconcile_ambiguous_delivery(
        self, intent: PublicationIntent, text: str
    ) -> AmbiguousReconciliationResult:
        """Attempt to check if tweet was published despite network timeout."""
        if intent.provider_post_id:
            lookup_res = await self.lookup(intent.provider_post_id)
            if lookup_res.found:
                return AmbiguousReconciliationResult(
                    resolved=True,
                    published=True,
                    provider_post_id=lookup_res.provider_post_id,
                    provider_url=f"https://x.com/i/status/{lookup_res.provider_post_id}",
                    reason="Verified existing post by ID lookup",
                )

        # Without provider post ID, search user's recent tweets
        client = self._get_client()
        headers = self._get_auth_headers()
        try:
            resp = await client.get(
                "https://api.x.com/2/users/me/tweets?max_results=5&tweet.fields=text",
                headers=headers,
            )
            if resp.status_code == 200:
                tweets = resp.json().get("data", [])
                target_snippet = text.strip()[:100]
                for tw in tweets:
                    tw_text = tw.get("text", "").strip()
                    if target_snippet and target_snippet in tw_text:
                        tw_id = str(tw.get("id"))
                        return AmbiguousReconciliationResult(
                            resolved=True,
                            published=True,
                            provider_post_id=tw_id,
                            provider_url=f"https://x.com/i/status/{tw_id}",
                            reason="Matched recent tweet text",
                        )
                # Not found in recent tweets -> safe to retry
                return AmbiguousReconciliationResult(
                    resolved=True,
                    published=False,
                    reason="Post not found in recent user tweets; retry permitted",
                )
        except Exception as e:
            logger.error("X reconciliation error: %s", sanitize_sensitive_text(str(e)))

        return AmbiguousReconciliationResult(
            resolved=False,
            published=False,
            reason="Could not definitively prove or disprove tweet creation",
        )


class ThreadsConnector(PublicationConnector):
    """Official Meta Threads Graph API two-step publication connector."""

    def __init__(
        self,
        access_token: str | None = None,
        user_id: str | None = None,
        expected_user_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = access_token or getattr(settings, "threads_access_token", None)
        self.user_id = user_id or getattr(settings, "threads_user_id", None)
        self.expected_user_id = expected_user_id or getattr(
            settings, "expected_threads_user_id", None
        )
        self._client = http_client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=30.0)

    def _has_credentials(self) -> bool:
        return bool(self.access_token and self.user_id)

    def capabilities(self) -> ConnectorCapabilities:
        status = (
            CapabilityStatus.CONNECTED_SUPPORTED
            if self._has_credentials()
            else CapabilityStatus.SUPPORTED_NOT_CONFIGURED
        )
        return ConnectorCapabilities(
            target=TargetPlatform.THREADS,
            status=status,
            publication_mode=PublicationMode.MANUAL_APPROVAL,
            max_chars=500,
            supports_text=True,
            supports_image=False,
            supports_video=False,
            supports_edit=False,
            supports_delete=False,
            supports_lookup=True,
            supports_timeline_reconciliation=True,
            supports_native_idempotency=False,
            max_media_count=0,
            supported_mime_types=["text/plain"],
            rate_limit_model="header_driven_threads_250_rolling_24h",
            auth_model="OAuth 2.0 Bearer Token (Meta Graph API)",
            official_endpoint="POST https://graph.threads.net/v1.0/{user-id}/threads",
            known_restrictions=[
                "Two-step container publish (create container -> publish)",
                "Text must be <= 500 characters",
                "Rate limit: dynamically governed by Retry-After and usage headers (default 250 posts/24h)",
                "Programmatic post deletion is not supported by Threads API",
            ],
        )

    async def validate_credentials(self) -> CredentialValidationResult:
        if not self._has_credentials():
            return CredentialValidationResult(
                is_valid=False,
                status=CapabilityStatus.SUPPORTED_NOT_CONFIGURED,
                error_code="CREDENTIALS_MISSING",
                error_message="Threads access token or user_id not configured.",
            )

        client = self._get_client()
        url = f"https://graph.threads.net/v1.0/me?fields=id,username&access_token={self.access_token}"
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                me_id = str(data.get("id", ""))
                username = data.get("username", "")

                if self.expected_user_id and me_id != str(self.expected_user_id):
                    return CredentialValidationResult(
                        is_valid=False,
                        status=CapabilityStatus.CONNECTED_SUPPORTED,
                        error_code="ACCOUNT_IDENTITY_MISMATCH",
                        error_message=f"Threads authenticated user {me_id} does not match expected {self.expected_user_id}",
                    )

                return CredentialValidationResult(
                    is_valid=True,
                    status=CapabilityStatus.CONNECTED_SUPPORTED,
                    account_id=me_id,
                    account_username=username,
                )
            else:
                _, err_code = self.classify_error(resp.status_code, resp.text)
                return CredentialValidationResult(
                    is_valid=False,
                    status=CapabilityStatus.CONNECTED_SUPPORTED,
                    error_code=err_code,
                    error_message=sanitize_sensitive_text(resp.text),
                )
        except Exception as e:
            return CredentialValidationResult(
                is_valid=False,
                status=CapabilityStatus.CONNECTED_SUPPORTED,
                error_code="NETWORK_ERROR",
                error_message=sanitize_sensitive_text(str(e)),
            )

    async def publish(self, text: str, intent: PublicationIntent) -> PublicationResult:
        if not self._has_credentials():
            return PublicationResult(
                success=False,
                error_code="SUPPORTED_NOT_CONFIGURED",
                error_message="Cannot publish to Threads: credentials not configured.",
                is_retryable=False,
            )

        if len(text) > 500:
            return PublicationResult(
                success=False,
                error_code="BUDGET_EXCEEDED",
                error_message=f"THREADS_POST length {len(text)} exceeds limit of 500 characters.",
                is_retryable=False,
            )

        client = self._get_client()

        # Step 1: Create media/text container
        container_url = f"https://graph.threads.net/v1.0/{self.user_id}/threads"
        container_payload = {
            "media_type": "TEXT",
            "text": text,
            "access_token": self.access_token,
        }

        try:
            c_resp = await client.post(container_url, data=container_payload)
            c_rate_info = extract_rate_limit_info(c_resp.headers)
            if c_resp.status_code not in (200, 201):
                is_retryable, err_code = self.classify_error(c_resp.status_code, c_resp.text)
                return PublicationResult(
                    success=False,
                    http_status=c_resp.status_code,
                    error_code=err_code,
                    error_message=f"Container creation failed: {sanitize_sensitive_text(c_resp.text)}",
                    is_retryable=is_retryable,
                    rate_limit_info=c_rate_info,
                    retry_after_seconds=c_rate_info.retry_after_seconds,
                )

            container_id = str(c_resp.json().get("id", ""))
            if not container_id:
                return PublicationResult(
                    success=False,
                    error_code="CONTAINER_ID_MISSING",
                    error_message="Threads container response did not contain an ID.",
                    is_retryable=False,
                    rate_limit_info=c_rate_info,
                    retry_after_seconds=c_rate_info.retry_after_seconds,
                )

            # Step 2: Publish container
            publish_url = f"https://graph.threads.net/v1.0/{self.user_id}/threads_publish"
            publish_payload = {
                "creation_id": container_id,
                "access_token": self.access_token,
            }
            p_resp = await client.post(publish_url, data=publish_payload)
            p_rate_info = extract_rate_limit_info(p_resp.headers)
            if p_resp.status_code in (200, 201):
                post_id = str(p_resp.json().get("id", ""))
                url = f"https://www.threads.net/t/{post_id}" if post_id else None
                return PublicationResult(
                    success=True,
                    provider_post_id=post_id,
                    provider_url=url,
                    http_status=p_resp.status_code,
                    response_payload=p_resp.json(),
                    rate_limit_info=p_rate_info,
                    retry_after_seconds=p_rate_info.retry_after_seconds,
                )
            else:
                is_retryable, err_code = self.classify_error(p_resp.status_code, p_resp.text)
                return PublicationResult(
                    success=False,
                    http_status=p_resp.status_code,
                    error_code=err_code,
                    error_message=f"Container publish failed: {sanitize_sensitive_text(p_resp.text)}",
                    is_retryable=is_retryable,
                    rate_limit_info=p_rate_info,
                    retry_after_seconds=p_rate_info.retry_after_seconds,
                )

        except (httpx.TimeoutException, httpx.NetworkError) as net_err:
            logger.warning("Threads publish ambiguous timeout: %s", sanitize_sensitive_text(str(net_err)))
            return PublicationResult(
                success=False,
                error_code="DELIVERY_UNKNOWN",
                error_message=sanitize_sensitive_text(str(net_err)),
                is_ambiguous=True,
                is_retryable=True,
            )
        except Exception as e:
            return PublicationResult(
                success=False,
                error_code="UNKNOWN_ERROR",
                error_message=sanitize_sensitive_text(str(e)),
                is_retryable=False,
            )

    async def lookup(self, provider_post_id: str) -> LookupResult:
        if not self._has_credentials() or not provider_post_id:
            return LookupResult(found=False, error_code="NOT_CONFIGURED_OR_EMPTY_ID")

        client = self._get_client()
        url = (
            f"https://graph.threads.net/v1.0/{provider_post_id}"
            f"?fields=id,text,permalink,timestamp&access_token={self.access_token}"
        )
        try:
            resp = await client.get(url)
            rate_info = extract_rate_limit_info(resp.headers)
            if resp.status_code == 200:
                data = resp.json()
                permalink = data.get("permalink")
                raw_metrics: dict[str, Any] = {}

                # Query official Meta Threads Insights endpoint
                try:
                    insights_url = (
                        f"https://graph.threads.net/v1.0/{provider_post_id}/insights"
                        f"?metric=views,likes,replies,reposts,quotes&access_token={self.access_token}"
                    )
                    insights_resp = await client.get(insights_url)
                    if insights_resp.status_code == 200:
                        idata = insights_resp.json().get("data", [])
                        for item in idata:
                            metric_name = item.get("name")
                            values = item.get("values", [])
                            if metric_name and values:
                                val = values[0].get("value")
                                if val is not None:
                                    raw_metrics[metric_name] = val
                            elif metric_name and item.get("total_value") is not None:
                                raw_metrics[metric_name] = item.get("total_value", {}).get("value")
                except Exception as insights_err:
                    logger.debug("Threads insights fetch non-fatal error: %s", insights_err)

                return LookupResult(
                    found=True,
                    provider_post_id=str(data.get("id", "") or provider_post_id),
                    text=data.get("text"),
                    created_at=data.get("timestamp"),
                    http_status=200,
                    raw_metrics=raw_metrics,
                    permalink=permalink,
                    rate_limit_info=rate_info,
                )
            elif resp.status_code == 404:
                return LookupResult(
                    found=False,
                    http_status=404,
                    error_code="POST_NOT_FOUND",
                    error_message="Post not found on Threads.",
                    rate_limit_info=rate_info,
                )
            elif resp.status_code in (401, 403):
                return LookupResult(
                    found=False,
                    http_status=resp.status_code,
                    error_code="AUTH_REQUIRED",
                    error_message=sanitize_sensitive_text(resp.text),
                    rate_limit_info=rate_info,
                )
            elif resp.status_code == 429:
                return LookupResult(
                    found=False,
                    http_status=429,
                    error_code="RATE_LIMITED",
                    error_message=sanitize_sensitive_text(resp.text),
                    rate_limit_info=rate_info,
                )
            else:
                _, err_code = self.classify_error(resp.status_code, resp.text)
                return LookupResult(
                    found=False,
                    http_status=resp.status_code,
                    error_code=err_code,
                    error_message=sanitize_sensitive_text(resp.text),
                    rate_limit_info=rate_info,
                )
        except Exception as e:
            return LookupResult(
                found=False,
                error_code="NETWORK_ERROR",
                error_message=sanitize_sensitive_text(str(e)),
            )

    async def delete(self, provider_post_id: str) -> bool:
        # Threads API does not currently support programmatic post deletion
        return False

    def classify_error(self, status_code: int | None, error_data: Any) -> tuple[bool, str]:
        if status_code is None:
            return True, "NETWORK_TIMEOUT"
        if status_code == 429:
            return True, "RATE_LIMITED"
        if status_code in (500, 502, 503, 504):
            return True, f"SERVER_ERROR_{status_code}"
        if status_code in (401, 403):
            return False, f"AUTH_ERROR_{status_code}"
        if status_code == 400:
            return False, "INVALID_PAYLOAD"
        return False, f"HTTP_{status_code}"

    async def reconcile_ambiguous_delivery(
        self, intent: PublicationIntent, text: str
    ) -> AmbiguousReconciliationResult:
        if intent.provider_post_id:
            lookup_res = await self.lookup(intent.provider_post_id)
            if lookup_res.found:
                return AmbiguousReconciliationResult(
                    resolved=True,
                    published=True,
                    provider_post_id=lookup_res.provider_post_id,
                    provider_url=f"https://www.threads.net/t/{lookup_res.provider_post_id}",
                    reason="Verified existing Threads post by ID",
                )

        if not self._has_credentials():
            return AmbiguousReconciliationResult(
                resolved=False,
                published=False,
                reason="Threads credentials not configured; manual reconciliation required",
            )

        # Without provider post ID, query recent user threads to verify if post was created
        client = self._get_client()
        url = (
            f"https://graph.threads.net/v1.0/{self.user_id}/threads"
            f"?limit=5&fields=id,text,permalink&access_token={self.access_token}"
        )
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                threads = resp.json().get("data", [])
                target_snippet = text.strip()[:100]
                for th in threads:
                    th_text = th.get("text", "").strip()
                    if target_snippet and target_snippet in th_text:
                        th_id = str(th.get("id"))
                        permalink = th.get("permalink") or f"https://www.threads.net/t/{th_id}"
                        return AmbiguousReconciliationResult(
                            resolved=True,
                            published=True,
                            provider_post_id=th_id,
                            provider_url=permalink,
                            reason="Matched recent Threads post text",
                        )
                # Confirmed not found in recent user threads -> safe to retry
                return AmbiguousReconciliationResult(
                    resolved=True,
                    published=False,
                    reason="Post not found in recent user threads; retry permitted",
                )
            else:
                logger.error(
                    "Threads reconciliation query failed: status %s %s",
                    resp.status_code,
                    sanitize_sensitive_text(resp.text),
                )
        except Exception as e:
            logger.error("Threads reconciliation error: %s", sanitize_sensitive_text(str(e)))

        return AmbiguousReconciliationResult(
            resolved=False,
            published=False,
            reason="Could not definitively prove or disprove Threads post creation; manual reconciliation required",
        )


class YouTubeCommunityConnector(PublicationConnector):
    """Adapter for YouTube Community Posts.

    Officially classifies YouTube Community as UNSUPPORTED_OFFICIAL_API.
    The YouTube Data API v3 does not expose an endpoint to create Community Posts.
    Browser automation is strictly forbidden.
    This connector directs the flow to manual export.
    """

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target=TargetPlatform.YOUTUBE_COMMUNITY,
            status=CapabilityStatus.UNSUPPORTED_OFFICIAL_API,
            publication_mode=PublicationMode.MANUAL_EXPORT,
            max_chars=2000,
            supports_text=False,
            supports_image=False,
            supports_video=False,
            supports_edit=False,
            supports_delete=False,
            supports_lookup=False,
            supports_timeline_reconciliation=False,
            supports_native_idempotency=False,
            max_media_count=0,
            supported_mime_types=["text/plain"],
            rate_limit_model="none_manual_export",
            auth_model="NONE (Official API does not support Community Post publishing)",
            official_endpoint="UNSUPPORTED_OFFICIAL_API",
            known_restrictions=[
                "YouTube Data API v3 has no Community Post publishing endpoint",
                "Browser automation / scraping is forbidden per project constraints",
                "Workflow must use MANUAL_EXPORT",
            ],
        )

    async def validate_credentials(self) -> CredentialValidationResult:
        return CredentialValidationResult(
            is_valid=False,
            status=CapabilityStatus.UNSUPPORTED_OFFICIAL_API,
            error_code="UNSUPPORTED_OFFICIAL_API",
            error_message="YouTube Community Post publishing is not supported by the official YouTube API. Use manual export.",
        )

    async def publish(self, text: str, intent: PublicationIntent) -> PublicationResult:
        return PublicationResult(
            success=False,
            error_code="UNSUPPORTED_OFFICIAL_API",
            error_message="YouTube Community Post publishing is not supported by official API. Must be published manually.",
            is_retryable=False,
        )

    async def lookup(self, provider_post_id: str) -> LookupResult:
        return LookupResult(
            found=False,
            error_code="UNSUPPORTED_OFFICIAL_API",
            error_message="Lookup not supported for YouTube Community.",
        )

    async def delete(self, provider_post_id: str) -> bool:
        return False

    def classify_error(self, status_code: int | None, error_data: Any) -> tuple[bool, str]:
        return False, "UNSUPPORTED_OFFICIAL_API"

    async def reconcile_ambiguous_delivery(
        self, intent: PublicationIntent, text: str
    ) -> AmbiguousReconciliationResult:
        return AmbiguousReconciliationResult(
            resolved=True,
            published=False,
            reason="YouTube Community is manual export only; no API delivery occurred",
        )


class ConnectorRegistry:
    """Registry providing connector instances by TargetPlatform."""

    _instances: dict[TargetPlatform, PublicationConnector] = {}

    @classmethod
    def get_connector(cls, target: TargetPlatform) -> PublicationConnector:
        if target not in cls._instances:
            if target == TargetPlatform.X:
                cls._instances[target] = XConnector()
            elif target == TargetPlatform.THREADS:
                cls._instances[target] = ThreadsConnector()
            elif target == TargetPlatform.YOUTUBE_COMMUNITY:
                cls._instances[target] = YouTubeCommunityConnector()
            else:
                raise ValueError(f"No connector registered for target platform {target}")
        return cls._instances[target]

    @classmethod
    def register_connector(
        cls, target: TargetPlatform, connector: PublicationConnector
    ) -> None:
        """Override connector for testing / mocking."""
        cls._instances[target] = connector

    @classmethod
    def reset(cls) -> None:
        cls._instances.clear()
