"""Strict LM Studio index and loaded-identifier readback for reviewed Qwen bytes."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .lm_studio_deadline import ServedDeadline, _deadline_evidence, call_budget, check_deadline, record_command, run_with_deadline
from .lm_studio_index_readback import CliReceipt, IndexReadback, LoadedReadback, _identity, _identity_from_stat, alias_path, parse_rows, select_index_rows, select_loaded_rows
from .lm_studio_models import (
    ModelArtifact,
    artifact_present,
    cli_available,
    cli_path,
    observe_loaded,
    verified_file_identity,
)
from .setup_adapter_contract import AdapterRequest, JsonObject, evidence, planned_action, result
from .setup_adapter_runtime import CommandOutcome, Runtime

INDEX_NAMESPACE = "solet-verified/Qwen3-14B"
LOCAL_SOURCE_REF_KEY = "local_qwen_attestation_ref"
_INDEX_OUTPUT_LIMIT = 64 * 1024


@dataclass(frozen=True, slots=True)
class VerifiedLocalSource:
    """Consumer-side handoff; only the verified vendor producer is reachable in B1.

    B2 must issue user-approved instances after its locked Manager re-preview.
    A path from public_inputs is never sufficient to construct this value.
    """

    origin: Literal["vendor_verified", "user_approved_local"]
    role: Literal["inference"]
    repository: str
    filename: str
    api_identifier: str
    expected_size_bytes: int
    expected_sha256: str
    observed_size_bytes: int
    observed_sha256: str
    canonical_source_path: Path
    staged_path: Path
    transaction_revision: str
    flow_revision: str
    approval_fingerprint: str | None


def vendor_source(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, deadline: ServedDeadline | None = None) -> VerifiedLocalSource | None:
    """Promote Fix A's genuine vendor provenance and digest predicate only."""

    if model.role != "inference" or not artifact_present(model, runtime.home, deadline):
        return None
    destination = model.path(runtime.home)
    return VerifiedLocalSource(
        origin="vendor_verified",
        role="inference",
        repository=model.repository,
        filename=model.filename,
        api_identifier=model.api_identifier,
        expected_size_bytes=model.size_bytes,
        expected_sha256=model.sha256,
        observed_size_bytes=model.size_bytes,
        observed_sha256=model.sha256,
        canonical_source_path=destination,
        staged_path=destination,
        transaction_revision=request.answers_fingerprint,
        flow_revision=request.flow_source_revision,
        approval_fingerprint=request.approval_fingerprint,
    )


def verified_source_for_request(
    request: AdapterRequest,
    runtime: Runtime,
    model: ModelArtifact,
    local_source: VerifiedLocalSource | None,
    deadline: ServedDeadline | None = None,
) -> VerifiedLocalSource | None:
    """Accept a local handoff only after B2 resolves its Manager record ref."""

    record_ref = request.public_inputs.get(LOCAL_SOURCE_REF_KEY)
    if local_source is None:
        return None if record_ref is not None else vendor_source(request, runtime, model, deadline)
    if local_source.origin != "user_approved_local" or record_ref != local_source.transaction_revision:
        return None
    if local_source.flow_revision != request.flow_source_revision:
        return None
    if request.phase == "apply" and local_source.approval_fingerprint != request.approval_fingerprint:
        return None
    return local_source if _source_identity(runtime, model, local_source, deadline) is not None else None


def local_mode_requested(request: AdapterRequest, local_source: VerifiedLocalSource | None) -> bool:
    return local_source is not None or LOCAL_SOURCE_REF_KEY in request.public_inputs


def local_artifact_result(
    request: AdapterRequest,
    runtime: Runtime,
    model: ModelArtifact,
    local_source: VerifiedLocalSource | None,
    repair: str,
) -> JsonObject:
    source = verified_source_for_request(request, runtime, model, local_source)
    if source is None:
        return result(request, status="blocked", error_kind="lm_studio_local_source_invalid", retry_safe=False, repair=repair)
    status = "verified" if request.phase == "probe" else "applied"
    return result(request, status=status, evidence_items=[evidence(evidence_id="lm_studio_local_qwen_bytes", kind="filesystem", status="verified", summary="Approved local Qwen destination matches reviewed bytes", observed=[f"source={source.canonical_source_path}", f"staged={source.staged_path}", f"sha256={source.observed_sha256}"], expected=model.sha256, source="Manager-issued local source handoff")])


