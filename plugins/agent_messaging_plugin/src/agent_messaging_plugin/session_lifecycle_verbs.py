"""Fleet session-management Phase B, D1 (§4) — the L1 verb bodies:
``spawn_session``, ``list_sessions``, ``session_status``, ``clear_session``,
``compact_session``, ``drive_session``, ``terminate_session``,
``retire_session``, ``report_alive``. Pure functions over
:mod:`session_lifecycle_store` + :mod:`session_hosts` (+ role-binding lookups
for the fill-never-mint validation) — the ``@platform_process`` wiring in
``plugin.py`` is a thin transport shim, mirroring the ``role_claim.py`` /
``plugin.py`` split.

``clear_session``/``compact_session`` (AMEND 5b) and ``drive_session`` (D2
window, rotation-boundary rider ruling 2026-08-04) ride the host driver's
driver channel — fire-and-forget (send the text, do not await the resulting
turn). ``clear_session(park=True)`` additionally drives
``live/idle/overdue -> parked`` (§3.2 matrix, L3 rule 2, steward direction) —
the ONLY writer of that edge; ``compact_session`` never parks;
``drive_session`` is the §3.2 ``parked -> live`` writer ("new dispatch through
the driver channel"). All three share ``_resolve_driver_channel`` for the
``unsupported_on_host`` refusal (hosts with no driver channel, e.g.
``host='operator'``) — a config/mechanism gap, never a silent degradation.

The platform sweep that marks ``overdue`` and fires/delivers ``deadline``
``session_dependency`` edges lives in ``session_sweep.py`` (an ``on_tick``
rider, not a verb) — this module provides the primitives it calls
(``transition_lifecycle_state``, the managed_session reads).
``terminate_session`` owns firing + best-effort delivering
``session_terminal`` edges (guarded, once — 2026-08-04, drive-on-delivery
lane fix slice); ``retire_session`` composes ``terminate_session`` and no
longer fires them itself — the sweep does not duplicate either.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_ROLE_CLASS,
    ROLE_CLASS_EPHEMERAL,
    ROLE_CLASS_PRIMARY,
    ROLE_CLASS_PRINCIPAL,
    ROLE_CLASS_PROJECT,
    TABLE_ROLE,
    is_reserved_primary_name,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import (
    require_records,
)

from . import workbench_brief_snapshot
from .driver_texts import render_driver_text
from .lane_worktrees import (
    DirtyStaleWorktreeSkippedWarning,
    LaneWorktree,
    LaneWorktreeError,
    LaneWorktreeRepoRootError,
    lane_worktree_disposability,
    lane_worktree_for,
    provision_lane_worktree,
    remove_lane_worktree,
    resolve_lane_repo_root,
    sweep_orphaned_lane_worktrees,
)
from .lane_worktrees import (
    active_lane_worktree_inventory as _active_lane_worktree_inventory,
)
from .managed_dispatch import (
    DISPATCH_PREPARING,
    NEXT_ENSURE_UNIT,
    DispatchError,
    read_managed_dispatch,
    record_first_turn_evidence,
)
from .role_binding_store import RoleClassConflictError, legislate_role_class
from .schema import (
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    SESSION_HOST_HEADLESS,
    SESSION_HOST_TMUX,
    SESSION_VISIBILITY_HEADLESS,
    SESSION_VISIBILITY_VISIBLE,
    TABLE_MANAGED_SESSION,
    WORK_CLASS_ANALYSIS_DELIVERABLE,
    WORK_CLASS_PRODUCTION_MUTATION,
    WORK_CLASS_READ_ONLY,
)
from .session_hosts import (
    DEFAULT_AGENT_RUNTIME,
    AgentRuntimeNotSupportedError,
    HostCannotSpawnError,
    HostMechanismMissingError,
    HostNotDeclaredError,
    resolve_host_driver,
)
from .session_lifecycle_store import (  # noqa: F401
    DEFAULT_REPORT_BY_SECONDS as DEFAULT_REPORT_BY_SECONDS,
)
from .session_lifecycle_store import (
    LaneCharterRecord,
    LaneCharterSpec,
    ManagedSessionSpec,
    SessionNotFoundError,
    insert_managed_session,
    persist_first_turn_evidence,
    read_managed_session,
    resolve_lane_charter,
    set_host_ref,
    transition_lifecycle_state,
)
from .session_lifecycle_store import capture_lane_charter as _store_capture_lane_charter
from .session_list import (
    LIST_SESSIONS_DEFAULT_LIMIT,
    SessionListError,
    list_session_rows,
)
from .session_list import LIST_SESSIONS_MAX_LIMIT as LIST_SESSIONS_MAX_LIMIT  # noqa: F401
from .session_role_claim_store import (
    delete_session_role_claim_if_still_holds,
    read_session_role_claim,
)
from .spawn_capability_floors import contract_list, recorded_floors, validate_spawn_policy
from .spawn_selection_receipts import (
    SelectionReceiptError,
    receipt_contract_mismatches,
    replay_selection_receipt,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .session_hosts import HostDriver

logger = logging.getLogger(__name__)
VerbError = workbench_brief_snapshot.VerbError

_VALID_SPAWN_ROLE_CLASSES = frozenset(
    {ROLE_CLASS_EPHEMERAL, ROLE_CLASS_PROJECT, ROLE_CLASS_PRINCIPAL},
)
_VALID_WORK_CLASSES = frozenset(
    {WORK_CLASS_READ_ONLY, WORK_CLASS_ANALYSIS_DELIVERABLE, WORK_CLASS_PRODUCTION_MUTATION},
)
_TERMINAL_STATES = frozenset({LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED})

# fleet-watch-transport-migration phase 2 slice 6 (2026-08-06) — Finding 0's
# fix: spawn_session used to boot a worker with NO first turn at all (see
# drive_session's own docstring, unchanged, for the pre-existing MCP-arm
# story); on the CLAUDE watch path, a watch-armed worker's Stop-hook wake
# loop only arms once its FIRST turn completes, so a pristine watch subject
# with no charter and no seat dispatch yet was unreachable at zero token
# cost. (A managed CODEX worker is not Stop-hook governed at all — stock
# Codex does not execute async command hooks, so its watch/tmux sidecar has
# no Stop-hook wake loop to arm; it is reached through the durable inbox /
# watch read path and, once managed, drive_on_delivery's driver-channel
# nudge — codex-0147-dead-spool-retirement, 2026-08-13.) spawn_session now
# always drives exactly one first turn immediately after a successful host
# dispatch, for EVERY runtime: the lane's captured charter if one is on
# file, else this fixed fallback. Ordering-ruling guard (a): the fallback
# must be small and DESIGNED TO COMPLETE in one turn — never a question,
# never a wait.
FIRST_TURN_SOURCE_CHARTER = "charter"
FIRST_TURN_SOURCE_FALLBACK = "fallback"

# SPN-01 (measured 2026-08-19, wave-2 dispatch): the fallback turn above USED
# to end in a reply and nothing else — "acknowledge, then stop; your work
# dispatch arrives separately over the peer channel." That is a HANDOFF TO
# NOBODY: it names a dispatch that exists only if a seat remembers to send one,
# and the turn ends with the lane holding nothing and having told no one it is
# waiting. Measured consequence: two of four freshly-spawned lanes sat idle ~30
# minutes on one bootstrap turn each, and the only difference from the two that
# started was a post-spawn driving message; confirmed by intervention 2/2.
#
# The turn now hands off to the SPAWNER, which is the only side of that gap this
# module controls. It stays inside the ordering-ruling guard the original text
# was written to satisfy — small, completing in ONE turn, never a question,
# never a wait: four bounded acts and a stop, with nothing to poll or block on.
#
# ★ WHAT THIS DOES NOT DO, stated here so a reader does not over-credit it: it
# NARROWS the window, it does not close it. If the spawner never reads its
# inbox the lane still waits (SPN-01's own fourth datum is a seat that missed
# fifteen role messages across eight hours), so "send a driving message after
# spawn" stays a REQUIRED runbook step. What changes is that the lane is no
# longer SILENTLY idle: it is addressable (claimed), its heartbeat contract is
# armed (so the overdue sweep can see it), and its spawner has been told.
#
# The claim-first ordering is LIF-05's, for LIF-05's reason: claiming is safe
# whatever the lane decides about the work, and a lane that has not claimed
# cannot be reached BY NAME to be given any.
_FALLBACK_BRIEF_CLAUSE = " at {brief_ref}"
_FALLBACK_NO_BRIEF_CLAUSE = (
    " — your row records no brief_ref, so ask your spawner for one rather than guessing at the work"
)
_FALLBACK_NO_SPAWNER = "whoever spawned you (your row records no spawning role)"

def build_fallback_first_turn(
    *,
    spawned_by_role: str,
    role_class: str,
    role_name: str,
    brief_ref: str,
    brief_snapshot: workbench_brief_snapshot.BriefSnapshot | None = None,
) -> str:
    """Render the JSON-defined fallback first turn for one spawn (SPN-01).

    Every substituted value comes off the row this spawn just wrote — the
    same recorded INTENT the charter frame uses — so the turn never asks the
    worker to invent a role name, a brief or a spawner to report to. Each
    absent field degrades to "ask", never to a guess: a lane that claims a
    made-up binding reads as addressable while routing nowhere, which is
    strictly worse than a lane that says it has no name.
    """
    fallback = render_driver_text(
        "spawn.fallback_first_turn",
        role_instruction=_role_instruction(role_class=role_class, role_name=role_name),
        brief_clause=(
            _FALLBACK_BRIEF_CLAUSE.format(brief_ref=brief_ref)
            if brief_ref
            else _FALLBACK_NO_BRIEF_CLAUSE
        ),
        spawned_by_role=spawned_by_role or _FALLBACK_NO_SPAWNER,
    )
    return fallback if brief_snapshot is None else fallback + workbench_brief_snapshot.render(brief_snapshot)


# Charter-rider provenance framing (phase-3 incident finding, 2026-08-06,
# coordinator-seat ruling approved verbatim): a charter-founded subject read the
# verbatim charter body as a LIVE human typing to it in real time, dismissed
# its actual assigned brief as fake, and began self-authorizing out-of-scope
# work under a fabricated "the operator asked me directly" belief — measured
# live, transcript-confirmed. Root mechanism, also measured: the subject ran
# peer_list, found a DIFFERENT pre-existing session already registered under
# the role name it expected, and concluded ITS OWN identity block must be
# fabricated (it had simply not registered yet). This frame wraps the
# verbatim charter body — never edits it, keeping slice 6's byte-exact
# turn-1 fidelity contract on charter_text intact — and closes both
# mechanisms: explicit "not a live conversation" framing, and the
# not-yet-registered clause that pre-empts the peer_list-mismatch inference
# directly, since that specific inference is what the incident transcript
# showed actually happened.
# LIF-05 rider (measured 2026-08-19, `lane-drive-honesty`): the frame above
# closed the believe-it-too-much failure and left the believe-it-too-little one
# open. A charter-founded lane read this same frame, judged that it could not
# verify its dispatcher existed or that the operator sentence quoted in its
# charter was real, DECLINED the work and went idle at the prompt. ★ THE
# REFUSAL WAS CORRECT AND IS NOT WHAT THIS FIXES: a relayed quote genuinely is
# unverifiable from the lane's seat, which is this fleet's own standing rule
# about a relayed assent hardening into a fabricated ruling, applied by a
# worker to its dispatcher. Suppressing that scepticism would be the wrong
# repair, and the seat's own recovery did the opposite — it handed the lane
# INSTRUMENTS instead of insistence, conceded the quote, and said standing
# down was legitimate. The frame now ships those instruments with the charter
# rather than making a seat rediscover them mid-incident.
#
# The SECOND-ORDER effect is the trap worth closing in code: claiming the role
# binding was step 1 of the charter the lane refused, so it never claimed, and
# `peer_send_by_name` answered `role_binding_vacant` — the fleet could not
# reach BY NAME the one session that most needed a follow-up. Hence the claim
# instruction is separated from the work and stated FIRST: it is safe
# independent of what the lane decides, and it cannot evict anyone (spawning
# still never claims on the worker's behalf, operator ruling 2026-08-14, and
# `peer_claim_role` refuses a live incumbent with `role_held_live` unless a
# caller passes an explicit takeover) — so nothing here reopens that ruling.
_ROLE_CLAUSE_WITH_NAME = (
    " (your row's role_name is {role_name!r}; peer_claim_role refuses a live "
    "incumbent, so claiming it cannot evict anyone)"
)
_ROLE_CLAUSE_NO_NAME = (
    " under the name your dispatcher assigns you — your row records no "
    "role_name, so ask rather than inventing one"
)
_EPHEMERAL_ROLE_INSTRUCTION = (
    "Your row is ephemeral: no role exists or will be assigned. Do not request, "
    "invent, or claim one."
)


def _role_instruction(*, role_class: str, role_name: str) -> str:
    """Render role guidance without inventing a role for ephemeral workers."""
    if role_class == ROLE_CLASS_EPHEMERAL:
        return _EPHEMERAL_ROLE_INSTRUCTION
    role_clause = (
        _ROLE_CLAUSE_WITH_NAME.format(role_name=role_name) if role_name else _ROLE_CLAUSE_NO_NAME
    )
    return (
        "CLAIM YOUR ROLE BINDING FIRST"
        f"{role_clause} — before you decide anything else, because it is safe "
        "whatever you decide. Spawning deliberately does not claim it for you, "
        "so until you claim it nobody can reach you BY NAME: not to answer your "
        "questions, and not to hear that you are standing down. Declining this "
        "work is a legitimate outcome; declining it while unaddressable is how "
        "a lane disappears."
    )


def _frame_charter_provenance(
    charter: LaneCharterRecord,
    *,
    agent_instance_id: str,
    spawned_by_role: str,
    role_class: str,
    role_name: str = "",
) -> str:
    """Wrap a resolved charter's verbatim body in the provenance frame —
    split out so :func:`_dispatch_first_turn` stays a plain dispatch,
    and so the frame's field substitution has exactly one call site.

    NEVER edits ``charter_text``: slice 6's byte-exact turn-1 fidelity
    contract is what makes the operator's captured words trustworthy at all,
    so the frame only ever prefixes.

    ``role_class`` decides whether the worker may hold a durable role at all.
    Ephemeral rows receive no-role guidance; other classes retain claim-first
    guidance derived from their recorded ``role_name``.
    """
    return render_driver_text(
        "spawn.charter_provenance_frame",
        captured_at=charter.captured_at,
        agent_instance_id=agent_instance_id,
        spawned_by_role=spawned_by_role or "(no spawning role recorded)",
        brief_ref=charter.brief_ref or "(no brief_ref recorded)",
        role_instruction=_role_instruction(role_class=role_class, role_name=role_name),
        charter_text=charter.charter_text,
    )


def _role_row(state: StateManagementInterface, name: str) -> dict[str, Any] | None:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_ROLE, "filters": {"external_id": role_binding_external_id(name)}},
    )
    records = require_records(result)
    return records[0] if records else None


def _validate_spawn_role(
    state: StateManagementInterface,
    *,
    role_class: str,
    role_name: str,
) -> None:
    """Fill-never-mint (§2) + the reserved-mint guard (§3.1), evaluated at
    spawn time so a doomed spawn fails BEFORE dispatch, not after."""
    if not role_name:
        return
    if is_reserved_primary_name(role_name) and _role_row(state, role_name) is None:
        raise VerbError(
            "reserved_role_name",
            f"role {role_name!r} matches the reserved primary-seat pattern and "
            "has no legislated role row yet; primary-seat legislation is a "
            "governance act (this plugin's legislate_role verb), not a spawn.",
        )
    existing = _role_row(state, role_name)
    if role_class == ROLE_CLASS_PRINCIPAL and existing is None:
        raise VerbError(
            "role_not_legislated",
            f"role_name {role_name!r} resolves to no role row; principal offices "
            "are fill-never-mint — legislate the office first via this plugin's "
            "legislate_role verb (a governance act, outside this verb).",
        )
    if existing is not None and existing.get(COL_ROLE_CLASS) not in (None, role_class):
        raise VerbError(
            "role_class_conflict",
            f"role_name {role_name!r} already exists with role_class="
            f"{existing.get(COL_ROLE_CLASS)!r}, not the requested {role_class!r}.",
        )


def resolve_provisioned_role_class(
    state: StateManagementInterface,
    *,
    role_name: str,
    requested_role_class: str,
) -> tuple[str, bool]:
    """Resolve a provisioned role without making callers know its taxonomy.

    An existing role row is authoritative.  A fresh role defaults to
    ``project``; ``principal`` remains an explicit request because creating
    that office is a logged governance act.  The boolean says whether the
    caller must legislate that requested principal office before spawning it.
    """
    name = role_name.strip()
    requested = requested_role_class.strip()
    if not name:
        raise VerbError("missing_argument", "provision_role_session requires non-empty role_name.")
    existing = _role_row(state, name)
    if existing is not None:
        existing_class = str(existing.get(COL_ROLE_CLASS) or "").strip()
        if not existing_class:
            raise VerbError(
                "role_class_missing",
                f"role_name {name!r} has a role row with no role_class; repair the row before provisioning.",
            )
        if existing_class not in {ROLE_CLASS_PROJECT, ROLE_CLASS_PRINCIPAL}:
            raise VerbError(
                "role_class_not_spawn_assignable",
                f"role_name {name!r} resolves to {existing_class!r}, which cannot be spawned.",
            )
        return existing_class, False
    if requested in ("", ROLE_CLASS_PROJECT):
        return ROLE_CLASS_PROJECT, False
    if requested == ROLE_CLASS_PRINCIPAL:
        return ROLE_CLASS_PRINCIPAL, True
    raise VerbError(
        "unknown_role_class",
        f"requested_role_class must be empty, 'project', or 'principal'; got {requested!r}.",
    )


def resolve_local_name(*, role_class: str, role_name: str, lane_id: str) -> str:
    """W6 (#13 §44.3): the name a spawned worker will answer to locally.

    ``role_name`` for a PROJECT-class role, ``lane_id`` otherwise. The
    project class is the one whose whole point is that the worker IS the
    role — a Git-Controller has to be named ``Git-Controller`` on its own
    machine or it cannot pass the mutation guard, which resolves the caller
    by reading the local session file's ``name`` and comparing it exactly.
    Every other class is lane work, and lane_id is what those sessions were
    already labelled with, so this is a no-op for them.
    """
    if role_name and role_class == ROLE_CLASS_PROJECT:
        return role_name
    return lane_id


def _refuse_if_local_name_held(
    state: StateManagementInterface,
    *,
    local_name: str,
    role_name: str,
) -> None:
    """W6 OPERATOR RULING (2026-08-14): a second spawn for a role that is
    already held is REFUSED, LOUDLY. Never a silent uniquifying suffix, never
    a silent eviction.

    A suffix is not available even in principle: the Git-Controller mutation
    guard compares the local session name EXACTLY
    (``.claude/hooks/git_controller_gate.py``: ``session_name == controller``),
    so ``Git-Controller-2`` is simply a session that cannot mutate git. And a
    silent eviction would make a routine act destructive. So the only
    non-colliding answer is to refuse and name the incumbent.

    The incumbent is a non-terminal ``managed_session`` row sharing this
    ``local_name`` — the ledger, not the role binding, because the collision
    being prevented is two LOCAL PROCESSES answering to one name on one
    machine. That choice also keeps crash succession cheap for free: a
    crashed holder's row is swept to ``terminated`` by the platform sweep,
    after which a replacement spawns without an operator in the loop. This
    mirrors the claim path's own posture, where a DEAD holder is deliberately
    still claimable (``role_claim.py``) — the refusal is about live
    collisions, not about corpses.
    """
    if not local_name:
        return
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {"local_name": local_name, "is_deleted": 0},
        },
    )
    for row in require_records(result):
        if str(row.get("lifecycle_state") or "") in _TERMINAL_STATES:
            continue
        raise VerbError(
            "local_name_already_held",
            f"refusing to spawn a second session named {local_name!r}"
            f"{f' (role {role_name!r})' if role_name else ''}: "
            f"{row.get('agent_instance_id')} is already live under that name "
            f"(lifecycle_state={row.get('lifecycle_state')!r}, "
            f"host={row.get('host')!r}, host_ref={row.get('host_ref')!r}, "
            f"lane_id={row.get('lane_id')!r}). Two sessions answering to one "
            "local name collide silently — the git mutation guard matches the "
            "name exactly, so both would pass it. Terminate the incumbent "
            "first (this plugin's terminate_session verb, agent_instance_id="
            f"{row.get('agent_instance_id')}), then spawn the replacement. "
            "Spawning does NOT claim the durable role binding either way; the "
            "new worker claims it explicitly once it is up.",
        )


def _resolve_and_guard_local_name(
    state: StateManagementInterface,
    req: SpawnSessionRequest,
) -> str:
    """W6: resolve the worker's local name, then refuse if it is already held.
    Split out of :func:`spawn_session` to keep it under the radon cc threshold
    (the same precedent :func:`_dispatch_first_turn` established)."""
    local_name = req.local_name or resolve_local_name(
        role_class=req.role_class,
        role_name=req.role_name,
        lane_id=req.lane_id,
    )
    _refuse_if_local_name_held(state, local_name=local_name, role_name=req.role_name)
    return local_name


_LEGISLATABLE_ROLE_CLASSES = frozenset({ROLE_CLASS_PRIMARY, ROLE_CLASS_PRINCIPAL})


@dataclass(frozen=True, slots=True)
class LegislateRoleRequest:
    name: str
    role_class: str
    brief_ref: str
    directed_by: str = ""


def legislate_role(
    state: StateManagementInterface,
    req: LegislateRoleRequest,
) -> dict[str, Any]:
    """D4 Part B item 1 — the ONE sanctioned governance-act path that stamps
    an authority-carrying ``role_class`` (``primary``/``principal``) onto a
    ``role`` row at birth (§3.1 Q1: claim-time is enforce-by-class, never
    class-assignment — this is the assignment half, deliberately outside D1).

    ``project``/``ephemeral``/``chat`` are minted (by a claim or a spawn),
    never legislated — requesting one of those here is refused, not silently
    downgraded to a mint. A ``primary``-class target MUST match the reserved
    ``<solet>-Main`` pattern the mint-refusal guard protects (§3.1) —
    legislating a non-matching name as ``primary`` would create a seat the
    guard was never watching, defeating the point of the reservation.
    """
    name = req.name.strip()
    brief_ref = req.brief_ref.strip()
    if not name or not brief_ref:
        raise VerbError(
            "missing_argument",
            "legislate_role requires non-empty 'name' and 'brief_ref'.",
        )
    if req.role_class not in _LEGISLATABLE_ROLE_CLASSES:
        raise VerbError(
            "role_class_not_legislatable",
            f"role_class {req.role_class!r} is not legislatable "
            f"({sorted(_LEGISLATABLE_ROLE_CLASSES)}); project/ephemeral/chat "
            "roles are minted (by a claim or a spawn), never legislated.",
        )
    if req.role_class == ROLE_CLASS_PRIMARY and not is_reserved_primary_name(name):
        raise VerbError(
            "reserved_primary_name_required",
            f"role_class='primary' requires a reserved-pattern name "
            f"(<solet>-Main shape); {name!r} does not match.",
        )
    try:
        action = legislate_role_class(
            state,
            name=name,
            role_class=req.role_class,
            directed_by=req.directed_by,
            brief_ref=brief_ref,
        )
    except RoleClassConflictError as exc:
        raise VerbError("role_class_conflict", str(exc)) from exc
    return {"action": action, "name": name, "role_class": req.role_class}


@dataclass(frozen=True, slots=True)
class SpawnSessionRequest:
    role_class: str
    lane_id: str
    brief_ref: str
    work_class: str
    budget_line: str
    unit_id: str = ""
    # The dispatcher's checked-out repository root for this lane.  The project
    # register persists repository identity, not a machine-local checkout path,
    # so the spawn boundary must carry the latter explicitly when it differs
    # from the serving Solet's own checkout.
    repository_root: str = ""
    # Server-issued durable work contract. Project-class managed work is a
    # hard cutover: raw spawn without a valid preparing dispatch is refused.
    dispatch_id: str = ""
    # Exact peer-registry agent_id vocabulary.  Kept orthogonal to ``host``:
    # claude_code/codex select the worker runtime; headless/tmux/operator
    # select the hosting topology.
    agent_runtime: str = DEFAULT_AGENT_RUNTIME
    role_name: str = ""
    host: str | None = None
    visibility: str = ""
    model: str = ""
    effort: str = ""
    report_by_seconds: int = 0
    spawned_by_instance_id: str = ""
    spawned_by_role: str = ""
    directed_by: str = ""
    # §6 permission-mode ruling (2026-08-03): the headless driver's
    # spawn-time PreToolUse allowlist gate. Resolved from plugin.yaml's
    # work_class_tool_allowlists at the platform_process shim (mirrors
    # work_class_defaults' model/effort resolution) -- this verb stays a
    # pure function over the request, no config access of its own.
    allowed_tools: tuple[str, ...] = ()
    # §6 permission-mode ruling, refined 2026-08-03 (Dawn ruling): the
    # headless driver's --permission-mode value, resolved from plugin.yaml's
    # headless_permission_mode (declared, not process-env -- a config value
    # is as declared as an env var, and it's the platform-native surface).
    # Superseded same day (operator ruling, "we don't have any restrictions
    # now", merge c6e24f319): "bypassPermissions" is NOT rejected anywhere
    # in this path -- it is the platform.yaml default (headless_adapter.py's
    # own docstring/README), the exact value an unattended spawn needs since
    # there is no human to approve a "default"-mode prompt.
    permission_mode: str = ""
    # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06): the
    # spawned worker's declared FLEET_TRANSPORT ("mcp" | "watch"), resolved
    # from plugin.yaml's default_fleet_transport at the platform_process
    # shim -- same "declared config, not a code default" posture as
    # permission_mode. Empty here means "let the resolver fill it from
    # policy," never "spawn with no transport declared."
    transport: str = ""
    # AskUserQuestion default-deny (operator ruling 2026-08-14): per-spawn
    # escape hatch, named after the seed launcher's own
    # SOLET_ALLOW_ASKUSERQUESTION=1 override for cross-surface consistency.
    # Unlike permission_mode/transport this has NO plugin.yaml resolution
    # step -- the ruling fixes the global default (deny) outright, so a bare
    # dataclass default is the whole story; only the tmux driver reads it
    # (the headless driver never enumerates the tool in the first place --
    # see headless_adapter.py's own docstring for the measured evidence --
    # and the codex runtime has no equivalent tool at all), but it is
    # threaded through the shared dispatch spec dict unconditionally, same
    # as every other claude_code-only field already is.
    allow_askuserquestion: bool = False
    # W6 (#13 §44.3): the name the spawned worker answers to on its own
    # machine. Empty means "resolve it" — spawn_session defaults it to
    # role_name for a project-class role and lane_id otherwise.
    local_name: str = ""
    # W4A item 3 (#8 §43.1): proceed even though the host's preflight can
    # prove the worker's hooks will not run. Default off — running degraded
    # is a stated choice, recorded on the ledger row and logged loudly.
    degraded_hooks_acknowledged: bool = False
    # Internal only: the fleet qualifier's real minimal worker has no checkout.
    synthetic_qualification_no_worktree: bool = False
    # Model-assignment policy is deliberately a required spawn fact. Empty is
    # the transport representation of an omitted argument and fails before a
    # row or host side effect is created.
    dispatch_kind: str = ""
    reviewed_report_vendor: str = ""
    pair_id: str = ""
    # Caller-declared work scope, e.g. ("state_schema",), retained as
    # provenance. See spawn_capability_floors for any configured future floor.
    scope_tags: tuple[str, ...] = ()
    # Every spawn must name its difficulty and carry the exact selector receipt
    # that chose this runtime/model/effort. Empty values are deliberate
    # fail-closed transport defaults for omitted public arguments.
    difficulty_score: float = 0.0
    selection_receipt: Mapping[str, Any] | None = None
    # Public process adapters set this before calling the internal lifecycle
    # primitive. Direct in-process fixtures deliberately exercise lifecycle
    # topology without a capability catalog and therefore leave it false.
    enforce_selection_receipt: bool = False


def _validate_prepared_dispatch(
    state: StateManagementInterface,
    req: SpawnSessionRequest,
) -> dict[str, Any] | None:
    """Require project work to arrive through a matching prepared contract."""
    if not req.dispatch_id:
        return _require_dispatch_id_for_project(req)
    try:
        row = read_managed_dispatch(state, req.dispatch_id)
    except DispatchError as exc:
        raise VerbError(exc.code, exc.message) from exc
    if str(row.get("state") or "") != DISPATCH_PREPARING:
        raise VerbError(
            "dispatch_not_preparing",
            f"dispatch {req.dispatch_id!r} is not in preparing state.",
        )
    if row.get("current_agent_instance_id"):
        raise VerbError("dispatch_attempt_exists", "Dispatch already has a current attempt.")
    # The register Unit is ensured between prepare and spawn (design
    # unt_57725090 s4.2); no caller may route a spawn around that step.
    if row.get("next_required_action") == NEXT_ENSURE_UNIT or not str(row.get("unit_id") or ""):
        raise VerbError(
            "unit_not_ensured",
            f"dispatch {req.dispatch_id!r} has no ensured register Unit.",
        )
    mismatched = _dispatch_contract_mismatches(row, req)
    if mismatched:
        raise VerbError(
            "dispatch_contract_mismatch",
            f"spawn_session differs from prepared dispatch fields: {mismatched}.",
        )
    return row


def _require_dispatch_id_for_project(req: SpawnSessionRequest) -> None:
    if req.role_class == ROLE_CLASS_PROJECT:
        raise VerbError(
            "managed_dispatch_required",
            "Project-class managed work must use dispatch_managed_work; raw "
            "spawn_session requires a server-issued preparing dispatch_id.",
        )
    return None


def _dispatch_contract_mismatches(row: Mapping[str, Any], req: SpawnSessionRequest) -> list[str]:
    text_expected = {
        "lane_id": req.lane_id,
        "role_name": req.role_name,
        "role_class": req.role_class,
        "work_class": req.work_class,
        "budget_line": req.budget_line,
        "brief_ref": req.brief_ref,
        "unit_id": req.unit_id,
        "repository_root": req.repository_root,
        "agent_runtime": req.agent_runtime,
        "host": str(req.host or ""),
        "visibility": req.visibility,
        "model": req.model,
        "effort": req.effort,
        "spawned_by_instance_id": req.spawned_by_instance_id,
        "spawned_by_role": req.spawned_by_role,
        "directed_by": req.directed_by,
        "permission_mode": req.permission_mode,
        "transport": req.transport,
        "local_name": req.local_name,
    }
    mismatches = {
        field for field, value in text_expected.items() if str(row.get(field) or "") != value
    }
    value_expected: dict[str, object] = {
        "report_by_seconds": req.report_by_seconds,
        "allow_askuserquestion": req.allow_askuserquestion,
        "degraded_hooks_acknowledged": req.degraded_hooks_acknowledged,
    }
    mismatches.update(field for field, value in value_expected.items() if row.get(field) != value)
    list_expected = {"allowed_tools": req.allowed_tools, "scope_tags": req.scope_tags}
    mismatches.update(field for field, value in list_expected.items() if contract_list(row, field) != value)
    mismatches.update(
        receipt_contract_mismatches(
            row,
            enforce=req.enforce_selection_receipt,
            difficulty_score=req.difficulty_score,
            selection_receipt=req.selection_receipt,
        ),
    )
    return sorted(mismatches)


def _require_dispatch_host_allowed(
    dispatch_row: Mapping[str, Any] | None, resolved_host: str
) -> None:
    if dispatch_row is None:
        return
    allowed_hosts = dispatch_row.get("allowed_hosts")
    if not isinstance(allowed_hosts, list) or resolved_host not in allowed_hosts:
        raise VerbError(
            "dispatch_host_not_allowed",
            f"Host {resolved_host!r} is outside the prepared dispatch host policy.",
        )


_VISIBLE_HOSTS: Final[frozenset[str]] = frozenset({SESSION_HOST_TMUX})
_HEADLESS_HOSTS: Final[frozenset[str]] = frozenset({SESSION_HOST_HEADLESS})


def _require_visibility_matches_host(visibility: str, resolved_host: str) -> None:
    """Refuse a descriptive visibility label that disagrees with the host.

    ``visible`` is the persisted visibility vocabulary and means a tmux-hosted
    worker. ``tmux`` is accepted as the explicit spelling used by callers of
    the spawn API. An omitted visibility has no asserted topology to check.
    """
    if resolved_host not in _VISIBLE_HOSTS | _HEADLESS_HOSTS:
        return
    expected_hosts = {
        SESSION_VISIBILITY_VISIBLE: _VISIBLE_HOSTS,
        "tmux": _VISIBLE_HOSTS,
        SESSION_VISIBILITY_HEADLESS: _HEADLESS_HOSTS,
    }.get(visibility)
    if expected_hosts is not None and resolved_host in expected_hosts:
        return
    if not visibility:
        return
    expected = " or ".join(sorted(expected_hosts)) if expected_hosts else "a declared visibility"
    raise VerbError(
        "visibility_host_mismatch",
        f"visibility {visibility!r} requires {expected}; resolved host is {resolved_host!r}.",
    )


def _record_dispatch_first_turn(
    state: StateManagementInterface,
    req: SpawnSessionRequest,
    *,
    agent_instance_id: str,
    source: str,
    delivered: bool,
    error: str,
    resolved_host: str,
    host_ref: str,
    observed_at: datetime,
) -> None:
    if not req.dispatch_id:
        return
    record_first_turn_evidence(
        state,
        dispatch_id=req.dispatch_id,
        agent_instance_id=agent_instance_id,
        source=source,
        delivered=delivered,
        error=error,
        host=resolved_host,
        host_ref=host_ref,
        agent_runtime=req.agent_runtime,
        observed_at=observed_at,
    )


def _resolve_lane_repo_root(repository_root: str = "") -> Path:
    """Map worktree-root validation errors onto the lifecycle verb contract."""
    app_home = os.environ.get("APP_HOME", "").strip()
    if not repository_root.strip() and not app_home:
        raise VerbError(
            "lane_worktree_app_home_required",
            "lane worktree provisioning requires an explicit APP_HOME-derived checkout",
        )
    try:
        return resolve_lane_repo_root(repository_root, app_home)
    except LaneWorktreeRepoRootError as exc:
        raise VerbError(
            exc.code,
            str(exc),
        ) from exc


def _provision_spawn_worktree(
    state: StateManagementInterface,
    *,
    role_name: str,
    agent_instance_id: str,
    repository_root: str = "",
) -> LaneWorktree:
    """Sweep only registered stale lane trees, then create this lane's exact tree."""
    repo_root = _resolve_lane_repo_root(repository_root)
    try:
        inventory = _active_lane_worktree_inventory(state, repo_root)
        if inventory.cleanup_safe:
            sweep = sweep_orphaned_lane_worktrees(
                repo_root,
                terminal_paths=inventory.terminal_paths,
            )
            for warning in sweep.skipped:
                _log_dirty_stale_worktree_skipped(warning)
        else:
            logger.warning(
                "lane_worktree_cleanup_skipped: incomplete_active_inventory=%s",
                "; ".join(inventory.incomplete_rows),
            )
        worktree = lane_worktree_for(
            repo_root,
            role_name=role_name,
            agent_instance_id=agent_instance_id,
        )
        provision_lane_worktree(worktree)
    except LaneWorktreeError as exc:
        raise VerbError("lane_worktree_provision_failed", str(exc)) from exc
    return worktree


def _log_dirty_stale_worktree_skipped(
    warning: DirtyStaleWorktreeSkippedWarning,
) -> None:
    """Keep a retained stale tree visible without converting it into a refusal."""
    logger.warning(
        "%s: path=%s git_diagnostic=%s",
        warning.code,
        warning.path,
        warning.git_diagnostic,
    )


def _provision_worktree_for_request(
    state: StateManagementInterface,
    req: SpawnSessionRequest,
    local_name: str,
    agent_instance_id: str,
) -> LaneWorktree | None:
    """Choose the recorded role first, then the local lane name, for a spawn."""
    if req.synthetic_qualification_no_worktree:
        if not (
            req.role_class == ROLE_CLASS_EPHEMERAL
            and req.host == "qualification"
            and req.lane_id == "qualify-fleet"
            and req.brief_ref == "internal:qualify_fleet"
        ):
            raise VerbError(
                "synthetic_no_worktree_not_allowed",
                "only qualify_fleet's internal qualification request may bypass "
                "worktree provisioning",
            )
        return None
    return _provision_spawn_worktree(
        state,
        role_name=req.role_name or local_name,
        agent_instance_id=agent_instance_id,
        repository_root=req.repository_root,
    )


def _provisioned_lane_repo_root(worktree: LaneWorktree | None) -> str:
    return "" if worktree is None else str(worktree.repo_root)


_WORKTREE_DISPOSITION_REMOVED = "removed"
_WORKTREE_DISPOSITION_RETAINED_NOT_DISPOSABLE = "retained_not_disposable"
_WORKTREE_DISPOSITION_RETAINED_ERROR = "retained_error"
_WORKTREE_DISPOSITION_NOT_PROVISIONED = "not_provisioned"


def _retiring_lane_worktree(row: Mapping[str, object]) -> LaneWorktree | None:
    """Resolve the one retiring worktree, or no target for a synthetic session."""
    if str(row.get("provisioning_mode") or "worktree") in {
        "synthetic_no_worktree", "operator_existing_checkout",
    }:
        return None
    role_name = str(row.get("role_name") or row.get("local_name") or "")
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not role_name or not agent_instance_id:
        return None
    recorded_root = str(row.get("lane_repo_root") or "")
    repo_root = _resolve_lane_repo_root(recorded_root)
    return lane_worktree_for(
        repo_root,
        role_name=role_name,
        agent_instance_id=agent_instance_id,
    )


def _adjudicate_retire_lane_worktree(  # pyright: ignore[reportUnusedFunction]
    row: Mapping[str, object],
) -> None:
    """Refuse before host termination when the worktree still holds state."""
    worktree = _retiring_lane_worktree(row)
    if worktree is None:
        return
    try:
        verdict = lane_worktree_disposability(worktree)
    except LaneWorktreeError as exc:
        raise VerbError("lane_worktree_adjudication_failed", str(exc)) from exc
    if not verdict.disposable:
        raise VerbError(
            "lane_worktree_not_disposable",
            f"retire_session refused before terminating the host: {verdict.reason}",
        )


def _retire_lane_worktree(  # pyright: ignore[reportUnusedFunction]
    row: Mapping[str, object],
) -> str:
    """Attempt exact teardown, recording rather than propagating its outcome."""
    try:
        worktree = _retiring_lane_worktree(row)
    except (LaneWorktreeError, VerbError) as exc:
        logger.warning("lane worktree retained after retirement: %s", exc)
        return _WORKTREE_DISPOSITION_RETAINED_ERROR
    if worktree is None:
        return _WORKTREE_DISPOSITION_NOT_PROVISIONED
    try:
        remove_lane_worktree(worktree)
    except LaneWorktreeError as exc:
        try:
            verdict = lane_worktree_disposability(worktree)
        except LaneWorktreeError:
            logger.warning("lane worktree teardown failed after retirement: %s", exc)
            return _WORKTREE_DISPOSITION_RETAINED_ERROR
        if not verdict.disposable:
            logger.warning("lane worktree retained after retirement: %s", verdict.reason)
            return _WORKTREE_DISPOSITION_RETAINED_NOT_DISPOSABLE
        logger.warning("lane worktree teardown failed after retirement: %s", exc)
        return _WORKTREE_DISPOSITION_RETAINED_ERROR
    return _WORKTREE_DISPOSITION_REMOVED


def _release_retiring_session_role_claim(  # pyright: ignore[reportUnusedFunction]
    state: StateManagementInterface,
    row: Mapping[str, object],
) -> None:
    """Best-effort cardinality cleanup after the host has reached terminal state."""
    session_id = str(row.get("agent_session_id") or "")
    if not session_id:
        return
    claim_row = read_session_role_claim(state, session_id)
    if claim_row is None:
        return
    delete_session_role_claim_if_still_holds(
        state,
        agent_session_id=session_id,
        expected_held_role=str(claim_row.get("held_role") or ""),
    )


def _insert_spawn_session_with_worktree(
    state: StateManagementInterface,
    spec: ManagedSessionSpec,
    worktree: LaneWorktree | None,
) -> dict[str, Any]:
    """Persist the pending session or remove the just-created exact worktree."""
    try:
        return insert_managed_session(state, spec)
    except Exception:
        if worktree is not None:
            remove_lane_worktree(worktree)
        raise


def _spawn_host_in_lane_worktree(
    driver: HostDriver,
    spawn_spec: dict[str, object],
    worktree: LaneWorktree | None,
) -> str:
    """Dispatch a host or tear down the exact pre-dispatch worktree on refusal."""
    try:
        return driver.spawn(spawn_spec)
    except HostCannotSpawnError as exc:
        if worktree is not None:
            try:
                remove_lane_worktree(worktree)
            except LaneWorktreeError as cleanup_exc:
                raise VerbError(
                    "worktree_teardown_failed",
                    f"host spawn failed ({exc.remedy}); lane worktree cleanup failed: {cleanup_exc}",
                ) from cleanup_exc
        raise


def _resolve_spawn_host(
    req: SpawnSessionRequest,
    dispatch_row: Mapping[str, object] | None,
) -> tuple[HostDriver, str]:
    """Resolve and policy-check a requested host before allocating a worker identity."""
    try:
        driver, resolved_host = resolve_host_driver(req.host, req.agent_runtime)
    except AgentRuntimeNotSupportedError as exc:
        raise VerbError("agent_runtime_unsupported", str(exc)) from exc
    except HostNotDeclaredError as exc:
        raise VerbError("host_not_declared", str(exc)) from exc
    except HostMechanismMissingError as exc:
        raise VerbError("host_mechanism_missing", exc.remedy) from exc
    _require_dispatch_host_allowed(dispatch_row, resolved_host)
    _require_visibility_matches_host(req.visibility, resolved_host)
    return driver, resolved_host


def spawn_session(
    state: StateManagementInterface,
    req: SpawnSessionRequest,
) -> dict[str, Any]:
    """§4 ``spawn_session``: validate -> write the ledger row (spawning,
    BEFORE dispatch) -> dispatch through the resolved host driver. A
    dispatch failure transitions the row straight to ``terminated`` (we know
    synchronously no process exists) and propagates the ORIGINAL error
    token — never leaves a permanently-stuck ``spawning`` row for a spawn we
    already know failed.

    ``agent_instance_id`` is passed through the dispatch spec so a real
    driver (``headless``) can inject it into the spawned process's own
    environment — that is what lets ``backfill_registration`` find the right
    ledger row when the process later registers with the platform.
    """
    if req.role_class not in _VALID_SPAWN_ROLE_CLASSES:
        raise VerbError(
            "unknown_role_class",
            f"role_class {req.role_class!r} is not spawn-assignable "
            f"({sorted(_VALID_SPAWN_ROLE_CLASSES)}).",
        )
    if req.work_class not in _VALID_WORK_CLASSES:
        raise VerbError(
            "unknown_work_class",
            f"work_class {req.work_class!r} is not one of {sorted(_VALID_WORK_CLASSES)}.",
        )
    if not req.budget_line:
        raise VerbError("budget_line_required", "spawn_session requires a non-empty budget_line.")
    _validate_spawn_role(state, role_class=req.role_class, role_name=req.role_name)
    dispatch_row = _validate_prepared_dispatch(state, req)
    # W6: resolved BEFORE the host lookup so a refused second spawn costs
    # nothing and, like the role validation above, fails BEFORE dispatch.
    local_name = _resolve_and_guard_local_name(state, req)
    charter = resolve_lane_charter(state, req.lane_id)
    brief_snapshot = workbench_brief_snapshot._workbench_brief_snapshot(
        req.brief_ref, req.repository_root, dispatch_row, has_charter=charter is not None,
    )
    # Model-dispatch policy receives the brief text for any configured future
    # floor. The retired state_schema floor does not gate this spawn. Everything
    # above this line is a read or a guard; every side effect is below it.
    applied_floors = validate_spawn_policy(req, brief_snapshot)
    driver, resolved_host = _resolve_spawn_host(req, dispatch_row)
    try:
        verified_receipt = replay_selection_receipt(
            state,
            synthetic_qualification=req.synthetic_qualification_no_worktree,
            enforce=req.enforce_selection_receipt,
            difficulty_score=req.difficulty_score,
            selection_receipt=req.selection_receipt,
            dispatch_kind=req.dispatch_kind,
            scope_tags=req.scope_tags,
            agent_runtime=req.agent_runtime,
            model=req.model,
            effort=req.effort,
        )
    except SelectionReceiptError as exc:
        raise VerbError(exc.code, exc.message) from exc

    agent_instance_id = f"agi-{secrets.token_hex(16)}"
    lane_worktree = _provision_worktree_for_request(state, req, local_name, agent_instance_id)
    spec = ManagedSessionSpec(
        agent_instance_id=agent_instance_id,
        lane_id=req.lane_id,
        brief_ref=req.brief_ref,
        unit_id=req.unit_id,
        work_class=req.work_class,
        budget_line=req.budget_line,
        agent_runtime=req.agent_runtime,
        host=resolved_host,
        dispatch_id=req.dispatch_id,
        spawned_by_instance_id=req.spawned_by_instance_id,
        spawned_by_role=req.spawned_by_role,
        visibility=req.visibility,
        model=req.model,
        effort=req.effort,
        report_by_seconds=req.report_by_seconds,
        directed_by=req.directed_by,
        dispatch_kind=req.dispatch_kind,
        reviewed_report_vendor=req.reviewed_report_vendor,
        pair_id=req.pair_id,
        scope_tags=req.scope_tags,
        capability_floors=recorded_floors(applied_floors),
        difficulty_score=req.difficulty_score,
        selection_receipt=verified_receipt,
        # W6: the spawn's stated INTENT. Recording role_name does not claim
        # the binding — spawning never claims a role as a side effect
        # (operator ruling 2026-08-14); the worker claims it explicitly.
        role_name=req.role_name,
        local_name=local_name,
        degraded_hooks_acknowledged=req.degraded_hooks_acknowledged,
        provisioning_mode=(
            "synthetic_no_worktree" if req.synthetic_qualification_no_worktree else "worktree"
        ),
        lane_repo_root=_provisioned_lane_repo_root(lane_worktree),
    )
    row = _insert_spawn_session_with_worktree(state, spec, lane_worktree)

    host_spec: dict[str, object] = {
        "agent_instance_id": agent_instance_id,
        "lane_id": req.lane_id,
        "brief_ref": req.brief_ref,
        "unit_id": req.unit_id,
        "model": req.model,
        "effort": req.effort,
        "allowed_tools": req.allowed_tools,
        "permission_mode": req.permission_mode,
        "transport": req.transport,
        "allow_askuserquestion": req.allow_askuserquestion,
        "agent_runtime": req.agent_runtime,
        "role_class": req.role_class,
        "spawned_by_role": req.spawned_by_role,
        "local_name": local_name,
        "degraded_hooks_acknowledged": req.degraded_hooks_acknowledged,
        "worktree_path": str(lane_worktree.path) if lane_worktree is not None else "",
    }
    try:
        host_ref = _spawn_host_in_lane_worktree(driver, host_spec, lane_worktree)
    except HostCannotSpawnError as exc:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED,
            directed_by=req.directed_by,
            reason=f"host_cannot_spawn: {exc.remedy}",
        )
        raise VerbError("host_cannot_spawn", exc.remedy) from exc

    if host_ref:
        set_host_ref(state, agent_instance_id=agent_instance_id, host_ref=host_ref)

    first_turn_source, first_turn_delivered, first_turn_error = _dispatch_first_turn(
        state,
        agent_instance_id=agent_instance_id,
        lane_id=req.lane_id,
        spawned_by_role=req.spawned_by_role,
        role_class=req.role_class,
        role_name=req.role_name,
        brief_ref=req.brief_ref,
        charter=charter,
        brief_snapshot=brief_snapshot,
        agent_runtime=req.agent_runtime,
        resolved_host=resolved_host,
        host_ref=host_ref,
    )
    first_turn_at = datetime.now(UTC)
    persist_first_turn_evidence(
        state,
        agent_instance_id=agent_instance_id,
        dispatch_id=req.dispatch_id,
        source=first_turn_source,
        delivered=first_turn_delivered,
        error=first_turn_error,
        observed_at=first_turn_at,
    )
    _record_dispatch_first_turn(
        state,
        req,
        agent_instance_id=agent_instance_id,
        source=first_turn_source,
        delivered=first_turn_delivered,
        error=first_turn_error,
        resolved_host=resolved_host,
        host_ref=host_ref,
        observed_at=first_turn_at,
    )

    return {
        "agent_instance_id": agent_instance_id,
        "agent_runtime": req.agent_runtime,
        "host": resolved_host,
        "host_ref": host_ref,
        "dispatch_id": req.dispatch_id,
        "lifecycle_state": str(row.get("lifecycle_state") or ""),
        "first_turn_source": first_turn_source,
        "first_turn_delivered": first_turn_delivered,
        "first_turn_error": first_turn_error,
    }


