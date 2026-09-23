"""KB install state management and lifecycle operations.

Covers: install-record helpers, auto-install, install/update/uninstall,
activate/deactivate, purge. No plugin instance — services are explicit params.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .constants import (
    PLUGIN_NAME,
    TABLE_KNOWLEDGE_INSTALL,
    TAG_DOMAIN_OFFICIAL,
    kb_id_tag,
)
from .kb_git import (
    _GIT_BRANCH,
    fetch_and_merge_git,
    git_clone,
    git_head_sha,
    git_setup_branch,
    resolve_git_token,
)
from .kb_indexing import (
    classify_source_type,
    collect_files,
    delete_kb_chunks,
    delete_kb_chunks_for_file,
    index_files,
    is_source_stale,
    now_iso,
    parse_memory_ids,
    resolve_manifest,
    resolve_source,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def get_install_record(name: str, state_service: Any) -> dict[str, Any] | None:
    """Get install record by name. Returns None if not found."""
    result = state_service.read_state(
        namespace=PLUGIN_NAME,
        query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"name": name}},
    )
    rows: list[dict[str, Any]] = result.get("data", {}).get("records", [])
    if not rows:
        return None
    return rows[0]


def has_valid_install(
    name: str,
    state_service: Any,
    memory_service: Any,
    kb_root: Path | None,
) -> bool:
    """Check if a KB is installed, embeddings exist, and content is current."""
    record = get_install_record(name, state_service)
    if record is None:
        logger.info("%s: has_valid_install('%s'): no install record", PLUGIN_NAME, name)
        return False

    chunk_count = int(record.get("chunk_count") or 0)
    memory_ids = parse_memory_ids(record.get("memory_ids", []))

    # A KB whose source has no indexable content (plugin KBs containing only
    # processes/ JSON files, or otherwise empty) installs to chunk_count=0 +
    # memory_ids=[]. That's a legitimate cache-valid state; skip the
    # embeddings-present check (no embeddings exist) and only gate on whether
    # newly-added source content has appeared since indexed_at.
    if chunk_count == 0 and not memory_ids:
        if is_source_stale(name, record, kb_root):
            return False
        logger.info(
            "%s: has_valid_install('%s'): valid (empty install, content current)",
            PLUGIN_NAME, name,
        )
        return True

    if not memory_ids:
        logger.info("%s: has_valid_install('%s'): no memory_ids", PLUGIN_NAME, name)
        return False

    tag = kb_id_tag(name)
    result = memory_service.recall(query=name, top_k=1, tags=[tag])
    memories = result.get("memories", [])
    if not memories:
        logger.info("%s: has_valid_install('%s'): embeddings purged", PLUGIN_NAME, name)
        return False

    if is_source_stale(name, record, kb_root):
        return False

    logger.info(
        "%s: has_valid_install('%s'): valid (%d chunks, embeddings present, content current)",
        PLUGIN_NAME, name, len(memory_ids),
    )
    return True


def _find_orphan_chunk_ids(
    state_service: Any, memory_service: Any,
) -> tuple[list[str], int, int]:
    """Return (orphan_ids, active_count, total_kb_chunk_count).

    Reads active install records' ``memory_ids`` (kb own namespace) and the
    full KB-chunk surface — every ``knowledge:official``-tagged live memory —
    through the OWNING memory service via ``get_memories_by_tag`` (cohort D1
    ruling, no foreign-namespace SQL); orphans = chunks not referenced by any
    active install record. Returns empty list when no active install records
    exist (skip case — nothing to safely-orphan-detect against).

    The owning-service read is behaviour-equivalent to the prior
    ``SELECT id FROM actr_memory_plugin__memory WHERE is_deleted = 0 AND
    tags::text LIKE '%knowledge:official%'`` it replaces, and strictly more
    correct on the tag match: ``get_memories_by_tag`` matches the tag by EXACT
    list-membership (no ``LIKE`` substring prefix-false-match).

    ``include_archived=True`` is REQUIRED for equivalence, not optional. The
    old ``is_deleted = 0`` predicate is STATUS-AGNOSTIC — it returns rows of
    ANY ``status`` (active AND archived), filtering only on the (vestigial)
    ``is_deleted`` flag, which is always 0 because actr memory hard-deletes.
    ``get_memories_by_tag`` defaults to active-only (``status='active'``); the
    default would silently MISS every archived chunk. This verb exists to
    drain the ACCUMULATED ARCHIVED orphan backlog (the 271k-archived-rows
    incident the JSON anchors to), so the FIND must be status-agnostic —
    ``include_archived=True`` (→ ``status=None`` → all rows) restores the old
    behaviour. (KB chunks are consolidation-exempt going forward, so no NEW
    archived rows accrue, but the historical backlog must still be reachable.)
    """
    result = state_service.read_state(
        namespace=PLUGIN_NAME,
        query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"is_active": 1}},
    )
    records: list[dict[str, Any]] = result.get("data", {}).get("records", [])
    active_ids: set[str] = set()
    for record in records:
        active_ids.update(parse_memory_ids(record.get("memory_ids", [])))
    if not active_ids:
        return [], 0, 0
    tag_result = memory_service.get_memories_by_tag(
        tag=TAG_DOMAIN_OFFICIAL, include_archived=True,
    )
    memories = tag_result.get("memories", [])
    all_kb_ids: list[str] = [
        str(memory["id"])
        for memory in memories
        if isinstance(memory, dict) and memory.get("id")
    ]
    orphan_ids = [mid for mid in all_kb_ids if mid not in active_ids]
    return orphan_ids, len(active_ids), len(all_kb_ids)


def purge_orphaned_chunks(
    state_service: Any,
    memory_service: Any,
    *,
    confirm: bool = False,
    batch_size: int = 5000,
    max_batches: int | None = None,
) -> dict[str, Any]:
    """Operator-fired orphan-cleanup verb (NEVER auto-fired on spawn path).

    Per W5.P §4.6: replaces the prior spawn-path auto-purge that blew the
    600s cutover budget on 2026-06-13. Hard-delete semantics per the §2.4
    reframing — KB chunks are system-managed indexed text, not cognitive
    memories. Each batch runs in its own state-service transaction; vector
    deletion is best-effort outside the transaction per §3.1 Option A.

    Modes:
    - ``confirm=False`` (default): dry-run; returns count + sample.
    - ``confirm=True, batch_size=N``: full cleanup in N-row batches.
    - ``confirm=True, batch_size=N, max_batches=K``: bounded run of
      ``min(N*K, total)`` orphans per invocation.
    """
    orphan_ids, active_count, total = _find_orphan_chunk_ids(state_service, memory_service)
    if not orphan_ids:
        logger.info(
            "%s: no orphaned chunks (active=%d, total=%d)",
            PLUGIN_NAME, active_count, total,
        )
        return {
            "status": "completed",
            "orphan_count": 0,
            "active_count": active_count,
            "total_chunk_count": total,
        }
    if not confirm:
        return {
            "status": "dry_run",
            "orphan_count": len(orphan_ids),
            "active_count": active_count,
            "total_chunk_count": total,
            "sample_ids": orphan_ids[:10],
        }
    logger.info(
        "%s: purging %d orphaned chunks (active=%d, total=%d, batch_size=%d)",
        PLUGIN_NAME, len(orphan_ids), active_count, total, batch_size,
    )
    deleted = 0
    batch_count = 0
    for batch_start in range(0, len(orphan_ids), batch_size):
        if max_batches is not None and batch_count >= max_batches:
            break
        batch = orphan_ids[batch_start : batch_start + batch_size]
        delete_kb_chunks(batch, memory_service)
        deleted += len(batch)
        batch_count += 1
        logger.info(
            "%s: purge batch %d (deleted=%d of %d total orphans)",
            PLUGIN_NAME, batch_count, deleted, len(orphan_ids),
        )
    return {
        "status": "completed",
        "orphan_count": deleted,
        "batches_run": batch_count,
        "active_count": active_count,
        "total_chunk_count": total,
    }


# ---------------------------------------------------------------------------
# Auto-install
# ---------------------------------------------------------------------------

def materialize_missing_plugin_kb_symlinks(
    kb_root: Path,
    manifest_plugin_set: set[str] | None,
) -> list[str]:
    """Create the ``kb_root`` entries that the genesis symlink sweep never made.

    Returns the plugin names materialised, in the order created.

    THE BUG THIS CLOSES. ``auto_install_knowledge_bases`` ran its two directions
    off two different sources of truth: removal is driven by the LIVE MANIFEST,
    while addition built its candidates from ``sorted(kb_root.iterdir())`` — the
    FILESYSTEM SYMLINK SET, a genesis artifact written once and never revisited.
    So a manifest-enabled plugin shipping a ``knowledge_base/`` with no symlink
    was not a candidate the loop rejected; it was not a candidate at all, and the
    manifest gate, ``is_source_stale`` and ``install_kb`` were all unreachable for
    it. The asymmetry survived because at genesis the two sources agree by
    construction. Every plugin added post-birth had KB content on disk, no
    symlink, and no index — and retrieval does not degrade gracefully when a
    corpus is missing, it answers confidently from whatever IS indexed.

    KEYED ON THE RESOLVED TARGET, NEVER THE ENTRY NAME. This is the whole safety
    argument. ``knowledge_bases/`` entry names are descriptive and need not match
    their plugin: ``default_thinking_plugin`` ships four DISTINCT corpora indexed
    as ``thinking_plans``, ``authored_joseki``, ``thinking_playbooks`` and
    ``plan_templates``. A name-keyed loop sees no ``knowledge_bases/
    default_thinking_plugin`` entry and creates a fifth link duplicating the
    corpus ``thinking_plans`` already indexes. Comparing resolved targets makes
    that case disappear without an alias registry — and the removal side already
    resolves for exactly this reason (``_resolve_owning_plugin``), so this
    applies the module's own existing convention to the direction that lacked it.

    ``manifest_plugin_set is None`` MATERIALISES NOTHING. Fail closed. A None
    manifest means "skip manifest filtering", which is harmless for removal (it
    does less) but INVERTS for addition: a manifest-unaware caller would link and
    index every plugin on disk, including ones deliberately absent from the
    manifest. Doing less on an unknown is the only safe direction here.

    Deliberately service-free — filesystem and manifest only. Indexing needs
    services; deciding WHICH links are missing does not. Splitting them keeps the
    resolved-target rule unit-testable with no services at all, and lets a
    pre-commit harness invoke the real selection logic without a running
    platform.
    """
    if manifest_plugin_set is None or not kb_root.is_dir():
        return []
    plugins_dir = kb_root.parent / "plugins"
    if not plugins_dir.is_dir():
        return []
    existing_targets = _existing_kb_targets(kb_root)
    created: list[str] = []
    for plugin_name in sorted(manifest_plugin_set):
        kb_dir = _unlinked_plugin_kb(plugins_dir, plugin_name, existing_targets)
        if kb_dir is None:
            continue
        if not _create_kb_symlink(kb_root, plugin_name, kb_dir):
            continue
        existing_targets.add(kb_dir.resolve())
        created.append(plugin_name)
    return created


def _existing_kb_targets(kb_root: Path) -> set[Path]:
    """Every directory already reachable from ``kb_root``, by RESOLVED path.

    Resolution is what makes entry names irrelevant: a corpus indexed under a
    descriptive alias is still reachable, so it is not missing.
    """
    targets: set[Path] = set()
    for entry in kb_root.iterdir():
        if entry.name.startswith("."):
            continue
        try:
            targets.add(entry.resolve())
        except OSError:
            logger.warning(
                "%s: kb_root entry %s could not be resolved; ignoring it as a "
                "target (a stale link must not suppress a real candidate)",
                PLUGIN_NAME, entry.name,
            )
    return targets


def _unlinked_plugin_kb(
    plugins_dir: Path, plugin_name: str, existing_targets: set[Path],
) -> Path | None:
    """The plugin's ``knowledge_base/`` iff it ships content nothing links to."""
    kb_dir = plugins_dir / plugin_name / "knowledge_base"
    if not kb_dir.is_dir() or not _has_content(kb_dir):
        return None
    try:
        resolved = kb_dir.resolve()
    except OSError:
        return None
    return None if resolved in existing_targets else kb_dir


