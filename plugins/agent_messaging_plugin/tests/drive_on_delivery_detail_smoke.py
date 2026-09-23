#!/usr/bin/env python3
"""Hermetic regression smoke for drive-on-delivery diagnostic detail.

The established drive-on-delivery smoke supplies the real dispatch/state
seam. This focused smoke changes only the driver fault to the exact
``DriverChannelSendError`` emitted by the Codex stable gate, then proves that
the existing enum remains ``driver_error`` while its original diagnostic is
available in the sibling payload field.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import drive_on_delivery_smoke as drive_smoke  # noqa: E402

from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
)
from agent_messaging_plugin.session_hosts import DriverChannelSendError  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    transition_lifecycle_state,
)

_passed = 0
_failed: list[str] = []
_DRIVER_DETAIL = (
    "Codex tmux pane 'codex-wedge-proof' did not stabilize before Enter; "
    "submission was not attempted."
)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def test_driver_error_preserves_its_original_diagnostic() -> None:
    """RED FIRST: omitting the sibling payload detail field makes this fail.

    Failing mutation: return only ``DRIVE_DRIVER_ERROR`` from the driver
    exception branch. That is the current pre-repair behavior and erases the
    difference between the stable-gate no-Enter failure and every other
    driver error before a peer-send caller can diagnose it.
    """
    driver = drive_smoke._install_fake_host()  # noqa: SLF001 -- real seam fixture
    try:
        state = drive_smoke._state()  # noqa: SLF001 -- real seam fixture
        drive_smoke._insert(state)  # noqa: SLF001 -- real seam fixture
        transition_lifecycle_state(
            state,
            agent_instance_id=drive_smoke._RECIPIENT_AGI,  # noqa: SLF001
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="test:drive-on-delivery-detail",
        )

        def raise_driver_error(_notice: str) -> None:
            raise DriverChannelSendError(_DRIVER_DETAIL)

        driver.channel.send = raise_driver_error  # type: ignore[method-assign]
        outcome, _manager = drive_smoke._send(state)  # noqa: SLF001
        payload: dict[str, Any] = outcome.to_payload()
        _check(
            payload.get("drive_on_delivery") == "driver_error",
            "the existing drive outcome enum remains driver_error",
        )
        _check(
            payload.get("drive_on_delivery_detail") == _DRIVER_DETAIL,
            "RED proof 3: caller receives the original DriverChannelSendError "
            "detail beside driver_error",
        )
    finally:
        drive_smoke._remove_fake_host()  # noqa: SLF001 -- real seam fixture


def main() -> int:
    print("=== drive-on-delivery detail smoke ===")
    test_driver_error_preserves_its_original_diagnostic()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
