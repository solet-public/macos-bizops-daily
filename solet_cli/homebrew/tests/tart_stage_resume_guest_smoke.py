#!/usr/bin/env python3
"""Hermetic checks for the stage-resume guest selected-exchange executor."""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path

CI = Path(__file__).resolve().parents[1] / "ci"
sys.path.insert(0, str(CI))

import tart_stage_resume_guest as guest  # noqa: E402
from tart_stage_resume_exchange import adapter_exchange, write_exchange  # noqa: E402

FLOW = Path(__file__).resolve().parents[3] / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    if not condition:
        raise AssertionError(message)
    _CHECKS += 1


def _envelopes(root: Path, *, status: str = "verified") -> None:
    exchange = adapter_exchange(FLOW, "homebrew")
    probe = exchange["probes"][0]
    assert isinstance(probe, dict)
    request_id = str(uuid.uuid4())
    request = {
        "kind": "operation_request",
        "request_id": request_id,
        "operation_id": probe["id"],
        "operation_ref": probe["probe_ref"],
        "phase": "probe",
    }
    result = {
        "kind": "operation_result",
        "request_id": request_id,
        "operation_id": probe["id"],
        "checkpoint_status": status,
    }
    (root / f"{request_id}-request.json").write_text(json.dumps(request), encoding="utf-8")
    (root / f"{request_id}-result.json").write_text(json.dumps(result), encoding="utf-8")


def _pass_receipt() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        exchanges, receipts, capture = root / "exchanges", root / "receipts", root / "capture"
        exchanges.mkdir()
        capture.mkdir()
        write_exchange(exchanges / "homebrew.json", adapter_exchange(FLOW, "homebrew"))
        _envelopes(capture)
        original = guest._tool_version
        guest._tool_version = lambda adapter, target: "Homebrew 7.0.1"  # type: ignore[assignment]
        try:
            guest.assemble_receipts(
                exchange_root=exchanges,
                receipt_root=receipts,
                capture_root=capture,
                selector="homebrew",
                target=root,
            )
        finally:
            guest._tool_version = original  # type: ignore[assignment]
        receipt = json.loads((receipts / "homebrew.json").read_text(encoding="utf-8"))
        _check(receipt["probes"][0]["checkpoint_status"] == "verified", "verified probe retained")
        evidence = json.loads((receipts / "homebrew.exchanges.json").read_text(encoding="utf-8"))
        _check(len(evidence["selected"]) == 1, "exact selected exchange retained")
        _check("request_sha256" in evidence["selected"][0], "exchange hashes retained")


def _reject_unverified_probe() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        exchanges, capture = root / "exchanges", root / "capture"
        exchanges.mkdir()
        capture.mkdir()
        write_exchange(exchanges / "homebrew.json", adapter_exchange(FLOW, "homebrew"))
        _envelopes(capture, status="blocked")
        original = guest._tool_version
        guest._tool_version = lambda adapter, target: "Homebrew 7.0.1"  # type: ignore[assignment]
        try:
            try:
                guest.assemble_receipts(
                    exchange_root=exchanges,
                    receipt_root=root / "receipts",
                    capture_root=capture,
                    selector="homebrew",
                    target=root,
                )
            except guest.GuestContractError:
                _check(True, "unverified manager probe is refused")
            else:
                raise AssertionError("unverified manager probe was accepted")
        finally:
            guest._tool_version = original  # type: ignore[assignment]


def _reject_missing_pair() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        try:
            guest._captured_pairs(root)
        except guest.GuestContractError:
            _check(True, "empty capture is refused")
        else:
            raise AssertionError("empty capture was accepted")


def main() -> int:
    _pass_receipt()
    _reject_unverified_probe()
    _reject_missing_pair()
    print(f"tart_stage_resume_guest_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
