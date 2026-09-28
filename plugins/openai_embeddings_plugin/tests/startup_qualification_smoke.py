#!/usr/bin/env python3
"""Offline regression smoke for embedding startup qualification."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "openai_embeddings_plugin" / "src"))

from openai_embeddings_plugin.constants import ErrorCode  # noqa: E402
from openai_embeddings_plugin.plugin import OpenAIEmbeddingsPlugin  # noqa: E402
from openai_embeddings_plugin.response_builders import error_result  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _AddressBookService:
    @staticmethod
    def resolve_with_secrets(_name: str) -> dict[str, Any]:
        return {
            "action_status": "completed",
            "data": {
                "entries": [
                    {"field_type": "base_url", "value": "http://embeddings.test/v1"},
                    {"field_type": "model", "value": "embed-test"},
                    {"field_type": "timeout_seconds", "value": "12"},
                ]
            },
        }


class _Orchestrator:
    @staticmethod
    def get_service(name: str) -> _AddressBookService | None:
        return _AddressBookService() if name == "address_book_service" else None


def test_qualified_model_marks_plugin_ready() -> None:
    plugin = OpenAIEmbeddingsPlugin()
    plugin.orchestrator_ref = _Orchestrator()  # type: ignore[assignment]
    calls: list[tuple[str, dict[str, Any]]] = []

    def _success(url: str, payload: dict[str, Any]) -> tuple[dict[str, Any], None]:
        calls.append((url, payload))
        return {"data": [{"index": 0, "embedding": [0.1, 0.2]}], "model": "embed-test"}, None

    plugin._call_embeddings_api = _success  # type: ignore[method-assign]
    plugin.prepare_for_readiness()

    _check(calls == [], "startup configuration does not call embeddings before registration")
    _check(plugin.qualification_status == {"state": "pending"}, "qualification is visibly pending")
    plugin.start_post_registration_qualification()
    plugin.wait_for_post_registration_qualification()
    _check(plugin.is_ready(), "configured plugin remains available after qualification")
    _check(plugin.qualification_status == {"state": "ready"}, "post-registration probe marks qualification ready")
    _check(
        calls == [
            (
                "http://embeddings.test/v1/embeddings",
                {"model": "embed-test", "input": ["startup readiness probe"]},
            )
        ],
        "qualification sends one embedding request to the configured model",
    )


def test_unreachable_model_stays_pending_before_registration() -> None:
    plugin = OpenAIEmbeddingsPlugin()
    plugin.orchestrator_ref = _Orchestrator()  # type: ignore[assignment]
    plugin._call_embeddings_api = lambda _url, _payload: (  # type: ignore[method-assign]
        None,
        error_result(ErrorCode.CONNECTION_FAILED, "connection refused"),
    )

    plugin.prepare_for_readiness()
    _check(plugin.is_ready(), "unreachable model cannot abort startup configuration")
    _check(plugin.qualification_status == {"state": "pending"}, "refusing model is pending, not fatal")
    _check(plugin.get_readiness_error() is not None, "health exposes pending qualification")


def main() -> int:
    print("=== startup_qualification_smoke ===")
    test_qualified_model_marks_plugin_ready()
    test_unreachable_model_stays_pending_before_registration()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
