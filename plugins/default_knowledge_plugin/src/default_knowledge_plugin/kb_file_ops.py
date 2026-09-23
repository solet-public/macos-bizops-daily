"""File operation helpers for knowledge base browsing and editing.

Covers: validate_path, reindex_file, browse, read, edit, create, delete.
No plugin instance — services are passed as explicit parameters.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chunking import split_title_metadata_body
from .constants import (
    DOC_HISTORY_DIRNAME,
    METADATA_BLOCK_REQUIRED_KEYS,
    PLUGIN_NAME,
    TABLE_KNOWLEDGE_INSTALL,
    WritePosture,
)
from .kb_git import git_commit_file
from .kb_indexing import (
    delete_kb_chunks_for_file,
    index_files,
    now_iso,
    parse_memory_ids,
    resolve_manifest,
)
from .kb_lifecycle import get_install_record
from .models import Manifest

_SNAPSHOT_STAMP_FORMAT = "%Y-%m-%dT%H-%M-%S-%fZ"


def content_sha256(text: str) -> str:
    """Hash of the exact decoded string ``read_file`` returns.

    Pinned normalization (W12): sha256 over the ``read_text(encoding='utf-8',
    errors='replace')`` string re-encoded as UTF-8. Both ``read_file`` and the
    ``edit_file`` verification re-read through the same decode path, so
    non-UTF8 bytes on disk normalize identically on both sides and cannot
    produce a spurious mismatch.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest_for(record: dict[str, Any]) -> Manifest:
    """Resolve the manifest for an install record's KB directory."""
    kb_dir = Path(record["resolved_path"]).resolve()
    return resolve_manifest(kb_dir, record["name"], None)


def _is_under_doc_history(relative_path: str) -> bool:
    """True iff a KB-relative path lies in the ``.doc_history`` snapshot sidecar."""
    return DOC_HISTORY_DIRNAME in Path(relative_path).parts


def _is_under_subdir(relative_path: str, subdir: str) -> bool:
    """True iff a KB-relative path lies strictly under ``subdir``."""
    parts = Path(relative_path).parts
    sub_parts = Path(subdir).parts
    return len(parts) > len(sub_parts) and parts[: len(sub_parts)] == sub_parts


def _enforce_write_guard(
    record: dict[str, Any], manifest: Manifest, *, verb: str, path: str,
) -> None:
    """Shared posture + protected-path guard for create / edit / delete.

    Posture rejections (W4): ``READ_ONLY`` rejects all three; ``CREATE_ONLY``
    rejects edit/delete; ``CREATE_AND_CAS_EDIT`` rejects delete (archive_file is
    the sanctioned retirement path). Protected-path rejections (all postures):
    a content write into the ``.doc_history`` sidecar (platform-written only) or,
    when ``archive_subdir`` is configured, into that subtree (only archive_file
    writes there, and only it stamps supersession). Rejections raise
    ``PermissionError`` naming the posture and the alternative.
    """
    name = record["name"]
    posture = manifest.write_posture
    if posture == WritePosture.READ_ONLY:
        raise PermissionError(
            f"KB '{name}' is read_only; {verb}_file is not permitted."
        )
    if posture == WritePosture.CREATE_ONLY and verb in ("edit", "delete"):
        raise PermissionError(
            f"KB '{name}' posture create_only rejects {verb}_file; "
            f"use create_file, or archive_file to retire a doc."
        )
    if posture == WritePosture.CREATE_AND_CAS_EDIT and verb == "delete":
        raise PermissionError(
            f"KB '{name}' posture create_and_cas_edit rejects delete_file; "
            f"use archive_file to retire a doc (workbench docs are archived, never deleted)."
        )
    if _is_under_doc_history(path):
        raise PermissionError(
            f"'{path}' is under the platform-written {DOC_HISTORY_DIRNAME} "
            f"snapshot sidecar; {verb}_file there is not permitted."
        )
    if manifest.archive_subdir and _is_under_subdir(path, manifest.archive_subdir):
        raise PermissionError(
            f"'{path}' is under the archive subdir '{manifest.archive_subdir}'; "
            f"use archive_file — a direct {verb}_file there bypasses supersession stamping."
        )


def _validate_metadata_block(name: str, path: str, content: str) -> None:
    """Reject content missing the §4 metadata block (W4, create-path only).

    Shallow presence check — a ``# `` title plus every required §4 key present
    in the leading metadata run — not a content-quality judgement, so docs stay
    cheap to write. The MCP create path is the one mechanical enforcement point
    of the forward-only convention; worktree authoring stays convention-governed.
    """
    title_line, pairs, _ = split_title_metadata_body(content)
    present = {key for key, _ in pairs}
    missing: list[str] = []
    if title_line is None:
        missing.append("a '# ' title line")
    missing.extend(key for key in METADATA_BLOCK_REQUIRED_KEYS if key not in present)
    if missing:
        raise ValueError(
            f"KB '{name}' requires the §4 metadata block on create; "
            f"'{path}' is missing: {', '.join(missing)}. See the workbench "
            f"document-authoring convention article (search name='workbench')."
        )


