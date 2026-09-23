"""OAuth 2.0 helpers built on the official Google client libraries.

Deliberately NOT hand-rolled. The authorization-code exchange runs through
``google_auth_oauthlib.flow.Flow`` (PKCE S256 + the confidential client_secret),
and access-token refresh runs through ``google.oauth2.credentials.Credentials``
+ ``google.auth.transport.requests.Request``. Letting the library own the token
transport is the whole point of the "use the secure Google libraries" mandate —
schwab hand-rolled its token POST only because Schwab has no official SDK.

All state, vault writes, and token persistence live in token_store; this module
is pure protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from ..constants import (
    DEFAULT_ACCESS_TTL_SECONDS,
    GOOGLE_AUTH_URI,
    GOOGLE_TOKEN_URI,
    OAUTH_SCOPES,
)
from .app_config import OAuthAppConfig


@dataclass(frozen=True)
class GoogleTokens:
    """Token fields consumed from a Google credential.

    ``refresh_token`` is optional: Google returns it on the initial consent
    (with ``access_type=offline`` + ``prompt=consent``) but usually OMITS it on
    a plain refresh — Google refresh tokens are durable and are not rotated per
    call the way Schwab's are.
    """

    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclass(frozen=True)
class AuthorizationRequest:
    """The browser-facing consent URL plus the PKCE material to persist."""

    authorize_url: str
    state: str
    code_verifier: str


def _client_config(app: OAuthAppConfig) -> dict[str, dict[str, str]]:
    return {
        "web": {
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
        }
    }


def build_authorization_request(app: OAuthAppConfig) -> AuthorizationRequest:
    """Build the consent URL (PKCE S256) and return the verifier to persist.

    ``access_type=offline`` + ``prompt=consent`` guarantee Google issues a
    refresh token. The returned ``code_verifier`` must be stored keyed by
    ``state`` and handed back to :func:`exchange_code` at callback time.
    """
    flow = Flow.from_client_config(
        _client_config(app),
        scopes=list(OAUTH_SCOPES),
        autogenerate_code_verifier=True,
    )
    flow.redirect_uri = app.redirect_uri
    authorize_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="false",
    )
    return AuthorizationRequest(
        authorize_url=authorize_url,
        state=state,
        code_verifier=str(flow.code_verifier),
    )


def exchange_code(
    app: OAuthAppConfig,
    *,
    code: str,
    state: str,
    code_verifier: str,
) -> GoogleTokens:
    """Exchange a one-time authorization code for an access+refresh pair."""
    flow = Flow.from_client_config(
        _client_config(app),
        scopes=list(OAUTH_SCOPES),
        state=state,
    )
    flow.redirect_uri = app.redirect_uri
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    return _tokens_from_credentials(flow.credentials)


def refresh_access_token(app: OAuthAppConfig, *, refresh_token: str) -> GoogleTokens:
    """Obtain a fresh access token using the durable refresh token."""
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=app.client_id,
        client_secret=app.client_secret,
        scopes=list(OAUTH_SCOPES),
    )
    credentials.refresh(Request())
    return _tokens_from_credentials(credentials)


def _tokens_from_credentials(credentials: Any) -> GoogleTokens:
    # `credentials` is a google-auth credential — `Flow.credentials` returns a
    # library union (oauth2 / external-account), so it is typed Any here in line
    # with the repo convention for untyped third-party objects. The str guard
    # below is the real runtime contract.
    access_token = credentials.token
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Google credentials missing access token after exchange")
    return GoogleTokens(
        access_token=access_token,
        refresh_token=credentials.refresh_token,
        expires_in=_seconds_until(credentials.expiry),
    )


def _seconds_until(expiry: datetime | None) -> int:
    """Seconds until a google-auth expiry (a NAIVE UTC datetime) elapses.

    google-auth stores ``expiry`` tz-naive in UTC. Compare against a naive-UTC
    ``now`` to avoid the aware/naive subtraction TypeError; the token store
    re-derives an aware-UTC absolute expiry from this delta.
    """
    if expiry is None:
        return DEFAULT_ACCESS_TTL_SECONDS
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    return max(0, int((expiry - now_naive).total_seconds()))
