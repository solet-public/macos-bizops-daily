#!/usr/bin/env python3
"""Smoke — file-ops write postures (W4), archive_file lifecycle (W11), and
hash-preconditioned edit + prior-version snapshot (W12).

Drives the real ``kb_file_ops`` verbs against a temp KB directory (real manifest,
real title_block chunking) with in-memory state/memory fakes at the service
boundary — the same fake-source / real-logic split the sibling ingest smoke uses.
Covers posture rejection, §4-block validation on create, the git-safety pin
(symlink KB never invokes a git verb), create-then-index single-chunk, the CAS
edit round-trip (hash success → snapshot + re-key; stale hash → fail-loud,
untouched; missing hash gated by posture), the archive round-trip (move + §4
stamp + chunk re-key + install-record consistency + zero orphans + refusals), the
protected-path guard, and the code-level ``.doc_history`` collect exclusion.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/file_ops_posture_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

import default_knowledge_plugin.kb_file_ops as fo  # noqa: E402
from default_knowledge_plugin.constants import DOC_HISTORY_DIRNAME, kb_id_tag  # noqa: E402
from default_knowledge_plugin.kb_indexing import collect_files, now_iso  # noqa: E402
from default_knowledge_plugin.models import Manifest  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _expect_raises(fn: Any, exc_type: type[BaseException], label: str) -> None:
    try:
        fn()
    except exc_type:
        _check(True, label)
        return
    except BaseException as other:  # noqa: BLE001
        _check(False, f"{label} (raised {type(other).__name__} instead)")
        return
    _check(False, f"{label} (did NOT raise)")


_COMPLIANT = (
    "# Doc Title\n\n"
    "Date: 2026-07-16\n"
    "Author: Claude-A\n"
    "Status: complete\n"
    "Embedding Description: A test document.\n"
    "Summary: Testing create.\n\n"
    "## Body\n\nSome body prose.\n"
)


class _FakeMemory:
    """In-memory chunk store exercising the owning-service verbs kb_file_ops uses."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self._n = 0

    def remember(self, content: str, tags: list[str]) -> dict[str, Any]:
        self._n += 1
        mid = f"mem-{self._n}"
        self.store[mid] = {"id": mid, "content": content, "tags": list(tags)}
        return {"memory_id": mid}

    def get_memories_by_tag(self, tag: str, include_archived: bool = False) -> dict[str, Any]:
        del include_archived
        return {"memories": [m for m in self.store.values() if tag in m["tags"]]}

    def delete_memories_by_ids(self, ids: list[str]) -> None:
        for i in ids:
            self.store.pop(str(i), None)

    def delete_memories_by_tag(self, tag: str) -> None:
        for mid in [m["id"] for m in self.store.values() if tag in m["tags"]]:
            self.store.pop(mid, None)


class _FakeTxn:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def __enter__(self) -> _FakeTxn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> None:
        del namespace, query
        self._record.update(updates)


class _FakeState:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        name = query.get("filters", {}).get("name")
        if name is not None and self.record.get("name") != name:
            return {"data": {"records": []}}
        return {"data": {"records": [self.record]}}

    def transactional(self) -> _FakeTxn:
        return _FakeTxn(self.record)


_MANIFEST_TEMPLATE = """name: {name}
write_posture: {posture}
require_metadata_block: {require_block}
{archive_line}
content:
  chunking:
    strategy: title_block
    max_chars: 3000
"""


def _make_kb(
    name: str,
    posture: str,
    *,
    require_block: bool = False,
    archive_subdir: str | None = "archive",
) -> tuple[Path, _FakeState, _FakeMemory]:
    tmp = tempfile.mkdtemp(prefix="fileops_smoke_")
    kb_dir = Path(tmp)
    archive_line = f"archive_subdir: {archive_subdir}" if archive_subdir else ""
    (kb_dir / "manifest.yaml").write_text(
        _MANIFEST_TEMPLATE.format(
            name=name, posture=posture,
            require_block=str(require_block).lower(), archive_line=archive_line,
        ),
        encoding="utf-8",
    )
    record = {
        "name": name,
        "resolved_path": str(kb_dir.resolve()),
        "source_type": "symlink",
        "memory_ids": [],
        "chunk_count": 0,
        "is_active": 1,
        "indexed_at": now_iso(),
    }
    return kb_dir, _FakeState(record), _FakeMemory()