def _snapshot_relative(relative_path: str) -> str:
    """Return the ``.doc_history`` snapshot path for a KB-relative file."""
    stamp = datetime.now(UTC).strftime(_SNAPSHOT_STAMP_FORMAT)
    return f"{DOC_HISTORY_DIRNAME}/{relative_path}/{stamp}.md"


def _write_prior_version_snapshot(kb_dir: Path, relative_path: str, prior: str) -> str:
    """Copy the pre-edit content under ``.doc_history`` before an overwrite.

    Makes every CAS edit mechanically reversible without git. Returns the
    KB-relative snapshot path (excluded from indexing by ``collect_files``).
    """
    snapshot_rel = _snapshot_relative(relative_path)
    snapshot_path = kb_dir / snapshot_rel
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(prior, encoding="utf-8")
    return snapshot_rel


def _upsert_pair(pairs: list[tuple[str, str]], key: str, value: str) -> None:
    """Set ``key`` in an ordered pair list in place; append when absent."""
    for idx, (existing_key, _) in enumerate(pairs):
        if existing_key == key:
            pairs[idx] = (key, value)
            return
    pairs.append((key, value))


def _stamp_archive_metadata(content: str, superseded_by: str | None) -> str:
    """Return ``content`` with the §4 block stamped for archival (W11).

    Adds ``Archived: <YYYY-MM-DD>``; when a successor is named, sets
    ``Status: superseded`` and ``Superseded_by: <successor>``. A legacy doc
    with no block gets a minimal one inserted. The rewrite is confined to the
    leading §4 block; the body is preserved verbatim.
    """
    title_line, pairs, body_lines = split_title_metadata_body(content)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    _upsert_pair(pairs, "Archived", today)
    if superseded_by:
        _upsert_pair(pairs, "Status", "superseded")
        _upsert_pair(pairs, "Superseded_by", superseded_by)

    out: list[str] = []
    if title_line is not None:
        out.append(title_line)
        out.append("")
    out.extend(f"{key}: {value}" for key, value in pairs)
    out.extend(body_lines)
    return "\n".join(out)


def validate_path(record: dict[str, Any], path: str) -> Path:
    """Resolve and validate a path within a KB directory.

    Raises ValueError for path traversal, FileNotFoundError if not found.
    """
    kb_dir = Path(record["resolved_path"]).resolve()
    target = (kb_dir / path).resolve() if path else kb_dir
    if not target.is_relative_to(kb_dir):
        raise ValueError(f"Path traversal rejected: {path}")
    if not target.exists():
        raise FileNotFoundError(f"Path not found in '{record['name']}': {path}")
    return target


def reindex_file(
    record: dict[str, Any],
    kb_dir: Path,
    relative_path: str,
    state_service: Any,
    memory_service: Any,
) -> None:
    """Hard-delete old chunks for a file, re-chunk, remember new chunks, update record.

    Per W5.P §3.1.B (same shape as ``update_kb``) + the 2026-06-21 SQL-lockdown
    cohort: the chunk-delete now flows through the OWNING ``memory_service`` verb
    (``delete_kb_chunks_for_file`` → ``delete_memories_by_ids``; no
    foreign-namespace SQL), so only the install-record UPDATE runs inside a
    ``state_service.transactional()`` block. ``index_files`` (new-chunk
    remember-path) runs outside the transaction; the narrow mid-indexing orphan
    window is a regenerable index, swept by the operator-fired
    ``purge_orphaned_chunks`` verb.

    SERIALIZATION (no advisory lock — D3/kb GAP-6, 2026-06-21): the per-KB
    ``pg_advisory_xact_lock(hashtext(name)::bigint)`` that previously guarded
    these two transactions was removed as REDUNDANT. Every caller of this
    function is a synchronous, non-self-completing service-interface EDGE verb
    (``edit_file`` / ``create_file`` / ``delete_file``), which the
    single-threaded ActionQueuePoller dispatches one-at-a-time (``_poll_once``
    awaits each ``execute_action`` inline) — so two same-KB reindex/edit
    operations can never overlap, and serial dispatch is strictly stronger than
    the per-name lock (which released between the two txns anyway).
    TRIPWIRE — this redundancy holds ONLY while: (a) these verbs stay
    ``is_async=False`` / non-self-completing; (b) a single active-color poller
    dispatches them; and (c) every in-process caller of
    ``knowledge_service.{edit,create}_file`` (e.g. ``default_thinking_plugin``,
    the one cross-plugin direct caller, itself poller-serialized) stays
    poller-serialized. A future ``is_async`` flip on these verbs, a second
    concurrent dispatcher, or a direct call from an async/threaded/non-poller
    context re-surfaces the need for the per-KB advisory lock.
    """
    if relative_path.startswith("processes/"):
        return
    name = record["name"]
    manifest = resolve_manifest(kb_dir, name, None)

    old_ids = parse_memory_ids(record.get("memory_ids", []))
    deleted = delete_kb_chunks_for_file(name, relative_path, memory_service)
    remaining_ids = [mid for mid in old_ids if mid not in deleted]

    file_path = kb_dir / relative_path
    new_ids: list[str] = []
    if file_path.exists():
        new_ids, _ = index_files(kb_dir, name, manifest, memory_service, [file_path])

    all_ids = remaining_ids + new_ids
    with state_service.transactional() as txn:
        txn.update_state(
            namespace=PLUGIN_NAME,
            query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"name": name}},
            updates={
                "memory_ids": all_ids,
                "chunk_count": len(all_ids),
                "indexed_at": now_iso(),
            },
        )


