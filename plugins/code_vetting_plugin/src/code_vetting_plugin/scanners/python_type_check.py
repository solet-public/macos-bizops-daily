"""python_type_check.py — R9-E: foreign-Python type checking (pyright + mypy), opt-in tier.

The Python mirror of R7-4's ts_toolchain (§R9-E): ONE `STACK={PYTHON}`, `executes_target_code=True`
scanner behind the existing ``execute_target_toolchain`` flag. A configured pyright/mypy run delivers
the TARGET'S OWN declared type contract (the class that justified tsc/eslint's opt-in admission) — and
the tier is factually forced: mypy plugins are target-controlled Python loaded at check time and both
checkers resolve imports into the target's environment, so ``executes_target_code=True`` is a
description, not a posture.

Config-driven, run EVERY configured checker (§E.1): pyright = ``pyrightconfig.json`` / ``[tool.pyright]``;
mypy = ``mypy.ini`` / ``.mypy.ini`` / ``[tool.mypy]`` / ``[mypy]`` in ``setup.cfg``. Target-config ONLY —
never synthesized (a "basic/no-deps mode" is the half-faithful-emulation family R7-4 rejected). Binary
resolution mirrors ``node_modules/.bin`` version-fidelity: prefer the target env's own ``<venv>/bin/<checker>``,
fall back to the scan-host binary with the version recorded (skew visible), absent → tool-absent gap.
NOTHING is installed at scan time.

Three DISTINCT gap classes (§E.3): no config → "no static-typing contract" (not_applicable); config +
flag unset → opt-in gap (disk-presence is not consent, R7-4); config + no materialized env → materialization
gap (the engine never creates a venv — build-backend execution is the R1 class). Env discovery is a curated
``{.venv/, venv/}`` at the tree root only, used path disclosed; diagnostics anchored inside the env /
site-packages are dropped with a disclosed count (the node_modules-drop mirror). Errors → MEDIUM, warnings /
notes → LOW; dimension TYPE_COVERAGE; MEDIUM ceiling; per-tool flood cap. SELF: the pyright ``--strict`` gate
stays sole self authority, so this scanner's Finding rows are suppressed on a self-vet (R8 §C).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..coverage import CoverageRecord, ScannerResult
from ..models import ContextProfile, Dimension, Finding, Layer, Provenance, Severity
from ..targets import TargetTree
from ..toolrun import ToolOutcome, run, tool_available, tool_unavailable_reason

_SCANNER = "python_type_check"
_PYRIGHT = "pyright"
_MYPY = "mypy"

_MAX_TOOL_FINDINGS = 200
_TOOL_TIMEOUT_S = 300

# Env discovery — a curated STRUCTURAL set at the tree root only (Python envs are heterogeneous; a
# curated set extended by visible review beats half-faithful env discovery — the FT walk-exclude principle).
_VENV_DIRS: tuple[str, ...] = (".venv", "venv")

# Checker severity → finding severity (errors MEDIUM, warnings/notes/information LOW — the _ESLINT_SEVERITY shape).
_PYRIGHT_SEVERITY: dict[str, Severity] = {"error": Severity.MEDIUM, "warning": Severity.LOW, "information": Severity.LOW}
_MYPY_SEVERITY: dict[str, Severity] = {"error": Severity.MEDIUM, "warning": Severity.LOW, "note": Severity.LOW}

# mypy text line: ``path:line[:col]: severity: message`` with a trailing ``[error-code]`` (--show-error-codes).
_MYPY_LINE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):(?:\d+:)?\s+(?P<sev>error|warning|note):\s+(?P<msg>.*)$")
_MYPY_CODE = re.compile(r"\[(?P<code>[\w-]+)\]\s*$")

_NO_CONTRACT_GAP = "not_applicable: no type-check configuration declared — target has no static-typing contract"
_OPTIN_GAP = (
    "type-check configuration present but target-toolchain execution not enabled (opt-in runs the target's "
    "type-checker — mypy plugins + import resolution into the target env — on the scan host; prefer the Phase-2 sandbox)"
)
_MATERIALIZATION_GAP = (
    "type-check configuration present but no environment ({.venv,venv}) materialized — the checker needs resolved "
    "imports/stubs; the engine never creates a venv (build-backend execution → Phase-2 sandbox)"
)


@dataclass(frozen=True, slots=True)
class _CheckerRun:
    """One checker's contribution: its findings + a coverage-disclosure fragment."""

    findings: list[Finding]
    disclosure: str
    completed: bool


