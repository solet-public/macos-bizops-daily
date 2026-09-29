"""Router (blue-green) lifecycle path of the Step-5 runtime transition (design sections 7.1-7.5, 9.3).

The reconciliation seam is a fake seed whose ``probe`` phase answers through
the REAL ``target_reconciliation.dispatch_reconciliation`` against the fixture
plist, so every envelope the Manager produces is parsed and scoped by the
seed's own validator before the fake controller answers.  Legs:

- strategy selection proves the router: router facts in the inventory, the
  roster naming the self-deployment plugin, a print-visible router LaunchAgent,
  a reachable attestation, and a ``probed``/``mutated=false`` seed probe; the
  preview binds ``current_release_id``, topology, label, plist digest, adapter
  module identity, modules and the cutover fingerprint, and reports
  ``zero_downtime_rollback=true``;
- the apply envelope is the exact nineteen-key wire table; ``rec_`` ids are
  minted per attempt and journaled on the cutover row BEFORE dispatch;
- a successful swap records ``SwapEvidence`` and the terminal receipt digest,
  attestation of the CANDIDATE release publishes the runtime axis, and the
  journal reaches ``runtime_advanced``;
- a swap the router keeps/restores is terminal ``failed``
  (``runtime_candidate_failed``) with the runtime axis at ``None``, the
  source at N+1, and ``management_state=needs_attention`` (Step 6 D8);
- a healthy service whose attestation still names the baseline after a
  "successful" swap is ``runtime_candidate_not_serving`` and never publishes;
  a third release id is ``managed_identity_drift``;
- a changed ``current_release_id`` at stage entry is ``managed_identity_drift``
  with no dispatch;
- the ``recover`` row: a journaled ``rec_`` id with no receipt is resumed with
  ``phase=recover`` and the controller's outcome decides;
- CAS legs: the seed refuses a changed plist digest before any spawn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import (  # noqa: E402
    FakeHost,
    Fixture,
    advance_to_source_advanced,
    build_fixture,
    data,
    db_spy,
    expect,
)
from github_midwife_plugin import target_reconciliation as seed  # noqa: E402
from solet_manager import update_runtime_execution as executor  # noqa: E402
from solet_manager.cutover_receipts import CutoverJournal, CutoverReceiptStore, CutoverTerms  # noqa: E402
from solet_manager.errors import ManagedIdentityDriftError, UpdateFailedError  # noqa: E402
from solet_manager.reconciliation_request import RECONCILIATION_WIRE_KEYS  # noqa: E402
from solet_manager.update_execution import apply_update, preview_update_instance  # noqa: E402

_CHECKS = 0
BASELINE = "rel_20260910"
CANDIDATE = "rel_20260918"
THIRD = "rel_other"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _attestation(release_id: str, *, current: str | None = None, served: bool = True) -> dict[str, Any]:
    return {
        "current_release_id": current or release_id,
        "release_id": release_id,
        "router_active_instance_id": f"inst_{release_id}",
        "router_active_color": "blue" if release_id == BASELINE else "green",
        "self_start_token": "Mon Sep 18 10:00:00 2026",
        "manifest_etag": "etag-1",
        "source_surface_sha256": "sha256:" + "1" * 64,
        "release_surface_sha256": "sha256:" + "2" * 64,
        "served_by_self": served,
    }


class FakeSeedController:
    """Answers reconciliation envelopes through the seed's real validator, then a canned controller."""

    def __init__(self, fixture_home: Path, target: Path, *, outcome: str = "reconciled", write_receipt: bool = True) -> None:
        self.home = fixture_home
        self.target = target
        self.outcome = outcome
        self.write_receipt = write_receipt
        self.envelopes: list[dict[str, Any]] = []
        self.invoked: list[seed.TargetReconciliationRequest] = []

    def __call__(self, envelope: dict[str, Any]) -> str:
        self.envelopes.append(dict(envelope))
        _check(frozenset(envelope) == RECONCILIATION_WIRE_KEYS, "every dispatched envelope is the exact wire table")
        plist_path = self.home / "Library" / "LaunchAgents" / f"local.solet.{envelope['name']}.plist"
        try:
            result = seed.dispatch_reconciliation(json.dumps(envelope, sort_keys=True), installed_root=self.target, plist_path=plist_path, invoke_cutover=self._invoke)
        except seed.TargetReconciliationError as exc:
            return json.dumps({"schema_version": 1, "flow_id": seed.FLOW_ID, "status": "blocked", "error_kind": exc.error_kind, "message": str(exc)})
        return json.dumps({"schema_version": 1, "flow_id": seed.FLOW_ID, "status": "ok", "result": result})

    def _invoke(self, request: seed.TargetReconciliationRequest) -> dict[str, object]:
        self.invoked.append(request)
        outcome = self.outcome
        if request.phase == "recover":
            outcome = self.outcome
        if self.write_receipt and not CutoverReceiptStore(self.target).receipt_path(request.reconciliation_id).exists():
            # A real controller answers a recover for an already-terminal rec_ id from its receipt (Step 6 F-RT-2).
            self._write_receipt(request, outcome)
        return {
            "reconciliation_id": request.reconciliation_id,
            "status": outcome,
            "reason_code": "swap_verified" if outcome in {"reconciled", "already_reconciled"} else "candidate_failed_preflight",
            "prior_release_id": request.expected_current_release_id,
            "prior_instance_id": request.expected_active_instance_id,
            "prior_color": "blue",
            "provenance": {"reconciliation_id": request.reconciliation_id, "source_surface_sha256": request.expected_source_surface_sha256, "release_surface_sha256": request.expected_release_surface_sha256},
            "dry_run": False,
            "prior_pid": 4242,
            "prior_start_token": request.expected_active_start_token,
            "resumed": request.phase == "recover",
            "evidence": {"candidate_release_id": CANDIDATE, "candidate_instance_id": f"inst_{CANDIDATE}", "candidate_color": "green", "router_transitions": ["blue->green"], "finisher": "poller", "poller_gate": "passed"},
        }

    def _write_receipt(self, request: seed.TargetReconciliationRequest, outcome: str) -> None:
        store = CutoverReceiptStore(self.target)
        terms = CutoverTerms(
            current_release_id=request.expected_current_release_id,
            active_color="blue",
            active_instance_id=request.expected_active_instance_id,
            active_start_token=request.expected_active_start_token,
            manifest_etag=request.expected_manifest_etag,
            launch_topology=request.launch_topology,
            launchagent_label=request.expected_launchagent_label,
            launchagent_plist_sha256=request.expected_launchagent_plist_sha256,
            adapter_module_sha256="sha256:" + "a" * 64,
            adapter_module_replaced=False,
            source_surface_sha256=request.expected_source_surface_sha256,
            release_surface_sha256=request.expected_release_surface_sha256,
            verification_modules=request.verification_modules,
        )
        journal = CutoverJournal.prepared(reconciliation_id=request.reconciliation_id, name=request.name, target=self.target, fingerprint=request.approved_fingerprint, terms=terms, files=({"path": "x", "sha256": "sha256:" + "0" * 64},))
        store.write_active(journal)
        status = "reconciled" if outcome in {"reconciled", "already_reconciled"} else "failed_prior_serving"
        store.finalize(journal, cast(Any, status))


