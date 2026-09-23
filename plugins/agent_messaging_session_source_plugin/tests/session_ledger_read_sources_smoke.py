#!/usr/bin/env python3
"""Plugin-owned list_sources coverage for the agent-messaging source descriptor."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_session_source_plugin" / "src"))

from _stub_state_service import StubBlobStorageService, StubStateService  # noqa: E402
from agent_messaging_session_source_plugin.plugin import AgentMessagingSessionSourcePlugin  # noqa: E402
from ananta.llm.session_ledger.types import IngestSourceKind, SourceVendor  # noqa: E402
from ananta.services.session_ledger_service import SessionLedgerService  # noqa: E402

_FAILED: list[str] = []
_PLUGIN_NAME = "agent_messaging_session_source_plugin"


def _check(condition: object, label: str) -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL  {label}")


class _PluginManager:
    def __init__(self) -> None:
        self.plugins = {_PLUGIN_NAME: AgentMessagingSessionSourcePlugin()}


def _service(state: StubStateService) -> SessionLedgerService:
    return SessionLedgerService(
        state_service=state,
        blob_storage_service=StubBlobStorageService(),
        plugin_manager=_PluginManager(),
    )  # type: ignore[arg-type]


def test_descriptor_only() -> None:
    entry = _service(StubStateService()).list_sources()["sources"][0]
    _check(
        entry["source_kind"] == IngestSourceKind.AGENT_MESSAGING.value,
        "descriptor source_kind is agent_messaging",
    )
    _check(
        entry["vendor"] == SourceVendor.AGENT_MESSAGING.value,
        "descriptor vendor is agent_messaging",
    )
    _check(
        entry["source_id"] is None and entry["enabled"] is None,
        "descriptor-only entry has no DB values",
    )


def test_joined_row() -> None:
    state = StubStateService()
    state.add_select_response(
        "FROM session_ledger__source WHERE is_deleted",
        [
            {
                "id": "src_am",
                "source_kind": IngestSourceKind.AGENT_MESSAGING.value,
                "root_uri": "local:agent_messaging",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    entry = _service(state).list_sources()["sources"][0]
    _check(
        entry["source_id"] == "src_am" and entry["enabled"] is True,
        "joined source keeps DB identity and enabled state",
    )
    _check(
        entry["vendor"] == SourceVendor.AGENT_MESSAGING.value,
        "joined source retains plugin descriptor",
    )


def main() -> int:
    print("=== session_ledger_read_sources_smoke ===")
    test_descriptor_only()
    test_joined_row()
    if _FAILED:
        print(f"FAILED: {_FAILED}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
