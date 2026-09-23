#!/usr/bin/env python3
"""
Code Quality Pre-Commit Check for the solet platform
Runs pyright (strict), ruff, and the three coherence-aware gates
(god-class, radon cyclomatic complexity, radon maintainability index)
with their tracked-debt allowlists.

Exit codes:
  0 - All checks passed
  1 - Type errors found (pyright)
  2 - Lint errors found (ruff)
  3 - Type + lint errors
  4 - Syntax errors (critical, supersedes all); also covers a missing venv
      and a required gate tool that never ran at all (pyright/ruff module
      unresolved -- see ToolUnavailableError) -- all "cannot proceed as
      configured," not a findings result
  6 - GATE CRASH: one or more gates raised instead of measuring, so no
      verdict exists for the code they cover. Deliberately distinct from 5:
      a crash is not a finding, and folding it into a violation count tells
      the reader they have debt to fix when nothing was measured at all
      (2026-08-16). Supersedes 5 when both occur — an unmeasured gate makes
      the run's other verdicts incomplete, not merely bad.
  5 - Blocking-gate violations (non-allowlisted findings from any
      blocking structural gate: god_class_check / radon_cc_check /
      radon_mi_check / whole_tree_integration_gate /
      service_interface_ast_check / return_shape_gate / bundle_license_gate;
      flow_probe_registry_gate;
      the summary
      line names the specific failing gate(s))

Note: codes 2 and 3 have historical drift from the original docstring
(which used 2 for "complexity issues"). The blocking-gate code 5 is
the new authoritative non-allowlisted-findings signal; the legacy 2
remains the ruff signal for backward compatibility. CI consumers and
the git-controller-commit SKILL should treat exit 5 as a non-allowlisted
blocking-gate block — the gap that let PluginBase 16-public-method
regression land in master without Step 7 catching it (2026-06-03).

Per the KB "Peer Pre-Completion Gate Procedure", gates run against the
platform's quality surface only — `ananta/src`, `ananta/tests` (operator-ruled
2026-07-07, GTE-05), `plugins/*/src`, `plugins/*/tests`, and
`quality_gates`. Operator-tooling (research/tools/migrations/
parity_tests/, workbench, deployment, top-level utilities) is excluded.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath

from gate_scope import (
    BUNDLED_VENV_PREFIX,
    GateCrashError,
    repo_files,
)

_QUALITY_GATES_DIR = Path("quality_gates")
_GOD_CLASS_CHECK = _QUALITY_GATES_DIR / "god_class_check.py"
_GOD_CLASS_ALLOWLIST = _QUALITY_GATES_DIR / "god_class_allowlist.txt"
_RADON_CC_CHECK = _QUALITY_GATES_DIR / "radon_cc_check.py"
_RADON_CC_ALLOWLIST = _QUALITY_GATES_DIR / "radon_cc_allowlist.txt"
_RADON_MI_CHECK = _QUALITY_GATES_DIR / "radon_mi_check.py"
_RADON_MI_ALLOWLIST = _QUALITY_GATES_DIR / "radon_mi_allowlist.txt"
_W_INT_CHECK = _QUALITY_GATES_DIR / "whole_tree_integration_gate.py"
_W_INT_ALLOWLIST = _QUALITY_GATES_DIR / "whole_tree_integration_gate_allowlist.txt"
_FLOW_PROBE_REGISTRY_CHECK = _QUALITY_GATES_DIR / "flow_probe_registry_gate.py"
_FLOW_PROBE_REGISTRY_ALLOWLIST = _QUALITY_GATES_DIR / "flow_probe_registry_gate_allowlist.txt"
_W_WINT2_CHECK = _QUALITY_GATES_DIR / "wint2_driver_import_check.py"
_W_WINT2_ALLOWLIST = _QUALITY_GATES_DIR / "wint2_driver_import_allowlist.txt"
_W_WINT2_VAULT_KEY_CHECK = _QUALITY_GATES_DIR / "wint2_vault_key_declaration_check.py"
_W_WINT2_VAULT_KEY_ALLOWLIST = _QUALITY_GATES_DIR / "wint2_vault_key_declaration_allowlist.txt"
_SI_AST_CHECK = _QUALITY_GATES_DIR / "service_interface_ast_check.py"
_SI_AST_ALLOWLIST = _QUALITY_GATES_DIR / "service_interface_ast_allowlist.txt"
_RETURN_SHAPE_CHECK = _QUALITY_GATES_DIR / "return_shape_gate.py"
_RETURN_SHAPE_ALLOWLIST = _QUALITY_GATES_DIR / "return_shape_allowlist.txt"
_SHIPPED_DOC_CHECK = _QUALITY_GATES_DIR / "shipped_doc_gate.py"
_SHIPPED_DOC_ALLOWLIST = _QUALITY_GATES_DIR / "cited_path_gate_allowlist.txt"
_EMBEDDING_BOUND_CHECK = _QUALITY_GATES_DIR / "embedding_description_bound_gate.py"
_EMBEDDING_BOUND_ALLOWLIST = (
    _QUALITY_GATES_DIR / "embedding_description_bound_allowlist.txt"
)
_BUNDLE_LICENSE_CHECK = _QUALITY_GATES_DIR / "bundle_license_gate.py"
_BUNDLE_LICENSE_ALLOWLIST = _QUALITY_GATES_DIR / "bundle_license_allowlist.txt"
_SCHEMA_INIT_CHECK = _QUALITY_GATES_DIR / "schema_init_gate.py"
_SCHEMA_INIT_ALLOWLIST = _QUALITY_GATES_DIR / "schema_init_allowlist.txt"

# Exit codes returned by the three wrapper scripts (see each script's
# docstring): 0 = clean (or every finding allowlisted), 2 = one or more
# non-allowlisted findings, 64 = usage error (bad arguments / missing
# allowlist / non-existent paths).
_WRAPPER_OK = 0
_WRAPPER_BLOCKING = 2
_WRAPPER_USAGE_ERROR = 64
# A wrapper that raised instead of measuring. Distinct from _WRAPPER_BLOCKING
# on purpose: a crash is not a verdict, and the summary must never fold it
# into a violation count (2026-08-16 — radon_cc's RecursionError surfaced as
# "❌ FAILED: Blocking gate violations (radon_cc)", which reads as complexity
# debt in the reader's own code when nothing had been measured at all).
_WRAPPER_GATE_CRASH = 70
_TRACEBACK_MARKER = "Traceback (most recent call last):"


class _GateOutcome(Enum):
    """What a gate run actually established — not merely its exit code."""

    OK = "ok"
    BLOCKING = "blocking"
    CRASH = "crash"

# Per-file gate scope: the platform's quality surface, mirroring the
# SKILL Step 6 SCOPE regex and the radon_cc/mi allowlists' coverage.
# Narrowing operator ruling, 2026-08-08 ("quality gates apply to
# everything, boy scout rules"): `research/` and `disabled_plugins/` stay
# out (see pyproject.toml's ruff/pyright exclude comments for the
# conditions that let something back in); `tools/`, `parity_tests/`,
# `.claude/hooks`, and plugin `policies/` are being brought in-scope,
# fix-first, one class per landing — see
# workbench/2026-08-08_gate_scope_widening_measurement_d3-impl.md.
# `.claude/hooks` is deferred: rotation-impl has live untracked/dirty
# work there as of 2026-08-08, and this scope walk is filesystem-based
# (rglob), not git-tracked-only, so widening now would sweep its WIP into
# this gate. workbench/ remains out per the (assumption, not
# operator-ruled) ephemeral-scratch carve-out in that same findings
# file. `deployment/` is only partly out: `deployment/scripts` (one
# file) is in-scope, fix-first, as part of ruling 1's "tail" class —
# the rest of `deployment/` was never part of that class and stays
# unaddressed pending its own measurement.
#
# Entries below that are individual FILES, not directories, rely on
# `_per_file_gate_paths()`'s `root.is_file()` branch — a bare-file
# `_PER_FILE_GATE_TOP_LEVEL` or `_PER_FILE_GATE_PLUGIN_GLOBS` entry is
# appended directly rather than rglob'd (rglob on a file path would
# error). `ananta/setup.py` and `bootstrap.py` are scoped to themselves
# specifically, not the directories they sit in, since those directories
# hold unrelated already-scoped or not-yet-ruled content.
# COUPLED SURFACE: .claude/skills/git-controller-commit/SKILL.md Step 1's
# SCOPE regex must name every non-plugins/ path class added here, in the
# same commit — the skill's per-file Steps 2-6 derive their scope from that
# regex, not from this table, and an entry missing there is silently
# un-gated per-file (caught live 2026-08-09 on initialization/tests/).
_PER_FILE_GATE_TOP_LEVEL = (
    Path("ananta/src"), Path("ananta/tests"), Path("quality_gates"),
    Path("ananta/setup.py"), Path("bootstrap.py"), Path("deployment/scripts"),
    Path("initialization/__init__.py"), Path("initialization/profile_loader.py"),
    Path("initialization/tests"), Path("initialization/src"),
    Path("plugins/github_midwife_plugin/coordination_hooks_common"),
    Path("solet_cli/src"), Path("solet_cli/tests"), Path("bootstrap_adapter"),
)
# `solet_cli/src`, `solet_cli/tests`, `bootstrap_adapter` added 2026-09-20
# (lane-fix-gates-b-ungated-surfaces-20260920, unt_26776365, closing
# iss_f99ef8c2/577, iss_7e4169ea/632, iss_17bb31c5/707, iss_2456a716/777,
# iss_e252fc48/702, iss_e9990877/990016): both were confirmed ungated top-level
# code surfaces — `is_per_file_gate_scoped_path` returned False for every
# changed file in two separate live landings (unt_e5120eb0, unt_52f2dadb),
# producing a vacuous clean instead of a real scoped result. `solet_cli/tools`
# and `solet_cli/homebrew/{scripts,ci}` are deliberately NOT included here —
# no filed issue names them, and `solet_cli/tools` is already documented
# operator-tooling (`quality_gates/gate_smokes.txt`'s `generator_tooling`
# not-shipped class). COUPLED SURFACE per the note above this table: this
# entry, `_check_ruff`'s two hardcoded path strings, and the git-controller-commit
# SKILL.md Step 1 SCOPE regex all changed together in this same commit for
# all three paths. `[tool.pyright] include` in `pyproject.toml` gained ONLY
# `bootstrap_adapter` (fully clean) in that same commit — NOT `solet_cli/src`
# or `solet_cli/tests`: `solet_cli/src/solet_manager/stage_boundaries.py`
# carries two pyright findings that a live smoke proved are not dead code (see
# the SKILL.md coupling note for the full account), so solet_cli's whole-tree
# pyright coverage is a disclosed, deferred follow-up, not part of this
# widening. The per-file SCOPE (this table + the SKILL.md regex, used by
# Git-Controller's Step 3 with explicit file args, which bypasses `include`
# entirely) still covers solet_cli/src and solet_cli/tests for pyright on any
# landing that actually touches them.
# `initialization/tests` re-added 2026-08-09 alongside the
# `profile_loader_smoke.py::main` CC(18) fix (radon_cc B(10) or better
# throughout the file) — see
# workbench/2026-08-08_gate_scope_widening_measurement_d3-impl.md.
# `initialization/src` added 2026-08-10 (ruling arm-07c73c73), landed
# CC-clean same commit as the code fixes. COUPLED SURFACE, all four
# landed together: this table entry, the `ruff check` command string in
# `_check_ruff` (both occurrences — the invocation and the `--fix` hint),
# `[tool.pyright] include` in pyproject.toml, and the git-controller-commit
# SKILL.md Step 1 SCOPE regex. `initialization/` ships its own
# `pyproject.toml` (own [tool.ruff]/[tool.mypy], package `solets`,
# never installed into this repo's shared `.venv`) — the root config
# governs this gate regardless (ruling arm-07c73c73 §3); its local config
# stays untouched as that package's own dev surface, not ours to silence.
# The last two patterns are two segments deep, not one: a single-segment
# `plugins/*/hooks`/`plugins/*/tests` glob is blind to a bundle directory
# (`claude_plugin`/`codex_plugin`) sitting between the plugin root and its
# code. Kept as an explicit two-segment pattern rather than a recursive
# `plugins/*/**/tests` glob, which would also match `plugins/*/research/tests`
# (out of scope per the research exemption) and `plugins/*/src/**/tests`
# (already covered by the `plugins/*/src` rglob).
_PER_FILE_GATE_PLUGIN_GLOBS = (
    "plugins/*/src",
    "plugins/*/tests",
    "plugins/*/*/coordination-hooks/hooks",
    "plugins/*/*/coordination-hooks/tests",
    "plugins/*/policies",
    "plugins/*/scripts",
    "plugins/*/setup.py",
    "plugins/*/parity_tests",
    # `tools/` (8 plugins, 26 files): all 8 landed CC-clean 2026-08-09 (16
    # violations in default_knowledge_plugin, four D-rank, were the last
    # holdout) — see
    # workbench/2026-08-08_gate_scope_widening_measurement_d3-impl.md.
    "plugins/*/tools",
)


def is_per_file_gate_scoped_path(path: str) -> bool:
    """Whether one repo-relative Python path is in the per-file gate surface.

    This is the programmatic form of the Git-Controller scope rule.  Keeping
    it beside the roots consumed by :func:`_per_file_gate_paths` prevents a
    landing helper from carrying a second, stale approximation of the gate
    surface.  The predicate intentionally classifies by PATH SHAPE only: it
    does not require the file to exist or be git-tracked, because callers use
    it before staging to learn which supplemental checks they must run.
    """
    candidate = PurePosixPath(path)
    if not _is_relative_python_path(candidate):
        return False
    return _matches_top_level_scope(candidate) or _matches_plugin_scope(candidate)


def _is_relative_python_path(candidate: PurePosixPath) -> bool:
    return not candidate.is_absolute() and ".." not in candidate.parts and candidate.suffix == ".py"


def _matches_top_level_scope(candidate: PurePosixPath) -> bool:
    for root in _PER_FILE_GATE_TOP_LEVEL:
        if _matches_declared_root(candidate, root):
            return True
    return False


def _matches_declared_root(candidate: PurePosixPath, root: Path) -> bool:
    root_parts = root.parts
    if candidate.parts[:len(root_parts)] != root_parts:
        return False
    if root.suffix == ".py":
        return candidate.parts == root_parts
    return len(candidate.parts) > len(root_parts)


def _matches_plugin_scope(candidate: PurePosixPath) -> bool:
    for pattern in _PER_FILE_GATE_PLUGIN_GLOBS:
        if _matches_plugin_pattern(candidate, PurePosixPath(pattern)):
            return True
    return False


def _matches_plugin_pattern(candidate: PurePosixPath, pattern: PurePosixPath) -> bool:
    exact_file = pattern.parts[-1].endswith(".py")
    if exact_file and len(candidate.parts) != len(pattern.parts):
        return False
    if not exact_file and len(candidate.parts) <= len(pattern.parts):
        return False
    return all(
        expected == "*" or actual == expected
        for actual, expected in zip(candidate.parts, pattern.parts, strict=False)
    )

# Path-segment prefix that flags a directory as a bundled venv (e.g.
# `.venv`, `.venv_cosyvoice`). Bundled venvs ship vendored library code
# (Cython, torch internals, etc.) that has unrelated coherence
# characteristics and is not part of the platform's quality surface.


def run_command(cmd: str, description: str, timeout: int = 60) -> tuple[bool, str]:
    """Run a command and return (success, output)."""
    print(f"🔍 {description}...")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout} seconds"
    except Exception as e:
        return False, str(e)


# Paths whose staged changes trigger the prompt assembly fixture replay.
_PROMPT_ASSEMBLY_TRIGGERS = (
    "ananta/src/ananta/core/prompts/",
    "ananta/knowledge_base/processes/",
    "plugins/default_inference_plugin/src/default_inference_plugin/plugin.py",
    "plugins/default_inference_plugin/research/prompt_engineering/prompt_sets/",
)


def _staged_files_touch_prompt_assembly() -> bool:
    """Check if any staged files fall under prompt assembly paths."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        staged = result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return any(
        f.startswith(trigger) for f in staged for trigger in _PROMPT_ASSEMBLY_TRIGGERS
    )


