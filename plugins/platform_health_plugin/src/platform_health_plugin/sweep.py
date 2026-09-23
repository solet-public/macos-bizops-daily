"""Process-registry sweep engine.

Module-level free functions so the sweep can be unit-tested against fixture
registries without standing up a full orchestrator.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from platform_health_plugin.constants import (
    OUTWARD_FACING_PLUGIN_NAMESPACES,
    READ_SHAPE_PREFIXES,
    SELF_PROCESS_KEY,
    SENTINEL_BOOLEAN,
    SENTINEL_INTEGER,
    SENTINEL_NUMBER,
    SENTINEL_STRING,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED_SCOPE,
    STATUS_SKIPPED_SELF,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_SKIPPED_WRITE,
)

RELOAD_SAFE = True


@dataclass(frozen=True)
class _EvaluationTarget:
    """A parsed process that is eligible for dry-run or live evaluation."""

    namespace: str
    provider: str
    method_name: str
    shape: str
    is_explicit_external_scope: bool

# ─── Orchestrator protocol used by the sweep ────────────────────────────────


class _PluginManagerProtocol(Protocol):
    """Subset of ``PluginManager`` used by the sweep.

    Canonical plugin lookup per ``ActionProcessor._execute_plugin_action``:
    ``plugin_manager.get_plugin(plugin_name)``. The public
    ``OrchestratorProtocol`` does NOT declare ``plugin_manager`` (by design),
    so the dispatch site uses ``getattr`` + a runtime ``cast`` to this
    protocol — duck-typed, structurally correct, and reachable through any
    ``EventOrchestrator`` instance.
    """

    def get_plugin(self, plugin_name: str) -> object: ...


class _OrchestratorProtocol(Protocol):
    """Subset of ``OrchestratorProtocol`` (``plugin_base.py``) used by the sweep.

    Kept narrow to whatever the sweep calls directly on the orchestrator
    surface. ``plugin_manager`` access flows through ``_PluginManagerProtocol``
    at the callsite, not through this protocol — keeping the public
    ``OrchestratorProtocol`` honest.
    """

    def get_service(self, service_name: str) -> object | None: ...

    def get_process_registry(self) -> dict[str, object]: ...


# ─── Classification + sentinel synthesis ────────────────────────────────────


def classify_shape(method_name: str) -> str:
    """Return 'read' or 'write' for one bare method name.

    Per Architect Q1: list_*, get_*, list_active_* are read-shape; everything
    else is write-shape and default-skipped unless write_enabled=True.
    """
    for prefix in READ_SHAPE_PREFIXES:
        if method_name.startswith(prefix):
            return "read"
    return "write"


def build_sentinel_args(parameters: Mapping[str, Any]) -> dict[str, object]:
    """Synthesize a minimal kwargs dict for sweep invocation.

    Only ``required=True`` params get sentinel values; optional params are
    omitted so server-side defaults apply. Unknown types fall back to the
    string sentinel — a real wiring bug will surface via TypeError downstream
    if the method expected a different shape.
    """
    args: dict[str, object] = {}
    for name, meta in parameters.items():
        if not isinstance(meta, Mapping):
            continue
        if not meta.get("required", False):
            continue
        args[name] = _sentinel_for(_lower_type(meta.get("type")))
    return args


def _lower_type(raw: object) -> str:
    if isinstance(raw, str):
        return raw.lower()
    name = getattr(raw, "value", None) or getattr(raw, "name", None)
    if isinstance(name, str):
        return name.lower()
    return ""


def _sentinel_for(type_token: str) -> object:
    if type_token in {"integer", "int"}:
        return SENTINEL_INTEGER
    if type_token in {"number", "float", "double"}:
        return SENTINEL_NUMBER
    if type_token in {"boolean", "bool"}:
        return SENTINEL_BOOLEAN
    if type_token in {"list", "array"}:
        return []
    if type_token in {"dict", "object", "map"}:
        return {}
    return SENTINEL_STRING


# ─── Dispatch ──────────────────────────────────────────────────────────────


def split_process_key(process_key: str) -> tuple[str, str, str] | None:
    """Return (namespace, provider, method) or None if the key is malformed.

    Namespace is either ``service_interface`` or ``plugin``; provider is the
    service name or plugin entry-point name; method is the verb.
    """
    parts = process_key.split("::")
    if len(parts) != 3:
        return None
    namespace, provider, method = parts
    if namespace not in {"service_interface", "plugin"}:
        return None
    return namespace, provider, method


def dispatch_one(
    orchestrator: _OrchestratorProtocol,
    namespace: str,
    provider: str,
    method_name: str,
    args: Mapping[str, object],
) -> None:
    """Invoke a single registered process with the supplied sentinel args.

    Raises the underlying exception unchanged. The caller is responsible for
    catching + recording.
    """
    if namespace == "service_interface":
        service = _resolve_service(orchestrator, provider)
        method = getattr(service, method_name)
        method(**args)
        return
    # plugin_manager is not declared on the public OrchestratorProtocol; access
    # via cast(Any, ...) and re-cast to the structurally-correct protocol.
    plugin_manager: _PluginManagerProtocol = cast(Any, orchestrator).plugin_manager
    plugin = plugin_manager.get_plugin(provider)
    if plugin is None:
        raise LookupError(
            f"plugin {provider!r} not present in plugin_manager (get_plugin returned None)",
        )
    method = getattr(plugin, method_name)
    method(params=dict(args), state={})


def _resolve_service(
    orchestrator: _OrchestratorProtocol,
    provider: str,
) -> object:
    """Mirror ActionProcessor._resolve_service for the legacy-direct services."""
    direct = getattr(orchestrator, provider, None)
    if direct is not None:
        return direct
    service = orchestrator.get_service(provider)
    if service is None:
        raise LookupError(f"service {provider!r} not bound in orchestrator")
    return service


# ─── Top-level sweep ────────────────────────────────────────────────────────


def run_sweep(
    orchestrator: _OrchestratorProtocol,
    *,
    write_enabled: bool = False,
    include_pattern: str | None = None,
    dry_run: bool = True,
    external_namespaces: tuple[str, ...] = (),
    operator_confirmation: str | None = None,
) -> dict[str, Any]:
    """Classify registry processes and, only with explicit scope, dispatch them.

    ``dry_run`` defaults to true and never calls ``dispatch_one``. Live dispatch
    requires ``dry_run=False``; its default scope is read-shape
    ``service_interface`` processes only. Plugin providers are excluded unless
    an outward-facing provider is explicitly enumerated with an operator
    confirmation citation. ``OUTWARD_FACING_PLUGIN_NAMESPACES`` is the declared
    classification table: a provider absent from it is fail-closed rather than
    inferred safe. A diagnostic name does not make a verb safe: a read-shape
    call can reach an external system.

    Result shape:
        {
            "total": int,
            "ok": int,
            "failed": int,
            "skipped": int,
            "results": [
                {
                    "process_key": "service_interface::...",
                    "shape": "read" | "write",
                    "status": "ok" | "failed" | "skipped_write" | ...,
                    "error_class": str | None,
                    "error_message": str | None,
                },
                ...
            ],
        }
    """
    external_namespace_set = _validate_external_scope(
        external_namespaces, operator_confirmation,
    )
    registry = orchestrator.get_process_registry()
    processes_obj = registry.get("processes", {})
    if not isinstance(processes_obj, dict):
        processes_obj = {}
    results: list[dict[str, object]] = []
    counts = {"ok": 0, "failed": 0, "skipped": 0, "would_dispatch": 0}
    for process_key, process_def in _filtered_processes(processes_obj, include_pattern):
        row = _evaluate_one(
            orchestrator,
            process_key,
            process_def,
            write_enabled,
            dry_run,
            external_namespace_set,
            operator_confirmation,
        )
        _count_row(counts, row)
        results.append(row)
    return {
        "total": len(results),
        "ok": counts["ok"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "would_dispatch": counts["would_dispatch"],
        "results": results,
    }


def _evaluate_one(
    orchestrator: _OrchestratorProtocol,
    process_key: str,
    process_def: object,
    write_enabled: bool,
    dry_run: bool,
    external_namespaces: frozenset[str],
    operator_confirmation: str | None,
) -> dict[str, object]:
    target, skipped_row = _evaluation_target(
        process_key, write_enabled, external_namespaces,
    )
    if skipped_row is not None:
        return skipped_row
    assert target is not None
    if dry_run:
        return _row(
            process_key,
            target.shape,
            STATUS_DRY_RUN,
            True,
            None,
            None,
            _external_confirmation(target, operator_confirmation),
        )
    parameters = _extract_parameters(process_def)
    args = build_sentinel_args(parameters)
    try:
        dispatch_one(orchestrator, target.namespace, target.provider, target.method_name, args)
    except Exception as exc:  # noqa: BLE001 — intentional broad capture: gate surfaces ANY exception
        return _row(
            process_key, target.shape,
            STATUS_FAILED, True,
            type(exc).__name__,
            str(exc),
            _external_confirmation(target, operator_confirmation),
        )
    return _row(
        process_key, target.shape, STATUS_OK, True, None, None,
        _external_confirmation(target, operator_confirmation),
    )


def _validate_external_scope(
    external_namespaces: tuple[str, ...], operator_confirmation: str | None,
) -> frozenset[str]:
    if external_namespaces and not operator_confirmation:
        names = ", ".join(external_namespaces)
        raise ValueError(
            "operator_confirmation is required when external_namespaces are "
            f"enumerated: {names}",
        )
    namespace_set = frozenset(external_namespaces)
    unknown_namespaces = sorted(namespace_set - OUTWARD_FACING_PLUGIN_NAMESPACES)
    if unknown_namespaces:
        raise ValueError(
            "external_namespaces contains providers without a declared outward-facing "
            f"classification: {', '.join(unknown_namespaces)}",
        )
    return namespace_set


def _filtered_processes(
    processes: dict[object, object], include_pattern: str | None,
) -> list[tuple[str, object]]:
    return [
        (process_key, process_def)
        for process_key, process_def in processes.items()
        if isinstance(process_key, str)
        and (include_pattern is None or include_pattern in process_key)
    ]


def _count_row(counts: dict[str, int], row: dict[str, object]) -> None:
    if row["would_dispatch"]:
        counts["would_dispatch"] += 1
    status = row["status"]
    if status == STATUS_OK:
        counts["ok"] += 1
    elif status == STATUS_FAILED:
        counts["failed"] += 1
    else:
        counts["skipped"] += 1


def _evaluation_target(
    process_key: str, write_enabled: bool, external_namespaces: frozenset[str],
) -> tuple[_EvaluationTarget | None, dict[str, object] | None]:
    if process_key == SELF_PROCESS_KEY:
        return None, _row(process_key, "read", STATUS_SKIPPED_SELF, False, None, None, None)
    split = split_process_key(process_key)
    if split is None:
        return None, _row(
            process_key, "write", STATUS_SKIPPED_UNRESOLVED, False, None,
            "malformed process_key", None,
        )
    namespace, provider, method_name = split
    shape = classify_shape(method_name)
    if shape == "write" and not write_enabled:
        return None, _row(process_key, shape, STATUS_SKIPPED_WRITE, False, None, None, None)
    is_explicit_external_scope = namespace == "plugin" and provider in external_namespaces
    if namespace != "service_interface" and not is_explicit_external_scope:
        return None, _row(process_key, shape, STATUS_SKIPPED_SCOPE, False, None, None, None)
    return _EvaluationTarget(
        namespace, provider, method_name, shape, is_explicit_external_scope,
    ), None


def _external_confirmation(
    target: _EvaluationTarget, operator_confirmation: str | None,
) -> str | None:
    if target.is_explicit_external_scope:
        return operator_confirmation
    return None


def _row(
    process_key: str,
    shape: str,
    status: str,
    would_dispatch: bool,
    error_class: str | None,
    error_message: str | None,
    operator_confirmation: str | None,
) -> dict[str, object]:
    return {
        "process_key": process_key,
        "shape": shape,
        "status": status,
        "would_dispatch": would_dispatch,
        "error_class": error_class,
        "error_message": error_message,
        "operator_confirmation": operator_confirmation,
    }


def _extract_parameters(process_def: object) -> Mapping[str, Any]:
    if not isinstance(process_def, Mapping):
        return {}
    parameters = process_def.get("parameters")
    if isinstance(parameters, Mapping):
        return parameters
    return {}
