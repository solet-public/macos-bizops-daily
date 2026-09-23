"""Storage-backend-agnostic OAuth client + refresh-token registry.

Extracted in Task #31 (Boy Scout) from the two ~3000-line vault
plugin classes (``MacosVaultPlugin``, ``SecretsManagerVaultPlugin``)
so the OAuth domain logic lives in ONE place and the plugins
collapse to thin platform-process glue.

Composition shape:

    class _MyVaultPlugin:
        def prepare_for_readiness(self) -> None:
            ...
            self._oauth_registry = VaultOAuthRegistry(
                client_storage=_MyVaultPluginClientStorage(...),
                refresh_store=self._oauth_refresh_tokens_store,
                b64_encode=self._b64encode_bytes,
                b64_decode=self._b64decode_str,
                logger=self.logger,
            )

        @platform_process(...)
        def oauth_client_register_action(self, params, state):
            outcome = self._oauth_registry.register_client(params)
            return self._success(outcome) if ok else self._error(...)

The registry knows nothing about ``ActionResult`` / @platform_process
/ Store internals / Bundle async — it returns plain dicts or raises
:class:`OauthGrantValidationError` / :class:`OauthClientNotFoundError`
and lets the plugin translate.

Backends:
* :class:`OAuthClientStorage` is a Protocol. Concrete impls live in
  the consuming plugins (one per storage backend).
* :class:`RefreshTokenStorage` is a Protocol. Both current plugins
  use a state-service ``Store`` backend with identical semantics, so
  in practice they share a single tiny adapter.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from logging import Logger
from typing import Any, Protocol

from .oauth_clients import (
    normalize_oauth_register_params,
    project_oauth_client_metadata,
)
from .records import (
    MAX_LAST_USE_USER_AGENT_LEN,
    mint_client_credentials,
    utc_now_iso,
)


class OauthClientNotFoundError(LookupError):
    """Raised when an operator-side lookup hits a missing client_id."""


class OAuthClientStorage(Protocol):
    """Backend-agnostic storage for the OAuth client table.

    Implementations adapt either a state-service ``Store`` (local
    default vault) or a JSON dict inside a Secrets Manager bundle
    (cloud SM vault). Every method is synchronous; the cloud
    implementation runs its async bundle round-trip internally.
    """

    def get_client(
        self, client_id: str,
    ) -> Mapping[str, Any] | None:
        """Return the raw stored record or None if absent."""
        ...

    def insert_client(self, record: dict[str, Any]) -> None:
        """Persist a freshly-minted client record (assumes unique id)."""
        ...

    def delete_client(self, client_id: str) -> int:
        """Hard-delete the row. Returns 1 if removed, 0 if absent."""
        ...

    def list_clients(self) -> list[Mapping[str, Any]]:
        """Return every stored client record (no filtering)."""
        ...

    def update_client_redirect_uris(
        self, client_id: str, redirect_uris: list[str],
    ) -> bool:
        """Overwrite the redirect_uris column. Returns True iff a row matched."""
        ...

    def record_client_token_use(
        self, client_id: str, fields: Mapping[str, str],
    ) -> bool:
        """Write the last-use evidence columns. True iff a row matched.

        ``fields`` carries ``last_used_at`` plus the corroboration
        columns; it is passed as a mapping rather than as keyword
        arguments so a backend can persist it with one write.
        """
        ...


class RefreshTokenStorage(Protocol):
    """Backend-agnostic storage for the refresh-token table.

    Both current plugins use the same shape (a state-service Store
    keyed on ``token_hash``), so the in-tree adapter is essentially a
    one-line wrapper around the underlying ``Store``.
    """

    def insert_token(self, row: dict[str, Any]) -> None:
        ...

    def consume_token(self, token_hash: str) -> Mapping[str, Any] | None:
        """Single-use: read the row, delete it, return the read result."""
        ...


B64Encoder = Callable[[bytes], str]
B64Decoder = Callable[[str], bytes]


class VaultOAuthRegistry:
    """OAuth client registry + refresh-token store, backend-agnostic.

    Owns:
      * register / lookup / verify / add_redirect_uri / list / revoke
        of OAuth client records
      * issue / consume single-use rotation of refresh tokens

    Each plugin composes the registry once at startup with adapter
    objects that bridge to its own storage backend. Plugin actions
    delegate to this surface and translate the outcomes into
    ``ActionResult`` envelopes.
    """

    def __init__(
        self,
        *,
        client_storage: OAuthClientStorage,
        refresh_store: RefreshTokenStorage,
        b64_encode: B64Encoder,
        b64_decode: B64Decoder,
        logger: Logger,
    ) -> None:
        self._clients = client_storage
        self._refresh = refresh_store
        self._b64_encode = b64_encode
        self._b64_decode = b64_decode
        self._logger = logger

    # ─── Client registry ───────────────────────────────────────────────────

    def register_client(
        self, params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Mint a fresh ``(client_id, client_secret)`` and persist.

        Raises :class:`OauthGrantValidationError` on bad input.
        Returns the projected success payload: ``client_id``,
        cleartext ``client_secret`` (one-time), ``client_name``,
        ``scopes``, ``redirect_uris``, ``grant_types``,
        ``operator_approved=True``.
        """
        client_name, scopes, redirect_uris, grant_types = (
            normalize_oauth_register_params(params)
        )
        client_id, client_secret, secret_hash, salt = _mint_credentials_pair()
        record = {
            "client_id": client_id,
            "client_name": client_name,
            "secret_hash": self._b64_encode(secret_hash),
            "secret_salt": self._b64_encode(salt),
            "scopes": scopes,
            "redirect_uris": redirect_uris,
            "operator_approved": True,
            "grant_types": grant_types,
        }
        self._clients.insert_client(record)
        self._logger.info(
            "OAuth client registered: client_id=%s name=%r scopes=%s "
            "redirect_uris=%s grant_types=%s operator_approved=True",
            client_id, client_name, scopes, redirect_uris, grant_types,
        )
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": client_name,
            "scopes": scopes,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "operator_approved": True,
        }

    def lookup_client(
        self, client_id: str,
    ) -> dict[str, Any] | None:
        """Return the public projection of ``client_id`` or None."""
        row = self._clients.get_client(client_id)
        if row is None:
            return None
        return project_oauth_client_metadata(client_id, row)

    def verify_client_credentials(
        self, client_id: str, client_secret: str,
    ) -> dict[str, Any] | None:
        """Constant-time scrypt verify; return public projection or None."""
        row = self._clients.get_client(client_id)
        if row is None:
            return None
        salt_b64 = row.get("secret_salt")
        hash_b64 = row.get("secret_hash")
        if not isinstance(salt_b64, str) or not isinstance(hash_b64, str):
            return None
        try:
            salt = self._b64_decode(salt_b64)
            expected = self._b64_decode(hash_b64)
        except ValueError:
            return None
        candidate = _scrypt_secret_hash(client_secret, salt)
        if not _constant_time_eq(candidate, expected):
            return None
        return project_oauth_client_metadata(client_id, row)

    def add_redirect_uri(
        self, client_id: str, redirect_uri: str,
    ) -> dict[str, Any]:
        """Idempotently append a redirect_uri. Raises if client unknown."""
        row = self._clients.get_client(client_id)
        if row is None:
            raise OauthClientNotFoundError(client_id)
        existing_raw = row.get("redirect_uris") or []
        existing = (
            [str(u) for u in existing_raw]
            if isinstance(existing_raw, list)
            else []
        )
        if redirect_uri in existing:
            return {
                "client_id": client_id,
                "redirect_uris": existing,
                "added": False,
            }
        updated = [*existing, redirect_uri]
        self._clients.update_client_redirect_uris(client_id, updated)
        self._logger.info(
            "OAuth client redirect_uri added: client_id=%s uri=%r",
            client_id, redirect_uri,
        )
        return {
            "client_id": client_id,
            "redirect_uris": updated,
            "added": True,
        }

    def record_token_use(
        self,
        client_id: str,
        *,
        used_at: str | None = None,
        client_ip: str = "",
        user_agent: str = "",
        transport: str = "",
    ) -> bool:
        """Record evidence observed at a successful token issuance.

        Called on EVERY successful ``/oauth/token`` response — both
        first issuance and silent refresh — so ``last_used_at`` is a
        truthful answer to "when did this credential last mint a
        token". It previously had no answer at all: the field was
        documented and projected but never written, and on the local
        Postgres backend the column did not exist.

        The three corroboration fields are deliberately separate from
        ``client_name``. ``client_name`` is operator-typed at
        registration and asserts what a client claims to be;
        ``last_use_ip`` / ``last_use_user_agent`` / ``last_use_transport``
        are observed at use and are what it actually presented as. An
        operator comparing the two can see a divergence; with only
        ``client_name`` there is nothing to compare against.

        Returns True iff a row matched. A miss is logged and swallowed
        rather than raised: the token has already been minted by the
        time this runs, and failing the caller's request after a
        successful issuance would deny a legitimately-authenticated
        client over a bookkeeping write.
        """
        stamp = used_at or utc_now_iso()
        fields = {
            "last_used_at": stamp,
            "last_use_ip": client_ip,
            "last_use_user_agent": user_agent[:MAX_LAST_USE_USER_AGENT_LEN],
            "last_use_transport": transport,
        }
        matched = self._clients.record_client_token_use(client_id, fields)
        if not matched:
            self._logger.warning(
                "OAuth token-use not recorded: no client row matched "
                "client_id=%s (token was still issued)",
                client_id,
            )
        return matched

    def list_clients(self) -> list[dict[str, Any]]:
        """Return all clients (public projection) sorted by created_at.

        Includes ``created_at`` + ``last_used_at`` operator-facing
        timestamps when present in the row. Local-default rows that
        omit ``last_used_at`` get an empty string so the projection
        shape stays stable across both storage backends (the SM
        plugin's action return schema promises this field).
        """
        clients: list[dict[str, Any]] = []
        for row in self._clients.list_clients():
            cid = str(row.get("client_id") or "")
            if not cid:
                continue
            projected = project_oauth_client_metadata(cid, row)
            projected["created_at"] = str(row.get("created_at") or "")
            last_used = row.get("last_used_at")
            projected["last_used_at"] = (
                str(last_used) if last_used is not None else ""
            )
            for evidence_field in (
                "last_use_ip",
                "last_use_user_agent",
                "last_use_transport",
            ):
                raw_evidence = row.get(evidence_field)
                projected[evidence_field] = (
                    str(raw_evidence) if raw_evidence is not None else ""
                )
            clients.append(projected)
        clients.sort(key=lambda c: str(c.get("created_at") or ""))
        return clients

    def revoke_client(self, client_id: str) -> int:
        """Hard-delete by id. Returns 1 if removed, 0 if absent."""
        removed = self._clients.delete_client(client_id)
        if removed:
            self._logger.info(
                "OAuth client revoked: client_id=%s", client_id,
            )
        return removed

    # ─── M5 — machine-only client minting + operator-equivalent lookup ──────

    def mint_internal_machine_client(
        self,
        *,
        client_label: str,
        scopes: tuple[str, ...],
        deliver_secret_to_caller: bool = True,
    ) -> dict[str, Any]:
        """Mint and persist a server-internal machine-only OAuth client.

        Spec §13.4 (M5). Not callable through any MCP surface — reachable
        only from :class:`SessionLedgerService`'s pairing poll handler.
        Returns the cleartext secret to the trusted internal caller
        exactly once, intended for direct-TLS delivery to a deployed
        agent (the shipper).

        Sets:
            ``operator_approved=False`` (operator did not manually register).
            ``operator_equivalent=False`` (shipper clients are NOT operator-equivalent).
            ``machine_grant_enabled=True`` (grant-eligibility signal for
            ``client_credentials`` per ``_require_grant_eligible``).

        Raises ``ValueError`` on empty ``client_label`` / empty ``scopes``
        / ``deliver_secret_to_caller=False`` (v1 only supports the
        deliver-once contract).
        """
        if not client_label:
            raise ValueError("mint_internal_machine_client requires non-empty client_label")
        if not scopes:
            raise ValueError("mint_internal_machine_client requires non-empty scopes")
        if not deliver_secret_to_caller:
            raise ValueError(
                "mint_internal_machine_client requires deliver_secret_to_caller=True for v1",
            )
        client_id, client_secret, secret_hash, salt = _mint_credentials_pair()
        record = {
            "client_id": client_id,
            "client_name": client_label,
            "secret_hash": self._b64_encode(secret_hash),
            "secret_salt": self._b64_encode(salt),
            "scopes": list(scopes),
            "redirect_uris": [],
            "operator_approved": False,
            "operator_equivalent": False,
            "machine_grant_enabled": True,
            "grant_types": ["client_credentials"],
        }
        self._clients.insert_client(record)
        self._logger.info(
            "OAuth machine client minted: client_id=%s label=%r scopes=%s "
            "machine_grant_enabled=True",
            client_id, client_label, list(scopes),
        )
        return {
            "client_id": client_id,
            "client_secret": client_secret,
        }

    def is_operator_equivalent(self, client_id: str) -> bool:
        """True iff the OAuth client carries ``operator_equivalent=True``.

        Spec §14.4 (M5). Used by ``BridgeSessionManager._resolve_session_policy``
        to decide whether to grant ``_UNRESTRICTED`` policy. Strict identity
        check (``is True``) — never inferred from side channels.
        """
        meta = self.lookup_client(client_id)
        if meta is None:
            return False
        return meta.get("operator_equivalent") is True

    # ─── Refresh-token rotation ───────────────────────────────────────────

    def issue_refresh_token(
        self, *,
        client_id: str,
        scopes: list[str],
        audience: str,
        ttl_seconds: int,
    ) -> str:
        """Mint a fresh cleartext refresh token; persist sha256(cleartext)."""
        cleartext = secrets.token_urlsafe(32)
        token_hash = _hash_refresh_token(cleartext)
        expires_at = _iso_utc_at(seconds_from_now=ttl_seconds)
        self._refresh.insert_token(
            {
                "token_hash": token_hash,
                "client_id": client_id,
                "scopes": scopes,
                "audience": audience,
                "expires_at": expires_at,
            },
        )
        return cleartext

    def consume_refresh_token(
        self, cleartext: str,
    ) -> dict[str, Any] | None:
        """Single-use consume; returns claims or None on miss/expiry/bad-row."""
        token_hash = _hash_refresh_token(cleartext)
        row = self._refresh.consume_token(token_hash)
        if row is None:
            return None
        expires_at_raw = row.get("expires_at")
        if not isinstance(expires_at_raw, str):
            return None
        if _expired(expires_at_raw):
            return None
        scopes_raw = row.get("scopes") or []
        return {
            "client_id": str(row.get("client_id") or ""),
            "scopes": (
                [str(s) for s in scopes_raw]
                if isinstance(scopes_raw, list)
                else []
            ),
            "audience": str(row.get("audience") or ""),
        }


