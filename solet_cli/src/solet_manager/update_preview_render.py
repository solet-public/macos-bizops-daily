"""Preview rendering for ``solet-manager update --dry-run`` (Step 4 source preview, Step 5 runtime preview,
Step 6 resume/terminal previews).

Split out of ``update_execution`` for maintainability: this module only renders ``CommandResult`` payloads
from a probe or a journal; every write-capable path stays in ``update_execution``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from .errors import HostRequirementError, SourceTransitionIncompleteError, UpdateBlockedError
from .models import ActiveOperation, CommandResult, ExitCode, InstanceInventoryRecordV2, JsonValue
from .paths import ManagerPaths, update_candidate_cache
from .update_enrollment import PendingEnrollment, plan_create_origin_enrollment
from .update_execution import (
    NON_TOUCH_SURFACES,
    PREVIEW_KIND,
    REPAIR_RELEASE_POINTER,
    STEP4_CAPABILITIES,
    UpdateProbe,
    UpdateRequest,
    _candidate_dict,
    _candidate_execution,
    load_update_record,
    probe_update,
    target_head,
    terminal_repair,
)
from .update_journal import DOCTOR_STATUSES, RUNTIME_STATUSES, TERMINAL_UPDATE_STATUSES, read_update_journal
from .update_preview import VERIFY_PREVIEW_MESSAGE, VERIFY_PREVIEW_STATUS
from .update_runtime_plan import build_runtime_plan
from .update_runtime_plan_codec import plan_preview_data
from .update_topology import shape_changed_rows

__all__ = ["preview_instance"]


def preview_instance(request: UpdateRequest) -> CommandResult:
    """Render the closed update preview; may write only the Manager candidate cache."""
    try:
        pending = plan_create_origin_enrollment(request)
    except UpdateBlockedError as exc:
        return _blocked_preview(exc)
    if pending is not None:
        return _enrollment_preview(request, pending)
    record = load_update_record(request)
    if record.active_operation is not None:
        return _resume_preview(request, record)
    try:
        probe = probe_update(request, record)
    except UpdateBlockedError as exc:
        return _blocked_preview(exc)
    return _preview_result(probe, request.manager_paths)


def _enrollment_preview(request: UpdateRequest, pending: PendingEnrollment) -> CommandResult:
    """iss_836499b3: the update of a not-yet-enrolled Manager-created instance, enrollment disclosed and bound."""
    try:
        probe = probe_update(request, pending.record, enrollment=pending.binding())
    except UpdateBlockedError as exc:
        result = _blocked_preview(exc)
    else:
        result = _preview_result(probe, request.manager_paths)
    data = {**result.data, "enrollment": pending.disclosure()}
    if result.status in {"preview_ready", VERIFY_PREVIEW_STATUS}:
        if pending.preview.superseded_operation_id is not None:
            enrolls = f"supersedes the stale enrollment {pending.preview.superseded_operation_id} and enrolls"
        else:
            enrolls = "finishes the interrupted enrollment of" if pending.resuming else "enrolls"
        message = f"{result.message} Approving it first {enrolls} this Manager-created Solet (no separate import)."
        return replace(result, message=message, data=data)
    return replace(result, data=data)


def _preview_data(probe: UpdateProbe, paths: ManagerPaths) -> dict[str, JsonValue]:
    record, candidate, facts = probe.record, probe.candidate, probe.baseline.facts
    local_state = probe.local_state
    blocked_paths = cast(list[JsonValue], sorted({path for paths in probe.blocked_paths.values() for path in paths}))
    by_reason: dict[str, JsonValue] = {reason: cast(list[JsonValue], list(paths)) for reason, paths in sorted(probe.blocked_paths.items())}
    shape_changes: list[JsonValue] = [row.to_dict() for row in shape_changed_rows(facts.tracked_entries.values)]
    blocked: dict[str, JsonValue] = {"paths": blocked_paths, "by_reason": by_reason, "shape_changes": shape_changes, "repair": _blocked_repair(probe)}
    return {
        # Step 7 section 7.2: the host group renders before the source/channel group; it is disclosed, never fingerprinted.
        "host": probe.host,
        "instance": {
            "instance_id": record.instance_id,
            "name": record.name,
            "canonical_target": record.target.canonical_path,
            "management_state": record.management_state.value,
        },
        "baseline": {
            "commit": record.source_release.commit,
            "tree": record.source_release.tree,
            "tag": record.source_release.tag,
            "branch": facts.branch,
            "origins": list(facts.origins),
            "working_tree": facts.working_tree.value,
            "identity_status": facts.identity_status.value,
        },
        "candidate": _candidate_dict(candidate),
        # Design §7.3: the pairing verdict is disclosed like `host`, never fingerprinted.
        "release_identity": dict(candidate.release_identity),
        "candidate_cache": {
            "status": candidate.cache_status,
            "descriptor_digest": candidate.descriptor_digest,
            "receipt_digest": candidate.receipt_digest,
            "repository_path": str(update_candidate_cache(paths, candidate.descriptor_digest).repository),
            "receipt_path": str(update_candidate_cache(paths, candidate.descriptor_digest).receipt),
        },
        "topology": {"reasons": list(probe.reasons), "actionable": probe.preview.topology.actionable},
        "collisions": [{"reason": row.reason, "path": row.path} for row in probe.collisions],
        "blocked": blocked,
        # Step 7 section 6.3: the tracked list, the committed inventory (never per-entry digests) and the preserved surface.
        "local_state": None
        if local_state is None
        else {
            "preserved_tracked_paths": [{"path": path, "sha256": digest, "size": size} for path, digest, size in local_state.state.preserved_tracked_paths],
            "committed": local_state.state.committed_rows(),
            "local_state_commitment": local_state.state.local_state_commitment,
            "preserved_surface": [{"path": path, "kind": kind, "mode": mode, "size": size} for path, kind, mode, size in local_state.state.preserved_surface],
            # iss_f1d8cfc2: preserved tracked paths proved byte-exact installer interpreter pins (disclosed, bound by digest above).
            "installer_pins": list(probe.installer_pins),
        },
        "planned_actions": list(probe.planned_actions),
        "source_mode": probe.source_mode,
        "candidate_ref": candidate.candidate_ref,
        "operation_id": probe.operation_id,
        "preservation": {
            "target_byte_writes": 0,
            "manager_state_writes": 0,
            "manager_cache_writes": 1 if candidate.cache_status == "acquired" else 0,
            "non_touch_surfaces": list(NON_TOUCH_SURFACES),
            "capabilities": list(STEP4_CAPABILITIES),
            "unreachable": ["adapters", "dependencies", "migrations", "hydration", "launchd_router", "restart", "doctor", "promotion"],
        },
        "approval_fingerprint": probe.fingerprint,
    }


def _preview_result(probe: UpdateProbe, paths: ManagerPaths) -> CommandResult:
    data = _preview_data(probe, paths)
    if probe.source_mode == "verify" and probe.fingerprint is not None:
        return CommandResult(PREVIEW_KIND, VERIFY_PREVIEW_STATUS, VERIFY_PREVIEW_MESSAGE.format(tag=probe.candidate.fields.release_tag), ExitCode.OK, data=data)
    if "already_current" in probe.reasons and probe.source_mode == "advance":
        return CommandResult(PREVIEW_KIND, "already_current", "Enrolled source already equals the installed channel release.", ExitCode.OK, data=data)
    if probe.fingerprint is None:
        return CommandResult(
            PREVIEW_KIND,
            "awaiting_user",
            "Update preview is blocked before any target write.",
            ExitCode.HUMAN_ACTION,
            "update_blocked",
            "Resolve every listed reason, then preview again.",
            data=data,
        )
    return CommandResult(PREVIEW_KIND, "preview_ready", "Update preview completed; approve with --yes --approval-fingerprint.", ExitCode.OK, data=data)


_OVERLAP_REPAIR = (
    "The candidate release changes {paths}, which this installation has modified locally. The Manager never overwrites, "
    "stashes or resets a local change. Resolve by hand: compare your copy against the candidate's "
    "(`git diff <baseline>..<candidate> -- <path>`), keep your local lines, and re-run `solet-manager update {name} --dry-run`. "
    "If the only local difference is the genesis rewrite of `solet_name:`, see the seed follow-up (design D8)."
)
#: iss_f1d8cfc2 / iss_c1a7df20: a candidate that changes a manifest carrying the installer's own interpreter pin.
_PIN_OVERLAP_REPAIR = (
    "The candidate release changes {paths}, which this installation's own installer bound to its Python interpreter "
    "(the coordination-hook interpreter pin); this Manager cannot yet carry that pin onto a changed manifest. "
    "Do not restore or edit the file -- the pin is required. Upgrade the Manager (`brew upgrade solet`), then re-run "
    "`solet-manager update {name} --dry-run`."
)
_REPAIRS = {
    "staged_changes_present": "Staged changes are refused (A7.1): `git restore --staged {paths}` is the operator's call; the Manager never runs it.",
    "tracked_shape_changed": "A deleted, retyped, mode-changed or symlinked tracked path is refused; restore {paths} to a content-only edit of the shipped regular file, then preview again.",
    "executed_code_modified": (
        "{paths} is under an executed-code root (bootstrap, an editable-installed distribution, or a roster plugin) and differs from "
        "the release's committed bytes by more than an installer write the Manager recognizes; the Manager will not execute a modified "
        "target. `git diff -- <path>` in the solet shows the local edit: undo only that edit (keep any installer write, such as the "
        "coordination-hook interpreter pin) or move your change out of the tree, then preview again."
    ),
    "git_metadata_present": "{paths} changes how the fast-forward writes candidate files (attributes: eol/text/filters; modules: gitlinks) and is never preserved through an update. Remove it, then preview again.",
    "preserved_surface_in_transition": "The candidate carries {paths} under profile/, which no sealed release may ship; this is a seed-side regression, not an installation you can repair.",
}


def _blocked_repair(probe: UpdateProbe) -> dict[str, JsonValue]:
    repairs: dict[str, JsonValue] = {}
    for reason, paths in sorted(probe.blocked_paths.items()):
        joined = ", ".join(paths)
        if reason == "tracked_overlap_present":
            repairs[reason] = _overlap_repair(paths, probe.installer_pins, probe.record.name)
        elif reason in _REPAIRS:
            repairs[reason] = _REPAIRS[reason].format(paths=joined)
    return repairs


def _overlap_repair(paths: list[str], pins: tuple[str, ...], name: str) -> str:
    """The operator's own overlaps are resolved by hand; an overlapped installer pin needs a newer Manager, never a hand edit."""
    own = [path for path in paths if path not in pins]
    pinned = [path for path in paths if path in pins]
    parts = [_OVERLAP_REPAIR.format(paths=", ".join(own), name=name)] if own else []
    if pinned:
        parts.append(_PIN_OVERLAP_REPAIR.format(paths=", ".join(pinned), name=name))
    return " ".join(parts)


