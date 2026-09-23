"""FastAPI route for the Google OAuth 2.0 callback.

Provides a single route: GET /oauth/google/callback

The callback validates the ``state`` nonce, restores the PKCE ``code_verifier``
persisted at authorize time, exchanges the authorization code via the Google
library, and writes both tokens to vault through the token store. On failure it
returns an HTTP error with a description.

The router is built by create_oauth_router() which captures the token store,
app-config loader, and pending-states dict by reference — no global state.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from ..constants import ERROR_OAUTH_STATE_INVALID, OAUTH_CALLBACK_PATH
from .app_config import AppConfigError, AppConfigLoader
from .oauth_client import exchange_code
from .token_store import TokenStore, TokenStoreError

_logger = logging.getLogger(__name__)


def create_oauth_router(
    token_store: TokenStore,
    app_config_loader: AppConfigLoader,
    pending_states: dict[str, str],
) -> Any:
    """Return a FastAPI APIRouter wired to the token store and PKCE-state map.

    Args:
        token_store: Vault-backed token store to write tokens into.
        app_config_loader: Resolves OAuth app config from the address book.
        pending_states: Mutable dict mapping ``state_nonce -> code_verifier``.
            Populated by connect_account; consumed and cleared here.
    """
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get(OAUTH_CALLBACK_PATH)
    async def oauth_callback(  # pyright: ignore[reportUnusedFunction]
        code: str = "",
        state: str = "",
        error: str = "",
    ) -> HTMLResponse:
        """Receive the Google authorization_code redirect and exchange for tokens."""
        if error:
            # The callback can be externally reachable when a cloud deployment
            # explicitly binds 0.0.0.0 behind an ALB, so provider/query text must
            # never be reflected into the HTML body or logs verbatim. Log only the
            # short OAuth error CODE (a defined token like 'access_denied'), not the
            # free-text error_description; render a generic body.
            _logger.error("Google OAuth returned an authorization error: %s", error)
            return HTMLResponse(
                content=_html_result(
                    "OAuth Error",
                    "Google returned an error during authorization. Check the "
                    "solet logs and re-run connect_account.",
                    success=False,
                ),
                status_code=400,
            )

        code_verifier = pending_states.pop(state, None) if state else None
        if not state or code_verifier is None:
            # state is max-sensitivity metadata (and usually attacker-supplied or
            # already spent here) — log only a short redacted prefix, never the value.
            _logger.warning(
                "OAuth callback: invalid or expired state nonce (%s)", _redact(state)
            )
            raise HTTPException(
                status_code=400,
                detail=f"{ERROR_OAUTH_STATE_INVALID}: state nonce is invalid or expired",
            )

        if not code:
            raise HTTPException(
                status_code=400, detail="OAuth callback received no authorization code"
            )

        try:
            app_config = app_config_loader.load()
        except AppConfigError as exc:
            _logger.error("OAuth callback: failed to load app config: %s", exc)
            raise HTTPException(
                status_code=500, detail="OAuth app configuration is unavailable."
            ) from exc

        try:
            tokens = exchange_code(
                app_config, code=code, state=state, code_verifier=code_verifier
            )
        except Exception as exc:
            _logger.error("OAuth callback: token exchange failed: %s", exc)
            raise HTTPException(status_code=502, detail="Google token exchange failed.") from exc

        refresh_token = tokens.refresh_token
        if not refresh_token:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Google did not return a refresh token. Re-run connect_account; "
                    "the OAuth app must request offline access with prompt=consent."
                ),
            )

        try:
            token_store.store_initial_tokens(
                refresh_token=refresh_token,
                access_token=tokens.access_token,
                expires_in=tokens.expires_in,
            )
        except TokenStoreError as exc:
            _logger.error("OAuth callback: failed to store tokens: %s", exc)
            raise HTTPException(status_code=500, detail="Token storage failed.") from exc

        _logger.info("Google OAuth bootstrap complete — tokens stored in vault")
        return HTMLResponse(
            content=_html_result(
                "Google Workspace Authentication Complete",
                "Tokens stored. You can close this window and return to the solet.",
                success=True,
            )
        )

    return router


def _redact(value: str) -> str:
    """Short, non-reversible-enough label for a sensitive nonce in a log line."""
    return f"prefix={value[:6]}…" if value else "empty"


def _html_result(title: str, body: str, *, success: bool) -> str:
    color = "#2d7a2d" if success else "#b00020"
    # Escape all rendered text (defense-in-depth): the callback origin is
    # externally reachable, so nothing dynamic may become markup.
    safe_title = html.escape(title)
    safe_body = html.escape(body)
    return (
        "<!DOCTYPE html><html><head>"
        f"<title>{safe_title}</title>"
        "<style>body{font-family:sans-serif;max-width:600px;margin:60px auto;padding:20px}"
        f"h1{{color:{color}}}</style></head><body>"
        f"<h1>{safe_title}</h1>"
        f"<p>{safe_body}</p>"
        "</body></html>"
    )
