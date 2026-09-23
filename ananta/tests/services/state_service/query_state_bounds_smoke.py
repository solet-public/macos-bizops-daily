#!/usr/bin/env python3
"""Smoke: the ``query_state`` row bound, at every implementation (no pytest).

Lane B of the 2026-08-15 unbounded-state-read programme. ``query_state`` had
FOUR implementations that did not agree about the ``MAX_READ_ROWS`` bound:

* postgres / rds plugin (autocommit) — ``return self.read_state(...)``, bounded,
  and the ``limit`` slot in the query envelope ALREADY worked. The programme's
  census recorded 70 call sites as "structurally unable to bound themselves";
  that premise was false, and §1 below is the regression pin for it.
* ``BootstrapDatabaseStorage.query_state`` — its own copy of the body, which
  skipped the cap that its sibling ``read_state`` applies to the same rows off
  the same in-memory store. Now delegates (§2).
* ``_PostgresStateTransaction.query_state`` / ``_RdsStateTransaction`` — no
  bound at all, and no ``read_state`` on ``StateTransaction`` to migrate to, so
  it carries the bound itself (§3, §4).

The load-bearing property is NEVER TRUNCATE: an over-cap unbounded read must
RAISE, never return a prefix. A truncated read corrupts every analysis built on
it, where a loud refusal stops one caller (``read_bounds`` module docstring).

PURE UNIT: real implementations against recording fakes. No DB, no platform.

Run:
    .venv/bin/python3 ananta/tests/services/state_service/query_state_bounds_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# parents[4], not [3]: this file sits four directories below the repo root
# (ananta/tests/services/state_service/). The sibling smokes in this directory
# use parents[3], which resolves to <repo>/ananta and makes their sys.path entry
# point at a path that does not exist — inert rather than broken only because
# ananta is installed into the venv as an editable package, so the import
# succeeds via site-packages and the dead path is never noticed.
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src")
)

from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.database_operations.bootstrap_database_storage import (  # noqa: E402
    BootstrapDatabaseStorage,
)
from ananta.services.state_service.read_bounds import (  # noqa: E402
    MAX_READ_ROWS,
    ReadBoundError,
)
from postgres_state_management_plugin.plugin import (  # noqa: E402
    PostgresStatePlugin,
    _PostgresStateTransaction,
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


class _RecordingProvider:
    """Captures what the autocommit read path asked the database for."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._rows = rows if rows is not None else [{"id": 1}]

    def select(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self._rows


class _RecordingTxnProvider:
    """Captures the composed SELECT for the transaction read path."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rows = rows if rows is not None else [{"id": 1}]

    def build_select_sql(
        self,
        namespace: str,
        table: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
        *,
        serialize: Any = None,
    ) -> tuple[str, list[Any]]:
        self.calls.append(
            {
                "namespace": namespace,
                "table": table,
                "conditions": conditions,
                "limit": limit,
            }
        )
        return ("SELECT *", [])


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, composed: object, params: object) -> None:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


def _autocommit_plugin(provider: _RecordingProvider) -> PostgresStatePlugin:
    plugin = PostgresStatePlugin.__new__(PostgresStatePlugin)
    plugin._get_provider = lambda: provider  # type: ignore[method-assign]
    return plugin


def _txn(rows: list[dict[str, Any]]) -> tuple[
    _PostgresStateTransaction, _RecordingTxnProvider
]:
    provider = _RecordingTxnProvider(rows)
    return (
        _PostgresStateTransaction(_FakeConn(rows), provider),  # type: ignore[arg-type]
        provider,
    )


# ---------------------------------------------------------------------------
# §1 — the autocommit alias: the limit slot exists and the bound applies.
#      Regression pin for the census's false "no limit slot" premise.
# ---------------------------------------------------------------------------
def section_autocommit_alias() -> None:
    print("\n§1 autocommit query_state — the limit slot works")

    provider = _RecordingProvider()
    _autocommit_plugin(provider).query_state(
        "core", {"table": "sessions", "filters": {"a": 1}, "limit": 7}
    )
    _check(
        provider.calls and provider.calls[0]["limit"] == 7,
        "explicit limit reaches the SQL (query_state CAN express a bound)",
    )

    provider = _RecordingProvider()
    _autocommit_plugin(provider).query_state(
        "core", {"table": "sessions", "filters": {"a": 1}}
    )
    _check(
        provider.calls and provider.calls[0]["limit"] == MAX_READ_ROWS + 1,
        "no limit fetches cap+1 so overflow can be PROVED, not truncated",
    )

    provider = _RecordingProvider()
    result = _autocommit_plugin(provider).query_state(
        "core", {"table": "sessions", "filters": {"a": 1}, "limit": MAX_READ_ROWS + 1}
    )
    _check(result["action_status"] == "error", "over-cap limit is refused")
    _check(not provider.calls, "over-cap limit refused BEFORE any SQL ran")

    # Consent path still available — the guard must not be unescapable.
    provider = _RecordingProvider()
    _autocommit_plugin(provider).query_state(
        "core",
        {
            "table": "sessions",
            "filters": {"a": 1},
            "limit": MAX_READ_ROWS + 1,
            "unbounded": True,
        },
    )
    _check(
        provider.calls and provider.calls[0]["limit"] == MAX_READ_ROWS + 1,
        "unbounded=True opts into a larger scan",
    )


# ---------------------------------------------------------------------------
# §2 — bootstrap: query_state now inherits read_state's cap, and returns the
#      SAME records it always did (the delegation must not change results).
# ---------------------------------------------------------------------------
def section_bootstrap() -> None:
    print("\n§2 BootstrapDatabaseStorage.query_state — delegates, so it is bounded")

    memory: Any = defaultdict(lambda: defaultdict(list))
    memory["core"]["small"] = [{"id": i} for i in range(5)]
    storage = BootstrapDatabaseStorage(memory)

    q = storage.query_state("core", {"table": "small", "filters": {"id": 3}})
    r = storage.read_state("core", {"table": "small", "filters": {"id": 3}})
    _check(
        q["data"]["records"] == r["data"]["records"],
        "under the cap: query_state and read_state agree (no result change)",
    )
    _check(
        len(q["data"]["records"]) == 5,
        "bootstrap still does not filter — unchanged, deliberately out of scope",
    )

    memory["core"]["big"] = [{"id": i} for i in range(MAX_READ_ROWS + 1)]
    raised = False
    try:
        storage.query_state("core", {"table": "big", "filters": {}})
    except FrameworkError:
        raised = True
    _check(raised, "over-cap bootstrap query_state REFUSES (was: shipped it all)")

    rows = storage.query_state("core", {"table": "big", "filters": {}, "unbounded": True})
    _check(
        len(rows["data"]["records"]) == MAX_READ_ROWS + 1,
        "bootstrap unbounded=True still consents to the full scan",
    )


# ---------------------------------------------------------------------------
# §3 — transaction: the bound is applied, and applied BEFORE the SQL runs.
# ---------------------------------------------------------------------------
def section_transaction_limit() -> None:
    print("\n§3 _PostgresStateTransaction.query_state — bounded like read_state")

    txn, provider = _txn([{"id": 1}])
    txn.query_state("core", {"table": "sessions", "filters": {"id": "x"}, "limit": 5})
    _check(
        provider.calls and provider.calls[0]["limit"] == 5,
        "explicit limit compiles into the txn SELECT",
    )

    txn, provider = _txn([{"id": 1}])
    txn.query_state("core", {"table": "sessions", "filters": {"id": "x"}})
    _check(
        provider.calls and provider.calls[0]["limit"] == MAX_READ_ROWS + 1,
        "no limit fetches cap+1 (was: no LIMIT clause at all)",
    )

    txn, provider = _txn([{"id": 1}])
    raised = False
    try:
        txn.query_state(
            "core",
            {"table": "sessions", "filters": {"id": "x"}, "limit": MAX_READ_ROWS + 1},
        )
    except ReadBoundError:
        raised = True
    _check(raised, "over-cap txn limit raises ReadBoundError")
    _check(not provider.calls, "over-cap txn limit refused BEFORE any SQL ran")


# ---------------------------------------------------------------------------
# §4 — the load-bearing one: overflow RAISES rather than returning a prefix.
# ---------------------------------------------------------------------------
def section_transaction_never_truncates() -> None:
    print("\n§4 txn overflow — refuses, never returns a silent prefix")

    over = [{"id": i} for i in range(MAX_READ_ROWS + 1)]
    txn, _ = _txn(over)
    raised = False
    returned: list[dict[str, Any]] | None = None
    try:
        returned = txn.query_state("core", {"table": "sessions", "filters": {}})
    except ReadBoundError:
        raised = True
    _check(raised, "cap+1 rows back from an unbounded txn read RAISES")
    _check(
        returned is None,
        "and returns nothing at all — a prefix would corrupt the caller",
    )

    at_cap = [{"id": i} for i in range(MAX_READ_ROWS)]
    txn, _ = _txn(at_cap)
    rows = txn.query_state("core", {"table": "sessions", "filters": {}})
    _check(
        len(rows) == MAX_READ_ROWS,
        "exactly at the cap is a complete, exact result (not an off-by-one refusal)",
    )

    txn, _ = _txn(over)
    rows = txn.query_state(
        "core", {"table": "sessions", "filters": {}, "unbounded": True}
    )
    _check(
        len(rows) == MAX_READ_ROWS + 1,
        "txn unbounded=True consents to the full scan",
    )


def main() -> int:
    print("query_state row-bound smoke (lane B, 2026-08-15)")
    section_autocommit_alias()
    section_bootstrap()
    section_transaction_limit()
    section_transaction_never_truncates()

    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