# --------------------------------------------------------------------------- #
# W4 — posture gating + metadata validation
# --------------------------------------------------------------------------- #

def test_read_only_rejects_all_writes() -> None:
    kb, st, mem = _make_kb("ro", "read_only")
    _expect_raises(
        lambda: fo.create_file_kb("ro", "a.md", _COMPLIANT, st, mem),
        PermissionError, "read_only rejects create_file",
    )
    (kb / "existing.md").write_text(_COMPLIANT, encoding="utf-8")
    _expect_raises(
        lambda: fo.edit_file_kb("ro", "existing.md", "x", st, mem),
        PermissionError, "read_only rejects edit_file",
    )
    _expect_raises(
        lambda: fo.delete_file_kb("ro", "existing.md", st, mem),
        PermissionError, "read_only rejects delete_file",
    )
    # read + browse still work under read_only
    r = fo.read_file_kb("ro", "existing.md", st)
    _check(r["status"] == "success", "read_only still allows read_file")
    b = fo.browse_kb("ro", "", st)
    _check(b["status"] == "success", "read_only still allows browse")


def test_create_only_posture() -> None:
    _kb, st, mem = _make_kb("co", "create_only", require_block=False)
    res = fo.create_file_kb("co", "a.md", _COMPLIANT, st, mem)
    _check(res["action"] == "created", "create_only allows create_file")
    _expect_raises(
        lambda: fo.create_file_kb("co", "a.md", _COMPLIANT, st, mem),
        FileExistsError, "create_only raises FileExistsError on collision",
    )
    _expect_raises(
        lambda: fo.edit_file_kb("co", "a.md", "x", st, mem),
        PermissionError, "create_only rejects edit_file",
    )
    _expect_raises(
        lambda: fo.delete_file_kb("co", "a.md", st, mem),
        PermissionError, "create_only rejects delete_file",
    )


def test_full_posture_unchanged() -> None:
    kb, st, mem = _make_kb("fl", "full", require_block=False)
    fo.create_file_kb("fl", "a.md", "# A\n\nbody\n", st, mem)
    res = fo.edit_file_kb("fl", "a.md", "# A\n\nnew body\n", st, mem)  # no hash → legacy
    _check(res["action"] == "edited", "FULL edit with no hash → legacy behavior (allowed)")
    _check(not (kb / DOC_HISTORY_DIRNAME).exists(), "FULL no-hash edit writes NO snapshot (byte-compat)")
    res2 = fo.delete_file_kb("fl", "a.md", st, mem)
    _check(res2["action"] == "deleted", "FULL allows delete_file")


def test_metadata_validation() -> None:
    _kb, st, mem = _make_kb("mv", "create_and_cas_edit", require_block=True)
    res = fo.create_file_kb("mv", "ok.md", _COMPLIANT, st, mem)
    _check(res["action"] == "created", "compliant §4 doc accepted")
    _expect_raises(
        lambda: fo.create_file_kb("mv", "notitle.md", "Date: x\nAuthor: y\n", st, mem),
        ValueError, "missing title → ValueError",
    )
    _expect_raises(
        lambda: fo.create_file_kb("mv", "partial.md", "# T\n\nDate: 2026\nAuthor: y\n", st, mem),
        ValueError, "missing required keys → ValueError",
    )
    _kb2, st2, mem2 = _make_kb("nov", "full", require_block=False)
    res2 = fo.create_file_kb("nov", "free.md", "# Free\n\nno block needed\n", st2, mem2)
    _check(res2["action"] == "created", "validation skipped when require_metadata_block is False")


# --------------------------------------------------------------------------- #
# W11/W12 — git safety, create-then-index, CAS, archive, protected paths
# --------------------------------------------------------------------------- #

def test_git_safety_pin() -> None:
    _kb, st, mem = _make_kb("git", "create_and_cas_edit", require_block=True)
    calls: list[Any] = []
    original = fo.git_commit_file
    fo.git_commit_file = lambda *a, **k: calls.append((a, k))  # type: ignore[assignment]
    try:
        fo.create_file_kb("git", "a.md", _COMPLIANT, st, mem)
        fo.archive_file_kb("git", "a.md", None, st, mem)
    finally:
        fo.git_commit_file = original  # type: ignore[assignment]
    _check(calls == [], "symlink KB never invokes git_commit_file (create + archive)")


