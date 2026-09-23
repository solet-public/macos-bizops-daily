#!/usr/bin/env python3
"""Regression smoke for iss_20cc187c: inference result/error templates must
submit only their declared ``params`` and ``state`` arguments.

Before this repair, the registered ``process_results`` / ``process_error``
templates emitted legacy top-level ``model`` and ``prompt`` fields, while the
result/error template helpers also copied ``session_id`` and ``flow_id`` into
``arguments``.  The strict validator therefore raised exactly:

``Unknown arguments: ['model', 'prompt', 'session_id', 'flow_id'].
Declared arguments: ['params', 'state']``.

This runs the real process-registry builder (including the knowledge-base
overlay), result-template context helper, and error-template preparation helper
entirely offline.  It proves that metadata remains action-level and the
inference payload stays inside ``params`` after the runtime overlay is applied.

Run:

    .venv/bin/python3 ananta/tests/core/actions/inference_result_processor_argument_shape_smoke.py
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_factory import ActionFactory, validate_process_arguments  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.core.process_registry.builder import build_process_registry  # noqa: E402
from ananta.core.services.bootstrap_manager import BootstrapManager  # noqa: E402
from ananta.services.inference_service.interfaces.public import InferenceServiceAPI  # noqa: E402

_RESULTS_KEY = "service_interface::inference_service::process_results"
_ERROR_KEY = "service_interface::inference_service::process_error"
_STARTUP_PROCESS_KEY = "service_interface::repo_service::git_status"
_DECLARED: dict[str, object] = {"params": {}, "state": {}}

_failures: list[str] = []


def _build_runtime_registry() -> dict[str, object]:
    """Build the startup registry, including its authoritative JSON overlays."""
    os.environ.setdefault("APP_HOME", str(REPO_ROOT / "profile"))
    bootstrap = BootstrapManager()
    plugin_manager = bootstrap.create_plugin_manager(
        bootstrap.create_bootstrap_services(),
    )
    return build_process_registry(plugin_manager)


def _check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        _failures.append(message)


def _mapping(value: object) -> dict[str, object] | None:
    """Narrow JSON-shaped runtime metadata without propagating ``Unknown``."""
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _template(method_name: str, process_key: str) -> dict[str, object]:
    method = getattr(InferenceServiceAPI, method_name)
    metadata = method._service_interface_metadata  # noqa: SLF001 - registered source under test
    template = copy.deepcopy(metadata.action_definition_template)
    template["process_key"] = process_key
    return template


def _registered_template(registry: dict[str, object], process_key: str) -> dict[str, object]:
    processes = _mapping(registry.get("processes"))
    if processes is None:
        return {}
    process = _mapping(processes.get(process_key))
    if process is None:
        return {}
    return _mapping(process.get("action_definition_template")) or {}


def test_runtime_registry_keeps_inference_payload_inside_params() -> None:
    """Startup registry must preserve the corrected decorator shape after JSON merge."""
    registry = _build_runtime_registry()
    for method_name, process_key in (
        ("process_results", _RESULTS_KEY),
        ("process_error", _ERROR_KEY),
    ):
        template = _registered_template(registry, process_key)
        arguments = _mapping(template.get("arguments"))
        _check(
            arguments is not None and list(arguments) == ["params"],
            f"runtime {method_name} registry template exposes only declared params",
        )
        if arguments is None:
            continue
        params = _mapping(arguments.get("params"))
        _check(
            params is not None and {"model", "prompt"}.issubset(params),
            f"runtime {method_name} registry nests model and prompt inside params",
        )
        try:
            validate_process_arguments(process_key, _DECLARED, arguments)
        except Exception as exc:  # pragma: no cover - failure is reported below
            _check(False, f"runtime {method_name} registry passes strict params/state validation ({exc})")
        else:
            _check(True, f"runtime {method_name} registry passes strict params/state validation")


def test_startup_action_validation_accepts_runtime_error_template() -> None:
    """Exercise the ActionFactory path invoked by orchestrator startup actions."""
    registry = _build_runtime_registry()
    factory = ActionFactory(process_registry=registry)
    startup_action: dict[str, object] = {"process_key": _STARTUP_PROCESS_KEY}
    try:
        factory._validate_action_legacy(startup_action)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    except Exception as exc:  # pragma: no cover - failure is reported below
        _check(False, f"startup action validation builds its runtime error processor ({exc})")
        return

    error_processor = _mapping(startup_action.get("error_processor"))
    _check(
        error_processor is not None,
        "startup action validation attaches a process_error processor from the runtime registry",
    )
    if error_processor is None:
        return
    arguments = _mapping(error_processor.get("arguments"))
    _check(
        arguments is not None and _mapping(arguments.get("params")) is not None,
        "startup action error processor keeps its inference payload under arguments.params",
    )


def _result_template_with_context() -> dict[str, object]:
    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    template = _template("process_results", _RESULTS_KEY)
    poller._inject_context_field_into_template(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        template, "session_id", "ses-result",
    )
    poller._inject_context_field_into_template(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        template, "flow_id", "flow-result",
    )
    return template


def test_result_processor_context_is_metadata_not_arguments() -> None:
    """The production-result path accepts the exact pair that was red before."""
    template = _result_template_with_context()
    arguments = _mapping(template.get("arguments"))
    _check(arguments is not None, "process_results template has an arguments mapping")
    if arguments is None:
        return

    _check(
        list(arguments) == ["params"],
        "result template exposes only declared params after session/flow propagation",
    )
    params = _mapping(arguments.get("params"))
    _check(
        params is not None and {"model", "prompt"}.issubset(params),
        "model and prompt are nested inside declared params",
    )
    _check(
        template.get("session_id") == "ses-result" and template.get("flow_id") == "flow-result",
        "session and flow remain action metadata",
    )
    try:
        validate_process_arguments(_RESULTS_KEY, _DECLARED, arguments)
    except Exception as exc:  # pragma: no cover - failure is reported below
        _check(False, f"result template passes strict validation ({exc})")
    else:
        _check(True, "result template passes strict params/state validation")


def test_error_processor_model_override_stays_inside_params() -> None:
    """The failure-recovery path uses the same declared argument shape."""
    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.inference_model_name = "model-for-regression-smoke"
    template = poller._prepare_error_template(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        _template("process_error", _ERROR_KEY),
        "ses-error",
        "flow-error",
        "ctx-error",
        "service_interface::inference_service::process_results",
    )
    arguments = _mapping(template.get("arguments"))
    _check(arguments is not None, "process_error template has an arguments mapping")
    if arguments is None:
        return

    params = _mapping(arguments.get("params"))
    _check(
        params is not None and params.get("model") == {"name": "model-for-regression-smoke"},
        "error recovery writes its model override inside params",
    )
    _check(
        {"session_id", "flow_id", "context_id"}.issubset(template),
        "error recovery preserves correlation as action metadata",
    )
    try:
        validate_process_arguments(_ERROR_KEY, _DECLARED, arguments)
    except Exception as exc:  # pragma: no cover - failure is reported below
        _check(False, f"error template passes strict validation ({exc})")
    else:
        _check(True, "error template passes strict params/state validation")


def main() -> int:
    print("iss_20cc187c inference result-processor argument-shape smoke")
    test_runtime_registry_keeps_inference_payload_inside_params()
    test_startup_action_validation_accepts_runtime_error_template()
    test_result_processor_context_is_metadata_not_arguments()
    test_error_processor_model_override_stays_inside_params()
    if _failures:
        print(f"\nFAIL: {len(_failures)} check(s) failed")
        return 1
    print("\nPASS: result/error templates submit declared params; context stays metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
