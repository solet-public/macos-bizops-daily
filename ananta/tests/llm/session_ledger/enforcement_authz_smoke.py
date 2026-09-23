#!/usr/bin/env python3
"""Smoke for the session-ledger registration authz gate (P1.1.E; no pytest).

Covers ``enforcement.assert_register_source_authorized`` + the
``assert_operator_principal`` / ``is_filesystem_root_uri`` helpers — the
security gate that guards the public ``register_source`` verb once the pulling
plugins honor a per-source ``root_uri``. Pure (no DB).

Asserted:
  * operator-principal required for a filesystem root_uri; None / non-operator
    is denied.
  * containment: a realpath-contained path under an allowed root is admitted; a
    path outside every allowed root is denied (commonpath, not string-prefix —
    defeats ``..`` traversal + ``/allowed-evil`` sibling-prefix tricks).
  * empty allowed_roots = deny every filesystem registration (secure default).
  * non-filesystem sentinels (pushed:* / local:* / blob ids) are admitted
    unconditionally (no operator, no containment).
  * a malformed ``file://`` authority is DENIED, not silently admitted as a
    sentinel (the gate-self-containment fix).

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/enforcement_authz_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.services.session_ledger_service.enforcement import (  # noqa: E402
    LedgerAuthorizationError,
    assert_operator_principal,
    assert_register_source_authorized,
    is_filesystem_root_uri,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _Ctx:
    """Duck-typed CallContext stub (the gate reads only is_operator_principal)."""

    def __init__(self, is_operator: bool) -> None:
        self.is_operator_principal = is_operator


def _denied(root_uri: str, ctx: Any, allowed: list[str]) -> bool:
    try:
        assert_register_source_authorized(
            root_uri=root_uri, call_context=ctx, allowed_roots=allowed,
        )
    except LedgerAuthorizationError:
        return True
    return False


def test_is_filesystem_root_uri_classification() -> None:
    for fs in ("file:///abs/x", "/abs/x", "~/x", "file://evil.com/x"):
        _check(is_filesystem_root_uri(fs), f"{fs!r} classified filesystem-intended")
    for sentinel in ("pushed:codex_pushed", "local:agent_messaging", "bmd-0001",
                     "session-ledger-export-sha256-deadbeef"):
        _check(
            not is_filesystem_root_uri(sentinel),
            f"{sentinel!r} classified non-filesystem (admitted)",
        )


def test_operator_principal_required_for_filesystem() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        allowed = [tmp]
        contained = f"{tmp}/sub"
        _check(
            _denied(contained, None, allowed),
            "filesystem root + None context is denied",
        )
        _check(
            _denied(contained, _Ctx(is_operator=False), allowed),
            "filesystem root + non-operator context is denied",
        )
        _check(
            not _denied(contained, _Ctx(is_operator=True), allowed),
            "filesystem root + operator + contained is admitted",
        )


def test_containment_and_deny_all_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        op = _Ctx(is_operator=True)
        _check(
            _denied("/etc/passwd", op, [tmp]),
            "operator + path outside every allowed root is denied",
        )
        _check(
            _denied(f"{tmp}/sub", op, []),
            "empty allowed_roots denies every filesystem registration (secure default)",
        )
        _check(
            _denied(f"{tmp}-sibling/x", op, [tmp]),
            "sibling-prefix path (commonpath, not string-prefix) is denied",
        )


def test_sentinels_admitted_unconditionally() -> None:
    for sentinel in ("pushed:codex_pushed", "local:agent_messaging", "bmd-0001"):
        _check(
            not _denied(sentinel, None, []),
            f"sentinel {sentinel!r} admitted with no operator + no allowed roots",
        )


def test_malformed_file_uri_is_denied() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        op = _Ctx(is_operator=True)
        _check(
            _denied("file://evil.com/x", op, [tmp]),
            "malformed file:// authority is DENIED, not admitted as a sentinel (NIT1)",
        )


def test_assert_operator_principal() -> None:
    try:
        assert_operator_principal(None, "m")
        _check(False, "None context should raise")
    except LedgerAuthorizationError:
        _check(True, "assert_operator_principal denies None context")
    try:
        assert_operator_principal(_Ctx(is_operator=False), "m")  # type: ignore[arg-type]
        _check(False, "non-operator should raise")
    except LedgerAuthorizationError:
        _check(True, "assert_operator_principal denies non-operator")
    try:
        assert_operator_principal(_Ctx(is_operator=True), "m")  # type: ignore[arg-type]
        _check(True, "assert_operator_principal admits operator")
    except LedgerAuthorizationError:
        _check(False, "operator should be admitted")


def main() -> int:
    print("=== enforcement_authz_smoke (P1.1.E gate) ===")
    test_is_filesystem_root_uri_classification()
    test_operator_principal_required_for_filesystem()
    test_containment_and_deny_all_default()
    test_sentinels_admitted_unconditionally()
    test_malformed_file_uri_is_denied()
    test_assert_operator_principal()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