def _create_kb_symlink(kb_root: Path, plugin_name: str, kb_dir: Path) -> bool:
    """Create ``kb_root/<plugin_name>`` -> ``kb_dir``. True iff created.

    Never clobbers an existing entry: if the name is taken by something
    resolving elsewhere, the operator's mapping outranks this repair, and
    silently re-pointing an indexed corpus would be worse than the gap.
    """
    link = kb_root / plugin_name
    if link.exists() or link.is_symlink():
        logger.warning(
            "%s: %s ships KB content with no resolving entry, but the name %s "
            "is already taken by a DIFFERENT target — skipping rather than "
            "replacing it", PLUGIN_NAME, plugin_name, link.name,
        )
        return False
    relative = os.path.relpath(kb_dir, kb_root)
    try:
        link.symlink_to(Path(relative))
    except OSError as exc:
        logger.error(
            "%s: could not materialise KB symlink for %s: %s",
            PLUGIN_NAME, plugin_name, exc,
        )
        return False
    # Logged at INFO with the plugin name so the working-tree change this makes
    # in a git checkout is attributable the moment it appears.
    logger.info(
        "%s: materialised missing KB symlink: %s -> %s",
        PLUGIN_NAME, plugin_name, relative,
    )
    return True


def _has_content(kb_dir: Path) -> bool:
    """True iff ``kb_dir`` holds at least one regular file (at any depth).

    "Non-empty" means real content, not merely an existing directory: an empty
    or directory-only ``knowledge_base/`` has nothing to index, and linking it
    would create tracked repo churn for no retrieval benefit.
    """
    try:
        return any(path.is_file() for path in kb_dir.rglob("*"))
    except OSError:
        return False


