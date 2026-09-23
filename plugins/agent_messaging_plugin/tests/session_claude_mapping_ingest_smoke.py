#!/usr/bin/env python3
"""Unit smoke for ``session_claude_mapping_ingest.py`` (T1 usage-capture
lane, the 2026-08-05 usage-capture ruling) — the
verb that drains the SessionStart hook's file-per-firing spool into
``session_claude_mapping``.

Proves: a fresh spool file is ingested and deleted; re-running on an empty
dir is a clean no-op; a malformed file is skipped and LEFT IN PLACE, never
silently deleted; a missing spool dir returns zero-processed rather than
erroring; a file that survives a crash before its post-write delete is
safely re-ingested next drain WITHOUT duplicating the row (idempotent
upsert on the filename's own conflict triple); and — the "wired consumer"
proof this plugin's own convention requires (mirrors
host_adapter_liveness_smoke.py's ``test_a_real_consumer_is_wired_to_the_interface``)
— the REAL ``AgentMessagingPlugin._run_session_lifecycle_sweep`` genuinely
reaches this drain, not just that the function exists unwired.

Also covers ``detect_hook_absent_sessions`` (S2c, named T1 follow-up): a
``managed_session`` row past the grace window with NO ``hook:startup``
mapping row fires a WARNING (the genuinely-broken-hook case the cross-check
alone cannot see); a row still inside the grace window, one that already
has its hook:startup row, and rows in terminal/too-early lifecycle states
are all NOT flagged; an old operator row is included; and the same wired-consumer
proof for the sweep tick.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_claude_mapping_ingest_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402

from agent_messaging_plugin import session_claude_mapping_ingest  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    SESSION_HOST_HEADLESS,
    SESSION_HOST_OPERATOR,
    SESSION_HOST_TMUX,
    WORK_CLASS_READ_ONLY,
)
from agent_messaging_plugin.session_claude_mapping_ingest import (  # noqa: E402
    DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S,
    detect_hook_absent_sessions,
    drain_session_claude_mapping_spool,
)
from agent_messaging_plugin.session_claude_mapping_store import (  # noqa: E402
    list_session_claude_mappings,
    upsert_session_claude_mapping,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    backfill_registration,
    insert_managed_session,
    transition_lifecycle_state,
)

_T0 = datetime(2026, 8, 5, 18, 0, 0, tzinfo=UTC)

_passed = 0
_failed: list[str] = []
_SPOOL_ENV = "ANANTA_SESSION_MAPPING_SPOOL_DIR"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _write_spool_file(
    spool_dir: Path,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    captured_at: str = "2026-08-05T16:00:00+00:00",
    capture_source: str = "hook:startup",
    content: str | None = None,
) -> Path:
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / f"{captured_at}__{agent_instance_id}__{claude_session_id}.json"
    if content is not None:
        path.write_text(content)
        return path
    path.write_text(
        json.dumps(
            {
                "agent_instance_id": agent_instance_id,
                "claude_session_id": claude_session_id,
                "captured_at": captured_at,
                "capture_source": capture_source,
            }
        )
    )
    return path


def test_missing_spool_dir_returns_zero_processed() -> None:
    # APP_HOME must ALSO be unset here: with the platform-side fallback in
    # place, an inherited dev-shell APP_HOME would silently point this leg
    # at the real profile spool (negative control must env -u BOTH vars).
    orig = os.environ.pop(_SPOOL_ENV, None)
    orig_app_home = os.environ.pop("APP_HOME", None)
    try:
        state = RealShapeState()
        result = drain_session_claude_mapping_spool(state)
        _check(
            result == {"files_seen": 0, "upserted": 0, "skipped_malformed": 0},
            "neither ANANTA_SESSION_MAPPING_SPOOL_DIR nor APP_HOME set -> zero-processed, never an error",
        )
    finally:
        if orig is not None:
            os.environ[_SPOOL_ENV] = orig
        if orig_app_home is not None:
            os.environ["APP_HOME"] = orig_app_home


def test_app_home_fallback_drains_platform_side() -> None:
    """The live-caught 2026-08-05 acceptance defect, rendered as a test: the
    platform process exports no ANANTA_SESSION_MAPPING_SPOOL_DIR (only the
    adapters export it, to WORKERS) -- the drain must fall back to the SAME
    APP_HOME derivation the adapters use, or every spool file sits undrained
    forever with no warning. Failing mutation: _resolve_spool_dir returning
    None when only APP_HOME is set."""
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "data" / "session_claude_mapping_spool"
        path = _write_spool_file(
            spool_dir,
            agent_instance_id="agi-apphome-1",
            claude_session_id="cs-apphome",
        )
        orig = os.environ.pop(_SPOOL_ENV, None)
        orig_app_home = os.environ.get("APP_HOME")
        os.environ["APP_HOME"] = tmp
        try:
            state = RealShapeState()
            result = drain_session_claude_mapping_spool(state)
        finally:
            if orig is not None:
                os.environ[_SPOOL_ENV] = orig
            if orig_app_home is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = orig_app_home
        _check(
            result == {"files_seen": 1, "upserted": 1, "skipped_malformed": 0},
            "env var unset + APP_HOME set -> drain resolves APP_HOME/data/session_claude_mapping_spool and ingests",
        )
        _check(
            not path.exists(), "APP_HOME-fallback ingest deletes the spool file after durable write"
        )


def test_explicit_spool_env_wins_over_app_home() -> None:
    """Declared beats derived when both are present -- the env var (tests,
    standalone contexts) must win over the APP_HOME fallback, and the
    APP_HOME-derived dir must stay untouched."""
    with tempfile.TemporaryDirectory() as tmp:
        env_spool = Path(tmp) / "declared"
        app_home_spool = Path(tmp) / "home" / "data" / "session_claude_mapping_spool"
        env_path = _write_spool_file(
            env_spool,
            agent_instance_id="agi-declared-1",
            claude_session_id="cs-declared",
        )
        home_path = _write_spool_file(
            app_home_spool,
            agent_instance_id="agi-derived-1",
            claude_session_id="cs-derived",
        )
        orig = os.environ.get(_SPOOL_ENV)
        orig_app_home = os.environ.get("APP_HOME")
        os.environ[_SPOOL_ENV] = str(env_spool)
        os.environ["APP_HOME"] = str(Path(tmp) / "home")
        try:
            state = RealShapeState()
            result = drain_session_claude_mapping_spool(state)
        finally:
            if orig is None:
                os.environ.pop(_SPOOL_ENV, None)
            else:
                os.environ[_SPOOL_ENV] = orig
            if orig_app_home is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = orig_app_home
        _check(
            result["files_seen"] == 1 and result["upserted"] == 1,
            "explicit env var wins: exactly the declared dir's file is ingested",
        )
        _check(not env_path.exists(), "declared-dir file ingested and deleted")
        _check(home_path.exists(), "APP_HOME-derived dir untouched when the env var is set")


def test_nonexistent_spool_dir_returns_zero_processed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig = os.environ.get(_SPOOL_ENV)
        os.environ[_SPOOL_ENV] = str(Path(tmp) / "never-created")
        try:
            state = RealShapeState()
            result = drain_session_claude_mapping_spool(state)
            _check(
                result == {"files_seen": 0, "upserted": 0, "skipped_malformed": 0},
                "a spool dir that was declared but never created -> zero-processed, no error",
            )
        finally:
            if orig is None:
                os.environ.pop(_SPOOL_ENV, None)
            else:
                os.environ[_SPOOL_ENV] = orig


def test_fresh_file_ingested_and_deleted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        path = _write_spool_file(
            spool_dir,
            agent_instance_id="agi-ingest-1",
            claude_session_id="cs-fresh",
        )
        os.environ[_SPOOL_ENV] = str(spool_dir)
        try:
            state = RealShapeState()
            result = drain_session_claude_mapping_spool(state)
        finally:
            os.environ.pop(_SPOOL_ENV, None)
        _check(
            result == {"files_seen": 1, "upserted": 1, "skipped_malformed": 0},
            "one fresh spool file -> files_seen=1, upserted=1, skipped_malformed=0",
        )
        _check(not path.exists(), "the spool file is deleted after a successful ingest")
        rows = list_session_claude_mappings(state, "agi-ingest-1")
        _check(len(rows) == 1, "exactly one row appears in session_claude_mapping")
        _check(
            rows[0]["claude_session_id"] == "cs-fresh",
            "the row carries the right claude_session_id",
        )


def test_rerun_after_deletion_is_clean_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        _write_spool_file(spool_dir, agent_instance_id="agi-ingest-2", claude_session_id="cs-a")
        os.environ[_SPOOL_ENV] = str(spool_dir)
        try:
            state = RealShapeState()
            first = drain_session_claude_mapping_spool(state)
            second = drain_session_claude_mapping_spool(state)
        finally:
            os.environ.pop(_SPOOL_ENV, None)
        _check(first["upserted"] == 1, "precondition: the first drain ingested the one file")
        _check(
            second == {"files_seen": 0, "upserted": 0, "skipped_malformed": 0},
            "re-running on the now-empty dir is a clean no-op",
        )


def test_malformed_file_skipped_not_deleted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        bad_json_path = _write_spool_file(
            spool_dir,
            agent_instance_id="agi-bad-1",
            claude_session_id="cs-bad-1",
            content="not valid json{{{",
        )
        missing_field_path = spool_dir / "2026-08-05T16:00:01+00:00__agi-bad-2__cs-bad-2.json"
        missing_field_path.write_text(json.dumps({"agent_instance_id": "agi-bad-2"}))
        os.environ[_SPOOL_ENV] = str(spool_dir)
        try:
            state = RealShapeState()
            result = drain_session_claude_mapping_spool(state)
        finally:
            os.environ.pop(_SPOOL_ENV, None)
        _check(
            result == {"files_seen": 2, "upserted": 0, "skipped_malformed": 2},
            f"both malformed files are skipped, neither upserted (got {result!r})",
        )
        _check(
            bad_json_path.exists(),
            "invalid-JSON spool file is LEFT IN PLACE, never silently deleted",
        )
        _check(missing_field_path.exists(), "missing-required-field spool file is LEFT IN PLACE")
        rows = list_session_claude_mappings(state, "agi-bad-1")
        _check(rows == [], "no row is created for a malformed file")


def test_crash_before_delete_reingests_idempotently() -> None:
    """The core crash-safety guarantee: a file that survives a crash BEFORE
    its post-write delete (simulated here by making the first unlink() call
    raise) is re-ingested next drain as an UPSERT on the same row, never a
    duplicate."""
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        path = _write_spool_file(
            spool_dir,
            agent_instance_id="agi-crash-1",
            claude_session_id="cs-crash",
        )
        os.environ[_SPOOL_ENV] = str(spool_dir)
        try:
            state = RealShapeState()
            original_unlink = Path.unlink
            calls: list[int] = []

            def _flaky_unlink(self: Path, *a: Any, **k: Any) -> None:  # noqa: ANN401
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("simulated crash before delete")
                original_unlink(self, *a, **k)

            Path.unlink = _flaky_unlink  # type: ignore[method-assign]
            try:
                first = drain_session_claude_mapping_spool(state)
            finally:
                Path.unlink = original_unlink  # type: ignore[method-assign]
            _check(first["upserted"] == 1, "first drain upserts despite the delete failing")
            _check(
                path.exists(),
                "the file survives when delete fails, even though the upsert succeeded",
            )

            second = drain_session_claude_mapping_spool(state)
            _check(second["upserted"] == 1, "the surviving file is re-ingested on the next drain")
            _check(not path.exists(), "this time the delete succeeds and the file is gone")
        finally:
            os.environ.pop(_SPOOL_ENV, None)
        rows = list_session_claude_mappings(state, "agi-crash-1")
        _check(
            len(rows) == 1,
            "exactly ONE row exists after the crash-then-reingest sequence -- idempotent upsert, "
            "never a duplicate",
        )


def _capture_warnings() -> tuple[list[str], Any]:
    """Monkeypatches session_claude_mapping_ingest's own module logger to
    record warning messages -- returns (records, restore_token); caller
    MUST restore via ``session_claude_mapping_ingest.logger.warning =
    restore_token`` in a finally block."""
    original = session_claude_mapping_ingest.logger.warning
    records: list[str] = []

    def _record(msg: str, *args: object, **_kwargs: object) -> None:
        records.append(msg % args if args else msg)

    session_claude_mapping_ingest.logger.warning = _record  # type: ignore[method-assign]
    return records, original


def _spawn_managed_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    host: str = SESSION_HOST_HEADLESS,
    lifecycle_state: str = LIFECYCLE_LIVE,
    created_at: datetime = _T0,
    agent_runtime: str = "claude_code",
) -> None:
    """Inserts a ``managed_session`` row with a CONTROLLED ``created_at``
    (via ``state.now_iso`` override -- the fake's own stamping hook) and
    drives it to ``lifecycle_state`` over the AMEND-2b legal-transition
    matrix. Every non-``spawning`` target here is directly reachable from
    ``live`` in one hop (schema.py's ``LIFECYCLE_TRANSITIONS``), so this
    never needs more than two calls."""
    state.now_iso = lambda: created_at.isoformat()  # type: ignore[assignment]
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            lane_id="lane-hook-absence",
            brief_ref="",
            work_class=WORK_CLASS_READ_ONLY,
            budget_line="b1",
            host=host,
            agent_runtime=agent_runtime,
        ),
    )
    if lifecycle_state == LIFECYCLE_SPAWNING:
        return
    transition_lifecycle_state(
        state,
        agent_instance_id=agent_instance_id,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )
    if lifecycle_state != LIFECYCLE_LIVE:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_LIVE,
            to_state=lifecycle_state,
            directed_by="operator:none",
        )


def test_hook_absence_fires_past_grace_window() -> None:
    """RED-FIRST core case: a headless, LIVE managed_session row older than
    the grace window with NO hook:startup mapping row -> exactly one
    WARNING, naming the agent_instance_id -- the genuinely-broken-hook
    signal the cross-check alone cannot produce (its both-exist condition
    silently no-ops on a missing side, by design)."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(state, agent_instance_id="agi-hookless-1", host=SESSION_HOST_HEADLESS)
    past_grace = _T0 + timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 1)
    records, original = _capture_warnings()
    try:
        warned = detect_hook_absent_sessions(state, now=past_grace)
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(warned == 1, f"exactly one hook-absent row is flagged (got {warned})")
    _check(
        records and "agi-hookless-1" in records[0],
        "the WARNING names the agent_instance_id",
    )


def test_hook_absence_no_warning_within_grace_window() -> None:
    """The false-positive-prevention leg: the SAME setup as the fires-case
    above, but ``now`` is still inside the grace window -- ordinary spawn ->
    hook-fires -> spool-write -> next-drain-tick latency is not absence."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(state, agent_instance_id="agi-hookless-2", host=SESSION_HOST_HEADLESS)
    still_within_grace = _T0 + timedelta(seconds=10)
    warned = detect_hook_absent_sessions(state, now=still_within_grace)
    _check(warned == 0, "a row still inside the grace window is never flagged")


def test_hook_absence_no_warning_when_hook_present() -> None:
    """GREEN companion: the hook DID fire (a hook:startup row exists) ->
    no warning, even well past the grace window."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(state, agent_instance_id="agi-hookful-1", host=SESSION_HOST_TMUX)
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-hookful-1",
        claude_session_id="cs-present",
        captured_at=_T0.isoformat(),
        capture_source="hook:startup",
    )
    past_grace = _T0 + timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 1)
    warned = detect_hook_absent_sessions(state, now=past_grace)
    _check(warned == 0, "a row whose hook already fired is never flagged")


