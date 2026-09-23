"""ts_toolchain.py — R7-4: the TypeScript/JS target-toolchain scanners (tsc + eslint).

The **materialized-deps tier** of the R7 TS-native adapter (design §2/§5.1/§5.2). Both
scanners run the TARGET's OWN toolchain — ``tsc`` strict typecheck + ``eslint`` config
sweep — which needs the target's ``node_modules`` MATERIALIZED and, because that executes
TARGET-CONTROLLED CODE (the eslint config is executed JS, its parser/plugins load from the
target's ``node_modules``, and ``node_modules/.bin/*`` are target-supplied binaries) on the
scan host, the explicit per-run ``execute_target_toolchain`` opt-in (R1, default-off). Every
precondition that is not met is a DISTINCT coverage gap — never a crash, never a silent clean
run over nothing. On-host execution is the discouraged escape hatch; the Phase-2 sandbox is
the recommended path for foreign repos.

Read-only invariant (design §8): ``tsc --incremental false`` never writes ``.tsbuildinfo``;
``eslint`` runs without ``--fix``/``--cache`` so it never mutates files or writes
``.eslintcache``. A post-run tree-hash smoke pins the target tree byte-identical.

Honesty (design §5.1/§5.2/§6): findings anchored inside ``node_modules/`` are dropped at
parse time (a materialized dep shipping broken types is not the target's code) with a count
disclosed; the per-tool finding count is bounded by ``_MAX_TOOL_FINDINGS`` and any overflow
is disclosed on a ``ran=True`` CoverageRecord — ran-with-disclosure (R3), never a gap row.
"""

from __future__ import annotations

import json
import re
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
from ..toolrun import ToolOutcome, run, tool_available, tool_unavailable_reason

_NODE = "node"
_TSC = "tsc"
_ESLINT = "eslint"
_NODE_MODULES = "node_modules"

# Per-tool finding flood bound (design §5.1/§5.2). A structurally-broken foreign repo produces
# a bounded report, not a 40k-diagnostic blob; the overflow is disclosed on a ran=True record.
_MAX_TOOL_FINDINGS = 200
_TOOL_TIMEOUT_S = 300

# tsc `--pretty false` diagnostic line: ``path(line,col): error TSnnnn: message``. A config-level
# tsc error (``error TS5xxx``/``TS6xxx`` with no file anchor) does NOT match — it is caught via
# the nonzero exit + no-anchored-diagnostic check (`_config_failure`), never mis-parsed.
_TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),\d+\):\s+error\s+TS(?P<code>\d+):\s+(?P<msg>.*)$")

# eslint config surfaces (design §5.2): flat config at root, legacy `.eslintrc*` anywhere in the
# tracked tree, or a `package.json#eslintConfig` block. Module-level — no magic strings.
_ESLINT_FLAT_CONFIGS: tuple[str, ...] = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
)
_ESLINT_LEGACY_PREFIX = ".eslintrc"
_ESLINT_PKG_KEY = '"eslintConfig"'
_ESLINT_SEVERITY: dict[int, Severity] = {2: Severity.MEDIUM, 1: Severity.LOW}

# Materialized-deps-tier precondition gap strings (design §2/§5.1). The opt-in gap fires ONLY when
# node_modules is present but the flag is unset — disk-presence is an honesty tier, NOT consent (R1).
_OPTIN_GAP = (
    "node_modules present but target-toolchain execution not enabled (opt-in executes "
    "target-controlled code — tsc/eslint config + binaries — on the scan host; prefer the "
    "Phase-2 sandbox)"
)
_NODE_GAP = "node runtime unavailable on scan host"
_NODE_MODULES_GAP = (
    "node_modules not materialized — the target toolchain needs resolved dependencies; the "
    "read-only invariant forbids installing into the target (materialize a throwaway copy "
    "out-of-band, or use the Phase-2 sandboxed install)"
)


def _as_str(value: Any) -> str:  # noqa: ANN401 — narrows untyped tool JSON
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int | None:  # noqa: ANN401 — narrows untyped tool JSON
    return value if isinstance(value, int) else None


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


def _in_node_modules(rel: str) -> bool:
    """A diagnostic anchored inside a materialized dependency — dropped, not the target's code."""
    return _NODE_MODULES in Path(rel).parts


def _gap(scanner: str, reason: str) -> ScannerResult:
    """A ran=False coverage gap — the scanner could not honestly examine the surface."""
    return ScannerResult(
        findings=[],
        coverage=CoverageRecord(scanner=scanner, ran=False, files_examined=0, gap_reason=reason),
    )


def _excerpt(outcome: ToolOutcome) -> str:
    return (outcome.stderr.strip() or outcome.stdout.strip())[:200]


def _resolve_binary(tree: TargetTree, name: str) -> str | None:
    """The target's OWN binary (``node_modules/.bin/<name>`` — version fidelity) preferred; else
    ``<name>`` on PATH (drift visible in provenance, not hidden); else None (a loud gap)."""
    local = tree.root / _NODE_MODULES / ".bin" / name
    if local.is_file():
        return str(local)
    return name if tool_available(name) else None


