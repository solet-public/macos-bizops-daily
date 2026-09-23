#!/usr/bin/env python3
"""Offline guard for releasing inference work only after router activation."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))

from macos_self_deployment_plugin.heartbeat_lifecycle import (  # noqa: E402
    _notify_post_registration_if_active,
)


class _Router:
    def __init__(self, active_instance_id: str) -> None:
        self._active_instance_id = active_instance_id

    def status(self) -> dict[str, str]:
        return {"active_instance_id": self._active_instance_id}


def main() -> int:
    calls: list[str] = []

    def callback() -> None:
        calls.append("released")

    logger = logging.getLogger("cold_boot_post_registration_release_smoke")

    _notify_post_registration_if_active(
        client=_Router("other-instance"),  # type: ignore[arg-type]
        self_instance_id="this-instance",
        logger=logger,
        callback=callback,
    )
    if calls:
        print("FAIL  inactive color released inference work")
        return 1
    print("PASS  inactive color does not release inference work")

    _notify_post_registration_if_active(
        client=_Router("this-instance"),  # type: ignore[arg-type]
        self_instance_id="this-instance",
        logger=logger,
        callback=callback,
    )
    if calls != ["released"]:
        print(f"FAIL  active color did not release work (calls={calls!r})")
        return 1
    print("PASS  active router registration releases inference work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