def test_hook_absence_includes_operator_host() -> None:
    """CATCHES: retaining the false operator-host exclusion after C1."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(state, agent_instance_id="agi-operator-1", host=SESSION_HOST_OPERATOR)
    past_grace = _T0 + timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 1)
    warned = detect_hook_absent_sessions(state, now=past_grace)
    _check(warned == 1, "an old hookless operator-host row is flagged")


def test_hook_absence_excludes_terminal_and_spawning_states() -> None:
    """terminated rows are no longer actionable (the worker is gone --
    warning forever would be noise); spawning rows are too early by
    construction (the grace window alone does not gate this -- the state
    itself is out of scope)."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(
        state,
        agent_instance_id="agi-terminated-1",
        host=SESSION_HOST_HEADLESS,
        lifecycle_state=LIFECYCLE_TERMINATED,
    )
    _spawn_managed_session(
        state,
        agent_instance_id="agi-spawning-1",
        host=SESSION_HOST_HEADLESS,
        lifecycle_state=LIFECYCLE_SPAWNING,
    )
    far_future = _T0 + timedelta(days=365)
    warned = detect_hook_absent_sessions(state, now=far_future)
    _check(warned == 0, "terminated and spawning rows are excluded regardless of elapsed time")


def test_hook_absence_counts_multiple_absent_rows() -> None:
    """The caller's own tick-summary count must reflect every distinct
    absent row, not just whether any fired."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(state, agent_instance_id="agi-multi-1", host=SESSION_HOST_HEADLESS)
    _spawn_managed_session(state, agent_instance_id="agi-multi-2", host=SESSION_HOST_TMUX)
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-multi-2",
        claude_session_id="cs-multi-2",
        captured_at=_T0.isoformat(),
        capture_source="hook:startup",
    )
    _spawn_managed_session(state, agent_instance_id="agi-multi-3", host=SESSION_HOST_HEADLESS)
    past_grace = _T0 + timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 1)
    warned = detect_hook_absent_sessions(state, now=past_grace)
    _check(warned == 2, f"exactly the two genuinely hook-absent rows are counted (got {warned})")


def test_hook_absence_is_once_latched_per_session() -> None:
    """Regression: an unresolved hook absence is logged once, never once per tick."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(state, agent_instance_id="agi-latched-1")
    past_grace = _T0 + timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 1)
    records, original = _capture_warnings()
    try:
        first = detect_hook_absent_sessions(state, now=past_grace)
        second = detect_hook_absent_sessions(state, now=past_grace + timedelta(minutes=5))
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(first == 1, "the first eligible sweep emits the actionable warning")
    _check(second == 0, "the next sweep does not re-warn the same absent session")
    _check(len(records) == 1, "one absent session produces exactly one WARNING across two ticks")


