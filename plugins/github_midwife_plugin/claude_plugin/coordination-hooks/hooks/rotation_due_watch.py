#!/usr/bin/env python3
"""rotation-systematization P2 slice B (ruling 2, P1 ratification 2026-08-07
-- workbench/2026-08-07_rotation_systematization_findings_rotation-impl.md)
-- PostToolUse hook, sibling to ``heartbeat_report_alive.py``: the
host-independent rotation-due trigger for BOTH the seat and every worker.

Reads this session's OWN ``transcript_path`` (the same stdin field Claude
Code hooks already carry), tails the most recent ``type=assistant`` line's
``message.usage`` block (a zero-lag, no-ingestion-latency proxy for current
context occupancy -- measured live against this session's own transcript
during P1(b)), compares against the declared ceiling in
``agent_messaging_plugin.rotation_thresholds`` (a SINGLE source of truth --
imported directly rather than duplicated, since that module has zero
third-party dependencies and is safe to import without the venv), and on a
threshold crossing NOTIFIES the steward. It never acts (never calls
``clear_session`` or the seat's own rotation helper) and never touches
``report_by``/``report_alive``/any lifecycle-transition verb -- rotation
timing stays a steward/seat decision (ruling 2; brief's own out-of-scope
line: "changing WHEN the fleet rotates as policy... stays with the
seat/operator").

Two independent marker files, distinct purposes (both under the SAME
declared ``AGENT_HEARTBEAT_MARKER_DIR`` this checkout already wires --
composing the existing declared directory rather than requesting new
adapter-side env wiring, per the brief's "prefer composing landed
machinery over new surface" framing):
- THROTTLE (per ``agent_instance_id``): how often this hook even computes
  -- cost control, same shape as ``heartbeat_report_alive.py``'s own
  throttle.
- LATCH (per ``agent_instance_id`` + the CURRENT ``claude_session_id``):
  fires the notification at most once per session-generation. Keying on
  the CURRENT claude_session_id (not just agent_instance_id) means a
  ``/clear`` naturally gets a fresh, absent latch file with no explicit
  reset logic -- the session_claude_mapping capture already re-fires on
  every new session_id (``hook:clear`` etc.), so this hook's own re-fire
  on the same PostToolUse wiring needs no bespoke reset path either.

Steward resolution: ``session_status`` for this ``agent_instance_id`` ->
``spawned_by_role`` when the row exists and carries one (every managed
worker). For a row that doesn't exist (``host=operator`` -- e.g. the seat
itself, never spawned via ``spawn_session``) or carries no
``spawned_by_role``, this falls back to a LOCALLY-SURFACED marker file
(self-notification artifact) rather than failing -- consumption of that
marker on the seat's own next turn is a named follow-on, not built here
(this hook's job is the trigger + delivery attempt, not the read-back
UX).

Notification identity discipline: the message CONTENT carries this
session's ``agent_instance_id``/``session_label`` verbatim as text (never
relies on the transport's own sender-identity field to carry it) --
per this fleet's own measured trap that a bare CLI send drops caller
identity (names route, content binds).

Non-fatal by design, same contract as this checkout's other hooks: any
failure (missing env var, unreadable transcript, unparseable JSON,
``solet-bridge`` subprocess failure) warns on stderr and exits 0 -- this
hook must never cost a session its tool call.

Stdlib-only for I/O and subprocess dispatch, EXCEPT the one direct import
of ``agent_messaging_plugin.rotation_thresholds`` (zero-dependency pure
module, safe outside the venv) -- mirrors this repo's other hooks
(``capture_session_mapping.py``, ``heartbeat_report_alive.py``).

THREE COPIES OF THIS FILE EXIST, AND THE ONE YOU ARE READING IS PROBABLY
NOT THE ONE THAT RAN. Editing any one of them changes nothing about the
other two:

1. ``.claude/hooks/rotation_due_watch.py`` -- the CHECKOUT copy, wired TWO
   independent ways: this repo's own ``.claude/settings.json`` (PostToolUse,
   no matcher), AND, for a SPAWNED WORKER, a generated ``--settings`` blob
   carrying this file's ABSOLUTE, spawn-time-resolved path (see
   ``agent_messaging_plugin.headless_adapter._resolve_worker_hook_path``,
   used by the tmux driver too: rung 1 is ``<repo_root>/.claude/hooks/``
   and always wins when present, rung 2 is the vendored copy below, and
   the version-keyed cache is DELIBERATELY never used because of exactly
   the staleness trap described further down). So a spawned worker runs
   THIS copy, resolved by absolute path -- it needs no environment
   variable and its ``parents[N]`` arithmetic is correct from that path.
2. ``plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/``
   ``rotation_due_watch.py`` -- the VENDORED SOURCE that is packaged into
   the ``coordination-hooks`` plugin. Nothing executes this path directly;
   it is what an install COPIES FROM.
3. ``~/.claude/plugins/cache/<marketplace>/coordination-hooks/<version>/``
   ``hooks/rotation_due_watch.py`` -- the INSTALLED copy, wired by the
   plugin's own ``hooks/hooks.json`` (PostToolUse, no matcher) whenever
   ``coordination-hooks@<marketplace>`` is enabled at user scope. This is
   what the PLUGIN's registration resolves to -- note it is an ADDITIONAL
   registration, not an alternative one: a session can run copy 1 and copy
   3 in the same tick, and a spawned worker in this checkout does exactly
   that. Do not read "the plugin copy fires" as "the plugin copy is the one
   that fires".

Copies 1 and 2 are held in deliberate content lockstep and differ ONLY
inside ``_resolve_plugin_src_path``/``_import_rotation_thresholds`` -- see
the docstring on the vendored copy's ``_resolve_plugin_src_path`` for why
(it has no fixed parent-directory depth to fall back on, so it must resolve
from ``CLAUDE_PROJECT_DIR`` alone or skip, rather than guess). That
divergence is an ADAPTATION, NOT ROT: a naive dedup into one file breaks
adopter path resolution. Any change to either copy must be mirrored into
the other in the SAME landing, and the diff between them must stay confined
to that one region.

Copy 3 DOES NOT FOLLOW FROM A COMMIT. Install copies the plugin into a
VERSIONED CACHE DIRECTORY; merging to master -- even bumping the manifest
version -- changes nothing about what executes. Only an explicit reinstall
moves the pinned entry in ``~/.claude/plugins/installed_plugins.json`` to a
new ``installPath``. A bump WITHOUT a reinstall is the expected failure
here, and it is silent: the repo reads correct at every version while the
process keeps running old code. Verify a hook change actually reached copy
3 by comparing that file's ``version`` and ``gitCommitSha`` against the
plugin manifest and against master -- NEVER by re-reading the repo file you
just edited.

Copies 1 and 3 both bind PostToolUse with no matcher, and settings sources
MERGE rather than override, so in a project that wires copy 1 while the
plugin is enabled BOTH still receive the event. A spawned worker's host
adapter now exports ``AGENT_CONTEXT_GAUGE_REPORTER_PATH`` as copy 1's
absolute, spawn-time-resolved path. Every copy compares that designation to
its own ``__file__`` before it claims the shared throttle or latch: only the
designated copy reports, and every other registration exits 0 without a
marker write. The setting is absent for sessions without generated worker
settings, preserving the plugin-cache reporter there. This is an election of
ONE writer, not a renamed pair of independent writers.
"""


