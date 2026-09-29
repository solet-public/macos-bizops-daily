#!/usr/bin/env python3
"""Prove the Formula's pip bootstrap survives a base Python whose pip is too old.

r61 (``iss_d62aeab7``, ``dec_b08cf4c7``) builds the Manager venv on whatever
Python 3.13 the Mac already has. Homebrew's ``Virtualenv#pip_install`` runs
``<base python> -m pip --python=<venv>/bin/python install <std_pip_args>``
(``Library/Homebrew/language/python.rb`` ``do_install``). pip's ``--python``
re-executes the CALLER's own pip under the venv interpreter, so the base
Python's pip parses ``std_pip_args``, which always carries
``--uploaded-prior-to=P1D`` (``formula.rb`` ``std_pip_args``). pip older than
26.1 rejects that option, so on a Mac with an older Python 3.13 the build fails
(review ``unt_f3bf05be`` F1).

The Formula therefore installs its pinned pip into the venv from pip's own
wheel, run by the venv's interpreter, and does every later install with the
venv's interpreter as ``@python``. This script runs both sequences against a
base whose pip is the old wheel given on the command line. No Homebrew and no
network are used: every venv lives in a temporary directory.

1. CONTROL: Homebrew's current sequence with the old base pip fails with
   ``no such option: --uploaded-prior-to``.
2. CONTROL: the venv's own ``-m pip`` without the bootstrap still resolves the
   old base pip, and fails the same way, so the bootstrap is load-bearing.
3. CANDIDATE: bootstrap from the pinned wheel, then the resources and the
   manager with the venv's pip, then ``solet --version``.

Run: ``.venv/bin/python3 solet_cli/homebrew/ci/pip_bootstrap_proof.py --old-pip-wheel <pip-25.3-py3-none-any.whl>``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_HOMEBREW = Path(__file__).resolve().parents[1]
_REPOSITORY = _HOMEBREW.parents[1]
_FORMULA = _HOMEBREW / "Formula" / "solet.rb.template"
_WHEELS = _HOMEBREW / "tests" / "fixtures" / "pinned_wheels"
_RESOURCE_WHEELS = ("setuptools-84.0.0-py3-none-any.whl", "wheel-0.48.0-py3-none-any.whl", "packaging-26.2-py3-none-any.whl")
_PIP_WHEEL = "pip-26.2.1-py3-none-any.whl"
# pip 25.3, the newest release without --uploaded-prior-to (PyPI digest).
_OLD_PIP_SHA256 = "9655943313a94722b7774661c21049070f6bbb0a1516bf02f7c8d5d9201514cd"
_DEFAULT_PYTHON = Path("/opt/homebrew/opt/python@3.13/bin/python3.13")
_REJECTED = "no such option: --uploaded-prior-to"


def std_pip_args(*, build_isolation: bool) -> list[str]:
    """Homebrew's ``Formula#std_pip_args(prefix: false, build_isolation:)``, verbatim."""
    args = ["--verbose", "--no-deps", "--no-binary=:all:", "--ignore-installed", "--no-compile", "--uploaded-prior-to=P1D"]
    return args if build_isolation else [*args, "--no-build-isolation"]


def _run(argv: list[str | Path]) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1"}
    environment.pop("_PIP_RUNNING_IN_SUBPROCESS", None)
    return subprocess.run([str(part) for part in argv], check=False, capture_output=True, text=True, env=environment)