def index_action(runtime: Runtime, source: VerifiedLocalSource) -> JsonObject:
    return planned_action(
        action_id="lm_studio.ensure_index_inference",
        title="Index the verified Qwen file with a reviewed hard link",
        mutation_kind="host_provisioning",
        target=f"source={source.canonical_source_path}; index_alias={alias_path(runtime.home)}; sha256={source.expected_sha256}",
        evidence_ref="lm_studio_inference_model_indexed",
    )


def index_evidence(model: ModelArtifact, source: VerifiedLocalSource, readback: IndexReadback) -> JsonObject:
    return evidence(
        evidence_id="lm_studio_inference_index_binding",
        kind="host",
        status="verified",
        summary="Unique LM Studio key resolves to the reviewed Qwen bytes and inode",
        observed=[
            f"canonical_source={source.canonical_source_path}",
            f"staged_source={source.staged_path}",
            f"indexed_path={readback.destination}",
            f"model_key={readback.model_key}",
            f"file_identity={readback.file_identity}",
            f"sha256={model.sha256}",
        ],
        expected=[f"api_identifier={model.api_identifier}", f"sha256={model.sha256}"],
        source="lms ls --llm --json and reviewed destination bytes",
    )


def load_action(model: ModelArtifact, readback: IndexReadback) -> JsonObject:
    return planned_action(
        action_id="lm_studio.load_inference",
        title="Load the verified Qwen key under the configured API identifier",
        mutation_kind="host_provisioning",
        target=f"model_key={readback.model_key}; api_identifier={model.api_identifier}; GPU off; context 8192",
        evidence_ref="lm_studio_inference_model_served",
    )


def relabel_action(model: ModelArtifact, readback: IndexReadback, loaded: LoadedReadback) -> JsonObject:
    return planned_action(
        action_id="lm_studio.relabel_inference",
        title="Review unloading the proven wrong-ID Qwen instance, then load once with the exact ID",
        mutation_kind="host_provisioning",
        target=f"model_key={readback.model_key}; wrong_identifier={loaded.wrong_identifier}; requested_identifier={model.api_identifier}; shared_host_review_required=true",
        evidence_ref="lm_studio_inference_identifier_conflict",
    )


def loaded_evidence(model: ModelArtifact, readback: IndexReadback) -> JsonObject:
    return evidence(
        evidence_id="lm_studio_inference_served_binding",
        kind="host",
        status="verified",
        summary="LM Studio ps and API v0 expose the reviewed key under the exact identifier",
        observed=[f"model_key={readback.model_key}", f"indexed_path={readback.destination}", f"api_identifier={model.api_identifier}"],
        expected=model.api_identifier,
        source="lms ps --json and /api/v0/models",
    )


def index_failure(request: AdapterRequest, readback: IndexReadback, repair: str) -> JsonObject:
    command = readback.command
    return result(
        request,
        status="blocked" if command is None or command.ok else "failed",
        error_kind=readback.error_kind or "lm_studio_index_unavailable",
        retry_safe=False,
        exit_code=None if command is None else command.returncode,
        timed_out=False if command is None else command.timed_out,
        duration_ms=0 if command is None else command.duration_ms,
        evidence_items=_index_command_items(readback),
        repair=repair,
    )


def inference_index(
    request: AdapterRequest,
    runtime: Runtime,
    model: ModelArtifact,
    repair: str,
    local_source: VerifiedLocalSource | None = None,
) -> JsonObject:
    """Run the Manager-side index operation or passive readiness probe."""

    source = verified_source_for_request(request, runtime, model, local_source)
    if source is None:
        return result(request, status="blocked", error_kind="lm_studio_index_source_invalid", retry_safe=False, repair=repair)
    if not cli_available(runtime.home):
        return result(request, status="blocked", error_kind="lm_studio_cli_unavailable", repair=repair)
    readback = inspect_index(runtime, model, source)
    if readback.status == "indexed":
        status = "verified" if request.phase == "probe" else "applied"
        return result(request, status=status, evidence_items=[index_evidence(model, source, readback)])
    if readback.status == "invalid":
        return index_failure(request, readback, repair)
    if request.phase == "probe":
        return result(request, status="pending", actions=[index_action(runtime, source)], repair=repair)
    imported = import_verified(runtime, model, source)
    if imported.status != "indexed":
        return index_failure(request, imported, repair)
    return result(request, status="applied", evidence_items=[index_evidence(model, source, imported), *_index_command_items(imported)])


