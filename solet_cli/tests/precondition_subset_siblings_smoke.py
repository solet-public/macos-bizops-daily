#!/usr/bin/env python3
"""Regression checks for the four precondition-subset sibling operations."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))
sys.path.insert(0, str(_ROOT / "solet_cli" / "tests"))

from operation_probe_adapter_checks import _operation, _result, _transaction  # noqa: E402
from solet_manager import contracts, operation_executor  # noqa: E402
from solet_manager.adapters import OperationRequest, OperationResult  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.flow import PlannedOperation  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402

_CONTRACTS = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
_LEGACY_FLOW = (
    _ROOT
    / "solet_cli/tests/fixtures/contracts/reconcile_contract_legacy_4ff38b3d"
    / "macos_setup_flow.json"
)
_LEGACY_DIGEST = "sha256:67c903ea332287f2c73f036ff055b793f50072ed3af0784d68a505801c040540"
_CASES = (
    (
        "configure_postgresql",
        ("postgres_role_policy_valid", "postgres_ready", "pgvector_ready"),
        ("postgres_role_policy_valid",),
        "postgres_ready",
    ),
    (
        "install_shell_integration",
        ("fresh_shell_path_valid", "fresh_shell_python_valid"),
        ("fresh_shell_path_valid",),
        "fresh_shell_python_valid",
    ),
    (
        "configure_lm_studio_embeddings",
        ("embedding_model_qualification",),
        (),
        "embedding_model_qualification",
    ),
)
_LEGACY_PRECONDITION_OVERRIDES = {
    "configure_lm_studio_embeddings": ("embedding_request_succeeds",),
}


def _check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def _probe_result(
    blocked_probe: str,
    observed: list[str],
    _registry: object,
    *,
    runner: str,
    request: OperationRequest,
) -> OperationResult:
    del runner
    observed.append(request.operation_id)
    if request.operation_id == blocked_probe:
        return _result(
            request,
            CheckpointStatus.BLOCKED,
            error_kind=f"fixture_{blocked_probe}_false",
        )
    return _result(request, CheckpointStatus.VERIFIED)


def _run_preprobe(
    bundle: ContractBundle,
    operation: PlannedOperation,
    *,
    blocked_probe: str,
) -> tuple[OperationResult, list[str]]:
    observed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="precondition_subset_") as raw:
        transaction = replace(
            _transaction(bundle, Path(raw) / "target"),
            answers={
                "decisions": {
                    "embedding_model": "fixture-embedding",
                    "inference_model": "fixture-inference",
                }
            },
        )
        with patch.object(
            operation_executor,
            "invoke_adapter",
            side_effect=lambda registry, runner, request: _probe_result(
                blocked_probe,
                observed,
                registry,
                runner=runner,
                request=request,
            ),
        ):
            result = operation_executor._invoke_operation_probe(
                bundle=bundle,
                operation=operation,
                transaction=transaction,
                registry=object(),
                purpose="pre_apply",
                attempt=1,
            )
    return result, observed


def _check_case(
    bundle: ContractBundle,
    *,
    operation_id: str,
    expected_preconditions: tuple[str, ...],
    narrow_preconditions: tuple[str, ...],
    blocked_probe: str,
) -> None:
    operation = _operation(bundle, operation_id)
    _check(
        operation.precondition_probe_ids == expected_preconditions,
        f"{operation_id} declares every postcondition as a precondition",
    )
    narrow_operation = replace(operation, precondition_probe_ids=narrow_preconditions)
    narrow_result, narrow_observed = _run_preprobe(
        bundle,
        narrow_operation,
        blocked_probe=blocked_probe,
    )
    _check(
        narrow_result.checkpoint_status is CheckpointStatus.VERIFIED,
        f"killing mutation: narrow {operation_id} precondition would SKIP despite {blocked_probe}",
    )
    _check(
        narrow_observed == list(narrow_preconditions) or narrow_observed == [operation_id],
        f"killing mutation probes only the narrow {operation_id} declaration",
    )
    repaired_result, repaired_observed = _run_preprobe(
        bundle,
        operation,
        blocked_probe=blocked_probe,
    )
    blocked_index = expected_preconditions.index(blocked_probe) + 1
    _check(
        repaired_result.checkpoint_status is CheckpointStatus.BLOCKED
        and repaired_observed == list(expected_preconditions[:blocked_index]),
        f"repaired {operation_id} blocks for {blocked_probe} and remediates instead of SKIP",
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    for operation_id, expected, narrow, blocked in _CASES:
        _check_case(
            bundle,
            operation_id=operation_id,
            expected_preconditions=expected,
            narrow_preconditions=narrow,
            blocked_probe=blocked,
        )
    legacy_flow = json.loads(_LEGACY_FLOW.read_text(encoding="utf-8"))
    normalized = contracts._normalize_legacy_resume_flow_v1(_LEGACY_DIGEST, legacy_flow)
    for operation_id, expected, _narrow, _blocked in _CASES:
        legacy_expected = _LEGACY_PRECONDITION_OVERRIDES.get(operation_id, expected)
        _check(
            normalized["operations"][operation_id]["idempotency"]["precondition_probe_refs"]
            == list(legacy_expected),
            f"digest-pinned legacy resume preserves {operation_id} precondition semantics",
        )
    print("precondition_subset_siblings_smoke: 16/16 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