def test_create_then_index_single_chunk() -> None:
    _kb, st, mem = _make_kb("idx", "create_and_cas_edit", require_block=True)
    fo.create_file_kb("idx", "sub/deep.md", _COMPLIANT, st, mem)
    _check(len(mem.store) == 1, "create → exactly ONE new chunk")
    chunk = next(iter(mem.store.values()))
    _check("Read path: sub/deep.md" in chunk["content"], "chunk body carries the KB-relative Read path")
    _check(st.record["chunk_count"] == 1, "install record chunk_count updated to 1")


def test_cas_edit_roundtrip() -> None:
    kb, st, mem = _make_kb("cas", "create_and_cas_edit", require_block=True)
    fo.create_file_kb("cas", "a.md", _COMPLIANT, st, mem)

    read = fo.read_file_kb("cas", "a.md", st)
    _check("content_sha256" in read and len(read["content_sha256"]) == 64, "read_file returns content_sha256")
    good_hash = read["content_sha256"]

    updated = _COMPLIANT.replace("Summary: Testing create.", "Summary: Revised summary.")
    res = fo.edit_file_kb("cas", "a.md", updated, st, mem, expected_content_hash=good_hash)
    _check(res["action"] == "edited", "CAS edit with current hash succeeds")
    snap_dir = kb / DOC_HISTORY_DIRNAME / "a.md"
    _check(snap_dir.is_dir() and len(list(snap_dir.glob("*.md"))) == 1, "prior-version snapshot written under .doc_history")
    _check(len(mem.store) == 1, "CAS edit → exactly one re-keyed chunk")

    # stale hash → fail loud, file + snapshot untouched
    disk_before = (kb / "a.md").read_text(encoding="utf-8")
    snaps_before = len(list(snap_dir.glob("*.md")))
    _expect_raises(
        lambda: fo.edit_file_kb("cas", "a.md", "# clobber\n", st, mem, expected_content_hash=good_hash),
        ValueError, "stale hash → ValueError (lost-update prevented)",
    )
    _check((kb / "a.md").read_text(encoding="utf-8") == disk_before, "stale-hash edit leaves the file untouched")
    _check(len(list(snap_dir.glob("*.md"))) == snaps_before, "stale-hash edit writes NO new snapshot")

    # missing hash under CAS posture → PermissionError
    _expect_raises(
        lambda: fo.edit_file_kb("cas", "a.md", "# blind\n", st, mem),
        PermissionError, "missing hash under create_and_cas_edit → PermissionError",
    )


def test_archive_roundtrip() -> None:
    kb, st, mem = _make_kb("arc", "create_and_cas_edit", require_block=True)
    fo.create_file_kb("arc", "sub/doc.md", _COMPLIANT, st, mem)

    res = fo.archive_file_kb("arc", "sub/doc.md", "archive/sub/successor.md", st, mem)
    _check(res["archived_path"] == "archive/sub/doc.md", "nested source preserves structure under archive/")
    _check(not (kb / "sub" / "doc.md").exists(), "source file removed after archive")
    dest = kb / "archive" / "sub" / "doc.md"
    _check(dest.is_file(), "doc relocated under archive_subdir")
    archived_text = dest.read_text(encoding="utf-8")
    _check("Archived:" in archived_text, "§4 block stamped with Archived date")
    _check("Superseded_by: archive/sub/successor.md" in archived_text, "Superseded_by stamped")
    _check("Status: superseded" in archived_text, "Status flipped to superseded")

    # chunk re-key + install-record consistency + zero orphans
    _check(len(mem.store) == 1, "exactly one chunk after archive (old re-keyed, not duplicated)")
    chunk = next(iter(mem.store.values()))
    _check("Read path: archive/sub/doc.md" in chunk["content"], "re-keyed chunk carries the new archive read path")
    active_ids = set(st.record["memory_ids"])
    kb_tag = kb_id_tag("arc")
    live_ids = {m["id"] for m in mem.store.values() if kb_tag in m["tags"]}
    _check(live_ids == active_ids, "install record memory_ids == live chunk ids (zero orphans post-archive)")
    _check(st.record["chunk_count"] == 1, "chunk_count consistent after archive")

    # re-archiving an already-archived path is refused
    _expect_raises(
        lambda: fo.archive_file_kb("arc", "archive/sub/doc.md", None, st, mem),
        PermissionError, "re-archiving a doc already under archive_subdir → PermissionError",
    )