def auto_install_knowledge_bases(
    kb_root: Path,
    state_service: Any,
    memory_service: Any,
    address_book_service: Any,
    manifest_plugin_set: set[str] | None = None,
) -> None:
    """Scan knowledge_base_root for directories with manifests and install if needed.

    When ``manifest_plugin_set`` is supplied, this also runs a
    complementary auto-uninstall pass beforehand: any existing install
    record whose KB is plugin-owned (resolved path under
    ``plugins/<name>/`` AND KB name == plugin name) and whose owning
    plugin is NOT in the manifest set is uninstalled (record deleted,
    chunk memories forgotten). The install loop then also skips
    plugin-owned KB directories whose plugin is absent from the
    manifest, so a leftover symlink doesn't re-create the install
    record that was just removed. This keeps the KB index in sync with
    the active plugin manifest so agents don't find documentation for
    verbs that aren't loaded.

    The two directions are now genuinely symmetric — both manifest-driven.
    They were NOT before: removal read the live manifest while addition
    read ``kb_root.iterdir()``, the genesis symlink set, so a
    manifest-enabled plugin with no symlink was invisible to this sweep
    entirely. :func:`materialize_missing_plugin_kb_symlinks` closes that
    and runs FIRST, so anything it creates falls through to the unchanged
    install path below instead of needing a parallel one.

    Passing ``manifest_plugin_set=None`` skips the manifest-aware
    filtering (preserves prior behaviour for callers that don't have
    manifest awareness — e.g. legacy call sites and tests that build
    fixture state without a manifest). It also materialises NOTHING: for
    addition, "no manifest" must mean "do less", never "link every plugin
    on disk".
    """
    if not kb_root.is_dir():
        logger.info("%s: auto-install skipped (kb_root=%s)", PLUGIN_NAME, kb_root)
        return

    if manifest_plugin_set is not None:
        _uninstall_orphaned_plugin_kbs(
            manifest_plugin_set, state_service, memory_service,
        )

    # Repair the addition side's source of truth BEFORE building candidates:
    # anything materialised here becomes an ordinary kb_root entry below and
    # takes the existing manifest-gate / staleness / install_kb path unchanged.
    materialize_missing_plugin_kb_symlinks(kb_root, manifest_plugin_set)

    candidates = [
        entry.name
        for entry in sorted(kb_root.iterdir())
        if entry.is_dir() and not entry.name.startswith(".")
    ]

    for name in candidates:
        try:
            if _is_orphaned_plugin_candidate(kb_root / name, name, manifest_plugin_set):
                logger.info(
                    "%s: auto-install skipped (plugin not in manifest): %s",
                    PLUGIN_NAME, name,
                )
                continue
            if has_valid_install(name, state_service, memory_service, kb_root):
                logger.info("%s: auto-install skipped (valid): %s", PLUGIN_NAME, name)
                continue
            logger.info("%s: auto-installing: %s", PLUGIN_NAME, name)
            install_kb(
                name, None, kb_root,
                state_service, memory_service, address_book_service,
            )
        except Exception as exc:
            logger.error("%s: auto-install failed for %s: %s", PLUGIN_NAME, name, exc)