def _binary_version(binary: str) -> str | None:
    """First line of ``<binary> --version`` — reads an ABSOLUTE target binary too (toolrun's
    ``tool_version`` only resolves PATH names, so this scanner reads the version directly)."""
    outcome = run([binary, "--version"], timeout_s=30, raise_on_timeout=False)
    text = outcome.stdout.strip() or outcome.stderr.strip()
    return text.splitlines()[0] if text else None


def _toolchain_precondition_gap(
    tree: TargetTree, scanner: str, *, execute_target_toolchain: bool
) -> ScannerResult | None:
    """The shared materialized-deps-tier preconditions, in design §5.1 order: (1) the R1 opt-in
    (node_modules present but flag unset → opt-in gap), (2) node on PATH, (3) node_modules
    materialized. Returns the FIRST gap, or None when all three pass."""
    node_modules = tree.root / _NODE_MODULES
    if node_modules.is_dir() and not execute_target_toolchain:
        return _gap(scanner, _OPTIN_GAP)
    if not tool_available(_NODE):
        return _gap(scanner, f"{_NODE_GAP}: {tool_unavailable_reason(_NODE)}")
    if not node_modules.is_dir():
        return _gap(scanner, _NODE_MODULES_GAP)
    return None


def _disclosure(total: int, dropped_node_modules: int) -> str | None:
    """The ran-with-disclosure note (R3) for a ran=True record: the flood-cap overflow and/or the
    node_modules-anchored drop count. None when neither applies (a clean, fully-attributed run)."""
    parts: list[str] = []
    if total > _MAX_TOOL_FINDINGS:
        parts.append(f"emitted {total} diagnostics; first {_MAX_TOOL_FINDINGS} converted to findings")
    if dropped_node_modules:
        parts.append(
            f"{dropped_node_modules} diagnostic(s) inside node_modules dropped (a materialized "
            "dependency's own issue — not attributed to the target's code)"
        )
    return "; ".join(parts) if parts else None


def _cap(findings: list[Finding]) -> list[Finding]:
    """Deterministic flood bound: sort by file/line, keep the first ``_MAX_TOOL_FINDINGS``."""
    ordered = sorted(findings, key=lambda finding: (finding.file, finding.line or 0))
    return ordered[:_MAX_TOOL_FINDINGS]


def _tsc_findings(stdout: str, root: Path, run_id: str, version: str | None) -> tuple[list[Finding], int]:
    """Parse ``tsc --pretty false`` diagnostics into TYPE_COVERAGE findings; drop node_modules-
    anchored ones (returning their count). Every diagnostic is a deterministic compiler fact."""
    findings: list[Finding] = []
    dropped = 0
    for raw in stdout.splitlines():
        match = _TSC_LINE.match(raw.strip())
        if match is None:
            continue
        rel = _relativize(root, match.group("file").strip())
        if _in_node_modules(rel):
            dropped += 1
            continue
        code = match.group("code")
        findings.append(
            Finding.build(
                run_id=run_id,
                layer=Layer.L1_DETERMINISTIC,
                dimension=Dimension.TYPE_COVERAGE,
                severity=Severity.MEDIUM,
                file=rel,
                line=int(match.group("line")),
                constraint_violated=f"tsc:TS{code}",
                evidence=match.group("msg").strip() or f"tsc reported TS{code}",
                fix_suggestion=None,
                provenance=Provenance(source=_TSC, tool_version=version, rule_id=f"TS{code}"),
                context_profile=ContextProfile.PRODUCTION,
            )
        )
    return findings, dropped


def _config_failure(outcome: ToolOutcome, findings: list[Finding], dropped: int) -> bool:
    """A nonzero exit that produced NO file-anchored diagnostics (and dropped none) is a config /
    invocation failure (a bad/composite tsconfig, an unresolved compiler) — a gap, not a clean run.
    A clean typecheck exits 0/empty; a typecheck with errors exits nonzero WITH anchored findings."""
    return outcome.returncode != 0 and not findings and dropped == 0


def scan_tsc(tree: TargetTree, run_id: str, *, execute_target_toolchain: bool = False) -> ScannerResult:
    """``tsc --noEmit`` strict typecheck at the target's OWN config (design §5.1).

    STACK/{TYPESCRIPT}, materialized-deps tier: gated on the R1 opt-in + node + a materialized
    ``node_modules`` + a tsc binary + a root ``tsconfig.json`` (each absence a distinct gap; no
    synthesized config — no half-faithful emulation). ``--incremental false`` guarantees no
    ``.tsbuildinfo`` dropping (read-only invariant).
    """
    gap = _toolchain_precondition_gap(tree, _TSC, execute_target_toolchain=execute_target_toolchain)
    if gap is not None:
        return gap
    binary = _resolve_binary(tree, _TSC)
    if binary is None:
        return _gap(
            _TSC,
            f"typescript compiler not available (target node_modules/.bin + PATH): {tool_unavailable_reason(_TSC)}",
        )
    if not (tree.root / "tsconfig.json").is_file():
        return _gap(_TSC, "no tsconfig.json at target root — tsc honors the target's own config only (no synthesized config)")
    outcome = run(
        [binary, "--noEmit", "--pretty", "false", "--incremental", "false", "-p", "tsconfig.json"],
        cwd=str(tree.root),
        timeout_s=_TOOL_TIMEOUT_S,
        raise_on_timeout=False,
    )
    if outcome.timed_out:
        return _gap(_TSC, "tsc timed out")
    findings, dropped = _tsc_findings(outcome.stdout, tree.root, run_id, _binary_version(binary))
    if _config_failure(outcome, findings, dropped):
        return _gap(_TSC, f"tsc config/invocation failure (no file-anchored diagnostics): {_excerpt(outcome)}")
    return ScannerResult(
        findings=_cap(findings),
        coverage=CoverageRecord(
            scanner=_TSC,
            ran=True,
            files_examined=len(tree.typescript_files()),
            gap_reason=_disclosure(len(findings), dropped),
        ),
    )


