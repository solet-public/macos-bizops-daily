"""Seed-side ``existing::`` handlers for the existing-install flow (design section 3.2).

Every handler here executes from the REFRESHED target tree against an install
that is already born and (in most cases) serving.  Handlers may call the same
low-level render and merge helpers genesis uses, but never a genesis,
credential, vault, database-provisioning, or router-install operation: the
import graph of this module is asserted by a seed-side smoke with that
denylist.  The vocabulary is closed and enumerated in ``SEED_OPERATION_REFS``;
the Manager's registry carries the same table and a smoke proves the seed-side
subset is byte-equal.

This module owns the dispatch table and the managed-artifact three-way engine
(sections 6.1-6.3); the migrations and the plugin cache refresh live in
``existing_install_migrations``.  The one hydration rule every handler
honours: a destination the Manager plan did not name is refused
(``preserved_surface_write_refused``), and a managed block or rendered file
whose local bytes match neither the previous nor the candidate render is a
conflict, never a side to pick.  The one exception is a ``rendered_whole``
file (a user-scope file such as the ``/feedback`` skill): it is only ever
refreshed, so a missing one is not created and an edited one is reported in its
state row (``locally_modified`` or ``unknown_origin``) and left in place without
blocking the update.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .autostart import render_launchagent_plist
from .existing_install_migrations import (
    STRUCTURED_OUTPUT_LIMIT,
    blocked,
    file_mode,
    migration_export_root_containment,
    migration_solet_rename,
    plugin_cache_refresh,
    read_text,
)
from .existing_install_plugin_transitions import migration_plugin_transition
from .managed_render import (
    TEMPLATE_ROOT_REF,
    BlockMatch,
    append_block,
    block_text,
    find_blocks,
    insert_stamp,
    marker_lines,
    render_tokens,
    replace_block,
    sha256_bytes,
    sha256_text,
    stamp_line,
    stamped_digest,
    strip_marker_lines,
    zsh_quote,
)
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, evidence, planned_action, result
from .setup_adapter_runtime import Runtime

type Handler = Callable[[AdapterRequest, Runtime], JsonObject]

#: The complete seed-side subset of the closed ``existing::`` vocabulary.
#: ``existing::dependencies.reconcile`` is routed by the pre-venv bootstrap
#: adapter, not by this module's handler table; it is listed here so the
#: byte-equality smoke against the Manager registry covers the whole subset.
SEED_OPERATION_REFS: tuple[str, ...] = (
    "existing::dependencies.reconcile",
    "existing::migration.solet_rename",
    "existing::migration.export_root_containment",
    "existing::migration.plugin_transition",
    "existing::hydration.reconcile",
    "existing::autostart.reconcile",
    "existing::runtime.plugin_cache_refresh",
)
EXISTING_ALLOWED_PUBLIC_INPUTS: dict[str, frozenset[str]] = {
    "existing::migration.solet_rename": frozenset(),
    "existing::migration.export_root_containment": frozenset(),
    "existing::migration.plugin_transition": frozenset(),
    "existing::hydration.reconcile": frozenset({"artifact_ids", "planned_destinations"}),
    "existing::autostart.reconcile": frozenset({"artifact_ids", "planned_destinations"}),
    "existing::runtime.plugin_cache_refresh": frozenset(),
}
BUNDLE_PATH = Path("plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json")
_HARDENED_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
}
_BLOCK_KINDS = frozenset({"managed_block", "rendered_whole"})
_PLIST_KINDS = frozenset({"launchd_plist"})


def operation_handlers() -> dict[str, Handler]:
    """The closed seed-side ``existing::`` handler table merged into ``setup_adapter``."""
    return {
        "existing::migration.solet_rename": migration_solet_rename,
        "existing::migration.export_root_containment": migration_export_root_containment,
        "existing::migration.plugin_transition": migration_plugin_transition,
        "existing::hydration.reconcile": hydration_reconcile,
        "existing::autostart.reconcile": autostart_reconcile,
        "existing::runtime.plugin_cache_refresh": plugin_cache_refresh,
    }


# --- managed artifacts (sections 6.1-6.3) -------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactDeclaration:
    artifact_id: str
    kind: str
    logical_destination: str
    marker_begin: str | None
    marker_end: str | None
    stamp: str | None
    template_ref: str
    template_digest: str
    previous_template_digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactState:
    artifact_id: str
    kind: str
    destination: str
    state: str
    action: str
    stamped_digest: str | None
    current_sha256: str | None
    expected_sha256: str
    conflict: str | None
    new_content: str | None
    mode: int


@dataclass(frozen=True, slots=True)
class _Context:
    """Everything the three-way engine needs to render and compare one artifact."""

    request: AdapterRequest
    runtime: Runtime
    predecessors: tuple[str, ...]


def hydration_reconcile(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    return _reconcile_artifacts(request, runtime, _BLOCK_KINDS)


def autostart_reconcile(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    return _reconcile_artifacts(request, runtime, _PLIST_KINDS)


def _reconcile_artifacts(request: AdapterRequest, runtime: Runtime, kinds: frozenset[str]) -> JsonObject:
    bundle = _load_bundle(request.target)
    context = _Context(request, runtime, tuple(str(cast(JsonObject, row)["commit"]) for row in cast(list[JsonValue], bundle["supported_predecessors"])))
    states = _declared_states(context, {item.artifact_id: item for item in _artifacts(bundle)}, kinds)
    if isinstance(states, dict):
        return states
    if request.phase == "probe":
        return _artifact_probe(request, states)
    return _artifact_apply(request, runtime, states)


def _declared_states(context: _Context, declared: dict[str, ArtifactDeclaration], kinds: frozenset[str]) -> list[ArtifactState] | JsonObject:
    """Resolve every requested artifact, refusing an undeclared id or an unplanned destination."""
    request = context.request
    planned = _planned_destinations(request)
    ids = request.public_inputs.get("artifact_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        return blocked(request, "adapter_protocol_error", "artifact_ids must name declared artifacts.")
    states: list[ArtifactState] = []
    for artifact_id in cast(list[str], ids):
        artifact = declared.get(artifact_id)
        if artifact is None or artifact.kind not in kinds:
            return blocked(request, "adapter_protocol_error", f"{artifact_id} is not a declared artifact of this kind.")
        destination = _resolve_destination(artifact, request, context.runtime)
        if planned.get(artifact_id) != destination:
            return blocked(request, "preserved_surface_write_refused", f"{artifact_id} resolves to {destination}, which the Manager plan did not name.")
        states.append(_artifact_state(artifact, destination, context))
    return states


def _artifact_probe(request: AdapterRequest, states: list[ArtifactState]) -> JsonObject:
    items = [_artifact_evidence(state) for state in states]
    conflict = next((state for state in states if state.conflict is not None), None)
    if conflict is not None:
        return result(request, status="blocked", error_kind=conflict.conflict, retry_safe=True, evidence_items=items, repair=f"Managed artifact {conflict.artifact_id} at {conflict.destination}: {conflict.conflict}. Re-run hydration for it by hand or remove the block; it is never rewritten silently.")
    stale = [state for state in states if state.action != "none"]
    if not stale:
        return result(request, status="verified", evidence_items=items)
    if request.action_arrays_must_be_empty:
        return result(request, status="blocked", error_kind="managed_artifact_stale", retry_safe=True, evidence_items=items, repair=f"{stale[0].artifact_id} still needs {stale[0].action}.")
    actions = [planned_action(action_id=f"hydrate.{state.artifact_id}", title=f"{state.action} for managed artifact {state.artifact_id}", mutation_kind="file_write", target=state.destination, evidence_ref=f"artifact.{state.artifact_id}") for state in stale]
    return result(request, status="pending", actions=actions, evidence_items=items, repair="Approve the exact managed-artifact writes shown.")


def _artifact_apply(request: AdapterRequest, runtime: Runtime, states: list[ArtifactState]) -> JsonObject:
    for state in states:
        if state.conflict is not None:
            return blocked(request, state.conflict, f"{state.artifact_id} is in conflict; nothing was written.")
    for state in states:
        if state.action != "none" and state.new_content is not None:
            runtime.atomic_write(Path(state.destination), state.new_content, mode=state.mode)
    return result(request, status="applied", retry_safe=True, evidence_items=[_artifact_evidence(state) for state in states])


def _artifact_evidence(state: ArtifactState) -> JsonObject:
    facts = {
        "action": state.action,
        "artifact_id": state.artifact_id,
        "conflict": state.conflict or "none",
        "current_sha256": state.current_sha256 or "none",
        "destination": state.destination,
        "expected_sha256": state.expected_sha256,
        "kind": state.kind,
        "stamped_digest": state.stamped_digest or "none",
        "state": state.state,
    }
    return evidence(
        evidence_id=f"artifact.{state.artifact_id}",
        kind="managed_artifact",
        status="blocked" if state.conflict else ("verified" if state.action == "none" else "pending"),
        summary=f"managed artifact {state.artifact_id} is {state.state}",
        observed=[f"{key}={value}" for key, value in sorted(facts.items())],
        expected="stamped_current",
        source=state.destination,
    )


def _artifact_state(artifact: ArtifactDeclaration, destination: str, context: _Context) -> ArtifactState:
    path = Path(destination)
    existing = read_text(path)
    mode = file_mode(path, 0o644)
    if artifact.kind == "managed_block":
        return _managed_block_state(artifact, destination, existing, mode, context)
    return _whole_file_state(artifact, destination, existing, mode, context)


class _Outcome:
    """Builds the closed ``ArtifactState`` rows for one artifact against its current bytes."""

    def __init__(self, artifact: ArtifactDeclaration, destination: str, existing: str | None, mode: int) -> None:
        self.artifact = artifact
        self.destination = destination
        self.existing = existing
        self.mode = mode
        self.current_digest = None if existing is None else sha256_text(existing)

    def state(self, state: str, action: str, stamped: str | None, conflict: str | None, new_content: str | None) -> ArtifactState:
        expected = sha256_text(new_content) if new_content is not None else (self.current_digest or sha256_text(""))
        return ArtifactState(self.artifact.artifact_id, self.artifact.kind, self.destination, state, action, stamped, self.current_digest, expected, conflict, new_content, self.mode)


def _managed_block_state(artifact: ArtifactDeclaration, destination: str, existing: str | None, mode: int, context: _Context) -> ArtifactState:
    outcome = _Outcome(artifact, destination, existing, mode)
    begin_template, end_template = cast(str, artifact.marker_begin), cast(str, artifact.marker_end)
    candidate_body = _block_body(_template_bytes(context.request.target, artifact.template_ref), artifact, context.request)
    begin_line, end_line = marker_lines(begin_template, end_template, context.request.name, artifact.template_digest)
    candidate_block = block_text(begin_line, candidate_body, end_line)
    current = existing or ""
    blocks = find_blocks(current, begin_template, end_template, context.request.name)
    if len(blocks) > 1:
        return outcome.state("duplicate_block", "none", None, "duplicate_managed_block", None)
    if not blocks:
        return outcome.state("absent", "append_block", None, None, append_block(current, candidate_block))
    match = blocks[0]
    if match.stamped:
        return _stamped_block_state(outcome, match, current, candidate_block, end_line, context)
    return _legacy_block_state(outcome, match, current, candidate_block, context)


def _stamped_block_state(outcome: _Outcome, match: BlockMatch, current: str, candidate_block: str, end_line: str, context: _Context) -> ArtifactState:
    stamped = _full_digest(match, outcome.artifact)
    block_bytes = current[match.start : match.end]
    if block_bytes == candidate_block:
        return outcome.state("stamped_current", "none", stamped, None, None)
    previous_body = _previous_body(outcome.artifact, stamped, context)
    if previous_body is not None and block_bytes == block_text(match.begin_line, previous_body, end_line):
        return outcome.state("stamped_previous", "replace_block", stamped, None, replace_block(current, match, candidate_block))
    return outcome.state("conflict", "none", stamped, "managed_block_conflict", None)


def _legacy_block_state(outcome: _Outcome, match: BlockMatch, current: str, candidate_block: str, context: _Context) -> ArtifactState:
    for digest in (*outcome.artifact.previous_template_digests, outcome.artifact.template_digest):
        previous_body = _previous_body(outcome.artifact, digest, context)
        if previous_body is not None and match.body.rstrip("\n") == previous_body.rstrip("\n"):
            return outcome.state("legacy_matched", "replace_block", None, None, replace_block(current, match, candidate_block))
    return outcome.state("unknown_origin", "none", None, "managed_block_unknown_origin", None)


def _full_digest(match: BlockMatch, artifact: ArtifactDeclaration) -> str:
    digest8 = cast(str, match.stamped_digest8)
    for digest in (artifact.template_digest, *artifact.previous_template_digests):
        if digest.removeprefix("sha256:").startswith(digest8):
            return digest
    return f"sha256:{digest8}"


def _whole_file_state(artifact: ArtifactDeclaration, destination: str, existing: str | None, mode: int, context: _Context) -> ArtifactState:
    outcome = _Outcome(artifact, destination, existing, mode)
    candidate = _whole_render(artifact, _template_bytes(context.request.target, artifact.template_ref), artifact.template_digest, context, stamped=True)
    if existing is None:
        return _absent_whole_state(outcome, candidate)
    if existing == candidate:
        return outcome.state("stamped_current", "none", artifact.template_digest, None, None)
    stamped = stamped_digest(existing, cast(str, artifact.stamp), artifact.template_ref)
    if stamped is not None:
        return _stamped_whole_state(outcome, existing, stamped, candidate, context)
    return _unstamped_whole_state(outcome, existing, candidate, context)


def _absent_whole_state(outcome: _Outcome, candidate: str) -> ArtifactState:
    """A missing LaunchAgent plist is created; a missing ``rendered_whole`` file is left missing, because it is only ever refreshed."""
    if outcome.artifact.kind == "rendered_whole":
        return outcome.state("absent", "none", None, None, None)
    return outcome.state("absent", "render_whole", None, None, candidate)


def _stamped_whole_state(outcome: _Outcome, existing: str, stamped: str, candidate: str, context: _Context) -> ArtifactState:
    artifact = outcome.artifact
    previous = _template_bytes_by_digest(artifact, stamped, context)
    if previous is not None and existing == _whole_render(artifact, previous, stamped, context, stamped=True):
        return outcome.state("stamped_previous", "render_whole", stamped, None, candidate)
    return outcome.state("locally_modified", "none", stamped, _edit_conflict(artifact, "managed_file_locally_modified"), None)


def _unstamped_whole_state(outcome: _Outcome, existing: str, candidate: str, context: _Context) -> ArtifactState:
    artifact = outcome.artifact
    for digest in (*artifact.previous_template_digests, artifact.template_digest):
        previous = _template_bytes_by_digest(artifact, digest, context)
        if previous is not None and existing == _whole_render(artifact, previous, digest, context, stamped=False):
            return outcome.state("legacy_matched", "render_whole", None, None, candidate)
    return outcome.state("unknown_origin", "none", None, _edit_conflict(artifact, "managed_block_unknown_origin"), None)


def _edit_conflict(artifact: ArtifactDeclaration, reason: str) -> str | None:
    """An edited Manager-owned file blocks the update; an edited ``rendered_whole`` file is reported in its state row and left in place."""
    return None if artifact.kind == "rendered_whole" else reason


def _whole_render(artifact: ArtifactDeclaration, template_bytes: bytes, digest: str, context: _Context, *, stamped: bool) -> str:
    request = context.request
    if artifact.kind == "launchd_plist":
        stamp = stamp_line(cast(str, artifact.stamp), artifact.template_ref, digest) if stamped else None
        return render_launchagent_plist(request.name, request.target, context.runtime.home, template_text=template_bytes.decode("utf-8"), stamp=stamp, stamped=stamped).decode("utf-8")
    body = render_tokens(template_bytes.decode("utf-8"), _values(request))
    if not stamped:
        return body
    return insert_stamp(body, stamp_line(cast(str, artifact.stamp), artifact.template_ref, digest), after_line=_stamp_position(body))


def _stamp_position(body: str) -> int:
    """How many lines stay above the stamp: a shebang line, or a front-matter block that must remain first in the file."""
    lines = body.split("\n")
    if lines[0].startswith("#!"):
        return 1
    if lines[0] == "---":
        closing = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), None)
        if closing is not None:
            return closing + 1
    return 0


def _block_body(template_bytes: bytes, artifact: ArtifactDeclaration, request: AdapterRequest) -> str:
    rendered = render_tokens(template_bytes.decode("utf-8"), _values(request))
    return strip_marker_lines(rendered, cast(str, artifact.marker_begin), cast(str, artifact.marker_end), request.name)


def _previous_body(artifact: ArtifactDeclaration, digest: str, context: _Context) -> str | None:
    template_bytes = _template_bytes_by_digest(artifact, digest, context)
    return None if template_bytes is None else _block_body(template_bytes, artifact, context.request)


def _template_bytes_by_digest(artifact: ArtifactDeclaration, digest: str, context: _Context) -> bytes | None:
    """Template bytes for ``digest``: the candidate tree's templates first, then predecessor history."""
    templates = context.request.target / TEMPLATE_ROOT_REF
    if templates.is_dir():
        for path in sorted(templates.iterdir()):
            if path.is_file() and sha256_bytes(path.read_bytes()) == digest:
                return path.read_bytes()
    for commit in context.predecessors:
        outcome = context.runtime.run(("git", "-C", str(context.request.target), "show", f"{commit}:{artifact.template_ref}"), timeout_seconds=30, extra_env=dict(_HARDENED_GIT_ENV), output_limit=STRUCTURED_OUTPUT_LIMIT)
        if outcome.ok and not outcome.stdout_truncated and sha256_bytes(outcome.stdout.encode("utf-8")) == digest:
            return outcome.stdout.encode("utf-8")
    return None


