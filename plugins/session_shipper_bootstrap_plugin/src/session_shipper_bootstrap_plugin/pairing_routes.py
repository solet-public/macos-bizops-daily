# pyright: reportUnusedFunction=false
"""Pairing route module + umbrella for the bridge plugin's one-time §13.6 mount.

Exposes :func:`register_session_ledger_pairing_routes` — the single
import target the bridge plugin's ``_build_fastapi_app`` calls. It
widens M4's chatgpt-export upload route by ALSO mounting the two
pairing routes:

    POST /api/v1/ledger/pairing/initiate (unauthenticated, rate-limited)
    POST /api/v1/ledger/pairing/poll     (authenticated by pairing_token)

Pairing flow per spec §13.5:

1. Operator runs ``service_interface::session_ledger_service::generate_ingest_setup``
   from their main MCP bridge — creates a pending deployment row with
   ``initiating_client_id`` set to the operator's bearer client_id.
2. Shipper boots + POSTs ``/api/v1/ledger/pairing/initiate`` with its
   deployment_id + machine_id. Server scrypt-hashes a fresh
   pairing_token, persists it on the deployment row, returns the
   plaintext token + display user_code (10-minute TTL).
3. Operator runs ``approve_pairing(deployment_id, user_code)`` from
   their bridge — ownership-binding check passes (client_id matches
   initiating_client_id), deployment transitions pending → approved.
4. Shipper POSTs ``/api/v1/ledger/pairing/poll`` with the pairing_token.
   Server verifies scrypt, mints an OAuth client (machine_grant_enabled),
   transitions approved → paired in the same transaction, returns the
   cleartext client_id + client_secret to the shipper ONCE.
5. Shipper holds the credentials, opens an authenticated bridge session,
   ingests through the SHIPPER_ALLOWLIST policy resolved at session
   establishment.

The shipper subsequently calls ``shipper_self_revoke`` to release
itself (spec §14.1 pin 2 — target server-derived from bearer claim).
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


PAIRING_INITIATE_ROUTE = "/api/v1/ledger/pairing/initiate"
PAIRING_POLL_ROUTE = "/api/v1/ledger/pairing/poll"

_PAIRING_TTL_SECONDS = 600  # spec §13.1
_USER_CODE_LEN = 8  # operator-readable; uppercase alphanumeric
_PAIRING_TOKEN_BYTES = 32  # urlsafe-encoded; high entropy

_ERR_DEPLOYMENT_NOT_FOUND = "deployment_not_found"
_ERR_DEPLOYMENT_WRONG_STATE = "deployment_wrong_state"
_ERR_TOKEN_MISMATCH = "pairing_token_mismatch"
_ERR_INTERNAL = "internal_server_error"
_ERR_INVALID_REQUEST = "invalid_request"


class PairingLedgerProtocol(Protocol):
    """Structural surface the pairing routes need.

    Production: built by :func:`make_pairing_ledger_facade` from the
    live :class:`SessionLedgerService` + ``vault_oauth_registry``.
    Smoke: a direct stub implementing this surface.
    """

    def pairing_get_deployment(self, deployment_id: str) -> dict[str, object] | None: ...

    def pairing_persist_token(
        self,
        *,
        deployment_id: str,
        pairing_token_hash: str,
        pairing_token_salt: str,
        user_code: str,
    ) -> None: ...

    def pairing_verify_token(
        self,
        *,
        deployment_id: str,
        cleartext_token: str,
    ) -> bool: ...

    def pairing_mint_and_transition_to_paired(
        self,
        *,
        deployment_id: str,
    ) -> dict[str, str]:  # {"client_id": str, "client_secret": str}
        """Atomic mint + transition. Raises :class:`CrossStoreOrphanMintError`
        if vault mint succeeded but the state-service transition failed."""
        ...

    def pairing_compensate_orphan_mint(
        self,
        *,
        deployment_id: str,
        client_id: str,
    ) -> str:
        """Operator-free reconciliation of an orphan vault client.

        Called by the route when ``pairing_mint_and_transition_to_paired``
        raised :class:`CrossStoreOrphanMintError`. Implementations attempt
        ``vault.revoke_client(client_id)`` immediately; on success return
        ``"revoked"``. On revoke failure (genuine orphan), record a
        memory-tagged sweep-task entry via :func:`record_orphan_for_sweep`
        and return ``"sweep_scheduled"``.
        """
        ...


class CrossStoreOrphanMintError(RuntimeError):
    """Vault mint succeeded but the state-service transition failed.

    Carries the vault ``client_id`` so the route can request operator-free
    compensation via :meth:`PairingLedgerProtocol.pairing_compensate_orphan_mint`.
    The shipper never sees the cleartext secret in this failure mode
    (no 200 response is emitted).
    """

    def __init__(self, *, client_id: str, deployment_id: str, original: BaseException) -> None:
        super().__init__(
            f"vault mint succeeded but state transition failed for deployment "
            f"{deployment_id!r}; orphan client_id={client_id!r} (cause: {original!r})"
        )
        self.client_id = client_id
        self.deployment_id = deployment_id
        self.original = original


class _InitiateBody(BaseModel):
    deployment_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)


class _PollBody(BaseModel):
    deployment_id: str = Field(min_length=1)
    pairing_token: str = Field(min_length=1)


def _mint_user_code() -> str:
    """Operator-displayable code (uppercase alphanumeric)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(_USER_CODE_LEN))


