"""Snowflake connection building + session hardening + error classification.

This is the ONLY module that imports ``snowflake.connector`` (the gate's S0
driver-import class does not fire for it — ``snowflake.connector`` is not a
recognized SQL-driver root — so no S0 allowlist entry is needed; see
knowledge_base/01_snowflake_overview.md §3).

Auth is key-pair (RSA JWT): the private key (already parsed to DER bytes by
app_config) is handed to the connector directly — no temp file, no PAT.

Posture asymmetry vs external_postgres_plugin (§4/§5.6): Snowflake has NO
session-level read-only flag equivalent to psycopg3's ``conn.read_only``. The
connector-side statement-leader guard (statement_guard.py) is therefore
FAST-FAIL ONLY — it cannot make an over-privileged role fail to write. The
TRUE developer-proof boundary is the read-only ROLE the connection is pinned
to (GRANT SELECT/USAGE only). Single-statement is NATIVE
(``MULTI_STATEMENT_COUNT`` defaults to 1 and this plugin never uses
``execute_string``), so no statement-splitting parser is needed here (unlike
the Postgres connector's ``sqlparse`` belt) — including for the write verb,
``run_statement`` (query_actions.py), verified live against the operator's own
account: a two-statement string is refused by the driver itself.

The write verb also opens no differently-configured connection: there is no
``connect()``-time flag equivalent to postgres's ``read_only=False`` to pass,
because there is nothing to flip — the connection this module builds is
identical for every verb, read or write; the registered role's own grants
decide what any of them can do (operator ruling 2026-08-09 + Amendment 1,
"vendor RBAC is the control plane"). What DOES differ for ``run_statement`` is
transactional, not connection-level: Snowflake defaults every session to
``AUTOCOMMIT=TRUE`` (each statement commits or rolls back on its own the
instant it finishes — confirmed against the installed
``snowflake-connector-python`` source and Snowflake's own transactions
documentation), so ``run_statement`` explicitly disables it on its own
connection before executing, to support a conditional rollback its RETURNING-
style branch needs. Every other verb runs unaffected under the normal
per-statement autocommit default.
"""

from __future__ import annotations

from typing import Any

import snowflake.connector

from .app_config import SnowflakeAccountConfig
from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_PERMISSION_DENIED,
    ERROR_READ_ONLY_VIOLATION,
    ERROR_TIMEOUT,
    ERROR_WAREHOUSE_SUSPENDED,
    GENERIC_MESSAGE_AUTH_FAILED,
    GENERIC_MESSAGE_CONNECTION_FAILED,
    GENERIC_MESSAGE_PERMISSION_DENIED,
    GENERIC_MESSAGE_READ_ONLY,
    GENERIC_MESSAGE_TIMEOUT,
    GENERIC_MESSAGE_WAREHOUSE_SUSPENDED,
)

# Known Snowflake DatabaseError.errno values (Snowflake Python connector docs).
_ERRNO_JWT_AUTH_FAILED = 390144
_ERRNO_INCORRECT_USERNAME_PASSWORD = 390100
_ERRNO_QUERY_TIMEOUT = 604
_ERRNO_WAREHOUSE_SUSPENDED = 606
_ERRNO_OBJECT_DOES_NOT_EXIST = 2003
_ERRNO_INSUFFICIENT_PRIVILEGES = 2036


def connect(config: SnowflakeAccountConfig, *, login_timeout_seconds: int) -> Any:
    """Open a Snowflake connection via key-pair (RSA JWT) auth.

    The private key is passed directly as DER bytes — no temp file. Session
    role is always set EXPLICITLY (never relies on the user's default role),
    so a misconfigured default role cannot silently widen or narrow authority.
    """
    return snowflake.connector.connect(
        account=config.account,
        user=config.user,
        private_key=config.private_key_der,
        warehouse=config.warehouse or None,
        database=config.database or None,
        schema=config.schema or None,
        role=config.role or None,
        login_timeout=login_timeout_seconds,
    )


