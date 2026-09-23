#!/usr/bin/env python3
"""Facade + bootstrap reachability smoke for the ``query_ordered`` primitive (no pytest).

v10 Step 1: the one operator-approved ``StateManagementInterface`` widening
(an ordered/bounded/tie-safe query) must land across the WHOLE state facade
or the bridge call can't reach the plugin. This smoke proves it without a DB:

  * ``INTERFACE_VERSION`` bumped 2.0.0 → 2.1.0.
  * both state plugins are CONCRETE — the new ``@abstractmethod`` did not
    leave ``PostgresStatePlugin`` / ``RdsPostgresStateManagementPlugin``
    abstract (load-time ABC validation would otherwise fail the platform).
  * ``BootstrapDatabaseStorage.query_ordered`` is a REAL in-memory impl
    (the v9-missed node): equality filter + ``is_deleted`` default,
    composite oldest/recent ordering, a tie-safe ``(created_at, id)``
    ``after`` cursor (no skip/dup across an equal-``created_at`` page
    boundary), and ``limit``.
  * ``DatabaseOperationService`` and ``PluginDatabaseStorage`` both delegate
    ``query_ordered`` (facade reachability through the chain).

The SQL ``provider.select_ordered`` path is exercised by the postgres/rds
plugin live-DB smokes (Step 7); here the in-memory path stands in for the
ordering/cursor semantics, which are shared via ``ordered_query``.

Run:
    .venv/bin/python3 ananta/tests/services/state_service/query_ordered_facade_smoke.py
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.services.database_operations.bootstrap_database_storage import (  # noqa: E402
    BootstrapDatabaseStorage,
)
from ananta.services.database_operations.database_operation_service import (  # noqa: E402
    DatabaseOperationService,
)
from ananta.services.database_operations.plugin_database_storage import (  # noqa: E402
    PluginDatabaseStorage,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    OrderedQueryError,
    parse_ordered_query,
)

_ENTRY_POINT_GROUP = "ananta.plugins"
_NS = "agent_messaging"
_TABLE = "agent_role_message"

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


def _rows() -> list[dict[str, object]]:
    # Two rows share an identical created_at (09:00) across the page boundary;
    # one row is soft-deleted; ids are the tie-break.
    return [
        {"id": "agm-3", "created_at": "2026-06-19T10:00:00", "is_deleted": 0,
         "recipient_key": "Architect"},
        {"id": "agm-1", "created_at": "2026-06-19T09:00:00", "is_deleted": 0,
         "recipient_key": "Architect"},
        {"id": "agm-2", "created_at": "2026-06-19T09:00:00", "is_deleted": 0,
         "recipient_key": "Architect"},
        {"id": "agm-x", "created_at": "2026-06-19T08:00:00", "is_deleted": 1,
         "recipient_key": "Architect"},
    ]


def _seed_bootstrap() -> BootstrapDatabaseStorage:
    store = BootstrapDatabaseStorage()
    store._memory_data[_NS][_TABLE] = list(_rows())  # type: ignore[index]  # noqa: SLF001
    return store


def _ids(result: Any) -> list[str]:
    records = result["data"]["records"]
    return [str(r["id"]) for r in records]


_ASC = [("created_at", "asc"), ("id", "asc")]
_DESC = [("created_at", "desc"), ("id", "desc")]


def test_interface_version_bumped() -> None:
    _check(
        StateManagementInterface.INTERFACE_VERSION == "2.1.0",
        "StateManagementInterface.INTERFACE_VERSION bumped to 2.1.0",
    )


def _installed_state_plugins() -> dict[str, type[StateManagementInterface]]:
    """Every INSTALLED ``ananta.plugins`` entry point whose class implements
    ``StateManagementInterface``.

    Discovered, never hardcoded. Naming the implementers by import binds this smoke to
    one tree: ``rds_postgres_state_management_plugin`` is an AWS plugin absent from
    local seed bundles, so the hard import failed there — and a fixed pair also misses
    any implementer added later, which is precisely the class of plugin a new
    ``@abstractmethod`` would strand.
    """
    found: dict[str, type[StateManagementInterface]] = {}
    for ep in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP):
        try:
            loaded = ep.load()
        except Exception:  # noqa: BLE001, S112 — entry point registered but not installed here
            continue
        if isinstance(loaded, type) and issubclass(loaded, StateManagementInterface):
            found[ep.name] = loaded
    return found


def test_plugins_are_concrete() -> None:
    """The new @abstractmethod must not leave any installed implementer abstract."""
    plugins = _installed_state_plugins()
    _check(
        bool(plugins),
        "at least one installed plugin implements StateManagementInterface "
        f"(entry-point group {_ENTRY_POINT_GROUP!r})",
    )
    print(f"  discovered implementers: {sorted(plugins)}")

    for cls in plugins.values():
        leftover = getattr(cls, "__abstractmethods__", frozenset())
        _check(
            not leftover,
            f"{cls.__name__} is concrete (no abstract methods left: {set(leftover)})",
        )
        _check(
            callable(getattr(cls, "query_ordered", None)),
            f"{cls.__name__} exposes query_ordered",
        )


def test_bootstrap_orders_and_filters() -> None:
    store = _seed_bootstrap()
    asc = store.query_ordered(
        _NS, {"table": _TABLE, "filters": {}, "order_by": _ASC, "limit": 10},
    )
    _check(
        _ids(asc) == ["agm-1", "agm-2", "agm-3"],
        "bootstrap asc oldest-first + is_deleted filtered (agm-x excluded)",
    )
    desc = store.query_ordered(
        _NS, {"table": _TABLE, "filters": {}, "order_by": _DESC, "limit": 10},
    )
    _check(
        _ids(desc) == ["agm-3", "agm-2", "agm-1"],
        "bootstrap desc recent-first",
    )


def test_bootstrap_tie_safe_cursor() -> None:
    store = _seed_bootstrap()
    # after the FIRST of the two equal-created_at rows: must return the second
    # (same created_at, larger id) then the later row — no skip, no dup.
    page = store.query_ordered(
        _NS,
        {"table": _TABLE, "filters": {}, "order_by": _ASC, "limit": 10,
         "after": ["2026-06-19T09:00:00", "agm-1"]},
    )
    _check(
        _ids(page) == ["agm-2", "agm-3"],
        "bootstrap tie-safe after-cursor: equal-created_at boundary, no skip/dup",
    )


def test_bootstrap_limit_and_filters() -> None:
    store = _seed_bootstrap()
    limited = store.query_ordered(
        _NS, {"table": _TABLE, "filters": {}, "order_by": _ASC, "limit": 2},
    )
    _check(_ids(limited) == ["agm-1", "agm-2"], "bootstrap limit caps the page")

    none = store.query_ordered(
        _NS,
        {"table": _TABLE, "filters": {"recipient_key": "Nobody"},
         "order_by": _ASC, "limit": 10},
    )
    _check(_ids(none) == [], "bootstrap equality filter excludes non-matching rows")

    with_deleted = store.query_ordered(
        _NS,
        {"table": _TABLE, "filters": {}, "order_by": _ASC, "limit": 10,
         "include_deleted": True},
    )
    _check(
        "agm-x" in _ids(with_deleted),
        "bootstrap include_deleted=True surfaces soft-deleted rows",
    )


def test_bootstrap_missing_table_empty() -> None:
    store = BootstrapDatabaseStorage()
    result = store.query_ordered(
        _NS, {"table": _TABLE, "filters": {}, "order_by": _ASC, "limit": 10},
    )
    _check(
        _ids(result) == [],
        "query_ordered against a never-written table returns an empty page",
    )


def test_operation_service_delegates() -> None:
    """Facade reachability: DatabaseOperationService -> storage strategy."""
    service = DatabaseOperationService(_seed_bootstrap())
    result = service.query_ordered(
        _NS, {"table": _TABLE, "filters": {}, "order_by": _ASC, "limit": 10},
    )
    _check(
        _ids(result) == ["agm-1", "agm-2", "agm-3"],
        "DatabaseOperationService.query_ordered reaches the storage strategy",
    )


class _RecordingPlugin:
    """Duck-typed StateManagementInterface stub recording the delegated call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        self.calls.append((namespace, data))
        return {"action_status": "completed", "data": {"records": [{"id": "sentinel"}]}}


