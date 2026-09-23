"""HMAC-signed bearer-token verification for Streamable HTTP MCP.

Bearer tokens are JWTs (RFC 7519) signed with HS256:

    <base64url(header)>.<base64url(payload)>.<base64url(HMAC-SHA256(secret, ...))>

- header: ``{"alg": "HS256", "typ": "JWT"}``
- payload: claim contents (``agent_id``, ``agent_instance_id``,
  ``issued_at``, ``session_label``, ``aud``, ``exp``)
- signature: HMAC-SHA256 of header + "." + payload, computed with the
  solet's HMAC secret. The secret is one vault entry; it never
  leaves the server. Forging a valid token requires possessing it.

Pre-Task-#53 design: claims were sealed to the solet's X25519
public key. That provided confidentiality but NOT origin
authentication — the public key is discoverable via the OAuth
resource-metadata endpoint, so anyone could mint a sealed claim with
any ``client_id`` and the server accepted it. HMAC-SHA256 closes the
forgery vulnerability. See
``workbench/2026-05-24_hmac_bearer_tokens_design.md``.

Algorithm pinning is mandatory: ``jwt.decode`` is always called with
``algorithms=["HS256"]``. This blocks the classic alg-confusion attack
(an attacker presents a token with ``alg: none`` or ``alg: RS256``
treating the public key as the HMAC secret).

Token verification is fast-fail: malformed token, invalid signature,
expired ``exp``, missing required claim fields, or ``issued_at``
outside the configured skew window all raise
:class:`BearerAuthError` with a stable ``.code`` the router maps to
``401 Unauthorized``. There are no fallback paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import jwt

from ..constants import TUNNEL_PASSTHROUGH_SENTINEL

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


# Maximum permitted clock skew between token issuance and verification.
# Five minutes matches the original sealed-claim design; tokens older
# or further in the future than this are rejected even if the
# signature is valid. Bounds the window for a leaked token's replay.
DEFAULT_MAX_TOKEN_AGE_SECONDS: Final[int] = 300

# Stable token namespace for the assignment-prescribed bearer-token
# subject. The phone client and the OAuth flow both claim this
# agent_id today; additional client types can claim distinct agent_ids
# in a future revision.
DEFAULT_AGENT_ID: Final[str] = "claude_phone"

# JWT signing algorithm. Pinned everywhere — never trust the token's
# header. Changing this constant alone is NOT enough to migrate
# algorithms; see ``_verify_hmac_token`` for the validation surface.
HMAC_SIGNING_ALGORITHM: Final[str] = "HS256"

# Minimum HMAC secret length. RFC 7518 §3.2 recommends >= the hash
# output (32 bytes for SHA-256). The vault entry generator uses
# exactly this length via ``secrets.token_bytes``.
HMAC_KEY_BYTE_LENGTH: Final[int] = 32


@dataclass(frozen=True, slots=True)
class BearerClaim:
    """Validated bearer-token claim.

    Field semantics match the pre-Task-#53 sealed-claim shape so
    downstream consumers (peer_registry binding, audience-bound
    routing, OAuth scope checks) need no changes. ``audience``
    carries the RFC 8707 ``aud`` claim; empty string means the token
    was minted before audience binding landed and the verifier's
    ``accepted_audiences`` setting decides whether to accept it.

    ``client_id`` (M5 §14.3) is the OAuth client_id that minted this
    token. Always populated for OAuth-issued tokens; first-party /
    in-process callers DO NOT construct a BearerClaim (they bypass
    the streamable HTTP transport entirely). Used at session
    establishment to derive the session's per-process export policy
    (see ``BridgeSessionManager._resolve_session_policy``).
    """

    agent_id: str
    agent_instance_id: str
    issued_at: datetime
    agent_session_id: str = ""
    session_label: str = ""
    audience: str = ""
    client_id: str = ""
    # B1 Q2: True iff the JWT carried an ``exp`` claim. ``exp`` is enforced
    # independently by ``jwt.decode``, so an exp-bearing token relies on it
    # for validity and is EXEMPT from the ``issued_at`` skew window. NOTE: no
    # exp-LESS mint path exists today — ``_issue_access_token`` stamps ``exp``
    # on every token — so the 300s skew is currently unreachable defensive
    # headroom for any future exp-less claim class, NOT a phone-vs-OAuth
    # distinction. Without this exemption a valid OAuth bearer would be
    # rejected 5 min after issuance despite a 24h exp.
    has_exp: bool = False


class BearerAuthError(Exception):
    """Bearer-token verification failed.

    The ``code`` attribute carries a stable token (``bearer.*``) that
    callers map to HTTP responses; ``message`` is safe to surface in
    ``WWW-Authenticate`` / response bodies (no plaintext, no key
    material, no fingerprint).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


