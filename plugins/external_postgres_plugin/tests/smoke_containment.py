#!/usr/bin/env python3
"""Platform-DB containment smoke tests (§8.4) for external_postgres_plugin.

Hermetic — pure guard logic, no Postgres. Proves the ROLE-INDEPENDENT refusal
of the platform's own DB INSTANCE ``(host, port, dbname)`` and the legitimate
target classes that must still pass.

RED-FIRST: the refusal assertions are the teeth. If ``assert_foreign_target``
were neutralized to a no-op (the design's "neutralize the guard → the
platform-instance connect is reached → red"), every ``*_refused`` check below
would FAIL — the connect would be reached. The allowed cases guard the opposite
error (an over-broad guard that refuses legitimate localhost dev DBs).

Exercises (``dbname=<platform>`` means the LIVE platform DB name, derived from
``PLATFORM_DBNAME`` at runtime — never hardcoded, so the refusal is proven
against whatever solet actually runs this smoke):
  1. user=ananta, dbname=<platform>, platform host:port  -> REFUSED
  2. user=trustuser, dbname=<platform>, platform host:port  -> REFUSED (F-D1 role-independent)
  3. host="" (unix socket),  dbname=<platform>, port     -> REFUSED (blank-host socket)
  4. host="/tmp" (socket dir), dbname=<platform>, port   -> REFUSED (/-prefixed socket)
  5. blank port (resolver -> 5432), dbname=<platform>    -> REFUSED (blank-port bypass closed)
  6. dbname=analytics on localhost                       -> ALLOWED (legit dev DB)
  7. dbname=<platform> on a DIFFERENT port               -> ALLOWED (not the platform instance)
  8. dbname=<platform> on a DIFFERENT host               -> ALLOWED (coincidental name)
  9. _normalize_host canonicalization sanity

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_containment.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin.app_config import AppConfigLoader, ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import (  # noqa: E402
    ExternalPgGuardError,
    _normalize_host,
    assert_foreign_target,
)
from external_postgres_plugin.constants import PLATFORM_DBNAME  # noqa: E402

_PLATFORM_PORT = 5432
_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _dsn(host: str, port: int, dbname: str, user: str) -> ExternalDsn:
    return ExternalDsn(
        name="probe",
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password="x",
        sslmode="disable",
    )


def _is_refused(dsn: ExternalDsn) -> bool:
    try:
        assert_foreign_target(dsn, _PLATFORM_PORT)
        return False
    except ExternalPgGuardError as exc:
        return exc.code == "external_pg.platform_db_refused"


class _FakeAddressBook:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def resolve_with_secrets(self, name: str) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"entries": self._entries}}


def test_refused_ananta() -> None:
    _assert(
        f"ananta+{PLATFORM_DBNAME}+localhost:5432 REFUSED",
        _is_refused(_dsn("localhost", 5432, PLATFORM_DBNAME, "ananta")),
    )


def test_refused_trust_role() -> None:
    # The F-D1 role-independent case: an arbitrary non-"ananta" role reaching
    # the SAME platform DB (named "trustuser" to echo smoke_readonly.py's
    # documented local passwordless-trust fixture role, but the identity of
    # the role is not load-bearing here — ANY role proves the point). The
    # refusal keys on the instance, not the role.
    _assert(
        f"trustuser+{PLATFORM_DBNAME}+localhost:5432 REFUSED (role-independent)",
        _is_refused(_dsn("localhost", 5432, PLATFORM_DBNAME, "trustuser")),
    )


def test_refused_socket_blank() -> None:
    _assert(
        f"blank-host socket + {PLATFORM_DBNAME} + port REFUSED",
        _is_refused(_dsn("", 5432, PLATFORM_DBNAME, "trustuser")),
    )


def test_refused_socket_path() -> None:
    _assert(
        f"/tmp socket-dir + {PLATFORM_DBNAME} + port REFUSED",
        _is_refused(_dsn("/tmp", 5432, PLATFORM_DBNAME, "trustuser")),
    )
    _assert(
        f"/var/run/postgresql socket-dir + {PLATFORM_DBNAME} + port REFUSED",
        _is_refused(_dsn("/var/run/postgresql", 5432, PLATFORM_DBNAME, "ananta")),
    )


def test_refused_blank_port_via_resolver() -> None:
    # End-to-end: a platform DSN registered with a BLANK port resolves to 5432
    # (app_config._coerce_port) and is then refused — the blank-port bypass is
    # closed at both layers.
    entries = [
        {"field_type": "host", "value": "127.0.0.1"},
        {"field_type": "port", "value": ""},
        {"field_type": "dbname", "value": PLATFORM_DBNAME},
        {"field_type": "user", "value": "trustuser"},
        {"field_type": "password", "value": "x"},
    ]
    dsn = AppConfigLoader(_FakeAddressBook(entries)).resolve("sneaky")
    _assert("resolver defaults blank port to 5432", dsn.port == 5432)
    _assert("blank-port platform DSN REFUSED end-to-end", _is_refused(dsn))


def test_allowed_localhost_dev_db() -> None:
    _assert("analytics on localhost ALLOWED", not _is_refused(_dsn("localhost", 5432, "analytics", "dev")))


def test_allowed_platform_dbname_different_port() -> None:
    _assert(
        f"{PLATFORM_DBNAME} on a DIFFERENT port ALLOWED",
        not _is_refused(_dsn("localhost", 5544, PLATFORM_DBNAME, "trustuser")),
    )


def test_allowed_platform_dbname_different_host() -> None:
    _assert(
        f"{PLATFORM_DBNAME}-named DB on a DIFFERENT host ALLOWED",
        not _is_refused(_dsn("warehouse.example.com", 5432, PLATFORM_DBNAME, "svc")),
    )


def test_normalize_host() -> None:
    _assert("blank -> socket sentinel", _normalize_host("") == "")
    _assert("whitespace -> socket sentinel", _normalize_host("   ") == "")
    _assert("/tmp -> socket sentinel", _normalize_host("/tmp") == "")
    _assert("loopback lowercased", _normalize_host("LocalHost") == "localhost")
    _assert("real host preserved", _normalize_host("Warehouse.example.com") == "warehouse.example.com")


_OPERATOR_USERNAME_TOKEN = "d" + "w"


def test_source_carries_no_operator_username() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-31): the DSN
    role-name fixtures in this file are arbitrary — ``test_refused_trust_role``
    proves refusal is ROLE-INDEPENDENT, so any role string proves the same
    point — there is no functional reason for the real operator's OS username
    to appear here. Composed from two concatenated halves (see
    ``_OPERATOR_USERNAME_TOKEN``) so this guard's own source never contains
    the contiguous token it hunts for. Word-bounded so it does not collide
    with an unrelated substring.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _assert(
        "fixture source carries no bare operator-username token",
        pattern.search(source) is None,
    )


def main() -> int:
    print("\nexternal_postgres_plugin containment smoke tests")
    print("=" * 48)
    test_source_carries_no_operator_username()
    test_refused_ananta()
    test_refused_trust_role()
    test_refused_socket_blank()
    test_refused_socket_path()
    test_refused_blank_port_via_resolver()
    test_allowed_localhost_dev_db()
    test_allowed_platform_dbname_different_port()
    test_allowed_platform_dbname_different_host()
    test_normalize_host()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All containment smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
