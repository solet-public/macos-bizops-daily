"""Red-first: iss_b37a1814 — a same-round dependent op must not deadlock preview.

At the models stage, ``acquire_coreai_asset`` is ready to run (its own
precondition is unmet only because IT is the fix for that) while its sibling
``configure_coreai_embeddings`` is genuinely blocked by the very same absent
asset. The flow contract already declares the relationship: probe
``coreai_asset_verified`` names ``acquire_coreai_asset`` as its
``remediation_operation_refs``. Before this fix, ``preview_engine`` ignored
that declaration at the operation-probe layer (it only honored it for stage
entry/exit boundary probes), so the sibling's block was folded into
``unresolved_actions`` and the whole preview stuck at ``awaiting_user`` —
``cli_commands.run_create_command`` refuses to proceed past a preview whose
status is not exactly ``preview_ready`` (``cli_commands.py:64``), so the same
approval fingerprint returns forever and ``acquire_coreai_asset`` is never
even attempted.

Run directly::

    .venv/bin/python3 solet_cli/tests/apple_preview_deadlock_smoke.py
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))

from github_midwife_plugin import apple_setup_adapter as apple  # noqa: E402
from github_midwife_plugin import setup_adapter  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from solet_manager import preview_engine  # noqa: E402
from solet_manager.adapter_protocol import OperationResult  # noqa: E402
from solet_manager.adapters import AdapterRegistry  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.models import CheckpointStatus, JsonValue  # noqa: E402
from solet_manager.operation_records import operation_request  # noqa: E402
from solet_manager.plan_builder import (  # noqa: E402
    PlannedOperation,
    SetupPlan,
    _planned_operation,
    ordered_operations,
    selected_operation_ids,
)
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402

_CONTRACTS = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {label}")


class _NoHost:
    home = Path("/nonexistent-home")

    def run(self, argv: tuple[str, ...], **_: object) -> object:
        raise AssertionError(f"a probe-phase call ran a host command: {argv}")

    def http_json(self, url: str, **_: object) -> object:
        raise AssertionError(f"a probe-phase call reached an endpoint: {url}")

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        raise AssertionError(f"a probe-phase call wrote through the runtime: {path}")


def _bundle() -> ContractBundle:
    return ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)


def _transaction(bundle: ContractBundle, target: Path) -> Transaction:
    return Transaction.create(
        name="preview-deadlock",
        target=target,
        input_fingerprint="sha256:" + "d" * 64,
        answers={},
        seed=SeedLock(
            "https://example.invalid/seed.git",
            "fixture",
            "a" * 40,
            "b" * 40,
            "c" * 64,
            "fixture",
        ),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=("completion",),
    )


def _operation(bundle: ContractBundle, stage_id: str, operation_id: str) -> PlannedOperation:
    return _planned_operation(bundle, stage_id, operation_id, {}, {})


def _real_invoke_adapter(registry: object, *, runner: str, request: object) -> OperationResult:
    """Route a probe through the REAL flow-declared handler, no subprocess.

    Mirrors exactly what ``AdapterRegistry``'s subprocess dispatch does on the
    wire (``OperationRequest.to_dict()`` -> ``AdapterRequest.from_dict()`` ->
    ``setup_adapter.dispatch_request()`` -> ``OperationResult.from_dict()``),
    without paying for a subprocess or a target-local venv.
    """
    del registry, runner
    adapter_request = AdapterRequest.from_dict(request.to_dict())  # type: ignore[attr-defined]
    raw = setup_adapter.dispatch_request(adapter_request, _NoHost())
    return OperationResult.from_dict(raw, request)  # type: ignore[arg-type]


def _seed_asset_manifest(target: Path) -> None:
    """Give the target a structurally valid, but never-downloaded, asset pin."""
    manifest = {"schema_version": 1, "model_id": apple._MODEL_ID, "files": {}}  # noqa: SLF001
    payload = json.dumps(manifest, sort_keys=True).encode("utf-8")
    path = target / apple._ASSET_MANIFEST  # noqa: SLF001
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return digest


_APPLE_DECISIONS: dict[str, JsonValue] = {
    "setup_profile": "macos-bizops",
    "autostart": "enabled",
    "embeddings_implementation": "coreai",
    "inference_implementation": "apple_foundation_models",
}


def _real_models_plan(bundle: ContractBundle) -> SetupPlan:
    """The models-stage plan the REAL plan builder selects for the Apple route.

    iss_b37a1814 recurred on a macOS 27 guest because the first fix was
    proven against a hand-picked two-operation plan that omitted
    ``install_launchagent``; this derives the plan from the shipped flow.
    """
    decisions = dict(_APPLE_DECISIONS)
    operations = tuple(
        operation
        for operation in ordered_operations(bundle, selected_operation_ids(bundle, decisions), {}, decisions)
        if operation.stage_id == "models"
    )
    return SetupPlan(answers={}, operations=operations, unresolved_decisions=(), unresolved_consents=())


def _apple_target(root: Path, name: str, *, corrupt_asset: bool) -> tuple[Path, str, str]:
    """A post-Genesis target whose roster loads Core AI and whose asset is not yet acquired."""
    target = root / name
    target.mkdir()
    if corrupt_asset:
        (target / apple._ASSET_ROOT / apple._ASSET_DIRECTORY).mkdir(parents=True)  # noqa: SLF001
    digest = _seed_asset_manifest(target)
    manifest = target / "profile" / "config" / "manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("plugins:\n  - coreai_embeddings_plugin\n  - macos_inference_plugin\n", encoding="utf-8")
    inference = target / apple._INFERENCE_SOURCE  # noqa: SLF001
    inference.parent.mkdir(parents=True)
    payload = json.dumps({"model": "apple-system"}).encode("utf-8")
    inference.write_bytes(payload)
    return target, digest, hashlib.sha256(payload).hexdigest()


def _real_models_stage_checks(bundle: ContractBundle, root: Path) -> None:
    registry = AdapterRegistry(target=root, base_python=None)
    plan = _real_models_plan(bundle)
    ids = [operation.operation_id for operation in plan.operations]
    _check(
        ids.index("acquire_coreai_asset") < ids.index("install_launchagent"),
        f"the shipped models stage schedules acquire before the LaunchAgent: {ids}",
    )
    target, digest, inference_digest = _apple_target(root, "real-plan", corrupt_asset=False)
    with (
        patch.object(apple, "_ASSET_MANIFEST_SHA256", digest),
        patch.object(apple, "_INFERENCE_SOURCE_SHA256", inference_digest),
        patch.object(preview_engine, "invoke_adapter", _real_invoke_adapter),
    ):
        results, failures = preview_engine._probe_operations(  # noqa: SLF001
            _transaction(bundle, target), bundle, plan, registry
        )
    launchagent = results["install_launchagent"]
    _check(
        launchagent.checkpoint_status is CheckpointStatus.BLOCKED
        and launchagent.error_kind == "coreai_asset_unavailable",
        "install_launchagent's real adapter defers on the unacquired Core AI asset",
    )
    _check(
        failures == [],
        f"the real models stage must reach preview_ready so acquire_coreai_asset runs: {failures}",
    )
    # Fail-closed: a different block reason is not excused by the scheduled acquire.
    other = dataclasses.replace(launchagent, error_kind="launchagent_bootstrap_failed")
    with (
        patch.object(apple, "_ASSET_MANIFEST_SHA256", digest),
        patch.object(apple, "_INFERENCE_SOURCE_SHA256", inference_digest),
        patch.object(preview_engine, "invoke_adapter", _real_invoke_adapter),
    ):
        excused = preview_engine._scheduled_peer_remediates_block(  # noqa: SLF001
            _transaction(bundle, target), bundle, plan, registry, other, set(ids) - {"install_launchagent"}, {}
        )
    _check(not excused, "a LaunchAgent block with another reason stays a genuine unresolved action")
    # Fail-closed: a corrupt asset blocks acquire itself, so nothing excuses the LaunchAgent.
    corrupt, corrupt_digest, corrupt_inference = _apple_target(root, "corrupt-plan", corrupt_asset=True)
    with (
        patch.object(apple, "_ASSET_MANIFEST_SHA256", corrupt_digest),
        patch.object(apple, "_INFERENCE_SOURCE_SHA256", corrupt_inference),
        patch.object(preview_engine, "invoke_adapter", _real_invoke_adapter),
    ):
        _, corrupt_failures = preview_engine._probe_operations(  # noqa: SLF001
            _transaction(bundle, corrupt), bundle, plan, registry
        )
    _check(
        corrupt_failures == ["acquire_coreai_asset", "configure_coreai_embeddings", "install_launchagent"],
        f"a corrupt asset keeps acquire and its dependents unresolved: {corrupt_failures}",
    )


def main() -> int:
    bundle = _bundle()
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "target"
        target.mkdir()
        digest = _seed_asset_manifest(target)
        transaction = _transaction(bundle, target)
        registry = AdapterRegistry(target=target, base_python=None)
        acquire = _operation(bundle, "models", "acquire_coreai_asset")
        configure = _operation(bundle, "models", "configure_coreai_embeddings")
        plan = SetupPlan(
            answers={},
            operations=(acquire, configure),
            unresolved_decisions=(),
            unresolved_consents=(),
        )

        with patch.object(apple, "_ASSET_MANIFEST_SHA256", digest):
            # Ground truth: probing each operation in isolation reproduces the
            # exact real shape of the defect -- acquire is runnable, configure
            # is genuinely BLOCKED by the same absent asset.
            acquire_request = operation_request(
                transaction, bundle, acquire, phase="probe", probe_purpose="preview",
                approval=None, attempt=1,
            )
            acquire_probe = _real_invoke_adapter(registry, runner=acquire.runner, request=acquire_request)
            _check(
                acquire_probe.checkpoint_status is CheckpointStatus.PENDING
                and bool(acquire_probe.planned_actions),
                "acquire_coreai_asset's own precondition is met by itself: pending, not blocked",
            )
            configure_request = operation_request(
                transaction, bundle, configure, phase="probe", probe_purpose="preview",
                approval=None, attempt=1,
            )
            configure_probe = _real_invoke_adapter(registry, runner=configure.runner, request=configure_request)
            _check(
                configure_probe.checkpoint_status is CheckpointStatus.BLOCKED
                and configure_probe.error_kind == "coreai_asset_unavailable",
                "configure_coreai_embeddings is genuinely blocked while the asset is absent",
            )

            # RED discriminator: the raw per-operation signal alone (the only
            # thing the pre-fix code consulted) says configure_coreai_embeddings
            # is unresolved -- with no awareness that acquire_coreai_asset, its
            # own declared remediation, is scheduled in this very round.
            _check(
                preview_engine._operation_probe_blocks(  # noqa: SLF001
                    configure.requires_confirmation, configure_probe
                ),
                "the raw per-operation signal alone reports configure_coreai_embeddings blocked",
            )

            # GREEN: the real preview_engine._probe_operations, the function
            # cli_commands.run_create_command's status gate depends on
            # transitively, resolves the round without deadlocking.
            with patch.object(preview_engine, "invoke_adapter", _real_invoke_adapter):
                results, failures = preview_engine._probe_operations(  # noqa: SLF001
                    transaction, bundle, plan, registry
                )
            _check(
                failures == ["acquire_coreai_asset"] or failures == [],
                f"configure_coreai_embeddings must not be a genuine unresolved_action: {failures}",
            )
            _check(
                "configure_coreai_embeddings" not in failures,
                "the sibling's block, remediated by acquire_coreai_asset in this same round, "
                "must not force the whole preview off preview_ready",
            )
            _check(
                results["acquire_coreai_asset"].checkpoint_status is CheckpointStatus.PENDING
                and bool(results["acquire_coreai_asset"].planned_actions),
                "acquire_coreai_asset's own planned download still surfaces for approval",
            )
            _check(
                results["configure_coreai_embeddings"].checkpoint_status is CheckpointStatus.BLOCKED,
                "configure_coreai_embeddings's real (still-blocked) probe result is preserved for "
                "diagnostics, only its effect on unresolved_actions changed",
            )

            # Negative control 1: an unrelated genuinely-blocked operation (no
            # declared remediation reachable this round) still refuses, named.
            corrupt_target = Path(raw) / "corrupt-target"
            corrupt_target.mkdir()
            (corrupt_target / apple._ASSET_ROOT / apple._ASSET_DIRECTORY).mkdir(parents=True)  # noqa: SLF001
            _seed_asset_manifest(corrupt_target)
            corrupt_transaction = _transaction(bundle, corrupt_target)
            with patch.object(preview_engine, "invoke_adapter", _real_invoke_adapter):
                lone_plan = SetupPlan(
                    answers={}, operations=(configure,), unresolved_decisions=(), unresolved_consents=(),
                )
                _, lone_failures = preview_engine._probe_operations(  # noqa: SLF001
                    corrupt_transaction, bundle, lone_plan, registry
                )
            _check(
                lone_failures == ["configure_coreai_embeddings"],
                "with no remediating peer scheduled this round, the genuine block still refuses",
            )

        # Negative control 2: an operation blocked for a reason its own
        # declared precondition probe does NOT name a remediation for must
        # still refuse even alongside a runnable peer (a non-precondition
        # BLOCKED probe, e.g. failed launchagent activation).
        install_launchagent = _operation(bundle, "models", "install_launchagent")
        _check(
            not preview_engine._precondition_remediation_scheduled(  # noqa: SLF001
                bundle, install_launchagent, {"acquire_coreai_asset"}
            ),
            "an operation with no precondition_probe_ids is never spuriously remediated",
        )

        # Named-reason repair/message: the preview no longer returns a bare
        # "resolve everything" refusal when it genuinely does stay blocked.
        named = preview_engine._unresolved_action_repair(  # noqa: SLF001
            ["configure_coreai_embeddings"],
            {"configure_coreai_embeddings": configure_probe},
        )
        _check(
            "configure_coreai_embeddings" in named and "coreai_asset_unavailable" in named,
            "a genuine refusal names the blocking operation and its reason, not a generic message",
        )

        _real_models_stage_checks(bundle, Path(raw))

    print(f"apple_preview_deadlock_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
