"""Hermetic smoke for the L1 deterministic scanners.

Exercises the pure-Python scanners (no external tool needed) on crafted fixtures
and asserts they emit the right F1 dimension/constraint — plus that a tool-absent
path records a coverage gap rather than silently passing. Run via the gate-smoke
runner (``quality_gates/run_smokes.py``) or directly with
``.venv/bin/python3 plugins/code_vetting_plugin/tests/l1_scanners_smoke.py``
(exit 0/1); the ``code_vetting_plugin`` package must be installed
(``pip install -e plugins/code_vetting_plugin``). Also runnable as pytest
``test_*`` functions.
"""

from __future__ import annotations

# ruff: noqa: E402
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin import toolrun
from code_vetting_plugin.models import Dimension
from code_vetting_plugin.scanners import deps, duplication, hidden_unicode, patterns, secrets
from code_vetting_plugin.targets import TargetTree
from code_vetting_plugin.toolrun import tool_available

# The fixture's hidden char is built from an escape so this source file carries no
# literal zero-width character (which the hidden-unicode scanner would rightly flag).
_ZERO_WIDTH_SPACE = chr(0x200B)
_DUP_BLOCK = "\n".join(f"    value_{i} = compute_step({i}, base, offset, scale)" for i in range(12))

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE -- exits 0 if every check ran (the
# common case, rg present), 77 if the battery ran but the two rg-dependent
# end-to-end halves below skipped their live half (structural half still
# ran and is asserted either way -- see each test's own comment), never
# silently 0 with the gap invisible to run_smokes.py's aggregate. Undeclared-
# dependency audit: workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SKIP_EXIT_CODE = 77
_rg_dependent_half_skipped = False


def _make_tree(root: Path) -> TargetTree:
    tracked: list[str] = []

    danger = root / "docs" / "danger.md"
    danger.parent.mkdir(parents=True, exist_ok=True)
    danger.write_text(f"visible{_ZERO_WIDTH_SPACE}hidden zero-width here\n", encoding="utf-8")
    tracked.append("docs/danger.md")

    manifest = root / "plugins" / "foo_plugin" / "pyproject.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('[project]\nname = "foo_plugin"\nversion = "0.1.0"\n', encoding="utf-8")
    tracked.append("plugins/foo_plugin/pyproject.toml")

    # A non-two-bucket SPDX with no blacklisted substring — must still be flagged
    # (the license gate is a pure allowlist, not a leaky substring denylist).
    bar = root / "plugins" / "bar_plugin" / "pyproject.toml"
    bar.parent.mkdir(parents=True, exist_ok=True)
    bar.write_text('[project]\nname = "bar_plugin"\nlicense = "MPL-2.0"\n', encoding="utf-8")
    tracked.append("plugins/bar_plugin/pyproject.toml")

    for name in ("alpha", "beta"):
        module = root / "plugins" / "foo_plugin" / "src" / "foo_plugin" / f"{name}.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(f"def {name}(base, offset, scale):\n{_DUP_BLOCK}\n    return value_0\n", encoding="utf-8")
        tracked.append(f"plugins/foo_plugin/src/foo_plugin/{name}.py")

    return TargetTree(root=root, tracked=tuple(sorted(tracked)))


def test_hidden_unicode_flags_zero_width() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = hidden_unicode.scan(_make_tree(Path(tmp)), "rid")
    hits = [f for f in result.findings if f.dimension is Dimension.HIDDEN_UNICODE]
    assert hits, "expected a hidden-unicode finding for the zero-width char"
    assert any(f.constraint_violated == "hidden_unicode:zero_width" for f in hits)


def test_license_flags_missing_spdx() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = deps.scan_licenses(_make_tree(Path(tmp)), "rid")
    assert any(
        f.dimension is Dimension.LICENSE and f.constraint_violated == "RB-LICENSE:missing-spdx"
        for f in result.findings
    ), "expected a missing-SPDX finding for the license-less plugin manifest"


def test_license_flags_non_two_bucket_spdx() -> None:
    # Regression guard: a non-two-bucket SPDX (MPL-2.0) with no blacklisted
    # substring must be flagged — the gate is an allowlist, not a denylist.
    with tempfile.TemporaryDirectory() as tmp:
        result = deps.scan_licenses(_make_tree(Path(tmp)), "rid")
    assert any(
        f.constraint_violated == "RB-LICENSE:non-two-bucket-spdx" for f in result.findings
    ), "expected MPL-2.0 to be flagged as a non-two-bucket SPDX"


def test_duplication_flags_exact_block() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = duplication.scan(_make_tree(Path(tmp)), "rid")
    assert any(f.dimension is Dimension.DUP for f in result.findings), "expected a duplicate-block finding"


def _never_available(_name: str) -> bool:
    return False


def test_absent_tool_records_coverage_gap() -> None:
    # Hermetic tool-absence: force the resolution seam to "not installed" instead of
    # asserting host state — installing trufflehog on the host must not red this smoke
    # (it did on 2026-07-21 when the R7 tool provisioning landed the binary).
    real_tool_available = secrets.tool_available
    secrets.tool_available = _never_available
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = secrets.scan_trufflehog(_make_tree(Path(tmp)), "rid")
    finally:
        secrets.tool_available = real_tool_available
    assert not result.coverage.ran
    assert result.coverage.gap_reason is not None
    assert not result.findings


def _write_hanging_tool(directory: Path, name: str) -> Path:
    tool = directory / name
    tool.write_text("#!/bin/sh\nsleep 1000\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def test_hung_scanner_tool_records_qualification_gap() -> None:
    """Red mutation: presence-only detection waits for the scanner's full 900s bound."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        name = "smoke-hanging-scanner"
        _write_hanging_tool(directory, name)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{directory}{os.pathsep}{old_path}"
        toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
        original = secrets._TRUFFLEHOG  # noqa: SLF001
        secrets._TRUFFLEHOG = name  # noqa: SLF001
        try:
            result = secrets.scan_trufflehog(_make_tree(directory / "tree"), "rid")
        finally:
            secrets._TRUFFLEHOG = original  # noqa: SLF001
            os.environ["PATH"] = old_path
            toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
    assert not result.coverage.ran
    assert result.coverage.gap_reason is not None
    assert "hung past 2s" in result.coverage.gap_reason
    assert name in result.coverage.gap_reason


def test_nonzero_version_probe_is_unusable() -> None:
    """A PATH hit that exits nonzero is disclosed as unusable, not treated as installed."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        name = "smoke-failing-version"
        tool = directory / name
        tool.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
        tool.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{directory}{os.pathsep}{old_path}"
        toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
        try:
            verdict = toolrun.qualify_tool(name)
        finally:
            os.environ["PATH"] = old_path
            toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
    assert not verdict.usable
    assert "exit 42" in verdict.reason


def test_quarantine_attribute_is_named_on_darwin() -> None:
    """A quarantined temporary fixture is diagnosed, never altered by qualification."""
    if platform.system() != "Darwin":
        print("SKIP quarantine fixture: com.apple.quarantine is macOS-only")
        return
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        name = "smoke-quarantined-tool"
        tool = _write_hanging_tool(directory, name)
        subprocess.run(
            ["xattr", "-w", "com.apple.quarantine", "0081;smoke", str(tool)], check=True, timeout=5
        )
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{directory}{os.pathsep}{old_path}"
        toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
        try:
            verdict = toolrun.qualify_tool(name)
        finally:
            os.environ["PATH"] = old_path
            subprocess.run(["xattr", "-d", "com.apple.quarantine", str(tool)], check=False, timeout=5)
            toolrun._TOOL_QUALIFICATIONS.clear()  # noqa: SLF001
    assert not verdict.usable
    assert "com.apple.quarantine is present" in verdict.reason
    assert f"xattr -d com.apple.quarantine {tool}" in verdict.reason


# --- operator-PII pattern is RUNTIME-DERIVED, never a hardcoded literal -------------
#
# RED-FIRST (2026-07-31). `scanners/patterns.py` ships in seeds, so a hardcoded operator
# name/email there is simultaneously a contamination guard and the exact leak the
# identity_leak dimension exists to catch. These checks prove the value is sourced from
# the RUNNING operator's git identity.
#
# ⚠ These assertions deliberately carry NO real identity token — not even as the thing
# they forbid. A test that spelled out a real name/email to assert its absence would
# re-introduce the leak into a test file, and tests now ship in seeds. So absence is
# asserted STRUCTURALLY (no operator_pii entry may be baked into the static tuple) and
# derivation is asserted with a SENTINEL, which also keeps the check machine-independent:
# it stays valid on a host whose git identity genuinely is the origin operator's.

_PII_CONSTRAINT = "identity:operator_pii"
_SENTINEL_NAME = "Smoke Sentinel Not A Real Operator"
_SENTINEL_EMAIL = "smoke.sentinel@example.invalid"


def test_operator_pii_is_not_baked_into_the_static_pattern_tuple() -> None:
    # Structural absence: if anyone re-hardcodes the PII pattern, it lands back in
    # _PATTERNS and this goes red — without this file naming a single real atom.
    baked = [p for p in patterns._PATTERNS if p.constraint == _PII_CONSTRAINT]  # noqa: SLF001
    assert not baked, f"operator PII must be runtime-composed, found baked pattern(s): {baked}"


def test_operator_pii_pattern_follows_the_running_git_identity() -> None:
    # A hardcoded literal would ignore the patch and fail to contain the sentinel.
    with patch.object(patterns, "_git_global_config", side_effect=[_SENTINEL_NAME, _SENTINEL_EMAIL]):
        composed = patterns._operator_pii_pattern()  # noqa: SLF001
    assert composed is not None, "a derivable git identity must produce a pattern"
    assert composed.constraint == _PII_CONSTRAINT
    assert re.escape(_SENTINEL_NAME) in composed.regex, composed.regex
    assert re.escape(_SENTINEL_EMAIL) in composed.regex, composed.regex


def test_operator_pii_atoms_are_regex_escaped() -> None:
    # An unescaped '.' would widen the pattern into a wildcard, so a derived identity
    # of "a.c" must NOT match the unrelated string "abc".
    with patch.object(patterns, "_git_global_config", side_effect=["a.c", ""]):
        composed = patterns._operator_pii_pattern()  # noqa: SLF001
    assert composed is not None
    assert re.search(composed.regex, "abc") is None, f"unescaped wildcard leaked: {composed.regex}"
    assert re.search(composed.regex, "a.c") is not None, composed.regex


def test_underivable_operator_identity_records_a_gap_not_an_empty_pattern() -> None:
    # Both atoms unset: the pattern must be OMITTED and the loss DISCLOSED. Interpolating
    # "" would produce an empty alternation branch that matches every file.
    #
    # The composition half is HERMETIC and always runs. The scan()-wiring half needs `rg` on
    # PATH, and ripgrep is one of the tools a born seed does not declare or install —
    # so it is guarded rather than allowed to red for a missing tool. Guarding is
    # safe here because the assertion that actually carries the fix (no empty pattern is ever
    # composed) is the unguarded one above it.
    global _rg_dependent_half_skipped
    with patch.object(patterns, "_git_global_config", return_value=""):
        assert patterns._operator_pii_pattern() is None  # noqa: SLF001
        if not tool_available("rg"):
            print("SKIP scan()-wiring half: rg not on PATH (composition half still asserted)")
            _rg_dependent_half_skipped = True
            return
        with tempfile.TemporaryDirectory() as tmp:
            result = patterns.scan(_make_tree(Path(tmp)), "rid")
    assert result.coverage.ran, "the rest of the battery still runs"
    assert result.coverage.gap_reason is not None, "the un-derivable PII pattern must be disclosed"
    assert _PII_CONSTRAINT in result.coverage.gap_reason
    assert not any(f.constraint_violated == _PII_CONSTRAINT for f in result.findings)


def test_operator_pii_finding_redacts_the_matched_value() -> None:
    """The detector must not reproduce the value it detects into its own report.

    RED-FIRST: the composed pattern was built without ``redact=True``, so ``_pattern_findings``
    took the ``else`` branch of ``shown = _redacted(...) if pattern.redact else first_match``
    and wrote the matched identity verbatim into ``evidence`` — which is persisted to
    ``vetting_runs.report`` and handed to third parties. A detector for "this value must not
    travel" was therefore the thing that made it travel.

    Asserting the FINDING FIRES is not enough to catch that; this asserts what the finding SAYS.
    The value used is the sentinel, never a real identity — tests ship.
    """
    # Structural half: hermetic, no `rg` needed, and it is the assertion that pins the fix.
    with patch.object(patterns, "_git_global_config", side_effect=[_SENTINEL_NAME, _SENTINEL_EMAIL]):
        composed = patterns._operator_pii_pattern()  # noqa: SLF001
    assert composed is not None
    assert composed.redact, "the operator-PII pattern MUST redact its match; every secret pattern does"

    # End-to-end half: prove the redaction actually reaches `evidence`. Guarded on `rg` for the
    # same reason as the gap test above — a born seed does not declare ripgrep.
    global _rg_dependent_half_skipped
    if not tool_available("rg"):
        print("SKIP evidence half: rg not on PATH (redact flag still asserted structurally)")
        _rg_dependent_half_skipped = True
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        leak = root / "docs" / "leak.md"
        leak.parent.mkdir(parents=True, exist_ok=True)
        leak.write_text(f"contact: {_SENTINEL_EMAIL}\n", encoding="utf-8")
        with patch.object(patterns, "_git_global_config", side_effect=[_SENTINEL_NAME, _SENTINEL_EMAIL]):
            result = patterns.scan(TargetTree.from_walk(root), "rid-redact")
    pii = [f for f in result.findings if f.constraint_violated == _PII_CONSTRAINT]
    assert pii, "the sentinel identity must be detected at all, or the test below proves nothing"
    for finding in pii:
        assert _SENTINEL_EMAIL not in finding.evidence, f"matched value leaked into evidence: {finding.evidence}"
        assert "<redacted" in finding.evidence, f"expected a redacted placeholder, got: {finding.evidence}"


def main() -> int:
    tests = [
        test_hidden_unicode_flags_zero_width,
        test_license_flags_missing_spdx,
        test_license_flags_non_two_bucket_spdx,
        test_duplication_flags_exact_block,
        test_absent_tool_records_coverage_gap,
        test_hung_scanner_tool_records_qualification_gap,
        test_nonzero_version_probe_is_unusable,
        test_quarantine_attribute_is_named_on_darwin,
        test_operator_pii_is_not_baked_into_the_static_pattern_tuple,
        test_operator_pii_pattern_follows_the_running_git_identity,
        test_operator_pii_atoms_are_regex_escaped,
        test_underivable_operator_identity_records_a_gap_not_an_empty_pattern,
        test_operator_pii_finding_redacts_the_matched_value,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} L1 scanner smoke checks passed.")
    if _rg_dependent_half_skipped:
        print(
            "SKIP: rg not on PATH -- the two end-to-end scan()-wiring halves above "
            "disclosed a gap rather than running; every other check ran and passed."
        )
        return _SKIP_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
