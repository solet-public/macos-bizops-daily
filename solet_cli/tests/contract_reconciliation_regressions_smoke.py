"""Additional migration-regression bodies split from the focused reconciliation smoke."""

from __future__ import annotations

from types import ModuleType

from contract_reconciliation_smoke import (
    _ACTIVE_ANSWER_MIGRATION_IDS,
    _B15_HISTORICAL_RECEIPT,
    _CONTRACT_PATH,
    _LEGACY_DIGEST,
    _R18_DIGEST,
    _RAM_PREFLIGHT_DESTINATION_DIGEST,
    _RAM_PREFLIGHT_SOURCE_COMMIT,
    _RAM_PREFLIGHT_SOURCE_DIGEST,
    _RECONCILIATION_MANIFEST,
    _ROOT,
    CheckpointStatus,
    CommandResult,
    ContractBundle,
    ContractReconciliation,
    ContractReconciliationManager,
    CreateConfig,
    CreateManager,
    ExitCode,
    InstanceRegistry,
    ManagerPaths,
    Path,
    StageProbeMapping,
    StateConflictError,
    StateError,
    Transaction,
    _active_answer_migration_environment,
    _check,
    _environment,
    _fixture_file_bytes,
    _manifest,
    _pinned_r18_contract,
    _production_migrations,
    _r20_answers,
    _raises,
    _seed,
    _write_manifest_migrations,
    ceremony_module,
    contextmanager,
    contract_digest,
    contract_filenames,
    create_module,
    doctor_module,
    initial_stage_probe_statuses,
    json,
    lifecycle_start_module,
    load_contract_reconciliations,
    load_reconciliation_destination_bundle,
    load_transaction,
    patch,
    receipts_module,
    reconcile_contract_stage_probe_state,
    reconciliation_chain_module,
    reconciliation_module,
    replace,
    tempfile,
    write_transaction,
)