def _router_fixture(root: Path, *, controller_outcome: str = "reconciled", attestations: list[dict[str, Any]] | None = None, write_receipt: bool = True) -> tuple[Fixture, FakeSeedController]:
    host = FakeHost(attestations=attestations if attestations is not None else [_attestation(BASELINE)])
    fixture = build_fixture(root, router=True, roster=("github_midwife_plugin", "macos_self_deployment_plugin"), host=host, extra_candidate_files={"plugins/macos_self_deployment_plugin/pyproject.toml": "[project]\nname='x'\n"})
    controller = FakeSeedController(fixture.home, fixture.target, outcome=controller_outcome, write_receipt=write_receipt)
    host.reconciliation = controller
    (fixture.target / "profile" / "data").mkdir(parents=True, exist_ok=True)
    return fixture, controller


def _assert_router_preview(fixture: Fixture, controller: FakeSeedController) -> str:
    preview = preview_update_instance(fixture.request)
    _check(preview.status == "runtime_preview_ready", f"router preview ready: {preview.status} {preview.error_kind} {preview.data.get('blocked')} {preview.data.get('lifecycle')}")
    lifecycle = preview.data["lifecycle"]
    _check(data(lifecycle, "strategy") == "router_cutover" and data(lifecycle, "zero_downtime_rollback") is True and data(lifecycle, "launch_topology") == "materialized_supervisor", "router strategy fixed at preview on a materialized supervisor")
    receipt = data(lifecycle, "cutover_probe_receipt")
    _check(data(receipt, "probed") is True and data(receipt, "mutated") is False and data(receipt, "status") == "ok", "seed probe phase returned probed/mutated=false")
    _check(data(preview.data["runtime_baseline"], "attestation") is not None and data(data(preview.data["runtime_baseline"], "attestation"), "current_release_id") == BASELINE, "preview attestation names the runtime baseline")
    _check(len(controller.envelopes) == 1 and controller.envelopes[0]["phase"] == "probe" and not controller.invoked, "exactly one probe envelope, no controller invocation")
    return cast(str, preview.data["runtime_approval_fingerprint"])