def test_plugin_storage_delegates() -> None:
    plugin = _RecordingPlugin()
    storage = PluginDatabaseStorage(plugin)  # type: ignore[arg-type]
    payload: dict[str, object] = {
        "table": _TABLE, "filters": {}, "order_by": _ASC, "limit": 5,
    }
    result = storage.query_ordered(_NS, payload)
    _check(
        plugin.calls == [(_NS, payload)],
        "PluginDatabaseStorage.query_ordered delegates verbatim to the plugin",
    )
    _check(
        _ids(result) == ["sentinel"],
        "PluginDatabaseStorage returns the plugin's result unchanged",
    )


def test_bootstrap_numeric_ordering() -> None:
    """REGRESSION (Codex MAJOR 2026-06-21): integer-ordered columns must order
    by VALUE, not lexically (`10 > 9`, not `'10' < '9'`). Pre-fix the in-memory
    comparator str()-normalized EVERY ordered value, so a multi-digit `cursor`
    page silently lost rows at the 9→10 digit boundary on this exact live
    BootstrapDatabaseStorage path (agent_messaging list_messages, R4). Ids
    correlate to cursors so `_ids` reflects the numeric order."""
    store = BootstrapDatabaseStorage()
    store._memory_data[_NS]["introws"] = [  # type: ignore[index]  # noqa: SLF001
        {"id": "r2", "cursor": 2, "is_deleted": 0},
        {"id": "r10", "cursor": 10, "is_deleted": 0},
        {"id": "r9", "cursor": 9, "is_deleted": 0},
        {"id": "r11", "cursor": 11, "is_deleted": 0},
    ]
    order = [("cursor", "asc"), ("id", "asc")]
    asc = store.query_ordered(
        _NS, {"table": "introws", "filters": {}, "order_by": order, "limit": 10},
    )
    _check(
        _ids(asc) == ["r2", "r9", "r10", "r11"],
        f"bootstrap numeric asc orders by cursor VALUE, not lexically (the "
        f"2-vs-10 case; pre-fix str-compare gave [r10,r11,r2,r9]) (got={_ids(asc)})",
    )
    page = store.query_ordered(
        _NS,
        {
            "table": "introws",
            "filters": {},
            "order_by": order,
            "after": [9, "￿"],
            "limit": 10,
        },
    )
    _check(
        _ids(page) == ["r10", "r11"],
        f"bootstrap after=[9,…] returns cursor>9 across the 9→10 DIGIT boundary "
        f"(pre-fix str-compare gave [] since '10'<'9') (got={_ids(page)})",
    )


