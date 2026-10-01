"""Step 7 section 6.6 at execution time: the local-state commitment across every re-entry, the per-operation re-baseline, and the section-7.2 host preflight.

``verify_local_state`` is the post-merge and re-entry postcondition that
replaced the ``CLEAN`` conjunct: the staged set must be empty, every preserved
tracked path must carry the digest ``journal.local_state.current`` records,
and the committed inventory must recompute equal -- except for the platform's
own ``knowledge_bases/<name>`` creation (B7), admitted only while the service
may be running, and the declared targets of a runtime operation whose row is
journaled in flight (its pending re-baseline).  ``rebaseline_revision`` is
the carve-out that follows a verified operation: a hard-tier change outside
the declared targets is ``preservation_violated``; a declared change becomes
a journaled revision; a B7 creation the restarted solet made while a
``runtime_reconcile`` operation ran is the solet's, not the operation's, and
is disclosed as a ``service_writes`` revision (iss_d22db98a).
``host_preflight`` refuses before the forward-only boundary when no runtime
stage could run on this host, and ``require_instance_interpreter`` is its
commit-time twin for ``--yes``.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import HostRequirementError, SourceTransitionIncompleteError, UpdateFailedError
from .existing_install_inspection import ExistingInstallFacts
from .host_software import claude_search_directories, host_section
from .local_state import LocalStateDelta, ObservedLocalState, allowed_service_write, compare, observe_entry, observe_local_state, snapshot_from_journal
from .models import JsonValue
from .target_git import run_target_git
from .update_journal import DOCTOR_STATUSES
from .update_runtime_plan import RuntimeSeams
from .update_topology import LocalState

REPAIR_HOST_PYTHON = "Install Python 3.13 outside the Manager keg (`brew install python@3.13`) -- an operator action outside the Manager -- then preview again."
REPAIR_CLAUDE_CLI = (
    "Install the Claude Code CLI yourself -- the update never installs it: `brew install --cask claude-code`. It is looked for on PATH, then in {directories}. "
    "Confirm `command -v claude` prints a path in the shell you run solet-manager from, then preview again."
)
REPAIR_INSTANCE_PYTHON = "Rebuild the instance virtual environment by hand (`python3.13 -m venv --upgrade <target>/.venv`, then reinstall the editable distributions), or restore its interpreter, then re-run --yes."
REPAIR_RECONCILE_DRY_RUN = "Run `solet-manager reconcile {name} --dry-run` to plan a successor operation."
#: Journal statuses under which the platform may be running and its own ``knowledge_bases/`` creation is admitted (B7).
_SERVICE_WRITE_STATUSES = frozenset({"lifecycle_advanced", "runtime_reconciling", "runtime_advanced", *DOCTOR_STATUSES, "promoted"})
#: Row statuses between the first apply write and the journaled re-baseline: ``applying`` (the apply may have run
#: before the crash) and ``applied`` (it did; the postcondition and the re-baseline were not yet recorded).
_IN_FLIGHT_ROW_STATUSES = frozenset({"applying", "applied"})
#: The one applying status whose operations run after the lifecycle restart, so the running solet's own B7 creation can
#: land inside an operation's window; the stage its ``service_writes`` revision names.
_SERVICE_WRITE_STAGE_BY_STATUS = {"runtime_reconciling": "runtime_reconcile"}
#: The clone's local ignore file, relative to the target: the one in-``.git`` path an operation may declare.
CLONE_EXCLUDE_RELATIVE = ".git/info/exclude"
#: The pseudo-path ``compare`` names when the committed commitment differs and no entry can be blamed.
_COMMITMENT_MISMATCH = "commitment_mismatch"
#: Drifted paths a refusal lists; the rest are counted, so the envelope stays bounded (iss_46de4f1d).
_DRIFT_LISTING_LIMIT = 20
REPAIR_READ_ONLY_WINDOW = (
    "Do not reset; the local state was fingerprinted when you approved the source stage, so from `source_advanced` until the runtime `--yes` finishes "
    "the checkout is read-only for you. Put each path back as listed, then resume:"
)


def host_preflight(seams: RuntimeSeams, target: Path) -> dict[str, JsonValue]:
    """Section 7.2: refuse before the forward-only boundary when no runtime stage could run on this host.

    iss_646b54b6: a missing Claude Code CLI is refused here too; the runtime stage's plugin-cache refresh
    would otherwise refuse it only after the source had already advanced.
    """
    host = host_section(seams, target)
    reason = cast(str | None, host["requirement"])
    if reason == "host_requirement_missing":
        raise HostRequirementError(reason, "no Python 3.13 outside the Manager keg is installed; the source cannot advance into a runtime no stage can execute", repair=REPAIR_HOST_PYTHON, host=host)
    if reason == "host_requirement_unknown":
        raise HostRequirementError(reason, "a host requirement probe could not run (see data.host); the preview does not guess", repair="Resolve the host probe failure (see data.host), then preview again.", host=host)
    if reason == "claude_cli_missing":
        searched = ", ".join(str(directory) for directory in claude_search_directories(seams))
        raise HostRequirementError(reason, f"the Claude Code CLI (`claude`) is not on PATH or in {searched}; the runtime stage's plugin-cache refresh needs it, so the source is not advanced", repair=REPAIR_CLAUDE_CLI.format(directories=searched), host=host)
    return host


def require_instance_interpreter(host: dict[str, JsonValue]) -> None:
    """Step 7 (CH-10/11, coordinator ruling 2026-09-19): the COMMIT-time twin of the section-7.2 preflight, for --yes only.

    The runtime plan probes every declared operation up front and the target-adapter
    operations run under the instance venv the dependencies stage would rebuild, so
    a dangling or absent instance interpreter cannot be repaired by the update: the
    source would advance into a runtime whose plan cannot be rendered (every
    target-adapter row ``adapter_missing``), forward-only.  ``--dry-run`` keeps
    CH-9's disclosure-only contract; the fresh apply refuses before any Manager or
    target write.  A two-phase plan (dependencies first, deferred probes) is the
    filed follow-on ``iss_5ba39f5a-8fe2-4f56-a1cc-65088b603688`` that would lift this.
    """
    rows = {str(cast(dict[str, JsonValue], row)["check_id"]): cast(dict[str, JsonValue], row) for row in cast(list[JsonValue], host.get("checks", []))}
    instance = rows.get("instance_python")
    if instance is not None and instance["status"] == "missing":
        raise HostRequirementError("instance_requirement_missing", "the instance interpreter is missing or dangling; the runtime plan could not be rendered after the fast-forward, so the source is not advanced", repair=REPAIR_INSTANCE_PYTHON, host=host)


@dataclass(frozen=True, slots=True)
class LocalStateReport:
    """What one re-entry check observed: the observation, the delta from ``current``, any admitted service writes, and the
    declared targets an in-flight operation already wrote (its pending re-baseline)."""

    observed: ObservedLocalState
    delta: LocalStateDelta
    additions: tuple[dict[str, JsonValue], ...]
    pending_rebaseline: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        state = self.observed.state
        return {
            "preserved_tracked_paths": [{"path": path, "sha256": digest, "size": size} for path, digest, size in state.preserved_tracked_paths],
            "committed": state.committed_rows(),
            "local_state_commitment": state.local_state_commitment,
            "preserved_surface": [{"path": path, "kind": kind, "mode": mode, "size": size} for path, kind, mode, size in state.preserved_surface],
            "preserved_surface_delta": list(self.delta.surface),
            "service_writes": [dict(item) for item in self.additions],
        }


def verify_local_state(target: Path, facts: ExistingInstallFacts, journal: dict[str, JsonValue], last_observed: ObservedLocalState | None) -> LocalStateReport:
    """Section 6.6: staged set empty, tracked digests equal, committed commitment equal (B7 creations admitted across the
    lifecycle stage), preserved-surface differences disclosed; raises ``SourceTransitionIncompleteError`` otherwise."""
    current = _current(journal)
    observed = observe_local_state(target, facts)
    delta = compare(current, observed, last_observed)
    if delta.staged:
        raise SourceTransitionIncompleteError(f"staged changes appeared after the fast-forward: {', '.join(delta.staged)}", repair="Do not reset; unstage by hand (`git restore --staged`), inspect, then resume.")
    # The crash window of section 6.6: an operation journaled in flight may already have written its declared
    # targets without the re-baseline having been recorded; those paths are the operation's pending re-baseline,
    # not a violation, and the executor journals them when the row verifies.
    declared_in_flight = in_flight_declared_targets(journal, target)
    in_flight = declared_in_flight | paths_ignored_by_declared_exclude(target, declared_in_flight, delta.hard, current, last_observed)
    tracked_drift = tuple(path for path in delta.tracked if path not in in_flight)
    allowance = cast(str, journal["status"]) in _SERVICE_WRITE_STATUSES
    additions, committed_drift = _service_write_additions(target, current, observed, tuple(path for path in delta.committed if path not in in_flight), allowance)
    if tracked_drift or committed_drift:
        raise _drift_refusal(target, current, observed, tracked_drift, committed_drift)
    pending = tuple(sorted(set(delta.hard) & in_flight))
    return LocalStateReport(observed, delta, additions, pending)


def _service_write_additions(target: Path, current: LocalState, observed: ObservedLocalState, changed: tuple[str, ...], allowance: bool) -> tuple[tuple[dict[str, JsonValue], ...], tuple[str, ...]]:
    """The admitted B7 creations among ``changed``, and every path that is not one (the drift the caller refuses)."""
    previous_paths = frozenset(row[0] for row in current.committed_inventory)
    additions: list[dict[str, JsonValue]] = []
    refused: list[str] = []
    for path in changed:
        digest = allowed_service_write(target, path, previous_paths) if allowance else None
        if digest is None:
            refused.append(path)
            continue
        entry = next(item for item in observed.committed if item.path == path)
        additions.append({"path": entry.path, "kind": entry.kind, "mode": entry.mode, "size": entry.size, "target_digest": digest})
    return tuple(additions), tuple(refused)


@dataclass(frozen=True, slots=True)
class _Drift:
    """One drifted path: the lead phrase older releases printed, its sub-case, and the repair that fits it."""

    lead: str
    sub_case: str
    repair: str


def _drift_refusal(target: Path, current: LocalState, observed: ObservedLocalState, tracked: tuple[str, ...], committed: tuple[str, ...]) -> SourceTransitionIncompleteError:
    """iss_46de4f1d (public #87): one refusal naming every drifted path with its sub-case and repair; the check itself is unchanged."""
    paths = (*((path, True) for path in tracked), *((path, False) for path in committed))
    shown = tuple(_tracked_drift(target, current, observed, path) if is_tracked else _committed_drift(target, current, path) for path, is_tracked in paths[:_DRIFT_LISTING_LIMIT])
    more = f" (+{len(paths) - _DRIFT_LISTING_LIMIT} more, run `git -C {shlex.quote(str(target))} status`)" if len(paths) > _DRIFT_LISTING_LIMIT else ""
    message = "; ".join(f"{item.lead}: {item.sub_case}" if item.sub_case else item.lead for item in shown) + more
    listing = " ".join(item.repair for item in shown)
    return SourceTransitionIncompleteError(message, repair=f"{REPAIR_READ_ONLY_WINDOW} {listing}{more}")


def _tracked_drift(target: Path, current: LocalState, observed: ObservedLocalState, path: str) -> _Drift:
    expected = next((digest for item, digest, _ in current.preserved_tracked_paths if item == path), None)
    observed_digest = next((entry.digest for entry in observed.tracked if entry.path == path), None)
    lead = f"preserved tracked path {path} no longer matches the recorded local state (expected_sha256={expected}, observed_sha256={observed_digest})"
    if expected is None:
        return _Drift(lead, "tracked at the new HEAD, not a preserved edit", _restore_repair(target, path))
    return _Drift(lead, "a preserved local edit changed again", f"{path}: put back the bytes the commitment recorded (sha256 {expected}); never restore it from HEAD, that discards the edit.")


def _committed_drift(target: Path, current: LocalState, path: str) -> _Drift:
    lead = f"committed local state changed at {path} (inventory_changed)"
    if path == _COMMITMENT_MISMATCH:
        return _Drift(lead, "", "inspect the target and resume once its untracked local state matches the commitment.")
    if not os.path.lexists(target / path):
        return _Drift(lead, "in the approved inventory, now missing", f"{path}: put it back where it was.")
    if run_target_git(("-C", str(target), "cat-file", "-e", f"HEAD:{path}")).returncode == 0:
        return _Drift(lead, "now tracked at the new HEAD", _restore_repair(target, path))
    approved = next((row for row in current.committed_inventory if row[0] == path), None)
    if approved is not None:
        return _Drift(lead, "in the approved inventory, changed since", f"{path}: put it back exactly as approved (kind {approved[1]}, mode {approved[2]}, size {approved[3]}).")
    return _Drift(lead, "untracked local-only file, move it out again", f"{path}: move it out of the checkout again (`mv {shlex.quote(path)} <scratch>`).")


def _restore_repair(target: Path, path: str) -> str:
    return f"{path}: move your copy aside, then `git -C {shlex.quote(str(target))} restore -- {shlex.quote(path)}` takes HEAD's bytes."


def rebaseline_revision(journal: dict[str, JsonValue], observed: ObservedLocalState, last_observed: ObservedLocalState | None, operation_id: str, declared: frozenset[str], name: str, target: Path) -> tuple[tuple[dict[str, JsonValue], ...], dict[str, JsonValue]] | None:
    """Section 6.6, the per-operation carve-out: a hard-tier change outside the operation's declared targets is a
    ``preservation_violated`` failure; declared changes re-baseline ``current`` with a journaled revision; a
    preserved-surface change is disclosed.  Under ``runtime_reconciling`` the solet is running, so an undeclared
    committed-tier path that is exactly a B7 creation (:func:`allowed_service_write`) is the solet's own write and
    is disclosed as a ``service_writes`` revision instead of being blamed on the operation.  Returns
    ``(revisions, current)`` or ``None`` when nothing moved."""
    current = _current(journal)
    delta = compare(current, observed, last_observed)
    undeclared = sorted(set(delta.hard) - declared - paths_ignored_by_declared_exclude(target, declared, delta.hard, current, last_observed))
    service = _service_writes_revision(journal, target, current, observed, tuple(path for path in undeclared if path in delta.committed))
    admitted = _service_write_paths(service)
    _refuse_undeclared(tuple(path for path in undeclared if path not in admitted), operation_id, name)
    operation = _operation_revision(current, observed, last_observed, delta, tuple(path for path in delta.hard if path not in admitted), operation_id)
    revisions = tuple(item for item in (service, operation) if item is not None)
    return (revisions, observed.snapshot()) if revisions else None


def _refuse_undeclared(violations: tuple[str, ...], operation_id: str, name: str) -> None:
    if violations:
        paths = ", ".join(violations)
        raise UpdateFailedError("preservation_violated", f"{operation_id} changed preserved local state outside its declared targets: {paths}", repair=f"Retain all evidence; {operation_id} wrote {paths} without declaring it; {REPAIR_RECONCILE_DRY_RUN.format(name=name)}")


def _operation_revision(current: LocalState, observed: ObservedLocalState, last_observed: ObservedLocalState | None, delta: LocalStateDelta, owned: tuple[str, ...], operation_id: str) -> dict[str, JsonValue] | None:
    """The operation's own revision: its declared hard-tier changes re-baselined, else its preserved-surface change disclosed."""
    if owned:
        before = {path: _entry_digest(current, last_observed, path) for path in owned}
        after = {path: _observed_digest(observed, path) for path in owned}
        return {"operation_id": operation_id, "paths": list(owned), "before": cast(dict[str, JsonValue], before), "after": cast(dict[str, JsonValue], after)}
    if delta.surface:
        return {"operation_id": operation_id, "preserved_surface_delta": list(delta.surface)}
    return None


def _service_writes_revision(journal: dict[str, JsonValue], target: Path, current: LocalState, observed: ObservedLocalState, candidates: tuple[str, ...]) -> dict[str, JsonValue] | None:
    """The ``service_writes`` revision for the B7 creations among ``candidates``, only while the solet runs across an operation."""
    stage = _SERVICE_WRITE_STAGE_BY_STATUS.get(cast(str, journal["status"]))
    if stage is None:
        return None
    additions = _admitted_service_writes(target, current, observed, candidates)
    if not additions:
        return None
    return {"stage": stage, "service_writes": {"committed_additions": list(additions), "preserved_surface_delta": []}}


def _service_write_paths(revision: dict[str, JsonValue] | None) -> frozenset[str]:
    if revision is None:
        return frozenset()
    additions = cast(list[JsonValue], cast(dict[str, JsonValue], revision["service_writes"])["committed_additions"])
    return frozenset(cast(str, cast(dict[str, JsonValue], item)["path"]) for item in additions)


def _admitted_service_writes(target: Path, current: LocalState, observed: ObservedLocalState, candidates: tuple[str, ...]) -> tuple[dict[str, JsonValue], ...]:
    """The committed-tier ``candidates`` that are exactly a B7 creation, as ``service_writes`` addition rows; anything else is left out."""
    previous_paths = frozenset(row[0] for row in current.committed_inventory)
    additions: list[dict[str, JsonValue]] = []
    for path in candidates:
        digest = allowed_service_write(target, path, previous_paths)
        if digest is None:
            continue
        entry = next(item for item in observed.committed if item.path == path)
        additions.append({"path": entry.path, "kind": entry.kind, "mode": entry.mode, "size": entry.size, "target_digest": digest})
    return tuple(additions)


def paths_ignored_by_declared_exclude(target: Path, declared: frozenset[str], paths: Iterable[str], current: LocalState, previous: ObservedLocalState | None) -> frozenset[str]:
    """The hard-tier paths an operation made ignored by declaring the clone's ignore file, and changed in no other way.

    The committed tier inventories untracked files, so an ignore rule moves every file it covers out of that
    inventory.  An operation that declared ``.git/info/exclude`` is allowed that one effect.  A path is admitted
    only when both hold: its entry is exactly the pre-operation one (kind, mode and size from the journaled
    inventory ``current``, and the per-entry digest when the in-process observation ``previous`` has it), and
    ``git check-ignore -v`` names the clone's own ``.git/info/exclude`` as the source that ignores it.  A deleted,
    truncated, rewritten or symlinked path, and one ignored by any other source, stays a violation.  On the resume
    path (``previous`` is ``None``) the journal holds no per-entry digest, so a same-size content change cannot
    be told apart from no change and is admitted.
    """
    if CLONE_EXCLUDE_RELATIVE not in declared:
        return frozenset()
    rows = {row[0]: row for row in current.committed_inventory}
    digests = {} if previous is None else {entry.path: entry.digest for entry in previous.committed}
    return frozenset(path for path in paths if _unchanged_since_journal(target, path, rows, digests) and _ignored_by_clone_exclude(target, path))


def _unchanged_since_journal(target: Path, path: str, rows: dict[str, tuple[str, str, str, int]], digests: dict[str, str]) -> bool:
    """The path still exists and reads exactly as the pre-operation row (and in-process digest, when there is one) recorded it."""
    if path not in rows or not os.path.lexists(target / path):
        return False
    entry = observe_entry(target, path)
    return entry.inventory() == rows[path] and digests.get(path, entry.digest) == entry.digest


def _ignored_by_clone_exclude(target: Path, path: str) -> bool:
    """``git check-ignore -v`` names the clone's own ``.git/info/exclude`` as the source that ignores ``path``."""
    verbose = run_target_git(("-C", str(target), "check-ignore", "-v", "--", path))
    sources = tuple(f"{root / CLONE_EXCLUDE_RELATIVE}:" for root in (target, target.resolve()))
    return verbose.returncode == 0 and verbose.stdout.decode("utf-8", "surrogateescape").partition("\t")[0].startswith(sources)


def in_flight_declared_targets(journal: dict[str, JsonValue], target: Path) -> frozenset[str]:
    """Every in-tree ``planned_targets`` path of a runtime operation whose row is journaled in flight."""
    declared: set[str] = set()
    for raw in cast(list[JsonValue], journal.get("runtime_operations", [])):
        row = cast(dict[str, JsonValue], raw)
        if row["status"] not in _IN_FLIGHT_ROW_STATUSES:
            continue
        for attempt in cast(list[JsonValue], row["attempts"]):
            evidence = cast(dict[str, JsonValue], cast(dict[str, JsonValue], attempt)["evidence"])
            declared.update(_in_tree(target, cast(list[JsonValue], evidence.get("planned_targets", []))))
    return frozenset(declared)


def _in_tree(target: Path, items: list[JsonValue]) -> list[str]:
    inside: list[str] = []
    for item in items:
        path = Path(str(item))
        if not path.is_absolute():
            continue
        try:
            inside.append(str(path.relative_to(target)))
        except ValueError:
            continue
    return inside


def _current(journal: dict[str, JsonValue]) -> LocalState:
    return snapshot_from_journal(cast(dict[str, JsonValue], cast(dict[str, JsonValue], journal["local_state"])["current"]))


def _entry_digest(current: LocalState, previous: ObservedLocalState | None, path: str) -> str | None:
    tracked = next((digest for item, digest, _ in current.preserved_tracked_paths if item == path), None)
    if tracked is not None:
        return tracked
    if previous is not None:
        return next((entry.digest for entry in previous.committed if entry.path == path), None)
    return None


def _observed_digest(observed: ObservedLocalState, path: str) -> str | None:
    return next((entry.digest for entry in (*observed.tracked, *observed.committed) if entry.path == path), None)
