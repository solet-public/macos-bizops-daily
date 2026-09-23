#!/usr/bin/env python3
"""Regression test for address-book memory auto-ingest id capture.

Guards the 2026-07-17 fix in ``memory_integration.ingest_to_memory``.

Root cause: ``memory_service.remember`` returns the actr backend shape
directly — ``{"memory_id": <str>, "message": <str>}`` — where ``memory_id``
is TOP-LEVEL. The pre-fix code read ``result["data"]["memory_id"]``, so it
silently returned ``None`` on every registration: the memory was created and
recallable, but its id was never written back into the address record
(``address.memory_id`` stayed null → the memory is orphaned on address delete,
because the delete path archives only when ``address.memory_id`` is set).

Cases:
  (a) real backend shape (top-level memory_id) -> returns the id (the regression).
  (b) auto_ingest disabled -> returns None, remember NOT called.
  (c) memory_service is None -> returns None.
  (d) legacy/None result / missing id -> returns None (no crash).

Pattern A direct-executable (matches aws_self_deployment_plugin/tests/test_restart.py):

    .venv/bin/python3 plugins/default_address_book_plugin/tests/test_memory_integration.py

Exits 0 on success, 1 on first failure with a labeled message.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1] / "src"))
sys.path.insert(0, str(_HERE.parents[3] / "ananta" / "src"))

from default_address_book_plugin.memory_integration import (  # noqa: E402
    ingest_to_memory,
)

_LOGGER = logging.getLogger("test_memory_integration")

_ENTRIES = [
    {"field_type": "city", "description": "Municipality", "value": "Langford"},
]


class _FakeMemoryService:
    """Records remember() calls and returns a configurable result."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.remember_calls: list[dict[str, Any]] = []

    def remember(self, content: str, tags: list[str]) -> Any:
        self.remember_calls.append({"content": content, "tags": tags})
        return self._result


def _fail(label: str) -> None:
    print(f"FAIL: {label}")
    sys.exit(1)


def _case_a_real_backend_shape() -> None:
    svc = _FakeMemoryService({"memory_id": "mem-test123", "message": "Remembered: ..."})
    got = ingest_to_memory(
        svc, True, "op_home", "postal", "desc", _ENTRIES, ["operator"], _LOGGER
    )
    if got != "mem-test123":
        _fail(f"(a) expected 'mem-test123' from top-level memory_id, got {got!r}")
    if len(svc.remember_calls) != 1:
        _fail(f"(a) expected exactly one remember() call, got {len(svc.remember_calls)}")
    tags = svc.remember_calls[0]["tags"]
    if tags[:2] != ["address-book", "type:postal"]:
        _fail(f"(a) expected leading address-book/type tags, got {tags!r}")


def _case_b_disabled() -> None:
    svc = _FakeMemoryService({"memory_id": "mem-should-not-be-read"})
    got = ingest_to_memory(
        svc, False, "op_home", "postal", "desc", _ENTRIES, [], _LOGGER
    )
    if got is not None:
        _fail(f"(b) expected None when auto_ingest disabled, got {got!r}")
    if svc.remember_calls:
        _fail("(b) remember() must NOT be called when auto_ingest disabled")


def _case_c_no_service() -> None:
    got = ingest_to_memory(
        None, True, "op_home", "postal", "desc", _ENTRIES, [], _LOGGER
    )
    if got is not None:
        _fail(f"(c) expected None when memory_service is None, got {got!r}")


def _case_d_missing_id() -> None:
    for result in ({"message": "no id here"}, None, {"data": {"memory_id": "x"}}):
        svc = _FakeMemoryService(result)
        got = ingest_to_memory(
            svc, True, "op_home", "postal", "desc", _ENTRIES, [], _LOGGER
        )
        if got is not None:
            _fail(f"(d) expected None for result={result!r}, got {got!r}")


def main() -> None:
    _case_a_real_backend_shape()
    _case_b_disabled()
    _case_c_no_service()
    _case_d_missing_id()
    print("OK: address-book memory-ingest id capture (4 cases)")


if __name__ == "__main__":
    main()