_PROMPT_SET_DIR = "plugins/default_inference_plugin/research/prompt_engineering/prompt_sets"
_RENDERER_SCRIPT = "plugins/default_inference_plugin/research/prompt_engineering/render_prompt_readable.py"


def _staged_prompt_json_files() -> list[str]:
    """Return staged *_prompt.json paths under the prompt set directory."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        staged = result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [
        f for f in staged
        if f.startswith(_PROMPT_SET_DIR)
        and f.endswith("_prompt.json")
        and "/archive/" not in f
    ]


def _existing_readable_title(readable: Path) -> str | None:
    try:
        first_line = readable.read_text().splitlines()[0].strip()
    except Exception:
        return None
    return first_line or None


def _render_to_temp(venv_python: Path, renderer: Path, json_file: Path, title: str | None) -> str | None:
    """Run the renderer to a temp file; return generated content or None on failure."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
    cmd = [str(venv_python), str(renderer), "--prompt", str(json_file), "--out", tmp_path]
    if title:
        cmd.extend(["--title", title])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    try:
        if result.returncode != 0:
            return None
        return Path(tmp_path).read_text()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _check_single_readable(project_root: Path, venv_python: Path, renderer: Path, rel_json: str) -> str | None:
    """Return a stale-line marker for this JSON, or None if up-to-date."""
    json_file = project_root / rel_json
    if not json_file.is_file():
        return None
    readable = json_file.with_name(
        json_file.name.replace("_prompt.json", "_readable.txt")
    )
    if not readable.exists():
        return f"  missing: {readable.relative_to(project_root)}"

    # Pass the existing readable's title so the renderer uses the same
    # title as a normal regeneration (which reads it from out_path).
    # Without this, --out to a temp file means the renderer can't find
    # the existing title and falls through to a different default.
    existing_title = _existing_readable_title(readable)
    generated = _render_to_temp(venv_python, renderer, json_file, existing_title)
    if generated is None:
        return None
    if generated != readable.read_text():
        return f"  stale:   {readable.relative_to(project_root)}"
    return None


