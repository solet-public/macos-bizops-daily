#!/usr/bin/env python3
"""Prove the smoke runner builds an explicit child environment."""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _assert_register_validation() -> None:
    """Duplicate and dangling registrations fail before any smoke runs."""
    sys.path.insert(0, str(_REPO_ROOT))
    from quality_gates.run_smokes import _read_register

    with tempfile.TemporaryDirectory() as temp_dir:
        register = Path(temp_dir) / "gate_smokes.txt"
        valid = "quality_gates/tests/run_smokes_git_environment_smoke.py"
        register.write_text(f"{valid}\n{valid}  # duplicate\n", encoding="utf-8")
        try:
            _read_register(register)
        except ValueError as exc:
            assert "duplicate smoke path" in str(exc), exc
        else:
            raise AssertionError("duplicate register entry was accepted")

        register.write_text("quality_gates/tests/does_not_exist_smoke.py\n", encoding="utf-8")
        try:
            _read_register(register)
        except ValueError as exc:
            assert "does not exist" in str(exc), exc
        else:
            raise AssertionError("dangling register entry was accepted")


def _assert_git_environment_is_scrubbed() -> None:
    """Nested Git commands see the worktree, not inherited Git variables."""
    from quality_gates.run_smokes import _run_one

    with tempfile.TemporaryDirectory() as temp_dir:
        foreign_root = Path(temp_dir) / "foreign"
        foreign_root.mkdir()
        _run(["git", "init"], foreign_root)
        fixture = Path(temp_dir) / "report_toplevel.py"
        fixture.write_text(
            "from pathlib import Path\n"
            "import subprocess\n"
            "result = subprocess.run(\n"
            "    ['git', 'rev-parse', '--show-toplevel'],\n"
            "    check=True, capture_output=True, text=True,\n"
            ")\n"
            "print(result.stdout.strip())\n",
            encoding="utf-8",
        )
        original_git_env = {
            key: value for key, value in os.environ.items() if key.startswith("GIT_")
        }
        os.environ["GIT_DIR"] = str(foreign_root / ".git")
        os.environ["GIT_WORK_TREE"] = str(foreign_root)
        try:
            verdict, output = _run_one(Path(sys.executable), fixture, timeout=10)
        finally:
            for key in [key for key in os.environ if key.startswith("GIT_")]:
                del os.environ[key]
            os.environ.update(original_git_env)
        assert verdict == "passed", output
        assert output.strip() == str(_REPO_ROOT), output


def _restore_environment(original: dict[str, str]) -> None:
    """Restore the test process environment after a fixture mutation."""
    os.environ.clear()
    os.environ.update(original)


def _run_fixture(source: str) -> tuple[str, str]:
    """Run fixture source through the runner with the active venv."""
    from quality_gates.run_smokes import _run_one

    with tempfile.TemporaryDirectory() as temp_dir:
        fixture = Path(temp_dir) / "fixture.py"
        fixture.write_text(source, encoding="utf-8")
        return _run_one(Path(sys.executable), fixture, timeout=10)


def _assert_environment_is_allowlisted() -> None:
    """Caller-only values, including PYTHONPATH, never reach a smoke."""
    original_environment = dict(os.environ)
    expected = {
        "APP_HOME": "/fixture/profile",
        "SOLET_NAME": "fixture-solet",
        "SOLET_HOME": "/fixture/solet-home",
        "SOLET_WORKSPACE_ROOT": "/fixture/workspace",
        "XDG_RUNTIME_DIR": "/fixture/runtime",
        "HOME": "/fixture/home",
    }
    try:
        os.environ.update(expected)
        os.environ["PYTHONPATH"] = "solet_cli"
        os.environ["RUN_SMOKES_UNALLOWLISTED_FIXTURE"] = "caller-only"
        os.environ["GIT_CONTROLLER_NAME"] = "caller-controller"
        names = (*expected, "PYTHONPATH", "RUN_SMOKES_UNALLOWLISTED_FIXTURE", "GIT_CONTROLLER_NAME")
        verdict, output = _run_fixture(
            "import json\n"
            "import os\n"
            f"print(json.dumps({{key: os.environ.get(key) for key in {names!r}}}))\n"
        )
    finally:
        _restore_environment(original_environment)
    assert verdict == "passed", output
    observed = json.loads(output)
    assert observed == expected | {
        "PYTHONPATH": None,
        "RUN_SMOKES_UNALLOWLISTED_FIXTURE": None,
        "GIT_CONTROLLER_NAME": None,
    }, observed


