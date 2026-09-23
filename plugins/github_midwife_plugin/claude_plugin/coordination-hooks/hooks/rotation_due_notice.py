#!/usr/bin/env python3
"""L4b -- the DELIVERY half of the rotation-due signal for a session that no
sweep can reach: a ``UserPromptSubmit`` hook that surfaces the self-notify
marker ``rotation_due_watch.py`` already writes, as context on the session's
own next turn.

WHY THIS EXISTS AT ALL, given that the sweep already notices
--------------------------------------------------------------------------
Detection and delivery are different problems and they failed in different
places. ``session_sweep.sweep_rotation_due_sessions`` detects perfectly well
and delivers to a STEWARD's bridge -- which works for a managed worker and
reaches an operator-present seat not at all: ``append_event`` is read when the
recipient session next takes a turn, and ``drive_on_delivery`` no-ops for
exactly this case (a session with no ``managed_session`` row, driven by the
degenerate ``operator`` host driver). The seat is both. So the notice fires
into a void for the one session whose rotation decision is the expensive one.

``rotation_due_watch.py`` already anticipated this: when it cannot resolve a
steward role (host=operator, i.e. the seat), it writes the notice to a local
marker file instead of dropping it, and its own docstring names consuming that
marker as a follow-on that was never built. THIS IS THAT FOLLOW-ON. It is
deliberately not a second detector and not a second notification channel: a
second channel for the same fact would be worse than none, because two sources
that can disagree teach the reader to trust neither.

THE BOUNDED CLAIM, which must not be inflated anywhere
--------------------------------------------------------------------------
The call carrying this warning IS ITSELF THE FULL-PRICE CALL. Nothing inside a
session can run before the session runs. So this converts an OPEN-ENDED SERIES
of full-price carries into EXACTLY ONE. It does not make the first one free,
and any framing stronger than that is wrong. "The sweep is the sole pre-cost
surface" is right about DETECTION and needs this qualification about DELIVERY.

Design constraints, each carried rather than assumed away
--------------------------------------------------------------------------
* CHEAP AND SILENT ON THE EMPTY PATH. This fires on EVERY prompt, so anything
  it does when there is nothing to say is paid constantly to deliver a rare
  message. The empty path here is: read stdin, then at most two directory
  globs. NO subprocess, no ``solet`` call, no identity resolution, no import
  beyond the stdlib. In particular it does NOT resolve this session's
  ``agent_instance_id`` -- the marker filename carries the Claude session id,
  which stdin already hands us, so the id we would have paid a registry
  round-trip for is not needed to find the file.
* LATCH. A notice that repeats every prompt trains its reader to ignore it,
  and an ignored warning is worse than none. Surfacing STAMPS the marker
  (``surfaced_at``) and a stamped marker is never surfaced again.
* ONE MARKER, STAMPED IN PLACE -- not a second "already surfaced" file beside
  it. Two markers for one fact converts a read race into a write race, and the
  pair can disagree about what happened; the record and its disposition belong
  in the same file.
* A FAILED STAMP STILL SURFACES. If the stamp cannot be written the notice is
  still delivered (and warns to stderr), because a repeated warning is a
  smaller failure than a swallowed one. That trade is stated here rather than
  discovered later.
* NO BAND IS RE-DERIVED HERE. The sibling sweep must flag an unattributable
  reporter, because it reads a STORED row whose band may be a warm DEFAULT
  rather than a measurement. This hook has no such exposure: the marker's text
  was written by the watcher from ITS OWN live reading of this session's
  transcript and carries the measured token count, model and ceiling with no
  band at all. Applying the attribution caveat here anyway would be ceremony,
  not care -- so it is deliberately absent, and this paragraph is why.

Non-fatal by design, same contract as this checkout's other hooks: every
failure path warns to stderr and exits 0. A hook must never cost a session its
turn -- least of all a hook whose entire job is to say "you may want to rotate".

TWO COPIES OF THIS FILE EXIST AND THEY ARE BYTE-IDENTICAL
--------------------------------------------------------------------------
``.claude/hooks/rotation_due_notice.py`` (checkout) and
``plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/``
``rotation_due_notice.py`` (vendored, packaged into the plugin an adopter
installs). Unlike ``rotation_due_watch.py`` -- whose two copies diverge in
``_resolve_plugin_src_path``/``_import_rotation_thresholds`` because it must
locate the repo's ``rotation_thresholds`` module and the vendored copy has no
fixed parent depth to fall back on -- this hook resolves NOTHING from the repo
tree. It reads stdin, an environment variable, and files. So the copies have
no reason to differ, and "identical" is the invariant to check on any edit,
not a 26-line adaptation window. Edit both in the same landing regardless.

And the third copy caveat that applies to every hook here: the INSTALLED copy
under ``~/.claude/plugins/cache/...`` does not follow from a commit. Only an
explicit reinstall moves the pinned entry. A version bump without a reinstall
is the expected silent failure.
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
import tempfile
import time
from pathlib import Path
from typing import Any

_MARKER_DIR_ENV = "AGENT_HEARTBEAT_MARKER_DIR"
_MARKER_SUFFIX = ".rotation_due_selfnotify.json"
# The stamp that makes this once-per-marker. Written INTO the marker rather
# than beside it -- see the module docstring on why a second file is worse.
_SURFACED_AT_KEY = "surfaced_at"
_FALLBACK_MARKER_DIRNAME = "agent_rotation_due_markers"


def _warn(message: str) -> None:
    try:
        print(f"[rotation-due-notice] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 -- telemetry strictly best-effort
        pass


def _read_stdin_payload() -> dict[str, Any] | None:
    """``None`` means "skip, already warned" -- same non-fatal shape as the
    sibling hooks' helper of this name."""
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


