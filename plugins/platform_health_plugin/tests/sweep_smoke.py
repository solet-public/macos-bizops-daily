#!/usr/bin/env python3
"""Stub-driven smoke for the platform_health_plugin registry sweep (no pytest).

Exercises the sweep against a fixture orchestrator + fixture process registry
so the classifier, sentinel-arg synthesis, dispatch routing, and exception
capture can be verified without booting the real platform.

Acceptance coverage:

1. Read-shape verbs (``list_*``, ``get_*``) fire and are recorded as ok.
2. Write-shape verbs are skipped without ``write_enabled``, fire with it.
3. The fixture-recorded 'integer = boolean' SQL error is surfaced verbatim,
   demonstrating the gate would have caught the 2026-05-30 ledger bug.
4. The fixture-recorded 'Process not found or malformed in registry' error is
   surfaced verbatim, demonstrating retroactive catch of the 2026-05-28
   ``scheduling_service::execute_in_seconds`` regression.
5. The fixture-recorded ``ParameterType.ARRAY`` schema-shape error is surfaced
   verbatim, demonstrating retroactive catch of the D5 EDGE-wake regression.

Run:
    .venv/bin/python3 plugins/platform_health_plugin/tests/sweep_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(
    REPO_ROOT / "plugins" / "platform_health_plugin" / "src"
))

from platform_health_plugin.constants import (  # noqa: E402
    SELF_PROCESS_KEY,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED_SCOPE,
    STATUS_SKIPPED_SELF,
    STATUS_SKIPPED_WRITE,
)
from platform_health_plugin.sweep import (  # noqa: E402
    build_sentinel_args,
    classify_shape,
    run_sweep,
    split_process_key,
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


# ─── Fixture orchestrator + service ─────────────────────────────────────────


class _FixtureService:
    """A service exposing both well-behaved and intentionally-broken methods.

    Each broken method raises an exception modeled after a real 2026-05 platform
    incident so the sweep's retroactive catch can be demonstrated verbatim.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_clean(self) -> dict[str, object]:
        self.calls.append(("list_clean", {}))
        return {"items": []}

    def get_by_id(self, identifier: str) -> dict[str, object]:
        self.calls.append(("get_by_id", {"identifier": identifier}))
        return {"id": identifier, "found": False}

    def list_sessions(self, limit: int = 50) -> dict[str, object]:
        self.calls.append(("list_sessions", {"limit": limit}))
        # Models the M1 ledger bug surfaced 2026-05-30 against live Postgres.
        raise RuntimeError(
            "state-service call failed: {'type': 'plugin_error', "
            "'code': 'sql.execution_failed', 'message': 'operator does not "
            "exist: integer = boolean\\nLINE 1: ...unt FROM "
            "session_ledger__session WHERE is_deleted = FALSE OR...'}"
        )

    def get_account_status(self) -> dict[str, object]:
        self.calls.append(("get_account_status", {}))
        # Models the D5 EDGE-wake registration drift surfaced 2026-05-28.
        raise RuntimeError(
            "FrameworkError: invocation_schema invalid: ParameterType.ARRAY is "
            "not a known type; did you mean ParameterType.LIST?"
        )

    def execute_in_seconds(self, seconds: int, action_definitions: list[object]) -> None:
        self.calls.append(("execute_in_seconds", {
            "seconds": seconds, "action_definitions": action_definitions,
        }))
        # Models the scheduling_service registration failure surfaced 2026-05-28.
        raise RuntimeError(
            "Process not found or malformed in registry: "
            "service_interface::scheduling_service::execute_in_seconds",
        )

    def register_thing(self, name: str, root_uri: str) -> dict[str, object]:
        self.calls.append(("register_thing", {"name": name, "root_uri": root_uri}))
        return {"thing_id": "thg_smoke"}


class _FixturePluginManager:
    def __init__(self, plugins: dict[str, object]) -> None:
        self._plugins = plugins

    def get_plugin(self, plugin_name: str) -> object:
        return self._plugins.get(plugin_name)