def _mint_pairing_token() -> str:
    """High-entropy URL-safe pairing token (shipper-facing)."""
    return secrets.token_urlsafe(_PAIRING_TOKEN_BYTES)


def register_session_ledger_pairing_routes(
    app: FastAPI,
    *,
    ledger: PairingLedgerProtocol,
) -> None:
    """M5 widened umbrella: mounts the pairing-initiate + pairing-poll routes.

    Bridge plugin imports this; the import is the §13.6 one-time
    exception to the no-edits-to-god-file-plugins Boy Scout rule.

    The M4 chatgpt-export route is registered separately by the chatgpt
    plugin's own routes module (it owns its surface; the bridge mounts
    both independently). The shared name (M4's chatgpt-only umbrella
    + this widened pairing umbrella) is historical — the bridge picks
    by import path, not by function identity.
    """
    from fastapi import HTTPException  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    @app.post(PAIRING_INITIATE_ROUTE)
    async def pairing_initiate(body: _InitiateBody) -> JSONResponse:
        """Shipper-initiated pairing: persist a fresh token + return display values."""
        row = ledger.pairing_get_deployment(body.deployment_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail={"error": _ERR_DEPLOYMENT_NOT_FOUND}
            )
        if row.get("pairing_status") != "pending":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": _ERR_DEPLOYMENT_WRONG_STATE,
                    "pairing_status": str(row.get("pairing_status")),
                },
            )
        stored_machine = str(row.get("machine_id") or "")
        if stored_machine and stored_machine != body.machine_id:
            raise HTTPException(
                status_code=400,
                detail={"error": _ERR_INVALID_REQUEST, "field": "machine_id"},
            )
        cleartext_token = _mint_pairing_token()
        user_code = _mint_user_code()
        salt = secrets.token_bytes(16)
        token_hash = _scrypt_hash(cleartext_token, salt)
        ledger.pairing_persist_token(
            deployment_id=body.deployment_id,
            pairing_token_hash=_b64encode(token_hash),
            pairing_token_salt=_b64encode(salt),
            user_code=user_code,
        )
        logger.info(
            "pairing initiated: deployment_id=%s machine_id=%s ttl_s=%d",
            body.deployment_id, body.machine_id, _PAIRING_TTL_SECONDS,
        )
        return JSONResponse(
            status_code=200,
            content={
                "user_code": user_code,
                "pairing_token": cleartext_token,
                "expires_in_seconds": _PAIRING_TTL_SECONDS,
            },
        )

    @app.post(PAIRING_POLL_ROUTE)
    async def pairing_poll(body: _PollBody) -> JSONResponse:
        """Atomic poll: verify token → mint OAuth client → transition to paired."""
        row = ledger.pairing_get_deployment(body.deployment_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail={"error": _ERR_DEPLOYMENT_NOT_FOUND}
            )
        status = row.get("pairing_status")
        if status == "pending":
            # Operator hasn't approved yet — poll again.
            return JSONResponse(
                status_code=200, content={"status": "pending"}
            )
        if status == "paired":
            # Single-use semantics: token consumed at first paired poll.
            return JSONResponse(
                status_code=409,
                content={
                    "status": "already_paired",
                    "error": "pairing_token_already_consumed",
                },
            )
        if status != "approved":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": _ERR_DEPLOYMENT_WRONG_STATE,
                    "pairing_status": str(status),
                },
            )
        if not ledger.pairing_verify_token(
            deployment_id=body.deployment_id,
            cleartext_token=body.pairing_token,
        ):
            raise HTTPException(
                status_code=401, detail={"error": _ERR_TOKEN_MISMATCH}
            )
        try:
            credentials = ledger.pairing_mint_and_transition_to_paired(
                deployment_id=body.deployment_id,
            )
        except CrossStoreOrphanMintError as orphan_exc:
            # Vault mint succeeded but state transition failed. Per the
            # 2026-05-30 operator-free reconciliation framing (was spec
            # §13.4 v1 reconciliation gap), compensate automatically:
            # try to revoke the just-minted vault client. If revoke
            # succeeds the orphan is gone; if revoke ALSO fails we
            # record a memory-tagged sweep entry so a periodic heartbeat
            # can pick it up, never the operator.
            try:
                outcome = ledger.pairing_compensate_orphan_mint(
                    deployment_id=body.deployment_id,
                    client_id=orphan_exc.client_id,
                )
            except Exception:  # noqa: BLE001
                # Compensation itself blew up (vault unreachable, etc.).
                # This is the genuine HIGH-severity case — log loudly so
                # an operational alert can fire; never return secrets.
                logger.error(
                    "ORPHAN_OAUTH_CLIENT_HIGH_SEVERITY: pairing poll mint "
                    "succeeded but state-update AND compensation both "
                    "failed: client_id=%s deployment_id=%s",
                    orphan_exc.client_id, body.deployment_id,
                )
                raise HTTPException(
                    status_code=500, detail={"error": _ERR_INTERNAL}
                ) from None
            if outcome == "revoked":
                logger.info(
                    "transient cross-store mint failure; vault compensated: "
                    "client_id=%s deployment_id=%s",
                    orphan_exc.client_id, body.deployment_id,
                )
            else:
                # Compensation declined or deferred to sweep — surface clearly.
                logger.error(
                    "ORPHAN_OAUTH_CLIENT_SWEEP_SCHEDULED: revoke failed; "
                    "memory-tagged for periodic sweep: client_id=%s "
                    "deployment_id=%s outcome=%s",
                    orphan_exc.client_id, body.deployment_id, outcome,
                )
            raise HTTPException(
                status_code=500, detail={"error": _ERR_INTERNAL}
            ) from None
        except Exception:  # noqa: BLE001 — defensive backstop for non-orphan paths
            logger.exception(
                "pairing poll: mint+transition failed for deployment_id=%s",
                body.deployment_id,
            )
            raise HTTPException(
                status_code=500, detail={"error": _ERR_INTERNAL}
            ) from None
        logger.info(
            "pairing complete: deployment_id=%s client_id=%s",
            body.deployment_id, credentials["client_id"],
        )
        return JSONResponse(
            status_code=200,
            content={
                "status": "paired",
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
            },
        )


