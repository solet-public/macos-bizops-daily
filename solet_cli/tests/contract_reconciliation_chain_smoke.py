"""Migration-regression support split from the focused reconciliation smoke."""

from __future__ import annotations

from types import ModuleType

from contract_reconciliation_smoke import (
    AdapterRegistry,
    CheckpointStatus,
    ContractError,
    ContractReconciliation,
    ContractReconciliationManager,
    InstanceRegistry,
    Path,
    StateConflictError,
    StateError,
    Transaction,
    _assert_r20_source_identity,
    _check,
    _empty_manifest,
    _environment,
    _launchagent_after_models_reconciliation,
    _r20_answers,
    _r20_environment,
    _raises,
    _verified_completion_result,
    contract_filenames,
    json,
    load_transaction,
    patch,
    reconcile_contract_stage_probe_state,
    run_completion_probes,
    tempfile,
)


def run_positive_and_refusals(base: ModuleType) -> None:
    """Run the positive/recovery controls against the caller's test fixture API."""

    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_positive_and_refusals()


def run_launchagent_after_models_regression(base: ModuleType) -> None:
    """Run the r20 LaunchAgent migration controls against the caller fixtures."""

    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_launchagent_after_models_reconciliation_regression()


def _run_positive_and_refusals() -> None:
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, _, destination, original = _environment(Path(raw))
        unlisted = Path(raw) / "unlisted.json"
        _empty_manifest(unlisted)
        strict = ContractReconciliationManager(
            paths=paths,
            contract_directory=destination.directory,
            manifest_path=unlisted,
        )
        _raises(
            StateConflictError,
            lambda: strict.run("fixture", dry_run=True, approved_fingerprint=None),
            "unlisted source digest is refused",
        )
        no_mapping = ContractReconciliation(
            migration_id="missing-map",
            flow_id=original.flow_id,
            source_revision=original.flow_source_revision,
            source_digest=original.flow_contract_digest,
            destination_digest=destination.contract_digest,
            stage_probe_mappings=(),
        )
        _raises(
            StateError,
            lambda: reconcile_contract_stage_probe_state(destination, original, no_mapping),
            "historical removed identity without a declared mapping is refused",
        )
        _check(
            manager.run("fixture", dry_run=True, approved_fingerprint=None).status == "preview_ready",
            "declared control remains previewable",
        )
    with tempfile.TemporaryDirectory() as raw:
        manager, _, target, _, _ = _environment(Path(raw))
        hybrid = target / "plugins/github_midwife_plugin/knowledge_base/setup_flow.schema.json"
        hybrid.write_bytes(hybrid.read_bytes() + b"\n")
        _raises(
            ContractError,
            lambda: manager.run("fixture", dry_run=True, approved_fingerprint=None),
            "hybrid target bundle is refused before reconciliation",
        )
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, target, destination, original = _environment(Path(raw))
        preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        fingerprint = str(preview.data["approval_fingerprint"])
        _check(preview.status == "preview_ready", "declared legacy identity previews")
        _check(preview.data["dry_run_writes"] == 0, "preview declares zero writes")
        drift = manager.run("fixture", dry_run=False, approved_fingerprint="sha256:" + "0" * 64)
        _check(drift.error_kind == "probe_drift", "wrong reconciliation approval is refused")
        _check(
            "approval_fingerprint" not in drift.data,
            "drift refusal does not disclose the fresh approval fingerprint",
        )
        _check(
            drift.data["migration_id"] == preview.data["migration_id"],
            "drift refusal retains reconciliation content for operator review",
        )
        result = manager.run("fixture", dry_run=False, approved_fingerprint=fingerprint)
        _check(result.status == "reconciled", "matching reconciliation approval applies")
        updated = load_transaction(paths.transaction_path("fixture"))
        _check(updated is not None, "updated journal remains readable")
        assert updated is not None
        _check(updated.flow_contract_digest == destination.contract_digest, "journal pin advances")
        _check(updated.approval_fingerprint is None, "old create approval is cleared")
        attempt = updated.stage_probe_attempts[0]
        _check(attempt["boundary"] == "entry", "declared attempt boundary normalizes")
        _check(attempt["probe_id"] == "git_checkout_valid", "attempt probe history is retained")
        _check(
            all((target / "plugins/github_midwife_plugin/knowledge_base" / filename).read_bytes() == (destination.directory / filename).read_bytes() for filename in contract_filenames()),
            "complete target bundle promotion carries every destination byte",
        )
        registry = InstanceRegistry(paths.registry_path).require("fixture")
        _check(registry.flow_contract_digest == destination.contract_digest, "registry pin advances")
        projection = json.loads((target / ".solet/install-state.json").read_text(encoding="utf-8"))
        _check(
            projection["flow_source_revision"] == original.flow_source_revision,
            "target projection retains its frozen checkout revision",
        )
        _check(original.seed == updated.seed, "seed identity remains unchanged")
        _raises(
            StateConflictError,
            lambda: manager.run("fixture", dry_run=True, approved_fingerprint=None),
            "reconciled target is not auto-reconciled again",
        )