from __future__ import annotations

# INTERPRETER FLOOR. These hooks are Python 3.13 source and use datetime.UTC
# (3.11+). Claude Code launches them with a bare `python3`, which resolves from
# PATH -- on a stock macOS that is frequently the system 3.9, and the resulting
# ImportError traceback surfaced to the operator as a hook error on EVERY tool
# call. Measured 2026-08-20: 8 of 20 shipped hook modules failed to import.
#
# Placed AFTER `from __future__` (which must stay the first statement) and
# BEFORE the first 3.11+ import, because an ImportError at module level cannot
# be caught by anything inside this file. Exits 0 and SILENTLY: the shipped
# contract is that a session which cannot run these hooks gets zero output and
# zero errors, and a diagnostic here would reproduce the very symptom it fixes.
# A floor, not a compatibility shim -- nothing is emulated or back-ported.
import sys

if sys.version_info < (3, 11):  # noqa: UP036 -- see above; ruff assumes
    # the project's py313 target, but this file ships to an ADOPTER's machine
    # and is launched by whatever `python3` their PATH resolves.
    raise SystemExit(0)


import json
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_OWNER_DIRECTORY = str(Path(__file__).resolve().parent)
if _OWNER_DIRECTORY not in sys.path:
    sys.path.insert(0, _OWNER_DIRECTORY)
import coordination_owner  # noqa: E402 -- sibling path follows runtime floor

_MARKER_DIR_ENV = "AGENT_HEARTBEAT_MARKER_DIR"
_INSTANCE_ID_ENV = "AGENT_INSTANCE_ID"
_GAUGE_REPORTER_PATH_ENV = "AGENT_CONTEXT_GAUGE_REPORTER_PATH"
# The session's STABLE id, distinct from the instance id above and NOT
# derivable from it. It is what makes a gauge row routable: the row keys on
# the LEDGER instance id while a watcher-held session's live bridge binding
# keys on its WATCH id, and only this value joins the two through the
# registry. See the schema column of the same name.
_SESSION_ID_ENV = "AGENT_SESSION_ID"
_SESSION_LABEL_ENV = "AGENT_SESSION_LABEL"
_PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"

# Comfortably more frequent than the heartbeat's 180s -- rotation-due is a
# slower-moving signal than liveness (context grows over many turns), but
# still needs to catch a boundary reasonably soon after it's crossed. Not
# measured against a real growth-rate curve this pass; a declared default,
# not a guess dressed as one -- named as such.
_THROTTLE_SECONDS = 120.0

_SESSION_STATUS_PROCESS_KEY = "plugin::agent_messaging_plugin::session_status"
_PEER_SEND_PROCESS_KEY = "plugin::agent_messaging_plugin::peer_send_by_name"
# maintenance-verbs M1 (workbench
# 2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3, shape (a)):
# this hook already computes current_tokens/model/ceiling every un-throttled
# tick for the notify path below -- piggybacking a plain state-cache write
# onto the SAME tick (same throttle window governs both) is the whole of
# shape (a)'s "hook-fed cache" design, no new wiring surface needed for
# worker coverage (workers already carry this hook in their spawn-time
# adapter blob, the same precedent heartbeat_report_alive.py set).
_REPORT_CONTEXT_STATUS_PROCESS_KEY = "plugin::agent_messaging_plugin::report_context_status"
_PEER_LIST_PROCESS_KEY = "plugin::agent_messaging_plugin::peer_list"
# This copy's own CONTENT GENERATION, reported alongside every snapshot so a
# reader can tell a current copy from an older one that is still being served
# (copies of this file go stale independently -- see the module docstring).
#
# BUMP THIS whenever the reporting content changes, in BOTH repo copies, in
# the same landing. It is not a version of the file, and deliberately not a
# git sha: this file cannot know the commit it was copied from. It answers
# exactly one question -- "is the code that wrote this row as new as the code
# I am reading?" -- and it can only answer it if the bump is not forgotten.
#
# 1 = the generation that first carried reporter attribution (2026-08-16),
#     which is also the first generation carrying the cache-state fields.
# 2 = surface classification resolved (2026-08-17): 'vendored' and 'release'
#     split out of the collapsed 'unknown', and a checkout hook in a
#     SUBDIRECTORY no longer misreports as unrecognised. A row reading
#     generation 1 with surface 'unknown' is therefore AMBIGUOUS by
#     construction -- it may be any of the three surfaces this generation
#     learned to tell apart -- and a row reading generation 2 is not.
# 3 = the routing join (2026-08-18): this generation reports
#     `agent_session_id`, which is what makes a watcher-held worker reachable
#     from its own gauge row. A row reading generation <= 2 carries NULL there
#     and is UNROUTABLE FOR A KNOWN, BENIGN REASON -- a stale reporter, not a
#     dead session. That distinction only exists because this constant was
#     bumped; without it, a NULL join could not be told apart from a session
#     that genuinely failed to route.
# 4 = the runtime-neutral identity fields (2026-08-29): the reporting verb
#     requires the runtime session id, provider, runtime, and selected effort.
# 5 = spawned-session reporter election (2026-08-30): the designated absolute
#     hook path is the only copy allowed to claim the shared throttle/write.
_REPORTER_GENERATION = 5
# Staleness bound for the seat's registry row. Basis (measured 2026-08-16 over
# 6 live rows): heartbeat ages were 3s / 11s median / 163s stalest, so this is
# ~1.8x the stalest observed and comfortably above this hook's own 60s poll.
# Far below any realistic pid release-reuse-reregister window. The sample is
# THIN; if a legitimate seat is ever skipped as stale, re-measure the cadence
# for genuinely IDLE sessions before widening it.
_SEAT_REGISTRY_MAX_AGE_SECONDS = 300
_ANCESTOR_WALK_MAX_DEPTH = 12
_CLAUDE_PROCESS_NAME = "claude"
_missing_effort_warned = False