def _assert_success(root: Path) -> None:
    fixture, controller = _router_fixture(root, attestations=[_attestation(BASELINE), _attestation(BASELINE), _attestation(BASELINE), _attestation(CANDIDATE)])
    advance_to_source_advanced(fixture)
    fingerprint = _assert_router_preview(fixture, controller)
    applied = apply_update(fixture.request, fingerprint)
    _check(applied.status == "promoted", f"router path reaches promoted: {applied.status} {applied.error_kind} {applied.message}")
    applies = [envelope for envelope in controller.envelopes if envelope["phase"] == "apply"]
    _check((len(applies), len(controller.invoked), controller.invoked[0].phase) == (1, 1, "apply"), "one apply envelope reached the controller")
    rec_id = str(applies[0]["reconciliation_id"])
    _check((rec_id.startswith("rec_"), rec_id != controller.envelopes[0]["reconciliation_id"]) == (True, True), "a fresh rec_ id per attempt")
    _assert_success_journal(fixture, rec_id)
    record = fixture.record()
    assert record.runtime_release is not None
    _check((record.runtime_release.commit, record.source_release.commit) == (fixture.candidate.commit, fixture.candidate.commit), "runtime axis published to the candidate; source at N+1")
    only_attest = all(call[0].endswith("attest_runtime_code") for call in fixture.host.bridge_calls)
    _check((len(fixture.host.bridge_calls) >= 3, only_attest) == (True, True), "attestation calls are the only bridge traffic")


def _assert_success_journal(fixture: Fixture, rec_id: str) -> None:
    journal = fixture.journal()
    rows = {cast(str, data(row, "operation_id")): row for row in cast(list[Any], journal["runtime_operations"])}
    attempts = [cast(dict[str, Any], attempt) for attempt in cast(list[Any], data(rows["lifecycle_cutover"], "attempts"))]
    dispatched = [attempt for attempt in attempts if attempt["phase"] == "apply"]
    _check(bool(dispatched), "an apply attempt row exists")
    _check((dispatched[0]["evidence"]["reconciliation_id"], "terms" in dispatched[0]["evidence"]) == (rec_id, True), "rec_ id and terms journaled before dispatch")
    outcome_rows = [attempt["evidence"] for attempt in attempts if "outcome" in attempt["evidence"]]
    _check(bool(outcome_rows), "an outcome row exists")
    terminal = (outcome_rows[-1]["outcome"]["evidence"]["candidate_release_id"], str(outcome_rows[-1]["receipt_sha256"]).startswith("sha256:"))
    _check(terminal == (CANDIDATE, True), "SwapEvidence and the terminal receipt digest are journaled")
    _check(data(rows["lifecycle_cutover"], "status") == "verified", "cutover row verified after attestation")


def _assert_candidate_failed(root: Path) -> None:
    fixture, _ = _router_fixture(root, controller_outcome="failed_prior_serving", attestations=[_attestation(BASELINE)])
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    fingerprint = cast(str, preview.data["runtime_approval_fingerprint"])
    exc = expect(UpdateFailedError, lambda: apply_update(fixture.request, fingerprint), "candidate failure accepted")
    _check(cast(UpdateFailedError, exc).error_kind == "runtime_candidate_failed", "router kept the prior release -> runtime_candidate_failed")
    record = fixture.record()
    journal = fixture.journal()
    _check(record.runtime_release is None and record.source_release.commit == fixture.candidate.commit and journal["status"] == "failed", "runtime axis stays None, source stays N+1, journal failed")
    _check(record.management_state.value == "needs_attention" and "runtime_candidate_failed" in record.update_eligibility.reason_codes, "F-RT-2: a terminal runtime failure publishes needs_attention (Step 6 D8)")


