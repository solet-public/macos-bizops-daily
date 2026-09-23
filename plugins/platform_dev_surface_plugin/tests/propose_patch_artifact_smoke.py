#!/usr/bin/env python3
"""propose_patch artifact-only acceptance smoke (no pytest, offline).

Covers B3 §5's ``propose_patch_artifact_smoke`` row (design §2.4): propose_patch
STORES a proposal artifact + a read-only ``git apply --check`` flag and returns a
patch_id — and NO apply path exists anywhere in the service (structurally absent).

RED-FIRST: a diff whose target path escapes the repo root is rejected (the diff
paths are confined) — the pin fails if that confinement is removed. The
"no apply verb" assertions fail if any apply/commit method is ever added.

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/propose_patch_artifact_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from ananta.interfaces.repo_service_interface import RepoServiceInterface  # noqa: E402
from ananta.services.repo_service.interfaces.public import RepoServicePublicAPI  # noqa: E402
from platform_dev_surface_plugin.repo.errors import RepoPathError  # noqa: E402
from platform_dev_surface_plugin.repo.operations import RepoOperations  # noqa: E402
from platform_dev_surface_plugin.repo.patch_store import PatchStore  # noqa: E402

_EXPECTED_VERBS = {"search", "read_file", "list_files", "git_status", "git_diff", "propose_patch"}
_APPLY_LIKE = ("apply", "apply_patch", "commit", "apply_manifest", "write_file", "checkout")

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


class _CapturingStore(PatchStore):
    """A PatchStore that captures store() kwargs instead of persisting (offline smoke).

    Subclasses the REAL PatchStore (not a duck type) so the override stays
    signature-locked to production — if PatchStore.store's contract changes,
    this fake breaks loudly rather than drifting."""

    def __init__(self) -> None:
        self.captured: dict[str, object] | None = None

    def store(
        self, *, unified_diff: str, paths: list[str], applies_cleanly: bool, principal: str,
    ) -> str:
        self.captured = {
            "unified_diff": unified_diff,
            "paths": paths,
            "applies_cleanly": applies_cleanly,
            "principal": principal,
        }
        return "patch-CAPTURED-0001"


def _public_verbs() -> set[str]:
    names: set[str] = set()
    for attr in dir(RepoServicePublicAPI):
        meta = getattr(getattr(RepoServicePublicAPI, attr), "_service_interface_metadata", None)
        if meta is not None:
            names.add(meta.name)
    return names


def _test_stores_artifact() -> None:
    store = _CapturingStore()
    ops = RepoOperations(_REPO_ROOT, store)
    # A fabricated diff won't apply against the live tree — a valid result.
    diff = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
    out = ops.propose_patch(diff, principal="operator:test")
    captured = store.captured or {}
    _check(out["patch_id"] == "patch-CAPTURED-0001", "returns the stored patch_id")
    _check(isinstance(out["applies_cleanly"], bool), "applies_cleanly is a bool (git apply --check)")
    _check(out["path_count"] == 1, "path_count reflects the confined diff targets")
    _check(captured.get("unified_diff") == diff, "artifact store captured the diff (state persistence)")
    _check(captured.get("principal") == "operator:test", "artifact store captured the caller principal (audit)")


def _test_rejects_escaping() -> None:
    ops = RepoOperations(_REPO_ROOT, _CapturingStore())
    escaping = "--- a/x\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n"
    try:
        ops.propose_patch(escaping, principal="operator:test")
    except RepoPathError:
        _check(True, "propose_patch rejects an escaping diff target path (confinement pin)")
        return
    _check(False, "propose_patch rejects an escaping diff target path (confinement pin)")


def _test_rename_copy_diff_git_confinement() -> None:
    """N2 pin: EVERY path in a diff is confined — not just ``--- a/``/``+++ b/``.

    A ``rename to``/``copy to``/``diff --git a/X b/Y`` header can smuggle an
    escaping target with NO ``--- a/``/``+++ b/`` line at all. Both halves are
    red-first against the N2 extraction (``rename from/to`` + ``copy from/to``
    prefixes and ``_git_header_paths``):

    * the benign in-root rename is ACCEPTED only because those headers are
      extracted + confined — neutralize the N2 extraction and it hits the
      empty-``found`` guard instead (canary flips green→red);
    * each escaping header is REJECTED by confinement specifically — the error
      names the OUTSIDE-root target. Checking ``"OUTSIDE"`` is what makes the
      security assertion red-first: neutralize the extraction and the escape is
      never seen, so the empty-``found`` guard raises a *different*
      ``RepoPathError`` (no ``"OUTSIDE"``) and the assertion flips green→red.
      A bare ``except RepoPathError`` would NOT be red-first here, because that
      empty-``found`` guard also raises ``RepoPathError``.
    """
    ops = RepoOperations(_REPO_ROOT, _CapturingStore())

    # Canary: a pure in-root rename references its paths ONLY via the
    # ``diff --git`` header + ``rename from/to`` lines (no ``--- a/``/``+++ b/``).
    # It is accepted only because the N2 extraction sees those headers.
    benign_rename = (
        "diff --git a/oldname.txt b/newname.txt\n"
        "similarity index 100%\n"
        "rename from oldname.txt\n"
        "rename to newname.txt\n"
    )
    try:
        out = ops.propose_patch(benign_rename, principal="operator:test")
        _check(
            out["path_count"] >= 1,
            "N2: in-root rename header paths ARE extracted+confined (accepted, path_count>=1)",
        )
    except RepoPathError as exc:
        _check(False, f"benign in-root rename must be accepted; N2 extraction missing? got {exc}")

    def _assert_confinement_rejects(diff: str, label: str) -> None:
        try:
            ops.propose_patch(diff, principal="operator:test")
        except RepoPathError as exc:
            # "OUTSIDE" distinguishes the confinement rejection (escape was
            # extracted THEN rejected) from the empty-found guard (escape never
            # seen) — both raise RepoPathError, so the type alone is not enough.
            _check("OUTSIDE" in str(exc), f"{label}: escape rejected by CONFINEMENT (names OUTSIDE-root target)")
            return
        _check(False, f"{label}: propose_patch must RAISE on the escaping header target")

    # rename to <escape> — header a/b both in-root, escape ONLY via `rename to`.
    _assert_confinement_rejects(
        "diff --git a/safe.txt b/safe.txt\n"
        "similarity index 100%\n"
        "rename from safe.txt\n"
        "rename to ../../../etc/repo-escape-N2\n",
        "rename to ../ escape",
    )
    # copy to <escape> — escape ONLY via `copy to`.
    _assert_confinement_rejects(
        "diff --git a/safe.txt b/safe.txt\n"
        "similarity index 90%\n"
        "copy from safe.txt\n"
        "copy to ../../../etc/repo-escape-N2\n",
        "copy to ../ escape",
    )
    # diff --git header b-path escape, NO rename/copy lines — pins _git_header_paths alone.
    _assert_confinement_rejects(
        "diff --git a/safe.txt b/../../../etc/repo-escape-N2\n"
        "index 0000000..1111111 100644\n",
        "diff --git b/ header escape",
    )


def _test_no_apply_path() -> None:
    iface_verbs = set(RepoServiceInterface.__abstractmethods__)
    _check(iface_verbs == _EXPECTED_VERBS, f"interface declares exactly the 6 read-only verbs ({sorted(iface_verbs)})")
    _check(_public_verbs() == _EXPECTED_VERBS, "public API exposes exactly the 6 verbs")
    reachable = set(dir(RepoServiceInterface)) | set(dir(RepoOperations))
    apply_leaks = [m for m in _APPLY_LIKE if m in reachable]
    _check(not apply_leaks, f"NO apply/commit/write method is reachable (found {apply_leaks})")


def main() -> int:
    print("propose_patch artifact-only smoke")
    _test_stores_artifact()
    _test_rejects_escaping()
    _test_rename_copy_diff_git_confinement()
    _test_no_apply_path()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