_INGEST_ALL: Final = "all"


def ingest_kb(
    name: str,
    kb_root: Path,
    state_service: Any,
    memory_service: Any,
    address_book_service: Any,
) -> dict[str, Any]:
    """Content-hash-gated idempotent ingest of one KB, or every KB when name == "all".

    Single name: skip when ``has_valid_install`` reports the KB current (returned
    under ``unchanged``); otherwise ``install_kb`` re-indexes it. A single named KB
    fails loud — the underlying exception propagates.

    ``"all"``: scan ``kb_root`` and apply the same per-KB gate to each directory —
    the install pass only, NO manifest-aware orphan uninstall (that is a boot /
    ``uninstall`` concern). Per-KB failures are collected into ``failed`` and the
    batch continues; ``status`` is ``"partial"`` when any KB failed, ``"success"``
    otherwise. Mirrors ``auto_install_knowledge_bases`` resilience but returns a
    structured, dispatch-friendly result instead of ``None``.
    """
    if name == _INGEST_ALL:
        return _ingest_all_kbs(kb_root, state_service, memory_service, address_book_service)
    return _ingest_one_kb(name, kb_root, state_service, memory_service, address_book_service)


def _ingest_one_kb(
    name: str,
    kb_root: Path,
    state_service: Any,
    memory_service: Any,
    address_book_service: Any,
) -> dict[str, Any]:
    """Ingest a single named KB (fail-loud); skip-if-current via has_valid_install."""
    if has_valid_install(name, state_service, memory_service, kb_root):
        return _ingest_result("single", [], [name], [], 0)
    result = install_kb(
        name, None, kb_root, state_service, memory_service, address_book_service,
    )
    return _ingest_result("single", [name], [], [], int(result.get("chunk_count") or 0))


