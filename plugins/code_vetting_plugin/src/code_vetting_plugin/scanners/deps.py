"""Dependency-risk + license scanners.

- ``pip-audit`` over the active environment → known-CVE dependency findings,
  attributed to the ``pyproject.toml`` that declares the package where possible.
- ``osv-scanner`` is optional; absent → coverage gap.
- License sweep enforces RB-LICENSE: each shipping ``pyproject`` declares a
  *bare SPDX string* (never ``license-files`` / a license table) drawn from the
  two-bucket set {Apache-2.0 (distributed), LicenseRef-Proprietary (undistributed)};
  MIT/GPL/AGPL declarations are flagged.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..coverage import CoverageRecord, ScannerResult
from ..models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from ..targets import TargetTree
from ..toolrun import run, tool_available, tool_unavailable_reason, tool_version

_PIP_AUDIT = "pip-audit"
_OSV = "osv-scanner"

_ALLOWED_SPDX = frozenset({"Apache-2.0", "LicenseRef-Proprietary"})

# The PINNED python lockfiles osv-scanner reads (a subset of python_dependency_manifests() —
# NOT bare pyproject/setup, which declare RANGES, not pins). osv covers npm lockfiles natively.
_OSV_PY_LOCKFILE_NAMES: frozenset[str] = frozenset({"poetry.lock", "pipfile.lock", "uv.lock"})


def _is_osv_py_lockfile(name_lower: str) -> bool:
    return name_lower in _OSV_PY_LOCKFILE_NAMES or (name_lower.startswith("requirements") and name_lower.endswith(".txt"))


def _relativize(root: Path, path_field: str) -> str:
    """Repo-relative path for a tool's absolute path field (own copy — cross-plugin-import rule)."""
    if not path_field:
        return path_field
    candidate = Path(path_field)
    if not candidate.is_absolute():
        return path_field
    try:
        return str(candidate.relative_to(root))
    except ValueError:
        return path_field


def _as_str(value: Any) -> str:  # noqa: ANN401 — narrows untyped tool JSON
    return value if isinstance(value, str) else ""


def _declaring_pyproject(tree: TargetTree, package: str) -> str:
    """Best-effort attribution of a package to the shipping pyproject that names it.

    Skips test-fixture manifests so an installed-env CVE is not mis-attributed to
    a fixture; falls back to a generic environment marker when no shipping manifest
    declares the package (a transitive/installed dependency).
    """
    needle = package.lower()
    for rel in tree.pyproject_files():
        if "/tests/" in rel or "/fixtures/" in rel:
            continue
        text = tree.abspath(rel).read_text(encoding="utf-8").lower()
        if needle in text:
            return rel
    return "environment (installed/transitive dependency)"


