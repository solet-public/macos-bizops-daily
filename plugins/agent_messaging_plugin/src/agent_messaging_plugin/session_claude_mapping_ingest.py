"""T1 usage-capture lane (2026-08-05, workbench
the 2026-08-05 usage-capture ruling) — drains the SessionStart
hook's file-per-firing spool (``.claude/hooks/capture_session_mapping.py``,
``ANANTA_SESSION_MAPPING_SPOOL_DIR``) into ``session_claude_mapping``
(schema.py + session_claude_mapping_store.py).

Crash-safe by construction (ruling Q1(b)): a spool file is deleted ONLY
after its upsert durably completes; :func:`upsert_session_claude_mapping`
conflicts on ``(agent_instance_id, claude_session_id, captured_at)`` — the
exact triple the spool filename encodes — so re-processing a file that
survived a crash before its post-write delete is a no-op re-upsert, never
a duplicate row. A malformed file (bad JSON, missing required field) is
logged and left in place for investigation — never silently deleted,
never fatal to the rest of the drain. A missing/nonexistent spool dir
(``APP_HOME`` unset on this process, or no firing has happened yet) is a
valid steady state, not an error.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_updated
from ananta.services.state_service.bounded_read import iter_table_rows

from .schema import (
    CAPTURE_SOURCE_HOOK_STARTUP,
    CAPTURE_SOURCE_INIT_EVENT,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    SESSION_HOST_HEADLESS,
    SESSION_HOST_OPERATOR,
    SESSION_HOST_TMUX,
    TABLE_MANAGED_SESSION,
)
from .session_claude_mapping_store import (
    list_session_claude_mappings,
    upsert_session_claude_mapping,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_SPOOL_DIR_ENV = "ANANTA_SESSION_MAPPING_SPOOL_DIR"
_REQUIRED_FIELDS = ("agent_instance_id", "claude_session_id", "captured_at", "capture_source")

# S2c (named T1 follow-up, ruling of record): long enough that a normally-
# firing hook has survived at least one full sweep-tick drain cycle before
# absence is declared. The sweep tick's default interval is 300s
# (plugin.py's BridgeConfig.bridge_sweep_interval_seconds) -- one grace
# window comfortably covers at least one drain regardless of exactly when
# in the cycle a session spawned, mirroring session_sweep.py's own
# DEFAULT_PRUNE_GRACE_WINDOW_S rationale: never fire on a row that simply
# hasn't been OBSERVED yet.
DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S: float = 600.0

# A lifecycle sweep is shared with action-path work.  The absence detector is
# deliberately limited to one fixed page, so a growing unresolved population
# cannot turn one tick into unbounded mapping reads and synchronous log I/O.
MAX_HOOK_ABSENCE_CHECKS_PER_TICK = 25

# Operator registrations now create an inventory row too. Its absent dispatch
# contract does not make a missing SessionStart hook unobservable, so every
# live host is eligible for this detector.
_HOOK_ABSENCE_ELIGIBLE_HOSTS = (SESSION_HOST_TMUX, SESSION_HOST_HEADLESS, SESSION_HOST_OPERATOR)

# 'spawning' is too early to judge (the grace window already covers that);
# 'terminated'/'retired' are no longer actionable -- the worker is gone, so
# re-warning about them every tick forever would be pure noise with nothing
# anyone could still do about it. Only the currently-live, still-actionable
# states are eligible.
_HOOK_ABSENCE_ELIGIBLE_STATES = (
    LIFECYCLE_LIVE,
    LIFECYCLE_IDLE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
)


def _resolve_spool_dir() -> Path | None:
    """The env var wins when declared (tests, standalone contexts); otherwise
    fall back to the SAME ``APP_HOME`` derivation the host adapters use when
    exporting the var to spawned workers (``headless_adapter.
    _resolve_session_mapping_spool_dir``). The fallback is load-bearing on the
    platform's own process: the sweep tick and the on-demand verb run inside
    the solet, whose environment carries ``APP_HOME`` but NOT the worker
    env var — 2026-08-05 live acceptance caught the env-var-only read
    no-opping every drain while spool files accumulated silently. ``None``
    only when neither is set."""
    spool_dir = os.environ.get(_SPOOL_DIR_ENV, "").strip()
    if spool_dir:
        return Path(spool_dir)
    app_home = os.environ.get("APP_HOME", "").strip()
    if not app_home:
        return None
    return Path(app_home) / "data" / "session_claude_mapping_spool"


def _load_record(path: Path) -> dict[str, str] | None:
    """``None`` means malformed — the caller logs and leaves the file in
    place (never silently deleted, per the ruling's non-fatal contract)."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "session_claude_mapping spool file %s unreadable/invalid JSON: %s",
            path,
            exc,
        )
        return None
    if not isinstance(raw, dict) or not all(raw.get(field) for field in _REQUIRED_FIELDS):
        logger.warning(
            "session_claude_mapping spool file %s missing required field(s) %s",
            path,
            _REQUIRED_FIELDS,
        )
        return None
    return {field: str(raw[field]) for field in _REQUIRED_FIELDS}


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored ISO-8601 ``created_at`` cell to an aware (UTC) datetime.

    Mirrors ``session_sweep._parse_iso`` exactly (state ``DATETIME`` columns
    read back offset-naive; live clocks are aware UTC -- coerce once at this
    boundary so every comparison here is aware-vs-aware). Kept as this
    module's own private copy rather than a shared import, matching the
    existing per-module convention (``session_sweep.py`` and
    ``http_routes.py`` each already carry their own)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_structurally_hookless(row: dict[str, Any]) -> bool:
    """Whether the registered worker cannot emit a Claude SessionStart hook.

    ``agent_id`` is the peer registry's actual runtime identity.  The
    spawn-time ``agent_runtime`` remains a supporting discriminator for rows
    not yet backfilled by registration; legacy operator rows may carry its
    historical ``claude_code`` default even while ``agent_id`` correctly says
    ``codex``.
    """
    return (
        str(row.get("agent_id") or "") == "codex"
        or str(
            row.get("agent_runtime") or "",
        )
        == "codex"
    )