def test_cap_fail_loud_and_unbounded() -> None:
    """Gap-C: over-cap is REFUSED unless unbounded=True; under-cap unchanged.

    The pre-Gap-C ``min(limit, _MAX_ORDERED_LIMIT)`` silently truncated an
    over-cap request (e.g. a 1..500 verb quietly cut to 100). Now it fails
    loud, and a caller opts into a larger page with ``unbounded=True``.
    """
    base: dict[str, object] = {"table": _TABLE, "order_by": _ASC}
    spec = parse_ordered_query({**base, "limit": 50})
    _check(spec.limit == 50, f"limit 50 (<=cap) used as-is (got {spec.limit})")
    spec = parse_ordered_query({**base, "limit": 100})
    _check(spec.limit == 100, f"limit 100 (==cap) used as-is (got {spec.limit})")
    try:
        parse_ordered_query({**base, "limit": 500})
    except OrderedQueryError:
        _check(True, "limit 500 (>cap) without unbounded raises (no silent clamp)")
    else:
        _check(False, "limit 500 (>cap) did NOT raise — silent-truncation regression")
    spec = parse_ordered_query({**base, "limit": 500, "unbounded": True})
    _check(spec.limit == 500, f"limit 500 + unbounded=True preserved (got {spec.limit})")
    spec = parse_ordered_query({**base, "limit": 0})
    _check(spec.limit == 1, f"limit 0 floors to 1 (got {spec.limit})")


