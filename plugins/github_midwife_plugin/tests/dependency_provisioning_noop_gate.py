"""Hermetic no-op and mutation-closure gate for dependency provisioners."""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SRC = _ROOT / "plugins/github_midwife_plugin/src"
_MANAGER_SRC = _ROOT / "solet_cli/src"
for _path in (_PLUGIN_SRC, _MANAGER_SRC, _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from github_midwife_plugin.profile_install import install_profile_allowlist
from github_midwife_plugin.setup_adapter_runtime import (
    HOMEBREW_GUARD_ENV,
    CommandOutcome,
    HomebrewAcquisition,
    SystemRuntime,
    homebrew_install_plan_error,
)
from solet_manager.adapters import _homebrew_install_plan_allowed

from bootstrap_adapter.homebrew import homebrew_guard_environment, run_homebrew_install_required
from bootstrap_adapter.models import PostgresObservation
from bootstrap_adapter.postgres import postgres_install_actions


def _check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _seed_compliant_profile(target: Path) -> None:
    venv_python = target / ".venv/bin/python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("fixture", encoding="utf-8")
    site_packages = target / ".venv/lib/python3.13/site-packages"
    for backend in ("pip", "setuptools", "wheel"):
        (site_packages / backend).mkdir(parents=True)
    for relative, name in (
        ("ananta", "ananta"),
        ("plugins/github_midwife_plugin", "github_midwife_plugin"),
        ("plugins/example_plugin", "example_plugin"),
    ):
        package = target / relative
        package.mkdir(parents=True)
        (package / "pyproject.toml").write_text(
            f"[project]\nname = '{name}'\n", encoding="utf-8"
        )
        dist_info = site_packages / f"{name}-1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "direct_url.json").write_text(
            json.dumps({"url": package.resolve().as_uri(), "dir_info": {"editable": True}}),
            encoding="utf-8",
        )


class _RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        stdout = "Would install 1 formula:\npgvector\n" if "--dry-run" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


def main() -> int:
    with (
        patch.dict(
            os.environ,
            {"HOME": "/operator/home", "HOMEBREW_CUSTOM_POLICY": "retained"},
            clear=True,
        ),
        patch(
            "bootstrap_adapter.homebrew.pwd.getpwuid",
            return_value=SimpleNamespace(pw_dir="/target/account"),
        ),
    ):
        guard_environment = homebrew_guard_environment()
    _check(
        guard_environment
        == {
            "HOMEBREW_CUSTOM_POLICY": "retained",
            "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
            "HOMEBREW_NO_INSTALL_UPGRADE": "1",
            "HOME": "/target/account",
        },
        "bootstrap Brew guard pins measured HOME-only requirement without ambient HOME or PATH",
    )

    with patch.dict(os.environ, {"HOMEBREW_CUSTOM_POLICY": "retained"}, clear=False):
        environment = SystemRuntime(home=Path("/fixture/home"))._environment(None)  # noqa: SLF001
    _check(environment["HOMEBREW_CUSTOM_POLICY"] == "retained", "ambient Homebrew policy survives")
    _check(all(environment[key] == value for key, value in HOMEBREW_GUARD_ENV.items()), "all Brew guards are exact")

    allowed = CommandOutcome(0, False, 1, "Would install 1 formula:\ntmux\n", "")
    hidden_upgrade = CommandOutcome(
        0, False, 1, "Would install 1 formula:\ntmux\nWould upgrade 1 dependency:\npython@3.13\n", ""
    )
    _check(homebrew_install_plan_error(allowed, "tmux") is None, "exact dry-run is allowed")
    _check(homebrew_install_plan_error(hidden_upgrade, "tmux") is not None, "named hidden upgrade fails closed")
    _check(_homebrew_install_plan_allowed(allowed.stdout, "tmux"), "manager accepts exact dry-run")
    _check(not _homebrew_install_plan_allowed(hidden_upgrade.stdout, "tmux"), "manager rejects hidden upgrade")

    sf_acquisition = HomebrewAcquisition("formula", "sf", ("node",))
    sf_closure = CommandOutcome(0, False, 1, "Would install 2 formulae:\nnode\nsf\n", "")
    sf_extra = CommandOutcome(0, False, 1, "Would install 3 formulae:\nnode\npython@3.13\nsf\n", "")
    claude_cask = CommandOutcome(0, False, 1, "Would install 1 cask:\nclaude-code\n", "")
    codex_cask = CommandOutcome(0, False, 1, "Would install 1 cask:\ncodex\n", "")
    _check(
        homebrew_install_plan_error(sf_closure, sf_acquisition) is None,
        "declared sf Node closure is allowed",
    )
    _check(
        homebrew_install_plan_error(sf_closure, "sf") is not None,
        "undeclared sf dependency fails closed",
    )
    _check(
        homebrew_install_plan_error(sf_extra, sf_acquisition) is not None,
        "undeclared formula in closure fails closed",
    )
    _check(
        homebrew_install_plan_error(
            claude_cask, HomebrewAcquisition("cask", "claude-code")
        ) is None,
        "declared cask is allowed",
    )
    _check(
        homebrew_install_plan_error(codex_cask, HomebrewAcquisition("cask", "codex"))
        is None,
        "dependency-free Codex cask is allowed; Node remains a separate declared operation",
    )
    _check(
        homebrew_install_plan_error(
            claude_cask, HomebrewAcquisition("formula", "claude-code")
        ) is not None,
        "package kind mismatch fails closed",
    )
    _check(
        _homebrew_install_plan_allowed(
            sf_closure.stdout, "sf", approved_closure=("node",)
        ),
        "manager accepts declared sf Node closure",
    )
    _check(
        not _homebrew_install_plan_allowed(sf_closure.stdout, "sf"),
        "manager rejects undeclared closure",
    )

    recording_runtime = _RecordingRuntime()
    run_homebrew_install_required(recording_runtime, "/fixture/brew", "pgvector", "pgvector install")  # type: ignore[arg-type]
    _check(len(recording_runtime.calls) == 2, "Brew dry-run precedes its only mutation")
    _check(
        all(
            all(call[1]["env"][key] == value for key, value in HOMEBREW_GUARD_ENV.items())
            for call in recording_runtime.calls
        ),
        "bootstrap Brew calls receive the exact guard environment",
    )

    stopped_with_pgvector = PostgresObservation(True, "/fixture/brew", True, True, 17, False, None)
    _check(
        [item["id"] for item in postgres_install_actions(stopped_with_pgvector)]
        == ["postgres.start_homebrew_service", "postgres.install_pgvector_formula"],
        "stopped PostgreSQL plans pgvector while discovery is unknown, before apply approval",
    )

    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary)
        _seed_compliant_profile(target)
        with patch("github_midwife_plugin.profile_install.subprocess.run") as run:
            installed = install_profile_allowlist(
                venv_dir=target / ".venv", target=target, plugin_allowlist=["example_plugin"]
            )
        _check(installed == [], "compliant Genesis package closure is a no-op")
        _check(run.call_count == 0, "compliant Genesis package closure invokes no pip mutation")
    print("dependency_provisioning_noop_gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
