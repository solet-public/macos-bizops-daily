from __future__ import annotations

import datetime
from datetime import UTC
from typing import Any

RELOAD_SAFE = True


def build_response(
    status: str, data: dict[str, Any], error: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "action_status": status,
        "timestamp": datetime.datetime.now(UTC).isoformat(),
        "data": data,
        "actions": [],
        "error": error,
    }
