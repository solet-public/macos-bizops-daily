#!/usr/bin/env python3
"""Focused U2 operation binding tests; no Manager lifecycle effects."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "solet_cli/src"),
    str(ROOT / "solet_setup_contracts/src"),
    str(Path(__file__).parent),
]

import release_observer_contract_smoke as fixture  # noqa: E402
from solet_manager.release_operation import ReleaseOperationStore  # noqa: E402
from solet_setup_contracts.release_observer_codec import ObserverContractError  # noqa: E402

from solet_setup_contracts import release_observer_contract as contract  # noqa: E402

checks = 0


def check(value: bool, label: str) -> None:
    global checks
    checks += 1
    if not value:
        raise AssertionError(label)


def main() -> None:
    request = fixture._request()
    execution = contract.ExecutionRequest(request.header, request.allocation, request.intent)
    with tempfile.TemporaryDirectory() as raw:
        store = ReleaseOperationStore(Path(raw))
        check(store.bind(execution) == execution, "T08 pre-invocation binding persists")
        check(store.bind(execution) == execution, "T08 exact replay is idempotent")
        try:
            store.bind(
                contract.ExecutionRequest(request.header, request.allocation, request.intent)
            )
        except ObserverContractError:
            check(False, "T08 exact replay must not conflict")
    print(f"release_operation_smoke: {checks} assertions passed")


if __name__ == "__main__":
    main()