def _has_eslint_config(tree: TargetTree) -> bool:
    """The target declares an eslint config: a flat config at root, a legacy ``.eslintrc*`` in the
    tracked tree, or a ``package.json#eslintConfig`` block (substring probe — no fragile parse)."""
    root = tree.root
    if any((root / name).is_file() for name in _ESLINT_FLAT_CONFIGS):
        return True
    if any(Path(rel).name.startswith(_ESLINT_LEGACY_PREFIX) for rel in tree.tracked):
        return True
    package_json = root / "package.json"
    return package_json.is_file() and _ESLINT_PKG_KEY in package_json.read_text(encoding="utf-8")


def _eslint_findings(
    parsed: list[Any], root: Path, run_id: str, version: str | None
) -> tuple[list[Finding], int, int]:
    """Map the eslint JSON report into CODE_QUALITY findings reflecting the target's OWN declared
    standards; drop node_modules-anchored messages (count returned). Also returns files-linted."""
    findings: list[Finding] = []
    dropped = 0
    files_linted = 0
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        files_linted += 1
        rel = _relativize(root, _as_str(entry.get("filePath")))
        in_node_modules = _in_node_modules(rel)
        messages = entry.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            if in_node_modules:
                dropped += 1
                continue
            rule = _as_str(message.get("ruleId")) or "fatal"
            findings.append(
                Finding.build(
                    run_id=run_id,
                    layer=Layer.L1_DETERMINISTIC,
                    dimension=Dimension.CODE_QUALITY,
                    severity=_ESLINT_SEVERITY.get(_as_int(message.get("severity")) or 0, Severity.LOW),
                    file=rel,
                    line=_as_int(message.get("line")),
                    constraint_violated=f"eslint:{rule}",
                    evidence=_as_str(message.get("message")) or f"eslint {rule}",
                    fix_suggestion=None,
                    provenance=Provenance(source=_ESLINT, tool_version=version, rule_id=rule),
                    context_profile=ContextProfile.PRODUCTION,
                )
            )
    return findings, dropped, files_linted


def scan_eslint(tree: TargetTree, run_id: str, *, execute_target_toolchain: bool = False) -> ScannerResult:
    """``eslint --format json .`` at the target's OWN config (design §5.2).

    STACK/{TYPESCRIPT, JAVASCRIPT} (eslint is a JavaScript linter first — R2 intersection),
    materialized-deps tier: the R1 opt-in is doubly non-negotiable here (the config is executed
    JS whose plugins are target-controlled). Never ``--fix``/``--cache`` (read-only invariant);
    exit 1 (lint findings) is normal, a config-load failure is a gap.
    """
    gap = _toolchain_precondition_gap(tree, _ESLINT, execute_target_toolchain=execute_target_toolchain)
    if gap is not None:
        return gap
    binary = _resolve_binary(tree, _ESLINT)
    if binary is None:
        return _gap(
            _ESLINT,
            f"eslint not available (target node_modules/.bin + PATH): {tool_unavailable_reason(_ESLINT)}",
        )
    if not _has_eslint_config(tree):
        return _gap(_ESLINT, "no eslint config in target — eslint honors the target's own config only")
    outcome = run([binary, "--format", "json", "."], cwd=str(tree.root), timeout_s=_TOOL_TIMEOUT_S, raise_on_timeout=False)
    if outcome.timed_out:
        return _gap(_ESLINT, "eslint timed out")
    try:
        parsed: Any = json.loads(outcome.stdout or "[]")
    except json.JSONDecodeError:
        return _gap(_ESLINT, f"eslint produced no JSON (config-load failure): {_excerpt(outcome)}")
    if not isinstance(parsed, list):
        raise RuntimeError("eslint did not emit a JSON array")
    findings, dropped, files_linted = _eslint_findings(parsed, tree.root, run_id, _binary_version(binary))
    return ScannerResult(
        findings=_cap(findings),
        coverage=CoverageRecord(
            scanner=_ESLINT,
            ran=True,
            files_examined=files_linted,
            gap_reason=_disclosure(len(findings), dropped),
        ),
    )
