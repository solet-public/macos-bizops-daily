"""Snowflake plugin constants.

Single source of truth for every magic value: the address-book credential
shape, session hardening knobs, the read-only statement-leader set (belt),
result caps, error codes, and result types.

Posture (operator-ratified RATIFY-2/§5.6 for the READ verbs; reversed for
write by operator ruling 2026-08-09 + Amendment 1 — "vendor RBAC is the
control plane"): every read verb stays READ-ONLY via the connector-side
statement-leader guard, FAST-FAIL ONLY (defense-in-depth + UX) since Snowflake
has no session-level read-only connection characteristic — the TRUE
developer-proof boundary for those verbs is the read-only ROLE the connection
is pinned to (GRANT SELECT/USAGE only, no INSERT/UPDATE/DELETE/MERGE/DDL). The
write verb (``run_statement``) performs NO plugin-side access control at all —
what it can do is decided entirely by the registered credential's own
server-side role grants. See knowledge_base/01_snowflake_overview.md for the
full posture + the asymmetry with the Postgres connector.

This plugin reaches ONLY the Snowflake account resolved from the
"snowflake_account" address-book entry — never any other external system.
"""

import os
from typing import Final


def _solet_or_fail() -> str:
    """Resolve SOLET_NAME at import-time for scoped vault keys.

    Vault keys follow the ``<solet>.<plugin>.<credential>`` convention.
    Mirrors the fast-fail helper in external_postgres_plugin.constants.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "snowflake_plugin.constants: SOLET_NAME env var is "
            "required to resolve scoped vault keys.",
        )
    return name


_SOLET = _solet_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "snowflake_plugin"
PLUGIN_VERSION: Final[str] = "1.0.0"

# ---------------------------------------------------------------------------
# Credentials — ONE address-book entry: "snowflake_account". Single-account v1
# (per-account entries are a v2 extension if a second account appears).
# ---------------------------------------------------------------------------
ACCOUNT_ENTRY_NAME: Final[str] = "snowflake_account"
ACCOUNT_ADDRESS_TYPE: Final[str] = "database"

FIELD_ACCOUNT: Final[str] = "account"
FIELD_USER: Final[str] = "user"
FIELD_WAREHOUSE: Final[str] = "warehouse"
FIELD_DATABASE: Final[str] = "database"
FIELD_SCHEMA: Final[str] = "schema"
FIELD_ROLE: Final[str] = "role"
FIELD_AUTH_METHOD: Final[str] = "auth_method"
FIELD_PRIVATE_KEY: Final[str] = "private_key"

AUTH_METHOD_KEY_PAIR: Final[str] = "key_pair"


def vault_key_for_private_key() -> str:
    """Scoped vault key for the connecting user's RSA private key — CHAIN-CONSUMED.

    Lives in the RESOLVER's namespace
    (``<solet>.default_address_book_plugin.snowflake_private_key``) so the
    address book reads it under its own identity via ``resolve_with_secrets``.
    Post-2026-06-07 vault namespace enforcement requires the key's ``<plugin>``
    segment to equal the retrieving caller, so this key is declared in NEITHER
    get_required_vault_keys nor get_declared_vault_keys (canonical:
    VAULT_AND_ADDRESS_BOOK.md).
    """
    return f"{_SOLET}.default_address_book_plugin.snowflake_private_key"


# ---------------------------------------------------------------------------
# Session hardening (connection.py)
# ---------------------------------------------------------------------------
STATEMENT_TIMEOUT_SECONDS_DEFAULT: Final[int] = 60
CONFIG_KEY_STATEMENT_TIMEOUT_SECONDS: Final[str] = "statement_timeout_seconds"
LOGIN_TIMEOUT_SECONDS_DEFAULT: Final[int] = 30
CONFIG_KEY_LOGIN_TIMEOUT_SECONDS: Final[str] = "login_timeout_seconds"

# ---------------------------------------------------------------------------
# Read-leader guard (belt — FAST-FAIL ONLY; Snowflake has no session-level
# read-only flag, so the TRUE write-stopper is the operator-granted read-only
# ROLE, not this guard). The Datagrip-parity read/introspection family
# (rev-D addendum b): SELECT, WITH, SHOW, DESCRIBE/DESC, EXPLAIN. DML-with-CTE
# leads with the DML verb (INSERT INTO … WITH …), so a WITH leader is
# SELECT-shaped; PUT/GET/COPY/CALL/USE/CREATE all refuse.
# ---------------------------------------------------------------------------
READ_LEADERS: Final[frozenset[str]] = frozenset(
    {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH"}
)

# ---------------------------------------------------------------------------
# Result caps (business-data limits + data-export migration, 2026-08-02 —
# workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
# Both run_query and export_query now ALWAYS write to a caller-supplied path
# (07-29 data-export requirement, unconditional — the former INLINE_BYTE_CAP/inline-return
# branch is deleted, not lowered: no record-read verb returns record values
# inline at any size). DEFAULT_ROW_LIMIT is the fetch ceiling absent an
# explicit, acknowledged override; MAX_ROWS_HARD_CAP is run_query's override
# ceiling, EXPORT_ROW_CAP is export_query's (the "N>>500" Pattern-A route,
# §7.2) — both are pushed into the fetch via ``fetchmany``, never
# fetch-everything-then-truncate.
# ---------------------------------------------------------------------------
DEFAULT_ROW_LIMIT: Final[int] = 500
MAX_ROWS_HARD_CAP: Final[int] = 1000
EXPORT_ROW_CAP: Final[int] = 50_000

# ---------------------------------------------------------------------------
# Override friction (§5) — required together or not at all; absent means the
# effective limit is DEFAULT_ROW_LIMIT. No vendor ceiling applies to this
# connector: Snowflake's own Python connector documentation
# (docs.snowflake.com/en/user-guide/python-connector-api, checked 2026-08-02)
# imposes no maximum row count per query, per fetch, or per result set —
# `arraysize`/`client_prefetch_threads` are client-side performance knobs, not
# vendor caps; `fetchall()` retrieves all remaining rows with no stated
# ceiling. Both limits below are entirely our own policy (§5.1 sourced-numbers
# rule — this is the citation).
# ---------------------------------------------------------------------------
PARAM_ACKNOWLEDGE_OVERRIDE: Final[str] = "acknowledge_default_limit_override"
PARAM_ROW_LIMIT: Final[str] = "row_limit"

# ---------------------------------------------------------------------------
# Export (A2, 2026-07-15): bulk results land as TSV files in the operator's
# OWN workspace — never platform blob storage. The write is gated by
# realpath+commonpath containment under the operator-configured roots.
# ---------------------------------------------------------------------------
TSV_SUFFIX: Final[str] = ".tsv"
CONFIG_KEY_EXPORT_ALLOWED_ROOTS: Final[str] = "export_allowed_roots"

# ---------------------------------------------------------------------------
# Error codes — snowflake.* surfaced to callers; the *_NOT_AVAILABLE codes are
# internal service-binding faults.
# ---------------------------------------------------------------------------
ERROR_VAULT_NOT_AVAILABLE: Final[str] = "vault_service_not_available"
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"

ERROR_NOT_CONFIGURED: Final[str] = "snowflake.not_configured"
ERROR_INVALID_PARAMS: Final[str] = "snowflake.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "snowflake.auth_failed"
ERROR_PERMISSION_DENIED: Final[str] = "snowflake.permission_denied"
ERROR_OBJECT_NOT_FOUND: Final[str] = "snowflake.object_not_found"
ERROR_READ_ONLY_VIOLATION: Final[str] = "snowflake.read_only_violation"
ERROR_TIMEOUT: Final[str] = "snowflake.timeout"
ERROR_WAREHOUSE_SUSPENDED: Final[str] = "snowflake.warehouse_suspended"
ERROR_API_ERROR: Final[str] = "snowflake.api_error"
ERROR_EXPORT_PATH_REFUSED: Final[str] = "snowflake.export_path_refused"

# ---------------------------------------------------------------------------
# Generic FIXED error messages (§1.6/F3). Driver exception strings embed
# account/user/warehouse topology — exactly the material §2.4 forbids in
# results — so connection/auth/permission/timeout/warehouse classes carry a
# fixed message per code and NEVER embed str(exc). Object-not-found and
# query-syntax classes may keep driver detail (it describes the caller's own
# query/object, not our topology).
# ---------------------------------------------------------------------------
GENERIC_MESSAGE_AUTH_FAILED: Final[str] = (
    "authentication failed for the configured Snowflake account"
)
GENERIC_MESSAGE_PERMISSION_DENIED: Final[str] = (
    "the configured role lacks permission for this operation"
)
GENERIC_MESSAGE_CONNECTION_FAILED: Final[str] = (
    "could not connect to the configured Snowflake account"
)
GENERIC_MESSAGE_TIMEOUT: Final[str] = "the query exceeded the statement timeout"
GENERIC_MESSAGE_WAREHOUSE_SUSPENDED: Final[str] = (
    "the configured warehouse is suspended or unavailable"
)
GENERIC_MESSAGE_READ_ONLY: Final[str] = (
    "this connector is read-only; write and DDL statements are refused"
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_RUN_QUERY: Final[str] = "snowflake_run_query_result"
RESULT_TYPE_RUN_STATEMENT: Final[str] = "snowflake_run_statement_result"
RESULT_TYPE_LIST_DATABASES: Final[str] = "snowflake_list_databases_result"
RESULT_TYPE_LIST_SCHEMAS: Final[str] = "snowflake_list_schemas_result"
RESULT_TYPE_LIST_TABLES: Final[str] = "snowflake_list_tables_result"
RESULT_TYPE_DESCRIBE_TABLE: Final[str] = "snowflake_describe_table_result"
RESULT_TYPE_EXPORT_QUERY: Final[str] = "snowflake_export_query_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "snowflake_test_connection_result"
