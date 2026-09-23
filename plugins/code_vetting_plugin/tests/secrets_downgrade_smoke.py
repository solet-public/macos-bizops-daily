"""Red-first smoke for the gitleaks low-confidence-rule path-class downgrade (suite v1.1).

Proves the moat stays honest after the ``generic-api-key`` zero-FP over-promotion
fix: a low-confidence catch-all hit on a known-safe path (test fixture, archived
doc, generated composition artifact) is DOWNGRADED to advisory and does not
render as a shown finding, while (a) the same rule on real source stays HIGH and
shown, and (b) a specific-provider rule (github-pat) stays HIGH even on a fixture
path. Pure-Python — no gitleaks binary needed. Run via the gate-smoke runner
(``quality_gates/run_smokes.py``) or directly with ``.venv/bin/python3
plugins/code_vetting_plugin/tests/secrets_downgrade_smoke.py`` (exit 0/1); the
``code_vetting_plugin`` package must be installed. Also runnable as pytest
``test_*`` functions.
"""

from __future__ import annotations

import sys

# ruff: noqa: E402
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
)
from code_vetting_plugin.report import DEFAULT_ZERO_FP_DIMENSIONS, is_zero_fp_promoted
from code_vetting_plugin.scanners.secrets import (
    _build_gitleaks_finding,
    _known_safe_class,
    _SafePathClass,
)

# The relativization snapshot root is irrelevant for relative File paths (they pass
# through unchanged), so any path serves — the entries below carry repo-relative Files.
_SNAP = Path("/tmp/vetting_snap_fixture")


def _entry(file: str, rule: str) -> dict[str, object]:
    """A minimal gitleaks report entry for one hit."""
    return {"RuleID": rule, "Description": "detected key", "Match": "REDACTED", "File": file, "StartLine": 3}


def _finding(file: str, rule: str) -> Finding:
    built = _build_gitleaks_finding(_entry(file, rule), _SNAP, "rid", "8.30.1")
    assert built is not None, f"expected a finding for {rule} @ {file}"
    return built


def test_known_safe_class_maps_the_three_path_classes() -> None:
    assert _known_safe_class("plugins/aws_midwife_plugin/tests/vpc_endpoints_smoke.py") is _SafePathClass.TEST
    assert _known_safe_class("a/b/tests/fixtures/x.json") is _SafePathClass.TEST
    assert _known_safe_class("knowledge_bases/.archive/old_design.md") is _SafePathClass.ARCHIVE
    assert _known_safe_class("workbench/archive/2026-06-14_secretgate_design.md") is _SafePathClass.ARCHIVE
    assert (
        _known_safe_class("knowledge_bases/compositions/ns06/prompts/step162_readable.txt")
        is _SafePathClass.COMPOSITION_ARTIFACT
    )
    # Real source is NOT a safe class — a secret there must still fire.
    assert _known_safe_class("ananta/src/ananta/services/state_service/client.py") is None
    assert _known_safe_class("plugins/foo_plugin/src/foo_plugin/client.py") is None


def test_generic_api_key_on_fixture_path_downgraded() -> None:
    finding = _finding("plugins/foo_plugin/tests/x_smoke.py", "generic-api-key")
    assert finding.severity is Severity.ADVISORY, "generic-api-key on a test path must downgrade to advisory"
    assert "downgraded" in finding.evidence.lower()
    assert _SafePathClass.TEST.value in finding.evidence


def test_generic_api_key_on_real_source_stays_high() -> None:
    finding = _finding("plugins/foo_plugin/src/foo_plugin/client.py", "generic-api-key")
    assert finding.severity is Severity.HIGH, "generic-api-key on real source must stay HIGH — the moat"
    assert "downgraded" not in finding.evidence.lower()


def test_generic_api_key_on_archive_and_composition_downgraded() -> None:
    for path in (
        "knowledge_bases/.archive/2026-01-02_claude_model_context_management_design_v2.md",
        "knowledge_bases/compositions/ns06/prompts/example_flow_step162_readable.txt",
    ):
        finding = _finding(path, "generic-api-key")
        assert finding.severity is Severity.ADVISORY, f"generic-api-key on {path} must downgrade"


def test_specific_rule_on_fixture_path_not_downgraded() -> None:
    # github-pat is a specific high-confidence rule — a real leaked key in a fixture
    # must STILL fire, so the path-class downgrade never touches it.
    finding = _finding("plugins/github_midwife_plugin/tests/seed_content_validator_smoke.py", "github-pat")
    assert finding.severity is Severity.HIGH, "a specific-provider rule must stay HIGH even on a test path"
    assert "downgraded" not in finding.evidence.lower()


def _secret(severity: Severity) -> Finding:
    return Finding.build(
        run_id="rid",
        layer=Layer.L1_DETERMINISTIC,
        dimension=Dimension.SECRETS,
        severity=severity,
        file="plugins/foo_plugin/tests/x_smoke.py",
        line=3,
        constraint_violated="gitleaks:generic-api-key",
        evidence="detected key (redacted match: REDACTED)",
        provenance=Provenance(source="gitleaks", tool_version="8.30.1", rule_id="generic-api-key"),
        context_profile=ContextProfile.PRODUCTION,
    )


def test_downgraded_secret_not_promoted_but_high_secret_is() -> None:
    advisory = _secret(Severity.ADVISORY)
    high = _secret(Severity.HIGH)
    assert not is_zero_fp_promoted(advisory, DEFAULT_ZERO_FP_DIMENSIONS), "advisory (downgraded) secret must NOT promote"
    assert is_zero_fp_promoted(high, DEFAULT_ZERO_FP_DIMENSIONS), "a HIGH secret must still zero-FP promote"


def main() -> int:
    tests = [
        test_known_safe_class_maps_the_three_path_classes,
        test_generic_api_key_on_fixture_path_downgraded,
        test_generic_api_key_on_real_source_stays_high,
        test_generic_api_key_on_archive_and_composition_downgraded,
        test_specific_rule_on_fixture_path_not_downgraded,
        test_downgraded_secret_not_promoted_but_high_secret_is,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} secrets-downgrade smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
