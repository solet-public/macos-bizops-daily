"""Base-Python bridge for the seven closed LM Studio provisioning routes."""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from typing import Any

from .models import AdapterRuntime
from .protocol import Request, result, run_public


def _plugin_source_root() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins" / "github_midwife_plugin" / "src"


def _load_handlers() -> tuple[Any, Any, Any, Any, Any]:
    """Load only the stdlib-safe LM Studio adapter modules from this bootstrap."""

    source_root = _plugin_source_root()
    if not source_root.is_dir():
        raise RuntimeError("LM Studio adapter source is absent from the bootstrap checkout")
    source_text = str(source_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    provisioning = importlib.import_module("github_midwife_plugin.lm_studio_provisioning")
    contract = importlib.import_module("github_midwife_plugin.setup_adapter_contract")
    runtime_module = importlib.import_module("github_midwife_plugin.setup_adapter_runtime")
    return (
        provisioning.provision,
        provisioning.probe,
        contract.AdapterRequest,
        runtime_module.SystemRuntime,
        runtime_module.bounded_command_outcome,
    )


def lm_studio_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    """Run one LM Studio operation with the manager-selected pre-venv Python."""

    try:
        provision, probe, request_type, system_runtime_type, bounded_outcome = _load_handlers()

        class BootstrapLMStudioRuntime(system_runtime_type):
            def __init__(self) -> None:
                super().__init__(home=Path.home())

            def run(
                self,
                argv: tuple[str, ...],
                *,
                timeout_seconds: int,
                cwd: Path | None = None,
                extra_env: dict[str, str] | None = None,
                input_text: str | None = None,
                output_limit: int = 4096,
            ) -> Any:
                started = time.monotonic()
                completed = run_public(
                    runtime,
                    list(argv),
                    timeout=timeout_seconds,
                    cwd=cwd,
                    env=self._environment(extra_env),
                    input=input_text,
                )
                duration_ms = int((time.monotonic() - started) * 1000)
                if completed is None:
                    return bounded_outcome(
                        returncode=None,
                        timed_out=True,
                        duration_ms=duration_ms,
                        stdout="",
                        stderr="",
                        output_limit=output_limit,
                    )
                return bounded_outcome(
                    returncode=completed.returncode,
                    timed_out=False,
                    duration_ms=duration_ms,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    output_limit=output_limit,
                )

        typed_request = request_type.from_dict(dict(request))
        handler = probe if request["operation_id"].startswith("lm_studio_") else provision
        return dict(handler(typed_request, BootstrapLMStudioRuntime()))
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        return result(
            request,
            status="blocked",
            error_kind="lm_studio_bootstrap_adapter_unavailable",
            retry_safe=False,
            repair=f"Repair the closed pre-venv LM Studio adapter before resuming: {type(exc).__name__}.",
        )
