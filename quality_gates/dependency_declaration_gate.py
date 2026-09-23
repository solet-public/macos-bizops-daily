#!/usr/bin/env python3
"""Static declared-dependency closure gate (GTE-15).

For every repository package with both ``pyproject.toml`` and ``src/``, this
gate inspects its owned Python source without importing it.  A finding blocks
only when all of the following are proved:

* an import appears directly in a module body (not under any control flow,
  function, or class);
* its root is neither relative, local to that package, nor from the stdlib;
* installed distribution metadata maps that root to a distribution; and
* none of those distributions is declared in ``project.dependencies``.

The deliberately narrow proof rule prevents a guarded optional import from
becoming a false required dependency.  A guarded or nested import, an import
root without an installed distribution mapping, and a dependency found only in
``project.optional-dependencies`` are reported as UNKNOWN, never as
violations -- UNLESS the import line carries the explicit opt-out marker (see
below), in which case it is reported as DECLARED_OPTIONAL_CAPABILITY: still
non-blocking, but classified rather than merely unknown.

**The optional-capability opt-out marker.** A guarded/nested import that is
deliberately never declared (the ``iterm2`` case: the capability ships only
with a sibling distribution a headless profile excludes) carries a trailing
comment on the *same source line* as the import statement:

    import iterm2  # dependency-declaration: optional-capability reason="..."

The tag ``# dependency-declaration: optional-capability`` is matched as a
literal substring, case-sensitive; everything after it is freeform
documentation for human reviewers and is not further parsed by the gate. The
marker is deliberately explicit rather than inferred from the *presence* of a
``try``/``except`` -- an unmarked guarded import stays UNKNOWN (unclassified
backlog); only a marked one is DECLARED_OPTIONAL_CAPABILITY. This keeps the
opt-out a reviewable code change at the import site, not a side-registry
entry disconnected from the reasoning it documents.

**The tracked-debt allowlist.** ``--allowlist <path>`` names a content-anchored
register of ``missing_required_dependency::<package>::<import_root>`` lines
(one per pre-existing blocking finding, ``#`` comments and blank lines
ignored) -- the transition mechanism for the campaign that repairs the
existing backlog without holding open every commit in the meantime. Matching
findings still print (prefixed ``[allowlisted]``) and still count in the
summary, but do not contribute to the exit-2 verdict. A NEW missing
declaration -- any ``(package, import_root)`` pair absent from the allowlist
-- blocks immediately. Removing an entry is the unit of remediation progress;
adding one without operator ratification defeats the gate (platform's Gate
Allowlist Conventions). The key is ``(package, import_root)``, not line
number, so it survives the source moving around while the same package is
still missing the same declaration.

Exit codes: 0 clean/unknown-only/fully allowlisted, 2 proven violations not
covered by the allowlist or invalid inspected metadata/source, 64 argparse
usage error.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import re
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

_PACKAGE_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SKIP_DIRS = frozenset({"__pycache__", ".git", ".mypy_cache", ".pytest_cache"})
_STDLIB_IMPORT_ROOTS = frozenset(name.lower() for name in sys.stdlib_module_names)
_OPTIONAL_CAPABILITY_MARKER = "# dependency-declaration: optional-capability"


class FindingKind(StrEnum):
    """Classification emitted for one static import site."""

    MISSING_REQUIRED_DEPENDENCY = "missing_required_dependency"
    UNKNOWN_GUARDED_OR_NESTED = "unknown_guarded_or_nested"
    UNKNOWN_OPTIONAL_ONLY = "unknown_optional_only"
    UNKNOWN_DISTRIBUTION_MAPPING = "unknown_distribution_mapping"
    DECLARED_OPTIONAL_CAPABILITY = "declared_optional_capability"
    INVALID_INPUT = "invalid_input"


class _NotPackageDeclarationError(ValueError):
    """Raised for a tool configuration that deliberately lacks ``[project]``."""


@dataclass(frozen=True)
class PackageSpec:
    """One source package and the declarations governing it."""

    root: Path
    source_root: Path
    required_dependencies: frozenset[str]
    optional_dependencies: frozenset[str]
    local_import_roots: frozenset[str]


@dataclass(frozen=True)
class Finding:
    """One machine-readable gate outcome."""

    kind: FindingKind
    package: str
    source: str
    line: int
    import_root: str
    message: str

    @property
    def blocking(self) -> bool:
        """Whether the finding proves a gate failure."""

        return self.kind in {
            FindingKind.MISSING_REQUIRED_DEPENDENCY,
            FindingKind.INVALID_INPUT,
        }


@dataclass(frozen=True)
class ScanReport:
    """Complete report for one repository scan."""

    findings: tuple[Finding, ...]

    @property
    def blocking_findings(self) -> tuple[Finding, ...]:
        """Findings that make the command exit nonzero."""

        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def unknown_findings(self) -> tuple[Finding, ...]:
        """Findings deliberately excluded from the blocking verdict."""

        return tuple(finding for finding in self.findings if not finding.blocking)


def _normalize_distribution_name(value: str) -> str:
    """Return the PEP 503-normalized distribution name."""

    match = _PACKAGE_NAME.match(value)
    if match is None:
        raise ValueError(f"dependency name is not parseable: {value!r}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _normalize_import_root(value: str) -> str:
    """Return a case-normalized Python import root, including private modules."""

    return value.partition(".")[0].lower()


def _relative(repo_root: Path, path: Path) -> str:
    """Render a stable repository-relative path."""

    return path.relative_to(repo_root).as_posix()


def _candidate_pyprojects(repo_root: Path) -> tuple[Path, ...]:
    """Return package metadata in the repository's shipped package layout."""

    candidates = {repo_root / "pyproject.toml"}
    candidates.update(repo_root.glob("*/pyproject.toml"))
    candidates.update((repo_root / "plugins").glob("*/pyproject.toml"))
    return tuple(sorted(path for path in candidates if path.is_file()))