def _ingest_all_kbs(
    kb_root: Path,
    state_service: Any,
    memory_service: Any,
    address_book_service: Any,
) -> dict[str, Any]:
    """Ingest every KB under kb_root (install pass only); collect per-KB failures."""
    ingested: list[str] = []
    unchanged: list[str] = []
    failed: list[dict[str, str]] = []
    total_chunks = 0
    for name in _scan_kb_candidates(kb_root):
        try:
            if has_valid_install(name, state_service, memory_service, kb_root):
                unchanged.append(name)
                continue
            result = install_kb(
                name, None, kb_root, state_service, memory_service, address_book_service,
            )
            ingested.append(name)
            total_chunks += int(result.get("chunk_count") or 0)
        except Exception as exc:
            logger.error("%s: ingest failed for %s: %s", PLUGIN_NAME, name, exc)
            failed.append({"name": name, "error": str(exc)})
    return _ingest_result("all", ingested, unchanged, failed, total_chunks)


def _scan_kb_candidates(kb_root: Path) -> list[str]:
    """Directory names under kb_root eligible for ingest (mirrors auto_install)."""
    if not kb_root.is_dir():
        return []
    return [
        entry.name
        for entry in sorted(kb_root.iterdir())
        if entry.is_dir() and not entry.name.startswith(".")
    ]


def _ingest_result(
    mode: str,
    ingested: list[str],
    unchanged: list[str],
    failed: list[dict[str, str]],
    total_chunks: int,
) -> dict[str, Any]:
    """Assemble the ingest return dict; ``status`` is 'partial' iff any KB failed."""
    return {
        "status": "partial" if failed else "success",
        "mode": mode,
        "ingested": ingested,
        "unchanged": unchanged,
        "failed": failed,
        "total_chunks": total_chunks,
    }


def _is_orphaned_plugin_candidate(
    kb_dir: Path, kb_name: str, manifest_plugin_set: set[str] | None,
) -> bool:
    """True iff ``kb_dir`` is plugin-owned and its plugin left the manifest."""
    if manifest_plugin_set is None:
        return False
    owning = _resolve_owning_plugin(kb_dir, kb_name)
    return owning is not None and owning not in manifest_plugin_set


def _owning_plugin_from_path(resolved_path: str, kb_name: str) -> str | None:
    """Return the owning plugin name if ``resolved_path`` is under
    ``plugins/<name>/`` AND ``kb_name == <name>``; else None.

    Conservative match: requires the KB name to equal the plugin
    directory name. KBs that don't follow that convention opt out of
    the auto-uninstall path and rely on explicit
    ``knowledge_service::uninstall`` instead.
    """
    if not resolved_path:
        return None
    parts = Path(resolved_path).parts
    if "plugins" not in parts:
        return None
    plugins_idx = parts.index("plugins")
    if plugins_idx + 1 >= len(parts):
        return None
    plugin_name = parts[plugins_idx + 1]
    return plugin_name if plugin_name == kb_name else None