def _iter_dep_vulns(dependencies: object) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (package, installed_version, vuln-object) for each pip-audit vuln."""
    if not isinstance(dependencies, list):
        raise RuntimeError("pip-audit JSON missing a dependencies array")
    for dep in dependencies:
        if not isinstance(dep, dict):
            continue
        vulns = dep.get("vulns")
        if not isinstance(vulns, list):
            continue
        name = _as_str(dep.get("name"))
        installed = _as_str(dep.get("version"))
        for vuln in vulns:
            if isinstance(vuln, dict):
                yield name, installed, vuln


def _fix_versions(vuln: dict[str, Any]) -> str:
    fixes = vuln.get("fix_versions")
    return ", ".join(v for v in fixes if isinstance(v, str)) if isinstance(fixes, list) else ""


def _pip_audit_findings(tree: TargetTree, payload: dict[str, Any], run_id: str, version: str | None) -> list[Finding]:
    findings: list[Finding] = []
    for name, installed, vuln in _iter_dep_vulns(payload.get("dependencies")):
        vuln_id = _as_str(vuln.get("id")) or "unknown"
        fix_str = _fix_versions(vuln)
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.DEPS,
                severity=Severity.HIGH,
                file=_declaring_pyproject(tree, name),
                line=None,
                constraint_violated=f"pip-audit:{vuln_id}",
                evidence=f"{name}=={installed} has {vuln_id}; fixed in: {fix_str or 'n/a'}",
                fix_suggestion=f"Raise the pin/floor for {name} to a fixed version ({fix_str or 'see advisory'}).",
                provenance=Provenance(source=_PIP_AUDIT, tool_version=version, rule_id=vuln_id),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings


def _pip_audit_foreign_gap(tree: TargetTree) -> ScannerResult:
    """A foreign target NEVER triggers pip-audit's environment-audit mode.

    Run with no manifest, pip-audit falls back to auditing the ACTIVE environment —
    which for a foreign scan is THIS engine's own venv, so its CVEs would be
    mis-attributed to the target (FT-1.1 defect 1: pip==25.3 findings stamped onto a
    TS target). A target with no python dependency manifest is genuinely
    ``not_applicable``; a target that DOES carry manifests is a deferred coverage gap —
    never a silent environment audit. R7-3 NARROWS that gap: osv-scanner now covers the
    lockfile-PINNED foreign python deps, so only UNPINNED manifests remain a Phase-2 need.
    """
    if tree.python_dependency_manifests():
        reason = (
            "foreign python dependencies not audited by pip-audit — osv-scanner (R7-3) covers "
            "lockfile-pinned deps (poetry/pipfile/uv/requirements); only UNPINNED manifests (a bare "
            "pyproject/requirements range needing hermetic resolution) remain a Phase-2 foreign-deps "
            "gap; pip-audit's environment-audit mode is self-vet-only"
        )
    else:
        reason = "not_applicable: no python dependency manifests in target (foreign)"
    return ScannerResult(
        findings=[],
        coverage=CoverageRecord(scanner=_PIP_AUDIT, ran=False, files_examined=0, gap_reason=reason),
    )


def scan_pip_audit(tree: TargetTree, run_id: str) -> ScannerResult:
    if not tool_available(_PIP_AUDIT):
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_PIP_AUDIT, ran=False, files_examined=0, gap_reason=tool_unavailable_reason(_PIP_AUDIT)
            ),
        )
    if tree.foreign:
        # Self-vet audits our deployed environment (below); a foreign target must not —
        # its findings would be THIS engine's venv, not the target's (FT-1.1 defect 1).
        return _pip_audit_foreign_gap(tree)
    version = tool_version(_PIP_AUDIT)
    outcome = run(
        [_PIP_AUDIT, "--format", "json", "--progress-spinner", "off"],
        cwd=str(tree.root),
        timeout_s=300,
        raise_on_timeout=False,
    )
    if outcome.timed_out:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_PIP_AUDIT, ran=False, files_examined=0, gap_reason="pip-audit timed out (offline advisory DB)"
            ),
        )
    try:
        parsed: Any = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_PIP_AUDIT,
                ran=False,
                files_examined=0,
                gap_reason=f"pip-audit produced no JSON (likely offline advisory DB): {outcome.stderr.strip()[:200]}",
            ),
        )
    if not isinstance(parsed, dict):
        raise RuntimeError("pip-audit did not emit a JSON object")
    findings = _pip_audit_findings(tree, parsed, run_id, version)
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_PIP_AUDIT, ran=True, files_examined=len(tree.pyproject_files())),
    )


def _osv_lockfiles(tree: TargetTree) -> tuple[str, ...]:
    """Every lockfile osv can SCA in the ENUMERATED tree: npm lockfiles + PINNED python
    lockfiles (poetry/pipfile/uv + requirements*.txt). Enumeration-driven — a materialized
    ``node_modules`` on disk is never traversed because it was excluded at enumeration, so this
    closes the walk-mode materialized-junk residual for this scanner by construction (R7-3)."""
    python = tuple(m for m in tree.python_dependency_manifests() if _is_osv_py_lockfile(Path(m).name.lower()))
    return tuple(sorted({*tree.npm_lockfiles(), *python}))


def _osv_major(version: str | None) -> int | None:
    """osv-scanner MAJOR version, or None if unparseable (→ a loud gap, never a guessed invocation)."""
    if not version:
        return None
    match = re.search(r"(\d+)\.\d+", version)
    return int(match.group(1)) if match else None


def _osv_argv(version: str | None, lockfile_args: list[str]) -> list[str] | None:
    """The version-PINNED osv invocation. v2: ``scan source --lockfile …``; v1: bare ``--lockfile …``.
    A version we cannot shape-match returns None so the caller records a loud gap, not a guess."""
    major = _osv_major(version)
    if major is not None and major >= 2:
        return [_OSV, "scan", "source", *lockfile_args, "--format", "json"]
    if major == 1:
        return [_OSV, *lockfile_args, "--format", "json"]
    return None


def scan_osv(tree: TargetTree, run_id: str) -> ScannerResult:
    """osv-scanner over each ENUMERATED lockfile (npm + pinned python) — target-scoped SCA (R7-3)."""
    if not tool_available(_OSV):
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_OSV, ran=False, files_examined=0,
                gap_reason=f"{tool_unavailable_reason(_OSV)} — lockfile SCA cross-check not run"
            ),
        )
    lockfiles = _osv_lockfiles(tree)
    if not lockfiles:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_OSV, ran=False, files_examined=0,
                gap_reason="not_applicable: no lockfiles (npm/poetry/pipfile/uv/requirements) in target",
            ),
        )
    version = tool_version(_OSV)
    lockfile_args = [arg for rel in lockfiles for arg in ("--lockfile", str(tree.abspath(rel)))]
    argv = _osv_argv(version, lockfile_args)
    if argv is None:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_OSV, ran=False, files_examined=0,
                gap_reason=f"osv-scanner version not shape-matched (got {version!r}) — refusing to guess the --lockfile invocation",
            ),
        )
    # Enumeration-driven explicit lockfile paths — never `-r <root>` (which would traverse a
    # materialized node_modules). Online OSV.dev API by default; timeout/offline → gap, never a masked pass.
    outcome = run(argv, cwd=str(tree.root), timeout_s=300, raise_on_timeout=False)
    if outcome.timed_out:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_OSV, ran=False, files_examined=0, gap_reason="osv-scanner timed out (offline / slow OSV.dev API)"
            ),
        )
    try:
        parsed: Any = json.loads(outcome.stdout)
    except json.JSONDecodeError:
        return ScannerResult(
            findings=[],
            coverage=CoverageRecord(
                scanner=_OSV, ran=False, files_examined=0, gap_reason=f"osv-scanner emitted no JSON: {outcome.stderr.strip()[:200]}"
            ),
        )
    findings = _osv_findings(tree, parsed, run_id, version)
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner=_OSV, ran=True, files_examined=len(lockfiles)),
    )


def _iter_pkg_vulns(pkg: object) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (package-name, vuln-object) for one osv-scanner package entry."""
    if not isinstance(pkg, dict):
        return
    vulns = pkg.get("vulnerabilities")
    if not isinstance(vulns, list):
        return
    info = pkg.get("package")
    name = _as_str(info.get("name")) if isinstance(info, dict) else ""
    for vuln in vulns:
        if isinstance(vuln, dict):
            yield name, vuln


