"""Record types + minting constants shared by both vault plugins.

The constants here match the values already in
``macos_vault_plugin.constants`` exactly so :func:`mint_client_credentials`
produces identifiers that are interchangeable across the two plugins
(critical for the abbey0011 migration in Task #26: clients minted by
the old plugin must round-trip cleanly through the new plugin).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import secrets
from typing import TypedDict

# OAuth scrypt parameters — verification at /oauth/token must complete
# in ~50ms on a modern x86 core. N=2**14 is the platform standard;
# matches macos_vault_plugin.constants exactly.
OAUTH_SCRYPT_N = 1 << 14
OAUTH_SCRYPT_R = 8
OAUTH_SCRYPT_P = 1
OAUTH_SCRYPT_DKLEN = 32
OAUTH_SCRYPT_SALT_BYTES = 16

# Client identifier format. "client-" + 32 hex chars = 39 chars.
OAUTH_CLIENT_ID_PREFIX = "client-"
OAUTH_CLIENT_ID_HEX_BYTES = 16

# Client secret format. 32 random bytes urlsafe-b64 encoded = 43 chars.
OAUTH_CLIENT_SECRET_BYTES = 32

# X25519 keypair byte width (libsodium / nacl).
X25519_KEY_LENGTH_BYTES = 32

# OAuth grant types both vault plugins will accept when minting a
# new client via oauth_client_register. The MCP transport enforces
# the per-client subset stored in each client's `grant_types` field
# at /oauth/token; this allowlist bounds what may go into that
# stored field. Discovery metadata is a subset (client_credentials
# is unadvertised but still usable by operator-created clients
# whose stored grant_types explicitly includes it).
OAUTH_ALLOWED_GRANT_TYPES = frozenset(
    {"authorization_code", "client_credentials", "refresh_token"},
)

# Upper bound on the stored User-Agent. The header is client-controlled
# and unbounded on the wire; a registry row is operator-facing evidence,
# not a log sink, so it is truncated rather than stored whole.
MAX_LAST_USE_USER_AGENT_LEN: int = 256


class OauthClientRecord(TypedDict, total=False):
    """One OAuth 2.1 client registration record.

    Required fields (always present):
        client_id, client_name, secret_hash, secret_salt, scopes,
        redirect_uris, created_at, operator_approved (Task #31)

    Optional fields (defaulted during migration; see Task #26 §4.4.7):
        grant_types — list of allowed grants; defaults to
          ``["authorization_code", "refresh_token"]``
        last_used_at — ISO timestamp of most recent token issuance,
          written by ``VaultOAuthRegistry.record_token_use``
        last_use_ip / last_use_user_agent / last_use_transport —
          evidence observed at the most recent successful token
          issuance. These corroborate (or contradict) the
          self-descriptive ``client_name``, which is operator-typed
          at registration and is not evidence of the product behind
          the credential.

    ``operator_approved`` (Task #31) is ``True`` exactly when the row
    was minted via the operator-only platform process
    ``oauth_client_register``. The MCP OAuth transport refuses to
    issue tokens for clients whose ``operator_approved`` is not
    exactly ``True``. Projections treat a missing field as ``False``.
    """

    client_id: str
    client_name: str
    secret_hash: str  # base64
    secret_salt: str  # base64
    scopes: list[str]
    redirect_uris: list[str]
    created_at: str  # ISO 8601 UTC
    grant_types: list[str]
    last_used_at: str  # ISO 8601 UTC
    last_use_ip: str
    last_use_user_agent: str
    last_use_transport: str
    operator_approved: bool


class SecretRecord(TypedDict, total=False):
    """One vault secret entry as stored in the SM bundle.

    The cloud bundle stores secrets as plaintext (encrypted at rest
    by KMS-via-SM; double-encryption with a master key would buy
    nothing). The local default vault stores ciphertext + nonce +
    auth_tag — those fields are local-only and never round-trip
    through this record shape.
    """

    value: str
    tags: list[str]
    metadata: dict[str, str]
    version: int
    created_at: str
    updated_at: str


def utc_now_iso() -> str:
    """Return current UTC time as a stable ISO-8601 string.

    Used for ``created_at`` / ``updated_at`` / ``last_used_at`` and as
    the canonical timestamp format throughout vault records.
    """
    return _dt.datetime.now(_dt.UTC).isoformat()


def mint_client_credentials() -> tuple[str, str, bytes, bytes]:
    """Mint a fresh ``(client_id, client_secret, secret_salt, secret_hash)``.

    Returns the cleartext ``client_secret`` (for one-time response to
    the registration caller — never stored) along with the salt and
    scrypt hash that the plugin persists in the bundle. The hash is
    raw bytes; callers are responsible for base64 encoding before
    serialization.
    """
    client_id = OAUTH_CLIENT_ID_PREFIX + secrets.token_hex(OAUTH_CLIENT_ID_HEX_BYTES)
    client_secret = secrets.token_urlsafe(OAUTH_CLIENT_SECRET_BYTES)
    secret_salt = secrets.token_bytes(OAUTH_SCRYPT_SALT_BYTES)
    secret_hash = hashlib.scrypt(
        client_secret.encode("utf-8"),
        salt=secret_salt,
        n=OAUTH_SCRYPT_N,
        r=OAUTH_SCRYPT_R,
        p=OAUTH_SCRYPT_P,
        dklen=OAUTH_SCRYPT_DKLEN,
    )
    return client_id, client_secret, secret_salt, secret_hash
