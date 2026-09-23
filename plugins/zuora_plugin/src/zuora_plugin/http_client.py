"""Zuora REST client — OAuth 2.0 client-credentials.

DEPRECATED-PENDING-REMEDIATION (2026-08-09): this client's calls run
inline on the action-dispatch path. The prior framing here — that sync
"matches the house verb shape" and that the 2026-06-20 async sketch was
correctly superseded — is retracted by operator ruling; see the 2026-08-05
action-pipeline sync-verb remediation ruling.
The umbrella design's "Prior art" note (workbench/2026-07-09_enterprise_connectors_design.md)
is superseded on this point only — its other content (auth, error
taxonomy, SQL-access lockdown) is unaffected. Zuora migration to the
deferred-completion shape is Phase 1 (bizops) scope, not yet scheduled.
See workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md.

The bearer token is cached in-memory and re-fetched ~30s before its recorded
expiry (clock-skew margin) or on a fresh 401 (force re-fetch once). It is
NEVER vaulted — only the client_secret (the durable credential) is a secret;
the token is trivially re-mintable.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .app_config import ZuoraTenantConfig
from .constants import TOKEN_REFRESH_MARGIN_SECONDS

# Plain int literals rather than ``httpx.codes.*`` — see errors.py's comment
# on the same pyright false-positive with httpx's status-code stub typing.
_HTTP_OK: int = 200
_HTTP_UNAUTHORIZED: int = 401


class ZuoraAuthError(RuntimeError):
    """Raised when the OAuth client-credentials token fetch itself fails."""


class ZuoraClient:
    """A synchronous Zuora REST client with cached, re-mintable bearer auth."""

    def __init__(
        self,
        config: ZuoraTenantConfig,
        *,
        timeout_seconds: float,
    ) -> None:
        self._config = config
        self._http = httpx.Client(base_url=config.base_url, timeout=timeout_seconds)
        self._token: str | None = None
        self._expires_at_monotonic: float = 0.0

    def close(self) -> None:
        self._http.close()

    def ensure_authenticated(self) -> None:
        """Mint (or reuse the cached) bearer token — used by test_connection to prove reachability."""
        self._bearer()

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("POST", path, json=json)

    def put(self, path: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("PUT", path, json=json)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = self._do_request(method, path, params=params, json=json)
        if response.status_code == _HTTP_UNAUTHORIZED:
            # Force exactly one re-fetch, then retry once — mirrors the
            # re-mint-on-expiry pattern used by salesforce_plugin's session.
            self._token = None
            response = self._do_request(method, path, params=params, json=json)
        return response

    def _do_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        return self._http.request(method, path, params=params, json=json, headers=headers)

    def _bearer(self) -> str:
        if self._token is not None and time.monotonic() < self._expires_at_monotonic:
            return self._token
        response = self._http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            },
        )
        if response.status_code != _HTTP_OK:
            raise ZuoraAuthError("Zuora OAuth token request failed")
        payload = response.json()
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token:
            raise ZuoraAuthError("Zuora OAuth token response carried no access_token")
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        self._token = token
        self._expires_at_monotonic = time.monotonic() + max(0.0, ttl - TOKEN_REFRESH_MARGIN_SECONDS)
        return token
