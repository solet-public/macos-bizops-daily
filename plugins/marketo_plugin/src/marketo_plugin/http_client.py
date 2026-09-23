"""Marketo REST client — OAuth 2.0 client-credentials.

DEPRECATED-PENDING-REMEDIATION (2026-08-09): this client's calls run
inline on the action-dispatch path, which the operator ruled a prohibited
antipattern — see the 2026-08-05 action-pipeline sync-verb remediation ruling.
"Synchronous ... to match the house verb shape" (the prior framing here)
is retracted: there is no legitimate class of inline-I/O verb, bounded or
not. Marketo migration to the deferred-completion shape is Phase 1
(bizops) scope, not yet scheduled. See
workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md
for the shape any new or migrated verb must use instead.

Unlike zuora_plugin's client, this one returns **decoded JSON envelopes**,
not raw ``httpx.Response`` objects — Marketo's fault model lives in the body
(``success: false`` + ``errors[]``) at HTTP 200, not in the status code (see
``errors.py`` module docstring). ``get_json``/``post_json``/``delete_json``
each: perform the request, decode the JSON body, and either return the
envelope dict (``success`` truthy — including partial-batch results where
individual records failed but the call itself succeeded) or raise
:class:`errors.MarketoEnvelopeError` (``success`` falsy) for the caller's
classifier. A transport-level fault (5xx, connection error, non-JSON body)
raises :class:`errors.MarketoTransportError` instead.

The bearer token is cached in-memory and re-fetched ~30s before its recorded
expiry (clock-skew margin), or once on a decoded envelope carrying error code
601/602 (invalid/expired token) — exactly one re-mint-and-retry, mirroring
zuora_plugin's 401-triggers-one-refetch pattern but keyed off the envelope
instead of the HTTP status. It is NEVER vaulted — only the client_secret (the
durable credential) is a secret; the token is trivially re-mintable.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any

import httpx

from .app_config import MarketoInstanceConfig
from .constants import (
    DEFAULT_TOKEN_TTL_SECONDS,
    IDENTITY_TOKEN_PATH,
    MARKETO_MAX_CONCURRENT_CALLS,
    MARKETO_RATE_WINDOW_MAX_CALLS,
    MARKETO_RATE_WINDOW_SECONDS,
    MIME_JSON,
    TOKEN_REFRESH_MARGIN_SECONDS,
)
from .errors import MarketoEnvelopeError, MarketoTransportError, is_retryable_auth_code

_HTTP_OK: int = 200

# Write methods that must still declare a content type when they carry no body.
_BODYLESS_CONTENT_TYPE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class _RateLimiter:
    """Proactive client-side throttle for Marketo's 606 (100 calls/20s) and 615
    (10 concurrent in-flight) error codes.

    D0.3 section 7 named constraint: this plugin's prior full seriality
    (every verb ran its vendor calls inline on the dispatch await) accidentally
    kept concurrency at 1, so 615 in particular was structurally unreachable —
    a real limiter was never built. The D0.3 migration adds a genuine
    concurrent background worker, which removes that accidental protection;
    this replaces it with a real one rather than inheriting nothing.
    """

    def __init__(self, *, max_concurrent: int, window_seconds: float, window_max_calls: int) -> None:
        self._semaphore = threading.Semaphore(max_concurrent)
        self._window_seconds = window_seconds
        self._window_max_calls = window_max_calls
        self._window_lock = threading.Lock()
        self._call_times: collections.deque[float] = collections.deque()

    def acquire(self) -> None:
        self._semaphore.acquire()
        self._wait_for_window_slot()

    def release(self) -> None:
        self._semaphore.release()

    def _wait_for_window_slot(self) -> None:
        while True:
            with self._window_lock:
                now = time.monotonic()
                while self._call_times and now - self._call_times[0] >= self._window_seconds:
                    self._call_times.popleft()
                if len(self._call_times) < self._window_max_calls:
                    self._call_times.append(now)
                    return
                sleep_for = self._window_seconds - (now - self._call_times[0])
            time.sleep(max(sleep_for, 0.01))


class MarketoAuthError(RuntimeError):
    """Raised when the OAuth client-credentials token fetch itself fails."""


class MarketoClient:
    """A synchronous Marketo REST client with cached, re-mintable bearer auth."""

    def __init__(
        self,
        config: MarketoInstanceConfig,
        *,
        timeout_seconds: float,
    ) -> None:
        self._config = config
        self._http = httpx.Client(base_url=config.base_url, timeout=timeout_seconds)
        self._token: str | None = None
        self._expires_at_monotonic: float = 0.0
        # D0.3 section 7 named constraint: the bearer-token cache below was a
        # plain unlocked check-then-set, safe only because the plugin's prior
        # full seriality never called it from two threads at once. The D0.3
        # migration's background worker removes that accidental protection.
        self._token_lock = threading.Lock()
        self._rate_limiter = _RateLimiter(
            max_concurrent=MARKETO_MAX_CONCURRENT_CALLS,
            window_seconds=MARKETO_RATE_WINDOW_SECONDS,
            window_max_calls=MARKETO_RATE_WINDOW_MAX_CALLS,
        )

    def close(self) -> None:
        self._http.close()

    def ensure_authenticated(self) -> None:
        """Mint (or reuse the cached) bearer token — used by test_connection to prove reachability."""
        self._bearer()

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", path, params=params, json=None)

    def post_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json("POST", path, params=params, json=json)

    def delete_json(self, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        # httpx.Client.delete() does not accept a json= body; Marketo's list
        # membership removal is DELETE-with-a-JSON-body, so this goes through
        # the low-level .request() form instead.
        return self._request_json("DELETE", path, params=None, json=json)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = self._do_request(method, path, params=params, json=json)
        payload = self._decode(response)
        if payload.get("success"):
            return payload
        errors = payload.get("errors")
        first_code = str(errors[0].get("code", "")) if isinstance(errors, list) and errors and isinstance(errors[0], dict) else ""
        if is_retryable_auth_code(first_code):
            # Exactly one re-mint-and-retry, then classify whatever comes back.
            self._invalidate_token()
            response = self._do_request(method, path, params=params, json=json)
            payload = self._decode(response)
            if payload.get("success"):
                return payload
        raise MarketoEnvelopeError(payload)

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise MarketoTransportError(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketoTransportError(response.status_code) from exc
        if not isinstance(payload, dict):
            raise MarketoTransportError(response.status_code)
        return payload

    def _do_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        if json is None and method in _BODYLESS_CONTENT_TYPE_METHODS:
            # Marketo rejects a body-less POST carrying no content type with
            # error 612 "Invalid Content Type", even when every argument is a
            # query parameter — which is exactly the shape of
            # /leads/{id}/merge.json. httpx sets Content-Type only when it
            # serialises a json= body, and nothing else here sets a default, so
            # without this merge_leads fails 100% of the time (field-verified
            # against a live instance).
            # Set at the CLASS level rather than in the one verb so any future
            # body-less write inherits the fix instead of rediscovering the bug.
            headers["Content-Type"] = MIME_JSON
        self._rate_limiter.acquire()
        try:
            return self._http.request(method, path, params=params, json=json, headers=headers)
        finally:
            self._rate_limiter.release()

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._token = None

    def _bearer(self) -> str:
        with self._token_lock:
            if self._token is not None and time.monotonic() < self._expires_at_monotonic:
                return self._token
            response = self._http.get(
                IDENTITY_TOKEN_PATH,
                params={
                    "grant_type": "client_credentials",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                },
            )
            if response.status_code != _HTTP_OK:
                raise MarketoAuthError("Marketo OAuth token request failed")
            try:
                payload = response.json()
            except ValueError as exc:
                raise MarketoAuthError("Marketo OAuth token response was not JSON") from exc
            token = payload.get("access_token") if isinstance(payload, dict) else None
            expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise MarketoAuthError("Marketo OAuth token response carried no access_token")
            ttl = float(expires_in) if isinstance(expires_in, (int, float)) else DEFAULT_TOKEN_TTL_SECONDS
            self._token = token
            self._expires_at_monotonic = time.monotonic() + max(0.0, ttl - TOKEN_REFRESH_MARGIN_SECONDS)
            return token