def run_legacy_loader_regressions(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_legacy_loader_regressions()


def run_destination_answer_validation_regression(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_destination_answer_validation_regression()


def run_answer_value_migration_regression(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_answer_value_migration_regression()


def run_unconditional_recovery_lock_regression(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_unconditional_recovery_lock_regression()


def run_destination_resolution_disclosure_regression(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_destination_resolution_disclosure_regression()


def run_destination_selection_regressions(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_destination_selection_regressions()


def run_first_use_supersession_regression(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_first_use_supersession_regression()


def run_historical_v2_per_probe_receipt_regression(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_historical_v2_per_probe_receipt_regression()


def run_recovery_control(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_recovery_control()


def run_reconciliation_chain_regressions(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_reconciliation_chain_regressions()


def run_lm_studio_source_pins(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_lm_studio_source_pins()


def run_ram_preflight_reconciliation_regression(base: ModuleType) -> None:
    globals().update(
        {name: value for name, value in vars(base).items() if not name.startswith("__")}
    )
    _run_ram_preflight_reconciliation_regression()


def _run_legacy_loader_regressions() -> None:
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, target, _, _ = _environment(Path(raw))
        record = InstanceRegistry(paths.registry_path).require("fixture")
        _check(
            manager.run("fixture", dry_run=True, approved_fingerprint=None).status == "preview_ready",
            "contract reconciliation loads the faithful legacy source bundle",
        )
        _, doctor_bundle, _ = doctor_module._doctor_context(paths, "fixture", record, target)
        _check(
            doctor_bundle.contract_digest == _LEGACY_DIGEST,
            "doctor context loads the faithful legacy source bundle",
        )
        _, start_bundle = lifecycle_start_module._start_context(paths, "fixture", record, target)
        _check(
            start_bundle.contract_digest == _LEGACY_DIGEST,
            "start context loads the faithful legacy source bundle",
        )


def _run_destination_answer_validation_regression() -> None:
    """A destination answer-schema tightening refuses before reconciliation can apply it."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        manager, _, target, destination, transaction = _environment(root)
        answer_schema_path = destination.directory / "setup_answers.schema.json"
        answer_schema = json.loads(answer_schema_path.read_text(encoding="utf-8"))
        assert isinstance(answer_schema, dict)
        required = answer_schema["required"]
        assert isinstance(required, list)
        answer_schema["required"] = [*required, "new_destination_answer"]
        answer_schema_path.write_text(
            json.dumps(answer_schema, indent=2) + "\n",
            encoding="utf-8",
        )
        tightened_destination = ContractBundle.load(
            source_revision=destination.source_revision,
            directory=destination.directory,
        )
        source_bundle = ContractBundle.load(
            source_revision=transaction.flow_source_revision,
            directory=target / _CONTRACT_PATH,
            expected_digest=transaction.flow_contract_digest,
            resume_compatibility=True,
        )
        _manifest(root / "contract_reconciliation_manifest.json", source_bundle, tightened_destination)
        try:
            manager.run("fixture", dry_run=True, approved_fingerprint=None)
        except StateConflictError as exc:
            _check(
                "new_destination_answer" in str(exc),
                "destination answer-schema tightening names the missing property",
            )
            _check(
                transaction.flow_contract_digest in str(exc),
                "destination answer mismatch names the journal contract digest",
            )
            _check(
                tightened_destination.contract_digest in str(exc),
                "destination answer mismatch names the destination contract digest",
            )
            _check(
                "destination contract moved" in str(exc),
                "destination answer mismatch names the side that moved",
            )
            _check(
                exc.repair == "Declare an answer migration or a new decision for the destination contract; do not treat the journal as corrupt.",
                "destination answer mismatch names the forward path without blaming the journal",
            )
        else:
            _check(False, "destination answer-schema tightening is refused before preview")


def _run_answer_value_migration_regression() -> None:
    """Every active bridge rewrites the retired source spelling before validation."""

    active = tuple(
        item
        for item in load_contract_reconciliations(manifest_path=_RECONCILIATION_MANIFEST)
        if item.destination_digest == _RAM_PREFLIGHT_DESTINATION_DIGEST
    )
    _check(
        {item.migration_id for item in active} == _ACTIVE_ANSWER_MIGRATION_IDS,
        "all five active bridges declare the retired Claude source migration",
    )
    for migration in active:
        _check(
            tuple(
                (item.decision_id, item.from_value, item.to_value)
                for item in migration.answer_value_migrations
            )
            == (("session_sources", "claude_local", "claude_code_local"),),
            f"{migration.migration_id} declares one canonical Claude source rewrite",
        )
        with tempfile.TemporaryDirectory() as raw:
            manager, paths, original = _active_answer_migration_environment(
                Path(raw),
                migration,
                session_sources=["claude_local"],
            )
            preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
            _check(
                preview.status == "preview_ready",
                f"{migration.migration_id} previews a persisted retired Claude source",
            )
            prepared = manager._prepare("fixture")
            _check(
                prepared.transaction.answers["decisions"]["session_sources"]  # type: ignore[index]
                == ["claude_code_local"],
                f"{migration.migration_id} rewrites only the retired Claude source value",
            )
            _check(
                load_transaction(paths.transaction_path("fixture")) == original,
                f"{migration.migration_id} preview leaves the persisted journal unchanged",
            )
            applied = manager.run(
                "fixture",
                dry_run=False,
                approved_fingerprint=str(preview.data["approval_fingerprint"]),
            )
            _check(
                applied.status == "reconciled",
                f"{migration.migration_id} persists the reviewed answer migration",
            )
            updated = load_transaction(paths.transaction_path("fixture"))
            _check(updated is not None, f"{migration.migration_id} result journal remains readable")
            assert updated is not None
            _check(
                updated.answers["decisions"]["session_sources"] == ["claude_code_local"],  # type: ignore[index]
                f"{migration.migration_id} stores only the canonical Claude source value",
            )
    with tempfile.TemporaryDirectory() as raw:
        manager, _, _ = _active_answer_migration_environment(
            Path(raw),
            active[0],
            session_sources=["claude_local", "not_a_registered_source"],
        )
        _raises(
            StateConflictError,
            lambda: manager.run("fixture", dry_run=True, approved_fingerprint=None),
            "an unknown session source remains refused after the retired value migrates",
        )


def _run_unconditional_recovery_lock_regression() -> None:
    """Dry runs take the same lock as applying runs because recovery can write."""

    with tempfile.TemporaryDirectory() as raw:
        manager, _, _, _, _ = _environment(Path(raw))
        actual = ceremony_module.instance_lock
        lock_creates: list[bool] = []

        @contextmanager
        def recording_lock(path: Path, *, create: bool):
            lock_creates.append(create)
            with actual(path, create=create):
                yield

        with patch.object(ceremony_module, "instance_lock", recording_lock):
            preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        _check(preview.status == "preview_ready", "dry-run remains previewable under recovery lock")
        _check(lock_creates == [True], "dry-run unconditionally creates the recovery lock")


def _run_destination_resolution_disclosure_regression() -> None:
    """The reviewed destination resolution is previewed and approval-bound."""

    with tempfile.TemporaryDirectory() as raw:
        manager, _, _, destination, transaction = _environment(Path(raw))
        preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        resolution = load_reconciliation_destination_bundle(
            source_revision=transaction.flow_source_revision,
            development_directory=destination.directory,
        ).resolution.to_identity_dict()
        _check(
            preview.data["destination_contract_resolution"] == resolution,
            "preview renders the exact typed destination contract resolution",
        )
        with patch.object(
            reconciliation_module,
            "_canonical_sha256",
            wraps=reconciliation_module._canonical_sha256,
        ) as fingerprint:
            manager.run("fixture", dry_run=True, approved_fingerprint=None)
        preimage = fingerprint.call_args.args[0]
        _check(
            preimage["destination_contract_resolution"] == resolution,
            "approval fingerprint covers the typed destination contract resolution",
        )


def _run_destination_selection_regressions() -> None:
    """Keep the r15 declaration and select only the installed destination digest."""

    r15_entry, r18_entry, *_ = _production_migrations()
    with tempfile.TemporaryDirectory() as raw:
        _, paths, _, destination, _ = _environment(Path(raw))
        manifest = Path(raw) / "singleton.json"
        _write_manifest_migrations(manifest, [r15_entry])
        singleton = ContractReconciliationManager(
            paths=paths,
            contract_directory=destination.directory,
            manifest_path=manifest,
        )
        preview = singleton.run("fixture", dry_run=True, approved_fingerprint=None)
        _check(
            preview.data["migration_id"] == "macos-repository-setup-r15-boundary-reconciliation-v1",
            "singleton r15 declaration remains selectable",
        )
        _check(
            json.loads(manifest.read_text(encoding="utf-8"))["migrations"][0] == r15_entry,
            "singleton regression leaves the r15 declaration untouched",
        )
        r18_bundle = _pinned_r18_contract(Path(raw) / "r18-contracts")
        missing_destination = ContractReconciliationManager(
            paths=paths,
            contract_directory=r18_bundle.directory,
            manifest_path=manifest,
        )
        try:
            missing_destination.run("fixture", dry_run=True, approved_fingerprint=None)
        except StateConflictError as exc:
            _check(
                str(exc) == f"release-declared reconciliation entries exist for this source but none target the installed contract {_R18_DIGEST}; a forward-only entry for this destination is needed",
                "source match without an installed-destination match gives the forward-only refusal",
            )
        else:
            _check(False, "source match without an installed-destination match is refused")
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, _, _, _ = _environment(Path(raw))
        r18_bundle = _pinned_r18_contract(Path(raw) / "r18-contracts")
        manifest = Path(raw) / "b15-shape.json"
        _write_manifest_migrations(manifest, [r15_entry, r18_entry])
        b15_shape = ContractReconciliationManager(
            paths=paths,
            contract_directory=r18_bundle.directory,
            manifest_path=manifest,
        )
        preview = b15_shape.run("fixture", dry_run=True, approved_fingerprint=None)
        _check(
            preview.data["migration_id"] == "macos-repository-setup-r18-boundary-first-use-reconciliation-v1",
            "two legacy-source entries select r18 for the installed r18 contract digest",
        )
        _check(
            json.loads(manifest.read_text(encoding="utf-8"))["migrations"][0] == r15_entry,
            "b15-shape regression leaves the r15 declaration untouched",
        )
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, _, _, _ = _environment(Path(raw))
        r18_bundle = _pinned_r18_contract(Path(raw) / "r18-contracts")
        duplicate_r18 = dict(r18_entry)
        duplicate_r18["migration_id"] = "fixture-duplicate-r18-destination-v1"
        duplicate_r18["first_use_inactive_probe_migrations"] = []
        manifest = Path(raw) / "ambiguous.json"
        _write_manifest_migrations(manifest, [r15_entry, r18_entry, duplicate_r18])
        ambiguous = ContractReconciliationManager(
            paths=paths,
            contract_directory=r18_bundle.directory,
            manifest_path=manifest,
        )
        try:
            ambiguous.run("fixture", dry_run=True, approved_fingerprint=None)
        except StateConflictError as exc:
            _check(
                str(exc) == f"contract reconciliation manifest authoring error: multiple entries match source digest {_LEGACY_DIGEST} and installed destination digest {_R18_DIGEST}",
                "duplicate source and destination identities refuse as a manifest authoring error",
            )
        else:
            _check(False, "duplicate source and destination identities are refused")
        _check(
            json.loads(manifest.read_text(encoding="utf-8"))["migrations"][0] == r15_entry,
            "ambiguous-destination regression leaves the r15 declaration untouched",
        )


def _run_first_use_supersession_regression() -> None:
    identity = ("optional_accounts", "exit", "plugin_roster_matches_plan")
    expected_error = f"contract reconciliation would change an historical or verified probe activation: {identity}"
    with tempfile.TemporaryDirectory() as raw:
        _, _, _, destination, original = _environment(Path(raw))
        attempted = original.with_stage_probe_status(
            *identity,
            CheckpointStatus.BLOCKED,
            attempt={
                "probe_id": identity[2],
                "stage_id": identity[0],
                "boundary": identity[1],
                "attempt": 1,
                "request_id": "19b81a4c-58fa-46ac-96c1-14aadb8a6e43",
                "checkpoint_status": "blocked",
                "error_kind": "probe_drift",
                "retry_safe": False,
                "evidence": [],
                "repair": "retain the historical failed first-use probe",
                "recorded_at": "2026-09-02T00:00:00Z",
            },
        )
        unmigrated = ContractReconciliation(
            migration_id="fixture-boundary-v1",
            flow_id=original.flow_id,
            source_revision=original.flow_source_revision,
            source_digest=original.flow_contract_digest,
            destination_digest=destination.contract_digest,
            stage_probe_mappings=(
                StageProbeMapping(
                    source=("preflight", "exit", "git_checkout_valid"),
                    destination=("preflight", "entry", "git_checkout_valid"),
                ),
            ),
        )
        try:
            reconcile_contract_stage_probe_state(destination, attempted, unmigrated)
        except StateError as exc:
            _check(
                str(exc) == expected_error,
                "undeclared attempted first-use transition keeps the exact activation guard error",
            )
        else:
            _check(False, "undeclared attempted first-use transition is refused")
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, _, destination, original = _environment(Path(raw), first_use_rule=True)
        attempted = original.with_stage_probe_status(
            *identity,
            CheckpointStatus.BLOCKED,
            attempt={
                "probe_id": identity[2],
                "stage_id": identity[0],
                "boundary": identity[1],
                "attempt": 1,
                "request_id": "19b81a4c-58fa-46ac-96c1-14aadb8a6e43",
                "checkpoint_status": "blocked",
                "error_kind": "probe_drift",
                "retry_safe": False,
                "evidence": [],
                "repair": "retain the historical failed first-use probe",
                "recorded_at": "2026-09-02T00:00:00Z",
            },
        )
        write_transaction(paths.transaction_path("fixture"), attempted)
        preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        result = manager.run(
            "fixture",
            dry_run=False,
            approved_fingerprint=str(preview.data["approval_fingerprint"]),
        )
        _check(result.status == "reconciled", "declared first-use migration reconciles")
        updated = load_transaction(paths.transaction_path("fixture"))
        _check(updated is not None, "declared first-use migration journal remains readable")
        assert updated is not None
        _check(
            updated.stage_probe_statuses[identity[0]][identity[1]][identity[2]] is CheckpointStatus.PENDING,
            "declared first-use migration clears only the stored effective status",
        )
        _check(
            updated.stage_probe_attempts[-1] == attempted.stage_probe_attempts[-1],
            "declared first-use migration preserves the historical attempt record verbatim",
        )
        receipt_path = paths.reconciliations_dir / "fixture.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        _check(receipt["schema_version"] == 2, "reconciliation writers emit receipt schema v2")
        _check(
            receipt["per_probe_migrations"]
            == [
                {
                    "identity": list(identity),
                    "prior_status": "blocked",
                    "new_status": "pending",
                    "rule_citation": "fixture-first-use-supersedes-attempted-v1",
                    "manifest_entry_id": "fixture-boundary-v1",
                    "source_digest": original.flow_contract_digest,
                    "destination_digest": destination.contract_digest,
                    "migration_reason": ("destination contract declares connector setup inactive on first use"),
                    "evidence_disposition": "preserve",
                }
            ],
            "receipt retains the declared per-probe supersession citation and evidence disposition",
        )
        v1_receipt = dict(receipt)
        del v1_receipt["per_probe_migrations"]
        v1_receipt["schema_version"] = 1
        reconciliation_module._parse_receipt(v1_receipt, "fixture")
        _check(True, "receipt reader preserves closed v1 compatibility")


def _run_historical_v2_per_probe_receipt_regression() -> None:
    """Keep bcd-era v2 first-use evidence readable without widening the writer."""

    receipt_bytes = _B15_HISTORICAL_RECEIPT.read_bytes()
    receipt = json.loads(receipt_bytes)
    reconciliation_module._parse_receipt(receipt, "bizopsb15")
    constructed_legacy = json.loads(receipt_bytes)
    constructed_legacy["per_probe_migrations"] = [
        {
            "identity": ["constructed", "exit", "legacy_probe"],
            "prior_status": "blocked",
            "new_status": "not_applicable",
            "rule_citation": "constructed-legacy-v2-reader-control",
            "manifest_entry_id": "constructed-legacy-v2-reader-control",
            "source_digest": "sha256:constructed-source",
            "destination_digest": "sha256:constructed-destination",
            "migration_reason": "constructed legacy dialect reader control",
            "evidence_disposition": "preserve",
        }
    ]
    reconciliation_module._parse_receipt(constructed_legacy, "bizopsb15")
    _check(
        _B15_HISTORICAL_RECEIPT.read_bytes() == receipt_bytes,
        "reading the certified historical v2 receipt does not rewrite its bytes",
    )
    with tempfile.TemporaryDirectory() as raw:
        paths = ManagerPaths.resolve(explicit_home=Path(raw) / "manager")
        paths.reconciliations_dir.mkdir(parents=True)
        replayed_receipt = paths.reconciliations_dir / "bizopsb15.json"
        replayed_receipt.write_bytes(receipt_bytes)
        replayed_receipt.chmod(0o600)
        _check(
            reconciliation_module.reconciliation_recovery_pending(paths, "bizopsb15") is False,
            "an applied historical v2 receipt does not block resume recovery inspection",
        )
        expected_preview = CommandResult(
            kind="setup",
            status="preview_ready",
            message="fixture preview reached",
            exit_code=ExitCode.OK,
        )
        manager = CreateManager(
            paths=paths,
            contract_directory=None,
            seed_lock_path=Path(raw) / "seed-lock.json",
        )
        with patch.object(create_module, "preview_create", return_value=expected_preview):
            preview = manager.preview(
                CreateConfig(
                    name="bizopsb15",
                    target=Path(raw) / "target",
                    autostart=False,
                )
            )
        _check(
            preview is expected_preview,
            "fixture-backed CreateManager preview gets past reconciliation recovery inspection",
        )
        _check(
            replayed_receipt.read_bytes() == receipt_bytes,
            "resume recovery inspection does not rewrite the historical receipt bytes",
        )
    for new_status, disposition, label in (
        ("pending", "preserve", "pending-to-pending"),
        ("pending", "discard", "blocked-to-pending-discard"),
        ("not_applicable", "discard", "blocked-to-not-applicable-discard"),
    ):
        mutated = json.loads(receipt_bytes)
        entry = mutated["per_probe_migrations"][0]
        entry["prior_status"] = "pending" if label == "pending-to-pending" else "blocked"
        entry["new_status"] = new_status
        entry["evidence_disposition"] = disposition
        _raises(
            StateError,
            lambda receipt=mutated: reconciliation_module._parse_receipt(receipt, "bizopsb15"),
            f"never-emitted {label} per-probe receipt transition remains rejected",
        )
    _raises(
        StateError,
        lambda: receipts_module._validate_transition(
            CheckpointStatus.BLOCKED,
            CheckpointStatus.NOT_APPLICABLE,
        ),
        "construction validator rejects the legacy dialect so new receipts cannot emit it",
    )


def _run_recovery_control() -> None:
    with tempfile.TemporaryDirectory() as raw:
        manager, paths, _, _, original = _environment(Path(raw))
        preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        fingerprint = str(preview.data["approval_fingerprint"])
        actual = reconciliation_module.atomic_replace_bytes
        calls = 0

        def interrupted(path: Path, value: bytes, *, mode: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 7:
                raise RuntimeError("fixture interruption")
            actual(path, value, mode=mode)

        with patch.object(reconciliation_module, "atomic_replace_bytes", interrupted):
            _raises(
                RuntimeError,
                lambda: manager.run("fixture", dry_run=False, approved_fingerprint=fingerprint),
                "interruption leaves a recoverable receipt",
            )
        recovered_preview = manager.run("fixture", dry_run=True, approved_fingerprint=None)
        _check(recovered_preview.data["recovery_performed"] is True, "next invocation recovers receipt")
        restored = load_transaction(paths.transaction_path("fixture"))
        _check(restored == original, "recovery restores authenticated journal bytes")


def _chain_migration(
    migration_id: str,
    source_revision: str,
    source_digest: str,
    destination_digest: str,
    mappings: tuple[StageProbeMapping, ...] = (),
    resets: tuple[str, ...] = (),
) -> ContractReconciliation:
    return ContractReconciliation(
        migration_id=migration_id,
        flow_id="fixture-flow",
        source_revision=source_revision,
        source_digest=source_digest,
        destination_digest=destination_digest,
        stage_probe_mappings=mappings,
        operation_statuses_to_reset=resets,
    )


def _chain_transaction() -> Transaction:
    return Transaction.create(
        name="chain",
        target=_ROOT,
        input_fingerprint="sha256:" + "c" * 64,
        answers={},
        seed=_seed(),
        flow_id="fixture-flow",
        flow_source_revision="source-revision",
        flow_contract_digest="sha256:" + "a" * 64,
        stage_ids=(),
        completion_probe_ids=(),
    )


def _run_reconciliation_chain_regressions() -> None:
    transaction = _chain_transaction()
    first = _chain_migration(
        "source-to-middle",
        transaction.flow_source_revision,
        transaction.flow_contract_digest,
        "sha256:" + "b" * 64,
        mappings=(
            StageProbeMapping(
                source=("stage", "entry", "old_probe"),
                destination=("stage", "entry", "middle_probe"),
            ),
        ),
        resets=("first_operation",),
    )
    second = _chain_migration(
        "middle-to-destination",
        "middle-revision",
        "sha256:" + "b" * 64,
        "sha256:" + "c" * 64,
        mappings=(
            StageProbeMapping(
                source=("stage", "entry", "middle_probe"),
                destination=("stage", "entry", "current_probe"),
            ),
        ),
        resets=("second_operation", "first_operation"),
    )
    resolved = reconciliation_chain_module.resolve_reconciliation_chain(
        transaction,
        "sha256:" + "c" * 64,
        (first, second),
    )
    _check(
        resolved.stage_probe_mappings
        == (
            StageProbeMapping(
                source=("stage", "entry", "old_probe"),
                destination=("stage", "entry", "current_probe"),
            ),
        ),
        "chained reconciliation composes stage-probe mappings",
    )
    _check(
        resolved.operation_statuses_to_reset == ("first_operation", "second_operation"),
        "chained reconciliation retains every intermediate operation reset",
    )
    alternate = replace(second, source_revision="alternate-middle-revision")
    _raises(
        StateConflictError,
        lambda: reconciliation_chain_module.resolve_reconciliation_chain(
            transaction,
            "sha256:" + "c" * 64,
            (first, second, alternate),
        ),
        "ambiguous intermediate revision identities are refused",
    )
    cycle = replace(second, destination_digest=transaction.flow_contract_digest)
    _raises(
        StateConflictError,
        lambda: reconciliation_chain_module.resolve_reconciliation_chain(
            transaction,
            "sha256:" + "c" * 64,
            (first, cycle),
        ),
        "cyclic reconciliation chains are refused",
    )


def _run_lm_studio_source_pins() -> None:
    # Actual persisted revision/digest pairs from r25, r26 and r27 ladder
    # resume receipts; two releases share bytes but have different revisions.
    sources = (
        ("92634d2b505becf51d34313ca8d27ee65151de23", "sha256:ce6d9d88fd77b2feeab964e2cd2f75e4d0ca6149634b0bcfac520cd047d720bb"),
        ("2b9eb9573ce136362ad566ea4ba38e5ebc55205e", "sha256:ce6d9d88fd77b2feeab964e2cd2f75e4d0ca6149634b0bcfac520cd047d720bb"),
        ("73af1c9de0b3132ce9f59abce20e2de19ced18c9", "sha256:515ab65fcf6d82756b3ffc6bf42f782ddc33b69907b5d32a372b019bea6a8e72"),
    )
    active_destination = contract_digest(_ROOT / _CONTRACT_PATH)
    _check(
        active_destination == _RAM_PREFLIGHT_DESTINATION_DIGEST,
        "LM Studio source bridges target the unmodified active candidate bundle",
    )
    declarations = load_contract_reconciliations(manifest_path=_RECONCILIATION_MANIFEST)
    for revision, digest in sources:
        transaction = Transaction.create(name="lm-pin", target=_ROOT, input_fingerprint="sha256:" + "a" * 64, answers={}, seed=_seed(), flow_id="macos.repository_setup", flow_source_revision=revision, flow_contract_digest=digest, stage_ids=(), completion_probe_ids=())
        candidates = reconciliation_chain_module.identity_reconciliation_candidates(
            (transaction.flow_id, transaction.flow_source_revision, transaction.flow_contract_digest),
            declarations,
        )
        matches = reconciliation_chain_module.destination_reconciliation_candidates(
            candidates, active_destination
        )
        _check(len(matches) == 1, "one exact persisted LM Studio source tuple reaches the active candidate contract")
        _check(matches[0].stage_probe_mappings == () and matches[0].first_use_inactive_probe_migrations == (), "new LM Studio probes cannot inherit verified old evidence")
        changed = replace(transaction, flow_source_revision="f" * 40)
        _check(
            not reconciliation_chain_module.identity_reconciliation_candidates(
                (changed.flow_id, changed.flow_source_revision, changed.flow_contract_digest),
                declarations,
            ),
            "a similar digest with an unknown source revision is refused",
        )


def _run_ram_preflight_reconciliation_regression() -> None:
    """A new active preflight probe never inherits an earlier host verdict."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source_path = root / "source-contracts"
        source_path.mkdir()
        for filename in contract_filenames():
            (source_path / filename).write_bytes(
                _fixture_file_bytes(_RAM_PREFLIGHT_SOURCE_COMMIT, filename)
            )
        source = ContractBundle.load(
            source_revision=_RAM_PREFLIGHT_SOURCE_COMMIT,
            directory=source_path,
            expected_digest=_RAM_PREFLIGHT_SOURCE_DIGEST,
        )
        destination = ContractBundle.load(
            source_revision=_RAM_PREFLIGHT_SOURCE_COMMIT,
            directory=_ROOT / _CONTRACT_PATH,
            expected_digest=_RAM_PREFLIGHT_DESTINATION_DIGEST,
        )
        _check(
            destination.contract_digest == _RAM_PREFLIGHT_DESTINATION_DIGEST,
            "RAM preflight destination pin is computed from unmodified active bundle bytes",
        )
        answers = _r20_answers(root / "target", session_sources=[])
        answers["flow_source_revision"] = source.source_revision
        transaction = Transaction.create(
            name="ram-preflight",
            target=root / "target",
            input_fingerprint="sha256:" + "7" * 64,
            answers=answers,  # type: ignore[arg-type]
            seed=_seed(),
            flow_id=source.flow_id,
            flow_source_revision=source.source_revision,
            flow_contract_digest=source.contract_digest,
            stage_ids=tuple(source.stages),
            stage_probe_statuses=initial_stage_probe_statuses(source, answers),  # type: ignore[arg-type]
            completion_probe_ids=source.completion_probe_ids,
        )
        candidates = reconciliation_chain_module.identity_reconciliation_candidates(
            (transaction.flow_id, transaction.flow_source_revision, transaction.flow_contract_digest),
            load_contract_reconciliations(manifest_path=_RECONCILIATION_MANIFEST),
        )
        matches = reconciliation_chain_module.destination_reconciliation_candidates(
            candidates,
            destination.contract_digest,
        )
        _check(len(matches) == 1, "RAM preflight source tuple has one declared migration")
        reconciled = reconcile_contract_stage_probe_state(destination, transaction, matches[0])
        _check(
            reconciled.stage_probe_statuses["preflight"]["entry"][
                "minimum_physical_memory_valid"
            ]
            is CheckpointStatus.PENDING,
            "a pending historical preflight receives the new RAM probe as pending",
        )
        verified = transaction.with_statuses(
            stages={**transaction.stages, "preflight": CheckpointStatus.VERIFIED}
        )
        _raises(
            StateError,
            lambda: reconcile_contract_stage_probe_state(destination, verified, matches[0]),
            "a verified historical preflight cannot inherit the new RAM probe",
        )