def test_hook_absence_excludes_codex_sessions_that_cannot_fire_claude_hooks() -> None:
    """Codex has no Claude SessionStart hook, so its absence must not be alarmed."""
    state = cast("StateManagementInterface", RealShapeState())
    _spawn_managed_session(
        state,
        agent_instance_id="agi-codex-hookless-1",
        agent_runtime="codex",
    )
    backfill_registration(
        state,
        agent_instance_id="agi-codex-hookless-1",
        agent_id="codex",
        agent_session_id="ases-agi-codex-hookless-1",
    )
    past_grace = _T0 + timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 1)
    records, original = _capture_warnings()
    try:
        warned = detect_hook_absent_sessions(state, now=past_grace)
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(warned == 0, "a structurally hookless Codex session is excluded")
    _check(records == [], "Codex produces no unactionable HOOK ABSENCE warning")


def test_hook_absence_tick_is_bounded_to_one_page() -> None:
    """A large unresolved population is handled incrementally, never in one sweep."""
    state = cast("StateManagementInterface", RealShapeState())
    for number in range(30):
        _spawn_managed_session(state, agent_instance_id=f"agi-bounded-{number:02d}")
    past_grace = _T0 + timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 1)
    warned = detect_hook_absent_sessions(state, now=past_grace)
    _check(warned == 25, "one sweep handles at most the fixed 25-session page")