def test_archive_refusals() -> None:
    # legacy doc without a §4 block gets a minimal block inserted
    kb, st, mem = _make_kb("leg", "full", require_block=False)
    fo.create_file_kb("leg", "old.md", "# Old\n\njust body\n", st, mem)
    res = fo.archive_file_kb("leg", "old.md", None, st, mem)
    archived = (kb / "archive" / "old.md").read_text(encoding="utf-8")
    _check("Archived:" in archived and "# Old" in archived, "legacy doc → minimal §4 block inserted on archive")
    _check(res["action"] == "archived", "legacy archive succeeds")

    # no archive_subdir configured → ValueError
    _kb2, st2, mem2 = _make_kb("noarc", "full", require_block=False, archive_subdir=None)
    fo.create_file_kb("noarc", "a.md", "# A\n\nb\n", st2, mem2)
    _expect_raises(
        lambda: fo.archive_file_kb("noarc", "a.md", None, st2, mem2),
        ValueError, "no archive_subdir configured → ValueError",
    )

    # destination collision → FileExistsError (never overwrite the record)
    kb3, st3, mem3 = _make_kb("col", "full", require_block=False)
    fo.create_file_kb("col", "a.md", "# A\n\nb\n", st3, mem3)
    (kb3 / "archive").mkdir(exist_ok=True)
    (kb3 / "archive" / "a.md").write_text("# preexisting archived\n", encoding="utf-8")
    _expect_raises(
        lambda: fo.archive_file_kb("col", "a.md", None, st3, mem3),
        FileExistsError, "archive destination collision → FileExistsError",
    )


def test_archive_allowed_while_delete_rejected() -> None:
    _kb, st, mem = _make_kb("mix", "create_and_cas_edit", require_block=True)
    fo.create_file_kb("mix", "a.md", _COMPLIANT, st, mem)
    _expect_raises(
        lambda: fo.delete_file_kb("mix", "a.md", st, mem),
        PermissionError, "create_and_cas_edit rejects delete_file",
    )
    res = fo.archive_file_kb("mix", "a.md", None, st, mem)
    _check(res["action"] == "archived", "create_and_cas_edit ALLOWS archive_file (sanctioned retirement)")


def test_protected_path_guard() -> None:
    _kb, st, mem = _make_kb("prot", "full", require_block=False)
    _expect_raises(
        lambda: fo.create_file_kb("prot", "archive/x.md", "# X\n\nb\n", st, mem),
        PermissionError, "create into archive_subdir → PermissionError (bypasses supersession)",
    )
    _expect_raises(
        lambda: fo.create_file_kb("prot", f"{DOC_HISTORY_DIRNAME}/x.md/1.md", "# X\n\nb\n", st, mem),
        PermissionError, "create into .doc_history → PermissionError",
    )


def test_doc_history_excluded_from_collect() -> None:
    kb, _st, _mem = _make_kb("hist", "full", require_block=False)
    manifest = Manifest(name="hist", chunking_strategy="title_block", max_chars=3000)
    (kb / "live.md").write_text("# Live\n\nb\n", encoding="utf-8")
    deep = kb / DOC_HISTORY_DIRNAME / "archive" / "foo.md"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "20260716T000000Z.md").write_text("# Snapshot\n\nb\n", encoding="utf-8")
    collected = {str(p.relative_to(kb)) for p in collect_files(kb, manifest)}
    _check("live.md" in collected, "live doc collected")
    _check(
        not any(DOC_HISTORY_DIRNAME in p for p in collected),
        "deeply-nested .doc_history snapshot excluded at the code level",
    )


def main() -> int:
    print("file-ops posture / archive / CAS smoke")
    print("======================================")
    test_read_only_rejects_all_writes()
    test_create_only_posture()
    test_full_posture_unchanged()
    test_metadata_validation()
    test_git_safety_pin()
    test_create_then_index_single_chunk()
    test_cas_edit_roundtrip()
    test_archive_roundtrip()
    test_archive_refusals()
    test_archive_allowed_while_delete_rejected()
    test_protected_path_guard()
    test_doc_history_excluded_from_collect()
    print(f"\nPASSED: {_passed}\nFAILED: {len(_failed)}")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