def _check_readable_freshness(project_root: Path, venv_python: Path) -> bool:
    """Check that *_readable.txt files match their staged *_prompt.json sources.

    Only checks JSON fixtures that are in the current staging area, so
    pre-existing drift in unmodified prompt sets does not block unrelated commits.

    Returns True if stale files were found (should block commit).
    """
    renderer = project_root / _RENDERER_SCRIPT
    if not renderer.is_file():
        return False

    staged_jsons = _staged_prompt_json_files()
    if not staged_jsons:
        return False

    print("\n📊 Readable Fixture Freshness Check...")
    stale = [
        marker
        for rel_json in staged_jsons
        if (marker := _check_single_readable(project_root, venv_python, renderer, rel_json)) is not None
    ]

    if not stale:
        print("✅ Readable fixtures up-to-date with staged JSON sources")
        return False

    print("❌ BLOCKING: Readable fixture files are out of sync with JSON sources!")
    for line in stale:
        print(line)
    print(f"\n💡 Regenerate with: {venv_python} {renderer} --prompt <json_file>")
    return True


def _run_fixture_replay_if_needed(project_root: Path, venv_python: Path) -> bool:
    """Run fixture replay tests if prompt assembly files are staged.

    Returns True if tests failed (should block commit).
    """
    if not _staged_files_touch_prompt_assembly():
        return False

    print("\n📊 Prompt Assembly Fixture Replay...")
    test_dir = "ananta/src/ananta/core/prompts/tests/"
    if not (project_root / test_dir).is_dir():
        print("⏭️  Fixture replay: test directory not present, skipping")
        return False
    pytest_cmd = f"cd {project_root} && {venv_python} -m pytest {test_dir} -q --tb=short 2>&1"
    success, output = run_command(pytest_cmd, "Running fixture replay tests", timeout=30)
    if success:
        print("✅ Fixture replay: all tests passed")
        return False

    print("❌ BLOCKING: Fixture replay tests failed!")
    # Show last 15 lines of output (summary + failures)
    lines = output.strip().splitlines()
    for line in lines[-15:]:
        print(f"   {line}")
    print(f"\n💡 Run: {venv_python} -m pytest {test_dir} -v")
    return True


@dataclass
class _CheckResults:
    has_type_errors: bool = False
    has_lint_errors: bool = False
    # Names of blocking structural gates (coherence trio, W-INT,
    # service-interface AST) that reported non-allowlisted findings —
    # the summary attributes the failure to each gate by name.
    failed_blocking_gates: list[str] = field(default_factory=list)
    # Gates that RAISED instead of measuring, as (gate_name, log excerpt).
    # Kept apart from `failed_blocking_gates` so the summary can say "this
    # gate produced no verdict" instead of asserting violations that were
    # never counted.
    crashed_gates: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _GateSpec:
    name: str
    description: str
    script: Path
    allowlist: Path


_COHERENCE_GATES: tuple[_GateSpec, ...] = (
    _GateSpec(
        name="god_class",
        description="god-class structure",
        script=_GOD_CLASS_CHECK,
        allowlist=_GOD_CLASS_ALLOWLIST,
    ),
    _GateSpec(
        name="radon_cc",
        description="cyclomatic complexity",
        script=_RADON_CC_CHECK,
        allowlist=_RADON_CC_ALLOWLIST,
    ),
    _GateSpec(
        name="radon_mi",
        description="maintainability index",
        script=_RADON_MI_CHECK,
        allowlist=_RADON_MI_ALLOWLIST,
    ),
)

# Whole-tree integration gate (W-INT) runs differently from the per-file
# coherence gates: it walks the tree from its own anchors (service public.py
# files, plugin.py files, KB JSON dirs, scheduling call-sites), so no
# scope_paths argument is threaded through. Wrapper signature, exit codes
# (0/1/64) and allowlist semantics mirror the per-file gates; integration
# uses a separate runner to skip the scope-path argv splicing.
_W_INT_GATE = _GateSpec(
    name="whole_tree_integration",
    description="whole-tree integration",
    script=_W_INT_CHECK,
    allowlist=_W_INT_ALLOWLIST,
)
_W_INT_WRAPPER_BLOCKING = 1  # gate uses 1 (mirrors design doc), not 2

# Setup-flow probes are executable claims: every reference must resolve through
# the source-owned bootstrap, target-local, or platform-registry surface. This
# gate walks those sources directly and uses 1 for a blocking finding.
_FLOW_PROBE_REGISTRY_GATE = _GateSpec(
    name="flow_probe_registry",
    description="setup-flow probe registry",
    script=_FLOW_PROBE_REGISTRY_CHECK,
    allowlist=_FLOW_PROBE_REGISTRY_ALLOWLIST,
)
_FLOW_PROBE_REGISTRY_WRAPPER_BLOCKING = 1

# Service-interface AST gate. Tree-walking (no scope-path argv splicing) —
# walks every ananta/src/ananta/services/*/interfaces/public.py and every
# ananta/knowledge_base/processes/<provider>/*.json on each invocation.
# Three checks per design v1 §1: (a) @service_interface_process(name=...)
# matches function __name__; (b) every @abstractmethod in a service-
# interface ABC carries @service_interface_process; (c) every JSON in
# processes/<provider>/ matches a registered decorator. Exit codes
# follow _WRAPPER_OK (0) / _WRAPPER_BLOCKING (2) — canonical pattern.
# See the service-interface-AST-gate design record v1 (dev-checkout
# workbench — not part of the shipped tree).
_SI_AST_GATE = _GateSpec(
    name="service_interface_ast",
    description="service-interface AST consistency",
    script=_SI_AST_CHECK,
    allowlist=_SI_AST_ALLOWLIST,
)

