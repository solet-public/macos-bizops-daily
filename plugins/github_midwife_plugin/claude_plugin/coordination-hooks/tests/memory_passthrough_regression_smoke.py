#!/usr/bin/env python3
"""Regression proof of the MEM-06 capture/drain race, against the VENDORED
memory-passthrough utilities this plugin actually ships.

MEM-06 (2026-08-19, hit live): the old ``drain.py --advance`` rebound the
journal watermark to the journal's CURRENT end at advance time, so a capture
landing between a drain listing and its later ``--advance`` was marked
drained without ever being upserted. The fix was landed and proved against
the CHECKOUT copy (``.claude/hooks/memory_passthrough/tests/
passthrough_loop_smoke.py``) and then mirrored onto this plugin's own
``drain.py``/``_journal.py`` pair (MEM-07, ``8eb55dad9``) — but that mirror
shipped with no smoke of its own inside THIS plugin, so a future edit to
these vendored files could silently reintroduce the exact swallow bug in
every clone and install that runs them, and nothing here would say so
(MEM-08).

This file closes that gap by re-running the same regression directly against
``hooks/capture.py`` and ``hooks/drain.py`` as shipped in THIS plugin, plus
the explicit fail-loud contract for a bare ``--advance`` with no recorded
listing. It does not re-prove the whole capture/hydrate loop (that behaviour
legitimately diverges between the checkout and vendored copies — the
checkout carries head-budget policing this plugin does not vendor — see
SECURITY.md's Known gaps note); it proves the one behaviour whose divergence
already bit in production once.

Run directly; exit 0 on success, non-zero on failure.
"""

from __future__ import annotations

import sys

# Must precede the _harness import — see manifest_consistency_smoke.py for why.
sys.dont_write_bytecode = True

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from _harness import HOOKS_DIR, PLUGIN_ROOT, Results, preflight, run_hook  # noqa: E402

sys.path.insert(0, str(HOOKS_DIR))
# ruff: noqa: E402
# pyright: reportMissingImports=false
import _journal  # type: ignore[import-not-found]
import hydrate_render  # type: ignore[import-not-found]
import sync  # type: ignore[import-not-found]

_FACT_TEMPLATE = (
    "---\nname: {name}\ndescription: {desc}\nmetadata:\n  type: feedback\n---\n\n{body}\n"
)


def _fact_text(name: str, desc: str, body: str) -> str:
    return _FACT_TEMPLATE.format(name=name, desc=desc, body=body)


# A synthetic, deliberately non-existent project root. It is only a marker --
# the directories these tests actually read and write come from the
# MEMORY_PASSTHROUGH_* scratch overrides -- so this value never needs to resolve.
#
# It must NOT contain a real operator/solet home path. This file SHIPS inside the
# seed, and the seal gate fails closed on an operator identity marker in bundle
# content (§4.2 #4). A `/Users/<someone>` here blocked a mint on 2026-08-20 and
# would have leaked whose machine the fixture was written on into every
# downstream install.
_PROBE_PROJECT_DIR = "/nonexistent/probe-repo"


def _capture_env(scratch_env: dict[str, str]) -> dict[str, str]:
    return {
        "CLAUDE_PROJECT_DIR": _PROBE_PROJECT_DIR,
        **scratch_env,
    }


def _capture(res: Results, fact_path: Path, scratch_env: dict[str, str]) -> None:
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(fact_path)}})
    proc = run_hook("capture.py", env=_capture_env(scratch_env), stdin=payload)
    res.check(proc.returncode == 0, "capture.py exits 0", proc.stderr.strip())


def _drain(res: Results, scratch_env: dict[str, str]) -> dict[str, object]:
    proc = run_hook("drain.py", env=_capture_env(scratch_env), stdin="")
    res.check(proc.returncode == 0, "drain.py exits 0", proc.stderr.strip())
    try:
        return dict(json.loads(proc.stdout))
    except json.JSONDecodeError as exc:
        res.fail("drain.py stdout is valid JSON", str(exc))
        return {}


