"""Build the Step 7 section-3 real-style clone for the VM lifecycle harness.

The clone comes from the SEED archive the installed formula's lock names --
``<keg>/libexec/share/solet/seed.lock.json`` (``repository`` at ``commit``,
``tree_hash`` verified) -- never from this checkout.  The seed's own genesis
writers and the ``solet create`` shell shape (design section 3.3) then leave
the tracked and untracked state a real newborn carries, so ``solet-manager
inspect`` classifies it ``local_changes_present`` and the import/update
phases of ``lifecycle_acceptance.py`` measure the opened classes across a
real process boundary.  The writers are imported by path from
``solet_cli/tests/_step7_support.py`` (the same module the unit-level
acceptance uses); the clone's bytes are the seed's.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast

_TESTS = Path(__file__).resolve().parents[2] / "tests"


class _Writers(Protocol):
    """The section-3.3 surface of ``_step7_support`` this builder drives."""

    NAME: str

    def Knobs(self) -> object: ...  # noqa: N802 - the support module's dataclass

    def _run_genesis_writers(self, target: Path, knobs: object) -> dict[str, str]: ...

    def _run_create_shape(self, target: Path) -> dict[str, str]: ...

    def _assert_attribution(self, target: Path, expected: dict[str, str]) -> None: ...

    def porcelain(self, target: Path) -> dict[str, str]: ...


def _support() -> _Writers:
    """Import ``solet_cli/tests/_step7_support.py`` by path (it and its siblings are not a package)."""
    sys.path.insert(0, str(_TESTS))
    spec = importlib.util.spec_from_file_location("_step7_support", _TESTS / "_step7_support.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {_TESTS / '_step7_support.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_step7_support"] = module
    spec.loader.exec_module(module)
    return cast(_Writers, module)

_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": "/var/empty"}


def _git(repo: Path | None, *arguments: str) -> str:
    command = ["git", *(("-C", str(repo)) if repo is not None else ()), *arguments]
    completed = subprocess.run(command, capture_output=True, text=True, env=_GIT_ENV, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _default_lock() -> Path:
    manager = shutil.which("solet-manager")
    if manager is None:
        raise SystemExit("solet-manager is not on PATH; pass --seed-lock explicitly")
    return Path(manager).resolve().parent.parent / "share" / "solet" / "seed.lock.json"


def build(root: Path, name: str, lock_path: Path) -> dict[str, object]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    repository, commit, tree_hash = str(lock["repository"]), str(lock["commit"]), str(lock["tree_hash"])
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"fixture root must be absent or empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    target = root / "target"
    _git(None, "clone", "--quiet", "--no-hardlinks", repository, str(target))
    _git(target, "checkout", "--quiet", "-B", "main", commit)
    observed_tree = _git(target, "rev-parse", "HEAD^{tree}")
    if observed_tree != tree_hash:
        raise SystemExit(f"the lock's commit {commit} carries tree {observed_tree}, not the lock's tree_hash {tree_hash}")
    _git(target, "config", "user.name", "Lifecycle Fixture")
    _git(target, "config", "user.email", "fixture@example.invalid")
    support = _support()
    support.NAME = name
    knobs = support.Knobs()
    expected = support._run_genesis_writers(target, knobs)  # noqa: SLF001 - the section-3.3 writers, by design
    expected.update(support._run_create_shape(target))  # noqa: SLF001
    support._assert_attribution(target, expected)  # noqa: SLF001
    porcelain = support.porcelain(target)
    return {
        "target": str(target),
        "name": name,
        "seed_lock": str(lock_path),
        "repository": repository,
        "commit": commit,
        "tracked_modified": sorted(path for path, code in porcelain.items() if code != "??"),
        "untracked": sorted(path for path, code in porcelain.items() if code == "??"),
        "writers": expected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Step 7 section-3 real-style clone from the installed seed lock.")
    parser.add_argument("--root", type=Path, required=True, help="Absent or empty directory; the clone lands at <root>/target.")
    parser.add_argument("--name", required=True, help="The instance name genesis writes into root_manifest.yaml and the marker.")
    parser.add_argument("--seed-lock", type=Path, default=None, help="Seed lock to clone from (default: the installed solet-manager's).")
    namespace = parser.parse_args()
    lock_path = namespace.seed_lock if namespace.seed_lock is not None else _default_lock()
    print(json.dumps(build(namespace.root.expanduser().resolve(strict=False), namespace.name, lock_path.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