def candidate_marker_dirs() -> list[Path]:
    """Every directory the WRITER may have used, in the writer's own order.

    Two, because ``rotation_due_watch.py`` has two: the declared
    ``AGENT_HEARTBEAT_MARKER_DIR``, and -- for a MANAGED session whose env was
    frozen at spawn before that variable was renamed -- a temp-dir fallback it
    falls back to precisely so throttle and latch keep working. A reader that
    knows only about the declared directory silently misses every notice
    written by a session in that state, which is the population most likely to
    be mis-wired in the first place.

    Non-existent directories are kept in the list rather than filtered here:
    the glob below tolerates them, and one ``stat`` per prompt to pre-filter
    buys nothing on the path that matters.
    """
    dirs: list[Path] = []
    declared = os.environ.get(_MARKER_DIR_ENV, "").strip()
    if declared:
        dirs.append(Path(declared))
    dirs.append(Path(tempfile.gettempdir()) / _FALLBACK_MARKER_DIRNAME)
    return dirs


def find_markers(dirs: list[Path], claude_session_id: str) -> list[Path]:
    """Self-notify markers belonging to THIS session generation.

    Matched on the Claude session id alone (the filename is
    ``<agent_instance_id>__<claude_session_id>.rotation_due_selfnotify.json``)
    because stdin already carries that id, while the instance id would cost a
    registry round-trip on every prompt to learn something the filename is
    already keyed on.

    Keying on the session generation is also what makes a ``/clear`` behave
    correctly with no reset logic: the new generation has a different id, so a
    marker from the previous one is simply not this session's to surface.
    """
    found: list[Path] = []
    for directory in dirs:
        try:
            found.extend(sorted(directory.glob(f"*__{claude_session_id}{_MARKER_SUFFIX}")))
        except OSError as exc:  # noqa: PERF203 -- a bad dir must not skip the others
            _warn(f"could not scan marker dir {directory}: {exc}")
    return found


def provenance_ok(st_uid: int, st_mode: int, *, expected_uid: int | None) -> bool:
    """Whether a marker's OWNERSHIP AND PERMISSIONS make it safe to inject.

    This hook is the one file in this plugin whose output lands INSIDE a
    prompt, so the provenance of what it reads is a security property, not
    hygiene. The fallback marker root lives under the system temp directory,
    which on a shared Linux host is world-writable: without this check, any
    local user who can create a file there could put arbitrary text into
    another user's next prompt.

    Two conditions, both cheap and both on the non-empty path only:

    * OWNED BY US. A marker written by a different uid is not ours to trust.
      ``expected_uid`` is ``None`` on a platform with no ``geteuid`` (Windows),
      where this check cannot be made -- and there it is SKIPPED rather than
      faked, because a check that cannot run must not report that it ran.
    * NOT GROUP- OR WORLD-WRITABLE. Correct ownership today does not help if
      anyone may rewrite the contents tomorrow.

    This is a bound on what this hook will INJECT. It is deliberately not a
    claim that the marker directory is secure -- see SECURITY.md.
    """
    if expected_uid is not None and st_uid != expected_uid:
        return False
    return not st_mode & 0o022


def _current_uid() -> int | None:
    """This process's effective uid, or ``None`` where the platform has none.

    ``hasattr`` rather than ``getattr(...)`` + call: the guard exists for a
    platform without ``geteuid`` (Windows), and this spelling keeps the call
    site statically typed as the ``int`` it is everywhere the attribute exists,
    instead of an ``object`` the checker cannot narrow.
    """
    if not hasattr(os, "geteuid"):
        return None
    return os.geteuid()


