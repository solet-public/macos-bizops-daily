"""Vault-backed token store for Google OAuth credentials.

Single-account model: one refresh token and one access token per plugin
install. Vault keys are scoped (``<solet>.g_suite_plugin.*``).

Google token semantics differ from Schwab's in one important way: Google
refresh tokens are DURABLE and are not rotated on every refresh. A plain
``grant_type=refresh_token`` returns a new access token and usually NO new
refresh token. So the refresh path updates only the access token, and rotates
the refresh token ONLY on the rare occasion Google returns a changed one. There
is no 7-day hard cap; a refresh token stays valid until revoked, the app is
unpublished, or it goes 6 months unused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ..constants import (
    ACCESS_TOKEN_EARLY_REFRESH_SECONDS,
    ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT,
    ACCESS_TOKEN_VALUE_KEY_TOKEN,
    ERROR_AUTH_EXPIRED,
    ERROR_NOT_CONNECTED,
    ERROR_REFRESH_TOKEN_ROTATE_FAILED,
    ERROR_TOKEN_EXCHANGE_FAILED,
    ERROR_TOKEN_STORE_FAILED,
    ERROR_VAULT_NOT_AVAILABLE,
    VAULT_KEY_ACCESS_TOKEN,
    VAULT_KEY_REFRESH_TOKEN,
    VAULT_TAG_ACCESS_TOKEN,
    VAULT_TAG_REFRESH_TOKEN,
)
from .app_config import AppConfigError, AppConfigLoader
from .oauth_client import refresh_access_token


class TokenStoreError(RuntimeError):
    """Raised when token resolution cannot proceed; carries a typed error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TokenStore:
    """Vault-backed access/refresh token manager for the Google plugin.

    The vault service is duck-typed (mirrors VaultServiceInterface) so this
    plugin never imports from macos_vault_plugin.
    """

    def __init__(self, vault_service: Any, app_config_loader: AppConfigLoader) -> None:
        if vault_service is None:
            raise TokenStoreError(
                ERROR_VAULT_NOT_AVAILABLE,
                "vault_service is required for TokenStore",
            )
        self._vault = vault_service
        self._app_config_loader = app_config_loader

    # ------------------------------------------------------------------
    # Bootstrap write path — called by the OAuth callback handler
    # ------------------------------------------------------------------

    def store_initial_tokens(
        self,
        refresh_token: str,
        access_token: str,
        expires_in: int,
    ) -> None:
        """Write both tokens after a successful authorization_code exchange."""
        self._write_refresh_token(refresh_token)
        self._write_access_token(access_token, expires_in)

    # ------------------------------------------------------------------
    # Runtime read path — called by the service factory
    # ------------------------------------------------------------------

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a non-expired access token, refreshing via OAuth if needed."""
        if not force_refresh:
            cached = self._read_cached_access_token()
            if cached is not None:
                return cached
        return self._refresh_and_persist()

    def is_connected(self) -> bool:
        """True when a refresh token is present in vault."""
        return self._exists(VAULT_KEY_REFRESH_TOKEN)

    # ------------------------------------------------------------------
    # Internal — refresh path
    # ------------------------------------------------------------------

    def _refresh_and_persist(self) -> str:
        try:
            app_config = self._app_config_loader.load()
        except AppConfigError as exc:
            raise TokenStoreError(exc.code, str(exc)) from exc

        refresh_value = self._read_value(VAULT_KEY_REFRESH_TOKEN)
        if refresh_value is None:
            raise TokenStoreError(
                ERROR_NOT_CONNECTED,
                "No Google refresh token stored. Run connect_account first.",
            )
        try:
            tokens = refresh_access_token(app_config, refresh_token=refresh_value)
        except Exception as exc:
            raise _classify_refresh_error(exc) from exc

        # Google usually omits refresh_token on refresh; rotate only if it
        # actually returned a new, different one.
        if tokens.refresh_token and tokens.refresh_token != refresh_value:
            self._rotate_refresh_token(tokens.refresh_token)
        self._write_access_token(tokens.access_token, tokens.expires_in)
        return tokens.access_token

    # ------------------------------------------------------------------
    # Internal — vault operations
    # ------------------------------------------------------------------

    def _write_refresh_token(self, refresh_token: str) -> None:
        if self._exists(VAULT_KEY_REFRESH_TOKEN):
            result = self._vault.rotate(VAULT_KEY_REFRESH_TOKEN, refresh_token)
        else:
            result = self._vault.store(
                key=VAULT_KEY_REFRESH_TOKEN,
                value=refresh_token,
                tags=[VAULT_TAG_REFRESH_TOKEN],
                metadata={"issued_at": datetime.now(UTC).isoformat()},
            )
        _require_completed(result, "refresh-token vault write failed")

    def _rotate_refresh_token(self, new_refresh_token: str) -> None:
        result = self._vault.rotate(VAULT_KEY_REFRESH_TOKEN, new_refresh_token)
        if not _is_completed(result):
            raise TokenStoreError(
                ERROR_REFRESH_TOKEN_ROTATE_FAILED,
                "Vault refresh-token rotation failed",
            )

    def _write_access_token(self, access_token: str, expires_in: int) -> None:
        envelope = _build_access_envelope(access_token, expires_in)
        if self._exists(VAULT_KEY_ACCESS_TOKEN):
            result = self._vault.rotate(VAULT_KEY_ACCESS_TOKEN, envelope)
        else:
            result = self._vault.store(
                key=VAULT_KEY_ACCESS_TOKEN,
                value=envelope,
                tags=[VAULT_TAG_ACCESS_TOKEN],
                metadata={},
            )
        _require_completed(result, "access-token vault write failed")

    def _read_cached_access_token(self) -> str | None:
        envelope = self._read_value(VAULT_KEY_ACCESS_TOKEN)
        if envelope is None:
            return None
        token, expires_at = _parse_access_envelope(envelope)
        if not _is_token_fresh(expires_at):
            return None
        return token

    def _read_value(self, key: str) -> str | None:
        if not self._exists(key):
            return None
        result = self._vault.retrieve(key)
        if not _is_completed(result):
            return None
        data = result.get("data") or {}
        value = data.get("value")
        return value if isinstance(value, str) else None

    def _exists(self, key: str) -> bool:
        result = self._vault.exists(key)
        if not _is_completed(result):
            return False
        data = result.get("data") or {}
        return bool(data.get("exists", False))


def _classify_refresh_error(exc: Exception) -> TokenStoreError:
    error_text = str(exc).lower()
    if "invalid_grant" in error_text or "invalid_client" in error_text:
        return TokenStoreError(
            ERROR_AUTH_EXPIRED,
            "Google refresh token was revoked or expired. Run connect_account "
            "to re-authenticate.",
        )
    return TokenStoreError(ERROR_TOKEN_EXCHANGE_FAILED, f"Google token refresh failed: {exc}")


def _build_access_envelope(access_token: str, expires_in: int) -> str:
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    return json.dumps(
        {
            ACCESS_TOKEN_VALUE_KEY_TOKEN: access_token,
            ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT: expires_at.isoformat(),
        },
        separators=(",", ":"),
    )


def _parse_access_envelope(envelope: str) -> tuple[str, datetime]:
    payload: Any = json.loads(envelope)
    if not isinstance(payload, dict):
        raise TokenStoreError(
            ERROR_TOKEN_EXCHANGE_FAILED,
            "Stored access-token entry is not a JSON object",
        )
    token = payload.get(ACCESS_TOKEN_VALUE_KEY_TOKEN)
    expires_at_raw = payload.get(ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT)
    if not isinstance(token, str) or not isinstance(expires_at_raw, str):
        raise TokenStoreError(
            ERROR_TOKEN_EXCHANGE_FAILED,
            "Stored access-token entry is missing token or expires_at",
        )
    return token, datetime.fromisoformat(expires_at_raw)


def _is_completed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("action_status") == "completed"


def _require_completed(result: Any, message: str) -> None:
    """Raise a typed TokenStoreError when a vault store/rotate did not complete.

    Message is generic on purpose — it never includes token material, so a
    failure surfaced to the caller/logs cannot leak a secret.
    """
    if not _is_completed(result):
        raise TokenStoreError(ERROR_TOKEN_STORE_FAILED, message)


def _is_token_fresh(expires_at: datetime) -> bool:
    threshold = datetime.now(UTC) + timedelta(seconds=ACCESS_TOKEN_EARLY_REFRESH_SECONDS)
    return expires_at > threshold
