"""Hermetic regression gate for consented tool-provisioning pairs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_FLOW = _ROOT / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
_SETUP_OPERATIONS = (
    _ROOT / "plugins/github_midwife_plugin/src/github_midwife_plugin/setup_operations.py"
)
_INSTALLATION_DOCTOR = (
    _ROOT / "plugins/github_midwife_plugin/src/github_midwife_plugin/installation_doctor.py"
)
_SALESFORCE_PLUGIN_ROOT = _ROOT / "plugins/salesforce_plugin"
_SKIP_EXIT_CODE = 77


@dataclass(frozen=True)
class BundlePluginSkip:
    """A visible skip caused only by a sealed bundle omitting a plugin."""

    profile: str
    plugin: str

    def render(self) -> str:
        return f"SKIP  {self.plugin} not in bundle {self.profile}; Salesforce provisioning leg is not shipped"


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
    if not condition:
        raise AssertionError(message)


def _check_coding_provisioning(flow: dict[str, object], operation_source: str) -> None:
    """Assert coding-agent self-remediation, no-reinstall, and Node wiring."""

    operations = flow["operations"]
    probes = flow["probes"]
    dependencies = flow["dependencies"]
    options = flow["decisions"]["coding_agents"]["option_source"]["options"]
    _check(
        probes["codex_cli_available"]["remediation_operation_refs"] == ["install_codex_cli"],
        "red: deleting Codex self-remediation must stop its provisioner",
    )
    _check(
        "install_codex_plugin" not in probes["codex_cli_available"]["remediation_operation_refs"],
        "red: Codex plugin install must remain blocked by a missing CLI",
    )
    _check(
        "def _homebrew_provisioner(" in operation_source
        and 'if request.phase == "probe":' in operation_source
        and "if executable is not None:" in operation_source,
        "red: provisioner must remain probe-first and no-op after an apply-time re-probe",
    )
    codex_condition = {"decision_ref": "coding_agents", "operator": "contains", "value": "codex"}
    _check(
        probes["node_available"]["required_when"] == codex_condition
        and dependencies["node_runtime"]["required_when"] == codex_condition
        and "install_node" not in options["claude_code"]["activates"]["operation_refs"],
        "red: Node must not enter a Claude-only plan",
    )
    _check(
        operations["install_codex_plugin"]["idempotency"]["precondition_probe_refs"]
        == ["node_available", "codex_cli_available", "codex_plugin_visible"],
        "red: Codex plugin must retain its Node precondition",
    )


def _check_homebrew_contract(operation_source: str) -> None:
    """Assert exact closure, kind, and concrete consent-target behaviour."""

    runtime_source = (
        _ROOT / "plugins/github_midwife_plugin/src/github_midwife_plugin/setup_adapter_runtime.py"
    ).read_text(encoding="utf-8")
    _check(
        "set(items) != approved" in runtime_source,
        "red: Homebrew guard must reject undeclared additions",
    )
    _check(
        "expected_count = 1 + len(acquisition.approved_closure)" in runtime_source
        and "package_kind = acquisition.kind" in runtime_source,
        "red: Homebrew guard must accept only the declared closure and kind",
    )
    _check(
        "--cask" in operation_source and 'HomebrewAcquisition("cask", "codex")' in operation_source,
        "red: Codex must retain its reviewed cask acquisition",
    )
    _check(
        "target=f\"{brew or 'unresolved'}:{acquisition.name}\"" in operation_source,
        "red: provisioning consent must name the concrete Homebrew package mutation",
    )


def _check_salesforce_contract(
    flow: dict[str, object], operation_source: str, doctor_source: str
) -> None:
    """Assert the live Salesforce provisioner remains the sole setup path."""

    operations = flow["operations"]
    probes = flow["probes"]
    optional_ops = flow["stages"]["optional_accounts"]["operation_refs"]
    client_source = (_ROOT / "plugins/salesforce_plugin/src/salesforce_plugin/client.py").read_text(
        encoding="utf-8"
    )
    plugin_source = (_ROOT / "plugins/salesforce_plugin/src/salesforce_plugin/plugin.py").read_text(
        encoding="utf-8"
    )
    _check(
        operations["configure_salesforce"]["idempotency"]["precondition_probe_refs"]
        == ["salesforce_connection_valid"]
        and probes["salesforce_cli_available"]["remediation_operation_refs"]
        == ["provision_salesforce_cli"],
        "red: reintroducing the Salesforce CLI deadlock must fail",
    )
    _check(
        "def provision_cli(" in client_source and "probe_cli(" in client_source,
        "red: Salesforce provisioner must re-use its executable probe",
    )
    _check(
        "acknowledge_system_change" in plugin_source,
        "red: Salesforce provisioning must require explicit system-change acknowledgement",
    )
    _check(
        "plugin::salesforce_plugin.provision_cli" in doctor_source
        and '"plugin::salesforce_plugin.provision_cli":' not in operation_source,
        "red: Salesforce setup must delegate to the live plugin process",
    )
    _check(
        optional_ops.index("provision_salesforce_cli") + 1
        == optional_ops.index("configure_salesforce"),
        "red: Salesforce provisioner must immediately precede configuration",
    )


def _check_stage_and_debt(flow: dict[str, object], operation_source: str) -> None:
    """Protect stage-20 execution and prohibit evaluator-inert conditions."""

    operations = flow["operations"]
    stage = flow["stages"]["system_dependencies"]
    refs = stage["operation_refs"]
    _check(
        stage["sequence"] == 20
        and refs.index("build_instance_environment")
        < refs.index("install_codex_cli")
        < refs.index("install_postgresql")
        and refs.index("install_codex_cli") + 2 == refs.index("install_node"),
        "red: consented coding-agent installs must execute at their stage-20 position",
    )
    _check(
        operations["install_codex_cli"]["consent_refs"] == ["system_change_consent"]
        and "target=f\"{brew or 'unresolved'}:{acquisition.name}\"" in operation_source,
        "red: provisioning consent must remain explicit and concrete",
    )
    _check(
        not any(
            "fact_ref" in definition.get("required_when", {})
            for section in ("operations", "probes", "dependencies")
            for definition in flow[section].values()
        ),
        "red: fact_ref required_when remains evaluator-inert platform debt",
    )


def main() -> int:
    skip = _bundle_plugin_skip(_SALESFORCE_PLUGIN_ROOT, "salesforce_plugin")
    if skip is not None:
        print(skip.render())
        return _SKIP_EXIT_CODE
    flow = json.loads(_FLOW.read_text(encoding="utf-8"))
    operation_source = _SETUP_OPERATIONS.read_text(encoding="utf-8")
    doctor_source = _INSTALLATION_DOCTOR.read_text(encoding="utf-8")
    _check_coding_provisioning(flow, operation_source)
    _check_homebrew_contract(operation_source)
    _check_salesforce_contract(flow, operation_source, doctor_source)
    _check_stage_and_debt(flow, operation_source)
    _check_lm_studio_contract(flow)
    print("provisioning_pair_gate OK: coding, Salesforce and seven LM Studio provisioning pairs passed")
    return 0


def _check_lm_studio_contract(flow: dict[str, object]) -> None:
    expected = ["install_lm_studio", "start_lm_studio_server", "pull_lm_studio_embedding_model", "load_lm_studio_embedding_model", "pull_lm_studio_inference_model", "load_lm_studio_inference_model", "install_lm_studio_login_agent"]
    expected_probes = ["lm_studio_cli_available", "lm_studio_server_ready", "lm_studio_embedding_artifact_present", "lm_studio_embedding_model_served", "lm_studio_inference_artifact_present", "lm_studio_inference_model_served", "lm_studio_login_agent_valid", "lm_studio_jit_disabled"]
    refs = flow["stages"]["system_dependencies"]["operation_refs"]
    _check(refs[refs.index("install_tmux") + 1:] == expected, "LM Studio must bootstrap before models entry")
    operations = flow["operations"]
    for operation_id in expected:
        operation = operations[operation_id]
        _check(operation["runner"] == "bootstrap" and operation["implementation_status"] == "implemented", "LM Studio operations must use the pre-venv bootstrap adapter")
        _check(operation["requires_confirmation"] and "system_change_consent" in operation["consent_refs"], "host mutation requires consent")
        _check(set(operation["parameters"]) == {"embeddings_implementation", "inference_implementation"}, "only existing implementation decisions activate provisioning")
        for probe_id in operation["idempotency"]["postcondition_probe_refs"]:
            _check(probe_id in flow["completion"]["required_probe_refs"], "every provisioning postcondition must also gate completion")
    probes = flow["probes"]
    for probe_id in expected_probes:
        probe = probes[probe_id]
        _check(probe["runner"] == "bootstrap" and probe["implementation_status"] == "implemented", "LM Studio probes must use the pre-venv bootstrap adapter")
    _check_lm_studio_pair_policies(flow)


def _check_lm_studio_pair_policies(flow: dict[str, object]) -> None:
    operations = flow["operations"]
    for role in ("embedding", "inference"):
        decision = "embeddings_implementation" if role == "embedding" else "inference_implementation"
        condition = {"decision_ref": decision, "operator": "equals", "value": "lm_studio"}
        pull = operations[f"pull_lm_studio_{role}_model"]
        load = operations[f"load_lm_studio_{role}_model"]
        _check(pull["required_when"] == condition and load["required_when"] == condition, "each model pair must follow its own implementation selection")
        _check(pull["apply_timeout_seconds"] == 900 and "apply_timeout_seconds" not in load, "only long pulls receive the reviewed deadline")
    login = operations["install_lm_studio_login_agent"]
    _check("lm_studio_background_service_consent" in login["consent_refs"] and login["permission_refs"] == ["lm_studio_background_items_permission"], "shared login job requires separate background assent")
    _check(operations["start_lm_studio_server"]["idempotency"]["postcondition_probe_refs"] == ["lm_studio_server_ready", "lm_studio_jit_disabled"], "server readiness and JIT-disabled are separate postconditions")


if __name__ == "__main__":
    raise SystemExit(main())