def _dispatch_first_turn(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    lane_id: str,
    spawned_by_role: str,
    role_class: str,
    role_name: str,
    brief_ref: str,
    charter: LaneCharterRecord | None,
    brief_snapshot: workbench_brief_snapshot.BriefSnapshot | None,
    agent_runtime: str,
    resolved_host: str,
    host_ref: str,
) -> tuple[str, bool, str]:
    """Split out of :func:`spawn_session` to keep it under the radon-cc
    threshold (the same ``_resolve_transport`` precedent slice 1+5
    established for the adapters' ``spawn()`` methods) — drives exactly one
    first turn immediately after a successful host dispatch: the lane's
    captured charter (provenance-framed, see :func:`_frame_charter_provenance`)
    if one is on file, else the rendered
    JSON driver text rendered by :func:`build_fallback_first_turn`. Returns
    ``(first_turn_source, first_turn_delivered, first_turn_error)``; NEVER
    raises — a delivery fault here must never block the spawn itself
    (ordering-ruling guard (b)), only be logged + surfaced to the caller."""
    if charter is not None:
        first_turn_text = _frame_charter_provenance(
            charter,
            agent_instance_id=agent_instance_id,
            spawned_by_role=spawned_by_role,
            role_class=role_class,
            role_name=role_name,
        )
        first_turn_source = FIRST_TURN_SOURCE_CHARTER
    else:
        first_turn_text = build_fallback_first_turn(
            spawned_by_role=spawned_by_role,
            role_class=role_class,
            role_name=role_name,
            brief_ref=brief_ref,
            brief_snapshot=brief_snapshot,
        )
        first_turn_source = FIRST_TURN_SOURCE_FALLBACK
    first_turn_delivered = False
    first_turn_error = ""
    try:
        channel = _resolve_driver_channel(
            {
                "host": resolved_host,
                "agent_runtime": agent_runtime,
                "agent_instance_id": agent_instance_id,
                "host_ref": host_ref,
            },
        )
        channel.send(first_turn_text)
        first_turn_delivered = True
    except VerbError as exc:
        first_turn_error = f"{exc.code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001 — visibility, never a spawn-blocking fault
        first_turn_error = str(exc)
    if not first_turn_delivered:
        # Ordering-ruling guard (b): a failed first-turn delivery must be
        # VISIBLE (logged + surfaced in the spawn result), never silent —
        # its silent failure mints exactly the unreachable-at-zero-cost
        # worker Finding 0 warns about. The spawn itself is NOT blocked.
        logger.warning(
            "spawn_session %s: first-turn delivery (%s) failed: %s",
            agent_instance_id,
            first_turn_source,
            first_turn_error,
        )
    return first_turn_source, first_turn_delivered, first_turn_error