def _iter_osv_vulns(payload: object) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield (source-lockfile-path, package-name, vuln-object) across an osv-scanner report.

    R7-3: each ``results`` entry carries its ``source.path`` (the lockfile scanned), so a finding
    is attributed to the lockfile that pins the vulnerable package — NOT ``_declaring_pyproject``
    (python-manifest-specific; it would mis-attribute an npm CVE to a python-env marker)."""
    if not isinstance(payload, dict):
        return
    results = payload.get("results")
    if not isinstance(results, list):
        return
    for result in results:
        if not isinstance(result, dict):
            continue
        source = result.get("source")
        source_path = _as_str(source.get("path")) if isinstance(source, dict) else ""
        packages = result.get("packages")
        if isinstance(packages, list):
            for pkg in packages:
                for name, vuln in _iter_pkg_vulns(pkg):
                    yield source_path, name, vuln


def _osv_findings(tree: TargetTree, payload: object, run_id: str, version: str | None) -> list[Finding]:
    findings: list[Finding] = []
    for source_path, name, vuln in _iter_osv_vulns(payload):
        vuln_id = _as_str(vuln.get("id")) or "unknown"
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.DEPS,
                severity=Severity.HIGH,
                file=_relativize(tree.root, source_path),
                line=None,
                constraint_violated=f"osv-scanner:{vuln_id}",
                evidence=f"{name}: {vuln_id} ({_as_str(vuln.get('summary'))[:160]})",
                fix_suggestion="Raise the dependency version above the vulnerable range.",
                provenance=Provenance(source=_OSV, tool_version=version, rule_id=vuln_id),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings


def _license_of(raw: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (declared SPDX string or None, uses_non_bare_form)."""
    project = raw.get("project")
    if not isinstance(project, dict):
        return None, False
    license_value = project.get("license")
    if isinstance(license_value, str):
        return license_value, False
    if isinstance(license_value, dict):
        # A license table ({file=...} or {text=...}) is the non-bare form RB-LICENSE forbids.
        return None, True
    if "license-files" in project:
        return None, True
    return None, False


