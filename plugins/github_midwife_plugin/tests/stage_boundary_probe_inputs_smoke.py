"""Prove every stage-boundary probe receives its declared public inputs."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))

from github_midwife_plugin.setup_adapter import _ALLOWED_PUBLIC_INPUTS  # noqa: E402
from solet_manager import stage_boundaries  # noqa: E402
from solet_manager.adapters import (  # noqa: E402
    AdapterRegistry,
    OperationRequest,
    OperationResult,
)
from solet_manager.contracts import ContractBundle, startup_readiness_budget  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CONTRACT_DIR = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_SOURCE_REVISION = "a" * 40
_CHECKS = 0


class SmokeFailureError(AssertionError):
    """Raised when a boundary cannot supply one declared adapter input."""


def _check(condition: object, detail: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise SmokeFailureError(detail)


def _capture_boundary_inputs(
    bundle: ContractBundle,
    *,
    stage_id: str,
    boundary: str,
    probe_id: str,
) -> frozenset[str]:
    """Invoke the real boundary runner and capture its one adapter request."""

    captured: list[OperationRequest] = []

    def capture(
        _registry: AdapterRegistry,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        _check(runner == str(bundle.probes[probe_id]["runner"]), "runner drift")
        captured.append(request)
        return cast(OperationResult, object())

    answers = {
        "decisions": {
            "autostart": "enabled",
            "coding_agents": ["codex"],
            "embeddings_implementation": "lm_studio",
            "inference_implementation": "lm_studio",
        },
        "public_inputs": {"lm_studio_base_url": "http://127.0.0.1:1234/v1"},
    }
    transaction = cast(
        Transaction,
        SimpleNamespace(
            name="boundary-inputs",
            target=Path("/tmp/boundary-inputs"),
            answers=answers,
            answers_fingerprint=canonical_sha256(answers),
        ),
    )
    # Mirrors stage_boundaries.py:145-153: ordinary non-manager boundary probes
    # start from an empty public-input set; only a declared startup-readiness
    # exit can add inputs. Invoke that real source rather than copying its policy.
    with patch.object(stage_boundaries, "invoke_adapter", capture):
        stage_boundaries._invoke_boundary_probe(  # noqa: SLF001
            bundle=bundle,
            transaction=transaction,
            registry=cast(AdapterRegistry, object()),
            stage_id=stage_id,
            boundary=boundary,
            probe_id=probe_id,
            answers=cast(dict[str, JsonValue], {}),
            attempt=1,
        )
    _check(len(captured) == 1, f"boundary {stage_id}/{boundary}/{probe_id} did not invoke once")
    return frozenset(captured[0].public_inputs)


def _render_inputs(values: frozenset[str]) -> str:
    return ", ".join(sorted(values)) if values else "<empty>"


def main() -> int:
    bundle = ContractBundle.load(
        source_revision=_SOURCE_REVISION,
        directory=_CONTRACT_DIR,
    )
    readiness = startup_readiness_budget(bundle)
    for stage_id, stage in bundle.stages.items():
        for field_name, boundary in (("entry_probe_refs", "entry"), ("exit_probe_refs", "exit")):
            probe_ids = stage.get(field_name, [])
            _check(isinstance(probe_ids, list), f"{stage_id}.{field_name} is not a list")
            for probe_id in probe_ids:
                _check(isinstance(probe_id, str), f"{stage_id}.{field_name} has a non-string probe")
                definition = bundle.probes[probe_id]
                runner = definition["runner"]
                probe_ref = definition["probe_ref"]
                _check(isinstance(runner, str), f"probe {probe_id} has invalid runner")
                _check(isinstance(probe_ref, str), f"probe {probe_id} has invalid probe_ref")
                if runner == "manager":
                    continue
                supplied = _capture_boundary_inputs(
                    bundle,
                    stage_id=stage_id,
                    boundary=boundary,
                    probe_id=probe_id,
                )
                if boundary == "exit" and probe_id in readiness.consumer_probe_refs:
                    readiness_inputs = frozenset(
                        readiness.public_inputs(
                            consumer_probe_purpose="stage_exit",
                            consumer_probe_ref=probe_id,
                        )
                    )
                    _check(
                        supplied == readiness_inputs,
                        f"boundary {stage_id}/{boundary}/{probe_id} did not preserve actual startup-readiness inputs",
                    )
                declared = frozenset(_ALLOWED_PUBLIC_INPUTS.get(probe_ref, frozenset()))
                _check(
                    declared.issubset(supplied),
                    "boundary probe "
                    f"{stage_id}/{boundary}/{probe_id} ({probe_ref}) declares unsupported runner inputs: "
                    f"{_render_inputs(declared - supplied)}; runner supplies: {_render_inputs(supplied)}",
                )
    print(f"stage_boundary_probe_inputs_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