def _require(condition: bool, message: str, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}: {detail}")
    print(f"ok    {message}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pinned_pip_sha256() -> str:
    block = re.search(r'resource "pip" do\s+url "[^"]+/' + re.escape(_PIP_WHEEL) + r'"\s+sha256 "(?P<sha>[0-9a-f]{64})"', _FORMULA.read_text(encoding="utf-8"))
    if block is None:
        raise SystemExit(f"FAIL: the Formula pins no pip resource at {_PIP_WHEEL}")
    return block.group("sha")


def _old_base(python: Path, old_wheel: Path, root: Path) -> Path:
    """A base interpreter whose ``-m pip`` is the old wheel and nothing else."""
    base = root / "old-base"
    _require(_run([python, "-m", "venv", "--without-pip", base]).returncode == 0, "old base venv created")
    base_python = base / "bin" / "python"
    installed = _run([base_python, old_wheel / "pip", "install", "--no-deps", "--no-index", old_wheel])
    _require(installed.returncode == 0, "old pip installed into the old base", installed.stderr[-300:])
    version = _run([base_python, "-m", "pip", "--version"]).stdout
    _require(version.startswith("pip 25.3 "), f"the old base's -m pip is pip 25.3: {version.strip()}")
    return base_python


def _formula_venv(base_python: Path, root: Path, name: str) -> Path:
    """``virtualenv_create``: ``--system-site-packages --without-pip`` on the base, whose site-packages holds the old pip."""
    venv = root / name
    _require(_run([base_python, "-m", "venv", "--system-site-packages", "--without-pip", venv]).returncode == 0, f"{name} created")
    base_site = _run([base_python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip()
    venv_site = Path(_run([venv / "bin" / "python", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip())
    # The old base is itself a venv, so its venv's system site is the real interpreter's. Expose the old
    # base's site-packages the way a real older python@3.13 exposes its own bundled pip.
    (venv_site / "old_base_site.pth").write_text(base_site + "\n", encoding="utf-8")
    seen = _run([venv / "bin" / "python", "-m", "pip", "--version"]).stdout
    _require(seen.startswith("pip 25.3 "), f"{name}: before any bootstrap, -m pip resolves the old base pip: {seen.strip()}")
    return venv / "bin" / "python"


def _homebrew_current_sequence_fails(base_python: Path, root: Path) -> None:
    venv_python = _formula_venv(base_python, root, "control-homebrew")
    done = _run([base_python, "-m", "pip", f"--python={venv_python}", "install", *std_pip_args(build_isolation=True), _WHEELS / _RESOURCE_WHEELS[2]])
    _require(done.returncode != 0 and _REJECTED in done.stderr, f"CONTROL 1: Homebrew's base-pip sequence fails with `{_REJECTED}`")


def _unbootstrapped_venv_pip_fails(base_python: Path, root: Path) -> None:
    venv_python = _formula_venv(base_python, root, "control-no-bootstrap")
    done = _run([venv_python, "-m", "pip", f"--python={venv_python}", "install", *std_pip_args(build_isolation=True), _WHEELS / _RESOURCE_WHEELS[2]])
    _require(done.returncode != 0 and _REJECTED in done.stderr, "CONTROL 2: the venv's -m pip without the bootstrap still runs the old pip and fails")


def _formula_sequence_succeeds(base_python: Path, root: Path) -> None:
    venv_python = _formula_venv(base_python, root, "candidate")
    pip_wheel = _WHEELS / _PIP_WHEEL
    # resource("pip").stage { system venv_python, wheel/"pip", "install", *std_pip_args(prefix: false), wheel }
    booted = _run([venv_python, pip_wheel / "pip", "install", *std_pip_args(build_isolation=False), pip_wheel])
    _require(booted.returncode == 0, "CANDIDATE: pip bootstrapped into the venv from its own wheel", booted.stderr[-300:])
    own = _run([venv_python, "-c", "import pip, sys; print(pip.__version__, pip.__file__.startswith(sys.prefix))"]).stdout.split()
    _require(own == ["26.2.1", "True"], f"CANDIDATE: the venv's -m pip is now its own pip 26.2.1: {own}")
    # venv = virtualenv_create(libexec, venv_python); venv.pip_install resources (Virtualenv#do_install)
    resources = _run([venv_python, "-m", "pip", f"--python={venv_python}", "install", *std_pip_args(build_isolation=True), *(_WHEELS / wheel for wheel in _RESOURCE_WHEELS)])
    _require(resources.returncode == 0, "CANDIDATE: the resources install with the venv's pip and std_pip_args", resources.stderr[-300:])
    for target in ("solet_setup_contracts", "solet_cli"):
        built = _run([venv_python, "-m", "pip", f"--python={venv_python}", "install", *std_pip_args(build_isolation=False), _REPOSITORY / target])
        _require(built.returncode == 0, f"CANDIDATE: {target} installs with build_isolation: false", built.stderr[-300:])
    version = _run([venv_python.parent / "solet", "--version"])
    _require(version.returncode == 0 and version.stdout.strip() == "solet 0.1.0", "CANDIDATE: the installed Manager runs", version.stdout + version.stderr[-200:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-pip-wheel", type=Path, required=True, help="pip-25.3-py3-none-any.whl (sha256-checked)")
    parser.add_argument("--python", type=Path, default=_DEFAULT_PYTHON, help="a Python 3.13 to build the base on")
    options = parser.parse_args()
    old_wheel: Path = options.old_pip_wheel.resolve()
    _require(old_wheel.name == "pip-25.3-py3-none-any.whl" and _sha256(old_wheel) == _OLD_PIP_SHA256, "the old pip wheel is PyPI's pip 25.3")
    _require(_sha256(_WHEELS / _PIP_WHEEL) == _pinned_pip_sha256(), "the vendored pip wheel matches the Formula's pinned pip resource")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        base_python = _old_base(options.python, old_wheel, root)
        _homebrew_current_sequence_fails(base_python, root)
        _unbootstrapped_venv_pip_fails(base_python, root)
        _formula_sequence_succeeds(base_python, root)
    print("pip_bootstrap_proof: the Formula sequence installs on an old base pip; Homebrew's base-pip sequence does not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