def _resolve_owning_plugin(kb_dir: Path, kb_name: str) -> str | None:
    """Resolve ``kb_dir`` (may be a symlink) and check for plugin ownership."""
    try:
        resolved = kb_dir.resolve()
    except OSError:
        return None
    return _owning_plugin_from_path(str(resolved), kb_name)


def _uninstall_orphaned_plugin_kbs(
    manifest_plugin_set: set[str],
    state_service: Any,
    memory_service: Any,
) -> int:
    """Uninstall plugin-owned KB records whose plugin left the manifest.

    Returns the count of uninstalled records. Failures on individual
    records are logged but do not abort the sweep — startup must not
    fail on a transient uninstall problem; the operator can re-issue
    ``knowledge_service::uninstall`` explicitly if needed.
    """
    result = state_service.read_state(
        namespace=PLUGIN_NAME,
        query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"is_active": 1}},
    )
    records: list[dict[str, Any]] = result.get("data", {}).get("records", [])
    uninstalled = 0
    for record in records:
        kb_name = record.get("name")
        if not isinstance(kb_name, str):
            continue
        resolved_path = record.get("resolved_path")
        if not isinstance(resolved_path, str):
            continue
        owning = _owning_plugin_from_path(resolved_path, kb_name)
        if owning is None or owning in manifest_plugin_set:
            continue
        try:
            uninstall_kb(
                kb_name,
                remove_files=False,
                state_service=state_service,
                memory_service=memory_service,
            )
            uninstalled += 1
            logger.info(
                "%s: auto-uninstalled orphaned plugin KB %s (plugin not in manifest)",
                PLUGIN_NAME, kb_name,
            )
        except Exception as exc:
            logger.error(
                "%s: auto-uninstall failed for %s: %s", PLUGIN_NAME, kb_name, exc,
            )
    if uninstalled:
        logger.info(
            "%s: auto-uninstall pass removed %d orphaned plugin KB(s)",
            PLUGIN_NAME, uninstalled,
        )
    return uninstalled


# ---------------------------------------------------------------------------
# Install / uninstall / update
# ---------------------------------------------------------------------------

def install_kb(
    name: str,
    source: str | None,
    kb_root: Path,
    state_service: Any,
    memory_service: Any,
    address_book_service: Any,
) -> dict[str, Any]:
    """Index a knowledge base directory.

    Per D4 (operator-locked 2026-06-21): old-chunk cleanup routes through the
    owning service — ``memory_service.delete_memories_by_tag('knowledge:<kb>')``
    (deletes the chunk-memories AND their embeddings, vector-first) — best-effort
    OUTSIDE the install-record transaction (no cross-service atomicity; chunks are
    a regenerable index). The install-record DELETE/INSERT stays in
    ``state_service.transactional()``. ``index_files`` (new-chunk writes via
    memory_service.remember) also runs outside the transaction; the narrow orphan
    window (mid-indexing process death) is bounded by operator-fired
    install/update invocations and swept by the ``purge_orphaned_chunks`` verb.
    """
    kb_dir = kb_root / name

    url, token, indexing_config = resolve_source(name, source, kb_root, address_book_service)

    if url and not kb_dir.exists():
        git_clone(url, kb_dir, token)

    if not kb_dir.exists():
        raise FileNotFoundError(f"Knowledge base directory not found: {kb_dir}")

    source_type = classify_source_type(kb_dir)
    branch: str | None = None
    if source_type == "git":
        git_setup_branch(kb_dir)
        branch = _GIT_BRANCH

    manifest = resolve_manifest(kb_dir, name, indexing_config)

    existing = get_install_record(name, state_service)
    if existing:
        memory_service.delete_memories_by_tag(kb_id_tag(name))
        with state_service.transactional() as txn:
            txn.delete_records(
                namespace=PLUGIN_NAME,
                query={
                    "table": TABLE_KNOWLEDGE_INSTALL,
                    "filters": {"name": name},
                    "soft_delete": False,
                },
            )

    # On partial-index failure the new chunks (tagged knowledge:official, with no
    # active install record yet — the old record was already deleted above and the
    # new one is not inserted until below) are orphans swept by the operator-fired
    # purge_orphaned_chunks verb: the relinquished-atomicity window this slice
    # already relies on (D4). The exception propagates loud; no inline cleanup.
    memory_ids, chunk_count = index_files(kb_dir, name, manifest, memory_service)

    resolved_path = str(kb_dir.resolve())
    clean_source = url if source_type == "git" else None
    last_commit = git_head_sha(kb_dir) if source_type == "git" else None
    now = now_iso()
    record_id = state_service.generate_id(prefix="kin-")

    with state_service.transactional() as txn:
        txn.write_state(
            namespace=PLUGIN_NAME,
            data={
                "table": TABLE_KNOWLEDGE_INSTALL,
                "record": {
                    "id": record_id,
                    "namespace": PLUGIN_NAME,
                    "name": name,
                    "source": clean_source,
                    "source_type": source_type,
                    "resolved_path": resolved_path,
                    "manifest_name": manifest.name,
                    "manifest_tags": manifest.tags,
                    "process_keys": manifest.process_keys,
                    "chunk_count": chunk_count,
                    "memory_ids": memory_ids,
                    "branch": branch,
                    "last_indexed_commit": last_commit,
                    "is_active": 1,
                    "indexed_at": now,
                },
            },
        )

    return {
        "status": "success",
        "name": name,
        "chunk_count": chunk_count,
        "source_type": source_type,
        "manifest_name": manifest.name,
    }


