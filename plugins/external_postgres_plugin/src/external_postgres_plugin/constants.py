"""External Postgres plugin constants.

Single source of truth for every magic value: the connection registry
address-book shape, the platform-DB containment markers (§8.4), session
hardening knobs, the Datagrip-parity read-leader set (belt), result caps,
error codes, and result types.

Posture (operator-ratified rev-D/rev-F for the READ verbs; reversed for write
by operator ruling 2026-08-09 + Amendment 1 — "vendor RBAC is the control
plane"): every read verb is READ-ONLY, HARD via the psycopg3 connection
read-only characteristic (``conn.read_only = True``, connection.py); the
read-leader guard + single-statement parser are belts, read-verb-only. The
write verb (``run_statement``) opens with ``read_only=False`` and performs NO
plugin-side access control — what it can do is decided entirely by the
registered credential's own server-side Postgres GRANTs. See knowledge_base/
01_external_postgres_overview.md for the full posture + containment invariants.

This is a "super Datagrip" over FOREIGN Postgres databases the operator
registers as ``external_pg::<name>`` address-book entries — never the platform's
own DB (postgres_state_management_plugin owns that). The containment guard
(§8.4) refuses the platform's own instance role-independently, for every verb.
"""

import os
from typing import Final


def _solet_or_fail() -> str:
    """Resolve SOLET_NAME at import-time for scoped vault keys.

    Vault keys follow the ``<solet>.<plugin>.<credential>`` convention.
    Mirrors the fast-fail helper in g_suite_plugin.constants +
    schwab_market_data_plugin.constants.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "external_postgres_plugin.constants: SOLET_NAME env var is "
            "required to resolve scoped vault keys.",
        )
    return name


_SOLET = _solet_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "external_postgres_plugin"
PLUGIN_VERSION: Final[str] = "1.0.0"

# ---------------------------------------------------------------------------
# Connection registry — one address-book entry per connection
# (``external_pg::<name>``). Every verb takes a connection NAME resolved
# through the address book — never a raw DSN (containment invariant #1).
# ---------------------------------------------------------------------------
CONNECTION_ENTRY_PREFIX: Final[str] = "external_pg::"
CONNECTION_ADDRESS_TYPE: Final[str] = "database"

FIELD_HOST: Final[str] = "host"
FIELD_PORT: Final[str] = "port"
FIELD_DBNAME: Final[str] = "dbname"
FIELD_USER: Final[str] = "user"
FIELD_SSLMODE: Final[str] = "sslmode"
FIELD_PASSWORD: Final[str] = "password"

DEFAULT_SSLMODE: Final[str] = "require"
# libpq's own default when a connection entry omits the port.
DEFAULT_PORT: Final[int] = 5432

# ``list_connections`` bounds its address-book scan (no silent caps).
LIST_CONNECTIONS_SCAN_LIMIT: Final[int] = 200


def vault_key_for_password(name: str) -> str:
    """Scoped vault key for a connection's password — CHAIN-CONSUMED.

    The password lives in the RESOLVER's namespace
    (``<solet>.default_address_book_plugin.external_pg_<name>_password``)
    so the address book reads it under its own identity via
    ``resolve_with_secrets``. Post-2026-06-07 vault namespace enforcement
    requires the key's ``<plugin>`` segment to equal the retrieving caller, so
    this key is declared in NEITHER get_required_vault_keys nor
    get_declared_vault_keys (canonical: VAULT_AND_ADDRESS_BOOK.md).
    """
    return f"{_SOLET}.default_address_book_plugin.external_pg_{name}_password"


# ---------------------------------------------------------------------------
# §8.4 platform-DB containment markers — refuse the platform's OWN instance
# ``(host, port, dbname)``, ROLE-INDEPENDENTLY.
# ---------------------------------------------------------------------------
PLATFORM_DBNAME: Final[str] = _SOLET
# The socket sentinel: a blank host and any absolute-path (unix-socket dir)
# host both canonicalize to "" via connection._normalize_host, so "" in this
# set matches every unix-socket spelling.
SOCKET_HOST_SENTINEL: Final[str] = ""
PLATFORM_HOSTS: Final[frozenset[str]] = frozenset(
    {"localhost", "127.0.0.1", "::1", SOCKET_HOST_SENTINEL}
)
# The platform's own Postgres port. Read from THIS plugin's own config
# (plugin.yaml ``platform_pg_port``) at readiness and injected into the guard;
# NEVER read from the state plugin's config (that coupling is what the design
# avoids). Default 5432 — keep in sync if the platform ever runs off-5432.
PLATFORM_PG_PORT_DEFAULT: Final[int] = 5432
CONFIG_KEY_PLATFORM_PG_PORT: Final[str] = "platform_pg_port"

# ---------------------------------------------------------------------------
# Session hardening (connection.py)
# ---------------------------------------------------------------------------
CONNECT_TIMEOUT_SECONDS: Final[int] = 10
STATEMENT_TIMEOUT_MS_DEFAULT: Final[int] = 30_000
CONFIG_KEY_STATEMENT_TIMEOUT_MS: Final[str] = "statement_timeout_ms"

# ---------------------------------------------------------------------------
# Read-leader guard (belt — the LOAD-BEARING write-stopper is conn.read_only).
# The full Datagrip read/introspection family (rev-D addendum b): blocking
# EXPLAIN/SHOW in a Datagrip replacement is a workflow-breaker, and
# ``EXPLAIN ANALYZE <write>`` is safe because the read-only session refuses the
# write at SQLSTATE 25006 regardless.
# ---------------------------------------------------------------------------
READ_LEADERS: Final[frozenset[str]] = frozenset(
    {"SELECT", "WITH", "EXPLAIN", "SHOW", "VALUES", "TABLE"}
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
# connector: an arbitrary customer database has no API-level cap to defer to
# (Reviewer-D's census, Part 1 — this row is OURS-ARBITRARY, not vendor-imposed).
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
# Error codes — external_pg.* surfaced to callers; the *_NOT_AVAILABLE codes
# are internal service-binding faults.
# ---------------------------------------------------------------------------
ERROR_VAULT_NOT_AVAILABLE: Final[str] = "vault_service_not_available"
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"

ERROR_NOT_CONFIGURED: Final[str] = "external_pg.not_configured"
ERROR_CONNECTION_UNKNOWN: Final[str] = "external_pg.connection_unknown"
ERROR_PLATFORM_DB_REFUSED: Final[str] = "external_pg.platform_db_refused"
ERROR_INVALID_PARAMS: Final[str] = "external_pg.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "external_pg.auth_failed"
ERROR_PERMISSION_DENIED: Final[str] = "external_pg.permission_denied"
ERROR_READ_ONLY_VIOLATION: Final[str] = "external_pg.read_only_violation"
ERROR_QUERY_FAILED: Final[str] = "external_pg.query_failed"
ERROR_TIMEOUT: Final[str] = "external_pg.timeout"
ERROR_API_ERROR: Final[str] = "external_pg.api_error"
ERROR_EXPORT_PATH_REFUSED: Final[str] = "external_pg.export_path_refused"

# ---------------------------------------------------------------------------
# Generic FIXED error messages (§1.6/F3). Driver exception strings embed
# host/port/db/user topology — exactly the DSN material §2.4 forbids in
# results — so connection/auth/permission/timeout classes carry a fixed
# message per code and NEVER embed str(exc). Query-syntax classes may keep
# driver detail (it describes the caller's own SQL, not our topology).
# ---------------------------------------------------------------------------
GENERIC_MESSAGE_AUTH_FAILED: Final[str] = (
    "authentication failed for the requested connection"
)
GENERIC_MESSAGE_PERMISSION_DENIED: Final[str] = (
    "the connection's role lacks permission for this operation"
)
GENERIC_MESSAGE_CONNECTION_FAILED: Final[str] = (
    "could not connect to the requested database"
)
GENERIC_MESSAGE_TIMEOUT: Final[str] = "the query exceeded the statement timeout"
GENERIC_MESSAGE_READ_ONLY: Final[str] = (
    "this connection is read-only; write and DDL statements are refused"
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_RUN_QUERY: Final[str] = "external_postgres_run_query_result"
RESULT_TYPE_RUN_STATEMENT: Final[str] = "external_postgres_run_statement_result"
RESULT_TYPE_LIST_CONNECTIONS: Final[str] = "external_postgres_list_connections_result"
RESULT_TYPE_LIST_SCHEMAS: Final[str] = "external_postgres_list_schemas_result"
RESULT_TYPE_LIST_TABLES: Final[str] = "external_postgres_list_tables_result"
RESULT_TYPE_DESCRIBE_TABLE: Final[str] = "external_postgres_describe_table_result"
RESULT_TYPE_EXPORT_QUERY: Final[str] = "external_postgres_export_query_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "external_postgres_test_connection_result"