@dataclass(frozen=True, slots=True)
class CaptureLaneCharterRequest:
    lane_id: str
    charter_text: str
    captured_at: str
    brief_ref: str = ""
    directed_by: str = ""


def capture_lane_charter(
    state: StateManagementInterface,
    req: CaptureLaneCharterRequest,
) -> dict[str, Any]:
    """§4 ``capture_lane_charter`` (phase 2 slice 6, design check-in ruling
    item 3(a)) — the seat-invoked governance act that writes a
    ``lane_charter`` row. Validates at the verb surface (stable
    ``VerbError`` tokens, same convention as every other L1 verb here) and
    delegates the actual insert-only write to
    :func:`session_lifecycle_store.capture_lane_charter`. ``directed_by`` is
    server-built from ``call_context`` at the ``@platform_process`` shim,
    never caller-supplied — the same provenance convention ``spawn_session``
    and ``legislate_role`` already use.

    A capture is ALWAYS an insert, never an update: calling this again for
    the same ``lane_id`` supersedes by recency (``spawn_session`` resolves
    the latest row), it does not edit the prior charter's text in place.
    """
    if not req.lane_id.strip():
        raise VerbError(
            "missing_lane_id",
            "capture_lane_charter requires a non-empty lane_id.",
        )
    if not req.charter_text.strip():
        raise VerbError(
            "missing_charter_text",
            "capture_lane_charter requires non-empty charter_text.",
        )
    if not req.captured_at.strip():
        raise VerbError(
            "missing_captured_at",
            "capture_lane_charter requires a non-empty captured_at.",
        )
    spec = LaneCharterSpec(
        lane_id=req.lane_id,
        charter_text=req.charter_text,
        captured_at=req.captured_at,
        brief_ref=req.brief_ref,
        directed_by=req.directed_by,
    )
    return _store_capture_lane_charter(state, spec)


