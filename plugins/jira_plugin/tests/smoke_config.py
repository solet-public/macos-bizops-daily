#!/usr/bin/env python3
"""JIR-A config/client/expiry smoke tests for jira_plugin (no pytest, no live Jira).

Hermetic — a faked address_book_service, a faked JIRA constructor. No network,
no credentials. Red-first: each check asserts real parsing / classification
behavior, so a regression fails here.

Exercises:
  1.  AppConfigLoader — resolve_with_secrets entries -> JiraAppConfig fields
  2.  AppConfigLoader — missing entry -> address_book_entry_missing (register example)
  3.  AppConfigLoader — incomplete entry -> address_book_entry_incomplete
  4.  AppConfigLoader — malformed expires_at -> expires_at_invalid (fail at load)
  5.  AppConfigLoader — naive expires_at coerced to aware UTC
  6.  JiraClientFactory — builds JIRA(server, basic_auth, options rest_api_version=2)
  7.  JiraClientFactory — logs a loud jira.token_expiring warning at build for a near-expiry token
  8.  check_token_expiry — within-N-days -> ExpiryWarning(code, days_remaining)
  9.  check_token_expiry — well-future -> None
  10. check_token_expiry — already expired -> ExpiryWarning with negative days_remaining

(The former deny-effectiveness checks 11-13 were removed 2026-07-15 —
process_export_deny_patterns is empty by operator ruling; see
workbench/2026-07-15_result_error_processing_architecture_deep_dive.md.)

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/jira_plugin/tests/smoke_config.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "jira_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import jira_plugin.client as client_module  # noqa: E402
from jira_plugin.app_config import AppConfigError, AppConfigLoader  # noqa: E402
from jira_plugin.client import JiraClientFactory, check_token_expiry  # noqa: E402
from jira_plugin.constants import (  # noqa: E402
    ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE,
    ERROR_ADDRESS_BOOK_ENTRY_MISSING,
    ERROR_EXPIRES_AT_INVALID,
    ERROR_TOKEN_EXPIRING,
)

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


def _entry(field_type: str, value: str) -> dict[str, str]:
    return {"field_type": field_type, "value": value}


def _full_entries(expires_at: str = "2099-01-01T00:00:00Z") -> list[dict[str, str]]:
    return [
        _entry("base_url", "https://acme.atlassian.net"),
        _entry("email", "svc-example@acme.example"),
        _entry("api_token", "tok-secret-123"),
        _entry("expires_at", expires_at),
        _entry("scope_note", "read/write project EXAMPLE"),
    ]


def _fake_address_book(
    entries: list[dict[str, str]] | None,
    status: str = "completed",
) -> MagicMock:
    ab = MagicMock()
    data: Any = {"entries": entries} if entries is not None else {}
    ab.resolve_with_secrets.return_value = {"action_status": status, "data": data}
    return ab


# ---------------------------------------------------------------------------
# 1-5: AppConfigLoader
# ---------------------------------------------------------------------------


def test_config_resolves_fields() -> None:
    loader = AppConfigLoader(_fake_address_book(_full_entries()))
    config = loader.load()
    _assert("base_url resolved", config.base_url == "https://acme.atlassian.net")
    _assert("email resolved", config.email == "svc-example@acme.example")
    _assert("api_token resolved (vault-swapped literal)", config.api_token == "tok-secret-123")
    _assert("scope_note resolved", config.scope_note == "read/write project EXAMPLE")
    _assert("expires_at is aware datetime", config.expires_at.tzinfo is not None)


def test_config_missing_entry() -> None:
    loader = AppConfigLoader(_fake_address_book(None, status="error"))
    code = ""
    message = ""
    try:
        loader.load()
    except AppConfigError as exc:
        code = exc.code
        message = str(exc)
    _assert("missing entry -> entry_missing code", code == ERROR_ADDRESS_BOOK_ENTRY_MISSING)
    _assert("missing entry message carries a register example", "address_book_service::register" in message)


def test_config_incomplete_entry() -> None:
    entries = [_entry("base_url", "https://acme.atlassian.net")]  # no email/token/expires_at
    loader = AppConfigLoader(_fake_address_book(entries))
    code = ""
    try:
        loader.load()
    except AppConfigError as exc:
        code = exc.code
    _assert("incomplete entry -> entry_incomplete code", code == ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE)


def test_config_malformed_expires_at() -> None:
    entries = _full_entries(expires_at="not-a-date")
    loader = AppConfigLoader(_fake_address_book(entries))
    code = ""
    try:
        loader.load()
    except AppConfigError as exc:
        code = exc.code
    _assert("malformed expires_at fails at LOAD -> expires_at_invalid", code == ERROR_EXPIRES_AT_INVALID)


def test_config_naive_expires_at_coerced() -> None:
    entries = _full_entries(expires_at="2099-01-01T00:00:00")  # no tz suffix
    config = AppConfigLoader(_fake_address_book(entries)).load()
    _assert("naive expires_at coerced to aware UTC", config.expires_at.tzinfo is UTC)


# ---------------------------------------------------------------------------
# 6-7: JiraClientFactory
# ---------------------------------------------------------------------------


def test_factory_builds_client_kwargs() -> None:
    captured: dict[str, Any] = {}

    class _FakeJIRA:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    original = client_module.JIRA
    client_module.JIRA = _FakeJIRA  # type: ignore[misc, assignment]
    try:
        loader = AppConfigLoader(_fake_address_book(_full_entries()))
        factory = JiraClientFactory(
            loader, logging.getLogger("jira_smoke"), warn_days=14, request_timeout=30.0
        )
        factory.client()
    finally:
        client_module.JIRA = original  # type: ignore[misc]
    _assert("server is the base_url", captured.get("server") == "https://acme.atlassian.net")
    _assert(
        "basic_auth is (email, token)",
        captured.get("basic_auth") == ("svc-example@acme.example", "tok-secret-123"),
    )
    _assert(
        "rest_api_version pinned to 2",
        (captured.get("options") or {}).get("rest_api_version") == "2",
    )


def test_factory_logs_expiry_warning_at_build() -> None:
    captured: dict[str, Any] = {}

    class _FakeJIRA:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    warnings: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warnings.append(record.getMessage())

    near = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    logger = logging.getLogger("jira_smoke_expiry")
    logger.setLevel(logging.WARNING)
    logger.addHandler(_CapturingHandler())

    original = client_module.JIRA
    client_module.JIRA = _FakeJIRA  # type: ignore[misc, assignment]
    try:
        loader = AppConfigLoader(_fake_address_book(_full_entries(expires_at=near)))
        JiraClientFactory(loader, logger, warn_days=14, request_timeout=30.0).client()
    finally:
        client_module.JIRA = original  # type: ignore[misc]
    _assert(
        "near-expiry token logs jira.token_expiring at build",
        any(ERROR_TOKEN_EXPIRING in w for w in warnings),
        f"warnings={warnings}",
    )


# ---------------------------------------------------------------------------
# 8-10: check_token_expiry (pure)
# ---------------------------------------------------------------------------


def test_expiry_within_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expires = datetime(2026, 1, 6, tzinfo=UTC)  # 5 days out
    warning = check_token_expiry(expires, now, warn_days=14)
    _assert("within-window returns a warning", warning is not None)
    if warning is not None:
        _assert("warning code is jira.token_expiring", warning.code == ERROR_TOKEN_EXPIRING)
        _assert("days_remaining == 5", warning.days_remaining == 5)
        _assert("warning message omits any host", "atlassian.net" not in warning.message)


def test_expiry_well_future_is_none() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expires = datetime(2026, 4, 1, tzinfo=UTC)  # ~90 days out
    _assert("well-future returns None", check_token_expiry(expires, now, warn_days=14) is None)


def test_expiry_already_expired() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expires = datetime(2025, 12, 29, tzinfo=UTC)  # 3 days ago
    warning = check_token_expiry(expires, now, warn_days=14)
    _assert("expired returns a warning", warning is not None)
    if warning is not None:
        _assert("expired days_remaining is negative", warning.days_remaining == -3)
        _assert("expired message says EXPIRED", "EXPIRED" in warning.message)


def main() -> int:
    print("\njira_plugin JIR-A config/client/expiry smoke tests")
    print("=" * 40)
    test_config_resolves_fields()
    test_config_missing_entry()
    test_config_incomplete_entry()
    test_config_malformed_expires_at()
    test_config_naive_expires_at_coerced()
    test_factory_builds_client_kwargs()
    test_factory_logs_expiry_warning_at_build()
    test_expiry_within_window()
    test_expiry_well_future_is_none()
    test_expiry_already_expired()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All JIR-A config smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
