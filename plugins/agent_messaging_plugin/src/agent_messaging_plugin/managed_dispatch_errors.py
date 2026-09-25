"""Stable errors shared by managed-dispatch state-machine modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DispatchError(RuntimeError):
    """Stable fail-loud error from a managed-dispatch operation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = dict(data or {})
        super().__init__(f"{code}: {message}")