def _dependency_names(value: object, *, context: str) -> frozenset[str]:
    """Read a closed array of PEP 508 requirement strings by name only."""

    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{context} must be an array of requirement strings")
    try:
        return frozenset(_normalize_distribution_name(item) for item in value)
    except ValueError as exc:
        raise ValueError(f"{context}: {exc}") from exc


def _local_import_roots(source_root: Path) -> frozenset[str]:
    """Return direct source-root names, including namespace-package directories."""

    roots: set[str] = set()
    for entry in source_root.iterdir():
        if entry.name in _SKIP_DIRS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            roots.add(_normalize_import_root(entry.name))
        elif entry.suffix == ".py" and entry.name != "__init__.py":
            roots.add(_normalize_import_root(entry.stem))
    return frozenset(roots)


def _read_package(pyproject: Path) -> PackageSpec:
    """Load one package declaration or fail with a closed metadata error."""

    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read valid TOML: {exc}") from exc
    project = payload.get("project")
    if not isinstance(project, dict):
        raise _NotPackageDeclarationError("pyproject lacks a [project] table")
    name = project.get("name")
    if not isinstance(name, str):
        raise ValueError("[project].name is not a string")
    required = _dependency_names(project.get("dependencies"), context="project.dependencies")
    optional_raw = project.get("optional-dependencies")
    if optional_raw is None:
        optional = frozenset()
    elif not isinstance(optional_raw, dict):
        raise ValueError("project.optional-dependencies is not a TOML table")
    else:
        optional = frozenset(
            dependency
            for group_name, group_value in optional_raw.items()
            for dependency in _dependency_names(
                group_value,
                context=f"project.optional-dependencies.{group_name}",
            )
        )
    source_root = pyproject.parent / "src"
    if not source_root.is_dir():
        raise ValueError("package lacks a src directory")
    return PackageSpec(
        root=pyproject.parent,
        source_root=source_root,
        required_dependencies=required,
        optional_dependencies=optional,
        local_import_roots=_local_import_roots(source_root),
    )


def _source_files(source_root: Path) -> Iterable[Path]:
    """Yield every owned Python source file, excluding caches."""

    for path in sorted(source_root.rglob("*.py")):
        if any(part in _SKIP_DIRS or part.startswith(".venv") for part in path.parts):
            continue
        yield path


def _module_imports(
    tree: ast.Module,
) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...]]:
    """Return direct imports and all non-direct imports as ``(line, root)`` pairs."""

    direct: list[tuple[int, str]] = []
    for node in tree.body:
        direct.extend(_import_from_node(node))
    direct_set = {(line, root) for line, root in direct}
    nested: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for item in _import_from_node(node):
            if item not in direct_set:
                nested.append(item)
    return tuple(direct), tuple(nested)


