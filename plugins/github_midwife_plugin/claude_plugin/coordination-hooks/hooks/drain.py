#!/usr/bin/env python3
"""Drain helper for memory-passthrough (Slice 2).

Agent-mediated: a hook subprocess has no MCP bridge (design §4.4 item 5), so the
DRAIN is done by the session, not the hook. This helper is the deterministic
tag-computing half — it reads the pending journal and emits the EXACT
``upsert_memory_by_tag`` arguments for each fact file, so the agent never guesses
the slot / provenance tags. The agent then issues one ``process_call`` per entry
and calls ``drain.py --advance`` to mark them drained.

Modes:
  (no args)   Print JSON: {"pending": N, "upserts": [{path, arguments}], "skipped_deleted": M}
              where arguments = {content, tag:<slot>, tags:[<umbrella,origin,scope,hash>]}.
              A journal entry whose file no longer exists is SKIPPED, not upserted
              (R6: a local delete is a cache clear, not a canonical forget — the
              record stays and the next hydrate regenerates the file). To forget
              canonically, the agent calls delete_memories_by_tag(<slot>).
  --advance   Advance the drain watermark to exactly the last (no-args) listing's
              end offset — never a fresh re-read of the journal's current end
              (MEM-06: that re-read used to swallow a capture that landed between
              the listing and this call). Call this only AFTER the upserts
              succeed. Captures that land during the drain sit past the listed
              offset and are picked up next time. Fails loud (exit 2) if no
              listing was recorded to bind to.

Stdlib-only — the agent runs it via Bash, outside the venv.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: E402
# pyright: reportMissingImports=false
import _journal

UPSERT_PROCESS_KEY = "service_interface::memory_service::upsert_memory_by_tag"


def _build_upserts() -> dict[str, object]:
    entries, end_offset = _journal.pending_entries_snapshot()
    upserts: list[dict[str, object]] = []
    skipped_deleted = 0
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        # Re-read the CURRENT file (the journaled sha may be stale by drain time).
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            skipped_deleted += 1  # local delete = cache clear, not a forget (R6)
            continue
        sha = _journal.sha256_file(path)
        upserts.append(
            {
                "path": path,
                "process_key": UPSERT_PROCESS_KEY,
                "arguments": {
                    "content": content,
                    "tag": _journal.slot_tag_for(path),
                    "tags": _journal.provenance_tags(sha),
                },
            }
        )
    # Bind a later --advance to exactly what THIS listing covered (MEM-06) —
    # never to whatever the journal has grown to by the time --advance runs.
    _journal.record_listing_offset(end_offset)
    return {"pending": len(upserts), "upserts": upserts, "skipped_deleted": skipped_deleted}


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--advance":
        try:
            _journal.advance_to_listed_offset()
        except _journal.NoPendingListingError as exc:
            print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps({"status": "advanced"}))
        return 0
    if len(sys.argv) != 1:
        print("usage: drain.py [--advance]", file=sys.stderr)
        return 2
    print(json.dumps(_build_upserts()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