def _blocked_preview(exc: UpdateBlockedError) -> CommandResult:
    data: dict[str, JsonValue] = {"topology": {"reasons": [exc.error_kind], "actionable": False}, "approval_fingerprint": None, "preservation": {"target_byte_writes": 0, "manager_state_writes": 0}}
    if isinstance(exc, HostRequirementError):
        data["host"] = exc.host
    return CommandResult(
        PREVIEW_KIND,
        "awaiting_user",
        str(exc),
        ExitCode.HUMAN_ACTION,
        exc.error_kind,
        exc.repair,
        data=data,
    )


def _resume_preview(request: UpdateRequest, record: InstanceInventoryRecordV2) -> CommandResult:
    active = cast(ActiveOperation, record.active_operation)
    journal = read_update_journal(request.manager_paths.operation_path(record.instance_id, active.operation_id))
    status = cast(str, journal["status"])
    approval = cast(dict[str, JsonValue], journal["approval"])
    data: dict[str, JsonValue] = {
        "instance": {"instance_id": record.instance_id, "name": record.name},
        "operation_id": active.operation_id,
        "journal_status": status,
        "baseline": journal["baseline"],
        "candidate": journal["candidate"],
        "attempts": len(cast(list[JsonValue], journal["attempts"])),
        "approval_fingerprint": approval["fingerprint"],
        "preservation": {"target_byte_writes": 0, "manager_state_writes": 0},
    }
    data["source_mode"] = journal["source_mode"]
    data["recovers"] = journal["recovers"]
    if status in TERMINAL_UPDATE_STATUSES:
        return _terminal_preview(request, record, journal, data)
    if status == "runtime_advanced" or status in DOCTOR_STATUSES:
        return CommandResult(PREVIEW_KIND, status, f"Runtime is advanced; `solet-manager update {record.name} --yes --approval-fingerprint <runtime fingerprint>` runs the final doctor and promotion.", ExitCode.HUMAN_ACTION, "update_in_progress", "Re-run --yes with the recorded runtime fingerprint.", data=data)
    if status == "source_advanced" or status in RUNTIME_STATUSES:
        return _runtime_preview(request, record, journal)
    return CommandResult(PREVIEW_KIND, "resume_pending", "An approved update is in flight; re-run --yes with its recorded fingerprint to resume.", ExitCode.HUMAN_ACTION, "update_in_progress", "Resume with `--yes --approval-fingerprint <recorded fingerprint>`.", data=data)