# Process return-shape gate (GTE-07, REL-11 follow-up). Tree-walking (no
# scope-path argv splicing) — AST-scans every @service_interface_process /
# @platform_process decorated def across ananta/src + plugins/*/src and
# fails on return annotations the process_call dispatch contract rejects
# (non-dict; dataclass DTOs allowed on the service path only, TypedDict
# allowed on both). Exit codes follow _WRAPPER_OK (0) / _WRAPPER_BLOCKING (2).
_RETURN_SHAPE_GATE = _GateSpec(
    name="return_shape",
    description="process return-shape",
    script=_RETURN_SHAPE_CHECK,
    allowlist=_RETURN_SHAPE_ALLOWLIST,
)

# embedding_description length bound. The platform ALREADY checks this at
# registry load, but WARNING-only, so nothing ever red and nothing ran at
# authoring time — 77 of 528 process JSONs were out of range when the gate was
# built (2026-07-30), including recent additions. This is the enforcement half.
# The gate reads the bound out of the validator's own source, so it cannot
# drift from the constraint it mirrors.
_EMBEDDING_BOUND_GATE = _GateSpec(
    name="embedding_description_bound",
    description="embedding_description length bound",
    script=_EMBEDDING_BOUND_CHECK,
    allowlist=_EMBEDDING_BOUND_ALLOWLIST,
)

# W-INT Cycle 2 driver-import gate (W-WINT2-EARLY) ships in WARN mode per
# master plan §1.7. Findings print but do NOT contribute to the blocking
# verdict — the gate's role at Tier 0 is to ratchet against NEW driver-
# import drift while cleanup workstreams remove the existing bypass sites.
# W-WINT2-FINAL (Tier 7) drops the `--warn-only` flag to flip to fail-mode.
_W_WINT2_GATE = _GateSpec(
    name="wint2_driver_import",
    description="W-INT Cycle 2 driver-import (warn)",
    script=_W_WINT2_CHECK,
    allowlist=_W_WINT2_ALLOWLIST,
)

# W-INT Cycle 2 vault-key-declaration gate (W-PLUGIN-LAUNCH-KEYS sub-1)
# ships in WARN mode at sub-1 landing per brief §5.3. Findings print but
# do NOT contribute to the blocking verdict; sub-2 (W-VAULT-CALLER-ENFORCE)
# drops the `--warn-only` flag to flip to fail-mode in lockstep with the
# runtime readiness gate's mode flip.
_W_WINT2_VAULT_KEY_GATE = _GateSpec(
    name="wint2_vault_key_declaration",
    description="W-INT Cycle 2 vault-key declaration (warn)",
    script=_W_WINT2_VAULT_KEY_CHECK,
    allowlist=_W_WINT2_VAULT_KEY_ALLOWLIST,
)


# Shipped-document gate (GTE-10). Tree-walking (no scope-path argv splicing) —
# derives what this checkout WOULD ship from seed_manifest.yaml and runs the
# seal-time cited-path and reserved-identity checks over it, bundle-free. It
# exists because both of those instruments take an assembled `bundle_dir`, so
# neither could run before a commit existed: measured on 2026-08-17/18, three
# shipped-content defects reached master and every one was caught at MINT time,
# each costing a full re-assemble/re-seal/re-verify lap.
#
# Its allowlist is the SAME tracked-debt register the seal-time gate reads, so
# a finding remediated for one is remediated for both; the per-profile
# tolerated-count declarations live beside it in shipped_doc_baseline.txt.
# Exit codes follow the canonical _WRAPPER_OK (0) / _WRAPPER_BLOCKING (2)
# pattern rather than sql_access_gate.py's 1, because exit 1 collides with
# Python's unhandled-exception code and _classify_gate_exit would have to read
# a crash as a finding count.
#
# In a BORN CLONE the seed factory does not ship (NO-FACTORY), so there is no
# manifest and no seed to mint; the gate prints a declared skip and exits 0.
# That is deliberate and it is checked: a shipped executable that assumes an
# unshipped path is one of the three defects this gate was built for.
_SHIPPED_DOC_GATE = _GateSpec(
    name="shipped_doc",
    description="shipped-document citations and identity",
    script=_SHIPPED_DOC_CHECK,
    allowlist=_SHIPPED_DOC_ALLOWLIST,
)

# Bundle-license gate (D-6.1). Tree-walking: its own bundle manifest is the
# scope, and it resolves every member's transitive dependency closure. This is
# deliberately BLOCKING: an Apache-2.0 bundle with a newly unlicensed plugin,
# a non-Apache declaration, strong copyleft, or an incomplete closure must not
# reach the quality summary as a warning-only observation. The gate returns 1
# for non-allowlisted findings, so its wrapper code is explicit below.
_BUNDLE_LICENSE_GATE = _GateSpec(
    name="bundle_license",
    description="bundle license compliance",
    script=_BUNDLE_LICENSE_CHECK,
    allowlist=_BUNDLE_LICENSE_ALLOWLIST,
)
_BUNDLE_LICENSE_WRAPPER_BLOCKING = 1

# Schema-init gate (iss_63d91ca9 member 2, 2026-09-20). Tree-walking and
# BEHAVIORAL: it boots the platform's own initialize_schemas path -- core
# schemas, every plugin's get_schema_definitions() instantiated as startup
# does, the real SchemaManager/SchemaStandardizer -- in a child interpreter
# whose import roots are proven to be THIS tree, with only the database write
# recorded. It exists because a landed ColumnDefinition that redeclared a
# platform-protected field passed every static gate here and then crashed
# every blue-green candidate boot (iss_2bc76075, repeated as iss_5f91287d).
# Deliberately BLOCKING from registration: a schema that cannot initialize is
# a startup crash, not a style finding, and by fleet-steward ruling (2026-09-20)
# a live boot-crash is never allowlisted -- the gate reddening the tree until
# the fix lands is the correct verdict. Exit codes follow the canonical
# _WRAPPER_OK / _WRAPPER_BLOCKING pattern; 70 is a child crash (not a verdict).
_SCHEMA_INIT_GATE = _GateSpec(
    name="schema_init",
    description="schema-init boot (initialize_schemas against this tree)",
    script=_SCHEMA_INIT_CHECK,
    allowlist=_SCHEMA_INIT_ALLOWLIST,
)


def _scope_roots(project_root: Path) -> list[Path]:
    """Resolve the declared quality-surface roots that exist in this checkout."""
    roots = [
        candidate
        for top in _PER_FILE_GATE_TOP_LEVEL
        if (candidate := project_root / top).exists()
    ]
    for pattern in _PER_FILE_GATE_PLUGIN_GLOBS:
        roots.extend(sorted(project_root.glob(pattern)))
    return roots


def _per_file_gate_paths(project_root: Path) -> list[Path]:
    """Enumerate every in-scope `.py` file (KB "Peer Pre-Completion Gate Procedure").

    Walks each scope root recursively and keeps a path only if git tracks it.
    The tracked filter is the load-bearing one: a `.venv`-prefixed name prune
    catches the bundled venvs we happen to have (and is kept below as cheap
    defence in depth), but it is a NAME check, so any vendored tree called
    something else — `node_modules`, `site-packages`, a plain `vendor/` —
    walks straight through it. Being untracked is the property that actually
    distinguishes vendored code from ours.

    Measured 2026-08-16: `plugins/cosyvoice2_tts_plugin/src/.venv_cosyvoice`
    holds 18,321 `.py` files against that plugin's 15 tracked ones, and one
    of them raises `RecursionError` inside radon's AST walk.

    The wrappers receive the concrete file list as separate argv entries —
    never directories, which would re-walk into vendored code inside the
    wrapper.
    """
    tracked = repo_files(project_root)
    py_files: list[Path] = []
    for root in _scope_roots(project_root):
        if root.is_file():
            candidates = [root]
        else:
            candidates = [
                path for path in root.rglob("*.py")
                if not any(
                    part.startswith(BUNDLED_VENV_PREFIX) for part in path.parts
                )
            ]
        py_files.extend(p for p in candidates if p.resolve() in tracked)
    return py_files


