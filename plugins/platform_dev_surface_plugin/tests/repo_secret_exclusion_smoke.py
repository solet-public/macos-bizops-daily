#!/usr/bin/env python3
"""repo_service secret-exclusion acceptance smoke (no pytest, offline).

Covers B3 §5's ``repo_secret_exclusion_smoke`` row + Rev-A ASK-4 (the denylist is
LOAD-BEARING because profile/ and the git dir are INSIDE the confinement root):

* denylist (control 2): a denylisted path (git dir, profile/ secrets, key files)
  is a TYPED rejection, never a silently-empty result; list_files EXCLUDES them.
* secret scrub (control 3, Q2): read_file REFUSES a whole file that carries a
  planted canary credential; search REDACTS the canary in the returned snippet.

RED-FIRST: the canary is a real credential shape — the read-refusal and
snippet-redaction assertions fail if the scrub is removed (the canary leaks).

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/repo_secret_exclusion_smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.repo.errors import (  # noqa: E402
    RepoDenylistError,
    RepoSecretError,
)
from platform_dev_surface_plugin.repo.operations import RepoOperations  # noqa: E402

_GIT_DIR = "." + "git"
# A realistic credential shape (Anthropic key) — planted, not real.
_CANARY = "sk-ant-api03-" + "CANARYSECRET1234567890abcdef"

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


class _FakeStore:
    def store(self, **_: object) -> str:
        return "patch-fake"


def _git_init(root: Path) -> None:
    """Make the fixture a git worktree — the shape this service actually runs in.

    `platform_dev_surface_plugin` operates on a worktree by construction (genesis
    git-inits every born tree), and since 2026-08-08 `search` falls back to the
    git-native grep when ripgrep is absent. A non-worktree fixture therefore tested
    the service outside its own contract and could not exercise the fallback path
    at all — it failed with "not a git repository" on exactly the node/rg-less
    machines the fallback exists for.
    """
    run = __import__("subprocess").run
    for args in (["-c", "init.defaultBranch=main", "init", "-q"],
                 ["config", "user.email", "t@localhost"], ["config", "user.name", "t"],
                 ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"]):
        run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=True)


def _build_fixture() -> Path:
    root = Path(tempfile.mkdtemp(prefix="secret_fixture_"))
    (root / "normal.py").write_text("x = 1  # clean file\n", encoding="utf-8")
    (root / _GIT_DIR).mkdir()
    (root / _GIT_DIR / "config").write_text("[core]\n", encoding="utf-8")
    (root / "profile").mkdir()
    (root / "profile" / "secrets.txt").write_text("top secret\n", encoding="utf-8")
    (root / "config.key").write_text("key material\n", encoding="utf-8")
    # Fragmented (not written whole): a realistic PEM header, planted only for
    # denylist-by-filename realism (never read back or content-scanned by
    # this smoke) — but the seal validator scans shipped bytes for exactly
    # this pattern, so it must be assembled rather than appear as a literal.
    (root / "secret.pem").write_text(
        "-----BEGIN " + "PRIVATE KEY-----\n", encoding="utf-8"
    )
    (root / "id_rsa").write_text("ssh key\n", encoding="utf-8")
    # A symlink whose TARGET is the denylisted profile/ dir (Rev-A's angle:
    # the denylist runs on the REALPATH, so this must reject too).
    os.symlink(root / "profile", root / "cfg_link")
    (root / "has_secret.py").write_text(
        f"CANARY_MARKER = 1\nAPI_KEY = {_CANARY}\n", encoding="utf-8"
    )
    _git_init(root)
    return root


def _test_denylist(ops: RepoOperations) -> None:
    _check(bool(ops.read_file("normal.py")["content"]), "clean in-root file reads fine")
    for deny in [f"{_GIT_DIR}/config", "profile/secrets.txt", "config.key"]:
        try:
            ops.read_file(deny)
        except RepoDenylistError:
            _check(True, f"read_file denylist TYPED-reject (not empty): {deny}")
            continue
        _check(False, f"read_file denylist TYPED-reject (not empty): {deny}")
    entries = {e["path"] for e in ops.list_files(".", depth=2)["entries"]}
    _check(not ({_GIT_DIR, "profile", "config.key"} & entries),
           "list_files EXCLUDES denylisted entries (git dir / profile / key file)")


def _test_case_variants(ops: RepoOperations) -> None:
    # F2 RED-FIRST: on a case-insensitive FS the denylist must reject case
    # variants (normalized NFC+casefold both sides). Reverting to exact-match
    # lets these dodge the denylist and the FS serves the real profile/ / secrets.
    variants = ["Profile/secrets.txt", _GIT_DIR.upper() + "/config", "SECRET.PEM", "Id_Rsa"]
    for variant in variants:
        try:
            ops.read_file(variant)
        except RepoDenylistError:
            _check(True, f"case-variant TYPED-rejected (F2): {variant}")
            continue
        _check(False, f"case-variant TYPED-rejected (F2): {variant}")


def _test_symlink_target(ops: RepoOperations) -> None:
    # Rev-A angle: the denylist runs on the REALPATH — a symlink whose target
    # lands in profile/ must reject even though the link name is benign.
    try:
        ops.read_file("cfg_link/secrets.txt")
    except RepoDenylistError:
        _check(True, "symlink whose TARGET is profile/ is denylist-rejected (realpath denylist)")
        return
    _check(False, "symlink whose TARGET is profile/ is denylist-rejected (realpath denylist)")


def _test_read_refuse(ops: RepoOperations) -> None:
    try:
        ops.read_file("has_secret.py")
    except RepoSecretError:
        _check(True, "read_file REFUSES a file carrying a credential shape (Q2)")
        return
    _check(False, "read_file REFUSES a file carrying a credential shape (Q2)")


def _test_search_redact(ops: RepoOperations) -> None:
    # Search a token ON the secret-bearing line so the hit snippet contains the canary.
    hits = ops.search("API_KEY")["hits"]
    secret_hits = [h for h in hits if "has_secret.py" in h["path"]]
    _check(bool(secret_hits), "search returns the hit on the secret-bearing line (non-vacuous)")
    _check(all(_CANARY not in h["snippet"] for h in secret_hits),
           "the raw canary is NEVER present in the returned snippet")
    _check(all("[REDACTED" in h["snippet"] for h in secret_hits),
           "the secret span is redacted in the snippet")


def main() -> int:
    print("repo_service secret-exclusion smoke")
    root = _build_fixture()
    ops = RepoOperations(root, _FakeStore())
    _test_denylist(ops)
    _test_case_variants(ops)
    _test_symlink_target(ops)
    _test_read_refuse(ops)
    _test_search_redact(ops)
    shutil.rmtree(root, ignore_errors=True)
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