def _terminal_preview(request: UpdateRequest, record: InstanceInventoryRecordV2, journal: dict[str, JsonValue], data: dict[str, JsonValue]) -> CommandResult:
    status = cast(str, journal["status"])
    result = cast(dict[str, JsonValue], journal["result"])
    if status in {"promoted", "abandoned"}:
        return CommandResult(PREVIEW_KIND, status, f"The active pointer names a {status} update; it is released on the next --yes.", ExitCode.HUMAN_ACTION, "pointer_release_pending", REPAIR_RELEASE_POINTER.format(name=record.name), data=data)
    head = target_head(record)
    repair = terminal_repair(record.name, journal, head)
    data["head_observed"] = head
    return CommandResult(PREVIEW_KIND, status, f"The active update is terminal ({result['reason_code']}); {repair}", ExitCode.HUMAN_ACTION, cast(str, result["reason_code"]), repair, data=data)


def _runtime_preview(request: UpdateRequest, record: InstanceInventoryRecordV2, journal: dict[str, JsonValue]) -> CommandResult:
    """Section 8.2: re-prove identity, then render the probed runtime plan (target probes disclosed)."""
    status = cast(str, journal["status"])
    journal_path = request.manager_paths.operation_path(record.instance_id, cast(str, journal["operation_id"]))
    execution = _candidate_execution(request, record, journal_path, journal, cast(str, cast(dict[str, JsonValue], journal["approval"])["fingerprint"]))
    try:
        execution._verify_advanced()
    except SourceTransitionIncompleteError as exc:
        return _blocked_runtime_preview(UpdateBlockedError(exc.error_kind, str(exc), repair=exc.repair), journal)
    context = execution.plan_context("preview")
    try:
        plan = build_runtime_plan(context)
    except UpdateBlockedError as exc:
        return _blocked_runtime_preview(exc, journal)
    data = plan_preview_data(plan, context, status)
    approval = journal["runtime_approval"]
    if approval is not None:
        data["recorded_runtime_approval_fingerprint"] = cast(dict[str, JsonValue], approval)["fingerprint"]
        return CommandResult(PREVIEW_KIND, "runtime_resume_pending", "A runtime plan is approved and in flight; re-run --yes with its recorded runtime fingerprint to resume.", ExitCode.HUMAN_ACTION, "update_in_progress", "Resume with `--yes --approval-fingerprint <recorded runtime fingerprint>`.", data=data)
    if plan.fingerprint is None:
        contradiction = any(reason in {"failed"} for _, reason in plan.blocked)
        return CommandResult(
            PREVIEW_KIND,
            "awaiting_user",
            "Runtime preview is blocked before any target write.",
            ExitCode.FAILED if contradiction else ExitCode.HUMAN_ACTION,
            "runtime_plan_blocked",
            "Resolve every listed reason, then preview again.",
            data=data,
        )
    return CommandResult(PREVIEW_KIND, "runtime_preview_ready", "Runtime preview completed; approve with --yes --approval-fingerprint <runtime fingerprint>.", ExitCode.OK, data=data)


def _blocked_runtime_preview(exc: UpdateBlockedError, journal: dict[str, JsonValue]) -> CommandResult:
    """A runtime plan that cannot even be rendered is blocked with the same closed fields."""
    approval = journal["runtime_approval"]
    return CommandResult(
        PREVIEW_KIND,
        "awaiting_user",
        str(exc),
        ExitCode.HUMAN_ACTION,
        exc.error_kind,
        exc.repair,
        data={
            "operation_id": journal["operation_id"],
            "journal_status": journal["status"],
            "blocked": [{"subject": "plan", "reason": exc.error_kind}],
            "runtime_approval_fingerprint": None,
            "recorded_runtime_approval_fingerprint": None if approval is None else cast(dict[str, JsonValue], approval)["fingerprint"],
            "preservation": {"target_byte_writes": 0, "manager_state_writes": 0},
        },
    )


