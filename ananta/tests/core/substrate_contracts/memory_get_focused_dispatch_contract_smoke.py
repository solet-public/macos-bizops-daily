#!/usr/bin/env python3
"""REL-11 — memory_service dict-envelope dispatch contracts (no pytest).

Background: ``service_interface::memory_service::get_focused`` was dead on
arrival over ``process_call`` since registration — the implementation chain
returned ``list[dict]`` while the service-interface dispatch contract
(``ActionProcessor._execute_standard_service_method``) rejects any non-dict
return with ``FrameworkError: Service method '<provider>.<name>' must return
dict``. The fix wraps the verb's return as the platform-standard envelope
``{"memories": [...], "count": N}`` (the same shape ``recall`` uses) across
the ABC / service wrapper / actr plugin, and unwraps at every in-process
consumer. The REL-11 Part-2 sweep found exactly one more same-class defect —
``get_recent_memory_structured`` — fixed the same way in the Part-B slice
(``{"events": [...], "count": N}``) and pinned here (cases F-J), together
with the GTE-07 gate's R1 collision fail-closed classifier rule (case K).

This smoke pins both fixes through the SAME act-time contract that killed
the verbs, at the real entrypoint (``_execute_service_interface_action``),
with the REAL registry entries built from the decorator metadata — not a
curated subset.

Cases:
  A. get_focused registration-shape pin: OBJECT return_value_schema with
     ``memories`` + ``count`` properties.
  B. get_focused production impl pin: the REAL ``ACTRMemoryPlugin``
     interface method (backend stand-in at the plugin's internal
     ``_backend`` seam) returns the envelope. Reverting the wrap = red.
  C. get_focused act-time dispatch pin through the REAL
     ``_execute_service_interface_action`` (pre-fix list shape raised
     ``FrameworkError`` right here — red-first proven by revert).
  D. Negative control: a bare-list return through the same entrypoint
     raises ``FrameworkError`` — the pinned contract check is live.
  E. Consumer-seam pin: ``advancement.has_focused_plan`` consumes the
     envelope (its except-guard would silently mask a missed unwrap).
  F. get_recent_memory_structured registration-shape pin: OBJECT schema
     with ``events`` + ``count``.
  G. get_recent_memory_structured production impl pin (red-first proven by
     reverting the plugin wrap).
  H. get_recent_memory_structured act-time dispatch pin through the REAL
     entrypoint — including the declared-parameter path (``session_id`` is
     schema-declared, so ``_filter_and_inject_arguments`` injects it).
  I. Negative control for the structured verb: bare-list return raises
     ``FrameworkError`` through the same entrypoint.
  J. Consumer-seam pin: ``context_attachments.fetch_recent_memory_records``
     unwraps the envelope and raises loud TypeError on a non-dict return
     (the pre-fix isinstance-else-[] silently masked a shape break).
  K. GTE-07 R1 collision probe: a return-type name defined as BOTH a
     TypedDict and a dataclass in-tree (real examples: ColumnDefinition,
     PluginConfig, ValidationResult) FAILS CLOSED on the plugin path and
     stays allowed on the service path.

Project policy: no pytest. Offline — no live solet / LM Studio / Postgres.
Exits 0 on success, 1 on first-failed-check aggregate.

Run from repo root:
    SOLET_NAME=<name> .venv/bin/python3 ananta/tests/core/substrate_contracts/memory_get_focused_dispatch_contract_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "actr_memory_plugin" / "src"))

from actr_memory_plugin.plugin import ACTRMemoryPlugin  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import QueuedAction  # noqa: E402
from ananta.core.plans.advancement import has_focused_plan  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.prompts.context import ACTIVE_PLAN_MARKER  # noqa: E402
from ananta.core.prompts.stages.context_attachments import (  # noqa: E402
    fetch_recent_memory_records,
)
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.memory_service.interfaces.public import MemoryServiceAPI  # noqa: E402

# substrate_contract_fixtures lives beside this file (sibling module); the
# GTE-07 gate module lives in quality_gates/ (not a package) — both imported
# via explicit path inserts.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_REPO_ROOT / "quality_gates"))
from return_shape_gate import (  # noqa: E402
    _PLUGIN_DECORATOR,
    _SERVICE_DECORATOR,
    _classify_symbol,
    _SymbolIndex,
)
from substrate_contract_fixtures import Checker  # noqa: E402

if TYPE_CHECKING:
    from ananta.core.services.prompt_context_builder import PromptContextBuilder

_PROCESS_KEY = "service_interface::memory_service::get_focused"
_PROVIDER = "memory_service"
_VERB = "get_focused"
_STRUCTURED_VERB = "get_recent_memory_structured"

_BACKEND_RECORDS: list[dict[str, Any]] = [
    {"memory_id": "mem-focus-1", "content": "focused fact one", "tags": ["note"]},
    {"memory_id": "mem-focus-2", "content": f"{ACTIVE_PLAN_MARKER}\n[>] 1. step", "tags": ["plan"]},
]

_EVENT_ROWS: list[dict[str, Any]] = [
    {
        "session_id": "sess-smoke",
        "event_type": "INPUT",
        "content": "recent event one",
        "timestamp": "2026-07-06T00:00:01+00:00",
        "metadata": {},
    },
    {
        "session_id": "sess-smoke",
        "event_type": "OUTPUT",
        "content": "recent event two",
        "timestamp": "2026-07-06T00:00:02+00:00",
        "metadata": {"attachments": [{"blob_id": "bmd-smoke-1"}]},
    },
]


class _StandInBackend:
    """Stand-in at the plugin's internal ``_backend`` seam (list-returning,
    like the real ``ACTRMemoryBackend`` methods, session-scoped per JOS-02)."""

    def get_focused(self, *, session_id: str) -> list[dict[str, Any]]:
        del session_id
        return list(_BACKEND_RECORDS)

    def get_recent_memory_structured(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        del session_id, max_events, max_age_hours, namespace_filter
        return list(_EVENT_ROWS)


def _real_plugin_with_standin_backend() -> ACTRMemoryPlugin:
    """A real ``ACTRMemoryPlugin`` whose internal backend is the stand-in.

    ``__new__`` skips ``__init__`` (which wires live Postgres collaborators);
    ``get_focused`` — the code under test — touches only ``self._backend``
    via ``_get_backend()``.
    """
    plugin = ACTRMemoryPlugin.__new__(ACTRMemoryPlugin)
    setattr(plugin, "_backend", _StandInBackend())  # noqa: B010
    return plugin


class _ListShapedService:
    """Negative control: the pre-fix (broken) bare-list return shapes."""

    def get_focused(self, **_: Any) -> list[dict[str, Any]]:
        return list(_BACKEND_RECORDS)

    def get_recent_memory_structured(
        self,
        session_id: str | None = None,
        max_events: int = 20,
        max_age_hours: int | None = None,
        namespace_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        del session_id, max_events, max_age_hours, namespace_filter
        return list(_EVENT_ROWS)


class _Orchestrator:
    """Minimal OrchestratorProtocol carrier for service resolution."""

    APP_HOME = "."
    action_recorder = None

    def __init__(self, service: object) -> None:
        self._service = service

    def get_service(self, service_name: str) -> object | None:
        return self._service if service_name == _PROVIDER else None


def _real_action_processor(service: object) -> ActionProcessor:
    """The REAL ActionProcessor over the REAL registry entries for both verbs.

    The registry entries are built from the live decorator metadata
    (``to_process_dict()``) — the same source the platform registry builder
    merges at startup — so the pins cannot drift from the real registration.
    """
    processes: dict[str, object] = {}
    for api_method in (
        MemoryServiceAPI.get_focused,
        MemoryServiceAPI.get_recent_memory_structured,
    ):
        metadata = getattr(api_method, "_service_interface_metadata")  # noqa: B009
        processes[f"service_interface::{_PROVIDER}::{metadata.name}"] = (
            metadata.to_process_dict()
        )
    return ActionProcessor(
        plugin_manager=PluginManager(),
        orchestrator=_Orchestrator(service),  # type: ignore[arg-type]
        process_registry={"processes": processes},
    )


class _DispatchAction(QueuedAction):
    """``QueuedAction`` + the ``source_plugin`` field ``QueuedActionProtocol``
    declares (the production dataclass predates the field; ActionProcessor
    reads it via a defensive ``getattr``)."""

    source_plugin: str | None = None


def _widen(value: object) -> object:
    """Defeat static narrowing so runtime shape pins stay meaningful (under
    the pre-fix code the annotated dict return is a bare list at runtime)."""
    return value


def _queued_action() -> _DispatchAction:
    return _DispatchAction(
        id="ae-get-focused-smoke",
        process_key=_PROCESS_KEY,
        parameters="{}",
        notes="",
        created_at="2026-07-06T00:00:00+00:00",
        session_id="sess-smoke",
        flow_id="flow-smoke",
    )


def _pin_get_focused_registration(checker: Checker) -> None:
    """Case A: get_focused registration-shape pins (real decorator metadata)."""
    metadata = getattr(MemoryServiceAPI.get_focused, "_service_interface_metadata")  # noqa: B009
    checker.check(
        metadata.name == _VERB and metadata.provider == _PROVIDER,
        "A1: decorator registers name=get_focused provider=memory_service",
    )
    process_def = metadata.to_process_dict()
    rvs = process_def["return_value_schema"]
    checker.check(
        isinstance(rvs, dict) and rvs.get("type") == "object",
        "A2: return_value_schema is OBJECT (envelope), not LIST",
    )
    props = rvs.get("properties", {}) if isinstance(rvs, dict) else {}
    checker.check(
        isinstance(props, dict) and set(props.keys()) == {"memories", "count"},
        "A3: schema properties are exactly {memories, count}",
    )
    # (former A4 field_sensitivities-mirror pin retired with the declarations,
    # 2026-07-16 frontier-first B4 — A3's schema-properties pin carries the
    # return-shape contract.)


def _pin_structured_registration(checker: Checker) -> None:
    """Case F: get_recent_memory_structured registration-shape pins."""
    structured_md = getattr(  # noqa: B009
        MemoryServiceAPI.get_recent_memory_structured, "_service_interface_metadata",
    )
    checker.check(
        structured_md.name == _STRUCTURED_VERB and structured_md.provider == _PROVIDER,
        "F1: decorator registers name=get_recent_memory_structured "
        "provider=memory_service",
    )
    structured_rvs = structured_md.to_process_dict()["return_value_schema"]
    checker.check(
        isinstance(structured_rvs, dict) and structured_rvs.get("type") == "object",
        "F2: return_value_schema is OBJECT (envelope), not LIST",
    )
    structured_props = (
        structured_rvs.get("properties", {}) if isinstance(structured_rvs, dict) else {}
    )
    checker.check(
        isinstance(structured_props, dict)
        and set(structured_props.keys()) == {"events", "count"},
        "F3: schema properties are exactly {events, count}",
    )
    # (former F4 field_sensitivities-mirror pin retired with the declarations,
    # 2026-07-16 frontier-first B4 — F3's schema-properties pin carries the
    # return-shape contract.)


def _pin_real_plugin_envelopes(checker: Checker) -> None:
    """Cases B + G: the real plugin interface methods return the envelopes."""
    plugin = _real_plugin_with_standin_backend()
    envelope_obj = _widen(plugin.get_focused(session_id="sess-contract"))
    checker.check(
        isinstance(envelope_obj, dict),
        "B1: ACTRMemoryPlugin.get_focused returns a dict (dispatch contract)",
    )
    envelope: dict[str, Any] = envelope_obj if isinstance(envelope_obj, dict) else {}
    checker.check(
        envelope.get("memories") == _BACKEND_RECORDS,
        "B2: envelope['memories'] carries the backend records unchanged",
    )
    checker.check(
        envelope.get("count") == len(_BACKEND_RECORDS),
        "B3: envelope['count'] == len(memories)",
    )

    structured_obj = _widen(
        _real_plugin_with_standin_backend().get_recent_memory_structured(
            session_id="sess-smoke",
        )
    )
    checker.check(
        isinstance(structured_obj, dict),
        "G1: ACTRMemoryPlugin.get_recent_memory_structured returns a dict",
    )
    structured_env: dict[str, Any] = (
        structured_obj if isinstance(structured_obj, dict) else {}
    )
    checker.check(
        structured_env.get("events") == _EVENT_ROWS,
        "G2: envelope['events'] carries the backend event rows unchanged",
    )
    checker.check(
        structured_env.get("count") == len(_EVENT_ROWS),
        "G3: envelope['count'] == len(events)",
    )


def _wrapper_shaped_service(plugin: Any) -> Any:
    """The REAL MemoryService wrapper methods bound over *plugin* (JOS-02).

    Production dispatch targets the MemoryService wrapper (which resolves the
    acting session from server-injected ``state``); this shim runs the real
    wrapper implementation without requiring a PluginManager.
    """
    from ananta.services.memory_service import MemoryService

    class _Shim:
        _resolve_acting_session = staticmethod(
            MemoryService._resolve_acting_session,  # noqa: SLF001
        )
        get_focused = MemoryService.get_focused

        def _get_backend(self) -> Any:
            return plugin

        def get_recent_memory_structured(
            self,
            session_id: str | None = None,
            max_events: int = 20,
        ) -> dict[str, Any]:
            return plugin.get_recent_memory_structured(
                session_id=session_id, max_events=max_events,
            )

    return _Shim()


def _pin_act_time_dispatch(checker: Checker) -> None:
    """Cases C + H: dispatch pins through the REAL entrypoint (both verbs)."""
    processor = _real_action_processor(
        _wrapper_shaped_service(_real_plugin_with_standin_backend()),
    )
    try:
        result = processor._execute_service_interface_action(  # noqa: SLF001
            _PROVIDER, _VERB, {}, _queued_action(),
        )
    except FrameworkError as err:
        # Pre-fix shape: the dispatch contract kills the verb right here.
        checker.check(False, f"C1: dispatch raised FrameworkError [{err}]")
    else:
        checker.check(
            result.get("memories") == _BACKEND_RECORDS
            and result.get("count") == len(_BACKEND_RECORDS),
            "C1: _execute_service_interface_action returns the envelope "
            "(pre-fix list shape raised FrameworkError here)",
        )

    processor_h = _real_action_processor(
        _wrapper_shaped_service(_real_plugin_with_standin_backend()),
    )
    try:
        result_h = processor_h._execute_service_interface_action(  # noqa: SLF001
            _PROVIDER, _STRUCTURED_VERB, {"max_events": 5}, _queued_action(),
        )
    except FrameworkError as err:
        checker.check(False, f"H1: dispatch raised FrameworkError [{err}]")
    else:
        checker.check(
            result_h.get("events") == _EVENT_ROWS
            and result_h.get("count") == len(_EVENT_ROWS),
            "H1: _execute_service_interface_action returns the envelope through "
            "the declared-parameter path (pre-fix list shape raised here)",
        )


def _pin_negative_controls(checker: Checker) -> None:
    """Cases D + I: bare-list returns still die at the contract (both verbs)."""
    broken = _real_action_processor(_ListShapedService())
    checker.expect_raises(
        FrameworkError,
        "D1: bare-list return raises FrameworkError('must return dict') "
        "through the SAME entrypoint",
        lambda: broken._execute_service_interface_action(  # noqa: SLF001
            _PROVIDER, _VERB, {}, _queued_action(),
        ),
    )

    broken_i = _real_action_processor(_ListShapedService())
    checker.expect_raises(
        FrameworkError,
        "I1: bare-list get_recent_memory_structured raises FrameworkError "
        "through the SAME entrypoint",
        lambda: broken_i._execute_service_interface_action(  # noqa: SLF001
            _PROVIDER, _STRUCTURED_VERB, {}, _queued_action(),
        ),
    )


def _pin_consumer_seams(checker: Checker) -> None:
    """Cases E + J: in-process consumers unwrap the envelopes correctly."""
    checker.check(
        has_focused_plan(
            _real_plugin_with_standin_backend(), session_id="sess-contract",
        ) is True,
        "E1: has_focused_plan finds the ACTIVE_PLAN through the envelope "
        "(a missed unwrap is silently masked to False by its guard)",
    )

    class _EmptyFocus:
        def get_focused(self, *, session_id: str) -> dict[str, Any]:
            del session_id
            return {"memories": [], "count": 0}

    checker.check(
        has_focused_plan(_EmptyFocus(), session_id="sess-contract") is False,
        "E2: empty envelope => no focused plan (control)",
    )

    class _BuilderStub:
        def __init__(self, memory_service: object) -> None:
            self._memory_service = memory_service

    records = fetch_recent_memory_records(
        session_id="sess-smoke",
        builder=cast("PromptContextBuilder", _BuilderStub(
            _real_plugin_with_standin_backend()
        )),
        max_events=5,
    )
    checker.check(
        records == _EVENT_ROWS,
        "J1: fetch_recent_memory_records unwraps envelope['events']",
    )
    checker.expect_raises(
        TypeError,
        "J2: a pre-fix bare-list return raises loud TypeError at the consumer "
        "(the old isinstance-else-[] silently masked it)",
        lambda: fetch_recent_memory_records(
            session_id="sess-smoke",
            builder=cast("PromptContextBuilder", _BuilderStub(_ListShapedService())),
            max_events=5,
        ),
    )


def _pin_collision_rule_probe(checker: Checker) -> None:
    """Case K: the GTE-07 R1 collision classifier fails closed on the plugin path."""
    collision_index = _SymbolIndex(
        typeddict_names=frozenset({"ValidationResult"}),
        dataclass_names=frozenset({"ValidationResult"}),
    )
    checker.check(
        _classify_symbol("ValidationResult", _PLUGIN_DECORATOR, collision_index)
        is not None,
        "K1: TypedDict+dataclass name collision FAILS CLOSED on the plugin path",
    )
    checker.check(
        _classify_symbol("ValidationResult", _SERVICE_DECORATOR, collision_index)
        is None,
        "K2: the same collision stays allowed on the service path (both kinds "
        "satisfy its dispatch contract)",
    )
    typeddict_only = _SymbolIndex(
        typeddict_names=frozenset({"ValidationResult"}),
        dataclass_names=frozenset(),
    )
    checker.check(
        _classify_symbol("ValidationResult", _PLUGIN_DECORATOR, typeddict_only)
        is None,
        "K3: an unambiguous TypedDict name stays allowed on the plugin path "
        "(control)",
    )


def main() -> int:
    checker = Checker("REL-11 get_focused dict-envelope dispatch contract")
    _pin_get_focused_registration(checker)
    _pin_structured_registration(checker)
    _pin_real_plugin_envelopes(checker)
    _pin_act_time_dispatch(checker)
    _pin_negative_controls(checker)
    _pin_consumer_seams(checker)
    _pin_collision_rule_probe(checker)
    return checker.summary()


if __name__ == "__main__":
    sys.exit(main())