def test_sweep_tick_wiring_reaches_hook_absence_detection() -> None:
    """The 'wired consumer' proof for this capability: drives the REAL
    ``AgentMessagingPlugin._run_session_lifecycle_sweep`` (mirrors
    ``test_sweep_tick_wiring_reaches_the_real_drain`` below) and confirms it
    genuinely reaches ``detect_hook_absent_sessions``, not merely that the
    function exists unwired. The sweep tick has no injectable clock, so the
    row's ``created_at`` is stamped far enough in the real past (well beyond
    the grace window) that any normal test runtime cannot false-negative it."""
    state = cast("StateManagementInterface", RealShapeState())
    long_ago = datetime.now(UTC) - timedelta(seconds=DEFAULT_HOOK_ABSENCE_GRACE_WINDOW_S + 300)
    _spawn_managed_session(
        state,
        agent_instance_id="agi-wired-hookless-1",
        host=SESSION_HOST_HEADLESS,
        created_at=long_ago,
    )
    records, original = _capture_warnings()
    fake_self = SimpleNamespace(
        _get_state_service=lambda: state,
        _peer_registry=None,
        _bridge_manager=None,
        _session_role_claim_pruner=None,
    )
    try:
        AgentMessagingPlugin._run_session_lifecycle_sweep(cast("Any", fake_self))  # noqa: SLF001
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(
        any("agi-wired-hookless-1" in r for r in records),
        "the REAL _run_session_lifecycle_sweep genuinely reaches hook-absence detection, "
        "not just that the function exists unwired",
    )


