#!/usr/bin/env python3
"""Phase 2 (2026-06-17) smoke — memory_service cron-only EDGE_SINK siblings
fire the underlying ACT-R backend via Shape-A in-process call.

Background: per `workbench/2026-06-17_scheduler_cron_action_contract_design.md`
§6 and Coordinator-Day's dispatch §5.3-REDIRECT (option-A authorization), the
3 actr_memory crons in `setup_schedules` (formerly dispatching
`service_interface::memory_service::*` with `result_processor_kind: "inference"`
— the bug-active shape that fired `Empty source_namespace in flow trigger_data`
~78 times per 10 minutes per target) now dispatch cron-only EDGE_SINK siblings
declared at the service-interface level:

  - service_interface::memory_service::process_memorization_queue_cron
  - service_interface::memory_service::recompute_strengths_cron
  - service_interface::memory_service::consolidate_cron

Each sibling is declared on `MemoryServiceInterface` with
`@service_interface_process(processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
is_discoverable=False, ...)` (mirroring the session_ledger `trigger_poll`
pattern at `session_ledger_service/interfaces/public.py:756`). The bound
provider `ACTRMemoryPlugin` implements them as thin Shape-A pass-throughs
that call `self._backend.<verb>()` directly (NOT via `process_call` / `submit`
— Shape-B would recreate the bug one dispatch level deeper). The wrapper
completes synchronously; `action_queue_poller` terminates the action at the
EDGE_SINK_SKIP branch (no result-processor dispatch, no inference scaffold).

The cron-only siblings exist ALONGSIDE the discoverable EDGE-category verbs
(`process_memorization_queue` / `consolidate` / `recompute_strengths` at
`services/memory_service/interfaces/public.py` L982 / L1017 / L1075). The
discoverable surface stays intact for direct model invocation.

This smoke asserts:
  (1) Each cron sibling exists on `ACTRMemoryPlugin` with the expected name.
  (2) Each cron sibling is declared on `MemoryServiceInterface` ABC
      (consumer-facing) as an @abstractmethod — ensuring the binding plugin
      MUST implement it.
  (3) Each cron sibling's @service_interface_process declaration on the
      service-interface ABC at `services/memory_service/interfaces/public.py`
      declares `processor_policy_category=ProcessorPolicyCategory.EDGE_SINK`
      and `is_discoverable=False`.
  (4) Each cron sibling invokes `self._backend.<underlying-verb>()` exactly once
      (Shape-A in-process call; NOT a `submit` / `process_call` on the queue).
  (5) Each cron sibling's return envelope shape mirrors the corresponding
      backend method's envelope (Shape-A direct pass-through).

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

from actr_memory_plugin.plugin import ACTRMemoryPlugin  # noqa: E402
from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.interfaces.memory_service_interface import MemoryServiceInterface  # noqa: E402
from ananta.services.memory_service import MemoryService  # noqa: E402
from ananta.services.memory_service.interfaces.public import (  # noqa: E402
    MemoryServiceAPI,
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


# ─── Fixture scaffolding ────────────────────────────────────────────────────


class _RecordingBackend:
    """Stub backend that records every method call so we can assert Shape-A."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def process_memorization_queue(self) -> dict[str, Any]:
        self.calls.append(("process_memorization_queue", {}))
        return {"processed_count": 3, "message": "processed 3 reviews"}

    def recompute_strengths(self) -> dict[str, Any]:
        self.calls.append(("recompute_strengths", {}))
        return {"updated_count": 12, "message": "updated 12 memories"}

    def consolidate(self, dry_run: bool = False, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("consolidate", {"dry_run": dry_run, **kwargs}))
        return {
            "candidates_found": 5,
            "clusters_formed": 2,
            "consolidations": [{"id": "mem-1"}],
            "dry_run": dry_run,
        }


def _make_plugin(*, backend: Any = None) -> ACTRMemoryPlugin:
    """Construct a minimal ACTRMemoryPlugin stand-in with the recording backend."""
    instance = ACTRMemoryPlugin.__new__(ACTRMemoryPlugin)
    instance.name = "actr_memory_plugin"  # type: ignore[assignment]
    import logging  # noqa: PLC0415

    instance.logger = logging.getLogger("actr_memory_plugin")
    instance._backend = backend  # type: ignore[assignment]
    instance._scheduling_service = None
    instance._state_service = None  # type: ignore[assignment]
    instance._services_started = False
    instance._schedules_configured = False
    return instance