def read_unsurfaced(marker_path: Path) -> dict[str, Any] | None:
    """The marker's record if it is still to be surfaced, else ``None``.

    ``None`` covers three distinct skips that all mean "say nothing": already
    surfaced (the latch), unreadable, or malformed. An unreadable marker is
    NOT re-raised and NOT surfaced blind -- injecting a half-parsed file into
    the prompt would be worse than the silence.
    """
    try:
        info = marker_path.stat()
        if not provenance_ok(info.st_uid, info.st_mode, expected_uid=_current_uid()):
            _warn(
                f"refusing to surface {marker_path}: it is not owned by this user "
                "or is group/world-writable, and its content would be injected "
                "into a prompt",
            )
            return None
        record = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"could not read marker {marker_path}: {exc}")
        return None
    if not isinstance(record, dict):
        _warn(f"marker {marker_path} did not contain a JSON object")
        return None
    if record.get(_SURFACED_AT_KEY):
        return None
    if not str(record.get("content") or "").strip():
        _warn(f"marker {marker_path} carried no content -- nothing to surface")
        return None
    return record


def stamp_surfaced(marker_path: Path, record: dict[str, Any], *, now: float) -> None:
    """Latch this marker by writing ``surfaced_at`` back into it.

    Best-effort ON PURPOSE: the caller surfaces FIRST and stamps after, so a
    failure here costs a repeated notice rather than a lost one. Warned, never
    raised.
    """
    stamped = dict(record)
    stamped[_SURFACED_AT_KEY] = now
    try:
        marker_path.write_text(json.dumps(stamped), encoding="utf-8")
    except OSError as exc:
        _warn(
            f"could not stamp {marker_path} as surfaced: {exc} -- the notice was "
            "delivered and will repeat next prompt",
        )


def _age_phrase(record: dict[str, Any], *, now: float) -> str:
    """How old the MEASUREMENT is, or an explicit admission that it is unknown.

    The reader is being asked to make a spend decision, and "measured 4 minutes
    ago" and "measured two hours ago" support different decisions. An absent or
    unparseable timestamp says so rather than quietly presenting a stale number
    as current.
    """
    written_at = record.get("written_at")
    if not isinstance(written_at, (int, float)) or isinstance(written_at, bool):
        return "measured at an unrecorded time"
    minutes = max(0.0, (now - float(written_at)) / 60.0)
    return f"measured {minutes:.0f} minute(s) ago"


def build_context(record: dict[str, Any], *, now: float) -> str:
    """The text injected into the prompt.

    Carries the watcher's own measured content verbatim -- never a re-worded
    summary of it, which would put a second, subtly different account of the
    same numbers into circulation.
    """
    return (
        "ROTATION-DUE NOTICE (surfaced once per session generation, from this "
        "session's own context measurement; "
        f"{_age_phrase(record, now=now)}):\n"
        f"{str(record['content']).strip()}\n"
        "This notice reaches you here because the fleet sweep's delivery path "
        "cannot reach an operator-present session: its notices land on a "
        "steward's bridge, and this session has no steward. Note what this "
        "does and does not buy -- the call carrying this warning is itself a "
        "full-price call, so surfacing it converts an open-ended series of "
        "full-price carries into exactly one. It does not make this one free. "
        "Nothing has been cleared or rotated; the timing is your decision."
    )


def emit(context: str) -> None:
    """The ``UserPromptSubmit`` contract: ``additionalContext`` on stdout is
    injected into the prompt BEFORE the model call, which is the only surface
    that reaches an operator-present session at its decision point rather than
    after it.

    EXACTLY ONE JSON object is written, however many markers were found. Two
    printed objects are not two notices -- they are one unparseable stdout, and
    the failure would be silent in the direction that matters (the reader sees
    nothing while the markers get stamped as delivered).
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                },
            },
        ),
    )


def main() -> int:
    payload = _read_stdin_payload()
    if payload is None:
        return 0
    claude_session_id = str(payload.get("session_id") or "").strip()
    if not claude_session_id:
        return 0
    markers = find_markers(candidate_marker_dirs(), claude_session_id)
    if not markers:
        return 0
    now = time.time()
    surfacing: list[tuple[Path, dict[str, Any]]] = []
    for marker_path in markers:
        record = read_unsurfaced(marker_path)
        if record is not None:
            surfacing.append((marker_path, record))
    if not surfacing:
        return 0
    emit("\n\n".join(build_context(record, now=now) for _, record in surfacing))
    # Stamp only AFTER the notice is on stdout: a crash between the two costs a
    # repeat, while stamping first would cost the delivery outright.
    for marker_path, record in surfacing:
        stamp_surfaced(marker_path, record, now=now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