def test_cross_check_mismatched_startup_pair_warns() -> None:
    """Red-first leg: a hook:startup row and an init_event row for the SAME
    agent_instance_id with DIFFERENT claude_session_id values -> WARNING
    fires, naming both session ids."""
    state = RealShapeState()
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-1",
        claude_session_id="cs-from-hook",
        captured_at="2026-08-05T18:00:00+00:00",
        capture_source="hook:startup",
    )
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-1",
        claude_session_id="cs-from-init-event",
        captured_at="2026-08-05T18:00:00+00:00",
        capture_source="init_event",
    )
    records, original = _capture_warnings()
    try:
        session_claude_mapping_ingest._cross_check_init_event(state, "agi-xcheck-1")  # noqa: SLF001
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(
        len(records) == 1,
        "a genuinely mismatched startup/init_event pair fires exactly one WARNING",
    )
    _check(
        records and "cs-from-hook" in records[0] and "cs-from-init-event" in records[0],
        "the WARNING names both claude_session_id values",
    )


def test_cross_check_matching_pair_no_warning() -> None:
    """GREEN companion: the SAME claude_session_id from both sources ->
    no warning -- the pairing only fires on a genuine disagreement."""
    state = RealShapeState()
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-2",
        claude_session_id="cs-agree",
        captured_at="2026-08-05T18:00:00+00:00",
        capture_source="hook:startup",
    )
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-2",
        claude_session_id="cs-agree",
        captured_at="2026-08-05T18:00:01+00:00",
        capture_source="init_event",
    )
    records, original = _capture_warnings()
    try:
        session_claude_mapping_ingest._cross_check_init_event(state, "agi-xcheck-2")  # noqa: SLF001
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(records == [], "a matching startup/init_event pair fires NO warning")


