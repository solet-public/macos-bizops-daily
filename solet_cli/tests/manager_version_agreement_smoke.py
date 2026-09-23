"""Manager version agreement smoke (iss_ba7f7103).

`solet_manager.models.MANAGER_VERSION` and `solet_cli/pyproject.toml`'s
`[project].version` must never drift apart — the release stager
(`solet_cli/homebrew/scripts/stage_release.py`'s
`_require_manager_version_agreement`) refuses to stage a release where they
disagree, but that refusal only fires when someone actually runs a staging.
This is the second, independent place the design calls for (review concern
raised on the fix that added the runtime refusal): a small, hermetic,
commit-time check with no fixture to build and nothing to invoke, so the two
constants cannot drift apart between releases without failing every commit,
not just the next release attempt.

Deliberately does not import `stage_release.py` or `solet_manager` — it reads
both source files directly (`ast` for the Python constant, `tomllib` for the
TOML value), the same technique the stager itself uses, so this check does
not depend on either module's import machinery working.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MODELS_PATH = _REPOSITORY_ROOT / "solet_cli" / "src" / "solet_manager" / "models.py"
_PYPROJECT_PATH = _REPOSITORY_ROOT / "solet_cli" / "pyproject.toml"
_checks = 0


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _manager_version() -> str:
    tree = ast.parse(_MODELS_PATH.read_text(encoding="utf-8"), filename=str(_MODELS_PATH))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "MANAGER_VERSION"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise AssertionError(f"{_MODELS_PATH} has no module-level MANAGER_VERSION string constant")


def _pyproject_version() -> str:
    data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise AssertionError(f"{_PYPROJECT_PATH} has no [project].version")
    return version


def main() -> int:
    declared = _manager_version()
    packaged = _pyproject_version()
    _check(
        declared == packaged,
        f"solet_manager.models.MANAGER_VERSION {declared!r} must equal "
        f"solet_cli/pyproject.toml [project].version {packaged!r} (iss_ba7f7103)",
    )
    print(f"manager_version_agreement_smoke: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