def _claim_hook_absence_check(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    checked_at: str,
    warned: bool,
) -> bool:
    """Atomically consume one absence candidate; only its winner may warn."""
    updates: dict[str, object] = {"hook_absence_checked_at": checked_at}
    if warned:
        updates["hook_absence_warned_at"] = checked_at
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {
                    "agent_instance_id": agent_instance_id,
                    "is_deleted": 0,
                    "hook_absence_checked_at": {"op": "is_null"},
                },
            },
            updates,
        ),
    )
    return updated == 1


def _warn_if_hook_absent(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    clock: datetime,
    grace_window_s: float,
) -> bool:
    """One ``managed_session`` row's worth of :func:`detect_hook_absent_sessions`.
    Returns whether a WARNING fired (the caller's own count)."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return False
    created_at = _parse_iso(row.get("created_at"))
    if created_at is None or (clock - created_at).total_seconds() < grace_window_s:
        return False
    checked_at = clock.isoformat()
    if _is_structurally_hookless(row):
        _claim_hook_absence_check(
            state,
            agent_instance_id=agent_instance_id,
            checked_at=checked_at,
            warned=False,
        )
        return False
    mappings = list_session_claude_mappings(state, agent_instance_id)
    warning_needed = not any(
        m.get("capture_source") == CAPTURE_SOURCE_HOOK_STARTUP for m in mappings
    )
    if not _claim_hook_absence_check(
        state,
        agent_instance_id=agent_instance_id,
        checked_at=checked_at,
        warned=warning_needed,
    ):
        return False
    if not warning_needed:
        return False
    logger.warning(
        "session_claude_mapping: HOOK ABSENCE for agent_instance_id=%s "
        "(host=%s, lifecycle_state=%s, created_at=%s, grace_window_s=%.0f) -- "
        "no hook:startup capture ever observed past the grace window; the "
        "SessionStart hook may not have fired (check --settings hook "
        "injection, the spool-dir env var, APP_HOME on this host) -- usage "
        "for this worker cannot be attributed via the mapping-table join "
        "until this is fixed",
        agent_instance_id,
        row.get("host"),
        row.get("lifecycle_state"),
        row.get("created_at"),
        grace_window_s,
    )
    return True


def detect_hook_absent_sessions(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    grace_window_s: float = DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S,
) -> int:
    """S2c (named T1 follow-up, ruling of record): :func:`_cross_check_init_event`'s
    both-exist condition silently no-ops when a ``hook:startup`` row never
    shows up at all -- by design, since a hook that simply hasn't fired YET
    is not a mismatch -- but that same silence hides a genuinely BROKEN hook
    installation (settings injection failed, spool-dir env var unset,
    ``APP_HOME`` misconfigured on that host) FOREVER: no row, no warning, no
    signal, ever. This is the distinct positive check the ruling names as
    its own follow-up, not a substitute for the cross-check.

    Scope: all host topologies except registered Codex workers, which cannot
    fire the Claude hook by construction. Only the currently-actionable non-terminal
    ``lifecycle_state`` values (live/idle/overdue/parked) are checked --
    ``spawning`` is too early (the grace window already covers that) and
    terminated/retired rows are no longer actionable.

    A row younger than ``grace_window_s`` is never flagged -- the ordinary
    spawn -> hook-fires -> spool-write -> next-drain-tick latency is not
    absence. Eligible rows are atomically checked once, which provides both
    the warning latch and the page cursor: each tick handles at most
    :data:`MAX_HOOK_ABSENCE_CHECKS_PER_TICK` unchecked rows, and the next tick
    progresses to the remainder. Returns the count of warnings fired this
    call, for the caller's own tick-summary log line (mirrors
    :func:`drain_session_claude_mapping_spool`'s own return-a-count shape).
    """
    clock = now or datetime.now(UTC)
    warned = 0
    checked = 0
    for host in _HOOK_ABSENCE_ELIGIBLE_HOSTS:
        for lifecycle_state in _HOOK_ABSENCE_ELIGIBLE_STATES:
            rows = iter_table_rows(
                state,
                namespace=AGENT_ROLE_BINDING_NAMESPACE,
                table=TABLE_MANAGED_SESSION,
                filters={
                    "host": host,
                    "lifecycle_state": lifecycle_state,
                    "hook_absence_checked_at": {"op": "is_null"},
                },
                ceiling=MAX_HOOK_ABSENCE_CHECKS_PER_TICK,
                reason="hook-absence detection has a fixed per-tick work budget",
            )
            for row in rows:
                checked += 1
                if _warn_if_hook_absent(
                    state,
                    row,
                    clock=clock,
                    grace_window_s=grace_window_s,
                ):
                    warned += 1
                if checked >= MAX_HOOK_ABSENCE_CHECKS_PER_TICK:
                    return warned
    return warned


def _cross_check_init_event(state: StateManagementInterface, agent_instance_id: str) -> None:
    """Ruling addendum (slice D, 2026-08-05) -- pair ``init_event`` rows
    ONLY against ``hook:startup`` rows, per ``agent_instance_id``, both-exist
    condition. ``hook:clear``/``hook:resume`` rows are deliberately OUT OF
    SCOPE: a /clear mints a fresh claude_session_id with no matching init
    event by design, so pairing against them would false-WARN on every
    /clear (the exact false-positive trap the ruling names). A missing side
    -- tmux has no init event; the hook may not have fired yet -- is
    silently NOT a mismatch, never logged. The two observations are
    independent witnesses and are never merged into one row."""
    rows = list_session_claude_mappings(state, agent_instance_id)
    startup_ids = {
        r["claude_session_id"]
        for r in rows
        if r.get("capture_source") == CAPTURE_SOURCE_HOOK_STARTUP
    }
    init_event_ids = {
        r["claude_session_id"] for r in rows if r.get("capture_source") == CAPTURE_SOURCE_INIT_EVENT
    }
    if not startup_ids or not init_event_ids:
        return
    if startup_ids.isdisjoint(init_event_ids):
        logger.warning(
            "session_claude_mapping cross-check MISMATCH for agent_instance_id=%s: "
            "hook:startup claude_session_id(s)=%s vs init_event claude_session_id(s)=%s",
            agent_instance_id,
            sorted(startup_ids),
            sorted(init_event_ids),
        )


def drain_session_claude_mapping_spool(state: StateManagementInterface) -> dict[str, object]:
    """List the spool dir, upsert each well-formed firing, delete each file
    ONLY after its upsert durably completes. Testable/on-demand (the
    ``@platform_process`` verb calls this directly) AND wired into the
    platform sweep tick (``plugin.py``'s ``_run_session_lifecycle_sweep``) —
    same call site, same fault-isolation posture as ``sweep_overdue_sessions``.
    """
    spool_dir = _resolve_spool_dir()
    if spool_dir is None or not spool_dir.is_dir():
        return {"files_seen": 0, "upserted": 0, "skipped_malformed": 0}

    files_seen = 0
    upserted = 0
    skipped_malformed = 0
    touched_instance_ids: set[str] = set()
    for path in sorted(spool_dir.glob("*.json")):
        files_seen += 1
        record = _load_record(path)
        if record is None:
            skipped_malformed += 1
            continue
        upsert_session_claude_mapping(
            state,
            agent_instance_id=record["agent_instance_id"],
            claude_session_id=record["claude_session_id"],
            captured_at=record["captured_at"],
            capture_source=record["capture_source"],
        )
        upserted += 1
        touched_instance_ids.add(record["agent_instance_id"])
        try:
            path.unlink()
        except OSError as exc:
            # The upsert already landed durably -- a delete failure just
            # means this file gets safely re-ingested (idempotent upsert)
            # next drain, never a lost or duplicated row.
            logger.warning(
                "session_claude_mapping spool file %s upserted but could not be deleted: %s",
                path,
                exc,
            )

    # Slice D (ruling addendum): the cross-check only needs to re-evaluate
    # instances that got a fresh row THIS call -- an already-settled pair
    # from a prior drain has nothing new to compare.
    for agent_instance_id in touched_instance_ids:
        _cross_check_init_event(state, agent_instance_id)

    return {"files_seen": files_seen, "upserted": upserted, "skipped_malformed": skipped_malformed}


__all__ = [
    "DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S",
    "MAX_HOOK_ABSENCE_CHECKS_PER_TICK",
    "detect_hook_absent_sessions",
    "drain_session_claude_mapping_spool",
]
