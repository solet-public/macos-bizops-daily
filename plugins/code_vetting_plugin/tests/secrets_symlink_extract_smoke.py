"""secrets_symlink_extract_smoke.py — tarfile symlink-death fix (self-vet defect B).

Red-first coverage for ``_extract_skipping_unsafe_links`` /
``_tracked_snapshot`` in ``scanners/secrets.py``: the secrets scanner's git
target snapshot used ``archive.extractall(dest, filter="data")`` unguarded,
which raises ``tarfile.AbsoluteLinkError`` (PEP 706's data filter) the moment
ANY tracked member is an absolute or destination-escaping symlink — killing
the whole scan on one bad member rather than skipping it. Measured live
against blob 383f874 (``workbench/2026-07-31_phase0_1_probe/out/latest``),
an absolute symlink pointing inside the checkout itself; that specific blob
is retracked to relative separately (git mutation, Git-Controller-executed),
but the FIX here is general — any tracked absolute/escaping symlink, not
just today's instance.

Run directly: ``.venv/bin/python3 plugins/code_vetting_plugin/tests/secrets_symlink_extract_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import subprocess
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.scanners.secrets import _tracked_snapshot
from code_vetting_plugin.targets import TargetTree

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _build_repo_with_absolute_symlink(root: Path) -> None:
    """A tiny git repo carrying ONE tracked absolute symlink alongside a
    normal file — mirrors the shape of blob 383f874 in miniature."""
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "smoke@example.invalid")
    _git(root, "config", "user.name", "smoke")
    (root / "afile.txt").write_text("hello\n", encoding="utf-8")
    link = root / "escaping_link"
    link.symlink_to("/definitely/not/a/real/path/on/this/machine")
    _git(root, "add", "afile.txt", "escaping_link")
    _git(root, "commit", "-q", "-m", "seed")


def _check_absolute_symlink_does_not_kill_the_scan(base: Path) -> None:
    repo = base / "repo"
    _build_repo_with_absolute_symlink(repo)
    tree = TargetTree.from_git(repo)
    try:
        with _tracked_snapshot(tree) as snapshot:
            _check(
                "extraction completes without raising on an absolute symlink member",
                True,
            )
            _check(
                "the co-tracked normal file survives extraction",
                (snapshot / "afile.txt").read_text(encoding="utf-8") == "hello\n",
            )
            _check(
                "the rejected symlink member is simply absent, not half-written",
                not (snapshot / "escaping_link").exists(),
            )
    except Exception as exc:  # noqa: BLE001 — the red-first assertion IS "no exception"
        raise SmokeFailureError(
            f"_tracked_snapshot raised on an absolute symlink member (the bug this smoke pins): {exc!r}"
        ) from exc


def _check_repo_with_no_symlinks_still_extracts_everything(base: Path) -> None:
    repo = base / "repo_plain"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    _git(repo, "config", "user.name", "smoke")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    tree = TargetTree.from_git(repo)
    with _tracked_snapshot(tree) as snapshot:
        _check(
            "negative control: an ordinary repo with no symlinks extracts both files",
            (snapshot / "a.txt").read_text(encoding="utf-8") == "a\n"
            and (snapshot / "b.txt").read_text(encoding="utf-8") == "b\n",
        )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_absolute_symlink_does_not_kill_the_scan(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_repo_with_no_symlinks_still_extracts_everything(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"secrets_symlink_extract_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