def _index_command_items(readback: IndexReadback) -> list[JsonObject]:
    if readback.receipts:
        return [_index_command_evidence(command, argv, index) for index, (argv, command) in enumerate(readback.receipts)]
    if readback.command is not None and readback.argv is not None:
        return [_index_command_evidence(readback.command, readback.argv, 0)]
    return []


def _index_command_evidence(command: CommandOutcome, argv: tuple[str, ...], index: int) -> JsonObject:
    return evidence(
        evidence_id=f"lm_studio_index_command_{index}",
        kind="command",
        status="observed",
        summary="Bounded LM Studio index command outcome",
        observed=[
            f"argv={' '.join(argv)}",
            f"exit_code={command.returncode}",
            f"timed_out={command.timed_out}",
            f"duration_ms={command.duration_ms}",
            f"stdout_bytes={command.stdout_bytes}",
            f"stderr_bytes={command.stderr_bytes}",
            f"stdout_truncated={command.stdout_truncated}",
            f"stderr_truncated={command.stderr_truncated}",
        ],
        expected="reviewed CLI command succeeds without timeout or truncation",
        source="lms index recovery",
    )


def inspect_index(runtime: Runtime, model: ModelArtifact, source: VerifiedLocalSource, deadline: ServedDeadline | None = None) -> IndexReadback:
    """Select one indexed key only when it names the verified source inode."""

    before = _source_identity(runtime, model, source, deadline)
    if before is None:
        return IndexReadback("invalid", "lm_studio_index_source_invalid")
    argv = (str(cli_path(runtime.home)), "ls", "--llm", "--json")
    command = runtime.run(argv, timeout_seconds=call_budget(deadline, 10), output_limit=_INDEX_OUTPUT_LIMIT)
    record_command(deadline, command)
    check_deadline(deadline)
    if not command.ok or command.stdout_truncated or command.stderr_truncated:
        return IndexReadback("invalid", "lm_studio_index_unavailable", command=command, argv=argv)
    rows = parse_rows(command.stdout, required_fields=("type", "modelKey", "path", "sizeBytes"))
    if rows is None:
        return IndexReadback("invalid", "lm_studio_index_malformed", command=command, argv=argv)
    selected = select_index_rows(rows, runtime.home, model, source.staged_path, before, deadline)
    if _identity(source.staged_path) != _identity_from_stat(before):
        return IndexReadback("invalid", "lm_studio_index_source_changed", command=command, argv=argv)
    return replace(selected, command=command, argv=argv)


def import_verified(runtime: Runtime, model: ModelArtifact, source: VerifiedLocalSource) -> IndexReadback:
    """Dry-run a hard link, apply once, then rehash and re-read the vendor index."""

    before = _source_identity(runtime, model, source)
    destination = alias_path(runtime.home)
    if before is None:
        return IndexReadback("invalid", "lm_studio_index_source_invalid")
    precondition = _import_precondition(runtime, destination, before)
    if precondition is not None:
        return precondition
    cli = str(cli_path(runtime.home))
    common = (cli, "import", str(source.staged_path), "--hard-link", "--user-repo", INDEX_NAMESPACE)
    preview = _preview_hardlink(runtime, model, source, destination, before, common)
    if preview.status == "invalid":
        return preview
    return _apply_hardlink(runtime, model, source, destination, common, preview)


def _import_precondition(runtime: Runtime, destination: Path, before: os.stat_result) -> IndexReadback | None:
    if destination.exists() or destination.is_symlink() or _has_symlink_ancestor(destination, runtime.home):
        return IndexReadback("invalid", "lm_studio_index_destination_conflict")
    models_root = runtime.home / ".lmstudio/models"
    root_identity = _identity(models_root)
    if root_identity is None or root_identity[0] != before.st_dev:
        return IndexReadback("invalid", "lm_studio_index_cross_device")
    return None


