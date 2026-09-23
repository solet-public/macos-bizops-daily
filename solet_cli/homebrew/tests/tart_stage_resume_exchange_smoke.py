#!/usr/bin/env python3
"""No-Tart checks for flow-derived guest adapter receipt exchanges."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "solet_cli" / "homebrew" / "ci"))

from tart_stage_resume_contract import ContractError, sha256  # noqa: E402
from tart_stage_resume_exchange import (  # noqa: E402
    adapter_exchange,
    validate_guest_receipt,
    write_exchange,
)

_CHECKS = 0
_FLOW = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base" / "macos_setup_flow.json"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _reject(callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except ContractError:
        _check(True, label)
    else:
        _check(False, label)


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for adapter, tool, probe_count in (
            ("homebrew", "brew", 1),
            ("lm_studio", "lms", 8),
            ("postgresql", "psql", 5),
            ("launchd", "launchctl", 1),
        ):
            exchange = adapter_exchange(_FLOW, adapter)
            _check(exchange["tool"] == tool, f"{adapter} receipt names its real tool")
            _check(
                len(exchange["probes"]) == probe_count,
                f"{adapter} reads its declared probe set",
            )
            _check(
                all(item["runner"] and item["probe_ref"] for item in exchange["probes"]),
                f"{adapter} carries declared runners and refs",
            )
            path = root / f"{adapter}-exchange.json"
            write_exchange(path, exchange)
            receipt = {
                "schema_version": 1,
                "adapter": adapter,
                "exchange_sha256": sha256(path),
                "flow_sha256": exchange["flow_sha256"],
                "tool": tool,
                "tool_version": f"{tool} fixture-version",
                "probes": [
                    {**probe, "checkpoint_status": "verified"}
                    for probe in exchange["probes"]
                ],
            }
            receipt_path = root / f"{adapter}-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            _check(
                validate_guest_receipt(path, receipt_path) == receipt,
                f"{adapter} matching guest receipt is accepted",
            )
            receipt["tool_version"] = ""
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            _reject(
                lambda path=path, receipt_path=receipt_path: validate_guest_receipt(
                    path, receipt_path
                ),
                f"{adapter} requires a real tool version",
            )
    _reject(lambda: adapter_exchange(_FLOW, "all"), "combined adapter exchanges are refused")
    print(f"tart_stage_resume_exchange_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