def _values(request: AdapterRequest) -> dict[str, str]:
    shell_file = request.target / "client" / f"{request.name}.zsh"
    return {
        "{{SOLET_NAME}}": request.name,
        "{{CLONE_DIR}}": str(request.target),
        "{{CLONE_DIR_ZSH}}": zsh_quote(str(request.target)),
        "{{SHELL_FILE_ZSH}}": zsh_quote(str(shell_file)),
        "{{MARKETPLACE_NAME}}": request.name.replace("_", "-"),
    }


def _template_bytes(target: Path, template_ref: str) -> bytes:
    return (target / template_ref).read_bytes()


def _resolve_destination(artifact: ArtifactDeclaration, request: AdapterRequest, runtime: Runtime) -> str:
    return (
        artifact.logical_destination.replace("{HOME}", str(runtime.home))
        .replace("{TARGET}", str(request.target))
        .replace("{PROFILE_HOME}", str(request.target / "profile"))
        .replace("{NAME}", request.name)
    )


def _planned_destinations(request: AdapterRequest) -> dict[str, str]:
    raw = request.public_inputs.get("planned_destinations", [])
    planned: dict[str, str] = {}
    if isinstance(raw, list):
        for item in cast(list[JsonValue], raw):
            if isinstance(item, str) and "=" in item:
                key, _, value = item.partition("=")
                planned[key] = value
    return planned


