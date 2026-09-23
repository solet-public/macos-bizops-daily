"""Slice B smoke — profile-driven allowlist install is fail-loud, fail-fast.

Pins the exact contract `profile_install.py` replaces:
`macos_midwife_plugin.venv_setup.install_target_tree` swallows per-plugin
pip failures (warn-and-continue) and returns a `(count, failed_names)`
tuple; genesis's `install_profile_allowlist` must instead RAISE
`ProfileInstallError` on the FIRST failure and stop — no later package
in the allowlist is attempted once one fails. `subprocess.run` is
mocked throughout (offline; no real pip invocations, no network) since
the behavior under test is the control flow, not pip itself.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/profile_install_faildloud_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.profile_install import (
    ProfileInstallError,
    install_profile_allowlist,
    load_plugin_allowlist,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _make_fixture_tree(root: Path, plugin_names: list[str]) -> Path:
    """Build a fake clone tree: `ananta/`, `plugins/github_midwife_plugin/`,
    and one directory per name in `plugin_names`, each with a stub
    `pyproject.toml` (content irrelevant — subprocess.run is mocked, so
    pip never actually reads it; only its *presence* is checked).
    """
    target = root / "clone"
    (target / "ananta").mkdir(parents=True)
    (target / "ananta" / "pyproject.toml").write_text("[project]\nname='ananta'\n")

    for name in ["github_midwife_plugin", *plugin_names]:
        plugin_dir = target / "plugins" / name
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "pyproject.toml").write_text(f"[project]\nname='{name}'\n")

    venv_dir = target / ".venv"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python3").write_text("#!/bin/sh\n")
    return target


def _fake_run_all_succeed(
    cmd: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


def _make_fake_run_fails_on(
    bad_dirname: str,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        target_arg = cmd[-1]
        if target_arg.endswith(f"/{bad_dirname}"):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, output="", stderr=f"simulated failure for {bad_dirname}"
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    return _fake_run


def _check_happy_path(root: Path) -> None:
    target = _make_fixture_tree(root, ["foo_plugin", "bar_plugin"])
    with patch("subprocess.run", side_effect=_fake_run_all_succeed) as mock_run:
        installed = install_profile_allowlist(
            venv_dir=target / ".venv",
            target=target,
            plugin_allowlist=["foo_plugin", "bar_plugin", "github_midwife_plugin"],
        )
    _check(
        "happy-path installs ananta+seed+allowlist in order",
        installed == ["ananta", "github_midwife_plugin", "foo_plugin", "bar_plugin"],
        f"got {installed!r}",
    )
    _check(
        "happy-path calls pip once for the build-backend prep + once per distinct package (seed dedup)",
        mock_run.call_count == 5,
        f"got {mock_run.call_count} calls",
    )


def _check_setup_py_editable_is_detected(root: Path) -> None:
    """The platform package uses setup.py metadata, not PEP 621 metadata.

    Editable presence is a property of ``direct_url.json`` and its resolved
    source path; it must not depend on a duplicate ``[project].name`` field in
    the source tree's pyproject.toml.
    """
    target = _make_fixture_tree(root, ["foo_plugin"])
    (target / "ananta" / "pyproject.toml").write_text("[tool.ruff]\nline-length=100\n")
    (target / "ananta" / "setup.py").write_text("from setuptools import setup\nsetup(name='ananta')\n")
    site_packages = target / ".venv" / "lib" / "python3.13" / "site-packages"
    direct_url = site_packages / "ananta-2.0.0.dist-info" / "direct_url.json"
    direct_url.parent.mkdir(parents=True)
    direct_url.write_text(
        json.dumps(
            {
                "url": target.joinpath("ananta").resolve().as_uri(),
                "dir_info": {"editable": True},
            }
        )
    )

    with patch("subprocess.run", side_effect=_fake_run_all_succeed):
        installed = install_profile_allowlist(
            venv_dir=target / ".venv",
            target=target,
            plugin_allowlist=["foo_plugin"],
        )
    _check(
        "setup.py-backed ananta editable is detected from direct_url without duplicate PEP 621 metadata",
        installed == ["github_midwife_plugin", "foo_plugin"],
        f"got {installed!r}",
    )


def _check_installs_build_backend_first(root: Path) -> None:
    """RED-FIRST (finding F8, 2026-07-11): stock py3.13 venvs ship pip but NOT
    setuptools (dropped from ensurepip in 3.12), so the `--no-build-isolation`
    editable installs fail with BackendUnavailable. install_profile_allowlist
    must upgrade the build backend BEFORE any editable install. Pre-fix, the
    first subprocess call was the ananta editable install, so this check
    fails RED.
    """
    target = _make_fixture_tree(root, ["foo_plugin"])
    with patch("subprocess.run", side_effect=_fake_run_all_succeed) as mock_run:
        install_profile_allowlist(
            venv_dir=target / ".venv",
            target=target,
            plugin_allowlist=["foo_plugin"],
        )
    first_cmd = mock_run.call_args_list[0].args[0]
    _check(
        "the FIRST pip call installs the missing build backend without upgrading it",
        all(pkg in first_cmd for pkg in ("pip", "setuptools", "wheel"))
        and "install" in first_cmd and "--upgrade" not in first_cmd,
        f"got {first_cmd!r}",
    )
    _check(
        "the build-backend prep runs before the ananta editable install",
        str(target / "ananta") not in " ".join(first_cmd),
        f"got {first_cmd!r}",
    )


def _check_fail_loud_stops_at_first_failure(root: Path) -> None:
    target = _make_fixture_tree(root, ["foo_plugin", "bar_plugin", "baz_plugin"])
    fake_run = _make_fake_run_fails_on("bar_plugin")
    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        try:
            install_profile_allowlist(
                venv_dir=target / ".venv",
                target=target,
                plugin_allowlist=["foo_plugin", "bar_plugin", "baz_plugin"],
            )
        except ProfileInstallError as exc:
            _check(
                "fail-loud error names the failing package",
                "bar_plugin" in str(exc),
                f"got {exc!r}",
            )
        else:
            raise SmokeFailureError(
                "fail-loud-stops-at-first-failure: install_profile_allowlist did not raise"
            )
    # RED-FIRST proof: a swallow-and-continue implementation (the shape
    # this module replaces) would call pip for every package regardless
    # of failure — backend-prep, ananta, seed, foo, bar (fails), baz = 6
    # calls. The fail-fast contract stops immediately after the bar_plugin
    # failure: backend-prep, ananta, seed, foo, bar = 5 calls. baz_plugin
    # is NEVER attempted. (The leading build-backend prep call is F8's
    # addition; see _check_installs_build_backend_first.)
    _check(
        "fail-loud stops immediately — baz_plugin never attempted",
        mock_run.call_count == 5,
        f"got {mock_run.call_count} calls (6 would mean it kept going past the failure)",
    )


def _check_missing_directory_raises(root: Path) -> None:
    target = _make_fixture_tree(root, ["foo_plugin"])
    with patch("subprocess.run", side_effect=_fake_run_all_succeed):
        try:
            install_profile_allowlist(
                venv_dir=target / ".venv",
                target=target,
                plugin_allowlist=["foo_plugin", "does_not_exist_plugin"],
            )
        except ProfileInstallError as exc:
            _check(
                "missing-directory error names the missing package",
                "does_not_exist_plugin" in str(exc),
                f"got {exc!r}",
            )
        else:
            raise SmokeFailureError("missing-directory-raises: did not raise")


def _check_missing_pyproject_raises(root: Path) -> None:
    target = _make_fixture_tree(root, [])
    naked_dir = target / "plugins" / "naked_plugin"
    naked_dir.mkdir(parents=True)  # directory exists but no pyproject.toml
    with patch("subprocess.run", side_effect=_fake_run_all_succeed):
        try:
            install_profile_allowlist(
                venv_dir=target / ".venv",
                target=target,
                plugin_allowlist=["naked_plugin"],
            )
        except ProfileInstallError as exc:
            _check(
                "missing-pyproject error names the offending package",
                "naked_plugin" in str(exc) and "pyproject.toml" in str(exc),
                f"got {exc!r}",
            )
        else:
            raise SmokeFailureError("missing-pyproject-raises: did not raise")


def _check_missing_venv_python_raises(root: Path) -> None:
    target = _make_fixture_tree(root, [])
    bogus_venv = root / "no_such_venv"
    with patch("subprocess.run", side_effect=_fake_run_all_succeed):
        try:
            install_profile_allowlist(
                venv_dir=bogus_venv, target=target, plugin_allowlist=[]
            )
        except ProfileInstallError as exc:
            _check("missing-venv-python raises", "venv python not found" in str(exc), str(exc))
        else:
            raise SmokeFailureError("missing-venv-python-raises: did not raise")


def _check_load_plugin_allowlist(root: Path) -> None:
    profile_path = root / "profile.yaml"
    profile_path.write_text(
        "profile_name: test\nplugins:\n  - foo_plugin\n  - bar_plugin\n"
    )
    allowlist = load_plugin_allowlist(profile_path)
    _check(
        "load_plugin_allowlist extracts the plugins list",
        allowlist == ["foo_plugin", "bar_plugin"],
        f"got {allowlist!r}",
    )

    missing_path = root / "does_not_exist.yaml"
    try:
        load_plugin_allowlist(missing_path)
    except ProfileInstallError:
        _check("load_plugin_allowlist raises on missing file", True)
    else:
        raise SmokeFailureError("load_plugin_allowlist-missing-file: did not raise")

    malformed_path = root / "malformed.yaml"
    malformed_path.write_text("plugins: not_a_list\n")
    try:
        load_plugin_allowlist(malformed_path)
    except ProfileInstallError:
        _check("load_plugin_allowlist raises on malformed plugins field", True)
    else:
        raise SmokeFailureError("load_plugin_allowlist-malformed: did not raise")

    invalid_yaml_path = root / "invalid.yaml"
    invalid_yaml_path.write_text("plugins: [unterminated\n")
    try:
        load_plugin_allowlist(invalid_yaml_path)
    except ProfileInstallError as exc:
        _check(
            "load_plugin_allowlist normalizes syntactically invalid YAML to ProfileInstallError",
            "invalid YAML" in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("load-plugin-allowlist-invalid-yaml: did not raise")


def _check_real_profile_a_allowlist_loads() -> None:
    """Sanity: the actual Slice A profile template loads through this path."""
    repo_root = Path(__file__).resolve().parents[3]
    profile_path = (
        repo_root / "plugins" / "github_midwife_plugin" / "knowledge_base"
        / "profile_templates" / "macos-free-solet.yaml"
    )
    allowlist = load_plugin_allowlist(profile_path)
    _check(
        "Slice A profile allowlist loads and is non-empty",
        len(allowlist) > 0 and "github_midwife_plugin" in allowlist,
        f"got {allowlist!r}",
    )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_happy_path(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_setup_py_editable_is_detected(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_installs_build_backend_first(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_fail_loud_stops_at_first_failure(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_missing_directory_raises(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_missing_pyproject_raises(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_missing_venv_python_raises(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_load_plugin_allowlist(root)
        _check_real_profile_a_allowlist_loads()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"profile_install_faildloud_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
