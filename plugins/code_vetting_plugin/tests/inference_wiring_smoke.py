"""inference_wiring_smoke.py — W3-B B3c: the inference SEAM is TESTED, not dead code.

The B3c seam is a STAGED plugin-layer primitive (the live LOCAL/SUBSCRIPTION caller is
W3-C's joseki), so Dusk's ruling requires it be driven end-to-end here — a tested seam is
not dead code. This smoke exercises:

  * DRIVER END-TO-END (local): a VettingDriver whose L3 is the substrate-selected inference
    verifier (RecordedTransport replies) stamps a routed candidate and records the substrate
    on the metrics row — L1(stub) -> substrate-selected L3 -> report -> metrics all flow.
  * RIDER-1 off-machine redaction: an OFF_MACHINE transport => off_operator=True => the
    forwarded skeptic prompt withholds a SENSITIVE finding's raw evidence but keeps its locus.
  * PRIVACY hard-refuse: PRIVACY bound to an off-machine transport raises (code must not leave).
  * assemble_inference_driver records the substrate + wires the L3 adapter.
  * the PLUGIN seam factories build the transports + the inference driver over a fake
    inference_service, with vacant-service fail-loud.

Hermetic: no live inference, no subprocess, no network — RecordedTransport / a fake infer_fn /
a capturing test-double transport. Run directly or via run_smokes.py.
"""

from __future__ import annotations

# ruff: noqa: E402
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_vetting_plugin.driver import L1Output, VettingDriver
from code_vetting_plugin.inference_wiring import (
    InferenceCompleter,
    assemble_inference_driver,
    build_substrate_verifier,
    make_local_infer_fn,
)
from code_vetting_plugin.l3_adapter import VerifierL3Adapter
from code_vetting_plugin.metrics import MetricsWriter
from code_vetting_plugin.models import (
    ContextProfile,
    Dimension,
    Finding,
    Layer,
    Provenance,
    Severity,
    Verdict,
)
from code_vetting_plugin.plugin import CodeVettingPlugin
from code_vetting_plugin.report import ReportRenderer
from code_vetting_plugin.run_context import repo_root
from code_vetting_plugin.run_record import AllowlistDelta, CoverageRecord, RunTarget
from code_vetting_plugin.samples import InMemoryStateWriter
from code_vetting_plugin.verify.inference import (
    RecordedTransport,
    TransportLocality,
    reply_key,
)
from code_vetting_plugin.verify.rulebook import load_rulebook
from code_vetting_plugin.verify.substrate import Substrate, select_substrate
from code_vetting_plugin.verify.verifier import VerificationPolicy

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _candidate(*, dimension: Dimension, evidence: str, constraint: str) -> Finding:
    """A non-zero-FP L2 candidate (routed to L3, never promoted)."""
    return Finding.build(
        run_id="vr-seam",
        layer=Layer.L2_CRITIC,
        dimension=dimension,
        severity=Severity.HIGH,
        file="pkg/mod.py",
        line=7,
        constraint_violated=constraint,
        evidence=evidence,
        provenance=Provenance(source="critic:test"),
        context_profile=ContextProfile.PRODUCTION,
    )


def _replies_for(finding: Finding, vote: str) -> dict[str, str]:
    """One recorded reply per policy lens for ``finding`` (keyed by reply_key)."""
    return {reply_key(finding.finding_id, lens): f"vote: {vote}\nrationale: seam-smoke" for lens in VerificationPolicy().lenses}


@dataclass(frozen=True, slots=True)
class _FakeL1:
    """A stub L1 (implements the driver's ``L1Scanner`` Protocol) — one candidate, no tools."""

    candidate: Finding

    async def scan(self, run_id: str, target: RunTarget) -> L1Output:
        del run_id, target
        return L1Output(
            findings=[self.candidate],
            coverage=[CoverageRecord(scanner="fake_l1", ran=True, files_examined=1)],
        )


@dataclass
class _CapturingOffMachineTransport:
    """A SkepticTransport test-double that declares OFF_MACHINE and captures each prompt."""

    prompts: list[str]

    @property
    def locality(self) -> TransportLocality:
        return TransportLocality.OFF_MACHINE

    def infer(self, request: object) -> str:
        self.prompts.append(request.prompt)  # type: ignore[attr-defined]
        return "vote: UPHOLD\nrationale: ok"


class _FakeInferenceService:
    """A minimal inference service satisfying the InferenceCompleter Protocol."""

    def generate_completion(self, request: object) -> object:
        del request
        return {"action_status": "completed", "data": {"completion": "vote: UPHOLD\nrationale: fake"}}


class _FakeOrchestrator:
    """Serves APP_HOME + get_service('inference_service') for the plugin seam test."""

    def __init__(self, app_home: Path, inference_service: object | None) -> None:
        self.APP_HOME = str(app_home)
        self._inference_service = inference_service

    def get_service(self, service_name: str) -> object | None:
        return self._inference_service if service_name == "inference_service" else None