def list_sessions(
    state: StateManagementInterface,
    filters: dict[str, Any] | None = None,
    *,
    live_only: bool = False,
    limit: object = LIST_SESSIONS_DEFAULT_LIMIT,
    after_created_at: object = None,
    after_id: object = None,
) -> dict[str, Any]:
    """§4 filter-required, cursor-paginated fleet roster.

    ``limit`` defaults to 50 because this is an operator context payload, not
    an export; its maximum of 250 admits broad filtered coordination queries
    without reinstating a whole-ledger dump.  A full page carries a tie-safe
    ``next_cursor`` instead of refusing an append-mostly operator backlog.
    """
    try:
        result = list_session_rows(
            state,
            filters,
            live_only=live_only,
            limit=limit,
            after_created_at=after_created_at,
            after_id=after_id,
        )
    except SessionListError as exc:
        raise VerbError(exc.code, exc.message) from exc
    result["sessions"] = [
        {
            **{key: value for key, value in row.items() if key != "expires_at"},
            "coordination_state": _coordination_state_projection(row),
        }
        for row in result["sessions"]
    ]
    return result


def _coordination_state_projection(row: Mapping[str, Any]) -> str:
    if row.get("dispatch_id"):
        return "managed"
    if str(row.get("lifecycle_state") or "") in _TERMINAL_STATES:
        return "legacy_unmanaged"
    return "legacy_unsupervised"


