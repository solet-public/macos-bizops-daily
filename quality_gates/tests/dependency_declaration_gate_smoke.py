#!/usr/bin/env python3
"""Offline smoke for GTE-15's sound dependency-declaration rule."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from quality_gates.dependency_declaration_gate import (  # noqa: E402
    FindingKind,
    scan_repository,
)
from quality_gates.dependency_declaration_gate import main as run_gate  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    """Count and assert one smoke condition."""

    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _write_package(
    root: Path,
    *,
    dependencies: str = "[]",
    optional_dependencies: str = "",
    source: str,
) -> None:
    """Create one isolated src-layout fixture package."""

    package = root / "fixture_package"
    module = package / "src" / "fixture_package"
    module.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'fixture-package'\n"
        f"dependencies = {dependencies}\n"
        f"{optional_dependencies}",
        encoding="utf-8",
    )
    (module / "__init__.py").write_text(source, encoding="utf-8")


def _check_failing_mutation() -> None:
    """Name the defect mutation: direct packaging import with no declaration reds."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(root, source="import packaging\n")
        report = scan_repository(root)
    _check(len(report.blocking_findings) == 1, "undeclared direct packaging import blocks")
    finding = report.blocking_findings[0]
    _check(
        finding.kind is FindingKind.MISSING_REQUIRED_DEPENDENCY
        and finding.import_root == "packaging",
        "failing mutation identifies the missing packaging declaration",
    )


def _check_guarded_import_is_not_naively_flagged() -> None:
    """A naive AST walk would red this fixture; the sound rule leaves it unknown."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(
            root,
            source=(
                "try:\n"
                "    import packaging\n"
                "except ModuleNotFoundError:\n"
                "    packaging = None\n"
            ),
        )
        report = scan_repository(root)
    _check(not report.blocking_findings, "guarded import is not a missing dependency")
    _check(
        [finding.kind for finding in report.unknown_findings]
        == [FindingKind.UNKNOWN_GUARDED_OR_NESTED],
        "guarded import is explicitly reported as unknown",
    )


def _check_optional_only_declaration_is_unknown() -> None:
    """An optional-only declaration cannot prove whether the module is core."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(
            root,
            optional_dependencies="[project.optional-dependencies]\nfeature = ['packaging']\n",
            source="import packaging\n",
        )
        report = scan_repository(root)
    _check(not report.blocking_findings, "optional-only dependency does not false-red")
    _check(
        [finding.kind for finding in report.unknown_findings]
        == [FindingKind.UNKNOWN_OPTIONAL_ONLY],
        "optional-only declaration is reported as unknown",
    )


def _check_local_relative_and_stdlib_imports_are_excluded() -> None:
    """Local, relative, and standard-library imports require no declaration."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(
            root,
            source="import sys\nfrom . import child\nimport fixture_package\n",
        )
        report = scan_repository(root)
    _check(not report.findings, "stdlib, relative, and local imports are excluded")


def _check_marked_optional_capability_is_classified_not_unknown() -> None:
    """The explicit opt-out marker reclassifies a guarded import as CLASSIFIED."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(
            root,
            source=(
                "try:\n"
                "    import packaging  # dependency-declaration: optional-capability"
                ' reason="test fixture"\n'
                "except ModuleNotFoundError:\n"
                "    packaging = None\n"
            ),
        )
        report = scan_repository(root)
    _check(not report.blocking_findings, "marked optional capability does not block")
    _check(
        [finding.kind for finding in report.findings]
        == [FindingKind.DECLARED_OPTIONAL_CAPABILITY],
        "marked guarded import is classified, not left as unknown_guarded_or_nested",
    )


def _check_unmarked_sibling_on_same_import_root_stays_unknown() -> None:
    """The marker is per-line: an unmarked guarded import of the same root is untouched."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(
            root,
            source=(
                "try:\n"
                "    import packaging\n"
                "except ModuleNotFoundError:\n"
                "    packaging = None\n"
            ),
        )
        report = scan_repository(root)
    _check(
        [finding.kind for finding in report.findings] == [FindingKind.UNKNOWN_GUARDED_OR_NESTED],
        "absent the marker, a guarded import is unclassified backlog, not silently accepted",
    )


def _check_allowlist_suppresses_tracked_debt_but_not_new_violations() -> None:
    """--allowlist exempts a known (package, import_root) pair; a different one still blocks."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(root, source="import packaging\n")
        allowlist_path = root / "allowlist.txt"
        allowlist_path.write_text(
            "fixture_package::packaging"
            "  # owner: smoke-fixture reason: tracked test debt expires: 2099-01-01\n",
            encoding="utf-8",
        )
        exit_code = run_gate(["--repo-root", str(root), "--allowlist", str(allowlist_path)])
    _check(exit_code == 0, "an allowlisted missing dependency does not fail the gate")

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _write_package(root, source="import packaging\nimport requests\n")
        allowlist_path = root / "allowlist.txt"
        allowlist_path.write_text(
            "fixture_package::packaging"
            "  # owner: smoke-fixture reason: tracked test debt expires: 2099-01-01\n",
            encoding="utf-8",
        )
        exit_code = run_gate(["--repo-root", str(root), "--allowlist", str(allowlist_path)])
    _check(
        exit_code == 2,
        "a NEW missing dependency not on the allowlist still blocks even with one allowlisted",
    )


def main() -> int:
    """Run every independent smoke control."""

    _check_failing_mutation()
    _check_guarded_import_is_not_naively_flagged()
    _check_optional_only_declaration_is_unknown()
    _check_local_relative_and_stdlib_imports_are_excluded()
    _check_marked_optional_capability_is_classified_not_unknown()
    _check_unmarked_sibling_on_same_import_root_stays_unknown()
    _check_allowlist_suppresses_tracked_debt_but_not_new_violations()
    print(f"dependency_declaration_gate_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
