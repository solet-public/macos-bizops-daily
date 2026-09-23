#!/usr/bin/env python3
"""Hermetic smoke for the bounded Google Workspace connection qualifier."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402


def main() -> int:
    plugin = GSuitePlugin()
    plugin.initialize({})
    plugin._token_store = SimpleNamespace(is_connected=lambda: True)  # noqa: SLF001
    calls: list[tuple[str, str]] = []

    def get_profile(*, userId: str) -> SimpleNamespace:
        calls.append(("getProfile", userId))
        return SimpleNamespace(execute=lambda: {"emailAddress": "operator@example.com"})

    plugin._service_factory = SimpleNamespace(  # noqa: SLF001
        gmail=lambda: SimpleNamespace(users=lambda: SimpleNamespace(getProfile=get_profile))
    )
    result = plugin.test_connection({}, {})
    assert result["action_status"] == "completed", result
    assert result["data"] == {"account_identity": "operator@example.com", "harmless_read_ok": True}, result
    assert calls == [("getProfile", "me")], calls
    assert "drive" not in repr(calls).lower()

    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator

    PluginRegistrationValidator().validate_edge_process_provider(
        "g_suite_plugin", plugin, discover_actions(plugin)
    )

    plugin._token_store = SimpleNamespace(is_connected=lambda: False)  # noqa: SLF001
    disconnected = plugin.test_connection({}, {})
    assert disconnected["error"]["code"] == "gsuite.not_connected", disconnected
    print("PASS bounded Google connection qualifier and EDGE registration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
