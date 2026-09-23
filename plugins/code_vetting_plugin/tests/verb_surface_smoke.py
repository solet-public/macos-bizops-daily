"""Hermetic smoke for the code_vetting_plugin verb surface (W3-A A2).

Pins the invariants that are NOT exercisable by running the real scanners (those
shell out to pyright/gitleaks/… and take minutes): the EDGE decorated<->declared
parity (the #1 startup fatal — a missing get_edge_process_definitions entry ->
process_registry.edge_process_mismatch), the mandatory result-envelope shape, the
deploy-invariant worktree anchoring (APP_HOME walk, fail-loud off the worktree),
and the bounded-report cap. Pure Python, no live solet / LM Studio / subprocess /
mocks. Run via the gate-smoke runner (``quality_gates/run_smokes.py``) or directly
with ``.venv/bin/python3 plugins/code_vetting_plugin/tests/verb_surface_smoke.py``
(exit 0/1); the ``code_vetting_plugin`` package must be installed (``pip install
-e``). Also runnable as pytest ``test_*`` functions.
"""

from __future__ import annotations

import sys

# ruff: noqa: E402
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ananta.core.domain.enums import ProcessorPolicyCategory
from code_vetting_plugin.plugin import (
    _REPORT_CHAR_CAP,
    CodeVettingPlugin,
    _bound_report,
    locate_worktree_root,
)
from code_vetting_plugin.scanners.platform_gates import _gate_paths

_EXPECTED_EDGE_VERBS = {"vet_codebase", "scan_quality_guidelines", "get_vetting_run"}


def _decorated_edge_verbs() -> set[str]:
    """The methods actually decorated ``@platform_process`` EDGE — the registry's own view."""
    verbs: set[str] = set()
    for name in dir(CodeVettingPlugin):
        attr = getattr(CodeVettingPlugin, name)
        metadata = getattr(attr, "_platform_process_metadata", None)
        if metadata is not None and metadata.processor_policy_category is ProcessorPolicyCategory.EDGE:
            verbs.add(name)
    return verbs


def test_edge_defs_match_decorated_edge_verbs() -> None:
    plugin = CodeVettingPlugin()
    declared = set(plugin.get_edge_process_definitions().keys())
    decorated = _decorated_edge_verbs()
    assert decorated == _EXPECTED_EDGE_VERBS, f"decorated EDGE verbs drifted: {decorated}"
    # Exact parity — a declared entry with no decorated method (or vice versa) is the
    # edge_process_mismatch FATAL; this is the red-first guard against that regression.
    assert declared == decorated, f"get_edge_process_definitions {declared} != decorated {decorated}"
    for name, definition in plugin.get_edge_process_definitions().items():
        assert definition.name == name, f"EdgeProcessDefinition.name {definition.name!r} != key {name!r}"


def test_envelope_shape() -> None:
    payload = {"run_id": "vr-l1-abc-x", "total_findings": 0}
    envelope = CodeVettingPlugin._envelope(payload)
    assert set(envelope) == {"action_status", "data", "actions", "error", "timestamp"}
    assert envelope["data"] is payload
    assert envelope["action_status"] == "completed"
    assert envelope["actions"] == []
    assert envelope["error"] is None
    assert isinstance(envelope["timestamp"], str) and envelope["timestamp"]


def test_locate_worktree_root_finds_and_fails_loud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp)
        (worktree / "quality_gates").mkdir()
        (worktree / ".git").mkdir()
        app_home = worktree / "profile"
        app_home.mkdir()
        # Walks UP from app_home (<worktree>/profile) to the worktree carrying both markers.
        assert locate_worktree_root(app_home) == worktree.resolve()

    with tempfile.TemporaryDirectory() as bare:
        # No ancestor carries both quality_gates/ and .git -> fail-loud (cloud has no worktree).
        try:
            locate_worktree_root(Path(bare))
        except RuntimeError:
            pass
        else:  # pragma: no cover - the assert below is the failure signal
            raise AssertionError("locate_worktree_root must fail loud when no worktree marker is found")


def test_bound_report_caps_with_marker() -> None:
    short = "# tiny report\n"
    assert _bound_report(short) == short
    huge = "x" * (_REPORT_CHAR_CAP + 5000)
    bounded = _bound_report(huge)
    assert bounded.startswith("x" * 100)
    assert "truncated" in bounded
    assert len(bounded) < len(huge)


def test_gate_paths_anchored_on_scanned_root_not_file() -> None:
    # DEPLOY-INVARIANT guard (A2.1): the gate-script + venv paths MUST resolve UNDER the
    # SCANNED tree root (the worktree the plugin locates via APP_HOME), NOT under
    # __file__/repo_root. A materialized blue-green release copy ships no top-level
    # quality_gates/, so a __file__ anchor gaps out the code_quality + sql_access gates on
    # the deployed color (observed live 2026-07-20 — the defect this guard prevents).
    # RED-FIRST: reverting _gate_paths to a repo_root()/__file__ constant makes the
    # `root in parents` asserts fail. This is the build-time guard that turns 'caught in
    # live verify' into 'caught here'.
    root = Path("/nonexistent/worktree-xyz")
    paths = _gate_paths(root)
    for path in (paths.aggregate, paths.sql_gate, paths.sql_allowlist, paths.venv_python):
        assert root in path.parents, f"gate path {path} not anchored under the scanned root {root}"
    assert paths.aggregate == root / "quality_gates" / "code_quality_check.py"
    assert paths.sql_gate == root / "quality_gates" / "sql_access_gate.py"
    assert paths.sql_allowlist == root / "quality_gates" / "sql_access_allowlist.txt"
    assert paths.venv_python == root / ".venv" / "bin" / "python3"
    # The anchor is the ARGUMENT, not a captured module constant: a different root yields
    # different paths (a __file__ constant would return the same paths regardless of arg).
    assert _gate_paths(Path("/another/root")).aggregate != paths.aggregate


def main() -> int:
    tests = [
        test_edge_defs_match_decorated_edge_verbs,
        test_envelope_shape,
        test_locate_worktree_root_finds_and_fails_loud,
        test_bound_report_caps_with_marker,
        test_gate_paths_anchored_on_scanned_root_not_file,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} code_vetting_plugin verb-surface smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
