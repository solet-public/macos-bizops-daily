"""Drift gate for setup's declared local permission and consent surface."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN_SRC = _ROOT / "plugins/github_midwife_plugin/src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_operations import _genesis  # noqa: E402

_FLOW_PATH = _ROOT / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
_GENESIS_PATH = _ROOT / "plugins/github_midwife_plugin/src/github_midwife_plugin/genesis.py"
_SETUP_OPERATIONS_PATH = (
    _ROOT / "plugins/github_midwife_plugin/src/github_midwife_plugin/setup_operations.py"
)
_ROUTER_INSTALL_PATH = (
    _ROOT / "plugins/github_midwife_plugin/src/github_midwife_plugin/router_install.py"
)
_BLUE_GREEN_ROUTER_PATH = (
    _ROOT
    / "plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/install_router.py"
)
_SELF_DEPLOYMENT_PLUGIN_ROOT = _ROOT / "plugins/macos_self_deployment_plugin"
_SKIP_EXIT_CODE = 77

_CHECKS = 0


@dataclass(frozen=True)
class BundlePluginSkip:
    """A visible skip caused only by a sealed bundle omitting a plugin."""

    profile: str
    plugin: str

    def render(self) -> str:
        return f"SKIP  {self.plugin} not in bundle {self.profile}; router LaunchAgent leg is not shipped"


def _bundle_plugin_skip(plugin_root: Path, plugin: str) -> BundlePluginSkip | None:
    if plugin_root.is_dir():
        return None
    provenance_path = _ROOT / "PROVENANCE.json"
    if not provenance_path.is_file():
        raise AssertionError(f"red: {plugin} is absent outside a sealed bundle")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    bundle = provenance.get("bundle")
    profile = bundle.get("name") if isinstance(bundle, dict) else None
    if not isinstance(profile, str) or not profile:
        raise AssertionError("red: sealed bundle has no valid PROVENANCE.json bundle.name")
    return BundlePluginSkip(profile=profile, plugin=plugin)


def _check(condition: object, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(message)


def _check_genesis_codex_config(flow: dict[str, Any], setup_operations: str, genesis: str) -> None:
    """Require the unconditional Codex MCP append to stay declared and previewed."""

    operation = flow["operations"]["run_genesis"]
    consent = flow["consents"]["codex_mcp_configuration_consent"]
    _check(
        "codex_mcp_configuration_consent" in operation["consent_refs"],
        "red: Genesis Codex config append lacks its consent declaration",
    )
    _check(
        consent["required"] is True and "required_when" not in consent,
        "red: unconditional Genesis Codex config append must remain unconditionally declared",
    )
    _check(
        "~/.codex/config.toml" in consent["scope"]
        and any("[mcp_servers.<name>]" in item for item in consent["scope"]),
        "red: Codex config declaration must name the file and appended MCP table",
    )
    _check(
        'runtime.home / ".codex" / "config.toml"' in setup_operations
        and "config_append" in setup_operations,
        "red: Genesis preview must enumerate the Codex config append",
    )
    actions = _genesis_preview_actions()
    _check(
        any(
            action["id"] == "genesis.configure_codex_mcp_server"
            and action["mutation_kind"] == "config_append"
            and action["target"].endswith("/.codex/config.toml")
            for action in actions
        ),
        "red: Genesis preview response must render the Codex config append action",
    )
    _check(
        "# the no-MCP-first primary interface" in genesis and "UNCONDITIONAL" in genesis,
        "red: Genesis must retain the measured unconditional command-launcher behavior",
    )


def _genesis_preview_actions() -> list[dict[str, object]]:
    request = AdapterRequest.from_dict(
        {
            "protocol_version": 1,
            "kind": "operation_request",
            "request_id": "11111111-1111-4111-8111-111111111111",
            "operation_id": "run_genesis",
            "operation_ref": "genesis::solet.run",
            "phase": "probe",
            "probe_purpose": "preview",
            "attempt": 1,
            "name": "permission-surface",
            "target": "/tmp/permission-surface-target",
            "flow_id": "macos.repository_setup",
            "flow_source_revision": "a" * 40,
            "answers_fingerprint": "sha256:" + "b" * 64,
            "approval_fingerprint": None,
            "dry_run": True,
            "timeout_seconds": 30,
            "public_inputs": {"autostart": "disabled"},
        }
    )
    response = _genesis(request, SimpleNamespace(home=Path("/tmp/permission-surface-home")))
    actions = response["planned_actions"]
    if not isinstance(actions, list) or not all(isinstance(action, dict) for action in actions):
        raise AssertionError("red: Genesis preview returned malformed planned actions")
    return actions


def _check_coding_agent_plugin_metadata(flow: dict[str, Any]) -> None:
    """Keep every coding-agent plugin install tied to truthful metadata."""

    expected = {
        "install_codex_plugin": (
            "codex_plugin_install_consent",
            "codex_plugin_configuration_permission",
        ),
        "install_claude_plugin": (
            "claude_plugin_install_consent",
            "claude_plugin_configuration_permission",
        ),
    }
    for operation_id, (consent_id, permission_id) in expected.items():
        operation = flow["operations"][operation_id]
        _check(
            operation.get("consent_refs") == [consent_id]
            and operation.get("permission_refs") == [permission_id],
            f"red: {operation_id} must carry its coding-agent consent and permission metadata",
        )
        _check(
            flow["consents"][consent_id]["required_when"]
            == {
                "decision_ref": "coding_agents",
                "operator": "contains",
                "value": "codex" if operation_id == "install_codex_plugin" else "claude_code",
            },
            f"red: {operation_id} metadata must remain selected-agent conditional",
        )
    claude_scope = flow["consents"]["claude_plugin_install_consent"]["scope"]
    _check(
        any("~/.claude/plugins/installed_plugins.json" in item for item in claude_scope),
        "red: Claude plugin metadata must disclose the installed-plugin registry read",
    )


def _check_launchagent_cardinality(
    flow: dict[str, Any], router_install: str, blue_green_router: str
) -> None:
    """Match the main and router plist paths to distinct declared entries."""

    main = flow["permissions"]["background_items_permission"]
    router = flow["permissions"]["blue_green_router_background_items_permission"]
    _check(
        "local.solet.<name>.plist" in main["purpose"],
        "red: main LaunchAgent declaration must name its plist identity",
    )
    _check(
        "local.solet.<name>.router.plist" in router["purpose"]
        and router["required_when"]
        == {
            "decision_ref": "setup_profile",
            "operator": "equals",
            "value": "macos-bizops",
        },
        "red: blue-green router LaunchAgent must remain separately and conditionally declared",
    )
    _check(
        flow["operations"]["run_genesis"].get("permission_refs")
        == ["blue_green_router_background_items_permission"],
        "red: Genesis router installer lacks its background-items declaration",
    )
    _check(
        "SELF_DEPLOYMENT_PLUGIN not in plugin_allowlist" in router_install
        and "local.solet.<name>.router.plist" in blue_green_router
        and '["bootout", service_target]' in blue_green_router
        and '["bootstrap", domain_target, str(plist_path)]' in blue_green_router
        and '["kickstart", "-k", service_target]' in blue_green_router,
        "red: every router LaunchAgent install path must retain its conditional declaration",
    )


def main() -> int:
    skip = _bundle_plugin_skip(_SELF_DEPLOYMENT_PLUGIN_ROOT, "macos_self_deployment_plugin")
    if skip is not None:
        print(skip.render())
        return _SKIP_EXIT_CODE
    flow = json.loads(_FLOW_PATH.read_text(encoding="utf-8"))
    _check_genesis_codex_config(
        flow,
        _SETUP_OPERATIONS_PATH.read_text(encoding="utf-8"),
        _GENESIS_PATH.read_text(encoding="utf-8"),
    )
    _check_coding_agent_plugin_metadata(flow)
    _check_launchagent_cardinality(
        flow,
        _ROUTER_INSTALL_PATH.read_text(encoding="utf-8"),
        _BLUE_GREEN_ROUTER_PATH.read_text(encoding="utf-8"),
    )
    print(f"permission_surface_contract_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