def _resolve_gate_artifacts(project_root: Path, gate: _GateSpec) -> tuple[Path, Path] | None:
    """Resolve and validate gate script + allowlist paths. Print + None on miss."""
    script = project_root / gate.script
    allowlist = project_root / gate.allowlist
    if not script.exists():
        print(f"❌ BLOCKING: gate script missing: {script}")
        return None
    if not allowlist.exists():
        print(f"❌ BLOCKING: allowlist missing: {allowlist}")
        return None
    return script, allowlist


def _invoke_gate_subprocess(
    venv_python: Path, script: Path, scope_paths: list[Path], allowlist: Path,
) -> subprocess.CompletedProcess[str] | str:
    """Invoke the wrapper script; return CompletedProcess or an error string."""
    argv = [
        str(venv_python),
        str(script),
        *[str(p) for p in scope_paths],
        "--allowlist",
        str(allowlist),
    ]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "timed out after 180s"
    except FileNotFoundError as exc:
        return f"cannot invoke gate: {exc}"


def _print_gate_blocking(gate_name: str, combined: str, venv_python: Path,
                         script: Path, allowlist: Path) -> None:
    """Print the blocking-finding report for one gate.

    Filters out [allowlisted] lines (they're tracked debt, not findings),
    radon mi's per-file `<path> - A (...)` / `<path> - B (...)` clean-rank
    chatter, and python-process boot noise. What survives is the actual
    non-allowlisted findings + the wrapper's summary line.
    """
    print(f"❌ BLOCKING: {gate_name} gate found non-allowlisted findings:")
    survivors: list[str] = []
    for line in combined.splitlines():
        if "[allowlisted]" in line:
            continue
        if " - A (" in line or " - B (" in line:
            continue
        if line.startswith("W") and "torch" in line:
            continue
        if line.strip():
            survivors.append(line)
    for line in survivors[:30]:
        print(f"   {line}")
    extra = max(0, len(survivors) - 30)
    if extra:
        print(f"   ... and {extra} more lines")
    print(f"\n💡 Re-run: {venv_python} {script} <paths> --allowlist {allowlist}")


def _print_gate_ok(gate_name: str, combined: str) -> None:
    """Print the clean-summary line for one gate (suppressing allowlisted noise)."""
    for line in combined.splitlines():
        if line.startswith("OK:"):
            print(f"✅ {line}")
            return
    print(f"✅ {gate_name} gate clean")


def _crash_excerpt(combined: str) -> str:
    """The most diagnostic tail of a crashed run, bounded for the summary.

    Prefers the wrapper's own GATE-CRASH line, then the traceback, then the
    tail. Something is always returned: a crash reported without evidence is
    only marginally better than a crash reported as a violation.
    """
    lines = [ln for ln in combined.splitlines() if ln.strip()]
    if not lines:
        return "(no output)"
    for marker in ("GATE-CRASH:", _TRACEBACK_MARKER):
        for idx, line in enumerate(lines):
            if marker in line:
                return "\n".join(lines[idx:][:12])
    return "\n".join(lines[-6:])


def _print_gate_crash(gate_name: str, returncode: int, combined: str) -> str:
    """Report a gate that produced no verdict. Returns the excerpt recorded."""
    excerpt = _crash_excerpt(combined)
    print(f"🛑 GATE CRASH: {gate_name} produced NO VERDICT (exit {returncode}).")
    print("   This is not a violation count — nothing was measured.")
    for line in excerpt.splitlines():
        print(f"   {line}")
    return excerpt


def _classify_gate_exit(returncode: int, combined: str, blocking_code: int) -> _GateOutcome:
    """Map an exit code + output to what the run actually established.

    Crash detection runs FIRST, and the traceback marker outranks the exit
    code, because the two are not independent signals: the W-INT gate's
    blocking code is 1, which is also what Python exits with on an unhandled
    exception. Testing the code first would classify every W-INT crash as a
    W-INT violation — the exact defect this function exists to prevent,
    reintroduced through a collision. When a run printed a traceback, it
    raised; what it exited with afterwards says nothing.
    """
    if returncode == _WRAPPER_GATE_CRASH or _TRACEBACK_MARKER in combined:
        return _GateOutcome.CRASH
    if returncode == _WRAPPER_OK:
        return _GateOutcome.OK
    if returncode in (blocking_code, _WRAPPER_USAGE_ERROR):
        return _GateOutcome.BLOCKING
    # An exit code outside the wrapper's declared contract is, by definition,
    # not a verdict it knows how to give.
    return _GateOutcome.CRASH


def _interpret_gate_result(
    gate_name: str, result: subprocess.CompletedProcess[str],
    venv_python: Path, script: Path, allowlist: Path,
    results: _CheckResults,
) -> _GateOutcome:
    """Translate a wrapper's exit code into what the run established."""
    combined = (result.stdout + result.stderr).rstrip()
    outcome = _classify_gate_exit(result.returncode, combined, _WRAPPER_BLOCKING)
    if outcome is _GateOutcome.OK:
        _print_gate_ok(gate_name, combined)
        return outcome
    if outcome is _GateOutcome.CRASH:
        results.crashed_gates.append(
            (gate_name, _print_gate_crash(gate_name, result.returncode, combined))
        )
        return outcome
    if result.returncode == _WRAPPER_USAGE_ERROR:
        print(f"❌ BLOCKING: {gate_name} gate usage error (exit 64):")
        print(combined or "(no output)")
        return outcome
    _print_gate_blocking(gate_name, combined, venv_python, script, allowlist)
    return outcome


def _run_coherence_gate(
    venv_python: Path, gate: _GateSpec, project_root: Path, scope_paths: list[Path],
    results: _CheckResults,
) -> _GateOutcome:
    """Run one coherence gate and report what it established."""
    artifacts = _resolve_gate_artifacts(project_root, gate)
    if artifacts is None:
        return _GateOutcome.BLOCKING
    script, allowlist = artifacts

    print(f"\n📊 {gate.description.title()} Gate ({gate.name})...")
    result = _invoke_gate_subprocess(venv_python, script, scope_paths, allowlist)
    if isinstance(result, str):
        # Could not run at all (timeout / missing interpreter) — no verdict.
        results.crashed_gates.append((gate.name, result))
        print(f"🛑 GATE CRASH: {gate.name} produced NO VERDICT — {result}")
        return _GateOutcome.CRASH
    return _interpret_gate_result(
        gate.name, result, venv_python, script, allowlist, results,
    )


def _check_coherence_gates(project_root: Path, venv_python: Path,
                           results: _CheckResults) -> list[str]:
    """Run all three coherence gates; return the names of gates that blocked.

    Crashes are recorded on `results.crashed_gates` rather than returned:
    they are not findings, and the summary reports them under their own
    heading.
    """
    try:
        scope_paths = _per_file_gate_paths(project_root)
    except GateCrashError as exc:
        print(f"\n🛑 GATE CRASH: per-file gate scope could not be resolved — {exc}")
        results.crashed_gates.append(("per-file-scope-resolution", str(exc)))
        return []
    if not scope_paths:
        print("\n❌ BLOCKING: per-file gate scope resolved to zero paths")
        return ["per-file-scope-resolution"]
    print(f"   scope: {len(scope_paths)} in-repo .py file(s)")
    return [
        gate.name
        for gate in _COHERENCE_GATES
        if _run_coherence_gate(venv_python, gate, project_root, scope_paths, results)
        is _GateOutcome.BLOCKING
    ]


def _interpret_tree_gate_result(
    gate: _GateSpec, result: subprocess.CompletedProcess[str],
    venv_python: Path, script: Path, allowlist: Path,
    results: _CheckResults, blocking_code: int = _WRAPPER_BLOCKING,
) -> bool:
    """Shared tail for the whole-tree gates. True iff blocking FINDINGS.

    A crash returns False and is recorded on `results.crashed_gates`: it is
    not a finding, and the summary must not count it as one. The run still
    fails overall, because an unmeasured gate is not a passed gate.
    """
    combined = (result.stdout + result.stderr).rstrip()
    outcome = _classify_gate_exit(result.returncode, combined, blocking_code)
    if outcome is _GateOutcome.OK:
        _print_gate_ok(gate.name, combined)
        return False
    if outcome is _GateOutcome.CRASH:
        results.crashed_gates.append(
            (gate.name, _print_gate_crash(gate.name, result.returncode, combined))
        )
        return False
    _print_gate_blocking(gate.name, combined, venv_python, script, allowlist)
    return True


