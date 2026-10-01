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
conflict, never a side to pick.  Two exceptions.  A ``launchd_plist`` is Manager-owned
(``manager_generated_whole``): one that parses, carries this solet's own label and launches
``ananta.cli`` directly (``legacy_direct``) is adopted, because its replacement is shown with a
bounded diff, bound into the approval by the digest of the bytes it replaces, and backed up by the
Manager before the write, so it is not a side picked silently; any other plist still blocks.  The other is a ``rendered_whole``
file (a user-scope file such as the ``/feedback`` skill): it is only ever
refreshed, so a missing one is not created and an edited one is reported in its
state row (``locally_modified`` or ``unknown_origin``) and left in place without
blocking the update.  A ``rendered_whole`` file that declares a ``section_end``
(the fleet launcher, which also holds the operator's own role functions) is
refreshed above that line only.
"""

from __future__ import annotations

import difflib
import json
import plistlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import cast

from .autostart import render_launchagent_plist
from .existing_install_migrations import (
    STRUCTURED_OUTPUT_LIMIT,
    NotTextError,
    blocked,
    file_mode,
    migration_export_root_containment,
    migration_solet_rename,
    not_text_blocked,
    plugin_cache_refresh,
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
from .setup_adapter_contract import AdapterRequest, JsonObject, JsonValue, evidence, neutralized, planned_action, result
from .setup_adapter_runtime import Runtime
from .target_reconciliation import PLIST_PARSE_ERRORS, LaunchTopology, detect_topology, plist_label

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
    """The closed seed-side ``existing::`` handler table merged into ``setup_adapter``; every handler blocks on a file that is not text."""
    return {
        ref: _blocking_non_text(handler)
        for ref, handler in {
            "existing::migration.solet_rename": migration_solet_rename,
            "existing::migration.export_root_containment": migration_export_root_containment,
            "existing::migration.plugin_transition": migration_plugin_transition,
            "existing::hydration.reconcile": hydration_reconcile,
            "existing::autostart.reconcile": autostart_reconcile,
            "existing::runtime.plugin_cache_refresh": plugin_cache_refresh,
        }.items()
    }


def _blocking_non_text(handler: Handler) -> Handler:
    """A ``NotTextError`` out of any file the handler reads is a blocked result with a repair, never an exception out of the adapter (iss_68bc97bb)."""

    @wraps(handler)
    def guarded(request: AdapterRequest, runtime: Runtime) -> JsonObject:
        try:
            return handler(request, runtime)
        except NotTextError as exc:
            return not_text_blocked(request, exc)

    return guarded


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
    section_end: str | None = None


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
    adopt_diff: str | None = None


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
    items = _probe_evidence(states)
    conflict = next((state for state in states if state.conflict is not None), None)
    if conflict is not None:
        return result(request, status="blocked", error_kind=conflict.conflict, retry_safe=True, evidence_items=items, repair=_conflict_repair(conflict))
    stale = [state for state in states if state.action != "none"]
    if not stale:
        return result(request, status="verified", evidence_items=items)
    if request.action_arrays_must_be_empty:
        return result(request, status="blocked", error_kind="managed_artifact_stale", retry_safe=True, evidence_items=items, repair=f"{stale[0].artifact_id} still needs {stale[0].action}.")
    actions = [planned_action(action_id=f"hydrate.{state.artifact_id}", title=f"{state.action} for managed artifact {state.artifact_id}", mutation_kind="file_write", target=state.destination, evidence_ref=f"artifact.{state.artifact_id}") for state in stale]
    return result(request, status="pending", actions=actions, evidence_items=items, repair="Approve the exact managed-artifact writes shown.")


def _probe_evidence(states: list[ArtifactState]) -> list[JsonObject]:
    """One state row per artifact, then one ``adopt_diff.`` row per adopted file; that id is not ``artifact.``-prefixed, which the Manager decodes as a state row."""
    return [*(_artifact_evidence(state) for state in states), *(_adopt_diff_evidence(state) for state in states if state.adopt_diff is not None)]


def _conflict_repair(state: ArtifactState) -> str:
    if state.kind == "launchd_plist":
        return (
            f"The LaunchAgent plist at {state.destination} is not a legacy_direct plist for this solet (its Label is not local.solet.<name>, it does not launch ananta.cli directly, or it does not parse), so the update will not replace it. "
            "Replace it by hand: see the seed update runbook, Part C Step 5."
        )
    return f"Managed artifact {state.artifact_id} at {state.destination}: {state.conflict}. Re-run hydration for it by hand or remove the block; it is never rewritten silently."


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


def _adopt_diff_evidence(state: ArtifactState) -> JsonObject:
    return evidence(
        evidence_id=f"adopt_diff.{state.artifact_id}",
        kind="managed_artifact_adopt_diff",
        status="pending",
        summary=f"replacing the unrecognised {state.artifact_id} with the release's render changes these lines",
        observed=[f"{index:03d}={line}" for index, line in enumerate(cast(str, state.adopt_diff).split("\n"))],
        expected="the release's stamped render",
        source=state.destination,
    )


def _read_managed(path: Path) -> tuple[str | None, str | None]:
    """The file's text, read the way ``read_text`` reads it (UTF-8, newlines translated), and the digest of the raw bytes it was decoded from.

    The digest is of the bytes the update will replace, so the Manager's backup of them, which hashes raw bytes, agrees with it for a file with CRLF line
    endings too; a digest of the translated text would not.  Both come from one read.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotTextError(path) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n"), sha256_bytes(raw)


def _artifact_state(artifact: ArtifactDeclaration, destination: str, context: _Context) -> ArtifactState:
    path = Path(destination)
    existing, digest = _read_managed(path)
    mode = file_mode(path, 0o644)
    if artifact.kind == "managed_block":
        return _managed_block_state(artifact, destination, existing, digest, mode, context)
    return _whole_file_state(artifact, destination, existing, digest, mode, context)


class _Outcome:
    """Builds the closed ``ArtifactState`` rows for one artifact against its current bytes."""

    def __init__(self, artifact: ArtifactDeclaration, destination: str, existing: str | None, digest: str | None, mode: int) -> None:
        self.artifact = artifact
        self.destination = destination
        self.existing = existing
        self.mode = mode
        self.current_digest = digest

    def state(self, state: str, action: str, stamped: str | None, conflict: str | None, new_content: str | None, *, adopt_diff: str | None = None) -> ArtifactState:
        expected = sha256_text(new_content) if new_content is not None else (self.current_digest or sha256_text(""))
        return ArtifactState(self.artifact.artifact_id, self.artifact.kind, self.destination, state, action, stamped, self.current_digest, expected, conflict, new_content, self.mode, adopt_diff)


def _managed_block_state(artifact: ArtifactDeclaration, destination: str, existing: str | None, digest: str | None, mode: int, context: _Context) -> ArtifactState:
    outcome = _Outcome(artifact, destination, existing, digest, mode)
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


def _whole_file_state(artifact: ArtifactDeclaration, destination: str, existing: str | None, digest: str | None, mode: int, context: _Context) -> ArtifactState:
    if artifact.section_end is not None:
        return _section_state(artifact, destination, existing, digest, mode, context)
    outcome = _Outcome(artifact, destination, existing, digest, mode)
    candidate = _whole_render(artifact, _template_bytes(context.request.target, artifact.template_ref), artifact.template_digest, context, stamped=True)
    if existing is None:
        return _absent_whole_state(outcome, candidate)
    if existing == candidate:
        return outcome.state("stamped_current", "none", artifact.template_digest, None, None)
    stamped = stamped_digest(existing, cast(str, artifact.stamp), artifact.template_ref)
    if stamped is not None:
        return _stamped_whole_state(outcome, existing, stamped, candidate, context)
    return _unstamped_whole_state(outcome, existing, candidate, context)


_CONTROLLER_LINE = re.compile(r'^[ \t]*GIT_CONTROLLER_NAME="([^"\n]*)" \\$', re.MULTILINE)


def _section_state(artifact: ArtifactDeclaration, destination: str, existing: str | None, digest: str | None, mode: int, context: _Context) -> ArtifactState:
    """Refresh only the launcher section of a file the operator also writes into.

    The section is the text above the first line starting with ``section_end``; the operator's role functions
    below it are kept byte-for-byte.  The one operator-chosen value inside the section, the ``GIT_CONTROLLER_NAME``
    line, is read from the file and rendered back unchanged, so a section that differs from a known render only
    in that value still matches it.  A missing file is not created; a section that matches no known render, or
    that has lost its marker or its controller line, is reported and left in place without blocking the update.
    """
    outcome = _Outcome(artifact, destination, existing, digest, mode)
    if existing is None:
        return outcome.state("absent", "none", None, None, None)
    split = _split_section(existing, cast(str, artifact.section_end))
    controller = None if split is None else _CONTROLLER_LINE.search(split[0])
    if split is None or controller is None:
        return outcome.state("unknown_origin", "none", None, None, None)
    head, tail = split
    values = {"{{GIT_CONTROLLER_NAME}}": controller.group(1)}
    candidate_head = _render_section(artifact, _template_bytes(context.request.target, artifact.template_ref), artifact.template_digest, context, values, stamped=True)
    if candidate_head is None:
        raise RuntimeError(f"{artifact.artifact_id}: the candidate template has no line starting {artifact.section_end!r}")
    if head == candidate_head:
        return outcome.state("stamped_current", "none", artifact.template_digest, None, None)
    return _older_section_state(outcome, head, candidate_head + tail, values, context)


def _older_section_state(outcome: _Outcome, head: str, refreshed: str, values: dict[str, str], context: _Context) -> ArtifactState:
    """A section that is not the candidate's: a previous render is replaced, anything else is reported and left."""
    artifact = outcome.artifact
    stamped = stamped_digest(head, cast(str, artifact.stamp), artifact.template_ref)
    if stamped is not None:
        previous = _template_bytes_by_digest(artifact, stamped, context)
        if previous is not None and head == _render_section(artifact, previous, stamped, context, values, stamped=True):
            return outcome.state("stamped_previous", "render_whole", stamped, None, refreshed)
        return outcome.state("locally_modified", "none", stamped, None, None)
    for digest in (*artifact.previous_template_digests, artifact.template_digest):
        previous = _template_bytes_by_digest(artifact, digest, context)
        if previous is not None and head == _render_section(artifact, previous, digest, context, values, stamped=False):
            return outcome.state("legacy_matched", "render_whole", None, None, refreshed)
    return outcome.state("unknown_origin", "none", None, None, None)


def _render_section(artifact: ArtifactDeclaration, template_bytes: bytes, digest: str, context: _Context, values: dict[str, str], *, stamped: bool) -> str | None:
    """The launcher section of one render of the template, or ``None`` when that template has no section marker."""
    rendered = _whole_render(artifact, template_bytes, digest, context, stamped=stamped, extra_values=values)
    part = _split_section(rendered, cast(str, artifact.section_end))
    return None if part is None else part[0]


def _split_section(text: str, section_end: str) -> tuple[str, str] | None:
    """``(refreshed section, kept remainder)``, split before the first line that starts with ``section_end``."""
    offset = 0
    for line in text.split("\n"):
        if line.startswith(section_end):
            return text[:offset], text[offset:]
        offset += len(line) + 1
    return None


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
    return _unmatched_whole_state(outcome, "locally_modified", stamped, "managed_file_locally_modified", candidate, context)


def _unstamped_whole_state(outcome: _Outcome, existing: str, candidate: str, context: _Context) -> ArtifactState:
    artifact = outcome.artifact
    for digest in (*artifact.previous_template_digests, artifact.template_digest):
        previous = _template_bytes_by_digest(artifact, digest, context)
        if previous is not None and existing == _whole_render(artifact, previous, digest, context, stamped=False):
            return outcome.state("legacy_matched", "render_whole", None, None, candidate)
    return _unmatched_whole_state(outcome, "unknown_origin", None, "managed_block_unknown_origin", candidate, context)


def _unmatched_whole_state(outcome: _Outcome, state: str, stamped: str | None, reason: str, candidate: str, context: _Context) -> ArtifactState:
    """A whole file that matches no known render: an adoptable plist is replaced with its diff shown, anything else is a conflict."""
    if _adoptable_plist(outcome, context.request.name):
        return outcome.state(state, "render_whole", stamped, None, candidate, adopt_diff=_adopt_diff(cast(str, outcome.existing), candidate))
    return outcome.state(state, "none", stamped, _edit_conflict(outcome.artifact, reason), None)


def _adoptable_plist(outcome: _Outcome, name: str) -> bool:
    """A plist this solet owns: the Label is this solet's own and it launches ``ananta.cli`` directly.

    A plist for another label, a materialized supervisor (``iss_5c2598a7``) or one that does not parse is never adopted:
    replacing it would hand the operator's launch to a render they did not write or move the solet off its topology.
    """
    if outcome.artifact.kind != "launchd_plist":
        return False
    path = Path(outcome.destination)
    return plist_label(path) == f"local.solet.{name}" and detect_topology(path) == LaunchTopology.LEGACY_DIRECT


_ADOPT_DIFF_LINES = 80
_ADOPT_DIFF_COLUMNS = 200


def _adopt_diff(existing: str, candidate: str) -> str:
    """The diff from the plist on disk to the candidate render, at most 80 lines of at most 200 characters, secret values redacted.

    It compares the parsed plists, not their bytes: each is parsed, every value under a secret-named key (and every value that repeats one) becomes
    ``[REDACTED]``, and the two are written back out canonically (XML, keys sorted).  However the file spells a value, entities, CDATA or tag
    whitespace included, the secret is gone before any text exists; layout, key order and comments are no change.  A plist that cannot be parsed gets
    no diff at all, and the adopt decision does not depend on this text.
    """
    try:
        trees = [plistlib.loads(text.encode("utf-8")) for text in (existing, candidate)]
        secrets = {value for tree in trees for value in _values_under_secret_keys(tree, secret=False)}
        texts = [plistlib.dumps(cast(dict[str, object], _redacted(tree, secrets, secret=False)), fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8") for tree in trees]
    except PLIST_PARSE_ERRORS:
        return "diff withheld: the plist could not be parsed, so no part of it is shown; compare the file with the candidate by hand"
    lines = list(difflib.unified_diff(_redacted_lines(texts[0]), _redacted_lines(texts[1]), "current", "candidate", lineterm="", n=1))
    if not lines:
        return "the plists hold the same values; they differ only in layout, key order or comments"
    kept = [line[:_ADOPT_DIFF_COLUMNS] for line in lines[:_ADOPT_DIFF_LINES]]
    if len(lines) > _ADOPT_DIFF_LINES:
        kept.append(f"... {len(lines) - _ADOPT_DIFF_LINES} more diff lines not shown")
    return "\n".join(kept)


#: Names whose value the diff never carries.  The house conventions, ``setup_adapter_contract._SECRET_KEY`` (seed evidence) and
#: ``solet_manager.adapter_validation._SECRET_PATTERNS`` (Manager evidence), name password, secret, token, api key, private key and credential; this set
#: also names a passphrase, authorization and ``DSN``, and a whole-word ``PASS``, ``PAT`` or ``KEY`` (``DB_PASS``, ``GH_PAT``, ``STRIPE_KEY``; not ``PATH``,
#: ``KEYBOARD`` or ``KEYCHAIN``).
_SECRET_PLIST_KEY = re.compile(
    r"(?i)password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|credential|authorization|dsn|(?<![a-z0-9])(?:pass|pat|key)(?![a-z0-9])"
)
#: A line of an evidence ``observed`` array is held to the Manager's ``public_string`` rules by ``setup_adapter_contract.neutralized`` below, which replaces a
#: secret-shaped run with ``[REDACTED]`` and a Homebrew keg path with ``[keg path]`` and leaves the rest of the line readable (iss_67472e3f).  A refusal
#: there raises out of the whole preview, so the seed never hands the Manager a line that breaks one.  The seed does not import the Manager.
_MIN_CONTAINED_SECRET = 6
_REDACTED = "[REDACTED]"


def _values_under_secret_keys(node: object, *, secret: bool) -> Iterator[str | bytes]:
    """Every string or ``<data>`` value under a key named like a secret, at any depth: the values a diff must not carry."""
    if isinstance(node, dict):
        for key, value in cast(dict[object, object], node).items():
            yield from _values_under_secret_keys(value, secret=secret or _SECRET_PLIST_KEY.search(str(key)) is not None)
    elif isinstance(node, list):
        for item in cast(list[object], node):
            yield from _values_under_secret_keys(item, secret=secret)
    elif secret and isinstance(node, str | bytes) and node:
        yield node


def _redacted(node: object, secrets: set[str | bytes], *, secret: bool) -> object:
    """The plist tree with every value under a secret-named key, and every string or data value equal to a collected secret, replaced.

    Out of scope, deliberately: a secret reused as a dict key, or re-encoded (base64 data for a string secret, a string for a data secret); the diff
    shows those as they are.
    """
    if secret:
        return _REDACTED
    if isinstance(node, dict):
        return {key: _redacted(value, secrets, secret=_SECRET_PLIST_KEY.search(str(key)) is not None) for key, value in cast(dict[str, object], node).items()}
    if isinstance(node, list):
        return [_redacted(item, secrets, secret=False) for item in cast(list[object], node)]
    if isinstance(node, str | bytes) and _repeats_secret(node, secrets):
        return _REDACTED
    return node


def _repeats_secret(value: str | bytes, secrets: set[str | bytes]) -> bool:
    """Whether a value under a plain key equals a secret found under a secret-named key (any length), or a string holds one of at least 6 characters."""
    if value in secrets:
        return True
    return isinstance(value, str) and any(isinstance(secret, str) and len(secret) >= _MIN_CONTAINED_SECRET and secret in value for secret in secrets)


def _redacted_lines(text: str) -> list[str]:
    return [neutralized(line) for line in text.splitlines()]


def _edit_conflict(artifact: ArtifactDeclaration, reason: str) -> str | None:
    """An edited Manager-owned file blocks the update; an edited ``rendered_whole`` file is reported in its state row and left in place."""
    return None if artifact.kind == "rendered_whole" else reason


def _whole_render(artifact: ArtifactDeclaration, template_bytes: bytes, digest: str, context: _Context, *, stamped: bool, extra_values: dict[str, str] | None = None) -> str:
    request = context.request
    if artifact.kind == "launchd_plist":
        stamp = stamp_line(cast(str, artifact.stamp), artifact.template_ref, digest) if stamped else None
        return render_launchagent_plist(request.name, request.target, context.runtime.home, template_text=template_bytes.decode("utf-8"), stamp=stamp, stamped=stamped).decode("utf-8")
    body = render_tokens(template_bytes.decode("utf-8"), {**_values(request), **(extra_values or {})})
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
                cast(str | None, row.get("section_end")),
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