def uninstall_kb(
    name: str,
    remove_files: bool,
    state_service: Any,
    memory_service: Any,
) -> dict[str, Any]:
    """Hard-delete all KB chunks and remove install record.

    Per D4 (operator-locked 2026-06-21): chunk cleanup routes through the owning
    service — ``memory_service.delete_memories_by_tag('knowledge:<kb>')`` (deletes
    chunk-memories AND embeddings, vector-first) — best-effort BEFORE the
    install-record-delete transaction (no cross-service atomicity; chunks are a
    regenerable index, the orphan window is swept by ``purge_orphaned_chunks``).
    The install-record DELETE stays in ``state_service.transactional()``.
    Filesystem cleanup runs after the transaction commits (operator intent —
    leaving files behind on partial DB failure is recoverable; re-installing
    the same KB recovers).
    """
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found in install records")

    memory_ids = parse_memory_ids(record.get("memory_ids", []))

    memory_service.delete_memories_by_tag(kb_id_tag(name))
    with state_service.transactional() as txn:
        txn.delete_records(
            namespace=PLUGIN_NAME,
            query={
                "table": TABLE_KNOWLEDGE_INSTALL,
                "filters": {"name": name},
                "soft_delete": False,
            },
        )

    if remove_files and record.get("source_type") == "git":
        resolved = record.get("resolved_path")
        if resolved:
            shutil.rmtree(resolved, ignore_errors=True)

    return {
        "status": "success",
        "name": name,
        "chunks_archived": len(memory_ids),
    }


def detect_local_changes(
    kb_dir: Path,
    record: dict[str, Any],
    manifest: Any,
) -> list[Path]:
    """Detect locally-modified files by comparing mtime to indexed_at."""
    indexed_at = record.get("indexed_at", "")
    changed: list[Path] = []
    for f in collect_files(kb_dir, manifest):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).isoformat()
        if mtime > indexed_at:
            changed.append(f)
    return changed