def _read_service_interface_metadata(api_class: type, method_name: str) -> dict[str, Any]:
    """Extract the @service_interface_process metadata attached to a method.

    @service_interface_process attaches the same flavor of metadata that
    @platform_process does. We probe the standard locations.
    """
    method = getattr(api_class, method_name, None)
    if method is None:
        return {}
    for attr in (
        "_service_interface_metadata",
        "_action_metadata",
        "_process_metadata",
        "_platform_process_metadata",
    ):
        meta = getattr(method, attr, None)
        if meta is not None:
            return meta if isinstance(meta, dict) else meta.__dict__
    return {}


_CRON_VERBS = ("process_memorization_queue_cron", "recompute_strengths_cron", "consolidate_cron")


# ─── Cases ──────────────────────────────────────────────────────────────────


def test_cron_siblings_implemented_on_plugin() -> None:
    """All 3 cron sibling methods exist on ACTRMemoryPlugin (override the ABC)."""
    for verb_name in _CRON_VERBS:
        method = getattr(ACTRMemoryPlugin, verb_name, None)
        _check(
            method is not None and callable(method),
            f"ACTRMemoryPlugin.{verb_name} exists + is callable",
        )


def test_cron_siblings_declared_on_consumer_abc() -> None:
    """All 3 cron sibling methods are @abstractmethod on MemoryServiceInterface.

    Strict abstract-only check (no `or hasattr(...)` fallback): a concrete
    method on the ABC would pass `hasattr` but lose the binding-plugin
    enforcement contract. Catches accidental @abstractmethod removal.
    """
    abstracts = MemoryServiceInterface.__abstractmethods__
    for verb_name in _CRON_VERBS:
        _check(
            verb_name in abstracts,
            f"MemoryServiceInterface declares {verb_name} as @abstractmethod (abstract set: {sorted(a for a in abstracts if '_cron' in a)})",
        )


def test_all_memory_service_interface_implementers_are_concrete() -> None:
    """Every concrete MemoryServiceInterface subclass implements all abstracts.

    Catches the Claude-C BLOCKER-1 bug class: adding @abstractmethod to the
    interface without adding concrete impls on EVERY subclass leaves the
    affected subclass uninstantiable (TypeError at construction). The original
    Phase 2 IMPL added concrete impls only to ACTRMemoryPlugin, missing
    MemoryService (the service-wrapper that the dispatcher actually
    instantiates via `provider_manager.get_service_instance("memory_service")`).
    Without this assertion, the breakage surfaces only at solet boot time.

    This smoke iterates every concrete MemoryServiceInterface subclass and
    asserts the abstract-method set is empty.
    """
    for cls in (MemoryService, ACTRMemoryPlugin):
        _check(
            not cls.__abstractmethods__,
            (
                f"{cls.__name__} implements all MemoryServiceInterface abstracts "
                f"(unimplemented: {sorted(cls.__abstractmethods__)})"
            ),
        )


def test_cron_siblings_declared_on_service_interface_with_edge_sink() -> None:
    """All 3 cron siblings on services/memory_service/interfaces/public.py
    declare EDGE_SINK + is_discoverable=False per the canonical contract.

    Asserts the metadata probe succeeded (meta non-empty) BEFORE per-field
    checks — a silently-empty meta dict would falsely satisfy `not meta.get(...)`
    style assertions on customizations. Catches accidental decorator
    attribute-name drift.
    """
    for verb_name in _CRON_VERBS:
        meta = _read_service_interface_metadata(MemoryServiceAPI, verb_name)
        _check(
            bool(meta),
            (
                f"{verb_name} @service_interface_process metadata probe found a non-empty "
                f"meta dict on MemoryServiceAPI.{verb_name} (got {len(meta)} keys; "
                "empty dict would mean the decorator attribute name has drifted)"
            ),
        )
        if not meta:
            # Skip per-field checks; probe failure already reported above.
            continue
        category = meta.get("processor_policy_category") or meta.get("processor_category")
        _check(
            category == ProcessorPolicyCategory.EDGE_SINK,
            (
                f"{verb_name} declares processor_policy_category=EDGE_SINK on service-interface "
                f"(got {category!r}; meta_keys={sorted(meta.keys())[:10]!r}...)"
            ),
        )
        _check(
            meta.get("is_discoverable") is False,
            f"{verb_name} declares is_discoverable=False (got {meta.get('is_discoverable')!r})",
        )
        _check(
            not meta.get("result_processor_customizations"),
            f"{verb_name} omits result_processor_customizations per EDGE_SINK contract",
        )
        _check(
            not meta.get("error_processor_customizations"),
            f"{verb_name} omits error_processor_customizations per EDGE_SINK contract",
        )


