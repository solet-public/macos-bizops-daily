#!/usr/bin/env python3
"""Operator host qualification through real HTTP registration and SMI shapes.

OS facts are substituted; no session, role, Git state or live database is
changed. This suite is deliberately not an idle-wake acceptance claim.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.services.store import open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin import http_routes  # noqa: E402
from agent_messaging_plugin import operator_codex_launcher as launcher  # noqa: E402
from agent_messaging_plugin import operator_codex_registration as registration  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.local_cli.client import BridgeCallError, BridgeClient  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import PEER_BINDING_NAMESPACE, get_peer_binding_schema  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    _rearm_report_by,
    _retiring_lane_worktree,
)

INSTANCE = "agi-" + "a" * 32
SESSION = "ases-operator-smoke"
HOST = "operator-codex-" + "b" * 32
CLAUDE_HOST = "Operator-Claude-Smoke"
REQUEST = registration.OperatorCodexRegistration(
    host_ref=HOST, agent_id="codex", agent_instance_id=INSTANCE,
    agent_session_id=SESSION, session_label="Operator-Smoke", parent_pid=701,
    watcher_declared=True,
)
METADATA = {"model": "gpt-5.6-sol", "effort": "low", "lane_repo_root": "/tmp"}
CLAUDE_REQUEST = registration.OperatorClaudeRegistration(
    host_ref=CLAUDE_HOST, agent_id="claude_code", agent_instance_id=INSTANCE,
    agent_session_id=SESSION, session_label=CLAUDE_HOST, parent_pid=701,
    watcher_declared=True,
)


def _probe(argv: list[str]) -> str:
    if "-v" in argv:
        raise AssertionError("tmux show-environment has no -v flag")
    if argv[0] == "/bin/ps":
        return f"{700 if argv[2] == '701' else 1} {os.getuid()} /bin/{'python3' if argv[2] == '701' else 'codex'}"
    if "list-panes" in argv:
        return "700\t0\t/tmp"
    values = {
        "AGENT_INSTANCE_ID": INSTANCE, "AGENT_SESSION_ID": SESSION,
        "AGENT_SESSION_LABEL": "Operator-Smoke", "FLEET_TRANSPORT": "watch",
        "SOLET_NAME": os.environ["SOLET_NAME"],
        "OPERATOR_CODEX_MODEL": METADATA["model"],
        "OPERATOR_CODEX_EFFORT": METADATA["effort"], "OPERATOR_CODEX_CWD": "/tmp",
    }
    return f"{argv[-1]}={values[argv[-1]]}"


def _claude_probe(argv: list[str]) -> str:
    if argv[0] == "/bin/ps":
        executable = "python3" if argv[2] == "701" else "claude"
        parent = 700 if argv[2] == "701" else 1
        return f"{parent} {os.getuid()} /bin/{executable}"
    if "list-panes" in argv:
        return "700\t0\t/tmp"
    values = {
        "AGENT_SESSION_ID": SESSION,
        "AGENT_SESSION_LABEL": CLAUDE_HOST,
        "FLEET_TRANSPORT": "watch",
        "SOLET_NAME": os.environ["SOLET_NAME"],
    }
    return f"{argv[-1]}={values[argv[-1]]}"


def _client(state: RealShapeState) -> TestClient:
    manager = BridgeSessionManager(
        session_id_factory=lambda _: "agc-operator-smoke", idle_timeout_s=60,
        max_pending_events=10, long_poll_timeout_s=1,
    )
    registry = PeerRegistry(bindings_store=open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    ))
    app = FastAPI()
    register_routes(
        app, bridge_manager=manager, peer_registry=registry,
        platform_surface=cast("Any", object()), agent_messaging_service=object(),
        config={}, state_service=state,
    )
    return TestClient(app)


def _body() -> dict[str, object]:
    body = dataclasses.asdict(REQUEST)
    body["operator_tmux_host"] = body.pop("host_ref")
    return body


def _claude_body() -> dict[str, object]:
    body = dataclasses.asdict(CLAUDE_REQUEST)
    body["operator_tmux_host"] = body.pop("host_ref")
    return body


class RegistrationTests(unittest.TestCase):
    def test_real_tmux_environment_readback_and_owned_host_cleanup(self) -> None:
        tmux = shutil.which("tmux")
        if tmux is None:
            self.skipTest("tmux is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "tmux-probe"
            socket = str(Path(directory) / "socket")
            wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(tmux)} -S {shlex.quote(socket)} \"$@\"\n")
            wrapper.chmod(0o700)
            command = [str(wrapper)]
            subprocess.run([*command, "new-session", "-d", "-s", HOST, "-e", f"AGENT_INSTANCE_ID={INSTANCE}", "-e", "PROBE=a=b c", "sleep 30"], check=True, capture_output=True, timeout=10)
            try:
                self.assertEqual(registration._tmux_environment_value(str(wrapper), "=" + HOST, "PROBE"), "a=b c")
                with self.assertRaises(registration.OperatorHostQualificationError):
                    registration._tmux_environment_value(str(wrapper), "=" + HOST, "MISSING")
                launcher._stop_failed_host(str(wrapper), HOST, "agi-foreign")
                self.assertEqual(subprocess.run([*command, "has-session", "-t", "=" + HOST], capture_output=True, timeout=10).returncode, 0)
                launcher._stop_failed_host(str(wrapper), HOST, INSTANCE)
                self.assertNotEqual(subprocess.run([*command, "has-session", "-t", "=" + HOST], capture_output=True, timeout=10).returncode, 0)
            finally:
                subprocess.run([*command, "kill-server"], capture_output=True, timeout=10, check=False)

    def test_http_registration_and_reconnect_preserve_operator_contract(self) -> None:
        state = RealShapeState()
        with _client(state) as client, patch.object(registration, "_output", side_effect=_probe), patch.object(registration.shutil, "which", return_value="/bin/tmux"):
            bridge = client.post("/api/v1/bridge/open", json={}).json()["bridge_id"]
            for _ in range(2):
                result = client.post(f"/api/v1/bridge/{bridge}/peer/register", json=_body())
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["operator_codex_host"]["host_ref"], HOST)
            row = read_managed_session(cast("StateManagementInterface", state), INSTANCE)
            self.assertEqual((row["host"], row["agent_runtime"]), ("tmux", "codex"))
            self.assertIsNone(row.get("report_by"))
            self.assertIsNone(row.get("expires_at"))
            self.assertEqual(row["report_by_seconds"], 0)
            self.assertIsNone(_retiring_lane_worktree(row))
            _rearm_report_by(cast("StateManagementInterface", state), INSTANCE, source="explicit_self_report")
            self.assertIsNone(read_managed_session(cast("StateManagementInterface", state), INSTANCE).get("report_by"))

    def test_failed_qualification_does_not_register_a_driver(self) -> None:
        state = RealShapeState()
        with _client(state) as client, patch.object(registration, "qualify_operator_codex_host", side_effect=registration.OperatorHostQualificationError("foreign pane")):
            bridge = client.post("/api/v1/bridge/open", json={}).json()["bridge_id"]
            result = client.post(f"/api/v1/bridge/{bridge}/peer/register", json=_body())
            self.assertEqual(result.status_code, 400, result.text)
            with self.assertRaises(registration.SessionNotFoundError):
                read_managed_session(cast("StateManagementInterface", state), INSTANCE)

    def test_claude_http_registration_qualifies_stamped_launcher_identity(self) -> None:
        state = RealShapeState()
        with _client(state) as client, patch.object(registration, "_output", side_effect=_claude_probe), patch.object(registration.shutil, "which", return_value="/bin/tmux"):
            bridge = client.post("/api/v1/bridge/open", json={}).json()["bridge_id"]
            result = client.post(f"/api/v1/bridge/{bridge}/peer/register", json=_claude_body())
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["operator_claude_host"]["host_ref"], CLAUDE_HOST)
            row = read_managed_session(cast("StateManagementInterface", state), INSTANCE)
            self.assertEqual(
                (row["host"], row["host_ref"], row["agent_runtime"]),
                ("tmux", CLAUDE_HOST, "claude_code"),
            )

    def test_claude_same_named_tmux_host_is_qualified_without_codex_field(self) -> None:
        state = RealShapeState()
        body = _claude_body()
        body["operator_tmux_host"] = ""
        body["watcher_declared"] = False
        with _client(state) as client, patch.object(http_routes, "claude_operator_tmux_host_ref", return_value=CLAUDE_HOST), patch.object(registration, "_output", side_effect=_claude_probe), patch.object(registration.shutil, "which", return_value="/bin/tmux"):
            bridge = client.post("/api/v1/bridge/open", json={}).json()["bridge_id"]
            result = client.post(f"/api/v1/bridge/{bridge}/peer/register", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["operator_claude_host"]["host_ref"], CLAUDE_HOST)

    def test_claude_automatic_qualification_failure_falls_back_to_registration(self) -> None:
        def missing_transport(argv: list[str]) -> str:
            if argv[-1] == "FLEET_TRANSPORT":
                return "FLEET_TRANSPORT=mcp"
            return _claude_probe(argv)

        state = RealShapeState()
        body = _claude_body()
        body["operator_tmux_host"] = ""
        with _client(state) as client, patch.object(http_routes, "claude_operator_tmux_host_ref", return_value=CLAUDE_HOST), patch.object(registration, "_output", side_effect=missing_transport), patch.object(registration.shutil, "which", return_value="/bin/tmux"):
            bridge = client.post("/api/v1/bridge/open", json={}).json()["bridge_id"]
            with self.assertLogs(http_routes.logger, level="WARNING"):
                result = client.post(f"/api/v1/bridge/{bridge}/peer/register", json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["operator_claude_host"], {})
            row = read_managed_session(cast("StateManagementInterface", state), INSTANCE)
            self.assertEqual((row["host"], row.get("host_ref")), ("operator", None))

    def test_codex_explicit_host_refuses_incomplete_declaration(self) -> None:
        state = RealShapeState()
        body = _body()
        body["watcher_declared"] = False
        with _client(state) as client:
            bridge = client.post("/api/v1/bridge/open", json={}).json()["bridge_id"]
            result = client.post(f"/api/v1/bridge/{bridge}/peer/register", json=body)
            self.assertEqual(result.status_code, 400, result.text)
            with self.assertRaises(registration.SessionNotFoundError):
                read_managed_session(cast("StateManagementInterface", state), INSTANCE)

    def test_existing_worker_cannot_be_adopted(self) -> None:
        state = cast("StateManagementInterface", RealShapeState())
        insert_managed_session(state, ManagedSessionSpec(
            agent_instance_id=INSTANCE, lane_id="worker", brief_ref="brief", work_class="analysis_deliverable",
            budget_line="budget", host="tmux", agent_runtime="codex",
        ))
        before = dict(read_managed_session(state, INSTANCE))
        with patch.object(registration, "qualify_operator_codex_host", return_value=METADATA), self.assertRaises(registration.OperatorHostQualificationError):
            registration.register_operator_codex_host(state, REQUEST)
        self.assertEqual(read_managed_session(state, INSTANCE), before)

    def test_identity_and_process_mismatches_are_refused(self) -> None:
        with patch.object(registration, "_output", side_effect=_probe), patch.object(registration.shutil, "which", return_value="/bin/tmux"):
            self.assertEqual(registration.qualify_operator_codex_host(REQUEST), METADATA)
            for change in [{"agent_id": "claude_code"}, {"watcher_declared": False}, {"agent_instance_id": "agi-" + "c" * 32}, {"parent_pid": 702}, {"host_ref": "other:0.0"}]:
                with self.subTest(change=change), self.assertRaises(registration.OperatorHostQualificationError):
                    registration.qualify_operator_codex_host(dataclasses.replace(REQUEST, **change))

    def test_old_service_response_cannot_arm_new_launcher(self) -> None:
        client = BridgeClient("http://127.0.0.1:1")
        with patch.object(client, "_require_bridge", return_value="agc-smoke"), patch.object(client, "_post_or_reject", return_value={"status": "registered"}), self.assertRaises(BridgeCallError):
            client.peer_register(
                agent_id="codex", agent_instance_id=INSTANCE, agent_session_id=SESSION,
                session_label="Operator-Smoke", watcher_declared=True, operator_tmux_host=HOST,
            )
        client.close()


class LauncherTests(unittest.TestCase):
    def spec(self) -> launcher.LaunchSpec:
        return launcher.LaunchSpec(
            role="Operator-Smoke", solet="smoke", cwd=Path("/tmp"),
            bridge_cli=Path("/bin/bridge"), codex_binary=Path("/bin/codex"),
            codex_args=("-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="low"'),
        )

    def test_new_identity_and_explicit_settings(self) -> None:
        model, effort = launcher._explicit_model_effort(self.spec().codex_args)
        env = launcher._launch_environment(self.spec(), instance_id=INSTANCE, session_id=SESSION, model=model, effort=effort)
        self.assertEqual(env["AGENT_INSTANCE_ID"], INSTANCE)
        self.assertEqual(env["AGENT_SESSION_ID"], SESSION)
        self.assertEqual(env["AGENT_ROLE"], "Operator-Smoke")
        self.assertEqual(env["GIT_CONTROLLER_NAME"], "Git-Controller")
        self.assertTrue(Path(env["AGENT_WAKE_CLI"]).is_absolute())
        with self.assertRaises(launcher.OperatorLaunchError):
            launcher._explicit_model_effort(("-m", "gpt-5.6-sol"))

    def test_launch_environment_exposes_configured_bridge_to_the_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge_dir = Path(directory) / "bridge-bin"
            bridge_dir.mkdir()
            bridge = bridge_dir / "solet-bridge"
            bridge.write_text("#!/bin/sh\nexit 0\n")
            bridge.chmod(0o700)
            spec = dataclasses.replace(self.spec(), bridge_cli=bridge)
            with patch.dict(os.environ, {"PATH": "/usr/bin"}):
                environment = launcher._launch_environment(
                    spec, instance_id=INSTANCE, session_id=SESSION,
                    model="gpt-5.6-sol", effort="low",
                )
            self.assertEqual(environment["AGENT_WAKE_CLI"], str(bridge))
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(bridge_dir))
            resolved = subprocess.run(
                ["/bin/sh", "-c", "command -v solet-bridge"], env=environment,
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(resolved.stdout.strip(), str(bridge))

    def test_parent_thread_is_removed_and_explicit_operator_role_restored(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "old-thread", "AGENT_ROLE": "old-role"}):
            command = launcher._pane_command(self.spec(), HOST, Path("/tmp/watch.log"))
        self.assertEqual(command.count("-u CODEX_THREAD_ID"), 2)
        self.assertEqual(command.count("AGENT_ROLE=Operator-Smoke"), 2)
        self.assertNotIn("old-thread", command)
        self.assertNotIn("old-role", command)

    def test_owned_options_precede_and_preserve_caller_separator(self) -> None:
        prompt = "-leading spaces\nand $literal `text`"
        spec = dataclasses.replace(
            self.spec(),
            codex_args=("-m", "gpt-5.6-sol", "-c", 'model_reasoning_effort="low"', "--", prompt),
        )
        argv = launcher._codex_argv(spec)
        separator = argv.index("--")
        self.assertEqual(argv[separator + 1:], [prompt])
        self.assertLess(argv.index("--cd"), separator)
        self.assertLess(argv.index("mcp_servers.smoke.enabled=false"), separator)
        self.assertEqual(argv[1:5], list(spec.codex_args[:4]))

    def test_preflight_validates_exact_argv_before_host_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / "solet-bridge"
            codex = root / "codex"
            for executable in (bridge, codex):
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o700)
            prompt = "-literal $prompt `without shell`"
            spec = dataclasses.replace(
                self.spec(), bridge_cli=bridge, codex_binary=codex,
                codex_args=("-m", "qwen3-coder", "-c", 'model_reasoning_effort="low"', "--", prompt),
            )
            calls: list[list[str]] = []

            def checked(argv: list[str]) -> str:
                calls.append(argv)
                return "--operator-tmux-host" if argv[1] == "watch" else "Codex CLI"

            with patch.object(launcher.shutil, "which", return_value="tmux"), patch.object(
                launcher, "_checked", side_effect=checked,
            ):
                launcher._preflight(spec)
            expected = launcher._codex_argv(spec)
            separator = expected.index("--")
            self.assertEqual(calls[1], [*expected[:separator], "--help", *expected[separator:]])

    def test_real_codex_parser_accepts_composed_prompt_boundaries(self) -> None:
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("codex is unavailable")
        prompts = (
            ("plain prompt",),
            ("--", "-dash spaces\nand $literal `metacharacter`"),
        )
        for caller_tail in prompts:
            with self.subTest(caller_tail=caller_tail):
                spec = dataclasses.replace(
                    self.spec(), codex_binary=Path(codex),
                    codex_args=(
                        "-m", "qwen3-coder", "-c", 'model_reasoning_effort="low"', *caller_tail,
                    ),
                )
                argv = launcher._codex_argv(spec)
                separator = argv.index("--") if "--" in argv else len(argv)
                probe = [*argv[:separator], "--help", *argv[separator:]]
                result = subprocess.run(
                    probe, capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Codex CLI", result.stdout)

    def test_ephemeral_watcher_registers_without_claiming_a_role(self) -> None:
        regular = launcher._pane_command(self.spec(), HOST, Path("/tmp/watch.log"))
        ephemeral = launcher._pane_command(
            dataclasses.replace(self.spec(), claim_role=False), HOST, Path("/tmp/watch.log"),
        )
        self.assertNotIn("--no-claim", regular)
        watcher, codex = ephemeral.split(" & exec ", 1)
        self.assertIn("--no-claim", shlex.split(watcher))
        self.assertNotIn("--no-claim", shlex.split(codex))
        self.assertIn("--operator-tmux-host", shlex.split(watcher))

    def test_duplicate_refusal_does_not_stop_or_start_any_host(self) -> None:
        with patch.object(launcher, "_preflight", return_value=("tmux", "gpt-5.6-sol", "low")), patch.object(launcher, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(launcher, "_checked") as mutate, self.assertRaises(launcher.OperatorLaunchError):
            launcher.launch(self.spec())
        mutate.assert_not_called()

    def test_cleanup_refuses_a_replaced_host(self) -> None:
        with patch.object(launcher, "_run", return_value=subprocess.CompletedProcess([], 0, "agi-other\n", "")), patch.object(launcher, "_checked") as mutate:
            launcher._stop_failed_host("tmux", HOST, INSTANCE)
        mutate.assert_not_called()

    def test_failed_arm_stops_only_its_new_host_and_retains_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def identity_result(argv: list[str]) -> subprocess.CompletedProcess[str]:
                if "has-session" in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "")
                receipt = next(Path(directory).glob(".local/state/solet-operator-codex/*/launch.json"))
                instance = json.loads(receipt.read_text())["environment"]["AGENT_INSTANCE_ID"]
                return subprocess.CompletedProcess(argv, 0, "AGENT_INSTANCE_ID=" + instance + "\n", "")

            with patch.object(launcher, "_preflight", return_value=("tmux", "gpt-5.6-sol", "low")), patch.object(Path, "home", return_value=Path(directory)), patch.object(launcher, "_run", side_effect=identity_result), patch.object(launcher, "_checked", return_value="") as mutate, patch.object(launcher, "_wait_for_arm", side_effect=launcher.OperatorLaunchError("arm failed")), self.assertRaises(launcher.OperatorLaunchError):
                launcher.launch(self.spec())
            self.assertEqual(mutate.call_count, 2)
            self.assertIn("new-session", mutate.call_args_list[0].args[0])
            self.assertIn("kill-session", mutate.call_args_list[1].args[0])
            self.assertEqual(len(list(Path(directory).glob(".local/state/solet-operator-codex/*/launch.json"))), 1)
            self.assertEqual(len(list(Path(directory).glob(".local/state/solet-operator-codex/*/failure.json"))), 1)

    def test_missing_executable_refuses_without_creating_a_host(self) -> None:
        with patch.object(launcher, "_checked") as mutate, self.assertRaises(launcher.OperatorLaunchError):
            launcher._preflight(dataclasses.replace(self.spec(), bridge_cli=Path("/nonexistent/operator-bridge")))
        mutate.assert_not_called()

    def test_armed_line_requires_matching_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "watch.log"
            log.write_text(json.dumps({"watch": "armed", "agent_instance_id": "wrong"}))
            with self.assertRaises(launcher.OperatorLaunchError):
                launcher._armed(log, INSTANCE)
            log.write_text(json.dumps({"watch": "armed", "agent_instance_id": INSTANCE}))
            self.assertTrue(launcher._armed(log, INSTANCE))


if __name__ == "__main__":
    unittest.main()