def _artifacts(bundle: JsonObject) -> list[ArtifactDeclaration]:
    rows: list[ArtifactDeclaration] = []
    for raw in cast(list[JsonValue], bundle["managed_artifacts"]):
        row = cast(JsonObject, raw)
        marker = row["marker"]
        rows.append(
            ArtifactDeclaration(
                str(row["artifact_id"]),
                str(row["kind"]),
                str(row["logical_destination"]),
                str(cast(JsonObject, marker)["begin"]) if isinstance(marker, dict) else None,
                str(cast(JsonObject, marker)["end"]) if isinstance(marker, dict) else None,
                cast(str | None, row["stamp"]),
                str(row["template_ref"]),
                str(row["template_digest"]),
                tuple(cast(list[str], row["previous_template_digests"])),
            )
        )
    return rows


def _load_bundle(target: Path) -> JsonObject:
    raw: object = json.loads((target / BUNDLE_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("existing_install_flow.json must be an object")
    return cast(JsonObject, raw)


__all__ = [
    "BUNDLE_PATH",
    "EXISTING_ALLOWED_PUBLIC_INPUTS",
    "SEED_OPERATION_REFS",
    "ArtifactState",
    "autostart_reconcile",
    "hydration_reconcile",
    "migration_export_root_containment",
    "migration_solet_rename",
    "operation_handlers",
    "plugin_cache_refresh",
]