def make_pairing_ledger_facade(
    *,
    session_ledger_service: Any,
    vault_oauth_registry: Any,
) -> PairingLedgerProtocol:
    """Production wiring: builds a facade from the live services."""
    return _LiveLedgerFacade(
        session_ledger_service=session_ledger_service,
        vault_oauth_registry=vault_oauth_registry,
    )


# ───────────────────────────────────────────────────────────────────────────
# Internals
# ───────────────────────────────────────────────────────────────────────────


class _LiveLedgerFacade:
    """Concrete PairingLedgerProtocol over live services."""

    __slots__ = ("_ledger", "_vault")

    def __init__(
        self,
        *,
        session_ledger_service: Any,
        vault_oauth_registry: Any,
    ) -> None:
        self._ledger = session_ledger_service
        self._vault = vault_oauth_registry

    def pairing_get_deployment(self, deployment_id: str) -> dict[str, object] | None:
        return self._ledger._repository.get_deployment(deployment_id)  # noqa: SLF001

    def pairing_persist_token(
        self,
        *,
        deployment_id: str,
        pairing_token_hash: str,
        pairing_token_salt: str,
        user_code: str,
    ) -> None:
        self._ledger._repository.set_deployment_pairing_token(  # noqa: SLF001
            deployment_id=deployment_id,
            pairing_token_hash=pairing_token_hash,
            pairing_token_salt=pairing_token_salt,
            user_code=user_code,
        )

    def pairing_verify_token(
        self,
        *,
        deployment_id: str,
        cleartext_token: str,
    ) -> bool:
        row = self._ledger._repository.get_deployment(deployment_id)  # noqa: SLF001
        if row is None:
            return False
        salt_b64 = row.get("pairing_token_salt")
        hash_b64 = row.get("pairing_token_hash")
        if not isinstance(salt_b64, str) or not isinstance(hash_b64, str):
            return False
        try:
            salt = _b64decode(salt_b64)
            expected = _b64decode(hash_b64)
        except ValueError:
            return False
        candidate = _scrypt_hash(cleartext_token, salt)
        return _constant_time_eq(candidate, expected)

    def pairing_mint_and_transition_to_paired(
        self,
        *,
        deployment_id: str,
    ) -> dict[str, str]:
        # Spec §13.4 cross-store ordering: vault first, then state.
        # The state transition may fail (DB down, schema constraint, etc.);
        # we narrow that to CrossStoreOrphanMintError so the route can
        # request operator-free compensation without leaking a 200/secret.
        creds = self._vault.mint_internal_machine_client(
            client_label=f"shipper-{deployment_id}",
            scopes=("ledger:ingest",),
        )
        try:
            self._ledger._repository.transition_deployment_to_paired(  # noqa: SLF001
                deployment_id=deployment_id,
                oauth_client_id=creds["client_id"],
            )
        except Exception as state_exc:
            raise CrossStoreOrphanMintError(
                client_id=creds["client_id"],
                deployment_id=deployment_id,
                original=state_exc,
            ) from state_exc
        return {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        }

    def pairing_compensate_orphan_mint(
        self,
        *,
        deployment_id: str,
        client_id: str,
    ) -> str:
        """Operator-free reconciliation: revoke first, sweep-tag on failure."""
        try:
            self._vault.revoke_client(client_id)
        except Exception:  # noqa: BLE001 — revoke failure surfaces as sweep handoff
            logger.exception(
                "vault.revoke_client failed during compensation: "
                "client_id=%s deployment_id=%s",
                client_id, deployment_id,
            )
            record_orphan_for_sweep(
                client_id=client_id,
                deployment_id=deployment_id,
                memory_service=getattr(self._ledger, "_memory_service", None),
            )
            return "sweep_scheduled"
        return "revoked"