def _relativize(root: Path, path_field: str) -> str:
    if not path_field:
        return path_field
    candidate = Path(path_field)
    if not candidate.is_absolute():
        return path_field
    try:
        return str(candidate.relative_to(root))
    except ValueError:
        return path_field


def _in_env(rel: str, venv_name: str) -> bool:
    """A diagnostic anchored inside the target's env / site-packages — not the target's own code."""
    parts = Path(rel).parts
    return venv_name in parts or "site-packages" in parts


def _has_section(path: Path, marker: str) -> bool:
    """Whether a config file declares a section (substring probe — no fragile TOML/INI parse)."""
    return path.is_file() and marker in path.read_text(encoding="utf-8")


def _configured_checkers(tree: TargetTree) -> list[str]:
    """Every type-checker the target has CONFIGURED (enumeration-driven; target-config only)."""
    root = tree.root
    pyproject = root / "pyproject.toml"
    checkers: list[str] = []
    if (root / "pyrightconfig.json").is_file() or _has_section(pyproject, "[tool.pyright]"):
        checkers.append(_PYRIGHT)
    mypy_configured = (
        (root / "mypy.ini").is_file()
        or (root / ".mypy.ini").is_file()
        or _has_section(pyproject, "[tool.mypy]")
        or _has_section(root / "setup.cfg", "[mypy]")
    )
    if mypy_configured:
        checkers.append(_MYPY)
    return checkers


def _venv(tree: TargetTree) -> str | None:
    """The first curated env dir present at the tree root, or None (materialization gap)."""
    for name in _VENV_DIRS:
        if (tree.root / name).is_dir():
            return name
    return None


def _resolve_binary(tree: TargetTree, venv_name: str, checker: str) -> str | None:
    """The target env's own binary (``<venv>/bin/<checker>`` — version fidelity) preferred; else the
    scan-host binary (version recorded so skew is visible); else None (a tool-absent gap)."""
    local = tree.root / venv_name / "bin" / checker
    if local.is_file():
        return str(local)
    return checker if tool_available(checker) else None


def _outcome_detail(outcome: ToolOutcome) -> str:
    """One bounded line suitable for a coverage disclosure, never a fake version."""
    text = outcome.stderr.strip() or outcome.stdout.strip()
    if not text:
        return "no output"
    return " ".join(text.splitlines()[0].split())[:240]


def _binary_version(binary: str, root: Path) -> tuple[str | None, str | None]:
    """Return ``(version, failure)`` from the SAME cwd the checker will use.

    Pyenv shims resolve by directory.  A PATH hit is therefore only a candidate;
    a successful version probe in the target cwd is the executable proof.
    """
    try:
        outcome = run(
            [binary, "--version"],
            cwd=str(root),
            timeout_s=30,
            raise_on_timeout=False,
        )
    except RuntimeError as exc:
        return None, str(exc)
    if outcome.timed_out:
        return None, "version probe timed out"
    if outcome.returncode != 0:
        return None, f"version probe exited {outcome.returncode}: {_outcome_detail(outcome)}"
    text = outcome.stdout.strip() or outcome.stderr.strip()
    if not text:
        return None, "version probe returned no version text"
    return text.splitlines()[0], None


# RIDER-2 (ruled): mypy resolves its check SCOPE from a files/packages/modules config key; a config
# with none (settings-only) leaves mypy nothing to check → 0 diagnostics (pyright defaults to the tree
# and catches it). The config stays 100% the RULES source, but when it declares NO scope we provide the
# enumerated target *.py (minus the env dirs) as the check TARGET. A DECLARED scope WINS (their contract).
_MYPY_SCOPE_KEY = re.compile(r"^\s*(?:files|packages|modules)\s*=", re.MULTILINE)
_MYPY_CONFIG_FILES: tuple[str, ...] = ("mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg")


def _in_venv_dir(rel: str) -> bool:
    parts = Path(rel).parts
    return any(venv in parts for venv in _VENV_DIRS)


def _mypy_declares_scope(tree: TargetTree) -> bool:
    for name in _MYPY_CONFIG_FILES:
        path = tree.root / name
        if path.is_file() and _MYPY_SCOPE_KEY.search(path.read_text(encoding="utf-8")):
            return True
    return False


