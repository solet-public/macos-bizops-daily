"""Address book plugin result builder utilities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plugins.plugin_contracts import ActionStatus

from .constants import PLUGIN_NAME


def now() -> str:
    return datetime.now(UTC).isoformat()


def success(data: dict[str, Any]) -> ActionResult:
    return ActionResult(
        action_status=ActionStatus.COMPLETED.value,
        timestamp=now(),
        data=data,
        actions=[],
        error=None,
    )


def error(code: str, message: str) -> ActionResult:
    error_detail: ErrorDetail = {
        "type": "AddressBookError",
        "code": code,
        "message": message,
        "details": {"plugin_name": PLUGIN_NAME},
        "severity": "error",
        "timestamp": now(),
    }
    return ActionResult(
        action_status=ActionStatus.ERROR.value,
        timestamp=now(),
        data={},
        actions=[],
        error=error_detail,
    )