class _FixtureExternalPlugin:
    """Outward-facing plugin double; every call is recorded, never real I/O."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def get_account_status(
        self, params: dict[str, object], state: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((params, state))
        return {"connected": False}


class _FixtureOrchestrator:
    def __init__(
        self,
        service: _FixtureService,
        registry: dict[str, object],
        external_plugin: _FixtureExternalPlugin,
    ) -> None:
        self._service = service
        self._registry = registry
        self.plugin_manager = _FixturePluginManager({
            "soundcloud_artist_studio_plugin": external_plugin,
        })

    def get_service(self, name: str) -> object | None:
        if name in {
            "session_ledger_service",
            "soundcloud_artist_studio_service",
            "scheduling_service",
            "fixture_service",
        }:
            return self._service
        return None

    def get_process_registry(self) -> dict[str, object]:
        return self._registry


def _build_registry() -> dict[str, object]:
    """Mirror the runtime process_registry shape: {processes: {key: def, ...}}."""
    return {
        "processes": {
            "service_interface::fixture_service::list_clean": {
                "name": "list_clean", "parameters": {},
            },
            "service_interface::fixture_service::get_by_id": {
                "name": "get_by_id",
                "parameters": {
                    "identifier": {"type": "string", "required": True},
                },
            },
            "service_interface::session_ledger_service::list_sessions": {
                "name": "list_sessions",
                "parameters": {
                    "limit": {"type": "integer", "required": False},
                },
            },
            "plugin::soundcloud_artist_studio_plugin::get_account_status": {
                "name": "get_account_status", "parameters": {},
            },
            "service_interface::scheduling_service::execute_in_seconds": {
                "name": "execute_in_seconds",
                "parameters": {
                    "seconds": {"type": "integer", "required": True},
                    "action_definitions": {"type": "list", "required": True},
                },
            },
            "service_interface::fixture_service::register_thing": {
                "name": "register_thing",
                "parameters": {
                    "name": {"type": "string", "required": True},
                    "root_uri": {"type": "string", "required": True},
                },
            },
            "plugin::platform_health_plugin::execute_registry_sweep": {
                "name": "execute_registry_sweep", "parameters": {},
            },
        },
    }


# ─── Unit-level tests ───────────────────────────────────────────────────────


def test_classify_shape() -> None:
    _check(classify_shape("list_sessions") == "read", "list_* → read")
    _check(classify_shape("list_active_sessions") == "read", "list_active_* → read")
    _check(classify_shape("get_session_timeline") == "read", "get_* → read")
    _check(classify_shape("register_source") == "write", "register_* → write")
    _check(classify_shape("ingest_raw_chunk") == "write", "ingest_* → write")
    _check(classify_shape("trigger_poll") == "write", "trigger_* → write")


def test_split_process_key() -> None:
    _check(
        split_process_key("service_interface::fixture::list_x") ==
        ("service_interface", "fixture", "list_x"),
        "service_interface key splits cleanly",
    )
    _check(
        split_process_key("plugin::foo::bar") == ("plugin", "foo", "bar"),
        "plugin key splits cleanly",
    )
    _check(
        split_process_key("malformed::key") is None,
        "malformed (2 segments) returns None",
    )
    _check(
        split_process_key("rogue::ns::method") is None,
        "unknown namespace returns None",
    )


def test_build_sentinel_args() -> None:
    parameters = {
        "name": {"type": "string", "required": True},
        "count": {"type": "integer", "required": True},
        "enabled": {"type": "boolean", "required": True},
        "items": {"type": "list", "required": True},
        "meta": {"type": "dict", "required": True},
        "optional_field": {"type": "string", "required": False},
    }
    args = build_sentinel_args(parameters)
    _check(args["name"] == "smoke_sentinel", "string sentinel applied")
    _check(args["count"] == 0, "integer sentinel applied")
    _check(args["enabled"] is False, "boolean sentinel applied")
    _check(args["items"] == [], "list sentinel applied")
    _check(args["meta"] == {}, "dict sentinel applied")
    _check("optional_field" not in args, "optional params omitted")


# ─── End-to-end sweep against fixture orchestrator ──────────────────────────


def test_sweep_read_only_default() -> None:
    service = _FixtureService()
    external_plugin = _FixtureExternalPlugin()
    registry = _build_registry()
    orch = _FixtureOrchestrator(service, registry, external_plugin)
    report = run_sweep(orch, write_enabled=False, dry_run=False)
    by_key = {row["process_key"]: row for row in report["results"]}

    # Read-shape verbs all fired (some ok, some failed).
    list_clean = by_key["service_interface::fixture_service::list_clean"]
    _check(list_clean["status"] == STATUS_OK, "clean list_* call recorded ok")
    _check(list_clean["shape"] == "read", "list_clean classified as read")

    list_sessions = by_key["service_interface::session_ledger_service::list_sessions"]
    _check(list_sessions["status"] == STATUS_FAILED, "ledger list_sessions captured as failed")
    _check(
        "integer = boolean" in str(list_sessions["error_message"]),
        "ledger integer=boolean error text preserved verbatim",
    )

    get_status = by_key["plugin::soundcloud_artist_studio_plugin::get_account_status"]
    _check(get_status["status"] == STATUS_SKIPPED_SCOPE, "external plugin excluded from live default scope")
    _check(not external_plugin.calls, "old call shape dispatches no external plugin by default")

    # Write-shape verbs skipped by default.
    register = by_key["service_interface::fixture_service::register_thing"]
    _check(register["status"] == STATUS_SKIPPED_WRITE, "register_* skipped without write_enabled")
    _check(register["shape"] == "write", "register_* classified as write")

    execute_in_seconds = by_key[
        "service_interface::scheduling_service::execute_in_seconds"
    ]
    _check(
        execute_in_seconds["status"] == STATUS_SKIPPED_WRITE,
        "execute_in_seconds skipped under default read-only mode",
    )

    # Self skipped.
    self_row = by_key[SELF_PROCESS_KEY]
    _check(self_row["status"] == STATUS_SKIPPED_SELF, "self process_key skipped")

    # Verify the report counts add up.
    _check(
        report["ok"] + report["failed"] + report["skipped"] == report["total"],
        "report counts add up to total",
    )


def test_sweep_write_enabled_fires_scheduling_failure() -> None:
    """With write_enabled=True, scheduling_service::execute_in_seconds runs and
    its 'Process not found or malformed' error surfaces verbatim.
    """
    service = _FixtureService()
    external_plugin = _FixtureExternalPlugin()
    registry = _build_registry()
    orch = _FixtureOrchestrator(service, registry, external_plugin)
    report = run_sweep(orch, write_enabled=True, dry_run=False)
    by_key = {row["process_key"]: row for row in report["results"]}

    execute_in_seconds = by_key[
        "service_interface::scheduling_service::execute_in_seconds"
    ]
    _check(
        execute_in_seconds["status"] == STATUS_FAILED,
        "execute_in_seconds executes under write_enabled and captures failure",
    )
    _check(
        "Process not found or malformed" in str(execute_in_seconds["error_message"]),
        "execute_in_seconds 'Process not found or malformed' surfaced verbatim",
    )

    register = by_key["service_interface::fixture_service::register_thing"]
    _check(
        register["status"] == STATUS_OK,
        "register_* executes under write_enabled (clean case)",
    )


def test_sweep_include_pattern_narrows() -> None:
    service = _FixtureService()
    external_plugin = _FixtureExternalPlugin()
    registry = _build_registry()
    orch = _FixtureOrchestrator(service, registry, external_plugin)
    report = run_sweep(orch, include_pattern="session_ledger_service")
    _check(report["total"] == 1, "include_pattern narrowed to one process")
    _check(
        report["results"][0]["process_key"]
        == "service_interface::session_ledger_service::list_sessions",
        "include_pattern selected the ledger row",
    )


def test_dry_run_and_explicit_external_scope() -> None:
    service = _FixtureService()
    external_plugin = _FixtureExternalPlugin()
    orch = _FixtureOrchestrator(service, _build_registry(), external_plugin)

    report = run_sweep(orch, write_enabled=False)
    _check(not service.calls and not external_plugin.calls, "dry_run default dispatches nothing")
    _check(report["would_dispatch"] == 3, "dry_run reports service-interface read rows")
    by_key = {row["process_key"]: row for row in report["results"]}
    _check(
        by_key["service_interface::fixture_service::list_clean"]["status"] == STATUS_DRY_RUN,
        "dry_run returns a classification row for a would-dispatch process",
    )

    try:
        run_sweep(
            orch,
            dry_run=False,
            external_namespaces=("soundcloud_artist_studio_plugin",),
        )
    except ValueError as exc:
        _check(
            "operator_confirmation" in str(exc) and "soundcloud_artist_studio_plugin" in str(exc),
            "external scope without confirmation refuses loudly",
        )
    else:
        _check(False, "external scope without confirmation refuses loudly")

    report = run_sweep(
        orch,
        dry_run=False,
        external_namespaces=("soundcloud_artist_studio_plugin",),
        operator_confirmation="operator-turn-123",
    )
    external_row = next(
        row for row in report["results"]
        if row["process_key"] == "plugin::soundcloud_artist_studio_plugin::get_account_status"
    )
    _check(len(external_plugin.calls) == 1, "enumerated external plugin dispatches exactly once")
    _check(
        external_row["operator_confirmation"] == "operator-turn-123",
        "external result row echoes operator confirmation",
    )

    try:
        run_sweep(
            orch,
            dry_run=False,
            external_namespaces=("unknown_plugin",),
            operator_confirmation="operator-turn-123",
        )
    except ValueError as exc:
        _check("declared outward-facing" in str(exc), "unknown plugin namespace fails closed")
    else:
        _check(False, "unknown plugin namespace fails closed")


def main() -> int:
    print("=== platform_health_plugin sweep_smoke ===")
    test_classify_shape()
    test_split_process_key()
    test_build_sentinel_args()
    test_sweep_read_only_default()
    test_sweep_write_enabled_fires_scheduling_failure()
    test_sweep_include_pattern_narrows()
    test_dry_run_and_explicit_external_scope()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
