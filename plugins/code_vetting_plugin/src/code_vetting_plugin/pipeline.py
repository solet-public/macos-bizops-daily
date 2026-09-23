"""pipeline.py — the wired deterministic end-to-end vetting run (Wave 1).

Assembles the real pipeline behind the Stream-O driver Protocols and runs it once
against the platform working tree, producing ONE report + ONE ``vetting_runs`` metrics
row:

  L1  L1DeterministicScanner        — the 15 real deterministic scanners
  L2  (none)                        — inference AI critics are W3 (agent-orchestrated)
  L3  AdversarialVerifier + HeuristicSkepticDispatcher
                                    — the deterministic DO-NOT-FLAG / RB-SCOPE refute
                                      pre-screen over the non-zero-FP candidates
  O   ReportRenderer + MetricsWriter — this stream

Per Coordinator-Dusk's W2 ruling, the full inference pipeline (L2 AI critics +
automated L3 adversarial-verify over a whole tree) is W3 crystallization — inference
dispatch is inherently out-of-process. The inference dispatcher drops into the same
``VerifierL3Adapter`` seam unchanged when W3 automates it. Live-state persistence
(``service_interface::state_service``) is the other documented follow-on; a standalone
process reaches neither, so the metrics row is written to workbench as JSON. Run from
the repo root:

    .venv/bin/python -m code_vetting_plugin.pipeline
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

from .driver import VettingDriver, VettingResult
from .l1_scanner import L1DeterministicScanner
from .l3_adapter import VerifierL3Adapter
from .metrics import MetricsWriter
from .models import ContextProfile
from .report import ReportRenderer
from .run_context import allowlist_totals, git_head, repo_root, system_clock
from .run_record import AllowlistDelta, RunTarget
from .samples import InMemoryStateWriter
from .verify.dispatch import HeuristicSkepticDispatcher
from .verify.rulebook import load_rulebook
from .verify.verifier import AdversarialVerifier

_WORKBENCH = repo_root() / "workbench"
_REPORT_PATH = _WORKBENCH / "2026-07-19_vetting_w2_end_to_end_report.md"
_ROW_PATH = _WORKBENCH / "2026-07-19_vetting_w2_end_to_end_run_row.json"
_NAMESPACE = "vetting_runs"

_PREAMBLE = (
    "> **W2 end-to-end (deterministic substrate).** L1 = the 15 real deterministic scanners on "
    "this working tree; L3 = the AdversarialVerifier with the heuristic DO-NOT-FLAG/RB-SCOPE "
    "refute pre-screen over the non-zero-FP candidates. Per Dusk's W2 ruling the inference L2 "
    "critics + automated L3 verify are W3 (agent-orchestrated); they drop into the same driver "
    "Protocols unchanged. Metrics row is also written to workbench JSON — a standalone process "
    "can't reach the live state interface (the state-adapter follow-on)."
)


def _build_driver(root: Path, state: InMemoryStateWriter) -> VettingDriver:
    """Assemble the deterministic pipeline behind the driver Protocols."""
    rulebook = load_rulebook()
    verifier = AdversarialVerifier(HeuristicSkepticDispatcher(rulebook), rulebook)
    return VettingDriver(
        l1=L1DeterministicScanner(root=root),
        l2_critics=(),
        l3=VerifierL3Adapter(verifier),
        renderer=ReportRenderer(),
        metrics_writer=MetricsWriter(state=state),
        clock=system_clock,
        context_profile=ContextProfile.PRODUCTION,
    )


def _print_summary(result: VettingResult, row: Mapping[str, object]) -> None:
    print(f"report            → {_REPORT_PATH}")
    print(f"vetting_runs row  → {_ROW_PATH}")
    print(f"findings emitted  : {len(result.findings)}")
    print(f"survival_rate     : {row['survival_rate']}")
    print(f"counts_by_severity: {row['counts_by_severity']}")
    print(f"coverage_gaps     : {row['coverage_gaps']}")


async def main() -> None:
    """Run the deterministic end-to-end pass and write the report + metrics row."""
    root = repo_root()
    state = InMemoryStateWriter()
    driver = _build_driver(root, state)
    ref = git_head(root)
    run_id = f"vr-{ref}"
    target = RunTarget(repo="platform", ref=ref, scope="whole tracked tree (self-vet)")
    result = await driver.run(
        run_id=run_id,
        target=target,
        allowlist_delta=AllowlistDelta(totals=allowlist_totals(root)),
        preamble=_PREAMBLE,
    )

    _REPORT_PATH.write_text(result.report, encoding="utf-8")
    row = state.rows[_NAMESPACE][run_id]
    _ROW_PATH.write_text(json.dumps(row, indent=2, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    _print_summary(result, row)


if __name__ == "__main__":
    asyncio.run(main())
