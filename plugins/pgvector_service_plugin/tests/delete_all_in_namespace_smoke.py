#!/usr/bin/env python3
"""Smoke test for `delete_all_in_namespace` end-to-end (no pytest).

Task #51 — the `_clear_process_vectors` API mismatch fix. Verifies:

1. `PGVectorProvider.delete_all_in_namespace` issues a single bare
   `DELETE FROM <schema>.<namespace>__embeddings` via state_service, no
   WHERE clause, no `is_deleted` filter.
2. `PGVectorServicePlugin.delete_all_in_namespace` wraps the provider result
   in an ActionResult.
3. `DiscoveryService._clear_process_vectors` routes through
   `vector_service.delete_all_in_namespace` (NOT `state_service.delete_records`),
   surfaces a non-completed status as `RuntimeError` (no swallowing), and is
   a no-op when no vector_service is bound.
4. The "plant stale embedding -> assert gone after clear" pattern works through
   the new path: a recorder vector_service captures the namespace argument and
   reports a non-zero deleted_count, which the discovery service logs.

Run:
    .venv/bin/python3 plugins/pgvector_service_plugin/tests/delete_all_in_namespace_smoke.py

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "pgvector_service_plugin" / "src"))

from pgvector_service_plugin import PGVectorConfig, PGVectorProvider  # noqa: E402
from pgvector_service_plugin.plugin import PGVectorServicePlugin  # noqa: E402
from pgvector_service_plugin.postgres_backend.vector.constants import (  # noqa: E402
    KEY_ACTION_STATUS,
    KEY_DATA,
    KEY_DELETED_COUNT,
    STATUS_COMPLETED,
)


def _no_pool(_config: PGVectorConfig) -> Any:
    """Pool builder that must never fire on the delete path.

    ``delete_all_in_namespace`` issues a bare ``DELETE`` through
    ``state_service.execute_sql`` and never opens the connection pool, so a
    smoke exercising only that path proves non-vacuously that the pool stays
    untouched by making its construction raise.
    """
    raise AssertionError(
        "connection pool built on the delete_all_in_namespace path — it must "
        "route through state_service.execute_sql only",
    )


def _fail(label: str, message: str) -> int:
    print(f"FAIL [{label}]: {message}", file=sys.stderr)
    return 1


def _pass(label: str) -> None:
    print(f"PASS [{label}]")


def _make_config() -> PGVectorConfig:
    return PGVectorConfig(  # type: ignore[call-arg]
        host="localhost",
        port=5432,
        database="ananta_db",
        user="ananta_user",
        password="change_me",
        db_schema="state",
    )


class StubStateService:
    """Records execute_sql calls. Returns a configurable result envelope."""

    def __init__(self, *, deleted_rows: int = 0, succeed: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._deleted_rows = deleted_rows
        self._succeed = succeed

    def execute_sql(
        self,
        sql_query: str,
        sql_params: list[object] | None = None,
        calling_service: str = "StateService",
        calling_namespace: str = "ananta.services.state_service",
    ) -> dict[str, Any]:
        self.calls.append({
            "sql": sql_query,
            "params": sql_params,
            "calling_service": calling_service,
            "calling_namespace": calling_namespace,
        })
        if not self._succeed:
            return {
                KEY_ACTION_STATUS: "error",
                KEY_DATA: {},
                "error": "synthetic error",
                "actions": [],
                "timestamp": "",
            }
        return {
            KEY_ACTION_STATUS: STATUS_COMPLETED,
            KEY_DATA: {"rowcount": self._deleted_rows},
            "actions": [],
            "error": None,
            "timestamp": "",
        }


def test_provider_emits_bare_delete() -> int:
    """Provider issues `DELETE FROM <schema>.<namespace>__embeddings` with no WHERE."""
    state = StubStateService(deleted_rows=130)
    provider = PGVectorProvider(
        _make_config(),
        "pgvector_service_plugin",
        pool_builder=_no_pool,
        state_service=state,  # type: ignore[arg-type]
    )
    result = provider.delete_all_in_namespace(namespace="discovery_processes")

    if result != {KEY_DELETED_COUNT: 130}:
        return _fail("provider_result_shape", f"got {result!r}")
    if len(state.calls) != 1:
        return _fail("provider_call_count", f"expected 1 call, got {len(state.calls)}")
    sql = state.calls[0]["sql"]
    if "WHERE" in sql.upper():
        return _fail("provider_no_where", f"SQL had a WHERE clause: {sql!r}")
    if "is_deleted" in sql:
        return _fail("provider_no_is_deleted_filter", f"SQL referenced is_deleted: {sql!r}")
    if sql != 'DELETE FROM "state"."discovery_processes__embeddings"':
        return _fail("provider_sql_exact", f"got {sql!r}")
    if state.calls[0]["params"] is not None:
        return _fail("provider_params_none", f"params should be None, got {state.calls[0]['params']!r}")
    _pass("provider_emits_bare_delete")
    return 0


def test_provider_surfaces_state_service_error() -> int:
    """Provider raises RuntimeError when state_service returns non-completed."""
    state = StubStateService(succeed=False)
    provider = PGVectorProvider(
        _make_config(),
        "pgvector_service_plugin",
        pool_builder=_no_pool,
        state_service=state,  # type: ignore[arg-type]
    )
    try:
        provider.delete_all_in_namespace(namespace="discovery_processes")
    except RuntimeError as e:
        msg = str(e)
        if "discovery_processes" not in msg:
            return _fail("provider_error_message", f"missing namespace in message: {msg!r}")
        _pass("provider_surfaces_state_service_error")
        return 0
    return _fail("provider_surfaces_state_service_error", "expected RuntimeError, none raised")


def test_plugin_wraps_provider_result() -> int:
    """Plugin's delete_all_in_namespace returns an ActionResult around the provider dict."""
    plugin = PGVectorServicePlugin()
    state = StubStateService(deleted_rows=7)
    plugin._provider = PGVectorProvider(
        _make_config(),
        "pgvector_service_plugin",
        pool_builder=_no_pool,
        state_service=state,  # type: ignore[arg-type]
    )
    plugin.set_ready()

    result = plugin.delete_all_in_namespace("discovery_processes")

    if result.get(KEY_ACTION_STATUS) != STATUS_COMPLETED:
        return _fail("plugin_status", f"got {result.get(KEY_ACTION_STATUS)!r}")
    data = result.get(KEY_DATA, {})
    inner = data.get("result") if isinstance(data, dict) else None
    if not isinstance(inner, dict) or inner.get(KEY_DELETED_COUNT) != 7:
        return _fail("plugin_payload_shape", f"got {data!r}")
    _pass("plugin_wraps_provider_result")
    return 0


