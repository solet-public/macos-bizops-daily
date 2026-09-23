"""Vault service database schema definition.

Defines relational tables for encrypted secrets storage,
using AES-256-GCM encryption with PBKDF2 key derivation.
"""

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

# SQL substrate namespace — INTENTIONALLY UNCHANGED across the
# default_vault_plugin → macos_vault_plugin rename (Tier 3
# W-VAULT-LOCAL-KEYCHAIN, 2026-06-07). Table identity stays at
# ``default_vault_plugin__secret`` / ``default_vault_plugin__audit`` /
# ``default_vault_plugin__oauth_client`` / ``default_vault_plugin__oauth_refresh_token``
# so existing rows remain readable across the rename. Tier 5
# W-VAULT-MIGRATE drops the SQL substrate entirely; namespace deletion
# happens there.
NAMESPACE = "default_vault_plugin"

# OAuth client registry identity.  Backs ``service_interface::vault_service::
# oauth_client_register / _revoke / _list`` plus the in-process verifier the
# Streamable HTTP MCP transport's ``/oauth/token`` endpoint uses to validate
# ``client_credentials`` grants.  Postgres-backed because client credentials
# must survive a restart.
OAUTH_CLIENT_TABLE = "oauth_client"
OAUTH_CLIENT_ID_PREFIX = "oac"

# OAuth refresh-token registry identity.  Single-use, rotated per
# OAuth 2.1 §4.3.1 — when /oauth/token accepts a refresh_token, the
# row is deleted (or marked used) and a fresh refresh_token is issued
# alongside the new access_token.  Postgres-backed for durability;
# the row count stays small because expired/used rows are pruned at
# consume time.
OAUTH_REFRESH_TOKEN_TABLE = "oauth_refresh_token"
OAUTH_REFRESH_TOKEN_ID_PREFIX = "ort"


