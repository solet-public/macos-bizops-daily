#!/usr/bin/env python3
"""Shared journal library for the memory-passthrough loop (Slice 2).

Unified-memory-passthrough (workbench/2026-07-16_unified_memory_passthrough_design_v2.md
§4.4-4.6). The local memory dir is a DISPOSABLE PROJECTION of the solet's memory_service:
the agent keeps writing per-fact `.md` files natively, a PostToolUse capture hook
journals each write, an agent-mediated drain flushes the journal to
`upsert_memory_by_tag`, and a SessionStart hydrate regenerates the dir from an
origin-filtered export.

This library is the local-filesystem half only — it NEVER touches the solet (a hook
subprocess has no MCP bridge; that is exactly why drain/hydrate are agent-mediated,
design §4.4 item 5). Pure stdlib: capture fires outside the venv.

State (all under ~/.claude/memory_passthrough/):
  * journal.jsonl        — append-only capture log; {path, sha256, mtime, origin, captured_at}
  * journal.watermark    — byte offset of the last drained position (advanced after a drain)
  * hydrated_hashes.json — {abs_path: sha256} last rendered by hydrate; the echo-break oracle
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


import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

# --- Locations -------------------------------------------------------------

# A test/override seam: point the whole loop at a scratch tree without touching
# the real dirs. When unset, the real per-agent locations are used.
_ENV_STATE_DIR = "MEMORY_PASSTHROUGH_STATE_DIR"
_ENV_MEMORY_DIR = "MEMORY_PASSTHROUGH_MEMORY_DIR"
_ENV_PROJECT_DIR = "CLAUDE_PROJECT_DIR"


def state_dir() -> Path:
    """Directory holding the journal + watermark + hydrated-hash oracle."""
    override = os.environ.get(_ENV_STATE_DIR)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "memory_passthrough"


def journal_path() -> Path:
    return state_dir() / "journal.jsonl"


def watermark_path() -> Path:
    return state_dir() / "journal.watermark"


def hydrated_hashes_path() -> Path:
    return state_dir() / "hydrated_hashes.json"


def project_dir() -> Path:
    """The repo/project root (Claude Code sets CLAUDE_PROJECT_DIR in hook env).

    Fails fast when unset: the origin tag derives from this path's basename, and
    a cwd fallback once minted records under a wrong origin (claude_code.scratchpad,
    2026-08-05) that hydrate's origin-filtered export could never pull back.
    """
    value = os.environ.get(_ENV_PROJECT_DIR)
    if not value:
        raise RuntimeError(
            f"{_ENV_PROJECT_DIR} is not set; refusing to derive the memory origin "
            f"from cwd. Invoke as: {_ENV_PROJECT_DIR}=<repo-root> python3 <script>"
        )
    return Path(value)


def memory_dir() -> Path:
    """The per-agent memory dir the projection lives in.

    Claude Code stores per-project memory under
    ``~/.claude/projects/<encoded-cwd>/memory/`` where <encoded-cwd> is the abs
    project path with '/' replaced by '-'. Overridable for tests.
    """
    override = os.environ.get(_ENV_MEMORY_DIR)
    if override:
        return Path(override)
    encoded = str(project_dir().resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / "memory"


# R4 seed-packaging audit, Package B (2026-08-10) -- the origin-resolution
# ladder, so this checkout's own memory tags and an adopter's stay portable
# across a rename, never coupled to the accident of what a clone directory
# happens to be named. The midwife rewrites root_manifest.yaml's own
# `solet_name:` field ONLY at genesis -- a raw/pre-genesis checkout
# keeps its unwritten placeholder, so rung 2 must skip that exact literal
# rather than treat it as a real name.
_ROOT_MANIFEST_PLACEHOLDER = "solet"
_ROOT_MANIFEST_NAME_RE = re.compile(r"^solet_name:\s*(\S+)\s*$", re.MULTILINE)


def solet_name() -> str:
    """Three-rung resolution ladder for this checkout's own solet name.

    1. ``SOLET_NAME`` env var, if set -- the platform's own single
       source of truth when the launching environment carries it.
    2. ``root_manifest.yaml``'s own ``solet_name:`` field, read via a
       minimal regex line-scan -- never a real YAML parse: PyYAML is a
       venv-only dependency on this platform (measured 2026-08-10) and
       every file in this module runs outside the venv, so a YAML parse
       here would silently violate this loop's own stdlib-only claim.
       Skipped when the field still carries the unwritten placeholder
       (rung 2 is only trustworthy post-genesis).
    3. ``CLAUDE_PROJECT_DIR``-basename -- this function's prior sole
       behavior, the final fallback, preserving continuity for any
       checkout where neither rung above resolves.

    Fails fast (same contract :func:`project_dir` already has) only when
    ``CLAUDE_PROJECT_DIR`` itself is unset AND ``SOLET_NAME`` is
    unset -- there is nothing left to derive a name from at all. On THIS
    checkout, at the time this ladder was added, every rung already
    yields the same value, so adding it here changes no existing tag.
    """
    env_name = os.environ.get("SOLET_NAME", "").strip()
    if env_name:
        return env_name
    root = project_dir()  # raises RuntimeError if CLAUDE_PROJECT_DIR unset
    try:
        text = (root / "root_manifest.yaml").read_text(encoding="utf-8")
        match = _ROOT_MANIFEST_NAME_RE.search(text)
        candidate = match.group(1).strip("\"'") if match else ""
        if candidate and candidate != _ROOT_MANIFEST_PLACEHOLDER:
            return candidate
    except OSError:
        pass  # unreadable or absent -- fall through to rung 3, never raise
    return root.resolve().name


def origin() -> str:
    """Stable origin tag value for THIS agent's projection records.

    ``claude_code.<solet_name>`` (see :func:`solet_name` for the
    resolution ladder) — stable across sessions of the same repo, so
    hydrate pulls exactly this agent's records and never another origin's.
    """
    return f"claude_code.{solet_name()}"


# --- Tag scheme (design §4.2) ----------------------------------------------
# One record per fact file. The SLOT tag is the upsert replace key; the umbrella
# + origin + scope + hash tags are unioned onto it (Slice 1(c)). Tag matching is
# exact membership, so every record literally carries the ``agent_memory``
# umbrella (that is what earns it the consolidation/purge protection) and the
# origin tag (so a per-origin export cannot leak another agent's projection).
UMBRELLA_TAG = "agent_memory"
SCOPE_SHARED_TAG = "agent_memory:scope:shared"


def origin_tag() -> str:
    return f"agent_memory:origin:{origin()}"


def slot_tag_for(path: str | os.PathLike[str]) -> str:
    """The replace-key slot tag for a memory file: one slot per file-slug."""
    slug = Path(path).stem
    return f"agent_memory:slot:{origin()}:{slug}"


def hash_tag(sha256: str) -> str:
    return f"agent_memory:hash:{sha256[:16]}"


def provenance_tags(sha256: str | None) -> list[str]:
    """Umbrella + origin + scope (+ hash when known) tags unioned onto the slot."""
    tags = [UMBRELLA_TAG, origin_tag(), SCOPE_SHARED_TAG]
    if sha256:
        tags.append(hash_tag(sha256))
    return tags


def slot_tag_of(tags: object) -> str | None:
    """Extract the slot tag from a record's tag list (None if absent)."""
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("agent_memory:slot:"):
            return tag
    return None