def _preview_hardlink(
    runtime: Runtime,
    model: ModelArtifact,
    source: VerifiedLocalSource,
    destination: Path,
    before: os.stat_result,
    common: tuple[str, ...],
) -> IndexReadback:
    preview_argv = (*common, "--dry-run", "--yes")
    preview = runtime.run(preview_argv, timeout_seconds=30, output_limit=_INDEX_OUTPUT_LIMIT)
    if not preview.ok or preview.stdout_truncated or preview.stderr_truncated:
        return IndexReadback("invalid", "lm_studio_index_dry_run_failed", command=preview, argv=preview_argv)
    if not _dry_run_matches(preview, destination):
        return IndexReadback("invalid", "lm_studio_index_dry_run_mismatch", command=preview, argv=preview_argv)
    if _source_identity(runtime, model, source) != before or destination.exists() or destination.is_symlink():
        return IndexReadback("invalid", "lm_studio_index_source_changed", command=preview, argv=preview_argv)
    return IndexReadback("missing", command=preview, argv=preview_argv)


def _apply_hardlink(
    runtime: Runtime,
    model: ModelArtifact,
    source: VerifiedLocalSource,
    destination: Path,
    common: tuple[str, ...],
    preview: IndexReadback,
) -> IndexReadback:
    if preview.argv is None or preview.command is None:
        return IndexReadback("invalid", "lm_studio_index_dry_run_failed")
    import_argv = (*common, "--yes")
    applied = runtime.run(import_argv, timeout_seconds=120, output_limit=_INDEX_OUTPUT_LIMIT)
    receipts: tuple[CliReceipt, ...] = ((preview.argv, preview.command), (import_argv, applied))
    if not applied.ok or applied.stdout_truncated or applied.stderr_truncated:
        return IndexReadback("invalid", "lm_studio_index_import_failed", command=applied, argv=import_argv, receipts=receipts)
    return _readback_imported(runtime, model, source, destination, applied, import_argv, receipts)


def _readback_imported(
    runtime: Runtime,
    model: ModelArtifact,
    source: VerifiedLocalSource,
    destination: Path,
    applied: CommandOutcome,
    import_argv: tuple[str, ...],
    receipts: tuple[CliReceipt, ...],
) -> IndexReadback:
    after = _source_identity(runtime, model, source)
    linked = verified_file_identity(model, destination, runtime.home)
    if after is None or linked is None or _identity_from_stat(after) != _identity_from_stat(linked):
        return IndexReadback("invalid", "lm_studio_index_destination_invalid", command=applied, argv=import_argv, receipts=receipts)
    selected = inspect_index(runtime, model, source)
    receipts = _append_index_receipt(receipts, selected)
    if selected.status != "indexed" or selected.destination != destination:
        return IndexReadback("invalid", "lm_studio_index_unavailable", command=selected.command or applied, argv=selected.argv or import_argv, receipts=receipts)
    return replace(selected, receipts=receipts)


def _append_index_receipt(receipts: tuple[CliReceipt, ...], selected: IndexReadback) -> tuple[CliReceipt, ...]:
    if selected.command is not None and selected.argv is not None:
        return (*receipts, (selected.argv, selected.command))
    return receipts


def inspect_loaded(runtime: Runtime, model: ModelArtifact, index: IndexReadback, deadline: ServedDeadline | None = None) -> LoadedReadback:
    """Bind lms ps key/path/identifier to the strict v0 exact loaded ID."""

    if index.status != "indexed" or index.model_key is None or index.destination is None:
        return LoadedReadback("invalid", "lm_studio_index_unavailable")
    command = runtime.run((str(cli_path(runtime.home)), "ps", "--json"), timeout_seconds=call_budget(deadline, 10), output_limit=_INDEX_OUTPUT_LIMIT)
    record_command(deadline, command)
    check_deadline(deadline)
    if not command.ok or command.stdout_truncated or command.stderr_truncated:
        return LoadedReadback("invalid", "lm_studio_loaded_index_unavailable", command=command)
    rows = parse_rows(command.stdout, required_fields=("type", "modelKey", "path", "sizeBytes", "identifier"))
    if rows is None:
        return LoadedReadback("invalid", "lm_studio_loaded_index_malformed", command=command)
    selected = select_loaded_rows(rows, runtime.home, model, index, deadline)
    if selected.status == "invalid" or selected.status == "identifier_conflict":
        return LoadedReadback(selected.status, selected.error_kind, selected.wrong_identifier, command)
    return _api_crosscheck(runtime, model, selected, command, deadline)