def test_inmemory_comparison_ops() -> None:
    """Gap-A: the in-memory matcher mirrors the SQL comparison grammar.

    Also locks the pre-existing parity gap closed alongside Gap-A — the old
    equality-only ``_matches_filters`` silently mis-matched is_null / list /
    comparison op-dicts (a dict never ``==`` a column value).
    """
    store = _seed_bootstrap()
    gte = store.query_ordered(
        _NS, {"table": _TABLE, "order_by": _ASC, "limit": 10,
              "filters": {"created_at": {"op": "gte", "value": "2026-06-19T09:00:00"}}},
    )
    _check(_ids(gte) == ["agm-1", "agm-2", "agm-3"], f"in-mem created_at>=09:00 (got {_ids(gte)})")
    gt = store.query_ordered(
        _NS, {"table": _TABLE, "order_by": _ASC, "limit": 10,
              "filters": {"created_at": {"op": "gt", "value": "2026-06-19T09:00:00"}}},
    )
    _check(_ids(gt) == ["agm-3"], f"in-mem created_at>09:00 -> agm-3 (got {_ids(gt)})")
    lt = store.query_ordered(
        _NS, {"table": _TABLE, "order_by": _ASC, "limit": 10,
              "filters": {"created_at": {"op": "lt", "value": "2026-06-19T10:00:00"}}},
    )
    _check(_ids(lt) == ["agm-1", "agm-2"], f"in-mem created_at<10:00 -> agm-1,agm-2 (got {_ids(lt)})")

    # Numeric comparison orders by VALUE (10 > 9, not '10' < '9').
    store._memory_data[_NS]["cmprows"] = [  # type: ignore[index]  # noqa: SLF001
        {"id": "r2", "cursor": 2, "is_deleted": 0},
        {"id": "r10", "cursor": 10, "is_deleted": 0},
        {"id": "r9", "cursor": 9, "is_deleted": 0},
    ]
    order = [("cursor", "asc"), ("id", "asc")]
    numeric = store.query_ordered(
        _NS, {"table": "cmprows", "order_by": order, "limit": 10,
              "filters": {"cursor": {"op": "gt", "value": 9}}},
    )
    _check(_ids(numeric) == ["r10"], f"in-mem cursor>9 numeric (not '10'<'9') -> r10 (got {_ids(numeric)})")

    # is_null / list parity (the gap the equality-only matcher could not do).
    store._memory_data[_NS]["nulrows"] = [  # type: ignore[index]  # noqa: SLF001
        {"id": "n1", "tag": None, "is_deleted": 0, "ord": 1},
        {"id": "n2", "tag": "x", "is_deleted": 0, "ord": 2},
    ]
    nord = [("ord", "asc"), ("id", "asc")]
    isnull = store.query_ordered(
        _NS, {"table": "nulrows", "order_by": nord, "limit": 10,
              "filters": {"tag": {"op": "is_null"}}},
    )
    _check(_ids(isnull) == ["n1"], f"in-mem tag IS NULL -> n1 (got {_ids(isnull)})")
    listmatch = store.query_ordered(
        _NS, {"table": "nulrows", "order_by": nord, "limit": 10, "filters": {"id": ["n2"]}},
    )
    _check(_ids(listmatch) == ["n2"], f"in-mem id = ANY [n2] -> n2 (got {_ids(listmatch)})")

    # Unknown op + missing value fail loud (parity with the providers' raise).
    try:
        store.query_ordered(
            _NS, {"table": _TABLE, "order_by": _ASC, "limit": 10,
                  "filters": {"created_at": {"op": "like", "value": "x"}}},
        )
    except OrderedQueryError:
        _check(True, "in-mem unknown op 'like' raises OrderedQueryError")
    else:
        _check(False, "in-mem unknown op did NOT raise")
    try:
        store.query_ordered(
            _NS, {"table": _TABLE, "order_by": _ASC, "limit": 10,
                  "filters": {"created_at": {"op": "gt"}}},
        )
    except OrderedQueryError:
        _check(True, "in-mem comparison op missing 'value' raises OrderedQueryError")
    else:
        _check(False, "in-mem missing 'value' did NOT raise")


def main() -> int:
    print("=== query_ordered facade + bootstrap reachability smoke (Step 1) ===")
    test_interface_version_bumped()
    test_plugins_are_concrete()
    test_bootstrap_orders_and_filters()
    test_bootstrap_tie_safe_cursor()
    test_bootstrap_numeric_ordering()
    test_bootstrap_limit_and_filters()
    test_bootstrap_missing_table_empty()
    test_cap_fail_loud_and_unbounded()
    test_inmemory_comparison_ops()
    test_operation_service_delegates()
    test_plugin_storage_delegates()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