def _drain_advance(env: dict[str, str]):  # noqa: ANN201 — subprocess.CompletedProcess[str]
    # run_hook() has no argv-extension hook, and this is the only call in the
    # suite that needs one — a bare subprocess.run mirroring run_hook's own
    # recipe (base_env + -B) is cheaper than widening the shared harness for
    # a single caller.
    import subprocess  # noqa: PLC0415 — only this one call needs argv extension

    from _harness import base_env  # noqa: PLC0415

    child_env = base_env()
    child_env.update(env)
    return subprocess.run(
        [sys.executable, "-B", str(HOOKS_DIR / "drain.py"), "--advance"],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _solet_bridge_absence_fails_closed(res: Results) -> None:
    """The vendored sync hook must name a missing bridge before subprocess use."""
    from unittest.mock import patch  # noqa: PLC0415 — isolated hook guard proof

    with (
        patch.object(sync, "which", return_value=None),
        patch.object(sync.subprocess, "run") as run,
    ):
        envelope, error = sync._solet_call("service_interface::probe", {})
    res.check(envelope is None, "sync missing bridge returns no envelope")
    res.check(
        error == "solet-bridge is unavailable on PATH; refusing the memory sync call",
        f"sync missing bridge has a named condition (got {error!r})",
    )
    res.check(not run.called, "sync missing bridge never attempts subprocess.run")


def main() -> int:
    preflight()
    res = Results(f"memory-passthrough MEM-06 regression (vendored) — {PLUGIN_ROOT.name}")
    _solet_bridge_absence_fails_closed(res)

    scratch = Path(tempfile.mkdtemp())
    state = scratch / "state"
    memory = scratch / "memory"
    memory.mkdir(parents=True)
    scratch_env = {
        "MEMORY_PASSTHROUGH_STATE_DIR": str(state),
        "MEMORY_PASSTHROUGH_MEMORY_DIR": str(memory),
    }
    # In-process calls below (pending_count/advance_past_all_pending) import
    # _journal directly, so it needs the same overrides in THIS process' env —
    # the subprocess calls above get them explicitly via run_hook(env=...).
    os.environ.update(scratch_env)
    os.environ["CLAUDE_PROJECT_DIR"] = _PROBE_PROJECT_DIR
    os.environ.pop("SOLET_NAME", None)

    # Setup: one fact captured, then fully drained — a clean slate before the
    # interleaved-capture scenario below.
    fact1 = memory / "feedback_probe_one.md"
    fact1.write_text(_fact_text("feedback_probe_one", "a probe fact", "body one"), encoding="utf-8")
    _capture(res, fact1, scratch_env)
    res.check(_journal.pending_count() == 1, "capture journaled the fact write")
    _journal.advance_past_all_pending()
    res.check(_journal.pending_count() == 0, "clean slate before the regression scenario")

    # A pending local edit must survive an older export. Once it has been
    # drained, the same stale local file must again yield to canonical truth.
    hydrate_fact = memory / "hydrate_probe.md"
    canonical = _fact_text("hydrate_probe", "a canonical probe fact", "canonical body")
    diverged = _fact_text(
        "hydrate_probe",
        "a locally edited probe fact",
        "LOCALLY DIVERGED — must survive pending hydrate",
    )
    hydrate_fact.write_text(diverged, encoding="utf-8")
    _capture(res, hydrate_fact, scratch_env)
    hydrate_snapshot = state / "hydrate_snapshot.json"
    hydrate_snapshot.write_text(
        json.dumps({
            "memories": [{
                "id": "mem-hydrate",
                "content": canonical,
                "tags": [f"agent_memory:slot:{_journal.origin_tag()}:hydrate_probe"],
            }],
        }),
        encoding="utf-8",
    )
    pending_render = hydrate_render._render(str(hydrate_snapshot))
    res.check(
        pending_render["skipped_pending"] == [str(hydrate_fact.resolve())],
        "pending hydrate visibly reports the skipped fact",
        str(pending_render),
    )
    res.check(
        hydrate_fact.read_text(encoding="utf-8") == diverged,
        "pending local edit survives hydrate",
    )
    _journal.advance_past_all_pending()
    clean_render = hydrate_render._render(str(hydrate_snapshot))
    res.check(clean_render["skipped_pending"] == [], "clean hydrate reports no pending skips")
    res.check(
        hydrate_fact.read_text(encoding="utf-8") == canonical,
        "clean stale fact is overwritten byte-identically",
    )

    # MEM-06 regression: a capture landing BETWEEN drain.py's listing and its
    # later --advance must NOT be swallowed.
    fact2 = memory / "feedback_probe_two.md"
    fact2.write_text(_fact_text("feedback_probe_two", "a second probe fact", "body two"), encoding="utf-8")
    _capture(res, fact2, scratch_env)
    fact2_resolved = str(fact2.resolve())
    listing = _drain(res, scratch_env)
    upserts = listing.get("upserts") if isinstance(listing, dict) else None
    res.check(
        listing.get("pending") == 1
        and isinstance(upserts, list)
        and upserts[0].get("path") == fact2_resolved,
        "listing covers only the pre-listing capture",
        str(listing),
    )

    # Simulate a capture landing during the drain window (between the listing
    # above and the --advance below).
    fact3 = memory / "feedback_probe_three.md"
    fact3.write_text(_fact_text("feedback_probe_three", "a third probe fact", "body three"), encoding="utf-8")
    _capture(res, fact3, scratch_env)
    fact3_resolved = str(fact3.resolve())

    advance_proc = _drain_advance(_capture_env(scratch_env))
    res.check(advance_proc.returncode == 0, "drain.py --advance exits 0", advance_proc.stderr.strip())
    res.check(
        _journal.pending_count() == 1,
        "the interleaved capture was NOT swallowed by --advance",
    )
    post_advance = _drain(res, scratch_env)
    post_upserts = post_advance.get("upserts") if isinstance(post_advance, dict) else None
    res.check(
        post_advance.get("pending") == 1
        and isinstance(post_upserts, list)
        and post_upserts[0].get("path") == fact3_resolved,
        "the interleaved capture is the one still pending, and drains next time",
        str(post_advance),
    )

    # A bare --advance with nothing freshly listed since the last consumption
    # must fail loud, never silently no-op or fall back to a fresh EOF.
    _journal.advance_past_all_pending()
    no_listing = _drain_advance(_capture_env(scratch_env))
    res.check(no_listing.returncode == 2, "--advance with no recorded listing fails loud (exit 2)")
    res.check('"status": "error"' in no_listing.stderr, "the loud failure names itself in the JSON output")

    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