def _mypy_scope_args(tree: TargetTree) -> tuple[list[str], str]:
    """(extra argv, scope-provenance) for mypy: a declared scope WINS (no args, config-driven); else
    the enumerated *.py (minus {.venv,venv}) as the check target, with a distinct provenance string."""
    if _mypy_declares_scope(tree):
        return [], "scope: config-declared"
    files = [str(tree.abspath(rel)) for rel in tree.python_files() if not _in_venv_dir(rel)]
    return files, "scope: enumerated — config declares no files/packages"


def _as_str(value: Any) -> str:  # noqa: ANN401 — narrows untyped tool JSON
    return value if isinstance(value, str) else ""


def _pyright_findings(stdout: str, root: Path, venv_name: str, run_id: str, version: str | None) -> tuple[list[Finding], int]:
    """Parse ``pyright --outputjson`` diagnostics (0-indexed lines) into TYPE_COVERAGE findings."""
    parsed: Any = json.loads(stdout or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("pyright output was not a JSON object")
    diagnostics = parsed.get("generalDiagnostics")
    if not isinstance(diagnostics, list):
        raise ValueError("pyright output omitted generalDiagnostics")
    findings: list[Finding] = []
    dropped = 0
    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        rel = _relativize(root, _as_str(diag.get("file")))
        if _in_env(rel, venv_name):
            dropped += 1
            continue
        rule = _as_str(diag.get("rule")) or "type"
        start = diag.get("range", {}).get("start", {}) if isinstance(diag.get("range"), dict) else {}
        line = int(start.get("line", 0)) + 1 if isinstance(start, dict) else None
        findings.append(_finding(run_id, _PYRIGHT, _PYRIGHT_SEVERITY.get(_as_str(diag.get("severity")), Severity.LOW), rel, line, rule, _as_str(diag.get("message")).replace("\n", " ").strip(), version))
    return findings, dropped


def _mypy_findings(stdout: str, root: Path, venv_name: str, run_id: str, version: str | None) -> tuple[list[Finding], int]:
    """Parse mypy text diagnostics (``path:line: severity: msg [code]``) into TYPE_COVERAGE findings."""
    findings: list[Finding] = []
    dropped = 0
    for raw in stdout.splitlines():
        match = _MYPY_LINE.match(raw.strip())
        if match is None:
            continue
        rel = _relativize(root, match.group("file").strip())
        if _in_env(rel, venv_name):
            dropped += 1
            continue
        message = match.group("msg").strip()
        code_match = _MYPY_CODE.search(message)
        rule = code_match.group("code") if code_match else match.group("sev")
        findings.append(_finding(run_id, _MYPY, _MYPY_SEVERITY.get(match.group("sev"), Severity.LOW), rel, int(match.group("line")), rule, message, version))
    return findings, dropped


def _finding(run_id: str, checker: str, severity: Severity, rel: str, line: int | None, rule: str, message: str, version: str | None) -> Finding:
    return Finding.build(
        run_id=run_id,
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.TYPE_COVERAGE,
        severity=severity,
        file=rel,
        line=line,
        constraint_violated=f"{checker}:{rule}",
        evidence=message or f"{checker} reported {rule}",
        fix_suggestion=None,
        provenance=Provenance(source=checker, tool_version=version, rule_id=rule),
        context_profile=ContextProfile.PRODUCTION,
    )


def _gap(reason: str) -> ScannerResult:
    return ScannerResult(findings=[], coverage=CoverageRecord(scanner=_SCANNER, ran=False, files_examined=0, gap_reason=reason))


_CHECKER_ARGV: dict[str, list[str]] = {
    _PYRIGHT: ["--outputjson"],
    _MYPY: ["--no-error-summary", "--show-error-codes", "--no-color-output"],
}
_CHECKER_PARSE = {_PYRIGHT: _pyright_findings, _MYPY: _mypy_findings}


def _execute_checker(
    argv: list[str],
    root: Path,
) -> tuple[ToolOutcome | None, str | None]:
    """Run a checker and turn every non-diagnostic execution state into a gap."""
    try:
        outcome = run(argv, cwd=str(root), timeout_s=_TOOL_TIMEOUT_S, raise_on_timeout=False)
    except RuntimeError as exc:
        return None, f"checker could not start ({exc})"
    if outcome.timed_out:
        return None, "timed out"
    if outcome.returncode not in {0, 1}:
        return None, f"checker exited {outcome.returncode}: {_outcome_detail(outcome)}"
    return outcome, None


def _parse_checker_outcome(
    checker: str,
    outcome: ToolOutcome,
    tree: TargetTree,
    venv_name: str,
    run_id: str,
    version: str | None,
) -> tuple[list[Finding] | None, int, str | None]:
    """Parse a completed checker run, distinguishing diagnostics from malformed output."""
    try:
        findings, dropped = _CHECKER_PARSE[checker](
            outcome.stdout,
            tree.root,
            venv_name,
            run_id,
            version,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return None, 0, f"checker output could not be parsed ({exc})"
    if outcome.returncode == 1 and not findings and dropped == 0:
        return (
            None,
            0,
            f"checker exited 1 without parseable diagnostics: {_outcome_detail(outcome)}",
        )
    return findings, dropped, None


def _run_checker(tree: TargetTree, venv_name: str, checker: str, run_id: str) -> _CheckerRun:
    """Resolve + run one configured checker over the target's own config; parse → findings."""
    binary = _resolve_binary(tree, venv_name, checker)
    if binary is None:
        return _CheckerRun(
            findings=[],
            disclosure=(
                f"{checker}: not available (target env + scan host): "
                f"{tool_unavailable_reason(checker)}"
            ),
            completed=False,
        )
    version, version_failure = _binary_version(binary, tree.root)
    if version_failure is not None:
        return _CheckerRun(
            findings=[],
            disclosure=f"{checker}: not runnable ({version_failure})",
            completed=False,
        )
    argv = [binary, *_CHECKER_ARGV[checker]]
    scope_parts: list[str] = []
    if checker == _MYPY:  # RIDER-2: provide the enumerated scope only when the config declares none
        scope_args, scope_note = _mypy_scope_args(tree)
        argv.extend(scope_args)
        scope_parts.append(scope_note)
    outcome, execution_failure = _execute_checker(argv, tree.root)
    if outcome is None:
        return _CheckerRun(
            findings=[],
            disclosure=f"{checker}: {execution_failure}",
            completed=False,
        )
    findings, dropped, parse_failure = _parse_checker_outcome(
        checker,
        outcome,
        tree,
        venv_name,
        run_id,
        version,
    )
    if findings is None:
        return _CheckerRun(
            findings=[],
            disclosure=f"{checker}: {parse_failure}",
            completed=False,
        )
    total = len(findings)
    parts = [f"{checker} v{version or '?'}: {total} diagnostic(s)", *scope_parts]
    if total > _MAX_TOOL_FINDINGS:
        parts.append(f"first {_MAX_TOOL_FINDINGS} kept")
    if dropped:
        parts.append(f"{dropped} env-anchored dropped")
    return _CheckerRun(
        findings=findings[:_MAX_TOOL_FINDINGS],
        disclosure="; ".join(parts),
        completed=True,
    )


def scan(tree: TargetTree, run_id: str, *, execute_target_toolchain: bool = False) -> ScannerResult:
    """Run every configured type-checker over the target's own config (opt-in, parse-heavy, R7-4 mirror).

    Preconditions in §E.3 order (each absence a distinct gap): a type-check config must exist (else the
    target has no static-typing contract); the ``execute_target_toolchain`` opt-in must be set (disk-presence
    is not consent); a ``{.venv,venv}`` env must already exist (the engine never materializes one). Findings
    render on a FOREIGN target only — a self-vet's pyright ``--strict`` gate is the sole self authority (R8 §C).
    """
    checkers = _configured_checkers(tree)
    if not checkers:
        return _gap(_NO_CONTRACT_GAP)
    if not execute_target_toolchain:
        return _gap(_OPTIN_GAP)
    venv_name = _venv(tree)
    if venv_name is None:
        return _gap(_MATERIALIZATION_GAP)
    findings: list[Finding] = []
    disclosures = [f"env={venv_name}"]
    checker_runs: list[_CheckerRun] = []
    for checker in checkers:
        checker_run = _run_checker(tree, venv_name, checker, run_id)
        checker_runs.append(checker_run)
        findings.extend(checker_run.findings)
        disclosures.append(checker_run.disclosure)
    emitted = findings if tree.foreign else []  # self-suppress: the --strict gate owns the self surface (R8 §C)
    completed = all(checker_run.completed for checker_run in checker_runs)
    examined = len(tree.python_files()) if any(checker_run.completed for checker_run in checker_runs) else 0
    return ScannerResult(
        findings=emitted,
        coverage=CoverageRecord(
            scanner=_SCANNER,
            ran=completed,
            files_examined=examined,
            gap_reason="; ".join(disclosures),
        ),
    )