def browse_kb(
    name: str, path: str, state_service: Any,
) -> dict[str, Any]:
    """List directory contents within a knowledge base."""
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")

    target = validate_path(record, path)

    if target.is_file():
        stat = target.stat()
        return {
            "status": "success",
            "name": name,
            "path": path,
            "type": "file",
            "size": stat.st_size,
        }

    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir()):
        if child.name.startswith("."):
            continue
        entry: dict[str, Any] = {
            "name": child.name,
            "type": "directory" if child.is_dir() else "file",
        }
        if child.is_file():
            entry["size"] = child.stat().st_size
        entries.append(entry)

    return {
        "status": "success",
        "name": name,
        "path": path,
        "type": "directory",
        "entries": entries,
        "count": len(entries),
    }


def read_file_kb(
    name: str, path: str, state_service: Any,
) -> dict[str, Any]:
    """Read a file from a knowledge base. Path traversal protected."""
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")

    target = validate_path(record, path)
    if not target.is_file():
        raise ValueError(f"Not a file: {path}")

    content = target.read_text(encoding="utf-8", errors="replace")
    return {
        "status": "success",
        "name": name,
        "path": path,
        "content": content,
        "size": len(content),
        "content_sha256": content_sha256(content),
    }


def edit_file_kb(
    name: str, path: str, content: str,
    state_service: Any, memory_service: Any,
    expected_content_hash: str | None = None,
) -> dict[str, Any]:
    """Overwrite an existing file, re-chunk, git commit if applicable.

    Optimistic concurrency (W12): when ``expected_content_hash`` is supplied it
    must equal the target's current content hash or the edit fails loud, naming
    both hashes and instructing a re-read-and-reapply — the file and its
    snapshot dir are left untouched. When engaged, a prior-version snapshot is
    written under ``.doc_history`` before the overwrite (reversible without git).
    Under ``CREATE_AND_CAS_EDIT`` a hash is mandatory (a blind overwrite is
    exactly what the posture prevents); under ``FULL`` an omitted hash keeps the
    legacy behavior byte-for-byte (no snapshot, direct write).
    """
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")

    target = validate_path(record, path)
    if not target.is_file():
        raise ValueError(f"Not a file: {path}")

    manifest = _manifest_for(record)
    _enforce_write_guard(record, manifest, verb="edit", path=path)

    if manifest.write_posture == WritePosture.CREATE_AND_CAS_EDIT and expected_content_hash is None:
        raise PermissionError(
            f"KB '{name}' posture create_and_cas_edit requires expected_content_hash "
            f"on edit_file; read_file returns content_sha256 — pass it back to edit."
        )

    kb_dir = Path(record["resolved_path"])

    if expected_content_hash is not None:
        current = target.read_text(encoding="utf-8", errors="replace")
        current_hash = content_sha256(current)
        if current_hash != expected_content_hash:
            raise ValueError(
                f"edit_file precondition failed for '{path}': expected content "
                f"hash {expected_content_hash} but current is {current_hash}. "
                f"Re-read the file and reapply your change to the current version."
            )
        _write_prior_version_snapshot(kb_dir, path, current)

    target.write_text(content, encoding="utf-8")

    if record.get("source_type") == "git":
        git_commit_file(kb_dir, path, f"kb: edit {path}")

    reindex_file(record, kb_dir, path, state_service, memory_service)

    return {"status": "success", "name": name, "path": path, "action": "edited"}