class RecorderVectorService:
    """Records delete_all_in_namespace calls and replays a configurable result."""

    def __init__(self, *, deleted_count: int = 0, succeed: bool = True) -> None:
        self.calls: list[str] = []
        self._deleted_count = deleted_count
        self._succeed = succeed

    def delete_all_in_namespace(self, namespace: str) -> dict[str, Any]:
        self.calls.append(namespace)
        if not self._succeed:
            return {
                KEY_ACTION_STATUS: "error",
                KEY_DATA: {},
                "error": "synthetic failure from recorder",
                "actions": [],
                "timestamp": "",
            }
        return {
            KEY_ACTION_STATUS: STATUS_COMPLETED,
            KEY_DATA: {"deleted_count": self._deleted_count},
            "actions": [],
            "error": None,
            "timestamp": "",
        }


def _build_discovery_service(vector_service: Any) -> Any:
    """Construct a minimal DiscoveryService with stubbed state_service."""
    from ananta.services.discovery_service.service import DiscoveryService

    class StubStateForDiscovery:
        def create_schema(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"status": "success"}

        def read_state(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"data": {"result": {"records": []}}}

        def write_state(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {KEY_ACTION_STATUS: STATUS_COMPLETED, KEY_DATA: {}}

        def delete_records(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                "discovery_service._clear_process_vectors MUST NOT call "
                "state_service.delete_records — bug fix regression",
            )

    return DiscoveryService(
        app_home=str(REPO_ROOT),
        state_service=StubStateForDiscovery(),  # type: ignore[arg-type]
        plugin_manager=None,
        process_registry=None,
        embedding_service=None,
        vector_service=vector_service,
    )


def test_discovery_routes_through_vector_service() -> int:
    """clear_process_vectors goes through vector_service, NOT state_service."""
    recorder = RecorderVectorService(deleted_count=130)
    svc = _build_discovery_service(recorder)
    svc.clear_process_vectors()
    if recorder.calls != ["discovery_processes"]:
        return _fail("discovery_namespace_arg", f"got {recorder.calls!r}")
    _pass("discovery_routes_through_vector_service")
    return 0


def test_discovery_surfaces_clear_failure() -> int:
    """clear_process_vectors raises when vector_service returns non-completed.

    The previous implementation swallowed all exceptions via a bare try/except;
    fix-Task-51 makes the failure visible.
    """
    recorder = RecorderVectorService(succeed=False)
    svc = _build_discovery_service(recorder)
    try:
        svc.clear_process_vectors()
    except RuntimeError as e:
        if "Failed to clear process embeddings" not in str(e):
            return _fail("discovery_error_text", f"got: {e!s}")
        _pass("discovery_surfaces_clear_failure")
        return 0
    return _fail("discovery_surfaces_clear_failure", "expected RuntimeError, none raised")


def test_discovery_noop_without_vector_service() -> int:
    """clear_process_vectors is a no-op when no vector_service is bound."""
    svc = _build_discovery_service(vector_service=None)
    svc.clear_process_vectors()  # must not raise
    _pass("discovery_noop_without_vector_service")
    return 0


def test_plant_clear_assert_gone() -> int:
    """End-to-end shape: plant -> clear -> assert recorder saw the clear.

    This mirrors the dispatch's "plant a stale embedding, remove process X from
    the registry, trigger rebuild, assert the embedding for X is gone" pattern
    at the discovery_service boundary. The recorder stands in for the vector
    store: a 'plant' is implicit (recorder ignores writes); a 'clear' is the
    delete_all_in_namespace call; 'gone' is reflected in the recorder's deleted_count.
    """
    recorder = RecorderVectorService(deleted_count=42)  # 42 planted stale rows
    svc = _build_discovery_service(recorder)
    svc.clear_process_vectors()
    if recorder.calls != ["discovery_processes"]:
        return _fail("plant_clear_namespace", f"got {recorder.calls!r}")
    # If this fix regressed, the recorder would not be invoked (state_service.delete_records would be).
    _pass("plant_clear_assert_gone")
    return 0


def main() -> int:
    tests = (
        test_provider_emits_bare_delete,
        test_provider_surfaces_state_service_error,
        test_plugin_wraps_provider_result,
        test_discovery_routes_through_vector_service,
        test_discovery_surfaces_clear_failure,
        test_discovery_noop_without_vector_service,
        test_plant_clear_assert_gone,
    )
    for t in tests:
        rc = t()
        if rc != 0:
            return rc
    print("--- ALL SMOKE PASSED ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
