"""Regression smoke for the adjudicated setup-flow precondition wiring."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "plugins/github_midwife_plugin/src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_ANANTA_SRC = _ROOT / "ananta/src"
if str(_ANANTA_SRC) not in sys.path:
    sys.path.insert(0, str(_ANANTA_SRC))
_MANAGER_SRC = _ROOT / "solet_cli/src"
if str(_MANAGER_SRC) not in sys.path:
    sys.path.insert(0, str(_MANAGER_SRC))

from github_midwife_plugin.installation_state_doctor import (  # noqa: E402
    _QUALIFIED_SOURCE_KINDS,
    _selected_source_row_valid,
    _sources_with_absent_roots,
    session_roots,
)
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from solet_manager.adapter_validation import validate_evidence  # noqa: E402

_FLOW_PATH = _ROOT / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json"
_FIELD_KINDS_PATH = _ROOT / "plugins/github_midwife_plugin/knowledge_base/setup_flow_field_kinds.json"
_HYDRATION_SMOKE_PATH = _ROOT / "plugins/github_midwife_plugin/tests/executable_hydration_smoke.py"
_REGISTRY_ALLOWLIST_PATH = _ROOT / "quality_gates/flow_probe_registry_gate_allowlist.txt"
_CHECKS = 0


def _rmtree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class FakeRuntime:
    """Hermetic adapter runtime for process and filesystem-probe contracts."""

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.home.mkdir()
        self.responses: dict[tuple[str, ...], CommandOutcome] = {}

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = 4096,
    ) -> CommandOutcome:
        del timeout_seconds, cwd, extra_env, input_text, output_limit
        return self.responses.get(argv, CommandOutcome(1, False, 1, "", "unexpected command"))

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonObject | None]:
        del url, timeout_seconds, payload
        return 503, None

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        del path, content, mode
        raise AssertionError("probe must not write")


def _request(target: Path, *, operation_id: str, operation_ref: str) -> AdapterRequest:
    return AdapterRequest.from_dict(
        {
            "protocol_version": 1,
            "kind": "operation_request",
            "request_id": "11111111-1111-4111-8111-111111111111",
            "operation_id": operation_id,
            "operation_ref": operation_ref,
            "phase": "probe",
            "probe_purpose": "pre_apply",
            "attempt": 1,
            "name": "flow-wiring",
            "target": str(target),
            "flow_id": "macos.repository_setup",
            "flow_source_revision": "a" * 40,
            "answers_fingerprint": "sha256:" + "b" * 64,
            "approval_fingerprint": None,
            "dry_run": True,
            "timeout_seconds": 30,
            "public_inputs": {},
        }
    )


def _flow() -> dict[str, Any]:
    return json.loads(_FLOW_PATH.read_text(encoding="utf-8"))


def _check_stage_wiring(flow: dict[str, Any]) -> None:
    preflight = flow["stages"]["preflight"]
    _check(
        preflight.get("entry_probe_refs")
        == ["minimum_physical_memory_valid", "git_checkout_valid"]
        and preflight["exit_probe_refs"] == ["python_version_valid"],
        "red: preserve RAM and checkout preflight entry probes",
    )
    session_stage = flow["stages"]["session_sources"]
    _check(
        session_stage.get("entry_probe_refs")
        == ["codex_session_roots_readable", "claude_session_roots_readable"],
        "red: remove a session-root stage-entry probe",
    )
    _check(
        flow["probes"]["launchagent_running"].get("remediation_operation_refs")
        == ["install_launchagent"],
        "red: offer Settings navigation instead of the LaunchAgent repair",
    )


def _check_conditional_wiring(flow: dict[str, Any]) -> None:
    probes = flow["probes"]
    _check(
        probes["codex_session_roots_readable"].get("required_when")
        == {"decision_ref": "session_sources", "operator": "contains", "value": "codex_local"}
        and probes["claude_session_roots_readable"].get("required_when")
        == {
            "decision_ref": "session_sources",
            "operator": "contains",
            "value": "claude_code_local",
        },
        "red: remove a session-root required_when condition",
    )
    salesforce = probes["salesforce_cli_available"]
    _check(
        salesforce.get("required_when")
        == {"decision_ref": "connectors_to_configure", "operator": "contains", "value": "salesforce"},
        "red: make the Salesforce CLI probe unconditional",
    )


def _check_salesforce_flow(flow: dict[str, Any]) -> None:
    probe = flow["probes"]["salesforce_cli_available"]
    preconditions = flow["operations"]["configure_salesforce"]["idempotency"][
        "precondition_probe_refs"
    ]
    _check(
        preconditions == ["salesforce_connection_valid"]
        and probe.get("remediation_operation_refs") == ["provision_salesforce_cli"],
        "red: restore the Salesforce provisioning self-remediation pair",
    )


def _check_coding_agent_preconditions(flow: dict[str, Any]) -> None:
    operations = flow["operations"]
    _check(
        operations["install_codex_plugin"]["idempotency"]["precondition_probe_refs"]
        == ["node_available", "codex_cli_available", "codex_plugin_visible"]
        and operations["install_claude_plugin"]["idempotency"]["precondition_probe_refs"]
        == ["claude_cli_available", "claude_plugin_visible"],
        "red: drop consented coding-agent executable preconditions while wiring this flow",
    )


def _check_keychain_and_auth_cleanup(flow: dict[str, Any]) -> None:
    keychain = flow["probes"]["keychain_available"]
    _check(
        keychain["level"] == "behavior"
        and keychain["probe_ref"] == "service_interface::vault_service.qualify_keychain",
        "red: restore the keychain precondition or stale process ref",
    )
    options = flow["decisions"]["coding_agents"]["option_source"]["options"]
    _check(
        "codex_cli_session" not in flow["auth_flows"]
        and "claude_cli_session" not in flow["auth_flows"]
        and "auth_flow_refs" not in flow["components"]["codex_cli"]
        and "auth_flow_refs" not in flow["components"]["claude_cli"]
        and "auth_flow_refs" not in options["codex"]["activates"]
        and "auth_flow_refs" not in options["claude_code"]["activates"],
        "red: restore any false coding-agent auth-flow metadata",
    )


def _check_r4_closeout() -> None:
    field_kinds = json.loads(_FIELD_KINDS_PATH.read_text(encoding="utf-8"))
    level_kind = field_kinds["fields"]["probes.level"]
    _check(
        level_kind
        == {
            "kind": "behavioural",
            "reader": "executable_hydration_smoke._precondition_reference_contract",
        },
        "red: restore R4 precondition-level tracked debt after wiring every orphan",
    )
    _check(
        "_PREEXISTING_ORPHAN_PRECONDITIONS = frozenset()"
        in _HYDRATION_SMOKE_PATH.read_text(encoding="utf-8"),
        "red: restore any R4 orphan-precondition allowlist entry",
    )
    stale_keychain_ref = "plugin::macos_vault_plugin::" + "check_keychain"
    _check(
        stale_keychain_ref not in _REGISTRY_ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines(),
        "red: retain stale source-absent Keychain registry debt",
    )
def _salesforce_outcome(*, executable: bool) -> CommandOutcome:
    data: JsonObject = {"executable": executable, "configured": executable}
    if executable:
        data.update({"executable_path": "/fixture/bin/sf", "version": "sf/2.0.0"})
    return CommandOutcome(
        0,
        False,
        1,
        json.dumps({"result": {"success": True, "error": None, "data": data}}),
        "",
    )


def _check_salesforce_inner_predicate(target: Path, runtime: FakeRuntime) -> None:
    request = _request(
        target,
        operation_id="salesforce_cli_available",
        operation_ref="plugin::salesforce_plugin.probe_cli",
    )
    salesforce_probe_key = "plugin::salesforce_plugin::" + "probe_cli"
    vector = (str(target / ".venv/bin/solet-bridge"), "call", salesforce_probe_key, "{}")
    runtime.responses[vector] = _salesforce_outcome(executable=False)
    false_envelope = dispatch_request(request, runtime)
    _check(
        false_envelope["checkpoint_status"] == "blocked"
        and false_envelope["error_kind"] == "salesforce_cli_missing"
        and false_envelope["planned_actions"] == [],
        "red: accept data.executable=false from a completed Salesforce envelope",
    )
    runtime.responses[vector] = _salesforce_outcome(executable=True)
    verified_envelope = dispatch_request(request, runtime)
    _check(
        verified_envelope["checkpoint_status"] == "verified",
        "Salesforce predicate accepts a complete executable result",
    )
    evidence_items = verified_envelope.get("evidence")
    evidence_item = evidence_items[0] if isinstance(evidence_items, list) else None
    observed = evidence_item.get("observed") if isinstance(evidence_item, dict) else None
    _check(
        isinstance(observed, list)
        and all(isinstance(item, str) for item in observed)
        and observed == sorted(set(observed)),
        "Salesforce CLI evidence uses deterministic unique public strings",
    )
    if not isinstance(evidence_item, dict):
        raise AssertionError("Salesforce CLI probe must emit one evidence object")
    validate_evidence(evidence_item)
    _check(True, "Salesforce CLI evidence is accepted by the manager validator")


def _session_root_requests(target: Path) -> tuple[AdapterRequest, AdapterRequest]:
    return (
        _request(
            target,
            operation_id="codex_session_roots_readable",
            operation_ref="hydration::sessions.probe_codex_roots",
        ),
        _request(
            target,
            operation_id="claude_session_roots_readable",
            operation_ref="hydration::sessions.probe_claude_roots",
        ),
    )


def _absent_evidence(envelope: JsonObject, evidence_id: str) -> JsonObject | None:
    items = envelope.get("evidence")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") == evidence_id:
            return item
    return None


def _check_session_roots_absent_is_not_a_denial(target: Path, runtime: FakeRuntime) -> None:
    """A fresh target has never run the agent CLIs, so its roots do not exist yet.

    ``os.access`` cannot tell ENOENT from EACCES, so before the split this case
    blocked ``session_sources`` with ``*_session_roots_unreadable`` and pointed at
    a macOS privacy pane that cannot create a directory.
    """

    codex, claude = _session_root_requests(target)
    _check(
        not (runtime.home / ".codex/sessions").exists()
        and not (runtime.home / ".claude/projects").exists(),
        "fresh-target fixture must leave the approved session roots absent",
    )
    codex_result = session_roots(codex, runtime)
    claude_result = session_roots(claude, runtime)
    _check(
        codex_result["checkpoint_status"] == "verified"
        and claude_result["checkpoint_status"] == "verified",
        "red: block a fresh target whose session roots have simply never been created",
    )
    _check(
        codex_result["error_kind"] is None and claude_result["error_kind"] is None,
        "red: report an absent session root as a permission denial",
    )
    codex_absent = _absent_evidence(codex_result, "codex_session_roots_absent")
    claude_absent = _absent_evidence(claude_result, "claude_session_roots_absent")
    _check(
        codex_absent is not None and claude_absent is not None,
        "red: pass an absent session root silently with no evidence of its own kind",
    )
    if codex_absent is None or claude_absent is None:
        raise AssertionError("absent session roots must emit their own evidence")
    _check(
        codex_absent.get("observed") == [str(runtime.home / ".codex/sessions")],
        "absent Codex root evidence names the root it measured",
    )
    validate_evidence(codex_absent)
    validate_evidence(claude_absent)
    _check(True, "absent-root evidence is accepted by the manager validator")


def _check_session_root_block_kinds(target: Path, runtime: FakeRuntime) -> None:
    """A root that EXISTS and cannot be read is still a blocking permission denial.

    This is the half ``open_files_permissions_settings`` can actually repair, so
    the split must not weaken it — the roots are created here precisely so the
    patched ``os.access`` measures a denial rather than an absence.
    """

    codex, claude = _session_root_requests(target)
    for relative in (".codex/sessions", ".claude/projects", ".claude/tasks"):
        (runtime.home / relative).mkdir(parents=True, exist_ok=True)
    (runtime.home / ".claude/history.jsonl").write_text("", encoding="utf-8")
    with patch("github_midwife_plugin.installation_state_doctor.os.access", return_value=False):
        codex_result = session_roots(codex, runtime)
        claude_result = session_roots(claude, runtime)
    _check(
        codex_result["error_kind"] == "codex_session_roots_unreadable"
        and claude_result["error_kind"] == "claude_session_roots_unreadable",
        "red: collapse distinct unreadable Codex and Claude root kinds",
    )
    _check(
        _absent_evidence(codex_result, "codex_session_roots_absent") is None,
        "red: label an existing but unreadable root as absent",
    )
    codex_readable = session_roots(codex, runtime)
    _check(
        codex_readable["checkpoint_status"] == "verified"
        and _absent_evidence(codex_readable, "codex_session_roots_absent") is None,
        "a present and readable root verifies with no absence recorded",
    )


def _check_empty_history_exit_predicate(runtime: FakeRuntime) -> None:
    """``backfill_count == 0`` is only excused by a root measured absent.

    Otherwise the entry-probe fix would unblock ``session_sources`` and the stage
    would re-block at its own exit four steps later.
    """

    row: dict[str, Any] = {
        "source": "codex_local",
        "selected": True,
        "consented": True,
        "registered": True,
        "backfill_count": 0,
        "retrieval_ok": False,
    }
    _check(
        _selected_source_row_valid(row, frozenset({"codex_local"})),
        "red: block a genuinely-empty-history target that has nothing to ingest",
    )
    _check(
        not _selected_source_row_valid(row, frozenset()),
        "red: excuse an empty backfill when the root exists and should have yielded content",
    )
    _check(
        not _selected_source_row_valid(
            {**row, "registered": False}, frozenset({"codex_local"})
        ),
        "red: accept an unregistered source just because its root is absent",
    )
    _check(
        _selected_source_row_valid(
            {**row, "backfill_count": 2, "retrieval_ok": True}, frozenset()
        ),
        "a measured backfill with retrieval proof stays valid",
    )
    _check(
        _sources_with_absent_roots(runtime) == frozenset(),
        "roots created by the unreadable fixture are not reported absent",
    )
    for relative in (".codex/sessions", ".claude/projects"):
        _rmtree(runtime.home / relative)
    _check(
        _sources_with_absent_roots(runtime)
        == frozenset({"codex_local", "claude_code_local"}),
        "red: claim a root is present when it does not exist",
    )


def _check_qualified_source_kind_drift() -> None:
    """The midwife's option-to-source_kind map must match the ledger's own.

    The exit predicate excuses an empty backfill per decision option, but the
    ``backfill_count`` it is excusing is keyed by ledger ``source_kind`` — if the
    two maps drift, the excuse lands on the wrong row and reads as green.
    """

    from ananta.services.session_ledger_service.selected_sources import _SOURCE_SPECS

    _check(
        _QUALIFIED_SOURCE_KINDS
        == {source: source_kind for source, _consent, source_kind in _SOURCE_SPECS},
        "red: let the midwife option-to-source_kind map drift from the ledger's",
    )


def main() -> int:
    flow = _flow()
    _check_stage_wiring(flow)
    _check_conditional_wiring(flow)
    _check_salesforce_flow(flow)
    _check_coding_agent_preconditions(flow)
    _check_keychain_and_auth_cleanup(flow)
    _check_r4_closeout()
    _check_qualified_source_kind_drift()
    with tempfile.TemporaryDirectory(prefix="flow-precondition-wiring-") as temporary:
        root = Path(temporary)
        target = root / "target"
        (target / ".venv/bin").mkdir(parents=True)
        runtime = FakeRuntime(root)
        _check_salesforce_inner_predicate(target, runtime)
        _check_session_roots_absent_is_not_a_denial(target, runtime)
        _check_session_root_block_kinds(target, runtime)
        _check_empty_history_exit_predicate(runtime)
    print(f"flow_precondition_wiring_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
