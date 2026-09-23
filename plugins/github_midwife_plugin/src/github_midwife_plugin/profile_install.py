"""Slice B — profile-driven fail-loud allowlist install (Layer 1).

Own-copy of the pip-install primitive `macos_midwife_plugin.venv_setup`
uses (per the no-cross-import-at-runtime convention, build spec §1/§2),
with the policy inverted: `venv_setup.install_target_tree` iterates
*every* plugin under a cloned tree and swallows per-plugin failures
(warn-and-continue, since some plugins legitimately fail in mixed
environments). Genesis has no such tolerance — an unresolvable
allowlist entry means the newborn cannot boot into the profile it was
told to run, so the FIRST failure raises immediately and installation
stops (fail-loud, fail-fast; no partial-install continuation).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import yaml

from .constants import BUILD_BACKEND_PACKAGES, PIP_INSTALL_TIMEOUT_S

_SEED_PACKAGE_NAME = "github_midwife_plugin"


class ProfileInstallError(RuntimeError):
    """Raised when the seed or any allowlisted plugin fails to install."""


def load_plugin_allowlist(profile_path: Path) -> list[str]:
    """Extract the `plugins:` allowlist from a profile template YAML.

    Deliberately minimal — only reads the one field this module needs,
    independent of Slice D's richer `config_materialize.py` (which
    reads baseline configs, service_bindings, and starting_actions too).
    """
    if not profile_path.is_file():
        raise ProfileInstallError(f"profile template not found: {profile_path}")
    try:
        raw: Any = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileInstallError(f"profile template contains invalid YAML: {profile_path}") from exc
    if not isinstance(raw, dict):
        raise ProfileInstallError(
            f"profile template did not parse to a mapping: {profile_path}"
        )
    plugins = raw.get("plugins") or []
    if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
        raise ProfileInstallError(
            f"profile template {profile_path}: 'plugins' must be a list of strings"
        )
    return list(plugins)


def _install_build_backend(venv_python: Path) -> None:
    """Provision the PEP 517 backend only when the target venv lacks it.

    Finding F8 (2026-07-11): a stock Python 3.13 venv ships pip but NOT
    setuptools (`ensurepip` dropped it in 3.12), so
    `pip install --no-build-isolation -e` fails with
    `BackendUnavailable: Cannot import 'setuptools.build_meta'` until the
    backend is present. A partial Genesis rerun must not turn an unrelated
    missing artifact into a package upgrade.
    """
    try:
        subprocess.run(
            [
                str(venv_python), "-m", "pip", "install",
                *BUILD_BACKEND_PACKAGES,
            ],
            check=True, capture_output=True, text=True,
            timeout=PIP_INSTALL_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        raise ProfileInstallError(
            "build-backend install (pip/setuptools/wheel) failed: "
            f"{(exc.stderr or '').strip()[:500]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProfileInstallError(
            "build-backend install (pip/setuptools/wheel) timed out "
            f"after {PIP_INSTALL_TIMEOUT_S}s"
        ) from exc


def _site_packages(venv_python: Path) -> Path | None:
    candidates = sorted(venv_python.parents[1].glob("lib/python*/site-packages"))
    return candidates[0] if len(candidates) == 1 else None


def _build_backend_present(venv_python: Path) -> bool:
    site_packages = _site_packages(venv_python)
    return bool(
        site_packages is not None
        and all((site_packages / package).exists() for package in BUILD_BACKEND_PACKAGES)
    )


def _editable_present(venv_python: Path, package_dir: Path) -> bool:
    site_packages = _site_packages(venv_python)
    if site_packages is None:
        return False
    for direct_url in site_packages.glob("*.dist-info/direct_url.json"):
        try:
            payload = json.loads(direct_url.read_text(encoding="utf-8"))
            url = payload.get("url")
            editable = payload.get("dir_info", {}).get("editable")
            installed = Path(unquote(urlparse(url).path)).resolve(strict=False)
        except (AttributeError, OSError, ValueError, json.JSONDecodeError):
            continue
        if editable is True and installed == package_dir.resolve(strict=False):
            return True
    return False


def _install_editable(venv_python: Path, package_dir: Path, package_label: str) -> bool:
    """`pip install --no-build-isolation -e <package_dir>` — raises loud on failure."""
    if not package_dir.is_dir():
        raise ProfileInstallError(
            f"{package_label}: package directory not found: {package_dir}"
        )
    if not (package_dir / "pyproject.toml").is_file():
        raise ProfileInstallError(f"{package_label}: no pyproject.toml at {package_dir}")
    if _editable_present(venv_python, package_dir):
        return False
    try:
        subprocess.run(
            [
                str(venv_python), "-m", "pip", "install",
                "--no-build-isolation", "-e", str(package_dir),
            ],
            check=True, capture_output=True, text=True,
            timeout=PIP_INSTALL_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        raise ProfileInstallError(
            f"{package_label}: pip install -e {package_dir} failed: "
            f"{(exc.stderr or '').strip()[:500]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProfileInstallError(
            f"{package_label}: pip install -e {package_dir} timed out "
            f"after {PIP_INSTALL_TIMEOUT_S}s"
        ) from exc
    return True


def install_profile_allowlist(
    *, venv_dir: Path, target: Path, plugin_allowlist: Sequence[str],
) -> list[str]:
    """Install `ananta` + `github_midwife_plugin` + every allowlisted plugin.

    Fail-loud: the first package that fails to install raises
    `ProfileInstallError` immediately and no further packages are
    attempted. Returns the ordered list of package names installed
    (only populated up to, and not including, a failure).

    `venv_dir`/`target` are caller-supplied (no hardcoded paths) so this
    stays independently testable against a fixture tree — this module
    does not read `config_materialize.py`'s output (Slice D); the two
    slices are decoupled per the build spec's independent-ish ordering.
    """
    venv_python = venv_dir / "bin" / "python3"
    if not venv_python.exists():
        raise ProfileInstallError(f"venv python not found: {venv_python}")

    # Finding F8 (2026-07-11): ensure the PEP 517 build backend is present
    # before any `--no-build-isolation -e` install (a stock py3.13 venv
    # lacks setuptools). Fail-loud via ProfileInstallError if it cannot be
    # installed.
    if not _build_backend_present(venv_python):
        _install_build_backend(venv_python)

    installed: list[str] = []

    if _install_editable(venv_python, target / "ananta", "ananta"):
        installed.append("ananta")

    if _install_editable(
        venv_python, target / "plugins" / _SEED_PACKAGE_NAME, _SEED_PACKAGE_NAME
    ):
        installed.append(_SEED_PACKAGE_NAME)

    for plugin_name in plugin_allowlist:
        if plugin_name == _SEED_PACKAGE_NAME:
            continue  # already installed as the seed above
        if _install_editable(venv_python, target / "plugins" / plugin_name, plugin_name):
            installed.append(plugin_name)

    return installed