def _run_launchagent_after_models_reconciliation_regression() -> None:
    """Rebind the post-preprobe contract to the models-owned LaunchAgent contract."""

    with tempfile.TemporaryDirectory() as raw:
        manager, paths, source, destination, original = _r20_environment(
            # R20 predates the canonical Claude Code source spelling; retain
            # the recorded historical answer while exercising its migration.
            Path(raw), session_sources=["codex_local", "claude_local"]
        )
        _assert_r20_source_identity(original, source, destination)
        first_preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        second_preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        _check(
            first_preview.data["migration_id"]
            == "macos-repository-setup-postgresql-preprobe-to-launchagent-after-models-reconciliation-v1",
            "post-preprobe source selects the launchagent-after-models destination entry",
        )
        _check(
            first_preview.data["approval_fingerprint"] == second_preview.data["approval_fingerprint"],
            "identical r20 dry-run previews preserve the approval fingerprint",
        )
        _check(
            first_preview.data["dry_run_writes"] == 0 and second_preview.data["dry_run_writes"] == 0,
            "r20 dry-run previews declare zero writes",
        )
        applied = manager.run(
            "fixture",
            dry_run=False,
            approved_fingerprint=str(first_preview.data["approval_fingerprint"]),
        )
        _check(applied.status == "reconciled", "approved launchagent-after-models reconciliation applies")
        updated = load_transaction(paths.transaction_path("fixture"))
        _check(updated is not None, "launchagent-after-models result remains readable")
        assert updated is not None
        _check(updated.flow_contract_digest == destination.contract_digest, "journal binds models-owned LaunchAgent digest")
        _check(updated.approval_fingerprint is None, "approval does not survive contract replacement")
        _check(
            updated.completion.get("codex_session_roots_readable") is CheckpointStatus.PENDING
            and updated.completion.get("claude_session_roots_readable") is CheckpointStatus.PENDING,
            "selected session sources retain newly required destination checks as pending",
        )
        _check(
            updated.completion.get("python_version_valid") is CheckpointStatus.VERIFIED,
            "an already-verified r20 completion check remains verified",
        )
        _check(
            updated.answers["decisions"]["session_sources"] == ["codex_local", "claude_local"],  # type: ignore[index]
            "reconciliation preserves selected session-source state",
        )
        _check(
            Transaction.from_dict(updated.to_dict()).completion == updated.completion,
            "rebuilt destination completion membership survives journal validation",
        )
        registry = AdapterRegistry(target=Path(updated.target), base_python=None)
        with patch(
            "solet_manager.completion_verifier.invoke_adapter",
            side_effect=lambda _registry, *, runner, request: _verified_completion_result(
                request.request_id, request.operation_id
            ),
        ):
            _, checks = run_completion_probes(destination, updated, registry, paths)
        scheduled = {str(check["id"]) for check in checks if isinstance(check, dict)}
        _check(
            {"codex_session_roots_readable", "claude_session_roots_readable"} <= scheduled,
            "post-apply completion execution schedules the newly pending destination checks",
        )
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, _, _, _ = _r20_environment(Path(raw), session_sources=[])
        preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        manager.run(
            "fixture",
            dry_run=False,
            approved_fingerprint=str(preview.data["approval_fingerprint"]),
        )
        inactive = load_transaction(paths.transaction_path("fixture"))
        _check(inactive is not None, "inactive launchagent-after-models result remains readable")
        assert inactive is not None
        _check(
            "codex_session_roots_readable" not in inactive.completion
            and "claude_session_roots_readable" not in inactive.completion,
            "deselected destination session-source controls remain absent",
        )
    with tempfile.TemporaryDirectory() as raw:
        _, paths, source, destination, original = _r20_environment(
            Path(raw), session_sources=[]
        )
        registry = AdapterRegistry(target=Path(original.target), base_python=None)
        with patch(
            "solet_manager.completion_verifier.invoke_adapter",
            side_effect=lambda _registry, *, runner, request: _verified_completion_result(
                request.request_id, request.operation_id
            ),
        ):
            historical, _ = run_completion_probes(source, original, registry, paths)
        changed_answers = _r20_answers(
            Path(historical.target), session_sources=[], autostart="disabled"
        )
        pruned = reconcile_contract_stage_probe_state(
            destination,
            historical.with_answers(changed_answers),  # type: ignore[arg-type]
            _launchagent_after_models_reconciliation(),
        )
        _check(
            "launchagent_running" not in pruned.completion,
            "a destination-inactive completion binding is pruned",
        )
        _check(
            any(item.get("operation_id") == "launchagent_running" for item in pruned.operation_attempts),
            "a pruned completion binding retains its historical attempt",
        )
        _check(
            Transaction.from_dict(pruned.to_dict()).completion == pruned.completion,
            "pruned completion history survives transaction round-trip",
        )
