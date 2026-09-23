#!/usr/bin/env python3
"""Config-resolution + JDBC-parse smoke tests for external_postgres_plugin.

Hermetic — a faked address_book_service, no network, no vault, no Postgres.
Red-first: every check asserts REAL behavior (a regression in app_config fails
here). Covers the rev-F R-D4 "password never logged" property directly: the
password never surfaces in a repr, a scrub, or a parse-failure error.

Exercises:
  1. resolve — builds ExternalDsn, vault password swapped in
  2. resolve — blank port defaults to 5432 (advisor blind-spot #1)
  3. resolve — unknown connection raises connection_unknown
  4. resolve — incomplete entry (missing field) raises connection_unknown
  5. ExternalDsn repr REDACTS the password
  6. parse_jdbc_url — netloc user:pass@host form
  7. parse_jdbc_url — query-param user/password form + jdbc: prefix strip
  8. parse_jdbc_url — password never appears in the parse-failure error
  9. scrub_password_from_url — password replaced by ***
  10. ParsedRegistration repr REDACTS the password
  11. list_connection_names — client-side prefix filter over substring search

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_config.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin.app_config import (  # noqa: E402
    AppConfigLoader,
    ExternalPgConfigError,
    parse_jdbc_url,
    scrub_password_from_url,
)

_PW = "s3cr3t-PW-MARKER"
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


class _FakeAddressBook:
    """Duck-typed address_book_service for resolve_with_secrets + search."""

    def __init__(
        self,
        entries: list[dict[str, Any]] | None = None,
        addresses: list[dict[str, Any]] | None = None,
        completed: bool = True,
    ) -> None:
        self._entries = entries
        self._addresses = addresses or []
        self._completed = completed

    def resolve_with_secrets(self, name: str) -> dict[str, Any]:
        if not self._completed or self._entries is None:
            return {"action_status": "error", "data": {}}
        return {"action_status": "completed", "data": {"entries": self._entries}}

    def search(self, query: str, address_type: str, limit: int) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"addresses": self._addresses}}


def _entries(**overrides: str) -> list[dict[str, Any]]:
    base = {
        "host": "db.example.com",
        "port": "5433",
        "dbname": "analytics",
        "user": "readonly",
        "sslmode": "require",
        "password": _PW,
    }
    base.update(overrides)
    return [{"field_type": k, "value": v} for k, v in base.items()]


def test_resolve_builds_dsn() -> None:
    loader = AppConfigLoader(_FakeAddressBook(entries=_entries()))
    dsn = loader.resolve("analytics")
    _assert("host resolved", dsn.host == "db.example.com")
    _assert("port coerced to int", dsn.port == 5433)
    _assert("dbname resolved", dsn.dbname == "analytics")
    _assert("user resolved", dsn.user == "readonly")
    _assert("sslmode resolved", dsn.sslmode == "require")
    _assert("vault password swapped in", dsn.password == _PW)
    _assert("name is the bare connection name", dsn.name == "analytics")


def test_resolve_port_defaults() -> None:
    loader = AppConfigLoader(_FakeAddressBook(entries=_entries(port="")))
    dsn = loader.resolve("analytics")
    _assert("blank port defaults to 5432", dsn.port == 5432)


def test_resolve_unknown() -> None:
    loader = AppConfigLoader(_FakeAddressBook(completed=False))
    code = ""
    try:
        loader.resolve("nope")
    except ExternalPgConfigError as exc:
        code = exc.code
    _assert("unknown connection -> connection_unknown", code == "external_pg.connection_unknown")


def test_resolve_incomplete() -> None:
    loader = AppConfigLoader(_FakeAddressBook(entries=_entries(user="")))
    code = ""
    raised_msg = ""
    try:
        loader.resolve("analytics")
    except ExternalPgConfigError as exc:
        code = exc.code
        raised_msg = str(exc)
    _assert("incomplete entry -> connection_unknown", code == "external_pg.connection_unknown")
    _assert("incomplete names the missing field", "user" in raised_msg)


def test_dsn_repr_redacts_password() -> None:
    loader = AppConfigLoader(_FakeAddressBook(entries=_entries()))
    dsn = loader.resolve("analytics")
    text = repr(dsn)
    _assert("dsn repr hides the password", _PW not in text, text)
    _assert("dsn repr shows the redaction marker", "***" in text)


def test_parse_jdbc_netloc() -> None:
    parsed = parse_jdbc_url(f"jdbc:postgresql://readonly:{_PW}@db.example.com:5433/analytics?sslmode=require")
    _assert("jdbc host", parsed.host == "db.example.com")
    _assert("jdbc port", parsed.port == 5433)
    _assert("jdbc dbname", parsed.dbname == "analytics")
    _assert("jdbc user", parsed.user == "readonly")
    _assert("jdbc sslmode", parsed.sslmode == "require")
    _assert("jdbc password extracted", parsed.password == _PW)


def test_parse_jdbc_query_params() -> None:
    parsed = parse_jdbc_url(f"jdbc:postgresql://db.example.com/analytics?user=svc&password={_PW}&sslmode=disable")
    _assert("query user", parsed.user == "svc")
    _assert("query password", parsed.password == _PW)
    _assert("query sslmode", parsed.sslmode == "disable")
    _assert("omitted port defaults to 5432", parsed.port == 5432)


def test_parse_failure_never_leaks_password() -> None:
    raised = ""
    try:
        # Wrong scheme, but the URL carries a password — the error must scrub it.
        parse_jdbc_url(f"mysql://readonly:{_PW}@db.example.com/analytics")
    except ValueError as exc:
        raised = str(exc)
    _assert("scheme error raised", raised != "")
    _assert("parse-failure error NEVER contains the password", _PW not in raised, raised)


def test_scrub_password_from_url() -> None:
    scrubbed = scrub_password_from_url(f"jdbc:postgresql://readonly:{_PW}@db.example.com:5433/analytics")
    _assert("scrub removes the password", _PW not in scrubbed, scrubbed)
    _assert("scrub keeps the host visible", "db.example.com" in scrubbed)
    scrubbed_q = scrub_password_from_url(f"postgresql://db/analytics?user=u&password={_PW}")
    _assert("scrub removes query password", _PW not in scrubbed_q, scrubbed_q)


def test_parsed_registration_repr_redacts() -> None:
    parsed = parse_jdbc_url(f"postgresql://u:{_PW}@h:5432/db")
    _assert("parsed repr hides the password", _PW not in repr(parsed), repr(parsed))


def test_list_connection_names() -> None:
    addresses = [
        {"name": "external_pg::analytics"},
        {"name": "external_pg::reporting"},
        {"name": "foo_external_pg::decoy"},   # substring hit — must be dropped
        {"name": "google_oauth_app"},          # unrelated
    ]
    loader = AppConfigLoader(_FakeAddressBook(addresses=addresses))
    names, truncated = loader.list_connection_names()
    _assert("only real prefix matches kept, sorted", names == ["analytics", "reporting"], str(names))
    _assert("substring decoy dropped", "decoy" not in names)
    _assert("not truncated for a small set", truncated is False)


def main() -> int:
    print("\nexternal_postgres_plugin config smoke tests")
    print("=" * 44)
    test_resolve_builds_dsn()
    test_resolve_port_defaults()
    test_resolve_unknown()
    test_resolve_incomplete()
    test_dsn_repr_redacts_password()
    test_parse_jdbc_netloc()
    test_parse_jdbc_query_params()
    test_parse_failure_never_leaks_password()
    test_scrub_password_from_url()
    test_parsed_registration_repr_redacts()
    test_list_connection_names()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All config smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
