#!/usr/bin/env python3
"""T1 usage-capture lane (2026-08-05, the 2026-08-05 T1 usage-capture
ruling) — SessionStart hook, self-declaration capture.

Fires on every worker SessionStart (startup/clear/resume/compact) and
writes one file-per-firing JSON to the spool dir the spawning adapter
declared via ``ANANTA_SESSION_MAPPING_SPOOL_DIR`` -- the mapping between
this worker's ``AGENT_INSTANCE_ID`` (already exported by both host
adapters, headless and tmux) and the Claude Code ``session_id`` this
firing carries on stdin. Re-fires on every new session -- the ONE-TO-MANY
rotation story (a /clear or /resume rotates the Claude session UUID
without touching the managed_session row) is handled for free by
re-firing, not by a spawn-time-only capture.

The spool path is DECLARED by the adapter, never derived here (ruling
Q1(a)) -- this hook stays dumb and host-agnostic, no APP_HOME logic.

Non-fatal by design (ruling Q1(e)): a missing env var, an unparseable
stdin payload, or an unwritable spool dir warns on stderr and exits 0 -- a
broken capture hook must never cost a worker its session start. The
honest cost is silent capture loss; detection (managed sessions with zero
mapping rows past a grace window) is a named S2/reconcile follow-up, not
silently omitted here.

Stdlib-only -- fires outside the venv, mirrors this repo's other hooks
(.claude/hooks/headless_tool_allowlist_gate.py,
.claude/hooks/memory_passthrough/*.py).

This is a spawn-injected worker hook: a spawned headless/tmux worker's own
host adapter (`agent_messaging_plugin`) ALSO references this file by path in
a generated Claude Code `--settings` blob at spawn time, independent of the
`hooks.json` registration below. Both routes are deliberate, not redundant
by accident (seed feedback #40/S51.1, 2026-08-24): the adapter's own
`--settings` injection is what a managed policy's
`strictPluginOnlyCustomization: ["hooks"]` strips, silently, on every host
that carries that policy; the `hooks.json` registration is what survives it,
because a plugin already listed in the operator's `strictKnownMarketplaces`
keeps firing its own manifest-registered hooks under that exact policy
(proven empirically for this plugin's other hooks — see §43.1/#8's Case C).
A session under a policy that does NOT strip `--settings` gets this hook
fired from both routes for one SessionStart event — harmless by design: the
handler is a pure function of its two env vars and stdin payload, so a
second firing with the same inputs re-derives the same spool record (a
`captured_at` a few milliseconds apart is the only difference), and the
platform side already upserts per file rather than assuming one-write.
It ships here as the fallback copy a born clone (no `.claude/hooks/` at all)
still carries; the origin checkout's own
`.claude/hooks/capture_session_mapping.py` is the primary copy and this file
must stay behaviorally byte-identical to it.
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
from datetime import UTC, datetime
from pathlib import Path

_SPOOL_DIR_ENV = "ANANTA_SESSION_MAPPING_SPOOL_DIR"
_INSTANCE_ID_ENV = "AGENT_INSTANCE_ID"


def _warn(message: str) -> None:
    try:
        print(f"[capture-session-mapping] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 -- telemetry strictly best-effort
        pass


def _read_stdin_payload() -> dict[str, object] | None:
    """``None`` means "skip capture, already warned" -- a parse failure is
    never fatal (module docstring)."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 -- never disrupt a session
        _warn(f"failed to read/parse stdin payload: {exc}")
        return None
    if not isinstance(payload, dict):
        _warn(f"stdin payload was not a JSON object (got {type(payload).__name__})")
        return None
    return payload


def _build_record(payload: dict[str, object], *, agent_instance_id: str) -> dict[str, str] | None:
    """``None`` means "no session_id in the payload, skip capture"."""
    claude_session_id = str(payload.get("session_id") or "")
    if not claude_session_id:
        _warn("stdin payload carried no session_id -- skipping capture")
        return None
    raw_source = str(payload.get("source") or "unknown")
    return {
        "agent_instance_id": agent_instance_id,
        "claude_session_id": claude_session_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "capture_source": f"hook:{raw_source}",
    }


def _write_spool_file(spool_dir: str, record: dict[str, str]) -> None:
    """File-per-firing (ruling Q1(b)) -- lock-free ingestion: the platform
    side lists the dir, upserts a row per file, and deletes each file only
    after its durable write. Raises ``OSError`` on failure; the caller
    decides how to report it (non-fatal by design)."""
    spool_path = Path(spool_dir)
    spool_path.mkdir(parents=True, exist_ok=True)
    file_name = (
        f"{record['captured_at']}__{record['agent_instance_id']}"
        f"__{record['claude_session_id']}.json"
    )
    (spool_path / file_name).write_text(json.dumps(record))


def main() -> int:
    spool_dir = os.environ.get(_SPOOL_DIR_ENV, "").strip()
    agent_instance_id = os.environ.get(_INSTANCE_ID_ENV, "").strip()
    if not spool_dir or not agent_instance_id:
        _warn(
            f"{_SPOOL_DIR_ENV} or {_INSTANCE_ID_ENV} not set -- not a "
            "fleet-managed spawn, or the adapter didn't wire it; skipping capture",
        )
        return 0

    payload = _read_stdin_payload()
    if payload is None:
        return 0

    record = _build_record(payload, agent_instance_id=agent_instance_id)
    if record is None:
        return 0

    try:
        _write_spool_file(spool_dir, record)
    except OSError as exc:
        _warn(f"failed to write spool file: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
