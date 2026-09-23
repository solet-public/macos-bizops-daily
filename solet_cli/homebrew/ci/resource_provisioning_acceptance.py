#!/usr/bin/env python3
"""Prove the Formula's pinned setuptools/wheel resources really provision a
clean virtualenv, over the network, on a runner that opts in.

The offline half of this proof — the Formula declares pinned resources with
sha256, installed before the build_isolation:false manager step, and a
clean venv genuinely lacks setuptools without them — is
`tests/release_payload_smoke.py`, gate-registered and fully offline. This
script is the other half: it actually fetches the pinned wheels, verifies
them against their declared checksums, installs them with `--no-index`,
and confirms the resulting venv can build the manager with
`build_isolation: false` and no ambient per-user site-packages — the exact
scenario a real `brew install` exercises. It cannot be a gate smoke because
gate smokes must pass with no network access at all.

Gated the same way `ci/lifecycle_acceptance.py` gates its own real run —
CI=true, GITHUB_ACTIONS=true, SOLET_ACCEPT_LIFECYCLE_MUTATION=1 — for
consistency with the one other network/host-dependent acceptance path in
this tree, even though nothing here mutates a live Homebrew tap: it builds
a throwaway virtualenv and never touches HOMEBREW_PREFIX or any installed
keg. Fails loud on a missing prerequisite (brew, python@3.13, network) —
never a silent skip; a check that skips whenever its precondition is
absent never runs on the machines that matter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_RENDERER = _ROOT / "scripts" / "render_release_payload.py"
_EXAMPLE = _ROOT / "release_metadata.example.json"
_REPOSITORY = _ROOT.parents[1]

_RESOURCE_BLOCK = re.compile(
    r'resource\s+"(?P<name>[^"]+)"\s+do\s+'
    r'url\s+"(?P<url>[^"]+)"\s+'
    r'sha256\s+"(?P<sha256>[0-9a-f]{64})"\s+'
    r"end",
)


def main() -> int:
    _require_opted_in()
    python = _homebrew_python()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        formula = _render_formula(root)
        resources = _parse_pinned_resources(formula)
        if not {"setuptools", "wheel"} <= resources.keys():
            raise RuntimeError(
                "Formula does not declare both pinned setuptools and wheel "
                "resources — nothing to verify"
            )
        venv_python = _create_venv(python, root)
        no_user_site = _no_user_site_env()
        _assert_absent(venv_python, no_user_site)
        for name, (url, sha256) in resources.items():
            wheel_path = _fetch_and_verify(root, name, url, sha256)
            _pip_install_no_index(venv_python, wheel_path, no_user_site)
        _assert_present(venv_python, no_user_site)
        _install_manager_no_ambient_state(venv_python)
    print("resource_provisioning_acceptance PASSED")
    return 0


def _require_opted_in() -> None:
    if (
        os.environ.get("CI") != "true"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("SOLET_ACCEPT_LIFECYCLE_MUTATION") != "1"
    ):
        raise RuntimeError(
            "resource provisioning acceptance requires CI=true, "
            "GITHUB_ACTIONS=true, and SOLET_ACCEPT_LIFECYCLE_MUTATION=1 on "
            "a runner with network access"
        )


def _homebrew_python() -> Path:
    import shutil

    brew = shutil.which("brew")
    if brew is None:
        raise RuntimeError("host prerequisite missing: Homebrew is required")
    result = subprocess.run(
        [brew, "--prefix", "python@3.13"], check=True, capture_output=True, text=True
    )
    python = Path(result.stdout.strip()) / "bin" / "python3.13"
    if not python.is_file():
        raise RuntimeError(f"host prerequisite missing: {python} is absent")
    return python


def _render_formula(root: Path) -> str:
    output = root / "output"
    subprocess.run(
        [
            sys.executable,
            str(_RENDERER),
            "--metadata",
            str(_EXAMPLE),
            "--output-root",
            str(output),
        ],
        check=True,
    )
    return (output / "Formula" / "solet.rb").read_text(encoding="utf-8")


def _parse_pinned_resources(formula: str) -> dict[str, tuple[str, str]]:
    return {
        match.group("name"): (match.group("url"), match.group("sha256"))
        for match in _RESOURCE_BLOCK.finditer(formula)
    }


def _create_venv(python: Path, root: Path) -> Path:
    venv = root / "formula-venv"
    subprocess.run(
        [str(python), "-m", "venv", "--system-site-packages", "--without-pip", str(venv)],
        check=True,
    )
    return venv / "bin" / "python3.13"


def _no_user_site_env() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _assert_absent(venv_python: Path, no_user_site: dict[str, str]) -> None:
    probe = subprocess.run(
        [str(venv_python), "-c", "import setuptools"],
        capture_output=True,
        text=True,
        env=no_user_site,
    )
    if probe.returncode == 0:
        raise RuntimeError(
            "setuptools was importable before the pinned resources were "
            "installed — Homebrew's python@3.13 may have started shipping "
            "it, or ambient per-user state leaked in despite PYTHONNOUSERSITE"
        )


def _fetch_and_verify(root: Path, name: str, url: str, sha256: str) -> Path:
    destination = root / "resource-cache" / url.rsplit("/", 1)[-1]
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["curl", "-sL", "-o", str(destination), url], check=True)
    real_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    if real_sha256 != sha256:
        raise RuntimeError(
            f"pinned resource {name!r} checksum mismatch: declared {sha256}, "
            f"got {real_sha256}"
        )
    return destination


def _pip_install_no_index(
    venv_python: Path, wheel_path: Path, no_user_site: dict[str, str]
) -> None:
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel_path)],
        check=True,
        env=no_user_site,
    )


def _assert_present(venv_python: Path, no_user_site: dict[str, str]) -> None:
    probe = subprocess.run(
        [str(venv_python), "-c", "import setuptools, wheel"],
        capture_output=True,
        text=True,
        env=no_user_site,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            f"the pinned resources did not make setuptools/wheel importable: {probe.stderr}"
        )


def _install_manager_no_ambient_state(venv_python: Path) -> None:
    environment = _no_user_site_env()
    environment.update({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-binary=:all:",
            "--ignore-installed",
            "--no-compile",
            "--no-build-isolation",
            str(_REPOSITORY / "solet_cli"),
        ],
        check=True,
        env=environment,
    )
    solet_bin = venv_python.parent / "solet"
    version = subprocess.run(
        [str(solet_bin), "--version"],
        capture_output=True,
        text=True,
        env=environment,
    )
    if version.returncode != 0 or version.stdout.strip() != "solet 0.1.0":
        raise RuntimeError(f"manager entrypoint did not run cleanly: {version.stdout!r}")
    print(json.dumps({"manager_version_output": version.stdout.strip()}))


if __name__ == "__main__":
    raise SystemExit(main())
