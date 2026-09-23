"""osv_lockfile_smoke.py — R7-3: osv-scanner is per-lockfile, multi-ecosystem, attributed right.

R7-3 makes osv the ONE lockfile-SCA surface for every ecosystem: enumeration-driven per-lockfile
invocation (npm + pinned python), a CLI-version-pinned invocation (v1 bare ``--lockfile`` vs v2
``scan source --lockfile``), findings anchored to the LOCKFILE that pins the vulnerable package
(not ``_declaring_pyproject``, which would mis-attribute an npm CVE to a python-env marker), and
``files_examined`` = the count of lockfiles scanned (was wrongly ``len(pyproject_files())``).

Pins (all HERMETIC — no live OSV.dev call; the design keeps the live SCA run to build-verify only):
  * ``_osv_lockfiles``: npm lockfiles + pinned python lockfiles (poetry/pipfile/uv + requirements*.txt);
    a bare pyproject/setup is NOT a lockfile (declares ranges, not pins); no lockfile → ().
  * ``_osv_major`` / ``_osv_argv``: version shape-match — v2 → ``scan source --lockfile``, v1 → bare
    ``--lockfile``, unparseable → None (a loud gap, never a guessed invocation).
  * ``_osv_findings``: a canned osv-v2 report attributes each finding to its lockfile ``source.path``
    (relativized), DEPS dimension, ``osv-scanner:<id>`` constraint.
  * ``scan_osv`` no-lockfile gap: a target with no lockfiles records an honest ``not_applicable`` (this
    path returns BEFORE any subprocess, so it is hermetic).

Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.models import Dimension
from code_vetting_plugin.scanners.deps import (  # noqa: PLC2701 — pin the internal lockfile/argv/attribution logic
    _OSV,
    _osv_argv,
    _osv_findings,
    _osv_lockfiles,
    _osv_major,
    scan_osv,
)
from code_vetting_plugin.targets import TargetTree
from code_vetting_plugin.toolrun import tool_available

_CHECKS_RUN: list[str] = []

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE. Undeclared-dependency audit:
# workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SKIP_EXIT_CODE = 77
_osv_check_skipped = False


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _tree(tracked: tuple[str, ...], *, foreign: bool = False) -> TargetTree:
    return TargetTree(root=Path("/t"), tracked=tracked, enumeration="git", foreign=foreign)


def _check_lockfile_enumeration() -> None:
    mixed = _tree((
        "package-lock.json", "web/yarn.lock", "poetry.lock", "requirements.txt",
        "pyproject.toml", "setup.py", "src/a.py", "web/app.ts",
    ))
    got = set(_osv_lockfiles(mixed))
    _check(
        "osv lockfiles = npm + pinned-python lockfiles (NOT pyproject/setup)",
        got == {"package-lock.json", "web/yarn.lock", "poetry.lock", "requirements.txt"},
        str(sorted(got)),
    )
    _check("a pyproject/setup-only tree has NO osv lockfiles", _osv_lockfiles(_tree(("pyproject.toml", "setup.py"))) == (), "")
    _check("uv.lock + Pipfile.lock count", set(_osv_lockfiles(_tree(("uv.lock", "Pipfile.lock")))) == {"uv.lock", "Pipfile.lock"}, "")


def _check_version_argv() -> None:
    _check("osv major parses from a version line", _osv_major("osv-scanner version: 2.4.0") == 2, "")
    _check("osv major parses a bare 1.x", _osv_major("1.9.2") == 1, "")
    _check("unparseable version -> None", _osv_major("weird") is None and _osv_major(None) is None, "")
    args = ["--lockfile", "/t/package-lock.json"]
    v2 = _osv_argv("2.4.0", args)
    _check("v2 invocation is `scan source --lockfile`", v2 == [_OSV, "scan", "source", "--lockfile", "/t/package-lock.json", "--format", "json"], str(v2))
    v1 = _osv_argv("1.9.0", args)
    _check("v1 invocation is bare `--lockfile`", v1 == [_OSV, "--lockfile", "/t/package-lock.json", "--format", "json"], str(v1))
    _check("unshapeable version -> None argv (loud gap, not a guess)", _osv_argv("nope", args) is None, "")


def _check_attribution() -> None:
    payload = {
        "results": [
            {
                "source": {"path": "/t/package-lock.json", "type": "lockfile"},
                "packages": [
                    {"package": {"name": "lodash", "version": "4.17.0", "ecosystem": "npm"},
                     "vulnerabilities": [{"id": "GHSA-p6mc-m468-83gw", "summary": "prototype pollution in lodash"}]}
                ],
            }
        ]
    }
    findings = _osv_findings(_tree(("package-lock.json",)), payload, "vr-r73", "2.4.0")
    _check("one osv vuln -> one finding", len(findings) == 1, str(findings))
    _check("finding is a DEPS dimension", findings[0].dimension is Dimension.DEPS, str(findings[0].dimension))
    _check("finding is attributed to the LOCKFILE (not a python-env marker)", findings[0].file == "package-lock.json", findings[0].file)
    _check("finding pins the osv vuln id", findings[0].constraint_violated == "osv-scanner:GHSA-p6mc-m468-83gw", findings[0].constraint_violated)
    _check("evidence names the package + summary", "lodash" in findings[0].evidence and "prototype pollution" in findings[0].evidence, findings[0].evidence)


def _check_no_lockfile_gap() -> None:
    # A python-source tree with no lockfile: osv records an honest not_applicable, before any subprocess.
    cov = scan_osv(_tree(("src/a.py", "pyproject.toml")), "vr-r73").coverage
    _check("osv on a no-lockfile tree: ran=False", cov.ran is False, str(cov))
    global _osv_check_skipped
    if tool_available(_OSV):
        _check(
            "osv no-lockfile gap is a distinct not_applicable (hermetic — no subprocess)",
            (cov.gap_reason or "").startswith("not_applicable:") and "no lockfiles" in (cov.gap_reason or ""),
            str(cov.gap_reason),
        )
    else:
        print("  SKIP  osv-scanner no-lockfile gap-reason assertion: osv-scanner not on PATH")
        _osv_check_skipped = True


def main() -> int:
    try:
        _check_lockfile_enumeration()
        _check_version_argv()
        _check_attribution()
        _check_no_lockfile_gap()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"osv_lockfile_smoke OK: {len(_CHECKS_RUN)} checks passed")
    if _osv_check_skipped:
        print(
            "SKIP: osv-scanner not on PATH -- the osv-specific gap-reason "
            "assertion disclosed a gap rather than running; every other "
            "check ran and passed."
        )
        return _SKIP_EXIT_CODE
    return 0


if __name__ == "__main__":
    sys.exit(main())
