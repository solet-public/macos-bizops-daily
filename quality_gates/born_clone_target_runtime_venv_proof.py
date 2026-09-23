#!/usr/bin/env python3
"""One-time C acceptance corpus: a target runtime without ``solet_manager``.

This is deliberately *not* in ``gate_smokes.txt``: creating a fresh virtual
environment and installing the target-runtime layer is too expensive for the
ordinary register.  Its durable, gate-tier corpus path is this file.  Run it
from a checkout with the repository's Python 3.13 interpreter:

    .venv/bin/python3 quality_gates/born_clone_target_runtime_venv_proof.py

The control proves the condition the born-clone gate cannot: ``ananta`` and
the actual target layer can import selected-source qualification when the
target venv contains the carried shared distribution but has no installed or
importable ``solet_manager``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TARGET_RUNTIME_PROJECTS = (
    "solet_setup_contracts",
    "ananta",
    "plugins/macos_vault_plugin",
    "plugins/github_midwife_plugin",
    "plugins/agent_messaging_plugin",
)


def _run(command: list[str]) -> None:
    result = subprocess.run(command, cwd=_REPO, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def main() -> int:
    if importlib.util.find_spec("solet_manager") is None:
        raise RuntimeError("proof must start from the development environment that can detect a missing manager")

    with tempfile.TemporaryDirectory(prefix="birth6-target-runtime-") as temporary:
        venv = Path(temporary) / "venv"
        python = venv / "bin" / "python3"
        _run([sys.executable, "-m", "venv", str(venv)])
        for project in _TARGET_RUNTIME_PROJECTS:
            _run([str(python), "-m", "pip", "install", "--no-deps", "--editable", str(_REPO / project)])
        # `pyyaml` is declared by the target layer and is the only third-party
        # import reached while loading the selected-source service.  Supplying
        # it explicitly keeps this proof focused on manager absence rather than
        # resolving unrelated target dependencies from an index.
        _run([str(python), "-m", "pip", "install", "pyyaml>=6.0"])
        _run([
            str(python),
            "-I",
            "-c",
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('solet_manager') is None; "
                "from ananta.services.session_ledger_service.selected_sources "
                "import load_selected_source_record; "
                "assert callable(load_selected_source_record); "
                "print('target-runtime selected-sources import without solet_manager: OK')"
            ),
        ])
    print("born_clone_target_runtime_venv_proof: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