def test_cross_check_clear_row_never_contaminates_the_pairing() -> None:
    """The FALSE-POSITIVE leg the ruling addendum names by name: a clean,
    matching hook:startup/init_event pair PLUS a hook:clear row with a
    DIFFERENT claude_session_id (exactly what a real /clear produces) must
    NOT fire a warning -- hook:clear is entirely out of the pairing's
    scope. A one-sided guard that manufactures warnings on every /clear
    would get ignored, un-guarding the real mismatch class."""
    state = RealShapeState()
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-3",
        claude_session_id="cs-startup-clean",
        captured_at="2026-08-05T18:00:00+00:00",
        capture_source="hook:startup",
    )
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-3",
        claude_session_id="cs-startup-clean",
        captured_at="2026-08-05T18:00:01+00:00",
        capture_source="init_event",
    )
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-3",
        claude_session_id="cs-after-a-real-clear",
        captured_at="2026-08-05T18:05:00+00:00",
        capture_source="hook:clear",
    )
    records, original = _capture_warnings()
    try:
        session_claude_mapping_ingest._cross_check_init_event(state, "agi-xcheck-3")  # noqa: SLF001
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(
        records == [],
        "a differing hook:clear row alongside a CLEAN startup/init_event pair fires NO "
        "warning -- clear rows are out of the pairing's scope by construction",
    )


def test_cross_check_missing_either_side_no_warning() -> None:
    """Both directions of 'missing either source -> no warning': tmux (no
    init event ever) and a headless worker whose hook hasn't fired yet
    both fall out silently, never flagged as a mismatch."""
    state = RealShapeState()
    upsert_session_claude_mapping(
        state,
        agent_instance_id="agi-xcheck-4",
        claude_session_id="cs-tmux-only",
        captured_at="2026-08-05T18:00:00+00:00",
        capture_source="hook:startup",
    )
    records, original = _capture_warnings()
    try:
        session_claude_mapping_ingest._cross_check_init_event(state, "agi-xcheck-4")  # noqa: SLF001
    finally:
        session_claude_mapping_ingest.logger.warning = original
    _check(records == [], "hook:startup with no init_event row (e.g. tmux) fires no warning")

    state2 = RealShapeState()
    upsert_session_claude_mapping(
        state2,
        agent_instance_id="agi-xcheck-5",
        claude_session_id="cs-init-only",
        captured_at="2026-08-05T18:00:00+00:00",
        capture_source="init_event",
    )
    records2, original2 = _capture_warnings()
    try:
        session_claude_mapping_ingest._cross_check_init_event(state2, "agi-xcheck-5")  # noqa: SLF001
    finally:
        session_claude_mapping_ingest.logger.warning = original2
    _check(records2 == [], "init_event with no hook:startup row yet fires no warning")


