#!/usr/bin/env python3
"""repo_service search fallback acceptance smoke (no pytest, offline).

`search` used to hard-fail without ripgrep, which nothing in the adopter install
path installs or even mentions — measured across `bootstrap.py`, the hydration
runbook and the bundle README. It now falls back to `git grep`, which genesis
guarantees because it git-inits every born tree.

The legs that matter are the ones that stop this becoming a WORSE defect than the
one it fixes:

  * the fallback must actually RUN with ripgrep absent — the original symptom;
  * it must find the SAME hits a literal query finds under ripgrep, so the
    fallback is not quietly a different search;
  * it must use PCRE, NOT POSIX ERE. Measured on the real repo, `git grep -E`
    agrees with ripgrep on 0% of `\\d` / `\\w` queries — it returns WRONG hits, not
    fewer. A silent downgrade to `-E` would trade a loud failure for a quiet wrong
    answer, which is precisely what the no-silent-fallback contract forbids;
  * a genuine error must still fail LOUDLY through the fallback path, never
    collapse into "no hits".

Run from repo root:
    .venv/bin/python3 plugins/platform_dev_surface_plugin/tests/repo_search_fallback_smoke.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "platform_dev_surface_plugin" / "src"))

from platform_dev_surface_plugin.repo import operations as ops_mod  # noqa: E402
from platform_dev_surface_plugin.repo.errors import RepoToolError  # noqa: E402
from platform_dev_surface_plugin.repo.operations import RepoOperations  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


class _FakeStore:
    def store(self, **_: object) -> str:
        return "patch-fake"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                   timeout=60, check=True)


def _fixture(tmp: Path) -> Path:
    """A git worktree, because that is what the fallback is entitled to assume."""
    root = tmp / "wt"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text(
        "TOKEN_ALPHA = 1\nexit 42\nvalue = 7\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text(
        "TOKEN_ALPHA = 2\nno digits here\n", encoding="utf-8")
    _git(root, "-c", "init.defaultBranch=main", "init", "-q")
    _git(root, "config", "user.email", "t@localhost")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture")
    return root


class _NoRipgrep:
    """Make `which('rg')` report absent, leaving every other lookup intact."""

    def __init__(self) -> None:
        self._real = ops_mod.shutil.which

    def __enter__(self) -> None:
        def fake(cmd: str, *a: object, **k: object) -> str | None:
            return None if cmd == "rg" else self._real(cmd, *a, **k)  # type: ignore[arg-type]
        ops_mod.shutil.which = fake  # type: ignore[assignment]
        ops_mod._rg_qualification = None  # noqa: SLF001

    def __exit__(self, *_: object) -> None:
        ops_mod.shutil.which = self._real  # type: ignore[assignment]
        ops_mod._rg_qualification = None  # noqa: SLF001


def _check_primary(ops: RepoOperations) -> set[tuple[str, int]]:
    """The ripgrep path, when this machine has ripgrep. Returns its hit set."""
    res = ops.search("TOKEN_ALPHA")
    _check(res["engine"] == "ripgrep", "ripgrep is used when present", str(res["engine"]))
    _check(isinstance(res.get("engine_reason"), str) and bool(res["engine_reason"]),
           "the primary path declares its engine too (no asymmetry to infer from)")
    baseline = {(h["path"], h["line"]) for h in res["hits"]}
    _check(len(baseline) == 2, "baseline finds both literal hits", str(baseline))
    return baseline


def _check_fallback_declares(ops: RepoOperations, baseline: set[tuple[str, int]],
                             rg_available: bool) -> None:
    """The original symptom, and the house rule: DECLARED, never silent."""
    res = ops.search("TOKEN_ALPHA")
    _check(res["engine"] == "git-grep", "falls back to git grep when rg is absent",
           str(res["engine"]))
    reason = res.get("engine_reason")
    _check(isinstance(reason, str) and "ripgrep absent from PATH" in reason,
           "the fallback DECLARES itself and its reason in the envelope", str(reason)[:90])
    _check(isinstance(reason, str) and "superset" in reason,
           "the declaration states how the fallback's results differ")
    fallback = {(h["path"], h["line"]) for h in res["hits"]}
    _check(len(fallback) == 2, "fallback finds both literal hits", str(fallback))
    if rg_available:
        _check(fallback == baseline,
               "fallback returns the SAME hits as ripgrep for a literal query",
               f"rg={baseline} gg={fallback}")


def _check_fallback_semantics(ops: RepoOperations) -> None:
    """PCRE not ERE, empty-is-valid, and errors still loud."""
    res = ops.search(r"exit \d+")
    digits = {(h["path"], h["line"]) for h in res["hits"]}
    _check(digits == {("pkg/a.py", 2)},
           r"fallback honours PCRE \d (an -E downgrade would return the WRONG hits)",
           str(digits))

    res = ops.search("NOTHING_MATCHES_THIS_TOKEN")
    _check(res["hit_count"] == 0 and res["hits"] == [],
           "fallback: no matches is an empty result, not a failure")

    raised = False
    try:
        ops.search("(?<BROKEN")
    except RepoToolError:
        raised = True
    _check(raised, "fallback: a malformed pattern raises, never collapses to no-hits")


def _check_hung_qualification_fallback(ops: RepoOperations, tmp: Path) -> None:
    """A PATH-present ripgrep that hangs before it can run must not reach the 60s search bound."""
    fake_bin = tmp / "hung-bin"
    fake_bin.mkdir()
    fake_rg = fake_bin / "rg"
    fake_rg.write_text("#!/bin/sh\nsleep 1000\n", encoding="utf-8")
    fake_rg.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{old_path}"
    ops_mod._rg_qualification = None  # noqa: SLF001
    try:
        res = ops.search("TOKEN_ALPHA")
    finally:
        os.environ["PATH"] = old_path
        ops_mod._rg_qualification = None  # noqa: SLF001
    _check(res["engine"] == "git-grep", "hung qualification falls back to git grep", str(res["engine"]))
    reason = res.get("engine_reason")
    _check(
        isinstance(reason, str) and "hung past 2s" in reason,
        "hung qualification is declared in the search envelope",
        str(reason),
    )


def _check_primary_timeout_fallback(ops: RepoOperations, tmp: Path) -> None:
    """A qualified primary that hangs on search must declare its per-call fallback."""
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    fake_rg = fake_bin / "rg"
    fake_rg.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo fake-rg; exit 0; fi\nsleep 3\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)
    old_path, old_timeout = os.environ.get("PATH", ""), ops_mod._RG_TIMEOUT  # noqa: SLF001
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{old_path}"
    ops_mod._RG_TIMEOUT = 1  # noqa: SLF001
    ops_mod._rg_qualification = None  # noqa: SLF001
    try:
        res = ops.search("TOKEN_ALPHA")
    finally:
        os.environ["PATH"] = old_path
        ops_mod._RG_TIMEOUT = old_timeout  # noqa: SLF001
        ops_mod._rg_qualification = None  # noqa: SLF001
    _check(res["engine"] == "git-grep", "qualified primary timeout falls back to git grep", str(res["engine"]))
    reason = res.get("engine_reason")
    _check(
        isinstance(reason, str) and "qualified usable but timed out after 1s" in reason,
        "primary timeout is declared in the search envelope",
        str(reason),
    )


def main() -> int:
    print("repo_service search fallback smoke")
    print("=" * 62)
    with tempfile.TemporaryDirectory(prefix="search_fallback_") as tmp:
        ops = RepoOperations(_fixture(Path(tmp)), _FakeStore())  # type: ignore[arg-type]
        rg_available = shutil.which("rg") is not None
        baseline: set[tuple[str, int]] = set()
        if rg_available:
            baseline = _check_primary(ops)
        else:
            print("  ....  ripgrep baseline NOT RUN — no rg on this machine to compare against")
        with _NoRipgrep():
            _check_fallback_declares(ops, baseline, rg_available)
            _check_fallback_semantics(ops)
        _check_hung_qualification_fallback(ops, Path(tmp))
        _check_primary_timeout_fallback(ops, Path(tmp))

    print("=" * 62)
    if _failed:
        print(f"FAIL: {_passed} passed, {len(_failed)} failed")
        return 1
    print(f"PASS: {_passed} search-fallback checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