def _is_shipping_plugin_manifest(rel: str) -> bool:
    """True for a top-level shipping plugin manifest ``plugins/<X>/pyproject.toml``.

    RB-LICENSE's two-bucket convention is plugin-scoped; test-fixture plugins,
    ``disabled_plugins/``, operator-tooling, and the core ``ananta`` manifest are
    out of this sweep's scope.
    """
    parts = rel.split("/")
    return len(parts) == 3 and parts[0] == "plugins" and parts[2] == "pyproject.toml"


def scan_licenses(tree: TargetTree, run_id: str) -> ScannerResult:
    """Enforce RB-LICENSE over every shipping plugin pyproject."""
    findings: list[Finding] = []
    examined = 0
    for rel in tree.pyproject_files():
        if not _is_shipping_plugin_manifest(rel):
            continue
        examined += 1
        raw = tomllib.loads(tree.abspath(rel).read_text(encoding="utf-8"))
        spdx, non_bare = _license_of(raw)
        if non_bare:
            findings.append(
                Finding.build(
                    run_id=run_id,
                    layer=Layer.L1_DETERMINISTIC,
                    dimension=Dimension.LICENSE,
                    severity=Severity.MEDIUM,
                    file=rel,
                    line=None,
                    constraint_violated="RB-LICENSE:non-bare-spdx",
                    evidence="license declared via a table / license-files (build-break trap); RB-LICENSE requires a bare SPDX string",
                    fix_suggestion='Use `license = "Apache-2.0"` (or "LicenseRef-Proprietary") as a bare string; drop license-files.',
                    provenance=Provenance(source="gate:license_sweep", rule_id="RB-LICENSE"),
                    context_profile=ContextProfile.PRODUCTION,
                )
            )
            continue
        if spdx is None:
            findings.append(
                Finding.build(
                    run_id=run_id,
                    layer=Layer.L1_DETERMINISTIC,
                    dimension=Dimension.LICENSE,
                    severity=Severity.LOW,
                    file=rel,
                    line=None,
                    constraint_violated="RB-LICENSE:missing-spdx",
                    evidence="pyproject declares no bare SPDX license string",
                    fix_suggestion='Add a bare SPDX `license` field ("Apache-2.0" or "LicenseRef-Proprietary").',
                    provenance=Provenance(source="gate:license_sweep", rule_id="RB-LICENSE"),
                    context_profile=ContextProfile.PRODUCTION,
                )
            )
            continue
        if spdx not in _ALLOWED_SPDX:
            findings.append(
                Finding.build(
                    run_id=run_id,
                    layer=Layer.L1_DETERMINISTIC,
                    dimension=Dimension.LICENSE,
                    severity=Severity.MEDIUM,
                    file=rel,
                    line=None,
                    constraint_violated="RB-LICENSE:non-two-bucket-spdx",
                    evidence=f"license '{spdx}' is outside the two-bucket set {{Apache-2.0, LicenseRef-Proprietary}}",
                    fix_suggestion="Relicense to Apache-2.0 (distributed) or LicenseRef-Proprietary (undistributed).",
                    provenance=Provenance(source="gate:license_sweep", rule_id="RB-LICENSE"),
                    context_profile=ContextProfile.PRODUCTION,
                )
            )
    return ScannerResult(
        findings=findings,
        coverage=CoverageRecord(scanner="license_sweep", ran=True, files_examined=examined),
    )