def create_file_kb(
    name: str, path: str, content: str,
    state_service: Any, memory_service: Any,
) -> dict[str, Any]:
    """Create a new file in a knowledge base, chunk and index it."""
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")

    kb_dir = Path(record["resolved_path"]).resolve()
    target = (kb_dir / path).resolve()

    if not target.is_relative_to(kb_dir):
        raise ValueError(f"Path traversal rejected: {path}")

    manifest = _manifest_for(record)
    _enforce_write_guard(record, manifest, verb="create", path=path)
    if manifest.require_metadata_block:
        _validate_metadata_block(name, path, content)

    if target.exists():
        raise FileExistsError(f"File already exists: {path}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    if record.get("source_type") == "git":
        git_commit_file(kb_dir, path, f"kb: create {path}")

    reindex_file(record, kb_dir, path, state_service, memory_service)

    return {"status": "success", "name": name, "path": path, "action": "created"}


def delete_file_kb(
    name: str, path: str,
    state_service: Any, memory_service: Any,
) -> dict[str, Any]:
    """Delete a file from a knowledge base and hard-delete its chunks."""
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")

    target = validate_path(record, path)
    if not target.is_file():
        raise ValueError(f"Not a file: {path}")

    _enforce_write_guard(record, _manifest_for(record), verb="delete", path=path)

    kb_dir = Path(record["resolved_path"])

    if record.get("source_type") == "git":
        subprocess.run(
            ["git", "rm", path],
            cwd=kb_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"kb: delete {path}"],
            cwd=kb_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        target.unlink()

    reindex_file(record, kb_dir, path, state_service, memory_service)

    return {"status": "success", "name": name, "path": path, "action": "deleted"}


def archive_file_kb(
    name: str, path: str, superseded_by: str | None,
    state_service: Any, memory_service: Any,
) -> dict[str, Any]:
    """Retire a workbench doc: move it under ``archive_subdir``, stamp the §4
    block, and re-key its index chunk so it stays discoverable and readable.

    "Delete" is not a workbench operation — archive is (W11). Allowed wherever
    ``archive_subdir`` is configured, independent of write posture; ``edit_file``
    / ``delete_file`` stay posture-gated. The move preserves the source's
    relative structure (``sub/foo.md`` → ``archive/sub/foo.md``, never
    flattened) and is a pure filesystem op — never a git verb (GIT-CONTROLLER
    policy; the rename lands at the next commit like any peer filesystem op).
    Fail-louds on: no ``archive_subdir`` configured, a source already under it
    or under ``.doc_history``, or a destination collision (never overwrites the
    forensic record).

    Chunk re-key rides ``reindex_file`` for BOTH paths, never the bare
    primitives, so the install record's ``memory_ids`` / ``chunk_count`` /
    ``indexed_at`` stay consistent (orphan detection builds its active set from
    the record). The record is RE-FETCHED between the two reindex calls: the
    first (old path) prunes the moved-away chunks and rewrites the record, so
    the second (new path) must read the pruned record — reusing the stale
    in-memory record would reintroduce the just-deleted chunk ids.
    """
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")

    manifest = _manifest_for(record)
    if not manifest.archive_subdir:
        raise ValueError(
            f"KB '{name}' has no archive_subdir configured; archive_file is not available."
        )
    if _is_under_doc_history(path):
        raise PermissionError(
            f"'{path}' is under the {DOC_HISTORY_DIRNAME} snapshot sidecar; cannot archive it."
        )
    if _is_under_subdir(path, manifest.archive_subdir):
        raise PermissionError(
            f"'{path}' is already under the archive subdir '{manifest.archive_subdir}'."
        )

    kb_dir = Path(record["resolved_path"]).resolve()
    source = validate_path(record, path)
    if not source.is_file():
        raise ValueError(f"Not a file: {path}")

    new_relative = f"{manifest.archive_subdir}/{path}"
    dest = (kb_dir / new_relative).resolve()
    if not dest.is_relative_to(kb_dir):
        raise ValueError(f"Path traversal rejected: {new_relative}")
    if dest.exists():
        raise FileExistsError(
            f"Archive destination already exists: {new_relative} (never overwrites the record)."
        )

    prior = source.read_text(encoding="utf-8", errors="replace")
    archived = _stamp_archive_metadata(prior, superseded_by)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(archived, encoding="utf-8")
    source.unlink()

    reindex_file(record, kb_dir, path, state_service, memory_service)
    fresh = get_install_record(name, state_service)
    if fresh is None:
        raise RuntimeError(f"Knowledge base '{name}' install record vanished mid-archive")
    reindex_file(fresh, kb_dir, new_relative, state_service, memory_service)

    return {
        "status": "success",
        "name": name,
        "path": path,
        "archived_path": new_relative,
        "superseded_by": superseded_by,
        "action": "archived",
    }