# ─── Module-level helpers (pure functions) ─────────────────────────────────


# Scrypt parameters; intentionally a duplicate of the per-plugin constants
# rather than an import, because the registry module deliberately has no
# plugin-package dependencies. Values match plugins.macos_vault_plugin.
# constants exactly so client records minted by either plugin remain
# verifiable here.
_SCRYPT_N: int = 1 << 14
_SCRYPT_R: int = 8
_SCRYPT_P: int = 1
_SCRYPT_DKLEN: int = 32


def _mint_credentials_pair() -> tuple[str, str, bytes, bytes]:
    """Return ``(client_id, cleartext_secret, secret_hash, salt)``."""
    client_id, client_secret, salt, secret_hash = mint_client_credentials()
    return client_id, client_secret, secret_hash, salt


def _scrypt_secret_hash(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


def _constant_time_eq(left: bytes, right: bytes) -> bool:
    import hmac as _hmac
    return _hmac.compare_digest(left, right)


def _hash_refresh_token(cleartext: str) -> str:
    """sha256(cleartext) base64-encoded; used as the storage key."""
    digest = hashlib.sha256(cleartext.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _iso_utc_at(*, seconds_from_now: int) -> str:
    return (
        (datetime.now(UTC).replace(microsecond=0)
         + timedelta(seconds=seconds_from_now))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _expired(expires_at_iso: str) -> bool:
    try:
        expires_at = datetime.fromisoformat(
            expires_at_iso.replace("Z", "+00:00"),
        )
    except ValueError:
        return True
    return expires_at < datetime.now(UTC)


__all__ = [
    "B64Decoder",
    "B64Encoder",
    "OAuthClientStorage",
    "OauthClientNotFoundError",
    "RefreshTokenStorage",
    "VaultOAuthRegistry",
]