def _warn(message: str) -> None:
    try:
        print(f"[rotation-due-watch] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 -- telemetry strictly best-effort
        pass


def _is_designated_gauge_reporter() -> bool:
    """Whether this hook copy owns gauge writes for this process.

    A spawned worker has an absolute hook path selected by its host adapter.
    Both that injected hook and a user-scope plugin-cache registration can
    receive the same PostToolUse event, but only the selected path may claim
    this session's throttle or write its gauge row. The cache registration is
    deliberately still useful for sessions without this spawn-only setting,
    so an absent designation preserves that standalone path.
    """
    designated = os.environ.get(_GAUGE_REPORTER_PATH_ENV, "").strip()
    if not designated:
        return True
    return Path(__file__).resolve() == Path(designated).expanduser().resolve()


def _throttle_marker_path(identity_dir: Path, agent_instance_id: str) -> Path:
    return Path(identity_dir) / "context_watch" / f"{agent_instance_id}.rotation_due_check.stamp"


def _latch_marker_path(identity_dir: Path, agent_instance_id: str, claude_session_id: str) -> Path:
    return Path(identity_dir) / "context_watch" / f"{agent_instance_id}__{claude_session_id}.rotation_due_latch"


def _fallback_marker_path(identity_dir: Path, agent_instance_id: str, claude_session_id: str) -> Path:
    return Path(identity_dir) / "context_watch" / f"{agent_instance_id}__{claude_session_id}.rotation_due_selfnotify.json"


def _deferred_marker_path(identity_dir: Path, agent_instance_id: str, claude_session_id: str) -> Path:
    """GAU-14 (D2): notes, ONCE per session generation, that this hook stood
    down because the solet's own legs are covering the same condition.

    ★ WHY A MARKER AND NOT A BARE WARNING. Standing down silently would make a
    hook that deferred indistinguishable from a hook that never ran or never
    fired -- the defect class this whole family keeps paying for (GAU-02). But
    the deferral holds for the REST of the generation, and this hook is on a
    120s throttle, so an ungated warning would repeat every two minutes for the
    life of the session. One line per generation is the only version that is
    both observable and not noise.

    ★ WHY IT IS NOT A NEW RACE. The split-marker hazard is real when two
    PROCESSES read and write the same fact. This file is the only writer of
    every marker it uses, the key is the same (instance, generation) pair the
    delivery latch already uses, and nothing reads it but the next tick of this
    same hook. It is a fourth marker of an existing shape, not a second source
    of truth for an existing fact.
    """
    return Path(identity_dir) / "context_watch" / f"{agent_instance_id}__{claude_session_id}.rotation_due_deferred"


def is_throttled(marker_path: Path, *, now: float, throttle_seconds: float = _THROTTLE_SECONDS) -> bool:
    """True means "skip -- computed recently enough". A marker that
    doesn't exist, or that fails to stat for any reason, is never
    throttled (the safe default is to attempt a compute, matching
    ``heartbeat_report_alive.py``'s own ``_throttled`` contract)."""
    try:
        age = now - marker_path.stat().st_mtime
    except OSError:
        return False
    return age < throttle_seconds


def touch_marker(marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(time.time()))


def _usage_bearing_line(raw_line: str) -> tuple[str, dict[str, Any], str | None] | None:
    """One transcript line -> ``(model, usage, reading_at)``, or ``None`` to
    skip it (blank, unparseable, not an assistant turn, or carrying no usage
    block).

    Split out of :func:`find_last_assistant_usage` when GAU-14's ``reading_at``
    took that scanner to cyclomatic C: the loop is a straight "first line that
    parses wins" and every one of the five reasons to skip is a property of the
    LINE, not of the scan. Extracting them leaves the caller as the scan it
    always was, which is also the honest decomposition rather than the one that
    merely moves branches somewhere the gate is not looking.
    """
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    stamp = record.get("timestamp")
    reading_at = stamp if isinstance(stamp, str) and stamp else None
    return str(message.get("model") or ""), usage, reading_at


def find_last_assistant_usage(
    transcript_path: str,
) -> tuple[str, dict[str, Any], str | None] | None:
    """The most recent ``type=assistant`` line's ``(model, usage, reading_at)``
    from the transcript JSONL, scanning from the end. ``None`` when the
    file is unreadable, empty, or carries no usage-bearing assistant line
    yet (a brand-new session before its first turn completes) -- never
    raises, matching this hook's non-fatal contract.

    ★ ``reading_at`` IS THAT LINE'S OWN ``timestamp`` (GAU-14 D3, 2026-08-19),
    and it was already in this function's hand -- it read the line, took two
    fields out of it, and dropped the third. Returning it costs nothing and
    fixes a measured defect: every notice built from this reading stamps the
    OBSERVER's clock and reads as though that were when the number was taken.
    On 2026-08-19 the seat's two paths reported 164,118 "measured 01:19:31Z"
    and 153,682 "measured ~01:21Z" -- later-but-LOWER -- and both were real
    lines of the same monotone transcript in the correct order (01:18:57.127Z,
    01:17:10.380Z). The readings never disagreed; only the stamps did.

    ``None`` for the third element means the line carried no usable
    ``timestamp``. It is NOT filled in from the clock: a fabricated
    ``reading_at`` would assert a zero observation lag exactly where the lag
    is unknown, which is the one thing this field exists to prevent.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        parsed = _usage_bearing_line(raw_line)
        if parsed is not None:
            return parsed
    return None


def _cache_read_tokens(usage: dict[str, Any]) -> int:
    """``cache_read_input_tokens``, or 0 when absent/non-numeric.

    0 here means "read nothing from cache", which is the cold signal -- so an
    absent field reads as cold rather than as unknown. That is safe ONLY
    because the classifier looks at a sequence and the caller omits the whole
    field set when it cannot parse the transcript; a single odd block cannot
    fabricate a cold verdict on its own.
    """
    raw = usage.get("cache_read_input_tokens")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return int(raw)


def _assistant_call_from_line(raw_line: str) -> dict[str, Any] | None:
    """One transcript line -> ``{"at", "cache_read"}``, or ``None`` to skip."""
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    stamp = record.get("timestamp")
    if not isinstance(message, dict) or not isinstance(stamp, str):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    return {"at": stamp, "cache_read": _cache_read_tokens(usage)}


def read_assistant_calls(transcript_path: str, limit: int = 40) -> list[dict[str, Any]]:
    """The most recent assistant calls as ``{"at": str, "cache_read": int}``,
    oldest-first, for cache-state classification.

    Separate from :func:`find_last_assistant_usage`, which answers a different
    question (the LATEST model + usage). Cache state is a property of a
    SEQUENCE -- one cold call after a long gap is ordinary expiry, repeated
    cold calls across short gaps are not -- so it cannot be read off a single
    block. Bounded to `limit` because the classifier only needs the recent
    tail and a transcript can carry thousands of blocks.

    Never raises, matching this hook's non-fatal contract.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    calls: list[dict[str, Any]] = []
    for raw_line in reversed(lines):
        call = _assistant_call_from_line(raw_line)
        if call is None:
            continue
        calls.append(call)
        if len(calls) >= limit:
            break
    calls.reverse()
    return calls


def sum_context_tokens(usage: dict[str, Any]) -> int:
    """``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``
    -- the full set of tokens the CLI reports as consumed to produce the
    most recent turn, the same fields ``budget_report.py`` sums server-side
    (measured live against this session's own transcript, P1(b)). Missing
    or non-numeric fields count as 0 -- never raises on a partial usage
    block."""
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def build_notification_content(
    *, agent_instance_id: str, session_label: str, model: str,
    current_tokens: int, ceiling: int, band: str, band_actionable: bool,
    threshold_fraction: float, fraction_crossed: bool,
) -> str:
    """Return the sole durability notice text for a due session."""
    return f"context is {current_tokens:,} — make sure everything is durable."


def _resolve_plugin_src_path() -> Path | None:
    """``CLAUDE_PROJECT_DIR``-relative only -- unlike the checkout-local
    original this vendored copy has no fixed parent-directory depth to fall
    back on (this file's own depth under ``$CLAUDE_PLUGIN_ROOT/hooks/``
    differs from the checkout original's ``.claude/hooks/`` depth, so a
    ``parents[N]`` guess would silently resolve to the wrong directory on
    an adopter machine rather than fail). Claude Code always sets this env
    var for a real hook invocation; an unset value means skip, never guess."""
    project_dir = os.environ.get(_PROJECT_DIR_ENV, "").strip()
    if not project_dir:
        return None
    return Path(project_dir) / "plugins" / "agent_messaging_plugin" / "src"


def _import_rotation_thresholds() -> Any | None:
    src_path = _resolve_plugin_src_path()
    if src_path is None:
        _warn(f"{_PROJECT_DIR_ENV} not set -- cannot locate rotation_thresholds, skipping")
        return None
    src_path_str = str(src_path)
    if src_path_str not in sys.path:
        sys.path.insert(0, src_path_str)
    try:
        from agent_messaging_plugin import rotation_thresholds  # noqa: PLC0415
    except ImportError as exc:
        _warn(f"could not import rotation_thresholds: {exc}")
        return None
    return rotation_thresholds


def _solet_call_env() -> dict[str, str]:
    """``os.environ``, with PATH APPENDED by ``AGENT_WAKE_CLI``'s directory
    when that directory actually contains a file named ``solet-bridge`` --
    SECURITY.md's disclosed contract for this hook keeps argv literally
    ``["solet-bridge", "call", ...]`` (PATH-resolved, from the session's own
    environment, same category as before); this widens WHICH directories
    PATH searches, not what gets exec'd by name.

    APPEND, not prepend (2026-08-16, cross-session review): a prepend would
    make the release venv's bin dir win PATH resolution for EVERY lookup in
    this subprocess and anything it spawns, not just ``solet-bridge`` -- that
    directory also carries ``python3``/``pip``, so a prepend would silently
    change which of those a child process resolves too, a behavior change
    beyond "find the right solet-bridge" with no signal in the diff's intent.
    Append fixes the identical missing-solet-bridge case (a PATH that lacks solet-bridge
    entirely resolves it either way, first match or last) while never
    shadowing an existing resolution -- it only ever adds a location PATH
    lookup falls through to, never reorders one already there.

    2026-08-16 dark-gauge root cause: a bare ``"solet-bridge"`` lookup against the
    UNMODIFIED PATH silently ``FileNotFoundError``s on a worker whose PATH
    excludes the venv bin dir -- caught below, warned to stderr (nothing
    reads it), exit 0. The throttle marker gets touched upstream of this
    call (see ``main``), so the failure looks identical to a healthy tick
    from the outside: "stamp updates, no report ever lands." Measured live
    (session_context_status resolved:false with no row at all despite an
    updating throttle stamp), reproduced by hand.

    ``AGENT_WAKE_CLI`` is exported at spawn time pointing into a versioned
    release directory, and a deploy reaps old releases -- so a long-lived
    worker's export can go DANGLING out from under it (measured live,
    2026-08-16: a worker spawned before a same-day deploy held an
    AGENT_WAKE_CLI naming a release directory that no longer existed). The
    ``is_file()`` guard -- a stat for the FILE, not merely the directory's
    existence -- means a dangling export contributes NOTHING to PATH -- no
    bogus directory gets appended at all -- so a session whose PATH already
    resolves solet-bridge fine is completely unaffected either way; only a session
    that would otherwise fail gains a chance to resolve."""
    cli = os.environ.get("AGENT_WAKE_CLI", "").strip()
    if not cli:
        return dict(os.environ)
    solet_dir = str(Path(cli).parent)
    if not (Path(solet_dir) / "solet-bridge").is_file():
        return dict(os.environ)
    env = dict(os.environ)
    env["PATH"] = f"{env.get('PATH', '')}:{solet_dir}"
    return env


def _solet_call(process_key: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["solet-bridge", "call", process_key, json.dumps(arguments)],
            capture_output=True, text=True, timeout=20, check=False,
            env=_solet_call_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"solet-bridge call {process_key} failed to run: {exc}")
        return None
    if result.returncode != 0:
        _warn(f"solet-bridge call {process_key} exited {result.returncode}: {result.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _warn(f"solet-bridge call {process_key} returned unparseable output: {exc}")
        return None


def _resolve_steward_role(agent_instance_id: str) -> str | None:
    """The managed_session row's ``spawned_by_role``, or ``None`` when the
    row doesn't exist (host=operator, e.g. the seat) or carries no
    steward -- callers fall back to local self-notification, they don't
    treat this as an error."""
    envelope = _solet_call(
        _SESSION_STATUS_PROCESS_KEY, {"agent_instance_id": agent_instance_id},
    )
    if envelope is None or envelope.get("status") != "completed":
        return None
    data = ((envelope.get("result") or {}).get("data")) or {}
    role = str(data.get("spawned_by_role") or "").strip()
    return role or None


def _deliver_notification(*, agent_instance_id: str, claude_session_id: str, content: str, marker_dir: Path) -> bool:
    """Peer-send to the resolved steward when one exists; otherwise write
    a locally-surfaced marker file. Returns True on any successful
    delivery path (peer-send OR marker write) -- the caller only touches
    the latch on a True return, so a fully-failed delivery attempt can
    retry on the next un-throttled tick instead of being silently
    latched-but-never-delivered."""
    steward_role = _resolve_steward_role(agent_instance_id)
    if steward_role is not None:
        envelope = _solet_call(_PEER_SEND_PROCESS_KEY, {"name": steward_role, "content": content})
        if envelope is not None and envelope.get("status") == "completed":
            return True
        _warn(f"peer_send_by_name to steward role {steward_role!r} failed; falling back to local marker")
    marker_path = _fallback_marker_path(marker_dir, agent_instance_id, claude_session_id)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({"content": content, "written_at": time.time()}))
    except OSError as exc:
        _warn(f"failed to write local self-notification marker: {exc}")
        return False
    return True


def _read_stdin_payload() -> dict[str, Any] | None:
    """``None`` means "skip, already warned" -- a parse failure is never
    fatal, same contract as ``capture_session_mapping.py``'s own helper of
    the same shape."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 -- never disrupt a session
        _warn(f"failed to read/parse stdin payload: {exc}")
        return None
    if not isinstance(payload, dict):
        _warn("stdin payload was not a JSON object")
        return None
    return payload


def _fallback_marker_dir() -> str | None:  # pyright: ignore[reportUnusedFunction]
    """A writable stand-in marker root for the managed-but-mis-wired case.

    Deliberately the OS temp dir rather than a project-relative path: this
    hook cannot rely on ``CLAUDE_PROJECT_DIR`` (measured absent from live
    spawned workers' env), and guessing a profile-relative location would
    invent a convention rather than use one. Returns ``None`` if the
    directory cannot be created, which sends the caller back to skipping --
    the one case where this hook still declines to fire.
    """
    path = Path(tempfile.gettempdir()) / "agent_rotation_due_markers"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(f"could not create fallback marker root {path}: {exc} -- skipping")
        return None
    return str(path)


def _ancestor_claude_pid() -> int | None:
    """The pid of the nearest ancestor ``claude`` process, or ``None``.

    A hook runs as a detached subprocess of the session it is observing, so
    walking up the process tree is how it finds the session's own pid — the
    same method the ``rename`` skill uses to locate a seat's session file.
    """
    pid = os.getpid()
    for _ in range(_ANCESTOR_WALK_MAX_DEPTH):
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _warn(f"ancestor walk failed at pid {pid}: {exc}")
            return None
        fields = result.stdout.split()
        if result.returncode != 0 or len(fields) < 2:
            return None
        if Path(fields[-1]).name == _CLAUDE_PROCESS_NAME:
            return pid
        try:
            pid = int(fields[0])
        except ValueError:
            return None
        if pid <= 1:
            return None
    return None


def _registry_row_is_fresh(row: dict[str, Any]) -> bool:
    """True iff the row's ``updated_at`` is within the staleness bound.

    ``updated_at`` is stored UTC-NAIVE (measured 2026-08-16: the value is a UTC
    wall clock carrying no timezone suffix). Comparing it against a naive local
    ``datetime.now()`` is wrong by the ENTIRE UTC offset — 25,200s on this host,
    two orders of magnitude larger than the bound below. That does not degrade
    the check, it REPLACES it, silently, in one of two directions: every age
    goes negative and nothing is ever stale (fails open), or an ``abs()`` makes
    everything stale forever and the seat never reports (fails closed — the
    very gap this fallback exists to close). So both sides are UTC-aware here,
    and a wildly negative age is treated as a clock/timezone fault rather than
    as freshness.
    """
    raw = str(row.get("updated_at") or "")
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        _warn(f"registry row has unparseable updated_at {raw!r} -- skipping")
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - stamp).total_seconds()
    if age_seconds < -_SEAT_REGISTRY_MAX_AGE_SECONDS:
        _warn(
            f"registry row is {-age_seconds:.0f}s in the FUTURE -- clock or "
            "timezone fault, not freshness; skipping rather than trusting it",
        )
        return False
    if age_seconds > _SEAT_REGISTRY_MAX_AGE_SECONDS:
        _warn(
            f"registry row is {age_seconds:.0f}s old (bound "
            f"{_SEAT_REGISTRY_MAX_AGE_SECONDS}s) -- a re-used pid may be "
            "matching a dead session; skipping rather than reporting under it",
        )
        return False
    return True


def _registry_rows_for_pid(pid: int) -> list[dict[str, Any]] | None:
    """Registry rows whose ``parent_pid`` matches, or ``None`` if the lookup
    itself failed (which is different from "no session has that pid", and the
    caller must not conflate them)."""
    envelope = _solet_call(_PEER_LIST_PROCESS_KEY, {})
    if envelope is None:
        return None
    instances = (
        (envelope.get("result") or {}).get("data", {}).get("instances") or {}
    )
    return [
        row
        for rows in instances.values()
        for row in rows
        if row.get("parent_pid") == pid
    ]


def _resolve_seat_instance_id() -> str | None:
    """The operator seat's OWN registered instance id, or ``None`` to skip.

    SEAT ONLY, and the scoping is load-bearing rather than incidental. This is
    called only from the branch where ``AGENT_INSTANCE_ID`` is ABSENT. For a
    watch-transport WORKER the registry returns the ``agi-watch-*`` id, not the
    ledger id, so running this fleet-wide would report context under the wrong
    identity for every such worker. The env var is present for exactly those
    sessions, so the gate and the hazard line up — DELIBERATELY, and the
    negative-control test pins it.

    The id is READ from the registry, never minted, and never cached: resolved
    per tick and used for that tick only.
    """
    pid = _ancestor_claude_pid()
    if pid is None:
        _warn("could not resolve an ancestor claude pid -- skipping")
        return None
    matches = _registry_rows_for_pid(pid)
    if matches is None:
        return None
    # 0/1/N: exactly one, or refuse. Reporting context under the wrong identity
    # is worse than not reporting, so an ambiguous match is never guessed. A
    # host driver that leaves parent_pid unpopulated lands here as 0 matches,
    # and skipping IS the correct behaviour for it.
    if len(matches) != 1:
        _warn(
            f"registry has {len(matches)} rows for ancestor pid {pid}, need "
            "exactly 1 -- skipping",
        )
        return None
    if not _registry_row_is_fresh(matches[0]):
        return None
    instance_id = str(matches[0].get("agent_instance_id") or "").strip()
    if not instance_id:
        _warn(f"registry row for pid {pid} carries no agent_instance_id -- skipping")
        return None
    return instance_id


def _resolve_instance_id() -> str:  # pyright: ignore[reportUnusedFunction]
    """This session's own instance id: env first, seat registry second, else "".

    Kept as its own function so :func:`_resolve_firing_context` carries exactly
    the one branch it always carried -- adding the seat path inline pushed it
    from clean to CC C(11) against the gate's ceiling of 10, and the allowlist
    is tracked debt for pre-existing code, not a bypass for new code.
    """
    env_id = os.environ.get(_INSTANCE_ID_ENV, "").strip()
    if env_id:
        return env_id
    return _identity_without_env() or ""


def _identity_without_env() -> str | None:
    """Identity for a session whose env carries no ``AGENT_INSTANCE_ID``.

    SEAT PATH (2026-08-16). An absent env id used to mean "not fleet-managed;
    skip", which is true for an unmanaged shell and WRONG for the operator
    seat: the seat's identity is resolved dynamically through its live bridge
    and is never baked into its process env, so this hook self-selected out on
    every tick and the seat had no context gauge at all. That is the gap behind
    the 2026-08-16 rotation at 559K against a >300K policy.

    Split out of :func:`_resolve_firing_context` to keep that function's branch
    count under the complexity gate -- and because the seat path deserves to be
    readable on its own rather than as a clause inside an env check.
    """
    resolved = _resolve_seat_instance_id()
    if resolved:
        return resolved
    _warn(
        f"{_INSTANCE_ID_ENV} not set and no registry row resolved -- not a "
        "fleet-managed spawn, or identity is unavailable; skipping",
    )
    return None


def _resolve_firing_context(identity_dir: Path) -> tuple[Path, str, str, str] | None:
    """``(marker_dir, agent_instance_id, transcript_path, claude_session_id)``,
    or ``None`` when this firing should be skipped (missing env, unreadable
    stdin, or a payload missing the fields this hook needs) -- split out of
    :func:`main` to keep it a straight-line dispatcher (radon cc)."""
    agent_instance_id = os.environ.get(_INSTANCE_ID_ENV, "").strip()
    if not agent_instance_id:
        return None
    payload = _read_stdin_payload()
    if payload is None:
        return None
    transcript_path = str(payload.get("transcript_path") or "")
    claude_session_id = str(payload.get("session_id") or "")
    if not transcript_path or not claude_session_id:
        _warn("stdin payload carried no transcript_path/session_id -- skipping")
        return None
    return identity_dir, agent_instance_id, transcript_path, claude_session_id


def _cache_arguments(
    transcript_path: str, thresholds: Any,
) -> dict[str, Any]:
    """The three optional cache fields, or ``{}`` when they cannot be measured.

    ``{}`` is deliberate and load-bearing: OMITTING the fields records NOT
    REPORTED, which the read-back verb surfaces as ``null``. Sending
    ``cache_cold=False`` here instead would assert a warm cache this hook
    never actually classified.
    """
    calls = read_assistant_calls(transcript_path)
    if not calls:
        return {}
    try:
        parsed = [
            thresholds.AssistantCall(
                at=thresholds.parse_transcript_timestamp(c["at"]),
                cache_read_tokens=c["cache_read"],
            )
            for c in calls
        ]
    except (ValueError, KeyError) as exc:
        _warn(f"could not parse transcript timestamps for cache state: {exc}")
        return {}
    state = thresholds.classify_cache_state(
        parsed, datetime.now(UTC), cleared_at=None,
    )
    return {
        "cache_read_tokens": parsed[-1].cache_read_tokens,
        "cache_cold": state.cold,
        "cache_overage_signature": state.overage_signature,
    }


def _session_id_argument() -> dict[str, Any]:
    """The stable session id, or ``{}`` when this session has none.

    ``{}`` IS THE POINT, exactly as in :func:`_cache_arguments`: omitting the
    key records NOT REPORTED, which the column stores as NULL. Sending ``""``
    instead would assert an empty session id -- a value the registry resolves
    to "no match", making a reporter that never had one indistinguishable from
    a session that genuinely could not be routed.

    This value is read from the environment and passed through UNMODIFIED. It
    is never constructed from ``$AGENT_INSTANCE_ID``, even though the launcher
    currently derives one from the other: that is the launcher's convention,
    and reconstructing it here would put a copy of that convention in a second
    place that has no way to learn when it changes.
    """
    agent_session_id = os.environ.get(_SESSION_ID_ENV, "").strip()
    return {"agent_session_id": agent_session_id} if agent_session_id else {}


def _reporter_arguments() -> dict[str, Any]:
    """Which COPY of this hook is speaking, on the two axes a reader needs.

    Several copies of this file can be registered on the same event at once
    (see the three-copy note in the module docstring), they serialize on a
    shared throttle marker that records nothing about who claimed it, and the
    stored row keeps only the latest write. So a row is unattributable unless
    the reporter says who it is, and an absent cache field cannot otherwise be
    told apart from a STALE copy having served that tick.

    SURFACE is derived from this file's own resolved location, as a path
    CLASS -- never the absolute path, which would write one machine's layout
    into shared state. The VENDORED source copy classifies as ``unknown``
    rather than ``plugin_cache``: it is the thing an install copies FROM, not
    a cache copy, and it is only ever executed directly in one measured
    headless configuration. Calling that ``unknown`` is accurate; calling it
    ``plugin_cache`` would be a guess wearing a specific label.

    GENERATION is a local constant, deliberately NOT a git sha -- this file
    cannot know the commit it was copied from, and deriving one would promise
    precision the reporter does not have.
    """
    try:
        parts = Path(__file__).resolve().parts
    except OSError:
        return {"reporter_surface": "unknown", "reporter_generation": _REPORTER_GENERATION}
    return {
        "reporter_surface": _classify_surface(parts),
        "reporter_generation": _REPORTER_GENERATION,
    }


def _runtime_identity_arguments(claude_session_id: str) -> dict[str, str]:
    """The runtime-neutral identity this Claude Code hook can state truthfully.

    ``claude_session_id`` is the native runtime-session id from this hook's
    stdin payload. Provider and runtime are fixed by this Claude Code-only
    hook. A missing ``CLAUDE_EFFORT`` is represented explicitly as ``unknown``:
    the hook must not darken its gauge merely because this optional launcher
    input was absent.
    """
    effort = os.environ.get("CLAUDE_EFFORT", "").strip()
    if not effort:
        global _missing_effort_warned
        if not _missing_effort_warned:
            _warn(
                "CLAUDE_EFFORT is unset; reporting effort='unknown' as a "
                "degraded identity rather than withholding the gauge",
            )
            _missing_effort_warned = True
        effort = "unknown"
    return {
        "runtime_session_id": claude_session_id,
        "provider": "anthropic",
        "runtime": "claude_code",
        "effort": effort,
    }


def _has_consecutive(parts: tuple[str, ...], *names: str) -> bool:
    """True when ``names`` appear as CONSECUTIVE path components."""
    width = len(names)
    return any(parts[i : i + width] == names for i in range(len(parts) - width + 1))


def _classify_surface(parts: tuple[str, ...]) -> str:
    """Path CLASS for this copy of the hook. ORDER IS LOAD-BEARING.

    The layouts NEST, so the tests must run most-specific-first:
    a deployed release tree CONTAINS a ``coordination-hooks/hooks/``
    directory, so testing ``vendored`` before ``release`` would label every
    release copy as vendored and silently merge two distinct surfaces --
    which is the same collapsing failure this whole change exists to undo.
    The smoke asserts the ordering by swapping these two tests; if that
    mutation does not go red, the order is not actually being tested.

    Release detection is STRUCTURAL (a ``releases`` component followed by a
    ``rel-*`` component) rather than an absolute prefix. Hardcoding one
    machine's release root into a shipped hook would be the same mistake as
    writing an absolute path into the stored row.

    ``checkout`` matches ``.claude/hooks`` ANYWHERE in the path, not only at
    the tail: hooks live in SUBDIRECTORIES too (``memory_passthrough/``), and
    the original tail-anchored test reported those as ``unknown`` -- a false
    unrecognised-surface for an entirely ordinary checkout file, and one of
    the three things the collapsed bucket was hiding.
    """
    if _has_consecutive(parts, ".claude", "plugins", "cache"):
        return "plugin_cache"
    for index, name in enumerate(parts):
        if name == "releases" and any(p.startswith("rel-") for p in parts[index + 1 :]):
            return "release"
    if _has_consecutive(parts, "coordination-hooks", "hooks"):
        return "vendored"
    if _has_consecutive(parts, ".claude", "hooks"):
        return "checkout"
    return "unknown"


def _report_context_status(
    *, agent_instance_id: str, claude_session_id: str, model: str,
    current_tokens: int, ceiling: int, cache_arguments: dict[str, Any],
    reading_at: str | None,
) -> bool:
    """Best-effort cache write for ``session_context_status`` (shape (a)) --
    non-fatal by this hook's own standing contract: a failed report here
    must never cost the notify path below, so failures warn to stderr.

    ★ RETURNS WHETHER THE SOLET ANSWERED (GAU-14 D2, 2026-08-19). The caller
    still does not branch on this for the REPORT -- that stays best-effort and
    unconditional. It branches on it for the NOTIFY, and this is the one place
    in the tick that already knows the answer. Deriving solet liveness from the
    call this tick has ALREADY made costs nothing; asking separately would add
    a second round trip to the hot path and a second chance to disagree with
    itself about the same fact.

    False means "the solet did not complete this call", which is exactly the
    condition under which the solet-side legs cannot be covering anything.
    """
    identity = _runtime_identity_arguments(claude_session_id)
    envelope = _solet_call(
        _REPORT_CONTEXT_STATUS_PROCESS_KEY,
        {
            "agent_instance_id": agent_instance_id,
            **identity,
            "model": model,
            "current_tokens": current_tokens,
            "ceiling": ceiling,
            # measured_at is THIS PROCESS'S clock -- when the hook looked.
            # reading_at is when the number it carries was produced. Sending
            # only the first is what made two notices about one monotone
            # series read as later-but-lower (GAU-14 D3). Omitted entirely
            # when the transcript line carried no timestamp, so the row
            # records NOT REPORTED rather than a fabricated zero lag.
            "measured_at": datetime.now(UTC).isoformat(),
            **({"reading_at": reading_at} if reading_at else {}),
            **cache_arguments,
            **_session_id_argument(),
            **_reporter_arguments(),
        },
    )
    if envelope is None or envelope.get("status") != "completed":
        _warn(f"report_context_status did not complete cleanly: {json.dumps(envelope)[:300]}")
        return False
    return True


def _resolve_usage(transcript_path: str) -> tuple[str, int, str | None, Any] | None:
    """``(model, current_tokens, reading_at, rotation_thresholds module)``, or
    ``None`` when there is no usage-bearing assistant line yet or the module
    import fails -- shared by the (unconditional, every-tick) cache report and
    the (latch-gated, once-per-generation) notify path so each tick reads the
    transcript file exactly once, not twice.

    ``reading_at`` rides the SAME read for the same reason the token sum does:
    it is a property of the one line this tick already parsed, and re-deriving
    it would be both a second file read and a second chance to disagree with
    itself about which line the reading came from.
    """
    found = find_last_assistant_usage(transcript_path)
    if found is None:
        return None
    model, usage, reading_at = found
    rotation_thresholds = _import_rotation_thresholds()
    if rotation_thresholds is None:
        return None
    return model, sum_context_tokens(usage), reading_at, rotation_thresholds


def _check_and_notify(
    *, marker_dir: Path, agent_instance_id: str, claude_session_id: str,
    latch_path: Path, model: str, current_tokens: int, cache_cold: Any,
    rotation_thresholds: Any, solet_live: bool,
) -> None:
    """The threshold-and-notify half of a firing (post throttle/latch
    gating) -- split out of :func:`main` to keep it a straight-line
    dispatcher (radon cc). Takes the ALREADY-resolved usage tuple (see
    :func:`_resolve_usage`) rather than a transcript path -- this is the
    LATCH-GATED half (``main`` never calls it once the latch exists for this
    session generation), so it must not be where the cache report lives;
    that runs unconditionally in :func:`main` before the latch check.

    ``cache_cold`` is the tri-state ``True``/``False``/``None`` that
    :func:`_cache_arguments` already measured for the unconditional cache
    report, threaded through rather than re-derived: it is the SECOND input to
    the rotation-due decision since GAU-08 (a cold cache above H is due at a
    size a warm one is not), and reading the transcript twice per tick to
    recover a value this tick already has would be both slower and a second
    chance to disagree with itself. ``None`` means NOT MEASURED and is treated
    as warm by the predicate -- never promoted to cold.
    """
    verdict = rotation_thresholds.rotation_due_verdict(
        ceiling=rotation_thresholds.resolve_ceiling(model),
        current_tokens=current_tokens,
        cache_cold=cache_cold,
    )
    if not verdict.due:
        return

    # ★ GAU-14 (D2), 2026-08-19 -- DEDUPE ACROSS DELIVERY PATHS, AT THE ONLY
    # LEG THAT CAN SEE BOTH.
    #
    # ONE due condition was reaching a steward up to THREE times inside six
    # minutes, and a seat twice, because four independent legs latch correctly
    # against four separate stores and none can observe any other: this hook's
    # latch file, `rotation_due_notice.py`'s surfaced_at stamp, and -- solet
    # side -- `sweep_rotation_due_sessions`'s NoticeLatch and
    # `sweep_rotation_self_notice`'s BandEdgeLatch, both in that process's RAM.
    # No shared key exists between them and adding one would be a read-then-
    # write across two processes, i.e. a write race, for a fact that does not
    # need to be shared at all.
    #
    # It does not need to be shared because the legs are not peers. They were
    # built for disjoint recipients and overlap only by accident:
    #   * managed lane -- the solet's steward leg notifies the steward, and its
    #     self-notice leg notifies the session. This hook's steward peer-send is
    #     a THIRD copy of the first one.
    #   * operator-present seat -- the solet's self-notice leg appends to the
    #     seat's own bridge, which for a bridge-held seat genuinely surfaces.
    #     This hook's local marker (surfaced later by `rotation_due_notice.py`)
    #     is a SECOND copy of that.
    # In both topologies this hook is the redundant one WHENEVER THE SOLET IS
    # UP -- so it stands down, and only then.
    #
    # ★ THE FAIL DIRECTION IS THE WHOLE DESIGN. `solet_live` is False whenever
    # this tick's own `report_context_status` call did not complete. A solet
    # that cannot answer a call cannot be running the legs this hook is
    # deferring to, so the guard fires the notice rather than suppressing it:
    # the failure mode is a DUPLICATE notice, never a lost one. Every uncertain
    # case resolves the same way, because the guard's input is a positive
    # answer, not the absence of a negative.
    #
    # ★ IMMUNE TO THE LANDED-NOT-LIVE ASYMMETRY, which is why the input is this
    # one. The solet-side legs go live only at a deploy, so between this
    # landing and that deploy the DEPLOYED legs are what actually run -- both
    # of them do: the deployed rider's own per-tick line shows `L4a=` and
    # `L4c=` on every tick (measured 2026-08-19 against pid 8929). A guard
    # keyed on some NEW thing the solet legs would write could not say that;
    # this one asks only whether the solet answered THIS hook, which is true of
    # every solet version that has ever served this call.
    #
    # ★ NO LATCH ON THIS PATH. Standing down must not consume the
    # one-notice-per-generation latch: if the solet dies later in this
    # session's life, the next un-throttled tick must be free to fire. Only the
    # deferral NOTE is latched, and only so it is said once.
    #
    # ★ AND IT DEFERS ONLY WHERE THE SOLET LEG ACTUALLY SPEAKS -- the condition
    # that "the solet is up" does NOT by itself establish. MEASURED, and it
    # cost this guard a real notice class before the check was added: the
    # self-notice leg fires only for bands in its own notify set, while THIS
    # hook fires on the full GAU-08 union (band actionable OR fraction
    # crossed). Sweeping 3 models x every size x both cache states found 59
    # sizes where this hook is due and the self-notice leg is SILENT -- all of
    # them fraction-crossed-with-a-quiet-band on a small ceiling
    # (claude-haiku-4-5 at 200,000 from 100,000 up; the conservative-ceiling
    # unknown model likewise), which is exactly the GAU-12 population. The
    # steward leg does use the full union, so a managed lane's STEWARD is still
    # told in that class -- but an operator-present seat has no steward, so
    # standing down there would have lost the notice outright.
    #
    # `band_actionable` is the safe discriminator and it was verified, not
    # assumed: every actionable band produced across that same sweep
    # (warm_task_boundary, warm_safe_checkpoint, warm_immediate, cold_above_h)
    # is inside the self-notice leg's notify set, so an actionable band is
    # exactly the condition on which that leg is guaranteed to speak. A
    # fraction-only crossing is not, and this hook keeps it.
    if solet_live and verdict.band_actionable:
        deferred_path = _deferred_marker_path(
            marker_dir, agent_instance_id, claude_session_id,
        )
        if not deferred_path.exists():
            _warn(
                f"rotation-due for {agent_instance_id} at {current_tokens} tokens "
                "(band "
                f"{verdict.band}) -- STANDING DOWN, not suppressing: the solet "
                "answered this tick, so its steward leg and its self-notice leg "
                "are covering this same condition. This hook fires only if the "
                "solet stops answering. Said once per session generation.",
            )
            touch_marker(deferred_path)
        return

    content = build_notification_content(
        agent_instance_id=agent_instance_id,
        session_label=os.environ.get(_SESSION_LABEL_ENV, "").strip(),
        model=model,
        current_tokens=current_tokens,
        ceiling=rotation_thresholds.resolve_ceiling(model),
        band=verdict.band,
        band_actionable=verdict.band_actionable,
        threshold_fraction=rotation_thresholds.ROTATION_THRESHOLD_FRACTION,
        fraction_crossed=verdict.fraction_crossed,
    )
    delivered = _deliver_notification(
        agent_instance_id=agent_instance_id, claude_session_id=claude_session_id,
        content=content, marker_dir=marker_dir,
    )
    if delivered:
        touch_marker(latch_path)


def _verified_identity_directory() -> Path | None:
    ownership = coordination_owner.verify("context_watch", __file__)
    if not ownership.eligible:
        if ownership.managed:
            coordination_owner.report_refusal(ownership)
        return None
    identity_dir = coordination_owner.runtime_identity_directory(ownership)
    if identity_dir is None:
        _warn("verified context owner has no receipt-derived runtime state root")
    return identity_dir


def main() -> int:
    identity_dir = _verified_identity_directory()
    if identity_dir is None:
        return 0
    if not _is_designated_gauge_reporter():
        return 0
    context = _resolve_firing_context(identity_dir)
    if context is None:
        return 0
    marker_dir, agent_instance_id, transcript_path, claude_session_id = context

    throttle_path = _throttle_marker_path(marker_dir, agent_instance_id)
    if is_throttled(throttle_path, now=time.time()):
        return 0

    resolved = _resolve_usage(transcript_path)
    if resolved is None:
        return 0
    model, current_tokens, reading_at, rotation_thresholds = resolved

    # Cache report rides EVERY un-throttled tick, UNCONDITIONALLY -- deliberately
    # ahead of the latch check below, which only gates the once-per-generation
    # notify. session_context_status must answer for a session nowhere near
    # rotation-due too, and must keep refreshing after the one-time notify has
    # already latched for this session generation.
    # Hoisted to a local so the SAME measurement serves the report and the
    # rotation-due decision below. Measured once per tick: `_cache_arguments`
    # re-reads and re-parses the whole transcript, and two reads could not only
    # cost twice, they could disagree if a call landed between them.
    cache_arguments = _cache_arguments(transcript_path, rotation_thresholds)
    solet_live = _report_context_status(
        agent_instance_id=agent_instance_id, claude_session_id=claude_session_id,
        model=model, current_tokens=current_tokens,
        ceiling=rotation_thresholds.resolve_ceiling(model),
        cache_arguments=cache_arguments,
        reading_at=reading_at,
    )
    if solet_live:
        try:
            touch_marker(throttle_path)
        except OSError as exc:
            _warn(f"failed to write context throttle marker: {exc}")

    latch_path = _latch_marker_path(marker_dir, agent_instance_id, claude_session_id)
    if latch_path.exists():
        return 0

    _check_and_notify(
        marker_dir=marker_dir, agent_instance_id=agent_instance_id,
        claude_session_id=claude_session_id, latch_path=latch_path,
        model=model, current_tokens=current_tokens,
        # `.get` returns None when the cache state could not be measured at all
        # -- `_cache_arguments` returns {} in that case precisely so the absence
        # stays visible instead of arriving as a fabricated False.
        cache_cold=cache_arguments.get("cache_cold"),
        rotation_thresholds=rotation_thresholds,
        # GAU-14 (D2): the same call that wrote the gauge row already told us
        # whether the solet is answering. Threaded, never re-asked.
        solet_live=solet_live,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