class BearerVerifier:
    """Verify HMAC-signed bearer tokens against the local HMAC secret.

    The HMAC secret is loaded from the vault at solet startup and
    passed to this verifier as raw bytes. Algorithm pinning, skew
    enforcement, and audience binding are applied per token. The
    verifier holds no vault reference — all validation is local once
    constructed.

    Audience binding: per MCP 2025-06-18 §authorization an MCP server
    MUST validate that an access token was issued for it.
    ``accepted_audiences`` carries the canonical MCP URIs this
    transport answers to (primary URL plus any alias mounts); a
    token's ``aud`` claim must match one of them. An empty tuple
    disables the check — used by laptop dev mode where no canonical
    resource URL is configured yet.
    """

    def __init__(
        self,
        hmac_key: bytes,
        *,
        max_age_seconds: int = DEFAULT_MAX_TOKEN_AGE_SECONDS,
        accepted_audiences: tuple[str, ...] = (),
        clock: Callable[[], datetime] | None = None,
        client_exists_check: Callable[[str], bool] | None = None,
    ) -> None:
        if len(hmac_key) < HMAC_KEY_BYTE_LENGTH:
            raise ValueError(
                f"hmac_key must be at least {HMAC_KEY_BYTE_LENGTH} bytes "
                f"(got {len(hmac_key)})",
            )
        self._hmac_key = hmac_key
        self._max_age = timedelta(seconds=max_age_seconds)
        # Empty strings would short-circuit the membership check (every
        # missing ``aud`` claim would look like a match), so filter them
        # out at construction time.
        self._accepted_audiences = tuple(a for a in accepted_audiences if a)
        # Injectable clock keeps the time check unit-testable without
        # mocking ``datetime.now``; production callers omit it.
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        # M5 §14.3 cross-check: an OAuth-issued bearer must reference a
        # client_id that EXISTS in the vault registry. Revoked clients'
        # tokens are rejected even if otherwise valid. None = test mode
        # (skip cross-check); production wires a vault-backed callback.
        self._client_exists_check = client_exists_check

    def verify(self, authorization_header: str | None) -> BearerClaim:
        """Decode + verify a bearer token; return the typed claim.

        Raises :class:`BearerAuthError` on any failure — never returns
        a partial / unvalidated claim.
        """
        token = _extract_bearer_token(authorization_header)
        payload = _verify_hmac_token(token, self._hmac_key)
        claim = _payload_to_bearer_claim(payload)
        # B1 Q2: an exp-bearing token is bounded by ``exp`` — already enforced
        # by ``jwt.decode`` in _verify_hmac_token — so it is EXEMPT from the
        # ``issued_at`` skew window. The 300s skew applies only to an exp-LESS
        # claim, of which no mint path exists today (every token carries exp);
        # it is defensive headroom, not a phone-vs-OAuth split. (Adopt (a),
        # Dawn ruling 2026-07-05.)
        if not claim.has_exp:
            self._check_skew(claim.issued_at)
        self._check_audience(claim.audience)
        self._check_client_exists(claim.client_id)
        return claim

    def _check_client_exists(self, client_id: str) -> None:
        """M5 §14.3: reject tokens whose client_id is not in the OAuth registry.

        A revoked client's tokens stop verifying within the same skew
        window. When ``client_exists_check`` is None (test mode), skip
        this check; production constructors wire a vault-backed callback.
        """
        if self._client_exists_check is None:
            return
        if not self._client_exists_check(client_id):
            raise BearerAuthError(
                "bearer.unknown_client",
                (
                    f"bearer token's client_id {client_id!r} is not in "
                    "the OAuth client registry (revoked or never minted)"
                ),
            )

    def _check_skew(self, issued_at: datetime) -> None:
        """Reject tokens whose ``issued_at`` falls outside the skew window."""
        now = self._clock()
        delta = now - issued_at
        if abs(delta) > self._max_age:
            raise BearerAuthError(
                "bearer.expired",
                (
                    f"bearer token issued_at is outside the "
                    f"{int(self._max_age.total_seconds())}s skew window "
                    f"(now={now.isoformat()}, issued_at={issued_at.isoformat()})"
                ),
            )

    def _check_audience(self, audience: str) -> None:
        """Reject tokens whose ``aud`` is not in the accepted set."""
        if not self._accepted_audiences:
            return
        if audience not in self._accepted_audiences:
            raise BearerAuthError(
                "bearer.audience_mismatch",
                (
                    f"bearer token audience {audience!r} does not match "
                    f"any accepted MCP endpoint for this transport "
                    f"({list(self._accepted_audiences)!r})"
                ),
            )