def test_process_memorization_queue_cron_shape_a() -> None:
    """process_memorization_queue_cron invokes backend.process_memorization_queue() exactly once."""
    backend = _RecordingBackend()
    plugin = _make_plugin(backend=backend)
    result = plugin.process_memorization_queue_cron()

    _check(
        len(backend.calls) == 1 and backend.calls[0][0] == "process_memorization_queue",
        f"exactly one backend.process_memorization_queue() call (got {backend.calls!r})",
    )
    _check(
        result.get("processed_count") == 3,
        f"Shape-A pass-through returns backend envelope (processed_count={result.get('processed_count')!r})",
    )


def test_recompute_strengths_cron_shape_a() -> None:
    """recompute_strengths_cron invokes backend.recompute_strengths() exactly once."""
    backend = _RecordingBackend()
    plugin = _make_plugin(backend=backend)
    result = plugin.recompute_strengths_cron()

    _check(
        len(backend.calls) == 1 and backend.calls[0][0] == "recompute_strengths",
        f"exactly one backend.recompute_strengths() call (got {backend.calls!r})",
    )
    _check(
        result.get("updated_count") == 12,
        f"Shape-A pass-through returns backend envelope (updated_count={result.get('updated_count')!r})",
    )


def test_consolidate_cron_shape_a_with_dry_run() -> None:
    """consolidate_cron invokes backend.consolidate(dry_run=...) exactly once with explicit dry_run."""
    backend = _RecordingBackend()
    plugin = _make_plugin(backend=backend)
    result = plugin.consolidate_cron(dry_run=True)

    _check(
        len(backend.calls) == 1 and backend.calls[0][0] == "consolidate",
        f"exactly one backend.consolidate() call (got {backend.calls!r})",
    )
    if backend.calls:
        kwargs = backend.calls[0][1]
        _check(
            kwargs.get("dry_run") is True,
            f"wrapper threads dry_run=True through to backend (got {kwargs.get('dry_run')!r})",
        )
    _check(
        result.get("candidates_found") == 5 and result.get("clusters_formed") == 2,
        (
            "Shape-A pass-through returns backend envelope "
            f"(candidates_found={result.get('candidates_found')!r}, "
            f"clusters_formed={result.get('clusters_formed')!r})"
        ),
    )


def test_consolidate_cron_default_dry_run_false() -> None:
    """consolidate_cron defaults dry_run=False (mirrors ABC signature default)."""
    backend = _RecordingBackend()
    plugin = _make_plugin(backend=backend)
    plugin.consolidate_cron()

    _check(
        len(backend.calls) == 1 and backend.calls[0][1].get("dry_run") is False,
        f"wrapper defaults dry_run=False when omitted (got {backend.calls!r})",
    )


def test_plugin_does_not_submit_via_action_queue() -> None:
    """Shape-A discipline: cron sibling never references submit / process_call.

    Inspects the source of each cron sibling on ACTRMemoryPlugin to confirm
    NONE of them route through the action queue dispatch path. Catches accidental
    Shape-B drift in IMPL.
    """
    for verb_name in _CRON_VERBS:
        method = getattr(ACTRMemoryPlugin, verb_name)
        source = inspect.getsource(method)
        for forbidden in ("process_call", "submit(", ".submit_action", "_action_queue"):
            _check(
                forbidden not in source,
                f"{verb_name} body does not reference '{forbidden}' (Shape-A discipline)",
            )


def main() -> int:
    print("=== cron_remediation_smoke (Phase 2 EDGE_SINK siblings; option-A FOLD) ===")
    test_cron_siblings_implemented_on_plugin()
    test_cron_siblings_declared_on_consumer_abc()
    test_all_memory_service_interface_implementers_are_concrete()
    test_cron_siblings_declared_on_service_interface_with_edge_sink()
    test_process_memorization_queue_cron_shape_a()
    test_recompute_strengths_cron_shape_a()
    test_consolidate_cron_shape_a_with_dry_run()
    test_consolidate_cron_default_dry_run_false()
    test_plugin_does_not_submit_via_action_queue()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