def _assert_runner_controls_reach_fixture_children() -> None:
    """Host-lock stub controls cross the runner boundary by exact name."""
    original_environment = dict(os.environ)
    expected = {
        "RUN_SMOKES_NESTED_SELF_TEST": "1",
        "RUN_SMOKES_HOST_LOCK_STUB_MODE": "fixture-mode",
        "RUN_SMOKES_HOST_LOCK_STUB_EVENTS": "/fixture/events.jsonl",
        "RUN_SMOKES_HOST_LOCK_STUB_LABEL": "fixture-label",
        "RUN_SMOKES_HOST_LOCK_STUB_SLEEP": "0.1",
        "RUN_SMOKES_HOST_SERIAL_LOCK_PATH": "/fixture/serial-only.lock",
    }
    try:
        os.environ.update(expected)
        names = tuple(expected)
        verdict, output = _run_fixture(
            "import json\n"
            "import os\n"
            f"print(json.dumps({{key: os.environ.get(key) for key in {names!r}}}))\n"
        )
    finally:
        _restore_environment(original_environment)
    assert verdict == "passed", output
    assert json.loads(output) == expected, output


def _assert_runner_controls_do_not_leak_from_a_shell() -> None:
    """A shell-set stub mode cannot silently turn a top-level smoke into a fixture."""
    original_environment = dict(os.environ)
    try:
        os.environ["RUN_SMOKES_HOST_LOCK_STUB_MODE"] = "polluted-shell"
        verdict, output = _run_fixture(
            "import json\n"
            "import os\n"
            "print(json.dumps(os.environ.get('RUN_SMOKES_HOST_LOCK_STUB_MODE')))\n"
        )
    finally:
        _restore_environment(original_environment)
    assert verdict == "passed", output
    assert json.loads(output) is None, output


def _assert_child_path_is_exactly_constructed() -> None:
    """The child PATH contains venv and fixed machine-global tool prefixes only."""
    expected = os.pathsep.join(
        (
            str(Path(sys.executable).parent),
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )
    verdict, output = _run_fixture("import os\nprint(os.environ['PATH'])\n")
    assert verdict == "passed", output
    assert output.strip() == expected, output


def _assert_smoke_can_own_its_import_path() -> None:
    """A smoke that adds its own import path still passes without PYTHONPATH."""
    original_environment = dict(os.environ)
    try:
        os.environ["PYTHONPATH"] = "solet_cli"
        verdict, output = _run_fixture("import sys\nsys.path.insert(0, 'solet_cli')\nimport homebrew.ci\n")
    finally:
        _restore_environment(original_environment)
    assert verdict == "passed", output


def _assert_editable_pth_remains_a_separate_channel() -> None:
    """The selected interpreter still processes its editable .pth files."""
    editable_paths: list[str] = []
    for package_dir in site.getsitepackages():
        for pth_file in Path(package_dir).glob("__editable__.*.pth"):
            editable_paths.extend(line for line in pth_file.read_text(encoding="utf-8").splitlines() if line.startswith("/"))
    assert editable_paths, "the test venv has no editable .pth path to prove"
    expected_path = editable_paths[0]
    verdict, output = _run_fixture(f"import json\nimport sys\nprint(json.dumps({expected_path!r} in sys.path))\n")
    assert verdict == "passed", output
    assert json.loads(output) is True, output


def _assert_runner_pins_child_cwd() -> None:
    """The child starts at the repository root, independently of its caller."""
    verdict, output = _run_fixture("from pathlib import Path\nprint(Path.cwd().resolve())\n")
    assert verdict == "passed", output
    assert output.strip() == str(_REPO_ROOT), output


def main() -> None:
    _assert_register_validation()
    _assert_git_environment_is_scrubbed()
    _assert_environment_is_allowlisted()
    _assert_runner_controls_reach_fixture_children()
    _assert_runner_controls_do_not_leak_from_a_shell()
    _assert_child_path_is_exactly_constructed()
    _assert_smoke_can_own_its_import_path()
    _assert_editable_pth_remains_a_separate_channel()
    _assert_runner_pins_child_cwd()


if __name__ == "__main__":
    main()