def _api_crosscheck(runtime: Runtime, model: ModelArtifact, selected: LoadedReadback, command: CommandOutcome, deadline: ServedDeadline | None = None) -> LoadedReadback:
    observation = observe_loaded(runtime, model.api_identifier, deadline)
    if observation.status == "protocol_error":
        return LoadedReadback("invalid", "lm_studio_served_protocol_error", command=command)
    if observation.status == "transport_unknown":
        return LoadedReadback("transport_unknown", "lm_studio_served_state_unknown", command=command)
    if selected.status == "served" and observation.status == "absent":
        return LoadedReadback("visibility_lag", "lm_studio_served_visibility_lag", command=command)
    if selected.status == "absent" and observation.status == "loaded":
        return LoadedReadback("invalid", "lm_studio_served_index_mismatch", command=command)
    check_deadline(deadline)
    return LoadedReadback(selected.status, command=command)


def _source_identity(runtime: Runtime, model: ModelArtifact, source: VerifiedLocalSource, deadline: ServedDeadline | None = None) -> os.stat_result | None:
    expected = model.path(runtime.home)
    if not _source_contract_matches(model, source, expected):
        return None
    if source.origin == "vendor_verified" and not artifact_present(model, runtime.home, deadline):
        return None
    return verified_file_identity(model, source.staged_path, runtime.home, deadline)


def _source_contract_matches(model: ModelArtifact, source: VerifiedLocalSource, expected: Path) -> bool:
    if model.role != "inference" or source.role != "inference":
        return False
    if source.origin == "vendor_verified":
        if source.canonical_source_path != expected:
            return False
    elif source.origin == "user_approved_local":
        if not source.approval_fingerprint:
            return False
    else:
        return False
    return (
        (source.repository, source.filename, source.api_identifier) == (model.repository, model.filename, model.api_identifier)
        and (source.expected_size_bytes, source.observed_size_bytes) == (model.size_bytes, model.size_bytes)
        and (source.expected_sha256, source.observed_sha256) == (model.sha256, model.sha256)
        and source.staged_path == expected
    )


def _dry_run_matches(command: CommandOutcome, destination: Path) -> bool:
    lines = [line.strip() for line in (command.stdout + "\n" + command.stderr).splitlines() if line.strip()]
    return lines == [f"Would create a hard link to {destination}", "But not actually doing it because of --dry-run"]


def _has_symlink_ancestor(path: Path, home: Path) -> bool:
    try:
        path.relative_to(home)
    except ValueError:
        return True
    return any(parent.is_symlink() for parent in path.parents if parent != home and home in parent.parents)


def inference_observation(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, local_source: VerifiedLocalSource | None, deadline: ServedDeadline | None) -> tuple[VerifiedLocalSource | None, IndexReadback, LoadedReadback]:
    check_deadline(deadline)
    if deadline is not None:
        deadline.attempts += 1
    source = verified_source_for_request(request, runtime, model, local_source, deadline)
    indexed = IndexReadback("invalid", "lm_studio_index_source_invalid") if source is None else inspect_index(runtime, model, source, deadline)
    if indexed.status != "indexed" or source is None:
        return source, indexed, LoadedReadback("invalid", indexed.error_kind or "lm_studio_index_unavailable")
    loaded = inspect_loaded(runtime, model, indexed, deadline)
    return _finish_inference_observation(runtime, model, source, indexed, loaded, deadline)


def _finish_inference_observation(runtime: Runtime, model: ModelArtifact, source: VerifiedLocalSource, indexed: IndexReadback, loaded: LoadedReadback, deadline: ServedDeadline | None) -> tuple[VerifiedLocalSource, IndexReadback, LoadedReadback]:
    if loaded.status in {"invalid", "identifier_conflict"}:
        if deadline is not None:
            deadline.terminal_class = loaded.error_kind or loaded.status
        return source, indexed, loaded
    # Revalidate after HTTP as well: a valid earlier hash cannot certify later bytes.
    after = inspect_index(runtime, model, source, deadline)
    if not same_index(indexed, after):
        loaded = LoadedReadback("invalid", "lm_studio_index_source_changed")
    if deadline is not None:
        deadline.terminal_class = loaded.status if loaded.error_kind is None else loaded.error_kind
    check_deadline(deadline)
    return source, indexed, loaded