def _import_from_node(node: ast.AST) -> tuple[tuple[int, str], ...]:
    """Extract non-relative, non-future import roots from one AST import node."""

    if isinstance(node, ast.Import):
        return tuple(
            (node.lineno, _normalize_import_root(alias.name))
            for alias in node.names
            if alias.name.partition(".")[0] != "__future__"
        )
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
        root = node.module.partition(".")[0]
        if root != "__future__":
            return ((node.lineno, _normalize_import_root(root)),)
    return ()


def _has_optional_capability_marker(source_lines: list[str], line: int) -> bool:
    """True if the 1-indexed source ``line`` carries the opt-out marker.

    A plain substring match on the physical line the import statement sits
    on -- deliberately not a semantic check of the surrounding guard shape.
    The marker itself, not an inferred try/except pattern, is what makes an
    opt-out explicit and machine-readable (see module docstring).
    """

    if line < 1 or line > len(source_lines):
        return False
    return _OPTIONAL_CAPABILITY_MARKER in source_lines[line - 1]


def _load_allowlist(path: Path) -> frozenset[tuple[str, str]]:
    """Read a `<package>::<import_root>` allowlist file.

    The key is content-anchored (package + import root), not line number,
    so it survives the source moving around while the package is still
    missing the same declaration. Mandatory owner/reason/expires schema
    (D-3-structural, iss_23fa51b5).
    """

    entries: set[tuple[str, str]] = set()
    for key in _load_tagged_allowlist(path):
        if "::" not in key:
            print(f"WARN: malformed allowlist line (missing '::'): {key!r}", file=sys.stderr)
            continue
        package_part, root_part = key.split("::", 1)
        entries.add((package_part.strip(), root_part.strip()))
    return frozenset(entries)


def _finding_is_allowlisted(finding: Finding, allowlist: frozenset[tuple[str, str]]) -> bool:
    """True if this blocking finding's (package, import_root) is tracked debt."""

    return (
        finding.kind is FindingKind.MISSING_REQUIRED_DEPENDENCY
        and (finding.package, finding.import_root) in allowlist
    )


def _distribution_map() -> dict[str, frozenset[str]]:
    """Map import roots to installed distribution names without importing targets."""

    raw = importlib.metadata.packages_distributions()
    return {
        _normalize_import_root(root): frozenset(
            _normalize_distribution_name(distribution) for distribution in distributions
        )
        for root, distributions in raw.items()
        if distributions
    }


def _classify_direct_import(
    *,
    package: PackageSpec,
    source: Path,
    line: int,
    import_root: str,
    repo_root: Path,
    distributions: dict[str, frozenset[str]],
) -> Finding | None:
    """Classify a direct import, returning no finding for proven local declarations."""

    if import_root in _STDLIB_IMPORT_ROOTS or import_root in package.local_import_roots:
        return None
    package_path = _relative(repo_root, package.root)
    source_path = _relative(repo_root, source)
    mapped = distributions.get(import_root)
    if not mapped:
        return Finding(
            FindingKind.UNKNOWN_DISTRIBUTION_MAPPING,
            package_path,
            source_path,
            line,
            import_root,
            "installed metadata does not prove which distribution supplies this import root",
        )
    if mapped & package.required_dependencies:
        return None
    if mapped & package.optional_dependencies:
        return Finding(
            FindingKind.UNKNOWN_OPTIONAL_ONLY,
            package_path,
            source_path,
            line,
            import_root,
            "only an optional dependency group declares a mapped distribution",
        )
    return Finding(
        FindingKind.MISSING_REQUIRED_DEPENDENCY,
        package_path,
        source_path,
        line,
        import_root,
        f"mapped distributions {sorted(mapped)} are absent from project.dependencies",
    )


def _classify_nested_import(
    *,
    package: PackageSpec,
    source: Path,
    line: int,
    import_root: str,
    repo_root: Path,
    source_lines: list[str],
) -> Finding | None:
    """Classify one guarded/nested import, or ``None`` for a proven exclusion."""

    if import_root in _STDLIB_IMPORT_ROOTS or import_root in package.local_import_roots:
        return None
    package_path = _relative(repo_root, package.root)
    source_path = _relative(repo_root, source)
    if _has_optional_capability_marker(source_lines, line):
        return Finding(
            FindingKind.DECLARED_OPTIONAL_CAPABILITY,
            package_path,
            source_path,
            line,
            import_root,
            "explicitly marked optional-capability at the import site",
        )
    return Finding(
        FindingKind.UNKNOWN_GUARDED_OR_NESTED,
        package_path,
        source_path,
        line,
        import_root,
        "import is not directly in the module body",
    )