def session_status(state: StateManagementInterface, agent_instance_id: str) -> dict[str, Any]:
    """§4 aggregate ledger state plus honest tri-state native liveness."""
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    liveness, detail = _probe_host_liveness(row)
    result = {key: value for key, value in row.items() if key != "expires_at"}
    result.update(
        {
            "ledger_state": str(row.get("lifecycle_state") or ""),
            "host_liveness": liveness,
            "host_liveness_detail": detail,
            "host_liveness_observed_at": datetime.now(UTC).isoformat(),
            "state_consistent": not (
                liveness == "dead" and str(row.get("lifecycle_state") or "") not in _TERMINAL_STATES
            ),
            "coordination_state": _coordination_state_projection(row),
        },
    )
    return result


from .session_lifecycle_control import (  # noqa: E402, F401
    _REPORT_ALIVE_EDGE,  # pyright: ignore[reportUnusedImport]
    CLEAR_VERIFICATION_CONFIRMED,
    CLEAR_VERIFICATION_UNSUPPORTED,
    DRIVE_DRIVER_ERROR,
    DRIVE_DRIVER_SENT,
    DRIVE_DRIVER_UNAVAILABLE,
    DRIVE_INELIGIBLE_STATE,
    DRIVE_NOT_MANAGED,
    REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT,  # pyright: ignore[reportUnusedImport]
    REPORT_BY_SOURCE_OBSERVED_SPAWNING,  # pyright: ignore[reportUnusedImport]
    ArmSessionDependencyRequest,  # pyright: ignore[reportUnusedImport]
    DriveOnDeliveryOutcome,
    _drive_parked_delivery,  # pyright: ignore[reportUnusedImport]
    _probe_host_liveness,
    _rearm_report_by,  # pyright: ignore[reportUnusedImport]
    _refuse_report_alive_on_ineligible_state,  # pyright: ignore[reportUnusedImport]
    _resolve_driver_channel,
    _resolve_termination_driver,  # pyright: ignore[reportUnusedImport]
    _row_report_by_seconds,  # pyright: ignore[reportUnusedImport]
    _send_driver_text,  # pyright: ignore[reportUnusedImport]
    _terminate_host,  # pyright: ignore[reportUnusedImport]
    _write_explicit_status_source,  # pyright: ignore[reportUnusedImport]
    arm_session_dependency,  # pyright: ignore[reportUnusedImport]
    clear_session,
    compact_session,
    dispatch_event_age_seconds,  # pyright: ignore[reportUnusedImport]
    drive_on_delivery,
    drive_session,
    notify_steward_of_managed_dispatch,  # pyright: ignore[reportUnusedImport]
    report_alive,
    retire_session,
    terminate_session,
)

__all__ = [
    "CLEAR_VERIFICATION_CONFIRMED",
    "CLEAR_VERIFICATION_UNSUPPORTED",
    "CaptureLaneCharterRequest",
    "DRIVE_DRIVER_ERROR",
    "DRIVE_DRIVER_SENT",
    "DRIVE_DRIVER_UNAVAILABLE",
    "DRIVE_INELIGIBLE_STATE",
    "DRIVE_NOT_MANAGED",
    "DriveOnDeliveryOutcome",
    "FIRST_TURN_SOURCE_CHARTER",
    "FIRST_TURN_SOURCE_FALLBACK",
    "LegislateRoleRequest",
    "LIST_SESSIONS_DEFAULT_LIMIT",
    "LIST_SESSIONS_MAX_LIMIT",
    "SpawnSessionRequest",
    "VerbError",
    "build_fallback_first_turn",
    "capture_lane_charter",
    "clear_session",
    "compact_session",
    "drive_on_delivery",
    "drive_session",
    "legislate_role",
    "list_sessions",
    "report_alive",
    "retire_session",
    "session_status",
    "spawn_session",
    "terminate_session",
]