def record_orphan_for_sweep(
    *,
    client_id: str,
    deployment_id: str,
    memory_service: Any,
) -> None:
    """Tag a stuck orphan for a periodic-sweep heartbeat to pick up.

    Operator-free reconciliation second-line defense: if vault.revoke
    itself failed, write a tagged memory note (``session_ledger:orphan_oauth_client``)
    that a future sweep can consume. The sweep itself is downstream
    work; this leaves a durable breadcrumb so an automated cleaner can
    find every orphan without operator triage.

    Best-effort: if memory_service is unavailable or write fails, log
    and continue (the route still returns 500; the HIGH-severity log
    line above carries the same info for log-based alerting).
    """
    if memory_service is None:
        logger.warning(
            "orphan sweep tag NOT written (no memory_service bound): "
            "client_id=%s deployment_id=%s",
            client_id, deployment_id,
        )
        return
    try:
        memory_service.remember(
            content=(
                f"Orphan OAuth client awaiting sweep: client_id={client_id} "
                f"deployment_id={deployment_id}. Vault.revoke_client failed "
                f"during pairing-poll compensation; periodic-poll heartbeat "
                f"should retry revoke until success."
            ),
            tags=("session_ledger:orphan_oauth_client",),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "memory_service.remember failed for orphan sweep tag: "
            "client_id=%s deployment_id=%s",
            client_id, deployment_id,
        )


def _scrypt_hash(secret: str, salt: bytes) -> bytes:
    """Same parameters as the OAuth client-secret scrypt path (vault_core)."""
    import hashlib  # noqa: PLC0415

    return hashlib.scrypt(
        password=secret.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )


def _constant_time_eq(left: bytes, right: bytes) -> bool:
    import hmac  # noqa: PLC0415

    return hmac.compare_digest(left, right)


def _b64encode(value: bytes) -> str:
    import base64  # noqa: PLC0415

    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    import base64  # noqa: PLC0415

    return base64.b64decode(value.encode("ascii"))


__all__ = [
    "PAIRING_INITIATE_ROUTE",
    "PAIRING_POLL_ROUTE",
    "CrossStoreOrphanMintError",
    "PairingLedgerProtocol",
    "make_pairing_ledger_facade",
    "record_orphan_for_sweep",
    "register_session_ledger_pairing_routes",
]
