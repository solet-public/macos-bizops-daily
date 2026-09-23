"""psycopg3 connection building + session hardening + platform-DB containment.

This is the ONLY module that imports psycopg (S0-exempt whole-file — its driver
use targets FOREIGN databases resolved from ``external_pg::*`` address-book
entries; it is structurally incapable of reaching the platform DB, enforced by
:func:`assert_foreign_target` + the red-first containment smoke).

Read-only is the psycopg3 CONNECTION CHARACTERISTIC (``conn.read_only = True``),
set BEFORE the first execute so it applies at the BEGIN of EVERY transaction
including the first implicit one — there is NO write-capable window. This is the
LOAD-BEARING write-stopper (§8.5) for every READ verb: a write fails at the
server with SQLSTATE 25006 regardless of statement leader/count/smuggling, even
for an over-privileged registered credential. NOT a post-connect
``SET default_transaction_read_only`` (which leaves the first implicit
transaction write-capable — Codex BLOCKER).

Write verbs (operator ruling, 2026-08-09 — the connector read-only posture
reversal + Amendment 1, "vendor RBAC is the control plane") open a connection
with ``read_only=False`` instead — a deliberate, verb-scoped choice, never a
default. Whether such a connection can ACTUALLY write is then decided entirely
by the registered credential's own server-side Postgres GRANTs; this module
performs no plugin-side permission check either way.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .app_config import ExternalDsn
from .constants import (
    CONNECT_TIMEOUT_SECONDS,
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_PERMISSION_DENIED,
    ERROR_PLATFORM_DB_REFUSED,
    ERROR_QUERY_FAILED,
    ERROR_READ_ONLY_VIOLATION,
    ERROR_TIMEOUT,
    GENERIC_MESSAGE_AUTH_FAILED,
    GENERIC_MESSAGE_CONNECTION_FAILED,
    GENERIC_MESSAGE_PERMISSION_DENIED,
    GENERIC_MESSAGE_READ_ONLY,
    GENERIC_MESSAGE_TIMEOUT,
    PLATFORM_DBNAME,
    PLATFORM_HOSTS,
    SOCKET_HOST_SENTINEL,
)


class ExternalPgGuardError(RuntimeError):
    """Raised when a target is refused by the platform-DB containment guard."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalize_host(host: str) -> str:
    """Canonicalize a host into the comparison form used by ``PLATFORM_HOSTS``.

    A blank host AND any absolute-path (unix-socket directory) host — libpq
    treats any ``/``-prefixed host as a socket dir (``/tmp``,
    ``/var/run/postgresql``) — both collapse to the socket sentinel, so the
    platform DB reached over its socket is caught no matter the spelling.
    Loopback names/IPs are lowercased verbatim.
    """
    stripped = host.strip()
    if not stripped or stripped.startswith("/"):
        return SOCKET_HOST_SENTINEL
    return stripped.lower()


def assert_foreign_target(dsn: ExternalDsn, platform_pg_port: int) -> None:
    """Refuse the platform's own DB INSTANCE (host + port + dbname), ROLE-INDEPENDENTLY.

    Catches every role on the platform's own database. The refusal keys on the
    INSTANCE, not the role, so a mis-registered platform DSN cannot read
    platform-internal state. localhost stays a legitimate target CLASS: a dev DB
    with a different dbname, or a same-named DB on a different host/port, both
    pass. Only the exact platform instance on the platform host:port is refused.
    """
    if (
        dsn.dbname.strip().lower() == PLATFORM_DBNAME
        and _normalize_host(dsn.host) in PLATFORM_HOSTS
        and int(dsn.port) == platform_pg_port
    ):
        raise ExternalPgGuardError(
            ERROR_PLATFORM_DB_REFUSED,
            "refusing the platform's own database instance; external_postgres_plugin "
            "targets FOREIGN databases only",
        )


def connect(
    dsn: ExternalDsn,
    *,
    statement_timeout_ms: int,
    platform_pg_port: int,
    read_only: bool = True,
) -> psycopg.Connection[Any]:
    """Open a hardened connection to a foreign Postgres DB.

    Enforces the containment guard first (never connects without it), sets the
    statement_timeout windowlessly via libpq startup options, and applies the
    ``read_only`` connection characteristic BEFORE any execute.

    ``read_only`` defaults to ``True`` — every existing (read-verb) call site is
    unaffected. A write verb passes ``read_only=False`` explicitly; whether that
    connection's credential can actually write is then entirely the server's own
    GRANT decision, never a plugin-side check.
    """
    assert_foreign_target(dsn, platform_pg_port)
    conn: psycopg.Connection[Any] = psycopg.connect(
        host=dsn.host or None,
        port=dsn.port,
        dbname=dsn.dbname,
        user=dsn.user,
        password=dsn.password,
        sslmode=dsn.sslmode,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={statement_timeout_ms}",
    )
    # BEFORE any execute (connection is not yet in a transaction): psycopg3 emits
    # the read_only characteristic at every transaction BEGIN incl. the first, so
    # there is no write-capable window for a read-only connection to slip through.
    conn.read_only = read_only
    return conn


def classify_pg_error(exc: Exception) -> tuple[str, str]:
    """Map a psycopg error to a typed (code, message) — TOPOLOGY-SAFE (§1.6/F3).

    Connection/auth/permission/timeout classes carry a GENERIC fixed message —
    driver exception strings embed host/port/db/user topology (the DSN material
    §2.4 forbids in results), so those NEVER surface ``str(exc)``. Query-shape /
    data / constraint classes describe the caller's OWN query (not our topology),
    so they carry the server's primary diagnostic message. An unrecognized error
    (no SQLSTATE — e.g. a bare connection failure) defaults to a generic
    connection failure, never a raw string.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if not isinstance(sqlstate, str):
        return ERROR_API_ERROR, GENERIC_MESSAGE_CONNECTION_FAILED
    if sqlstate in ("28P01", "28000"):  # invalid_password / invalid_authorization
        return ERROR_AUTH_FAILED, GENERIC_MESSAGE_AUTH_FAILED
    if sqlstate == "42501":  # insufficient_privilege
        return ERROR_PERMISSION_DENIED, GENERIC_MESSAGE_PERMISSION_DENIED
    if sqlstate == "25006":  # read_only_sql_transaction
        return ERROR_READ_ONLY_VIOLATION, GENERIC_MESSAGE_READ_ONLY
    if sqlstate == "57014":  # query_canceled (statement_timeout)
        return ERROR_TIMEOUT, GENERIC_MESSAGE_TIMEOUT
    if sqlstate[:2] in ("08", "53", "57", "58", "3D", "3F"):
        # connection / resource / operator-intervention / catalog classes — a
        # topology-leak risk, so a generic connection message.
        return ERROR_API_ERROR, GENERIC_MESSAGE_CONNECTION_FAILED
    if sqlstate[:2] in ("42", "22", "23", "0A", "2B", "2F"):
        # syntax / data / integrity / feature — describes the caller's OWN query.
        return ERROR_QUERY_FAILED, _safe_pg_message(exc)
    return ERROR_API_ERROR, GENERIC_MESSAGE_CONNECTION_FAILED


def _safe_pg_message(exc: Exception) -> str:
    """The server's primary diagnostic message (no host/port topology), or a fallback."""
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None)
    return primary if isinstance(primary, str) and primary else "query failed"