def _check_whole_tree_integration_gate(project_root: Path, venv_python: Path,
                                       results: _CheckResults) -> bool:
    """Run the W-INT gate (structural mode). True iff non-allowlisted findings."""
    artifacts = _resolve_gate_artifacts(project_root, _W_INT_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_W_INT_GATE.description.title()} Gate ({_W_INT_GATE.name})...")
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_W_INT_GATE.name} gate timed out after 180s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_W_INT_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _W_INT_GATE, result, venv_python, script, allowlist, results,
        blocking_code=_W_INT_WRAPPER_BLOCKING,
    )


def _check_flow_probe_registry_gate(
    project_root: Path, venv_python: Path, results: _CheckResults,
) -> bool:
    """Run the setup-flow probe resolver gate against the live repository."""
    artifacts = _resolve_gate_artifacts(project_root, _FLOW_PROBE_REGISTRY_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(
        f"\n📊 {_FLOW_PROBE_REGISTRY_GATE.description.title()} "
        f"Gate ({_FLOW_PROBE_REGISTRY_GATE.name})...",
    )
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_FLOW_PROBE_REGISTRY_GATE.name} gate timed out after 180s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_FLOW_PROBE_REGISTRY_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _FLOW_PROBE_REGISTRY_GATE,
        result,
        venv_python,
        script,
        allowlist,
        results,
        blocking_code=_FLOW_PROBE_REGISTRY_WRAPPER_BLOCKING,
    )


def _check_service_interface_ast_gate(project_root: Path, venv_python: Path,
                                      results: _CheckResults) -> bool:
    """Run the service-interface AST gate. True iff non-allowlisted findings.

    Tree-walking gate per design v1 §5 — walks every interfaces/public.py +
    every processes/<provider>/*.json on each invocation. Three checks
    (a/b/c). Exit codes follow the canonical _WRAPPER_OK / _WRAPPER_BLOCKING
    pattern.
    """
    artifacts = _resolve_gate_artifacts(project_root, _SI_AST_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_SI_AST_GATE.description.title()} Gate ({_SI_AST_GATE.name})...")
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_SI_AST_GATE.name} gate timed out after 120s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_SI_AST_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _SI_AST_GATE, result, venv_python, script, allowlist, results,
    )


def _check_return_shape_gate(project_root: Path, venv_python: Path,
                             results: _CheckResults) -> bool:
    """Run the process return-shape gate. True iff non-allowlisted findings.

    Tree-walking gate (GTE-07): AST over every decorated verb; a non-dict
    return annotation is dead on arrival over process_call (the REL-11
    class). Exit codes follow the canonical _WRAPPER_OK / _WRAPPER_BLOCKING
    pattern.
    """
    artifacts = _resolve_gate_artifacts(project_root, _RETURN_SHAPE_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_RETURN_SHAPE_GATE.description.title()} Gate ({_RETURN_SHAPE_GATE.name})...")
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_RETURN_SHAPE_GATE.name} gate timed out after 120s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_RETURN_SHAPE_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _RETURN_SHAPE_GATE, result, venv_python, script, allowlist, results,
    )


def _check_shipped_doc_gate(project_root: Path, venv_python: Path,
                            results: _CheckResults) -> bool:
    """Run the shipped-document gate (GTE-10). True iff non-allowlisted findings.

    Tree-walking gate: no scope-path argv splicing — it derives its own file
    set from the seed manifest rather than being handed one, because "what
    ships" is a manifest question and not a checkout-walk question. Exit codes
    follow the canonical _WRAPPER_OK / _WRAPPER_BLOCKING pattern.
    """
    artifacts = _resolve_gate_artifacts(project_root, _SHIPPED_DOC_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_SHIPPED_DOC_GATE.description.title()} Gate ({_SHIPPED_DOC_GATE.name})...")
    argv = [str(venv_python), str(script), "--repo-root", str(project_root),
            "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_SHIPPED_DOC_GATE.name} gate timed out after 300s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_SHIPPED_DOC_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _SHIPPED_DOC_GATE, result, venv_python, script, allowlist, results,
    )


def _check_bundle_license_gate(project_root: Path, venv_python: Path,
                               results: _CheckResults) -> bool:
    """Run the blocking bundle-license gate. True iff findings remain."""

    artifacts = _resolve_gate_artifacts(project_root, _BUNDLE_LICENSE_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(
        f"\n📊 {_BUNDLE_LICENSE_GATE.description.title()} "
        f"Gate ({_BUNDLE_LICENSE_GATE.name})...",
    )
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_BUNDLE_LICENSE_GATE.name} gate timed out after 180s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_BUNDLE_LICENSE_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _BUNDLE_LICENSE_GATE,
        result,
        venv_python,
        script,
        allowlist,
        results,
        blocking_code=_BUNDLE_LICENSE_WRAPPER_BLOCKING,
    )