def _check_driver_end_to_end_local() -> None:
    candidate = _candidate(dimension=Dimension.CORRECTNESS, evidence="off-by-one at the boundary", constraint="critic:logic")
    verifier = build_substrate_verifier(
        Substrate.LOCAL,
        subscription_transport=None,
        local_transport=RecordedTransport(_replies_for(candidate, "UPHOLD")),
        rulebook=load_rulebook(),
    )
    _check("LOCAL substrate builds a VerifierL3Adapter", isinstance(verifier, VerifierL3Adapter), str(type(verifier)))
    state = InMemoryStateWriter()
    driver = VettingDriver(
        l1=_FakeL1(candidate),
        l2_critics=(),
        l3=verifier,
        renderer=ReportRenderer(),
        metrics_writer=MetricsWriter(state=state),
        clock=lambda: "2026-07-20T00:00:00+00:00",
        context_profile=ContextProfile.PRODUCTION,
        substrate=Substrate.LOCAL.value,
    )
    result = asyncio.run(
        driver.run(run_id="vr-seam", target=RunTarget(repo="t", ref="deadbeef", scope="s"), allowlist_delta=AllowlistDelta(totals={}))
    )
    _check("end-to-end: metrics record the substrate", result.metrics.substrate == "local", result.metrics.substrate)
    stamped = [f for f in result.findings if f.finding_id == candidate.finding_id]
    _check("end-to-end: the routed candidate was L3-verified", len(stamped) == 1, str(result.findings))
    _check("end-to-end: all-UPHOLD -> CONFIRMED", stamped and stamped[0].verdict is Verdict.CONFIRMED, str(stamped))
    _check("end-to-end: the report renders", "## Findings" in result.report and "## Scanner Coverage" in result.report, result.report[:200])


def _check_offmachine_redaction() -> None:
    sensitive = _candidate(dimension=Dimension.SECURITY, evidence="RAWSECRET_AKIA_LEAKED_LINE", constraint="bandit:B105")
    transport = _CapturingOffMachineTransport(prompts=[])
    verifier = build_substrate_verifier(
        Substrate.SUBSCRIPTION,
        subscription_transport=transport,
        local_transport=None,
        rulebook=load_rulebook(),
    )
    asyncio.run(verifier.verify([sensitive]))
    _check("off-machine transport produced skeptic prompts", len(transport.prompts) >= 1, str(len(transport.prompts)))
    joined = "\n".join(transport.prompts)
    _check("RIDER-1: raw sensitive evidence is WITHHELD off-machine", "RAWSECRET_AKIA_LEAKED_LINE" not in joined, joined[:300])
    _check("RIDER-1: the locus (file:line + rule) is PRESERVED off-machine", "pkg/mod.py" in joined and "bandit:B105" in joined, joined[:300])


def _check_privacy_refuses_offmachine() -> None:
    _CHECKS_RUN.append("PRIVACY hard-refuses an off-machine transport")
    try:
        select_substrate(Substrate.PRIVACY, local_transport=_CapturingOffMachineTransport(prompts=[]))
    except ValueError:
        return
    raise SmokeFailureError("PRIVACY hard-refuses an off-machine transport: expected ValueError, none raised")


def _check_assemble_records_substrate() -> None:
    candidate = _candidate(dimension=Dimension.CORRECTNESS, evidence="e", constraint="critic:logic")
    driver = assemble_inference_driver(
        root=repo_root(),
        substrate=Substrate.SUBSCRIPTION,
        subscription_transport=RecordedTransport(_replies_for(candidate, "REFUTE")),
        local_transport=None,
        rulebook=load_rulebook(),
        metrics_writer=MetricsWriter(state=InMemoryStateWriter()),
        clock=lambda: "t",
        context_profile=ContextProfile.PRODUCTION,
    )
    _check("assemble records substrate on the driver", driver.substrate == "subscription", driver.substrate)
    _check("assemble wires the L3 adapter", isinstance(driver.l3, VerifierL3Adapter), str(type(driver.l3)))
    _check("assemble keeps L2 critics empty (agent-orchestrated, W3-C)", driver.l2_critics == (), str(driver.l2_critics))


def _check_plugin_seam() -> None:
    worktree = repo_root()
    plugin = CodeVettingPlugin()
    plugin._worktree_root = worktree  # noqa: SLF001 — inject the anchored root for the hermetic seam check
    plugin.orchestrator_ref = _FakeOrchestrator(worktree / "profile", _FakeInferenceService())  # type: ignore[assignment]

    _check("fake inference service satisfies InferenceCompleter", isinstance(_FakeInferenceService(), InferenceCompleter), "protocol")
    local_transport = plugin.build_local_skeptic_transport()
    _check("plugin builds the ON-MACHINE local transport", local_transport.locality is TransportLocality.ON_MACHINE, str(local_transport.locality))
    _check("local transport's infer_fn round-trips a completion", make_local_infer_fn(_FakeInferenceService())("p").startswith("vote:"), "infer_fn")
    sub_transport = plugin.build_subscription_skeptic_transport()
    _check("plugin builds the OFF-MACHINE subscription transport", sub_transport.locality is TransportLocality.OFF_MACHINE, str(sub_transport.locality))

    driver = plugin.build_inference_driver(Substrate.LOCAL, metrics_writer=MetricsWriter(state=InMemoryStateWriter()))
    _check("plugin build_inference_driver records the local substrate", driver.substrate == "local", driver.substrate)
    _check("plugin driver loads the assembled in-package rulebook (W3-C; B3a worktree-anchor retired)", driver.renderer is not None and load_rulebook().directives != {}, "assembled rulebook")

    # Vacant inference_service fails loud on a LOCAL substrate (never silently no-L3).
    _CHECKS_RUN.append("vacant inference_service fails loud on LOCAL")
    vacant = CodeVettingPlugin()
    vacant._worktree_root = worktree  # noqa: SLF001
    vacant.orchestrator_ref = _FakeOrchestrator(worktree / "profile", None)  # type: ignore[assignment]
    try:
        vacant.build_local_skeptic_transport()
    except RuntimeError:
        pass
    else:
        raise SmokeFailureError("vacant inference_service fails loud on LOCAL: expected RuntimeError, none raised")


def main() -> int:
    try:
        _check_driver_end_to_end_local()
        _check_offmachine_redaction()
        _check_privacy_refuses_offmachine()
        _check_assemble_records_substrate()
        _check_plugin_seam()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1
    print(f"inference_wiring_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