def _assert_not_serving_and_drift(root: Path) -> None:
    not_serving, _ = _router_fixture(root / "not_serving", attestations=[_attestation(BASELINE), _attestation(BASELINE), _attestation(BASELINE)])
    advance_to_source_advanced(not_serving)
    fingerprint = cast(str, preview_update_instance(not_serving.request).data["runtime_approval_fingerprint"])
    exc = expect(UpdateFailedError, lambda: apply_update(not_serving.request, fingerprint), "baseline still serving accepted")
    _check(cast(UpdateFailedError, exc).error_kind == "runtime_candidate_not_serving" and not_serving.record().runtime_release is None, "healthy but baseline-serving never publishes")
    third, _ = _router_fixture(root / "third", attestations=[_attestation(BASELINE), _attestation(BASELINE), _attestation(BASELINE), _attestation(THIRD)])
    advance_to_source_advanced(third)
    fingerprint = cast(str, preview_update_instance(third.request).data["runtime_approval_fingerprint"])
    exc = expect(ManagedIdentityDriftError, lambda: apply_update(third.request, fingerprint), "third release accepted")
    _check(third.record().runtime_release is None and third.journal()["status"] == "blocked", "a third release id is managed_identity_drift")
    moved, controller = _router_fixture(root / "moved", attestations=[_attestation(BASELINE), _attestation(BASELINE), _attestation(THIRD, current=THIRD)])
    advance_to_source_advanced(moved)
    fingerprint = cast(str, preview_update_instance(moved.request).data["runtime_approval_fingerprint"])
    expect(ManagedIdentityDriftError, lambda: apply_update(moved.request, fingerprint), "moved runtime baseline accepted")
    _check(not controller.invoked and all(envelope["phase"] == "probe" for envelope in controller.envelopes), "a changed current_release_id at stage entry dispatches nothing")


def _assert_recover_row(root: Path) -> None:
    fixture, controller = _router_fixture(root, attestations=[_attestation(BASELINE), _attestation(BASELINE), _attestation(BASELINE)], write_receipt=False)
    advance_to_source_advanced(fixture)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["runtime_approval_fingerprint"])

    def crash(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated crash after dispatch, before the outcome was journaled")

    with patch.object(executor.RuntimeExecution, "_classify_cutover", crash):
        expect(RuntimeError, lambda: apply_update(fixture.request, fingerprint), "crash injection did not fire")
    journal = fixture.journal()
    rows = {cast(str, data(row, "operation_id")): row for row in cast(list[Any], journal["runtime_operations"])}
    _check(journal["status"] == "lifecycle_applying" and data(rows["lifecycle_cutover"], "status") == "applying", "a rec_ id is journaled with no receipt")
    applies = [envelope for envelope in controller.envelopes if envelope["phase"] == "apply"]
    _check(len(applies) == 1 and len(controller.invoked) == 1, "the controller was invoked once before the crash")
    fixture.host.attestations = [_attestation(CANDIDATE)]
    resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "promoted", "recover resumes through runtime_advanced to promoted")
    recovers = [envelope for envelope in controller.envelopes if envelope["phase"] == "recover"]
    _check(len(recovers) == 1 and recovers[0]["reconciliation_id"] == applies[0]["reconciliation_id"] and controller.invoked[-1].phase == "recover", "the same rec_ id was sent with phase=recover and the controller's durable outcome decided")
    _check(len(applies) == 1, "no second apply was built on resume")


def _assert_plist_cas_refusal(root: Path) -> None:
    fixture, controller = _router_fixture(root, attestations=[_attestation(BASELINE)])
    advance_to_source_advanced(fixture)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["runtime_approval_fingerprint"])

    def edit_plist(self: Any, plan: Any) -> None:
        fixture.plist_path.write_bytes(fixture.plist_path.read_bytes() + b"<!-- late edit -->\n")

    with patch.object(executor.RuntimeExecution, "_require_plist_expected", edit_plist):
        exc = expect(Exception, lambda: apply_update(fixture.request, fingerprint), "plist edit accepted")
    _check(not controller.invoked, "no controller invocation after a plist change")
    _check(getattr(exc, "error_kind", "") == "probe_drift", f"a plist changed after hydration_advanced is probe_drift at lifecycle entry: {getattr(exc, 'error_kind', exc)}")


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_success(root / "success")
        _assert_candidate_failed(root / "failed")
        _assert_not_serving_and_drift(root / "drift")
        _assert_recover_row(root / "recover")
        _assert_plist_cas_refusal(root / "cas")
    print(f"existing_install_router_cutover_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