def run_terminal_inference(request: AdapterRequest, operation: Callable[[ServedDeadline | None], JsonObject], repair: str) -> JsonObject:
    """Keep a classified failure if later envelope checks exhaust the budget."""

    terminal: JsonObject | None = None
    terminal_deadline: ServedDeadline | None = None

    def observed(deadline: ServedDeadline | None) -> JsonObject:
        nonlocal terminal, terminal_deadline
        answer = operation(deadline)
        if deadline is not None and answer["retry_safe"] is False and answer["error_kind"] == deadline.terminal_class:
            terminal = answer
            terminal_deadline = deadline
        return answer

    answer = run_with_deadline(request, observed, repair)
    if terminal is None or answer["error_kind"] != "lm_studio_served_budget_exhausted":
        return answer
    assert terminal_deadline is not None
    items = terminal["evidence"]
    assert isinstance(items, list)
    if not any(isinstance(item, dict) and item.get("id") == "lm_studio_served_deadline" for item in items):
        items.append(_deadline_evidence(terminal_deadline))
    return terminal


def same_index(before: IndexReadback, after: IndexReadback) -> bool:
    return after.status == "indexed" and (before.model_key, before.destination, before.file_identity) == (after.model_key, after.destination, after.file_identity)




def loaded_result(
    request: AdapterRequest,
    model: ModelArtifact,
    source: VerifiedLocalSource,
    indexed: IndexReadback,
    loaded: LoadedReadback,
    repair: str,
) -> JsonObject | None:
    terminal = terminal_loaded_result(request, model, indexed, loaded, repair)
    if terminal is not None:
        return terminal
    if loaded.status == "served":
        status = "verified" if request.phase == "probe" else "applied"
        return result(request, status=status, evidence_items=[index_evidence(model, source, indexed), loaded_evidence(model, indexed)])
    if loaded.status in {"transport_unknown", "visibility_lag"}:
        return result(request, status="blocked", error_kind=loaded.error_kind, retry_safe=False, repair=repair)
    if request.phase == "probe":
        return result(request, status="pending", actions=[load_action(model, indexed)], repair=repair)
    return None


def terminal_loaded_result(
    request: AdapterRequest,
    model: ModelArtifact,
    indexed: IndexReadback,
    loaded: LoadedReadback,
    repair: str,
) -> JsonObject | None:
    """Render a known terminal readback without another source or index read."""

    if loaded.status == "identifier_conflict":
        actions = [relabel_action(model, indexed, loaded)] if loaded.wrong_identifier is not None else []
        return result(request, status="blocked", error_kind="identifier_conflict", retry_safe=False, actions=actions, repair=repair)
    if loaded.status == "invalid":
        return result(request, status="blocked", error_kind=loaded.error_kind, retry_safe=False, repair=repair)
    return None


def wait_inference(request: AdapterRequest, runtime: Runtime, model: ModelArtifact, local_source: VerifiedLocalSource | None, pinned: IndexReadback, loaded: LoadedReadback, deadline: ServedDeadline, repair: str) -> JsonObject:
    """Wait only on eligible passive states, preserving terminal readback."""

    while loaded.status in {"absent", "visibility_lag", "transport_unknown"}:
        deadline.pause()
        source, indexed, loaded = inference_observation(request, runtime, model, local_source, deadline)
        if not same_index(pinned, indexed) or source is None:
            return result(request, status="blocked", error_kind="lm_studio_index_source_changed", retry_safe=False, repair=repair)
        if loaded.status in {"invalid", "identifier_conflict"}:
            break
    if (terminal := terminal_loaded_result(request, model, pinned, loaded, repair)) is not None:
        return terminal
    # A successful HTTP readback needs a fresh source identity for evidence.
    source = verified_source_for_request(request, runtime, model, local_source, deadline)
    if source is None:
        return result(request, status="blocked", error_kind="lm_studio_index_source_invalid", retry_safe=False, repair=repair)
    answer = loaded_result(request, model, source, pinned, loaded, repair)
    assert answer is not None
    return answer