def _check_schema_init_gate(project_root: Path, venv_python: Path,
                            results: _CheckResults) -> bool:
    """Run the schema-init boot gate. True iff non-allowlisted findings.

    Behavioral, tree-walking gate: no scope-path argv splicing, because a
    change to a shared table builder can break a schema declared elsewhere
    and the whole-tree boot is cheap. SOLET_NAME is passed through when the
    caller exported it (the standing gate habit); the gate discloses when it
    had to supply its own, since several plugins refuse to import without one.
    """
    artifacts = _resolve_gate_artifacts(project_root, _SCHEMA_INIT_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(f"\n📊 {_SCHEMA_INIT_GATE.description.title()} Gate ({_SCHEMA_INIT_GATE.name})...")
    argv = [str(venv_python), str(script), "--repo-root", str(project_root),
            "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_SCHEMA_INIT_GATE.name} gate timed out after 300s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_SCHEMA_INIT_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _SCHEMA_INIT_GATE, result, venv_python, script, allowlist, results,
    )


def _check_embedding_bound_gate(project_root: Path, venv_python: Path,
                                results: _CheckResults) -> bool:
    """Run the embedding_description bound gate. True iff non-allowlisted findings.

    Tree-walking gate: every discoverable process JSON's embedding_description
    is measured against the platform's own [MIN, MAX] constants, read out of
    plugin_registration_validator.py at run time. The registry-load check is
    WARNING-only; this is the authoring-time half. Exit codes follow the
    canonical _WRAPPER_OK / _WRAPPER_BLOCKING pattern.
    """
    artifacts = _resolve_gate_artifacts(project_root, _EMBEDDING_BOUND_GATE)
    if artifacts is None:
        return True
    script, allowlist = artifacts

    print(
        f"\n📊 {_EMBEDDING_BOUND_GATE.description.title()} "
        f"Gate ({_EMBEDDING_BOUND_GATE.name})...",
    )
    argv = [str(venv_python), str(script), "--allowlist", str(allowlist)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"❌ BLOCKING: {_EMBEDDING_BOUND_GATE.name} gate timed out after 120s")
        return True
    except FileNotFoundError as exc:
        print(f"❌ BLOCKING: {_EMBEDDING_BOUND_GATE.name} gate cannot invoke: {exc}")
        return True

    return _interpret_tree_gate_result(
        _EMBEDDING_BOUND_GATE, result, venv_python, script, allowlist, results,
    )


def _check_wint2_warn_only_gate(
    project_root: Path, venv_python: Path, gate: _GateSpec,
) -> None:
    """Run a W-INT Cycle 2 wrapper script in WARN mode.

    Shared driver for the two Cycle 2 gates (driver-import +
    vault-key-declaration). Per master plan §1.7 + brief §5.3,
    findings print to the summary but do NOT contribute to the
    blocking verdict during the state-service consolidation
    campaign. Mode flips warn→fail in a follow-up commit (driver-
    import at W-WINT2-FINAL; vault-key-declaration at sub-2
    W-VAULT-CALLER-ENFORCE) by dropping the `--warn-only` flag.
    """
    script = project_root / gate.script
    allowlist = project_root / gate.allowlist
    if not script.exists():
        print(
            f"⚠️ {gate.name} gate script missing: {script} "
            "(warn mode: continuing)",
        )
        return
    if not allowlist.exists():
        print(
            f"⚠️ {gate.name} gate allowlist missing: {allowlist} "
            "(warn mode: continuing)",
        )
        return

    print(f"\n📊 {gate.description.title()} Gate ({gate.name})...")
    argv = [
        str(venv_python), str(script),
        "--allowlist", str(allowlist),
        "--warn-only",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(
            f"⚠️ {gate.name} gate timed out after 180s "
            "(warn mode: continuing)",
        )
        return
    except FileNotFoundError as exc:
        print(
            f"⚠️ {gate.name} gate cannot invoke: {exc} "
            "(warn mode: continuing)",
        )
        return

    combined = (result.stdout + result.stderr).rstrip()
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(gate.name, combined)
        return
    print(
        f"⚠️ {gate.name} gate unexpected exit "
        f"{result.returncode} (warn mode: continuing):",
    )
    print(combined or "(no output)")


def _check_wint2_driver_import_gate(project_root: Path, venv_python: Path) -> None:
    """Run W-INT Cycle 2 driver-import gate in WARN mode.

    Per master plan §1.7, findings print to the summary but do NOT
    contribute to the blocking verdict during the state-service
    consolidation campaign. Mode flips warn→fail at W-WINT2-FINAL by
    dropping the `--warn-only` flag below.
    """
    script = project_root / _W_WINT2_GATE.script
    allowlist = project_root / _W_WINT2_GATE.allowlist
    if not script.exists():
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate script missing: {script} "
            "(warn mode: continuing)"
        )
        return
    if not allowlist.exists():
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate allowlist missing: {allowlist} "
            "(warn mode: continuing)"
        )
        return

    print(f"\n📊 {_W_WINT2_GATE.description.title()} Gate ({_W_WINT2_GATE.name})...")
    argv = [
        str(venv_python), str(script),
        "--allowlist", str(allowlist),
        "--warn-only",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate timed out after 180s "
            "(warn mode: continuing)"
        )
        return
    except FileNotFoundError as exc:
        print(
            f"⚠️ {_W_WINT2_GATE.name} gate cannot invoke: {exc} "
            "(warn mode: continuing)"
        )
        return

    combined = (result.stdout + result.stderr).rstrip()
    # `--warn-only` always returns 0; non-zero is a harness anomaly. Print
    # and continue either way — no blocking verdict in warn mode.
    if result.returncode == _WRAPPER_OK:
        _print_gate_ok(_W_WINT2_GATE.name, combined)
        return
    print(
        f"⚠️ {_W_WINT2_GATE.name} gate unexpected exit "
        f"{result.returncode} (warn mode: continuing):"
    )
    print(combined or "(no output)")


def _find_venv_python(project_root: Path) -> Path | None:
    venv_paths = [
        project_root / ".venv" / "bin" / "python3",
        project_root / "ananta" / "venv" / "bin" / "python3",
        project_root / "venv" / "bin" / "python3",
    ]
    for path in venv_paths:
        if path.exists():
            return path
    return None


def _check_gate_toolchain(project_root: Path) -> bool:
    """True if ruff/pyright/radon are all present; prints the shared preflight's own message if not.

    Runs BEFORE `_check_pyright`/`_check_ruff` deliberately: a `python3 -m
    pyright`/`-m ruff` invocation against a venv where the module is simply
    not installed does not raise a normal per-tool error — `_check_pyright`
    in particular has no way to distinguish "0 type errors" from "pyright
    isn't here to report any," since its error count comes from counting
    pyright's own `" - error:"` diagnostic lines, which a bare
    `ModuleNotFoundError` traceback never contains. Undeclared-dependency
    audit: workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
    """
    script = project_root / "deployment" / "scripts" / "check_gate_toolchain.sh"
    if not script.exists():
        print(
            f"FAIL: the gate-toolchain preflight script itself is missing: {script}\n"
            "This platform ships it at deployment/scripts/check_gate_toolchain.sh, so "
            "this solet cannot verify ruff/pyright/radon are installed before "
            "running the gate. Pull an update that carries this file (born-clone-gate-"
            "toolchain fix) rather than proceeding without a toolchain check.",
        )
        return False
    result = subprocess.run([str(script), "gate"], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, end="")
        return False
    return True


class ToolUnavailableError(RuntimeError):
    """A required checker never executed at all -- distinct from running and
    finding zero issues, and distinct from running and producing genuinely
    unparseable output. Fatal, always: a checker that did not run is not a
    passing checker, regardless of what upstream preflight ran or didn't run
    before this call -- reachability is a property of the current call
    graph, not of the defect this guards against. Undeclared-dependency
    audit: workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
    """


def _module_unavailable(output: str, module: str) -> bool:
    """True iff `output` is Python's OWN ``-m`` failure for a genuinely
    unresolvable module -- ``<interpreter>: No module named <module>``,
    printed by the interpreter itself before the module ever loads. This is
    categorically different from a diagnostic the module emits once it HAS
    loaded (which requires the module to already be importable), so this
    string is never something a working checker would itself print as a
    finding -- it can only mean "this never ran."
    """
    return f"No module named {module}" in output


def _report_pyright_errors(output: str, error_count: int) -> None:
    print(f"❌ BLOCKING: {error_count} type errors found")
    print("\nFirst 5 errors:")
    error_lines = [line for line in output.split("\n") if " - error:" in line]
    for line in error_lines[:5]:
        print(f"   {line}")
    if error_count > 5:
        print(f"   ... and {error_count - 5} more errors")
    print("\n💡 Run: pyright")


def _run_blocking_gates(
    project_root: Path, venv_python: Path, results: _CheckResults,
) -> None:
    """Run the seven whole-tree blocking gates + the two warn-only gates into
    `results`. Split out of `main()` purely to keep its own branch count
    (and cyclomatic complexity) down -- no behavior change, same calls in
    the same order."""
    if _check_whole_tree_integration_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_W_INT_GATE.name)
    if _check_flow_probe_registry_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_FLOW_PROBE_REGISTRY_GATE.name)
    if _check_service_interface_ast_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_SI_AST_GATE.name)
    if _check_return_shape_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_RETURN_SHAPE_GATE.name)
    if _check_embedding_bound_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_EMBEDDING_BOUND_GATE.name)
    if _check_shipped_doc_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_SHIPPED_DOC_GATE.name)
    if _check_bundle_license_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_BUNDLE_LICENSE_GATE.name)
    if _check_schema_init_gate(project_root, venv_python, results):
        results.failed_blocking_gates.append(_SCHEMA_INIT_GATE.name)
    # W-INT Cycle 2 driver-import gate runs in WARN mode per master plan
    # §1.7 — emits findings but never blocks. Mode flip at W-WINT2-FINAL.
    _check_wint2_driver_import_gate(project_root, venv_python)
    # W-INT Cycle 2 vault-key-declaration gate (W-PLUGIN-LAUNCH-KEYS sub-1)
    # runs in WARN mode per brief §5.3 — emits findings but never blocks.
    # Mode flip at sub-2 W-VAULT-CALLER-ENFORCE in lockstep with the
    # runtime readiness gate.
    _check_wint2_warn_only_gate(
        project_root, venv_python, _W_WINT2_VAULT_KEY_GATE,
    )


def _run_pyright_checked(
    project_root: Path, venv_python: Path, results: _CheckResults,
) -> int | None:
    """Run pyright into `results`; return an exit code to bail `main()` with,
    or None to continue. Split out of `main()` to keep the fatal-vs-continue
    branch from adding to `main()`'s own cyclomatic complexity."""
    try:
        results.has_type_errors = _check_pyright(project_root, venv_python)
    except ToolUnavailableError as exc:
        # Same exit code as the venv-missing precondition failure -- a
        # checker that never ran is the same class of "cannot proceed as
        # configured," not a type-errors-found result.
        print(f"❌ FATAL: {exc}")
        return 4
    return None


