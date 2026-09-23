"""Frozen preactivation decision-barrier discrimination and census proof."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import cast

from discovered_decision_support import (
    FakeAdapter,
    _apply_current_frontier,
    _check,
    _prepare,
    _set_invoke_adapter,
)
from solet_manager.adapters import OperationRequest, OperationResult
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle, target_contract_directory
from solet_manager.create import CreateManager
from solet_manager.flow import build_setup_plan, initial_stage_probe_statuses
from solet_manager.lifecycle import LifecycleManager
from solet_manager.models import CommandResult
from solet_manager.paths import ManagerPaths
from solet_manager.release_lock import SeedLock
from solet_manager.transaction import Transaction, canonical_sha256, write_transaction

type JsonObject = dict[str, object]

_PROTECTED_NAMES = (
    "fixture-alpha",
    "fixture-beta",
    "fixture-gamma",
    "fixture-delta",
    "fixture-epsilon",
    "fixture-zeta",
    "fixture-eta",
)
_ARTIFACT_PATHS = {
    "service_label": "launchctl/local.solet.fixture",
    "router_label": "launchctl/local.solet.fixture.router",
    "service_plist": "LaunchAgents/local.solet.fixture.plist",
    "router_plist": "LaunchAgents/local.solet.fixture.router.plist",
    "service_process": "processes/service.pid",
    "router_process": "processes/router.pid",
    "runtime_listener": "runtime/listener.port",
    "runtime_socket": "runtime/router.sock",
    "health_endpoint": "runtime/health.json",
    "shell_zsh": "shell/.zshrc",
    "shell_bash": "shell/.bash_profile",
    "named_launcher": "bin/fixture",
    "postgres_role": "postgres/role.fixture",
    "postgres_database": "postgres/database.fixture",
}
_PERSISTENT_KEYS = frozenset(_ARTIFACT_PATHS)
_MANAGED_BLOCK = "# >>> solet fixture >>>"
_LM_STUDIO_INPUTS: JsonObject = {"embeddings_implementation": "lm_studio", "inference_implementation": "lm_studio", "lm_studio_base_url": "http://localhost:1234/v1"}
_GOLDEN_APPLY_TRACE: tuple[tuple[str, str, JsonObject], ...] = (
    ("request_homebrew_install", "setup::homebrew.request_install", {}),
    ("install_python_runtime", "setup::python.install_313", {}),
    (
        "build_instance_environment",
        "bootstrap::environment.ensure_dependency_closure",
        {},
    ),
    ("install_codex_cli", "setup::coding_agents.install_codex", {}),
    ("install_claude_cli", "setup::coding_agents.install_claude", {}),
    ("install_node", "setup::coding_agents.install_node", {}),
    ("install_postgresql", "bootstrap::postgres.install", {}),
    (
        "configure_postgresql",
        "bootstrap::postgres.configure_solet",
        {"solet_name": "barrier-terminal"},
    ),
    ("install_lm_studio", "setup::lm_studio.install", _LM_STUDIO_INPUTS),
    ("start_lm_studio_server", "setup::lm_studio.start_server", _LM_STUDIO_INPUTS),
    ("pull_lm_studio_embedding_model", "setup::lm_studio.pull_embedding", _LM_STUDIO_INPUTS),
    ("load_lm_studio_embedding_model", "setup::lm_studio.load_embedding", _LM_STUDIO_INPUTS),
    ("pull_lm_studio_inference_model", "setup::lm_studio.pull_inference", _LM_STUDIO_INPUTS),
    ("load_lm_studio_inference_model", "setup::lm_studio.load_inference", _LM_STUDIO_INPUTS),
    ("install_lm_studio_login_agent", "setup::lm_studio.install_login_agent", _LM_STUDIO_INPUTS),
    (
        "run_genesis",
        "genesis::solet.run",
        {
            "autostart": "enabled",
            "clone_directory": "$TARGET",
            "setup_profile": "macos-" + "biz" + "ops",
            "solet_name": "barrier-terminal",
        },
    ),
    ("install_shell_integration", "hydration::shell.install", {}),
    (
        "configure_lm_studio_embeddings",
        "setup::models.configure_lm_studio_embeddings",
        {
            "lm_studio_base_url": "http://localhost:1234/v1",
            "model": "embedding_model.recommended",
        },
    ),
    (
        "configure_lm_studio_inference",
        "setup::models.configure_lm_studio_inference",
        {
            "lm_studio_base_url": "http://localhost:1234/v1",
            "model": "inference_model.recommended",
        },
    ),
    (
        "install_launchagent",
        "genesis::autostart.install",
        {"autostart": "enabled", "setup_profile": "macos-" + "biz" + "ops"},
    ),
    (
        "install_codex_plugin",
        "hydration::codex.install_plugin",
        {"clone_directory": "$TARGET", "solet_name": "barrier-terminal"},
    ),
    (
        "install_claude_plugin",
        "hydration::claude.install_plugin",
        {"clone_directory": "$TARGET", "solet_name": "barrier-terminal"},
    ),
)
_GOLDEN_PLANNED_ORDER = (
    "build_instance_environment",
    "configure_postgresql",
    "install_claude_cli",
    "install_codex_cli",
    "install_lm_studio",
    "install_lm_studio_login_agent",
    "install_node",
    "install_postgresql",
    "install_python_runtime",
    "load_lm_studio_embedding_model",
    "load_lm_studio_inference_model",
    "pull_lm_studio_embedding_model",
    "pull_lm_studio_inference_model",
    "request_homebrew_install",
    "start_lm_studio_server",
    "install_shell_integration",
    "run_genesis",
    "configure_lm_studio_embeddings",
    "configure_lm_studio_inference",
    "install_launchagent",
    "install_claude_plugin",
    "install_codex_plugin",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_identity(path: Path) -> JsonObject:
    if not path.exists() and not path.is_symlink():
        return {"exists": False}
    if path.is_symlink():
        return {"exists": True, "kind": "symlink", "target": str(path.readlink())}
    data = path.read_bytes()
    return {
        "exists": True,
        "kind": "file",
        "size": len(data),
        "sha256": _sha256(data),
    }


def _write_artifact(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _initialize_fixture_state(root: Path) -> None:
    _write_artifact(root / _ARTIFACT_PATHS["shell_zsh"], "zsh-before\n")
    _write_artifact(root / _ARTIFACT_PATHS["shell_bash"], "bash-before\n")
    _write_artifact(root / _ARTIFACT_PATHS["named_launcher"], "launcher-before\n")
    for name in _PROTECTED_NAMES:
        for field in ("labels", "plists", "processes", "target", "evidence"):
            _write_artifact(
                root / "protected" / name / field,
                f"{name}:{field}:preserved\n",
            )


def _protected_census(root: Path) -> JsonObject:
    return {
        name: {
            field: _file_identity(root / "protected" / name / field)
            for field in ("labels", "plists", "processes", "target", "evidence")
        }
        for name in _PROTECTED_NAMES
    }


def _artifact_census(
    root: Path,
    paths: ManagerPaths,
    name: str,
) -> JsonObject:
    artifacts = {
        key: _file_identity(root / relative)
        for key, relative in sorted(_ARTIFACT_PATHS.items())
    }
    shell_text = "".join(
        (root / _ARTIFACT_PATHS[key]).read_text(encoding="utf-8")
        for key in ("shell_zsh", "shell_bash")
    )
    return {
        "artifacts": artifacts,
        "shell_managed_block_count": shell_text.count(_MANAGED_BLOCK),
        "registry_instances": LifecycleManager(paths).list_instances().data[
            "instances"
        ],
        "transaction": _file_identity(paths.transaction_path(name)),
        "protected": _protected_census(root),
    }


def _persistent_slice(census: JsonObject) -> JsonObject:
    artifacts = cast(JsonObject, census["artifacts"])
    return {
        "artifacts": {key: artifacts[key] for key in sorted(_PERSISTENT_KEYS)},
        "shell_managed_block_count": census["shell_managed_block_count"],
        "registry_instances": census["registry_instances"],
        "protected": census["protected"],
    }


class PersistentArtifactAdapter(FakeAdapter):
    """Record actual adapter dispatch and every nested persistent fixture effect."""

    def __init__(
        self,
        artifact_root: Path,
        *,
        event_trace: list[JsonObject],
        empty_decision: str | None = None,
        fail_qualification: str | None = None,
        missing_metadata_decision: str | None = None,
    ) -> None:
        super().__init__(
            empty_decision=empty_decision,
            fail_qualification=fail_qualification,
            missing_metadata_decision=missing_metadata_decision,
        )
        self.artifact_root = artifact_root
        self.event_trace = event_trace

    def __call__(
        self,
        registry: object,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        result = super().__call__(registry, runner=runner, request=request)
        self.event_trace.append(
            {
                "event": "adapter_request",
                "phase": request.phase,
                "purpose": request.probe_purpose,
                "operation_id": request.operation_id,
            }
        )
        if (
            request.probe_purpose == "decision_discovery"
            and request.public_inputs.get("decision_id") == self.empty_decision
        ):
            self.event_trace.append(
                {
                    "event": "decision_closure_nonterminal",
                    "decision_id": self.empty_decision,
                }
            )
        if request.phase == "apply":
            self.event_trace.append(
                {"event": "apply_dispatch", "operation_id": request.operation_id}
            )
            self._record_nested_effects(request.operation_id)
        return result

    def _record_nested_effects(self, operation_id: str) -> None:
        effects = {
            "configure_postgresql": (
                ("postgres_role", "create_role"),
                ("postgres_database", "create_database"),
            ),
            "run_genesis": (
                ("service_label", "install_start_service"),
                ("service_process", "start_service_process"),
                ("router_label", "install_start_router"),
                ("router_process", "start_router_process"),
                ("runtime_listener", "create_runtime_listener"),
                ("runtime_socket", "create_runtime_socket"),
                ("health_endpoint", "start_health_endpoint"),
            ),
            "install_shell_integration": (
                ("shell_zsh", "modify_zsh_managed_block"),
                ("shell_bash", "modify_bash_managed_block"),
                ("named_launcher", "replace_named_launcher"),
            ),
            "install_launchagent": (
                ("service_plist", "write_service_launchagent"),
                ("router_plist", "write_router_launchagent"),
            ),
        }.get(operation_id, ())
        for artifact, mutator in effects:
            path = self.artifact_root / _ARTIFACT_PATHS[artifact]
            if artifact in {"shell_zsh", "shell_bash"}:
                prior = path.read_text(encoding="utf-8")
                _write_artifact(path, prior + _MANAGED_BLOCK + "\n")
            else:
                _write_artifact(path, operation_id + "\n")
            self.event_trace.append(
                {
                    "event": "persistent_mutator",
                    "mutator": mutator,
                    "artifact": artifact,
                }
            )


def _stable_result(result: CommandResult) -> JsonObject:
    return cast(JsonObject, json.loads(json.dumps(result.to_dict(), sort_keys=True)))


def _decision_error_ids(result: CommandResult) -> set[str]:
    raw = result.data.get("decision_errors")
    if not isinstance(raw, list):
        return set()
    return {
        str(item["id"])
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _has_prohibited_effect(trace: list[JsonObject]) -> bool:
    return any(item.get("event") == "persistent_mutator" for item in trace)


def _advance_to_models(
    manager: CreateManager,
    config: CreateConfig,
    selections: dict[str, str],
) -> None:
    dependencies, dependencies_result = _apply_current_frontier(
        manager,
        config,
        decision_selections=selections,
    )
    _check(
        dependencies.data["frontier"] == ["system_dependencies"]
        and dependencies_result.error_kind == "next_stage_preview_required",
        "model barrier advances through the dependency frontier",
    )
    genesis, genesis_result = _apply_current_frontier(
        manager,
        config,
        decision_selections=selections,
    )
    _check(
        genesis.data["frontier"] == ["genesis"]
        and genesis_result.error_kind == "next_stage_preview_required",
        "model barrier advances through the genesis frontier",
    )


def _model_matrix_case(
    root: Path,
    *,
    name: str,
    empty_decision: str | None = None,
    fail_qualification: str | None = None,
    missing_metadata_decision: str | None = None,
    inference_implementation: str = "lm_studio",
    selections: dict[str, str] | None = None,
) -> JsonObject:
    if selections is None:
        raise AssertionError("model barrier scenarios require explicit selections")
    fixture_root = root / f"{name}-fixture"
    _initialize_fixture_state(fixture_root)
    paths, config, manager, _transaction = _prepare(
        root,
        name=name,
        inference_implementation=inference_implementation,
    )
    trace: list[JsonObject] = []
    adapter = PersistentArtifactAdapter(
        fixture_root,
        event_trace=trace,
        empty_decision=empty_decision,
        fail_qualification=fail_qualification,
        missing_metadata_decision=missing_metadata_decision,
    )
    _set_invoke_adapter(adapter)
    _advance_to_models(manager, config, selections)
    before = _artifact_census(fixture_root, paths, config.name)
    trace_start = len(trace)
    preview = manager.preview(config, decision_selections=selections)
    after = _artifact_census(fixture_root, paths, config.name)
    return {
        "frontier": preview.data.get("frontier"),
        "status": preview.status,
        "error_kind": preview.error_kind,
        "decision_ids": sorted(_decision_error_ids(preview)),
        "zero_effect_after_models": (
            not _has_prohibited_effect(trace[trace_start:])
            and _persistent_slice(before) == _persistent_slice(after)
        ),
        "inference_probed": any(
            request.public_inputs.get("decision_id") == "inference_model"
            for request in adapter.requests
        ),
    }


def _inactive_inference_case(root: Path) -> JsonObject:
    """The supported free profile exercises an inactive inference model at models."""
    name = "barrier-inference-free"
    fixture_root = root / f"{name}-fixture"
    _initialize_fixture_state(fixture_root)
    paths = ManagerPaths.resolve(explicit_home=root / f"manager-{name}", home=root)
    target = root / "Solets" / name
    contracts = target_contract_directory(target)
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "plugins/github_midwife_plugin/knowledge_base",
        contracts,
    )
    seed = SeedLock("https://github.com/solet-public/free.git", "fixture", "a" * 40, "b" * 40, "c" * 64, "free")
    bundle = ContractBundle.load(source_revision=seed.commit, directory=contracts)
    config = CreateConfig(name=name, target=target, autostart=True)
    plan = build_setup_plan(
        bundle=bundle, config=config, seed=seed, journal_path=paths.transaction_path(name),
        decision_selections={"coding_agents": ["codex", "claude_code"], "session_sources": []},
    )
    transaction = Transaction.create(
        name=name, target=target, input_fingerprint=canonical_sha256(config.to_identity_dict()),
        answers=plan.answers, seed=seed, flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision, flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages), completion_probe_ids=bundle.completion_probe_ids,
        stage_probe_statuses=initial_stage_probe_statuses(bundle, plan.answers),
    )
    write_transaction(paths.transaction_path(name), transaction)
    seed_lock = root / "free.seed.lock.json"
    seed_lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": seed.repository,
                "release_tag": seed.release_tag,
                "commit": seed.commit,
                "tree_hash": seed.tree_hash,
                "archive_sha256": seed.archive_sha256,
                "profile": seed.profile,
            }
        ),
        encoding="utf-8",
    )
    manager = CreateManager(paths=paths, contract_directory=None, seed_lock_path=seed_lock)
    trace: list[JsonObject] = []
    adapter = PersistentArtifactAdapter(fixture_root, event_trace=trace)
    _set_invoke_adapter(adapter)
    selections = {"embedding_model": "embedding_model.recommended"}
    dependencies, result = _apply_current_frontier(manager, config, decision_selections=selections)
    _check(
        dependencies.data["frontier"] == ["system_dependencies"]
        and result.error_kind == "next_stage_preview_required",
        "free profile advances the dependency frontier",
    )
    genesis, result = _apply_current_frontier(manager, config, decision_selections=selections)
    _check(
        genesis.data["frontier"] == ["genesis"]
        and result.error_kind == "next_stage_preview_required",
        "free profile advances genesis before the model barrier",
    )
    before = _artifact_census(fixture_root, paths, name)
    trace_start = len(trace)
    preview = manager.preview(config, decision_selections=selections)
    after = _artifact_census(fixture_root, paths, name)
    _check(preview.data.get("frontier") == ["models"], "free inverse reaches the actual model barrier")
    return {
        "frontier": preview.data.get("frontier"),
        "status": preview.status,
        "error_kind": preview.error_kind,
        "decision_ids": sorted(_decision_error_ids(preview)),
        "zero_effect_after_models": (
            not _has_prohibited_effect(trace[trace_start:])
            and _persistent_slice(before) == _persistent_slice(after)
        ),
        "inference_probed": any(
            request.public_inputs.get("decision_id") == "inference_model"
            or "inference" in request.operation_ref
            or request.operation_ref == "setup::models.qualify_structured_actions"
            for request in adapter.requests
        ),
    }


def _refused_arm(root: Path, selections: dict[str, str]) -> JsonObject:
    fixture_root = root / "barrier-blocked-fixture"
    _initialize_fixture_state(fixture_root)
    paths, config, manager, _transaction = _prepare(root, name="barrier-blocked")
    trace: list[JsonObject] = []
    adapter = PersistentArtifactAdapter(
        fixture_root,
        event_trace=trace,
        empty_decision="embedding_model",
    )
    _set_invoke_adapter(adapter)
    _advance_to_models(manager, config, selections)
    before = _artifact_census(fixture_root, paths, config.name)
    trace_start = len(trace)
    journal_before = paths.transaction_path(config.name).read_bytes()
    preview = manager.preview(config, decision_selections=selections)
    argv_fingerprint = str(preview.data.get("approval_fingerprint", "not-approved"))
    first = manager.create(
        config,
        approved_fingerprint=argv_fingerprint,
        decision_selections=selections,
    )
    middle = _artifact_census(fixture_root, paths, config.name)
    second = manager.create(
        config,
        approved_fingerprint=argv_fingerprint,
        decision_selections=selections,
    )
    after = _artifact_census(fixture_root, paths, config.name)
    journal_after = paths.transaction_path(config.name).read_bytes()
    prohibited = [
        item
        for item in trace[trace_start:]
        if item.get("event") == "persistent_mutator"
    ]
    return {
        "fixture": "barrier-blocked",
        "argv": [
            "create",
            "--name",
            "barrier-blocked",
            "--decision",
            "embedding_model=embedding_model.recommended",
        ],
        "preview_status": preview.status,
        "preview_frontier": preview.data.get("frontier"),
        "first": _stable_result(first),
        "second": _stable_result(second),
        "same_refusal": _stable_result(first) == _stable_result(second),
        "decision_named": _decision_error_ids(first) == {"embedding_model"},
        "journal_byte_identical": journal_before == journal_after,
        "model_activation_not_started": (
            before["transaction"] == after["transaction"]
            and after["registry_instances"] == before["registry_instances"]
        ),
        "before": before,
        "after_first": middle,
        "after_second": after,
        "trace": trace,
        "prohibited_effects": prohibited,
        "compensation_or_cleanup": [
            item
            for item in trace
            if item.get("event") in {"compensation", "cleanup", "adopt", "repair"}
        ],
        "zero_prohibited_effects_after_models": (
            not prohibited
            and _persistent_slice(before) == _persistent_slice(middle)
            and _persistent_slice(before) == _persistent_slice(after)
        ),
    }


def _canonical_request(request: OperationRequest) -> tuple[str, str, JsonObject]:
    public_inputs = cast(JsonObject, json.loads(json.dumps(request.public_inputs)))
    if public_inputs.get("clone_directory") == str(request.target):
        public_inputs["clone_directory"] = "$TARGET"
    return request.operation_id, request.operation_ref, public_inputs


def _expected_planned_actions(operation_ids: tuple[str, ...]) -> list[JsonObject]:
    return [
        {
            "operation_id": operation_id,
            "id": f"host.{operation_id}",
            "title": f"Apply {operation_id}",
            "mutation_kind": "fixture_mutation",
            "target": f"$TARGET/{operation_id}",
            "requires_confirmation": True,
            "condition_or_evidence_ref": f"{operation_id}.required",
        }
        for operation_id in operation_ids
    ]


def _closure_matrix(root: Path, selections: dict[str, str]) -> JsonObject:
    outside = dict(selections)
    outside["embedding_model"] = "embedding_model.outside-permitted-set"
    return {
        "platform_expectation_missing": _model_matrix_case(
            root,
            name="barrier-missing-expectation",
            missing_metadata_decision="embedding_model",
            selections=selections,
        ),
        "empty_embedding_candidates": _model_matrix_case(
            root,
            name="barrier-empty-embedding",
            empty_decision="embedding_model",
            selections=selections,
        ),
        "failed_embedding_qualification": _model_matrix_case(
            root,
            name="barrier-failed-embedding",
            fail_qualification="embedding_model",
            selections=selections,
        ),
        "selected_outside_permitted_set": _model_matrix_case(
            root,
            name="barrier-outside-selection",
            selections=outside,
        ),
        "conditional_inference_empty": _model_matrix_case(
            root,
            name="barrier-empty-inference",
            empty_decision="inference_model",
            selections=selections,
        ),
        "conditional_inference_unqualified": _model_matrix_case(
            root,
            name="barrier-failed-inference",
            fail_qualification="inference_model",
            selections=selections,
        ),
        # Retain the consumer key; the inverse now uses the legal free topology.
        "inverse_inference_none": _inactive_inference_case(root),
    }


def run_barrier_scenario(root: Path) -> JsonObject:
    """Load the focused case suite after this module has finished initializing."""

    from discovered_decision_barrier_cases import run_barrier_scenario as run_cases

    return run_cases(root)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        run_barrier_scenario(Path(raw))
    print("discovered_decision_barrier_scenarios OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
