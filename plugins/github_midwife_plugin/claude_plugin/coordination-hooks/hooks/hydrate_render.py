#!/usr/bin/env python3
"""Hydrate renderer for memory-passthrough (Slice 2).

Run LOCALLY by the agent AFTER it has done the origin-filtered export process_call
(``export_memories(tags=["agent_memory","agent_memory:origin:<this agent>"],
file_path=<allowed spool>)``). The renderer never touches the solet — it reads the
exported JSON snapshot and regenerates the local projection:

  1. For each record: reconstruct its per-fact ``.md`` file from the slot tag
     (``agent_memory:slot:<origin>:<slug>`` -> ``<memory_dir>/<slug>.md``) and
     write ``content`` VERBATIM (frontmatter included; never rewritten on render —
     byte-for-byte, so hash equality is the round-trip test).
  2. Render ``MEMORY.md`` as a projection: one deterministically-sorted line per
     record from the frontmatter ``name`` / ``description``. MEMORY.md is NOT a
     stored record — it is regenerated, never captured back.
  3. Stamp the hydrated hashes (path -> sha256 of the bytes just written) BEFORE
     returning, so the PostToolUse capture that fires on these writes echo-breaks
     instead of re-draining hydrate's own output.

Fail-loud (Q6): a record with no slot tag, an unresolvable slot path, or missing/
invalid frontmatter (no ``name``) aborts the whole render non-zero — a corrupt
projection is never silently half-written. Solet-down is handled by the AGENT (it
skips the export step and never invokes this renderer), so the last projection
stays untouched.

Usage:  python3 hydrate_render.py <exported_snapshot.json>

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
import index_render


class RenderError(RuntimeError):
    """A record cannot be rendered — abort loud rather than half-write."""


def _load_snapshot(path: str) -> list[dict[str, object]]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    memories = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(memories, list):
        raise RenderError(f"snapshot {path}: no 'memories' list")
    return [m for m in memories if isinstance(m, dict)]


def _render(snapshot_path: str) -> dict[str, object]:
    memories = _load_snapshot(snapshot_path)
    mem_dir = _journal.memory_dir()
    mem_dir.mkdir(parents=True, exist_ok=True)

    written_hashes: dict[str, str] = {}
    facts: list[index_render.Fact] = []
    pending_paths = {
        path
        for entry in _journal.pending_entries()
        if isinstance(path := entry.get("path"), str)
    }
    skipped_pending: list[str] = []

    for record in memories:
        slot_tag = _journal.slot_tag_of(record.get("tags"))
        if slot_tag is None:
            raise RenderError(f"record {record.get('id')}: no agent_memory:slot tag")
        target = _journal.path_for_slot_tag(slot_tag)
        if target is None:
            raise RenderError(f"record {record.get('id')}: unresolvable slot tag {slot_tag!r}")
        content = record.get("content")
        if not isinstance(content, str):
            raise RenderError(f"record {slot_tag}: content is not text")

        # Single-sourced with the standalone tool: one frontmatter contract,
        # so a record that indexes one way cannot hydrate another.
        name, description, kind = index_render.parse_frontmatter(content, slot_tag)

        target_path = str(target.resolve())
        if target_path in pending_paths:
            # A captured edit is in-flight to the canonical store. Rendering an
            # older export over it would turn the next drain into a stale write.
            # Leave it intact and report the omission rather than losing data.
            skipped_pending.append(target_path)
        else:
            # Write the fact file VERBATIM (never rewrite frontmatter on render).
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
            written_hashes[target_path] = _journal.sha256_file(target) or ""

        facts.append(
            index_render.Fact(
                filename=target.name,
                name=name,
                description=description,
                kind=kind,
            ),
        )

    # MEMORY.md — head PRESERVED, tail regenerated. Hydrate must not author the
    # index itself: it shares index_render with the standalone tool so the two
    # cannot drift, and so it inherits the byte AND line budgets. Rendering one
    # line per record here is what made a 172-record hydrate emit 55 KB against
    # a 24.4 KB limit.
    index_path = mem_dir / "MEMORY.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""
    # The curated head is judgment that exists in no record — which lanes have
    # an open next action. A hydrate that overwrote it would silently destroy
    # the only part of the index no export can reconstruct.
    head = index_render.split_head(existing)
    index_text, report = index_render.render_index(head, facts)
    with open(index_path, "w", encoding="utf-8") as handle:
        handle.write(index_text)

    # Stamp hydrated hashes BEFORE returning so capture echo-breaks these writes.
    _journal.stamp_hydrated_hashes(written_hashes)

    return {
        "records": len(memories),
        "files_written": len(written_hashes),
        "skipped_pending": skipped_pending,
        "indexed": report.indexed,
        # Surfaced, never silent: a hydrate that could not index every fact
        # must say so in its own output, or a truncated index reads as complete.
        "omitted": report.omitted,
        "index_bytes": report.total_bytes,
        "index_lines": report.total_lines,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: hydrate_render.py <exported_snapshot.json>", file=sys.stderr)
        return 2
    try:
        result = _render(sys.argv[1])
    except (
        RenderError,
        index_render.IndexRenderError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"HYDRATE FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "hydrated", **result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