def get_oauth_client_schema() -> TableSchema:
    """OAuth 2.1 client registry for the Streamable HTTP MCP transport.

    One row per registered ``(client_id, client_secret)`` pair issued by
    ``service_interface::vault_service::oauth_client_register``.  The
    ``client_secret`` itself is never stored — only an scrypt hash plus
    per-row salt.  Verification at ``/oauth/token`` recomputes the hash
    with the stored salt and compares constant-time against
    ``secret_hash``.

    Scopes are stored as a JSON array so the token endpoint can echo
    the requested intersection back in the ``scope`` response field.

    Backs a :class:`~ananta.services.store.Store` opened by
    ``MacosVaultPlugin`` in ``prepare_for_readiness``.

    Standard fields (``id``, ``created_at``, ``updated_at``, ...) are
    auto-injected by :class:`SchemaStandardizer`.
    """
    return TableSchema(
        table_name=OAUTH_CLIENT_TABLE,
        description=(
            "OAuth 2.1 client registry (operator-approved; multi-grant) "
            "for the Streamable HTTP MCP transport"
        ),
        id_prefix=OAUTH_CLIENT_ID_PREFIX,
        data_sensitivity=0.8,  # secret hashes are sensitive even though not plaintext
        columns={
            "client_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description="Public client identifier (no secret material).",
                data_sensitivity=0.3,
            ),
            "client_name": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Human label supplied at registration time.",
                data_sensitivity=0.3,
            ),
            "secret_hash": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Base64-encoded scrypt hash of the client_secret.",
                data_sensitivity=1.0,  # RESTRICTED — never expose to LLM
            ),
            "secret_salt": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Base64-encoded scrypt salt for the client_secret.",
                data_sensitivity=1.0,  # RESTRICTED — cryptographic material
            ),
            "scopes": ColumnDefinition(
                type=ColumnType.JSON,
                default="[]",
                description="JSON array of OAuth scopes granted to this client.",
                data_sensitivity=0.2,
            ),
            "redirect_uris": ColumnDefinition(
                type=ColumnType.JSON,
                default="[]",
                description=(
                    "JSON array of pre-registered redirect URIs.  The "
                    "/authorize endpoint exact-matches against this "
                    "list before redirecting; empty list means the "
                    "client may only use grant_type=client_credentials."
                ),
                data_sensitivity=0.2,
            ),
            "operator_approved": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                default="false",
                not_null=True,
                description=(
                    "True iff the row was created via the operator-only "
                    "platform process oauth_client_register. The MCP OAuth "
                    "transport refuses to issue tokens for clients whose "
                    "operator_approved is not exactly True (Task #31). "
                    "Missing field is treated as False at the projection "
                    "layer — never infer approval from any side channel."
                ),
                data_sensitivity=0.2,
            ),
            "operator_equivalent": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                default="false",
                not_null=True,
                description=(
                    "True iff this client receives operator-equivalent "
                    "rights at bridge-session establishment (session policy "
                    "= _UNRESTRICTED; all process_call/search/schema "
                    "permitted). Set only via operator-initiated "
                    "registration; NEVER via mint_internal_machine_client. "
                    "Per spec §13.4 / §14.4 (M5)."
                ),
                data_sensitivity=0.2,
            ),
            "machine_grant_enabled": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                default="false",
                not_null=True,
                description=(
                    "True iff this client was minted via "
                    "VaultOAuthRegistry.mint_internal_machine_client (e.g., "
                    "shipper-pairing flow per spec §13.4). Grant-eligibility "
                    "for client_credentials accepts either operator_approved "
                    "OR machine_grant_enabled (see _require_grant_eligible "
                    "in mcp_streamable/oauth.py). M5 addition."
                ),
                data_sensitivity=0.2,
            ),
            "grant_types": ColumnDefinition(
                type=ColumnType.JSON,
                default='["authorization_code", "refresh_token"]',
                description=(
                    "JSON array of OAuth grant types this client is "
                    "authorized to use. Allowlist: authorization_code, "
                    "client_credentials, refresh_token. Enforced per-grant "
                    "at /oauth/token; a client may use ONLY the grants "
                    "explicitly listed here regardless of what the "
                    "auth-server metadata advertises."
                ),
                data_sensitivity=0.2,
            ),
            "last_used_at": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "ISO-8601 UTC timestamp of the most recent access-token "
                    "issuance for this client, written by "
                    "VaultOAuthRegistry.record_token_use on every "
                    "/oauth/token success. Empty until the client's first "
                    "token. Before this column existed the field was "
                    "documented on OauthClientRecord and projected by "
                    "list_clients but had nowhere to persist, so it read "
                    "as '' forever and no operator could date a client's "
                    "last use."
                ),
                data_sensitivity=0.2,
            ),
            "last_use_ip": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Peer IP observed on the most recent successful "
                    "/oauth/token request. Corroborating evidence about who "
                    "actually used the credential, as opposed to the "
                    "self-descriptive client_name supplied at registration."
                ),
                data_sensitivity=0.6,
            ),
            "last_use_user_agent": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "User-Agent header observed on the most recent "
                    "successful /oauth/token request, truncated to "
                    "MAX_LAST_USE_USER_AGENT_LEN. Client-controlled and "
                    "therefore evidence, never proof — recorded so an "
                    "operator can compare what a client CLAIMS to be "
                    "(client_name) against what it PRESENTS as."
                ),
                data_sensitivity=0.6,
            ),
            "last_use_transport": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Which OAuth grant minted the most recent successful "
                    "token (authorization_code, client_credentials or "
                    "refresh_token). Distinguishes an interactive browser "
                    "exchange from silent machine renewal."
                ),
                data_sensitivity=0.2,
            ),
        },
        indexes=[
            IndexDefinition("idx_oauth_client_id", ["client_id"]),
        ],
    )