def _scan_source_file(
    *, source: Path, package: PackageSpec, repo_root: Path, distributions: dict[str, frozenset[str]]
) -> list[Finding]:
    """Scan one source file for its declared-vs-actual import findings."""

    try:
        source_text = source.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=str(source))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return [
            Finding(
                FindingKind.INVALID_INPUT,
                _relative(repo_root, package.root),
                _relative(repo_root, source),
                getattr(exc, "lineno", None) or 1,
                "<source>",
                f"source cannot be parsed: {exc}",
            )
        ]
    direct, nested = _module_imports(tree)
    findings: list[Finding] = []
    for line, import_root in direct:
        finding = _classify_direct_import(
            package=package,
            source=source,
            line=line,
            import_root=import_root,
            repo_root=repo_root,
            distributions=distributions,
        )
        if finding is not None:
            findings.append(finding)
    source_lines = source_text.splitlines()
    for line, import_root in nested:
        finding = _classify_nested_import(
            package=package,
            source=source,
            line=line,
            import_root=import_root,
            repo_root=repo_root,
            source_lines=source_lines,
        )
        if finding is not None:
            findings.append(finding)
    return findings


def _scan_package(
    *, pyproject: Path, repo_root: Path, distributions: dict[str, frozenset[str]]
) -> list[Finding]:
    """Scan one package's owned source given its ``pyproject.toml``."""

    try:
        package = _read_package(pyproject)
    except _NotPackageDeclarationError:
        return []
    except ValueError as exc:
        return [
            Finding(
                FindingKind.INVALID_INPUT,
                _relative(repo_root, pyproject.parent),
                _relative(repo_root, pyproject),
                1,
                "<metadata>",
                str(exc),
            )
        ]
    findings: list[Finding] = []
    for source in _source_files(package.source_root):
        findings.extend(
            _scan_source_file(
                source=source, package=package, repo_root=repo_root, distributions=distributions
            )
        )
    return findings


def scan_repository(repo_root: Path) -> ScanReport:
    """Scan the package layout rooted at ``repo_root`` without executing source."""

    root = repo_root.resolve()
    distributions = _distribution_map()
    findings: list[Finding] = []
    for pyproject in _candidate_pyprojects(root):
        if not (pyproject.parent / "src").is_dir():
            continue
        findings.extend(_scan_package(pyproject=pyproject, repo_root=root, distributions=distributions))
    return ScanReport(tuple(findings))


def _render(finding: Finding, *, allowlisted: bool = False) -> str:
    """Render one stable human-readable finding."""

    if finding.blocking:
        verdict = "BLOCKING"
    elif finding.kind is FindingKind.DECLARED_OPTIONAL_CAPABILITY:
        verdict = "CLASSIFIED"
    else:
        verdict = "UNKNOWN"
    marker = " [allowlisted]" if allowlisted else ""
    return (
        f"{verdict}{marker} {finding.kind.value} package={finding.package} "
        f"source={finding.source}:{finding.line} import={finding.import_root}: "
        f"{finding.message}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line gate."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        help=(
            "tracked-debt register of pre-existing missing_required_dependency "
            "findings, as <package>::<import_root> lines. See module docstring."
        ),
    )
    args = parser.parse_args(argv)
    report = scan_repository(args.repo_root)
    allowlist: frozenset[tuple[str, str]] = frozenset()
    if args.allowlist is not None:
        allowlist = _load_allowlist(args.allowlist)
    non_allowlisted_blocking = [
        finding
        for finding in report.blocking_findings
        if not _finding_is_allowlisted(finding, allowlist)
    ]
    allowlisted_count = len(report.blocking_findings) - len(non_allowlisted_blocking)
    for finding in report.findings:
        print(_render(finding, allowlisted=_finding_is_allowlisted(finding, allowlist)))
    print(
        "dependency_declaration_gate summary: "
        f"blocking={len(report.blocking_findings)} "
        f"({allowlisted_count} allowlisted; {len(non_allowlisted_blocking)} still failing) "
        f"unknown={len(report.unknown_findings)} findings={len(report.findings)}"
    )
    if non_allowlisted_blocking:
        return 2
    print("dependency_declaration_gate OK: no non-allowlisted missing required dependency")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
