#!/usr/bin/env python3
"""Memory-passthrough sync wrapper (maintenance-verbs M1, interim slice).

Composes the existing hydrate/drain steps
(``workbench/2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md`` §2.4)
into ONE Bash invocation per direction instead of the multi-step manual dance
(``export_memories`` process_call -> ``hydrate_render.py``; or ``drain.py`` ->
N ``upsert_memory_by_tag`` process_calls -> ``drain.py --advance``). A
client-side script exactly like the two it wraps -- not a platform verb, no
new server-side surface, so it carries no born-async question at all. Ships
NOW, independent of D0.3 (coordinator-seat ruling, M0 review, 2026-08-09).

Every failure contract the two wrapped scripts already earned is preserved
VERBATIM, not redesigned:
  * solet unreachable on export -> hydrate is skipped entirely, non-zero exit,
    the last projection stays untouched (never a partial write).
  * A record with no slot tag, an unresolvable slot path, or invalid
    frontmatter -> the whole render aborts non-zero (never a half-written
    projection) -- ``hydrate_render._render`` raises, this wrapper does not
    catch-and-continue.
  * A drain upsert that fails -> the watermark is NOT advanced, so the failed
    (and only the failed) entries are retried on the next run; a partial
    success is reported loudly, never silently swallowed into "done."
  * A deleted local fact file at drain time -> skipped, not upserted (local
    delete is a cache clear, not a canonical forget -- R6); to forget
    canonically, call ``delete_memories_by_tag`` on the slot tag directly.

Stdlib-only, run outside the venv -- same posture as ``hydrate_render.py``/
``drain.py``, which this module imports directly (function calls, not
subprocess re-invocation) to avoid an extra process layer.

Usage:
  python3 sync.py hydrate [spool_path]
  python3 sync.py drain
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: E402
# pyright: reportMissingImports=false
import _journal
import drain as _drain
import hydrate_render as _hydrate

EXPORT_PROCESS_KEY = "service_interface::memory_service::export_memories"
_SOLET_TIMEOUT_SECONDS = 60


def _solet_call(process_key: str, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Shell out to the ``solet-bridge`` CLI (this script has no MCP bridge of
    its own, same reason ``drain``/``hydrate_render`` are agent-mediated
    rather than server-side). Returns ``(envelope, None)`` on a parseable
    response, or ``(None, <error text>)`` -- never raises, so callers decide
    fail-loud-and-stop vs. fail-loud-and-report per their own contract."""
    if which("solet-bridge") is None:
        return None, "solet-bridge is unavailable on PATH; refusing the memory sync call"
    try:
        result = subprocess.run(
            ["solet-bridge", "call", process_key, json.dumps(arguments)],
            capture_output=True, text=True, timeout=_SOLET_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"solet-bridge call {process_key} failed to run: {exc}"
    if result.returncode != 0:
        # The CLI writes its JSON error envelope to STDOUT even on a non-zero
        # exit (measured live 2026-08-09 against export_memories' own
        # export_allowed_roots refusal) -- stderr is typically empty, so
        # surfacing only stderr here would silently swallow the actual
        # error_message every time.
        detail = result.stdout.strip() or result.stderr.strip()
        return None, f"solet-bridge call {process_key} exited {result.returncode}: {detail[:400]}"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"solet-bridge call {process_key} returned unparseable output: {exc}"


def _default_spool_path() -> Path:
    # "spool" is not this script's own convention -- it is the literal
    # subdirectory the operator's export_allowed_roots config declares
    # (profile/config/plugins/actr_memory_plugin.json), confirmed live
    # 2026-08-09: state_dir() alone (no "spool") is OUTSIDE that allowlist
    # and export_memories refuses it loud. Composing the declared root
    # rather than inventing a sibling path.
    return _journal.state_dir() / "spool" / "export.json"


def cmd_hydrate(spool_arg: str | None) -> int:
    spool_path = Path(spool_arg) if spool_arg else _default_spool_path()
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    envelope, err = _solet_call(
        EXPORT_PROCESS_KEY,
        {"tags": [_journal.UMBRELLA_TAG, _journal.origin_tag()], "file_path": str(spool_path)},
    )
    if err is not None or envelope is None:
        print(f"HYDRATE SKIPPED (solet unreachable, last projection untouched): {err}", file=sys.stderr)
        return 1
    result_block = envelope.get("result")
    if envelope.get("status") != "completed" or not isinstance(result_block, dict) or not result_block.get("success", True):
        print(
            "HYDRATE SKIPPED (export_memories did not complete, last projection "
            f"untouched): {json.dumps(envelope)[:500]}",
            file=sys.stderr,
        )
        return 1
    try:
        rendered = _hydrate._render(str(spool_path))  # noqa: SLF001 — the sanctioned entry point; main() only wraps argv
    except (_hydrate.RenderError, _hydrate.index_render.IndexRenderError, OSError, json.JSONDecodeError) as exc:
        print(f"HYDRATE FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "hydrated", **rendered}))
    return 0


def cmd_drain() -> int:
    built = _drain._build_upserts()  # noqa: SLF001 — the sanctioned entry point; main() only wraps argv
    # _build_upserts() is annotated -> dict[str, object] (drain.py's own
    # convention, unchanged here); cast to the shape its own docstring
    # promises rather than widen that annotation for this one caller.
    upserts = cast("list[dict[str, Any]]", built["upserts"])
    skipped_deleted = built["skipped_deleted"]
    if not upserts:
        if skipped_deleted:
            # Nothing to submit, but skipped-deleted entries still occupy the
            # journal — advance past them so a re-deleted-then-gone file
            # doesn't get re-read forever (R6: a local delete is a forget of
            # the FILE, not a reason to keep polling it). Bound to the
            # listing _build_upserts() just recorded (MEM-06), not a fresh EOF.
            _journal.advance_to_listed_offset()
        print(json.dumps({"status": "drained", "upserts_submitted": 0, "skipped_deleted": skipped_deleted}))
        return 0
    submitted = 0
    failures: list[dict[str, str]] = []
    for entry in upserts:
        envelope, err = _solet_call(entry["process_key"], entry["arguments"])
        if err is not None or not isinstance(envelope, dict) or envelope.get("status") != "completed":
            failures.append({"path": entry["path"], "error": err or json.dumps(envelope)[:300]})
            continue
        submitted += 1
    if failures:
        print(
            json.dumps({"status": "drain_partial", "upserts_submitted": submitted, "failures": failures}),
            file=sys.stderr,
        )
        print(
            "DRAIN PARTIAL FAILURE -- watermark NOT advanced; ALL pending entries "
            "(including the ones that succeeded above) will be retried next run, "
            "since upsert_memory_by_tag is idempotent on the slot tag.",
            file=sys.stderr,
        )
        return 1
    # Bound to the listing _build_upserts() recorded above, not a fresh EOF
    # read (MEM-06) — a capture landing during the upsert loop above stays
    # pending and is retried on the next drain.
    _journal.advance_to_listed_offset()
    print(json.dumps({"status": "drained", "upserts_submitted": submitted, "skipped_deleted": skipped_deleted}))
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("hydrate", "drain"):
        print("usage: sync.py {hydrate [spool_path]|drain}", file=sys.stderr)
        return 2
    if sys.argv[1] == "hydrate":
        return cmd_hydrate(sys.argv[2] if len(sys.argv) > 2 else None)
    return cmd_drain()


if __name__ == "__main__":
    sys.exit(main())