class PermissiveBearerVerifier(BearerVerifier):
    """Bypass verifier — returns a synthetic claim regardless of header.

    Used ONLY when ``_BridgeRuntimeConfig.streamable_no_auth=True``
    (operator-opt-in for setups where an outer security boundary like
    an OpenAI tunnel + runtime API key, mTLS, or network isolation is
    the auth gate). Removes per-request bearer enforcement on the
    streamable MCP surface; the synthetic claim's ``agent_id`` carries
    the ``tunnel_passthrough`` sentinel so downstream session policy +
    audit logs can distinguish bypass-path callers.

    Do NOT use without an outer trust boundary.
    """

    def __init__(self) -> None:
        # Skip parent ``__init__`` — it requires a real HMAC key the
        # bypass path doesn't have. ``verify()`` ignores all instance
        # state, so there's nothing else to initialize.
        pass

    def verify(self, authorization_header: str | None) -> BearerClaim:
        """Always-accept: emit a synthetic claim, never raise."""
        del authorization_header  # bypass: header intentionally ignored
        return BearerClaim(
            agent_id=TUNNEL_PASSTHROUGH_SENTINEL,
            agent_instance_id=TUNNEL_PASSTHROUGH_SENTINEL,
            issued_at=datetime.now(UTC),
            session_label="streamable_no_auth",
        )


# ----------------------------------------------------------------------
# Helpers — kept module-local; never reach into BearerVerifier state.
# ----------------------------------------------------------------------


def _extract_bearer_token(authorization_header: str | None) -> str:
    """Pull the JWT string out of an ``Authorization: Bearer`` header."""
    if not authorization_header:
        raise BearerAuthError(
            "bearer.missing",
            "Authorization header is required",
        )
    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise BearerAuthError(
            "bearer.malformed",
            "Authorization header must be 'Bearer <token>'",
        )
    token = parts[1].strip()
    if not token:
        raise BearerAuthError("bearer.empty", "bearer token is empty")
    return token


def _verify_hmac_token(token: str, hmac_key: bytes) -> dict[str, Any]:
    """Verify the JWT signature + ``exp``; return the payload dict.

    Algorithm pinning to ``HS256`` is mandatory — pyjwt rejects any
    token whose header advertises a different ``alg`` (including
    ``none``). The internal signature comparison uses
    :func:`hmac.compare_digest` for constant-time semantics.
    """
    try:
        decoded: dict[str, Any] = jwt.decode(
            token,
            hmac_key,
            algorithms=[HMAC_SIGNING_ALGORITHM],
            # Audience validation is performed by BearerVerifier._check_audience
            # against the configured accepted set; skip pyjwt's built-in aud
            # check which would otherwise require passing audience= here.
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError as exc:
        raise BearerAuthError("bearer.expired", str(exc)) from exc
    except jwt.InvalidTokenError as exc:
        raise BearerAuthError("bearer.invalid_signature", str(exc)) from exc
    return decoded


def _require_nonempty_str(value: object, field_name: str) -> str:
    """Reject anything that isn't a non-empty string; return the value."""
    if not isinstance(value, str) or not value:
        raise BearerAuthError(
            "bearer.invalid_claim",
            f"claim.{field_name} must be a non-empty string",
        )
    return value


def _require_str(value: object, field_name: str) -> str:
    """Reject anything that isn't a string (empty allowed); return the value."""
    if not isinstance(value, str):
        raise BearerAuthError(
            "bearer.invalid_claim",
            f"claim.{field_name} must be a string when present",
        )
    return value


def _payload_to_bearer_claim(payload: dict[str, Any]) -> BearerClaim:
    """Validate + project the JWT payload onto :class:`BearerClaim`.

    M5 §14.3: every accepted claim carries a non-empty ``client_id``.
    First-party callers bypass this layer (they call platform processes
    in-process and never construct a BearerClaim), so any claim that
    reaches here is OAuth-issued and MUST carry its issuing client_id.
    """
    agent_id = _require_nonempty_str(
        payload.get("agent_id") or DEFAULT_AGENT_ID, "agent_id",
    )
    agent_instance_id = _require_nonempty_str(
        payload.get("agent_instance_id"), "agent_instance_id",
    )
    agent_session_id = _require_str(
        payload.get("agent_session_id") or "", "agent_session_id",
    )
    issued_at_raw = _require_nonempty_str(
        payload.get("issued_at"), "issued_at",
    )
    session_label = _require_str(payload.get("session_label") or "", "session_label")
    audience = _require_str(payload.get("aud") or "", "aud")
    client_id = _require_nonempty_str(payload.get("client_id"), "client_id")
    try:
        issued_at = _parse_iso_utc(issued_at_raw)
    except ValueError as exc:
        raise BearerAuthError(
            "bearer.invalid_claim",
            f"claim.issued_at is not a valid ISO-8601 datetime: {exc}",
        ) from exc
    return BearerClaim(
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        issued_at=issued_at,
        session_label=session_label,
        audience=audience,
        client_id=client_id,
        has_exp="exp" in payload,
    )


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; coerce to aware UTC."""
    normalised = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        # No tz means the client lied about issuing a UTC token; reject.
        raise ValueError("issued_at must be timezone-aware UTC")
    return parsed.astimezone(UTC)


__all__ = [
    "DEFAULT_AGENT_ID",
    "DEFAULT_MAX_TOKEN_AGE_SECONDS",
    "HMAC_KEY_BYTE_LENGTH",
    "HMAC_SIGNING_ALGORITHM",
    "BearerAuthError",
    "BearerClaim",
    "BearerVerifier",
]