def _run_ruff_checked(
    project_root: Path, venv_python: Path, results: _CheckResults,
) -> int | None:
    """Run ruff into `results`; same shape and reasoning as
    `_run_pyright_checked` -- kept as a separate, parallel function rather
    than a shared generic wrapper, matching this module's existing
    one-function-per-tool style."""
    try:
        results.has_lint_errors = _check_ruff(project_root, venv_python)
    except ToolUnavailableError as exc:
        print(f"❌ FATAL: {exc}")
        return 4
    return None


def _check_pyright(project_root: Path, venv_python: Path) -> bool:
    """Run pyright; return True if blocking type errors were found.

    Raises ToolUnavailableError if pyright's own module never resolved (the
    interpreter's own "No module named" failure) -- distinct from the
    remaining, genuinely ambiguous "ran but produced output this function
    can't parse" case, which stays the pre-existing non-blocking "status
    unclear" outcome. Do not collapse the two: one means the checker ran and
    said something odd, the other means it never checked anything.
    """
    venv_activate = venv_python.parent / "activate"
    print("\n📊 Type Checking with pyright (strict)...")
    pyright_cmd = f"cd {project_root} && source {venv_activate} && python3 -m pyright 2>&1"
    success, output = run_command(pyright_cmd, "Running pyright", timeout=120)

    error_count = 0
    if not success and output.strip():
        error_count = len([line for line in output.split("\n") if " - error:" in line])

    if error_count > 0:
        _report_pyright_errors(output, error_count)
        return True
    if success:
        print("✅ ZERO type errors - strict enforcement passed!")
        return False
    if _module_unavailable(output, "pyright"):
        raise ToolUnavailableError(
            f"pyright never ran -- its module could not be resolved: {output.strip()!r}"
        )
    print("⚠️ pyright check completed but status unclear")
    return False


def _check_ruff(project_root: Path, venv_python: Path) -> bool:
    """Run ruff; return True if lint errors were found.

    Raises ToolUnavailableError if ruff's own module never resolved — same
    shape and same reasoning as `_check_pyright`: a genuinely absent ruff
    must not read as "lint errors found" (the wrong noun attached to a true
    signal) any more than it should read as a silent pass.
    """
    print("\n📊 Linting with ruff...")
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        raise ToolUnavailableError(
            f"ruff cannot run correctly -- its --config target is missing: {pyproject}. "
            "This file's [tool.ruff] section is what activates import-sort (I001) and "
            "typing-modernization (UP035) rules; running ruff without it would silently "
            "diverge from this platform's own gate rather than failing loud, so this "
            "checks the config target before invoking ruff at all, not after. Pull an "
            "update that ships this file (born-clone-gate-toolchain fix).",
        )
    ruff_cmd = f"cd {project_root} && {venv_python} -m ruff check ananta/src ananta/tests plugins initialization/src quality_gates solet_cli/src solet_cli/tests bootstrap_adapter --config pyproject.toml 2>&1"
    success, output = run_command(ruff_cmd, "Running ruff linter")

    if success:
        print("✅ No lint errors found")
        return False

    if _module_unavailable(output, "ruff"):
        raise ToolUnavailableError(
            f"ruff never ran -- its module could not be resolved: {output.strip()!r}"
        )

    lint_lines = [line for line in output.split("\n") if line.strip()]
    if not lint_lines:
        return False
    print("❌ Lint errors found:")
    for line in lint_lines[:10]:
        print(f"   {line}")
    if len(lint_lines) > 10:
        print(f"   ... and {len(lint_lines) - 10} more")
    print("\n💡 Run: ruff check ananta/src ananta/tests plugins initialization/src quality_gates solet_cli/src solet_cli/tests bootstrap_adapter --config pyproject.toml --fix")
    return True


def _check_syntax(project_root: Path, venv_python: Path) -> list[str]:
    """Validate Python syntax across the tree; return list of error strings.

    Compiles under `venv_python` -- the same already-resolved validated
    interpreter every other checker in this module uses -- never a bare
    `python3` resolved through the caller's ambient PATH, which would make
    this verdict a property of the caller's shell rather than of the code.
    GTE-A4.
    """
    print("\n📊 Python Syntax Validation...")
    syntax_errors: list[str] = []
    for py_file in project_root.glob("**/*.py"):
        if ".venv" in str(py_file) or "venv" in str(py_file):
            continue
        result = subprocess.run(
            [str(venv_python), "-m", "py_compile", str(py_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            syntax_errors.append(f"{py_file}: {result.stderr}")

    if not syntax_errors:
        print("✅ Python syntax valid")
    else:
        print("❌ CRITICAL: Python syntax errors found!")
        for err in syntax_errors[:5]:
            print(f"   {err}")
    return syntax_errors


def _check_debuggers(project_root: Path) -> None:
    print("\n📊 Quick Issue Scan...")
    debugger_cmd = f"cd {project_root} && grep -r 'import pdb\\|pdb.set_trace\\|breakpoint()' ananta/src plugins --include='*.py' 2>/dev/null"
    success, output = run_command(debugger_cmd, "Checking for debugger statements")

    if success or not output.strip():
        print("✅ No debugger statements found")
    else:
        print("⚠️ Warning: Debugger statements found:")
        print(output[:200])


def _summary_exit_code(results: _CheckResults) -> int:
    """Pure mapping from results to exit code; see module docstring."""
    if results.crashed_gates:
        return 6
    if results.failed_blocking_gates:
        return 5
    if results.has_type_errors and results.has_lint_errors:
        return 3
    if results.has_type_errors:
        return 1
    if results.has_lint_errors:
        return 2
    return 0


def _print_summary(results: _CheckResults) -> int:
    print("\n" + "=" * 60)
    print("📊 CODE QUALITY CHECK SUMMARY")
    print("=" * 60)
    exit_code = _summary_exit_code(results)
    if exit_code == 0:
        print("✅ All code quality checks passed!")
        return 0
    if results.has_type_errors:
        print("❌ FAILED: Type errors found - fix before committing!")
    if results.has_lint_errors:
        print("❌ FAILED: Lint errors found - fix before committing!")
    if results.failed_blocking_gates:
        print(
            "❌ FAILED: Blocking gate violations "
            f"({', '.join(results.failed_blocking_gates)}) — fix before committing!"
        )
    if results.crashed_gates:
        names = ", ".join(name for name, _ in results.crashed_gates)
        print(
            f"🛑 GATE CRASH: no verdict from ({names}). This is NOT a violation "
            "count — these gates raised instead of measuring, so this run says "
            "NOTHING about the code they cover. Investigate the excerpt above; "
            "do not read it as complexity or structural debt."
        )
    return exit_code


def main() -> int:
    """Run code quality checks and report results."""
    project_root = Path(__file__).parent.parent

    print("=" * 60)
    print("🔍 CODE QUALITY CHECK")
    print("=" * 60)

    venv_python = _find_venv_python(project_root)
    if venv_python is None:
        print("❌ venv not found - run: python3 -m venv .venv")
        print("   Then: source .venv/bin/activate && pip install ruff pyright radon")
        return 4

    if not _check_gate_toolchain(project_root):
        return 4

    results = _CheckResults()
    pyright_bail = _run_pyright_checked(project_root, venv_python, results)
    if pyright_bail is not None:
        return pyright_bail
    ruff_bail = _run_ruff_checked(project_root, venv_python, results)
    if ruff_bail is not None:
        return ruff_bail
    results.failed_blocking_gates.extend(
        _check_coherence_gates(project_root, venv_python, results)
    )
    _run_blocking_gates(project_root, venv_python, results)

    if _check_syntax(project_root, venv_python):
        return 4

    _check_debuggers(project_root)

    if _run_fixture_replay_if_needed(project_root, venv_python):
        results.has_type_errors = True  # Block commit on fixture failures

    if _check_readable_freshness(project_root, venv_python):
        results.has_type_errors = True  # Block commit on stale readable files

    return _print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
