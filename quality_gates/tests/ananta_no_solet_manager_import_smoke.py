#!/usr/bin/env python3
"""Registered boundary discriminator for core's born-clone import closure."""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ANANTA_SOURCE = _REPO / "ananta" / "src"


def _find_solet_manager_imports(source_root: Path) -> list[str]:
    return [
        finding
        for path in sorted(source_root.rglob("*.py"))
        for finding in _manager_import_locations(path)
    ]


def _manager_import_locations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [f"{path}:{node.lineno}" for node in ast.walk(tree) if _is_manager_import(node)]


def _is_manager_import(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(_is_manager_module(alias.name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module is not None and _is_manager_module(node.module)
    return False


def _is_manager_module(module: str) -> bool:
    return module == "solet_manager" or module.startswith("solet_manager.")


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    _check(
        _find_solet_manager_imports(_ANANTA_SOURCE) == [],
        "ananta/src must not import solet_manager at module or function scope",
    )
    with tempfile.TemporaryDirectory(prefix="ananta-import-boundary-") as temporary:
        fixture = Path(temporary) / "src"
        fixture.mkdir()
        (fixture / "selected_sources.py").write_text(
            "def restored_selected_sources_import() -> None:\n"
            "    from solet_manager.contracts import ContractBundle\n"
            "    del ContractBundle\n",
            encoding="utf-8",
        )
        _check(
            _find_solet_manager_imports(fixture) == [f"{fixture / 'selected_sources.py'}:2"],
            "red mutation restoring selected_sources.py:9 solet_manager import is detected",
        )
    print("ananta_no_solet_manager_import_smoke: 2 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
