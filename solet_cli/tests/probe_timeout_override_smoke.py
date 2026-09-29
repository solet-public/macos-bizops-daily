"""Only the llama-server probe gets a larger Manager budget; other probes keep 30 s (iss_1086dfbb)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.adapter_protocol import (  # noqa: E402
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    PROBE_TIMEOUT_OVERRIDES,
    probe_timeout_seconds,
)
from solet_manager.operation_records import operation_probe_request  # noqa: E402

_LLAMA_PROBE_REF = "setup::llama_cpp.server_available"
_OTHER_PROBE_REF = "setup::llama_cpp.models_present"
_CHECKS = 0


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _probe_request_budget(probe_ref: str) -> int:
    transaction = SimpleNamespace(name="fixture", target="/fixture/target", answers_fingerprint="sha256:" + "a" * 64)
    bundle = SimpleNamespace(
        flow_id="macos.repository_setup",
        source_revision="a" * 40,
        probes={"probe": {"probe_ref": probe_ref, "runner": "system"}},
    )
    operation = SimpleNamespace(operation_id="op")
    with_inputs = cast(Any, sys.modules["solet_manager.operation_records"])
    original = with_inputs.probe_public_inputs
    with_inputs.probe_public_inputs = lambda _transaction, _ref: {}
    try:
        _runner, request = operation_probe_request(
            cast(Any, transaction), cast(Any, bundle), cast(Any, operation), probe_id="probe", purpose="postcondition", attempt=1
        )
    finally:
        with_inputs.probe_public_inputs = original
    return request.timeout_seconds


def main() -> int:
    _check(DEFAULT_PROBE_TIMEOUT_SECONDS == 30, "the global probe default is unchanged")
    _check(set(PROBE_TIMEOUT_OVERRIDES) == {_LLAMA_PROBE_REF}, "exactly one probe carries an override")
    _check(probe_timeout_seconds(_LLAMA_PROBE_REF) == 90, "llama-server probe budget is 90 s")
    _check(probe_timeout_seconds(_OTHER_PROBE_REF) == 30, "other probes keep the default budget")
    _check(_probe_request_budget(_LLAMA_PROBE_REF) == 90, "operation probe request carries the llama-server override")
    _check(_probe_request_budget(_OTHER_PROBE_REF) == 30, "operation probe request keeps the default for others")
    print(f"probe_timeout_override_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
