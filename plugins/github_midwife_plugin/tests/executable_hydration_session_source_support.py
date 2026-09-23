"""Session-source registration regression exercised by executable hydration."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ananta.llm.session_ledger.root_uri import canonicalize_root_uri_for_storage


def run_session_source_boot_registration_regression(
    target: Path,
    *,
    fake_runtime: Callable[[Path], Any],
    adapter_request: Any,
    raw_request: Callable[..., dict[str, object]],
    operation_handlers: Callable[[], dict[str, Callable[..., dict[str, object]]]],
    command_outcome: Any,
    check: Callable[[object, str], None],
) -> None:
    """Verify and poll an enabled boot-created source without public re-registration."""
    request = adapter_request.from_dict(
        raw_request(
            target,
            operation_id="register_codex_sessions",
            operation_ref="hydration::sessions.register_codex_filesystem",
            phase="apply",
            purpose=None,
        )
    )
    bridge = str(target / ".venv/bin/solet-bridge")
    health_command = (bridge, "health")
    healthy_outcome = command_outcome(0, False, 1, json.dumps({"status": "healthy"}), "")
    list_sources_command = (
        bridge,
        "call",
        "service_interface::session_ledger_service::list_sources",
        "{}",
    )
    runtime = fake_runtime(target.parent / "session-source-boot")
    runtime.responses[health_command] = healthy_outcome
    root = runtime.home / ".codex/sessions"
    root.mkdir(parents=True)
    stored_root_uri = canonicalize_root_uri_for_storage(str(root))
    disabled_runtime = fake_runtime(target.parent / "session-source-disabled")
    disabled_runtime.responses[health_command] = healthy_outcome
    disabled_root = disabled_runtime.home / ".codex/sessions"
    disabled_root.mkdir(parents=True)
    disabled_root_uri = canonicalize_root_uri_for_storage(str(disabled_root))
    disabled_runtime.responses[list_sources_command] = command_outcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "action_status": "completed",
                    "actions": [],
                    "error": None,
                    "data": {
                        "sources": [
                            {
                                "source_kind": "codex_local",
                                "root_uri": disabled_root_uri,
                                "source_id": "source-disabled",
                                "enabled": False,
                            }
                        ]
                    },
                }
            }
        ),
        "",
    )
    disabled = operation_handlers()[request.operation_ref](request, disabled_runtime)
    check(
        disabled["checkpoint_status"] != "applied",
        "setup does not report applied for a disabled boot-created source",
    )
    check(
        all(
            command[2] != "service_interface::session_ledger_service::poll_source"
            for command in disabled_runtime.commands
            if len(command) > 2
        ),
        "setup does not poll a disabled boot-created source",
    )
    runtime.responses[list_sources_command] = command_outcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "action_status": "completed",
                    "actions": [],
                    "error": None,
                    "data": {
                        "sources": [
                            {
                                "source_kind": "codex_local",
                                "root_uri": stored_root_uri,
                                "source_id": "source-boot",
                                "enabled": True,
                            }
                        ]
                    },
                }
            }
        ),
        "",
    )
    poll_command = (
        bridge,
        "call",
        "service_interface::session_ledger_service::poll_source",
        json.dumps({"source_id": "source-boot"}, separators=(",", ":")),
    )
    runtime.responses[poll_command] = command_outcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "action_status": "completed",
                    "actions": [],
                    "error": None,
                }
            }
        ),
        "",
    )
    applied = operation_handlers()[request.operation_ref](request, runtime)
    check(
        applied["checkpoint_status"] == "applied"
        and all(
            command[2] != "service_interface::session_ledger_service::register_source"
            for command in runtime.commands
            if len(command) > 2
        ),
        "setup verifies and polls the boot-created source without public re-registration",
    )


def run_router_and_qualification_regressions(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Callable[..., Any],
    dispatch_request: Callable[[Any, Any], dict[str, Any]],
    run_once: Callable[..., int],
    raw_request: Callable[..., dict[str, object]],
    run_knowledge_readiness_poll: Callable[..., None],
    run_knowledge_output_cap: Callable[..., None],
    structured_output_limit: int,
) -> None:
    """Exercise router, peer, knowledge, journal, and adapter-I/O boundaries."""
    router_manifest = target / "profile/config/manifest.yaml"
    router_manifest.parent.mkdir(parents=True, exist_ok=True)
    router_manifest.write_text(
        "profile_name: fixture\nplugins:\n- macos_self_deployment_plugin\n",
        encoding="utf-8",
    )
    router = request(
        target,
        operation_id="router_ready",
        operation_ref="service_interface::local_self_deployment_service.swap_status",
        probe_purpose="completion",
    )
    router_command = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::local_self_deployment_service::swap_status",
        "{}",
    )

    def router_result(status: dict[str, Any], *, nested: bool = False) -> dict[str, Any]:
        result_payload: dict[str, Any] = {
            "success": True,
            "action_status": "completed",
        }
        if nested:
            result_payload["data"] = {"router_status": status}
        else:
            result_payload["router_status"] = status
        runtime.responses[router_command] = command_outcome(
            0,
            False,
            1,
            json.dumps({"result": result_payload}),
            "",
        )
        return dispatch_request(router, runtime)

    canonical_instance = "solet-blue-deadbeef"
    canonical_status: dict[str, Any] = {
        "active_color": "blue",
        "active_instance_id": canonical_instance,
        "colors": [
            {
                "color": "blue",
                "instance_id": canonical_instance,
                "status": "active",
            }
        ],
    }
    canonical_router = router_result(canonical_status)
    check(
        canonical_router["checkpoint_status"] == "verified", "canonical router identity verifies"
    )
    check(
        canonical_router["evidence"][0]["observed"] == canonical_instance,
        "router evidence records the observed canonical instance id",
    )
    _router_transport_regression(router, runtime, router_command, router_result, dispatch_request, check, command_outcome)
    nested_router = router_result(canonical_status, nested=True)
    check(
        nested_router["checkpoint_status"] == "blocked",
        "nested router envelope blocks instead of being accepted as flat",
    )

    substring_router = router_result(
        {
            "active_color": "blue",
            "active_instance_id": "other-iris-solet-blue-deadbeef",
            "colors": [
                {
                    "color": "blue",
                    "instance_id": "other-iris-solet-blue-deadbeef",
                    "status": "active",
                }
            ],
        }
    )
    check(
        substring_router["checkpoint_status"] != "verified",
        "target-name substring in router identity is non-green",
    )

    color_mismatch_router = router_result(
        {
            "active_color": "blue",
            "active_instance_id": "solet-green-deadbeef",
            "colors": [
                {
                    "color": "blue",
                    "instance_id": "solet-green-deadbeef",
                    "status": "active",
                }
            ],
        }
    )
    check(
        color_mismatch_router["checkpoint_status"] != "verified",
        "router active color and instance id mismatch is non-green",
    )

    duplicate_router = router_result(
        {
            "active_color": "blue",
            "active_instance_id": canonical_instance,
            "colors": [
                {"color": "blue", "instance_id": canonical_instance, "status": "active"},
                {"color": "green", "instance_id": "solet-green-cafebabe", "status": "active"},
            ],
        }
    )
    check(
        duplicate_router["checkpoint_status"] != "verified",
        "duplicate active router rows are non-green",
    )

    malformed_router = router_result(
        {
            "active_color": "blue",
            "active_instance_id": "solet-blue-nothex",
            "colors": [{"color": "blue", "instance_id": "solet-blue-nothex", "status": "active"}],
        }
    )
    check(
        malformed_router["checkpoint_status"] != "verified",
        "malformed router identity is non-green",
    )

    unbounded_router = router_result(
        {
            "active_color": "blue",
            "active_instance_id": "x" * 4096,
            "colors": [{"color": "blue", "instance_id": "x" * 4096, "status": "active"}],
        }
    )
    check(
        unbounded_router["evidence"][0]["observed"] is None,
        "unbounded malformed router identity is omitted from evidence",
    )

    malformed_extra_row = router_result(
        {
            "active_color": "blue",
            "active_instance_id": canonical_instance,
            "colors": [
                {"color": "blue", "instance_id": canonical_instance, "status": "active"},
                {"color": "purple", "instance_id": "solet-purple-cafebabe", "status": "inactive"},
            ],
        }
    )
    check(
        malformed_extra_row["checkpoint_status"] != "verified",
        "malformed extra router roster row is non-green",
    )

    codex_launcher = target / "client/bin/codex-iris"
    codex_launcher.parent.mkdir(parents=True, exist_ok=True)
    codex_launcher.write_text(
        'export SOLET_NAME="iris"\nexport AGENT_SESSION_ID="fixture"\n',
        encoding="utf-8",
    )
    named_launcher = runtime.home / ".local/bin/iris"
    check(named_launcher.is_symlink(), "shared fixture retains the named bridge launcher")
    check(
        named_launcher.resolve(strict=True) == target / ".venv/bin/solet-bridge",
        "shared fixture named launcher remains target-bound",
    )
    peer = request(
        target,
        operation_id="peer_identity_valid",
        operation_ref="plugin::agent_messaging_plugin.peer_identity",
        probe_purpose="completion",
        public_inputs={"selected_coding_agents": ["codex"]},
    )
    peer_command = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "plugin::agent_messaging_plugin::peer_identity",
        "{}",
    )
    runtime.responses[peer_command] = command_outcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "data": {
                        "caller_identity_available": False,
                        "registered_bridge": False,
                        "bridge_identity": "unavailable",
                    },
                }
            }
        ),
        "",
    )
    check(
        dispatch_request(peer, runtime)["checkpoint_status"] == "verified",
        "one-shot bridge response with healthy selected Codex launcher verifies",
    )
    codex_launcher.write_text('export SOLET_NAME="iris"\n', encoding="utf-8")
    check(
        dispatch_request(peer, runtime)["checkpoint_status"] != "verified",
        "missing selected Codex launcher session marker is non-green",
    )
    codex_launcher.write_text(
        'export SOLET_NAME="iris"\nexport AGENT_SESSION_ID="fixture"\n',
        encoding="utf-8",
    )
    named_launcher.unlink()
    wrong_bridge = target / "other/.venv/bin/solet-bridge"
    wrong_bridge.parent.mkdir(parents=True, exist_ok=True)
    wrong_bridge.touch()
    named_launcher.symlink_to(wrong_bridge)
    check(
        dispatch_request(peer, runtime)["checkpoint_status"] != "verified",
        "named CLI symlink resolving outside the target is non-green",
    )
    named_launcher.unlink()
    named_launcher.symlink_to(target / ".venv/bin/solet-bridge")
    runtime.responses[peer_command] = command_outcome(1, False, 1, "", "bridge unavailable")
    check(
        dispatch_request(peer, runtime)["checkpoint_status"] != "verified",
        "failed bridge call is non-green even with healthy launcher artifacts",
    )
    runtime.responses[peer_command] = command_outcome(
        0,
        False,
        1,
        json.dumps(
            {
                "result": {
                    "success": True,
                    "data": {
                        "caller_identity_available": True,
                        "registered_bridge": True,
                        "bridge_identity": 1,
                    },
                }
            }
        ),
        "",
    )
    check(
        dispatch_request(peer, runtime)["checkpoint_status"] != "verified",
        "malformed peer identity is non-green",
    )

    kb = request(
        target,
        operation_id="knowledge_retrieval_succeeds",
        operation_ref="service_interface::knowledge_service.search",
        probe_purpose="completion",
    )
    runtime.responses[
        (
            str(target / ".venv/bin/solet-bridge"),
            "call",
            "service_interface::knowledge_service::search",
            '{"query":"session start orientation","top_k":1}',
        )
    ] = command_outcome(0, False, 1, '{"result":{"data":{"count":0,"results":[]}}}', "")
    failed_kb = dispatch_request(kb, runtime)
    check(
        failed_kb["checkpoint_status"] != "verified",
        "green process with empty KB retrieval is non-green",
    )
    run_knowledge_readiness_poll(
        target, runtime, request=request, check=check, command_outcome=command_outcome
    )
    run_knowledge_output_cap(
        target,
        runtime,
        request=request,
        check=check,
        command_outcome=command_outcome,
        structured_output_limit=structured_output_limit,
    )

    journal = request(
        target,
        operation_id="install_state_projection_matches",
        operation_ref="setup::journal.install_state_projection_matches",
        probe_purpose="completion",
    )
    check(
        dispatch_request(journal, runtime)["checkpoint_status"] != "verified",
        "missing install-state projection is non-green",
    )
    journal_path = target / ".solet/install-state.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps(
            {
                "name": "iris",
                "target": str(target),
                "flow_id": "macos.repository_setup",
                "flow_source_revision": "a" * 40,
                "answers_fingerprint": "sha256:" + "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    check(
        dispatch_request(journal, runtime)["checkpoint_status"] == "verified",
        "atomic install-state projection verifies its manager identity",
    )
    journal_path.write_text(
        journal_path.read_text(encoding="utf-8").replace("b" * 64, "c" * 64),
        encoding="utf-8",
    )
    check(
        dispatch_request(journal, runtime)["checkpoint_status"] != "verified",
        "install-state projection fingerprint drift remains non-green",
    )

    malformed_stdout = io.StringIO()
    malformed_stderr = io.StringIO()
    malformed_code = run_once(
        io.StringIO('{"secret":"must-not-echo"'),
        malformed_stdout,
        malformed_stderr,
        runtime=runtime,
    )
    check(malformed_code == 2, "malformed adapter input exits with protocol error")
    check(malformed_stdout.getvalue() == "", "malformed adapter emits no result fragment")
    check(
        "must-not-echo" not in malformed_stderr.getvalue(),
        "malformed input is not reflected to diagnostics",
    )

    valid_stdout = io.StringIO()
    valid_stderr = io.StringIO()
    valid_code = run_once(
        io.StringIO(json.dumps(raw_request(target, operation_ref="setup::unknown.operation"))),
        valid_stdout,
        valid_stderr,
        runtime=runtime,
    )
    valid_output = json.loads(valid_stdout.getvalue())
    check(valid_code == 0 and valid_stderr.getvalue() == "", "valid adapter I/O is closed")
    check(valid_output["error_kind"] == "adapter_missing", "adapter output is parseable JSON")


def _router_transport_regression(
    router: Any,
    runtime: Any,
    command: tuple[str, ...],
    router_result: Callable[[dict[str, Any]], dict[str, Any]],
    dispatch_request: Callable[[Any, Any], dict[str, Any]],
    check: Callable[[object, str], None],
    command_outcome: Callable[..., Any],
) -> None:
    null_binding = router_result({"active_color": None, "active_instance_id": None, "colors": []})
    check(
        null_binding["error_kind"] == "router_identity_failed"
        and null_binding["evidence"][0]["observed"] is None,
        "reachable router with a null active binding remains an identity failure",
    )
    management_unreachable = router_result({"error": "router socket unavailable"})
    check(
        management_unreachable["error_kind"] == "router_mgmt_unreachable"
        and management_unreachable["evidence"][0]["observed"] == "router_mgmt_unreachable",
        "platform-reported router management failure is distinct from an identity fault",
    )
    runtime.responses[command] = command_outcome(1, False, 1, "", "connection refused")
    unreachable = dispatch_request(router, runtime)
    check(
        unreachable["error_kind"] == "router_unreachable"
        and unreachable["evidence"][0]["observed"] == "router_unreachable"
        and unreachable["error_kind"] != null_binding["error_kind"],
        "unreachable router is distinct from a null active binding",
    )
    runtime.responses[command] = command_outcome(0, False, 1, "HTTP/1.1 503 Service Unavailable", "")
    unavailable = dispatch_request(router, runtime)
    check(
        unavailable["error_kind"] == "router_transport_503"
        and unavailable["evidence"][0]["observed"] == "router_transport_503",
        "router public-port 503 is distinct from a null active binding",
    )