def path_for_slot_tag(slot_tag: str) -> Path | None:
    """Reconstruct the memory-file path a slot tag names (inverse of slot_tag_for).

    ``agent_memory:slot:<origin>:<slug>`` -> ``<memory_dir>/<slug>.md``. The slug
    is the final colon-delimited segment; a malformed slot tag returns None.
    """
    parts = slot_tag.split(":")
    if len(parts) < 4 or parts[0] != "agent_memory" or parts[1] != "slot":
        return None
    slug = parts[-1]
    if not slug:
        return None
    return memory_dir() / f"{slug}.md"


# --- Memory-file predicate --------------------------------------------------

def is_memory_file(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` is a per-fact memory markdown file under the memory dir.

    Resolves symlinks before the containment check (an edit through a link that
    escapes the memory dir is not a memory write). MEMORY.md (the rendered index)
    is deliberately EXCLUDED — it is a projection of the record set, never a
    canonical fact, so it must not round-trip back into the store.
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    mem = memory_dir().resolve()
    if resolved.suffix != ".md" or resolved.name == "MEMORY.md":
        return False
    try:
        return os.path.commonpath([str(mem), str(resolved)]) == str(mem)
    except ValueError:
        return False


# --- Hashing ----------------------------------------------------------------

def sha256_file(path: str | os.PathLike[str]) -> str | None:
    """SHA-256 of a file's bytes, or None if it cannot be read (e.g. deleted)."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


# --- Hydrated-hash oracle (echo-break) --------------------------------------

def _load_hydrated_hashes() -> dict[str, str]:
    try:
        with open(hydrated_hashes_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def last_hydrated_hash(path: str | os.PathLike[str]) -> str | None:
    """The sha256 hydrate last rendered for ``path`` (the echo-break oracle)."""
    return _load_hydrated_hashes().get(str(Path(path).resolve()))


def stamp_hydrated_hashes(path_to_sha: dict[str, str]) -> None:
    """Record the hashes hydrate just rendered, BEFORE any capture can fire.

    Merges into the existing oracle so a partial re-hydrate never forgets other
    files' hashes. Atomic write (tmp + replace) so a crash mid-write cannot
    corrupt the oracle.
    """
    merged = _load_hydrated_hashes()
    merged.update({str(Path(k).resolve()): str(v) for k, v in path_to_sha.items()})
    _atomic_write_json(hydrated_hashes_path(), merged)


# --- Journal ----------------------------------------------------------------

def append_capture(path: str | os.PathLike[str]) -> str:
    """Append one capture record for ``path``; return the disposition.

    Echo-break: a write whose sha256 equals the last-hydrated hash for that path
    is the hydrate renderer's own write bouncing back — skipped, so it never
    re-drains as a fresh edit. A deleted file (no readable bytes) is journaled
    with sha256=null so the drain can treat it as a local-cache clear.

    Returns 'captured', 'echo_skipped', or 'not_memory'.
    """
    if not is_memory_file(path):
        return "not_memory"
    resolved = str(Path(path).resolve())
    sha = sha256_file(resolved)
    if sha is not None and sha == last_hydrated_hash(resolved):
        return "echo_skipped"
    try:
        mtime = os.path.getmtime(resolved)
    except OSError:
        mtime = None
    record = {
        "path": resolved,
        "sha256": sha,
        "mtime": mtime,
        "origin": origin(),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    _append_line(journal_path(), json.dumps(record, ensure_ascii=False))
    return "captured"


def _read_watermark() -> int:
    try:
        with open(watermark_path(), encoding="utf-8") as handle:
            return int(handle.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def pending_snapshot() -> tuple[list[dict[str, object]], int]:
    """Parsed journal records since the watermark, plus the EXACT byte offset
    this read stopped at.

    The offset is bound to what this call actually returned, never re-derived
    later from a fresh read of the file's current size. MEM-06 (2026-08-19):
    the old ``advance_watermark()`` re-read the journal's current EOF at
    advance time, so a capture landing between a listing and a later,
    separately-invoked advance was marked drained without ever being
    upserted. Every advance now must consume exactly the offset a listing
    call like this one returned — see :func:`record_listing_offset` /
    :func:`advance_to_listed_offset`.
    """
    path = journal_path()
    start = _read_watermark()
    if not path.exists():
        return [], start
    records: list[dict[str, object]] = []
    with open(path, encoding="utf-8") as handle:
        handle.seek(start)
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
        end_offset = handle.tell()
    return records, end_offset


def pending_lines() -> list[dict[str, object]]:
    """Parsed journal records appended since the last drain watermark."""
    records, _ = pending_snapshot()
    return records


def pending_entries_snapshot() -> tuple[list[dict[str, object]], int]:
    """Deduped drain work (latest record per path, newest wins, order-stable)
    plus the exact end offset the underlying listing was read through."""
    records, end_offset = pending_snapshot()
    latest: dict[str, dict[str, object]] = {}
    for rec in records:
        p = rec.get("path")
        if isinstance(p, str):
            latest[p] = rec
    return list(latest.values()), end_offset


def pending_entries() -> list[dict[str, object]]:
    """Deduped drain work: latest record per path (newest wins), order-stable."""
    entries, _ = pending_entries_snapshot()
    return entries


def pending_count() -> int:
    """Number of distinct paths awaiting drain."""
    return len(pending_entries())


def listing_offset_path() -> Path:
    return state_dir() / "journal.listing_offset"


def record_listing_offset(end_offset: int) -> None:
    """Persist the end offset of the most recent listing.

    A later, separately-invoked advance (possibly in a different process —
    the agent-mediated ``drain.py`` / ``drain.py --advance`` pair runs across
    two Bash calls with N upsert process_calls in between) must bind to
    exactly this value rather than to whatever the journal has grown to by
    the time it runs (MEM-06).
    """
    _atomic_write_text(listing_offset_path(), str(end_offset))


class NoPendingListingError(RuntimeError):
    """Raised when an advance is requested with no recorded listing to bind to."""


def advance_to_listed_offset() -> int:
    """Advance the watermark to exactly the last recorded listing's end offset.

    Never re-reads the journal's current size — that re-read is the MEM-06
    bug's exact shape. Raises :class:`NoPendingListingError` (rather than a
    silent no-op or a fallback to a fresh EOF) when no listing was recorded,
    or it was already consumed by a prior advance. Consumes the marker on
    success so a stale offset can't be reused by a second, unpaired advance.
    """
    marker = listing_offset_path()
    try:
        with open(marker, encoding="utf-8") as handle:
            end_offset = int(handle.read().strip())
    except (OSError, ValueError) as exc:
        raise NoPendingListingError(
            "no listing recorded to advance to — list pending entries first "
            "(drain.py with no args)"
        ) from exc
    _atomic_write_text(watermark_path(), str(end_offset))
    try:
        marker.unlink()
    except OSError:
        pass
    return end_offset


def advance_past_all_pending() -> int:
    """List everything currently pending and advance the watermark to cover it.

    Safe ONLY when nothing else can capture between the list and the advance
    within this call — e.g. test setup under a scratch state dir. Production
    drain flows (``drain.py``, ``sync.py``) must keep the listing and the
    advance as two separate calls across their own real work (the upserts),
    using :func:`record_listing_offset` / :func:`advance_to_listed_offset`
    directly, so a capture landing in that real window is retried next time,
    never swallowed.
    """
    _, end_offset = pending_snapshot()
    record_listing_offset(end_offset)
    return advance_to_listed_offset()


# --- Low-level IO -----------------------------------------------------------

def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: object) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))