def apply_session_hardening(conn: Any, *, statement_timeout_seconds: int) -> None:
    """Set the session-level statement timeout explicitly (windowless bound).

    Snowflake has no libpq-style startup option, so the timeout is applied via
    an explicit ``ALTER SESSION`` immediately after connect — before any
    caller-supplied statement runs.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(statement_timeout_seconds)}"
        )


# errno -> fixed (code, message) for the GENERIC-message classes (§1.6/F3).
# JWT auth and incorrect-username/password both map to the same auth_failed code.
_ERRNO_GENERIC_CLASSIFICATION: dict[int, tuple[str, str]] = {
    _ERRNO_JWT_AUTH_FAILED: (ERROR_AUTH_FAILED, GENERIC_MESSAGE_AUTH_FAILED),
    _ERRNO_INCORRECT_USERNAME_PASSWORD: (ERROR_AUTH_FAILED, GENERIC_MESSAGE_AUTH_FAILED),
    _ERRNO_QUERY_TIMEOUT: (ERROR_TIMEOUT, GENERIC_MESSAGE_TIMEOUT),
    _ERRNO_WAREHOUSE_SUSPENDED: (ERROR_WAREHOUSE_SUSPENDED, GENERIC_MESSAGE_WAREHOUSE_SUSPENDED),
    _ERRNO_INSUFFICIENT_PRIVILEGES: (ERROR_PERMISSION_DENIED, GENERIC_MESSAGE_PERMISSION_DENIED),
}

# sqlstate class prefix -> fixed (code, message) for the GENERIC-message classes.
_SQLSTATE_GENERIC_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("42501", ERROR_PERMISSION_DENIED, GENERIC_MESSAGE_PERMISSION_DENIED),
    ("08", ERROR_API_ERROR, GENERIC_MESSAGE_CONNECTION_FAILED),
)
# sqlstate class prefixes whose message describes the caller's OWN query/object.
_SQLSTATE_DETAIL_ALLOWED_PREFIXES: tuple[str, ...] = ("42", "22", "23")


def classify_snowflake_error(exc: Exception) -> tuple[str, str]:
    """Map a Snowflake connector error to a typed (code, message) — TOPOLOGY-SAFE.

    Auth/connection/permission/timeout/warehouse classes carry a GENERIC fixed
    message — driver exception strings embed account/user/warehouse topology
    (the material §2.4 forbids in results), so those NEVER surface ``str(exc)``.
    Object-not-found and query-syntax classes describe the caller's OWN query
    or object (not our topology), so they carry the driver's message.
    """
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and errno in _ERRNO_GENERIC_CLASSIFICATION:
        return _ERRNO_GENERIC_CLASSIFICATION[errno]
    if errno == _ERRNO_OBJECT_DOES_NOT_EXIST:
        return ERROR_API_ERROR, _safe_snowflake_message(exc)
    return _classify_by_sqlstate(exc)


def _classify_by_sqlstate(exc: Exception) -> tuple[str, str]:
    sqlstate = getattr(exc, "sqlstate", None)
    if not isinstance(sqlstate, str):
        return ERROR_API_ERROR, GENERIC_MESSAGE_CONNECTION_FAILED
    for prefix, code, message in _SQLSTATE_GENERIC_PREFIXES:
        if sqlstate == prefix or sqlstate.startswith(prefix):
            return code, message
    if sqlstate.startswith(_SQLSTATE_DETAIL_ALLOWED_PREFIXES):
        # syntax / data / integrity — describes the caller's OWN query.
        return ERROR_API_ERROR, _safe_snowflake_message(exc)
    return ERROR_API_ERROR, GENERIC_MESSAGE_CONNECTION_FAILED


def _safe_snowflake_message(exc: Exception) -> str:
    """A short, driver-provided message with no obvious account/host topology."""
    msg = getattr(exc, "msg", None)
    if isinstance(msg, str) and msg:
        return msg
    return "query failed"


# Re-exported for the guard module's naming symmetry with external_postgres_plugin.
GENERIC_MESSAGE_READ_ONLY_VIOLATION = GENERIC_MESSAGE_READ_ONLY
ERROR_READ_ONLY_VIOLATION_CODE = ERROR_READ_ONLY_VIOLATION