def update_kb(
    name: str,
    state_service: Any,
    memory_service: Any,
    address_book_service: Any,
) -> dict[str, Any]:
    """Pull upstream changes (git) or reindex changed files (local).

    Per W5.P §3.1.B + the 2026-06-21 SQL-lockdown cohort: the per-file
    chunk-delete now flows through the OWNING ``memory_service`` verb
    (``delete_kb_chunks_for_file`` → ``delete_memories_by_ids``; no
    foreign-namespace SQL), so only the final install-record UPDATE runs inside
    a ``state_service.transactional()`` block. ``index_files`` runs OUTSIDE the
    transaction (existing memory_service.remember path). Cross-service atomicity
    between the chunk-delete and the record UPDATE is RELINQUISHED (a service
    call cannot join the state transaction); the narrow orphan window is a
    regenerable index, swept via the operator-fired ``purge_orphaned_chunks`` verb.

    SERIALIZATION (no advisory lock — D3/kb GAP-6, 2026-06-21): the per-KB
    ``pg_advisory_xact_lock(hashtext(name)::bigint)`` that previously guarded
    these transactions (W5.P §4.5) was removed as REDUNDANT. The only caller is
    the ``update`` service-interface EDGE verb — synchronous, non-self-completing
    (``is_async`` is not even a ``service_interface_process`` field, so the
    registry blueprint defaults it False) — which the single-threaded
    ActionQueuePoller dispatches one-at-a-time (``_poll_once`` awaits each
    ``execute_action`` inline). Two concurrent ``update(same_name)`` calls
    therefore cannot overlap, and serial dispatch is strictly stronger than the
    lock (which was transaction-scoped and released between the two txns
    anyway — it never gave whole-operation atomicity). TRIPWIRE — this holds
    ONLY while: (a) ``update`` (and the sibling ``edit_file`` / ``create_file``
    / ``delete_file`` that share ``reindex_file``'s locked shape) stay
    ``is_async=False`` / non-self-completing; (b) a single active-color poller
    dispatches them; and (c) every in-process caller of these KB write verbs
    (e.g. ``default_thinking_plugin.create_file``, itself poller-serialized)
    stays poller-serialized. A future ``is_async`` flip, a second concurrent
    dispatcher, or a direct call from an async/threaded/non-poller context
    re-surfaces the need for the per-KB advisory lock.
    """
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found in install records")

    kb_dir = Path(record["resolved_path"])
    source_type = record["source_type"]
    manifest = resolve_manifest(kb_dir, name, None)

    if source_type == "git":
        token = resolve_git_token(name, address_book_service)
        result = fetch_and_merge_git(kb_dir, record, token, manifest, collect_files)
        if isinstance(result, dict):
            result["name"] = name
            return result
        changed_files = result
    else:
        changed_files = detect_local_changes(kb_dir, record, manifest)

    if not changed_files:
        return {"status": "success", "name": name, "files_changed": 0}

    old_memory_ids = parse_memory_ids(record.get("memory_ids", []))

    for cf in changed_files:
        relative = str(cf.relative_to(kb_dir))
        deleted = delete_kb_chunks_for_file(name, relative, memory_service)
        old_memory_ids = [mid for mid in old_memory_ids if mid not in deleted]

    added_ids, _ = index_files(kb_dir, name, manifest, memory_service, changed_files)
    new_memory_ids = old_memory_ids + added_ids

    now = now_iso()
    last_commit = git_head_sha(kb_dir) if source_type == "git" else None

    with state_service.transactional() as txn:
        txn.update_state(
            namespace=PLUGIN_NAME,
            query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"name": name}},
            updates={
                "memory_ids": new_memory_ids,
                "chunk_count": len(new_memory_ids),
                "indexed_at": now,
                "last_indexed_commit": last_commit,
            },
        )

    return {"status": "success", "name": name, "files_changed": len(changed_files)}


def list_installed_kbs(
    active_only: bool, state_service: Any,
) -> dict[str, Any]:
    """List indexed knowledge bases with metadata."""
    filters: dict[str, Any] = {}
    if active_only:
        filters["is_active"] = 1

    result = state_service.read_state(
        namespace=PLUGIN_NAME,
        query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": filters},
    )
    rows = result.get("data", {}).get("records", [])

    installs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        manifest_tags_raw = row.get("manifest_tags", "[]")
        process_keys_raw = row.get("process_keys", "[]")
        installs.append({
            "name": row.get("name"),
            "source": row.get("source"),
            "source_type": row.get("source_type"),
            "manifest_name": row.get("manifest_name"),
            "chunk_count": row.get("chunk_count", 0),
            "is_active": bool(row.get("is_active", 1)),
            "indexed_at": row.get("indexed_at"),
            "branch": row.get("branch"),
            "manifest_tags": (
                json.loads(manifest_tags_raw)
                if isinstance(manifest_tags_raw, str)
                else manifest_tags_raw
            ),
            "process_keys": (
                json.loads(process_keys_raw)
                if isinstance(process_keys_raw, str)
                else process_keys_raw
            ),
        })

    return {"status": "success", "installs": installs, "count": len(installs)}


def activate_kb(name: str, state_service: Any) -> dict[str, Any]:
    """Activate a knowledge base for search inclusion."""
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")
    state_service.update_state(
        namespace=PLUGIN_NAME,
        query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"name": name}},
        updates={"is_active": 1},
    )
    return {"status": "success", "name": name, "is_active": True}


def deactivate_kb(name: str, state_service: Any) -> dict[str, Any]:
    """Deactivate a knowledge base from search results."""
    record = get_install_record(name, state_service)
    if record is None:
        raise FileNotFoundError(f"Knowledge base '{name}' not found")
    state_service.update_state(
        namespace=PLUGIN_NAME,
        query={"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"name": name}},
        updates={"is_active": 0},
    )
    return {"status": "success", "name": name, "is_active": False}