def test_full_drain_pipeline_fires_the_cross_check() -> None:
    """End-to-end: a hook:startup spool file and an init_event spool file
    for the same agent_instance_id, MISMATCHED, both ingested in ONE
    drain() call -> the drain's own wiring reaches the cross-check, not
    just the standalone _cross_check_init_event function."""
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        _write_spool_file(
            spool_dir,
            agent_instance_id="agi-xcheck-e2e",
            claude_session_id="cs-e2e-hook",
            capture_source="hook:startup",
        )
        spool_dir.mkdir(parents=True, exist_ok=True)
        (spool_dir / "2026-08-05T18:00:01+00:00__agi-xcheck-e2e__cs-e2e-init.json").write_text(
            json.dumps(
                {
                    "agent_instance_id": "agi-xcheck-e2e",
                    "claude_session_id": "cs-e2e-init",
                    "captured_at": "2026-08-05T18:00:01+00:00",
                    "capture_source": "init_event",
                }
            ),
        )
        os.environ[_SPOOL_ENV] = str(spool_dir)
        records, original = _capture_warnings()
        try:
            state = RealShapeState()
            result = drain_session_claude_mapping_spool(state)
        finally:
            os.environ.pop(_SPOOL_ENV, None)
            session_claude_mapping_ingest.logger.warning = original
        _check(result["upserted"] == 2, "both spool files (hook:startup + init_event) are ingested")
        _check(
            any("agi-xcheck-e2e" in r for r in records),
            "the full drain() pipeline itself fires the cross-check WARNING -- genuinely "
            "wired, not just the standalone comparison function",
        )


def test_sweep_tick_wiring_reaches_the_real_drain() -> None:
    """The 'wired consumer' proof: drives the REAL
    ``AgentMessagingPlugin._run_session_lifecycle_sweep`` against a
    duck-typed self (peer_registry/bridge_manager both None, mirroring an
    early-boot tick) and confirms a spool file placed under the declared
    env var is genuinely ingested and deleted BY THE SWEEP TICK ITSELF --
    not merely that ``drain_session_claude_mapping_spool`` exists as a
    standalone, unwired function."""
    with tempfile.TemporaryDirectory() as tmp:
        spool_dir = Path(tmp) / "spool"
        path = _write_spool_file(
            spool_dir,
            agent_instance_id="agi-wired-1",
            claude_session_id="cs-wired",
        )
        os.environ[_SPOOL_ENV] = str(spool_dir)
        state = RealShapeState()
        fake_self = SimpleNamespace(
            _get_state_service=lambda: state,
            _peer_registry=None,
            _bridge_manager=None,
            _session_role_claim_pruner=None,
        )
        try:
            AgentMessagingPlugin._run_session_lifecycle_sweep(cast("Any", fake_self))  # noqa: SLF001
        finally:
            os.environ.pop(_SPOOL_ENV, None)
        _check(
            not path.exists(),
            "the REAL _run_session_lifecycle_sweep deleted the spool file -- the drain is "
            "genuinely wired into the sweep tick, not just a standalone function",
        )
        rows = list_session_claude_mappings(state, "agi-wired-1")
        _check(
            len(rows) == 1 and rows[0]["claude_session_id"] == "cs-wired",
            "the sweep-tick-driven drain wrote the correct row into session_claude_mapping",
        )


def main() -> int:
    test_missing_spool_dir_returns_zero_processed()
    test_app_home_fallback_drains_platform_side()
    test_explicit_spool_env_wins_over_app_home()
    test_nonexistent_spool_dir_returns_zero_processed()
    test_fresh_file_ingested_and_deleted()
    test_rerun_after_deletion_is_clean_noop()
    test_malformed_file_skipped_not_deleted()
    test_crash_before_delete_reingests_idempotently()
    test_cross_check_mismatched_startup_pair_warns()
    test_cross_check_matching_pair_no_warning()
    test_cross_check_clear_row_never_contaminates_the_pairing()
    test_cross_check_missing_either_side_no_warning()
    test_full_drain_pipeline_fires_the_cross_check()
    test_sweep_tick_wiring_reaches_the_real_drain()
    test_hook_absence_fires_past_grace_window()
    test_hook_absence_no_warning_within_grace_window()
    test_hook_absence_no_warning_when_hook_present()
    test_hook_absence_includes_operator_host()
    test_hook_absence_excludes_terminal_and_spawning_states()
    test_hook_absence_counts_multiple_absent_rows()
    test_hook_absence_is_once_latched_per_session()
    test_hook_absence_excludes_codex_sessions_that_cannot_fire_claude_hooks()
    test_hook_absence_tick_is_bounded_to_one_page()
    test_sweep_tick_wiring_reaches_hook_absence_detection()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
