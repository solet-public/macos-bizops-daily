"""render_sample.py — Stream-O Wave-1 sample renderer (synthetic layers).

Renders a representative report + ``vetting_runs`` row from the Wave-1 synthetic
layers, against the platform tree identity + real tracked-debt totals, and proves
bounded retention. The real deterministic end-to-end run lives in ``pipeline.py``;
this stays as the shape demonstration with a deterministic (fixed-clock) sample.
Run from the repo root:

    .venv/bin/python -m code_vetting_plugin.render_sample
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping

from .driver import Clock, VettingDriver, VettingResult
from .metrics import MetricsWriter
from .models import ContextProfile
from .report import ReportRenderer
from .run_context import allowlist_totals, git_head, repo_root
from .run_record import AllowlistDelta, RunTarget, build_run_metrics
from .samples import InMemoryStateWriter, SampleL1Scanner, SampleL3Verifier, sample_critics

_WORKBENCH = repo_root() / "workbench"
_REPORT_PATH = _WORKBENCH / "2026-07-19_vetting_stream_o_sample_report.md"
_ROW_PATH = _WORKBENCH / "2026-07-19_vetting_stream_o_sample_run_row.json"
_NAMESPACE = "vetting_runs"

_PREAMBLE = (
    "> **SAMPLE — Stream-O skeleton (Wave 1).** Ran end to end against the platform tree identity "
    "and real tracked-debt totals; the *findings* below are synthetic stand-ins (`example_*` "
    "paths) — this exercises the report + metrics **shapes**. The real deterministic end-to-end "
    "run (L1's 17 scanners → heuristic L3) is `pipeline.py`."
)


def _fixed_clock(timestamps: tuple[str, str]) -> Clock:
    """A deterministic two-tick clock (started, finished) for a reproducible sample."""
    iterator: Iterator[str] = iter(timestamps)
    return lambda: next(iterator)


async def _demo_retention() -> str:
    """Persist more runs than the bound and assert the trail pruned to it."""
    retention = 3
    demo_state = InMemoryStateWriter()
    writer = MetricsWriter(state=demo_state, retention=retention)
    target = RunTarget(repo="platform", ref="demo", scope="retention demo")
    for index in range(5):
        metrics = build_run_metrics(
            run_id=f"vr-retn-{index}",
            target=target,
            started=f"2026-07-19T20:0{index}:00+00:00",
            finished=f"2026-07-19T20:0{index}:30+00:00",
            substrate="heuristic",
            layers_run=[],
            findings=[],
            coverage=[],
            allowlist_delta=AllowlistDelta(totals={}),
        )
        await writer.persist(metrics)
    kept = demo_state.count(_NAMESPACE)
    if kept != retention:
        raise AssertionError(f"retention bound broken: kept {kept}, expected {retention}")
    return f"persisted 5 runs at retention={retention} → kept {kept} (oldest {5 - kept} pruned)"


def _print_summary(result: VettingResult, row: Mapping[str, object], retention_summary: str) -> None:
    print(f"report            → {_REPORT_PATH}")
    print(f"vetting_runs row  → {_ROW_PATH}")
    print(f"findings emitted  : {len(result.findings)}")
    print(f"survival_rate     : {row['survival_rate']}")
    print(f"counts_by_severity: {row['counts_by_severity']}")
    print(f"allowlist totals  : {row['allowlist_delta']}")
    print(f"retention demo    : {retention_summary}")


async def main() -> None:
    """Render the sample report + metrics row and prove bounded retention."""
    state = InMemoryStateWriter()
    driver = VettingDriver(
        l1=SampleL1Scanner(),
        l2_critics=sample_critics(),
        l3=SampleL3Verifier(),
        renderer=ReportRenderer(),
        metrics_writer=MetricsWriter(state=state),
        clock=_fixed_clock(("2026-07-19T21:15:00+00:00", "2026-07-19T21:15:04+00:00")),
        context_profile=ContextProfile.PRODUCTION,
    )
    root = repo_root()
    ref = git_head(root)
    run_id = f"vr-sample-{ref}"
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

    retention_summary = await _demo_retention()
    _print_summary(result, row, retention_summary)


if __name__ == "__main__":
    asyncio.run(main())