def get_oauth_refresh_token_schema() -> TableSchema:
    """OAuth 2.1 refresh-token registry.

    One row per *active* refresh token; rows are deleted at consume
    time so the table never grows beyond the set of currently-valid
    tokens.  Tokens are stored as sha256 hashes — the cleartext value
    only exists outside the vault (carried in the token response and
    held by the client).

    Audience binding ensures a refresh token can only mint access
    tokens for the same MCP resource it was originally bound to;
    rotating to a token for a different audience would defeat the
    point of audience binding.

    Standard fields (``id``, ``created_at``, ``updated_at``, ...) are
    auto-injected by :class:`SchemaStandardizer`.
    """
    return TableSchema(
        table_name=OAUTH_REFRESH_TOKEN_TABLE,
        description="OAuth 2.1 refresh-token registry (single-use, rotated)",
        id_prefix=OAUTH_REFRESH_TOKEN_ID_PREFIX,
        data_sensitivity=0.8,
        columns={
            "token_hash": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description="sha256 of the cleartext refresh token, base64-encoded.",
                data_sensitivity=1.0,  # RESTRICTED — derived from token material
            ),
            "client_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="OAuth client the refresh token is bound to.",
                data_sensitivity=0.3,
            ),
            "scopes": ColumnDefinition(
                type=ColumnType.JSON,
                default="[]",
                description="Scopes granted at the originating authorize step.",
                data_sensitivity=0.2,
            ),
            "audience": ColumnDefinition(
                type=ColumnType.TEXT,
                default="",
                description=(
                    "Canonical MCP URI the refresh token may mint access "
                    "tokens for (RFC 8707 aud binding)."
                ),
                data_sensitivity=0.2,
            ),
            "expires_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Absolute expiry timestamp as ISO-8601 UTC.",
                data_sensitivity=0.1,
            ),
        },
        indexes=[
            IndexDefinition("idx_oauth_refresh_token_hash", ["token_hash"]),
            IndexDefinition("idx_oauth_refresh_client", ["client_id"]),
        ],
    )


def get_vault_schema() -> SchemaDefinition:
    """Vault secrets schema.

    Tables:
    - secret: Encrypted secrets with AES-256-GCM, salt, nonce, auth_tag
    - oauth_client: OAuth 2.1 client registry (operator-approved; multi-grant)
      for the Streamable HTTP MCP transport
    - oauth_refresh_token: OAuth 2.1 refresh-token registry, single-use
      rotation per §4.3.1

    The audit table (default_vault_plugin__audit) is declared separately in
    get_schema_definitions() using the shared schema from vault_core.
    """
    return SchemaDefinition(
        namespace=NAMESPACE,
        tables={
            OAUTH_CLIENT_TABLE: get_oauth_client_schema(),
            OAUTH_REFRESH_TOKEN_TABLE: get_oauth_refresh_token_schema(),
            "secret": TableSchema(
                table_name="secret",
                description="Encrypted secrets with AES-256-GCM encryption",
                id_prefix="sec",
                data_sensitivity=1.0,  # RESTRICTED - entire table contains sensitive data
                columns={
                    "secret_key": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        unique=True,
                        description="Unique secret lookup key",
                        data_sensitivity=0.3,  # Key name is low sensitivity
                    ),
                    "encrypted_value": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="AES-256-GCM encrypted value (base64 encoded)",
                        data_sensitivity=1.0,  # RESTRICTED - never expose to LLM
                    ),
                    "salt": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="PBKDF2 salt for key derivation (base64 encoded)",
                        data_sensitivity=1.0,  # RESTRICTED - cryptographic material
                    ),
                    "nonce": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="GCM nonce/IV (base64 encoded)",
                        data_sensitivity=1.0,  # RESTRICTED - cryptographic material
                    ),
                    "auth_tag": ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="GCM authentication tag (base64 encoded)",
                        data_sensitivity=1.0,  # RESTRICTED - cryptographic material
                    ),
                    "tags": ColumnDefinition(
                        type=ColumnType.JSON,
                        default="[]",
                        description="JSON array of tags for organization",
                        data_sensitivity=0.3,  # Tags are organizational metadata
                    ),
                    "metadata": ColumnDefinition(
                        type=ColumnType.JSON,
                        default="{}",
                        description="Secret metadata (key type, expiration, rotation info)",
                        data_sensitivity=0.5,  # Metadata may contain sensitive info
                    ),
                    "version": ColumnDefinition(
                        type=ColumnType.INTEGER,
                        default=1,
                        description="Secret version (increments on key rotation)",
                        data_sensitivity=0.2,  # Version number is low sensitivity
                    ),
                    # Standard fields auto-provided: id, external_id, name, created_at, updated_at
                },
                indexes=[
                    IndexDefinition("idx_secret_key", ["secret_key"]),
                    IndexDefinition("idx_secret_version", ["version"]),
                ],
            ),
        },
    )
