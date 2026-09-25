"""Host-singleton login rendering and real loopback loaded-state controls."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/github_midwife_plugin/src"))
sys.path.insert(0, str(ROOT / "solet_cli/src"))

from github_midwife_plugin.lm_studio_login_agent import LABEL, install_login_agent, login_classification, login_definition_current, login_paths, render_login_agent  # noqa: E402
from github_midwife_plugin.lm_studio_models import ModelArtifact, cli_path, model_loaded, reviewed_models, served_models  # noqa: E402
from github_midwife_plugin.lm_studio_provisioning import provision  # noqa: E402
from github_midwife_plugin.lm_studio_settings import disable_jit, settings_path  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import JsonObject, JsonValue  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, SystemRuntime, bounded_command_outcome  # noqa: E402
from lm_studio_provisioning_smoke import FixtureRuntime, outcome, request, write_artifact  # noqa: E402


def check_singleton() -> None:
    with tempfile.TemporaryDirectory(prefix="lm-login-") as temporary:
        runtime = FixtureRuntime(Path(temporary))
        models = reviewed_models(ROOT)
        plist_text, helper_text = render_login_agent(runtime.home, models)
        plist = plistlib.loads(plist_text.encode())
        assert plist["Label"] == LABEL and str(ROOT) not in plist_text + helper_text
        assert plist["ProgramArguments"] == ["/bin/sh", str(login_paths(runtime.home)[1])]
        assert "justInTimeModelLoading" in helper_text and "--context-length 8192" in helper_text
        assert login_classification(runtime, models) == "absent"
        assert install_login_agent(runtime, models)
        assert login_definition_current(runtime.home, models)
        assert login_classification(runtime, models) == "present_already_current"
        check_repeated_install(runtime, models, helper_text)


def check_repeated_install(runtime: FixtureRuntime, models: dict[str, ModelArtifact], helper_text: str) -> None:
    helper = login_paths(runtime.home)[1]
    subprocess.run(("/bin/sh", "-n", str(helper)), check=True)
    before = tuple(runtime.commands)
    assert install_login_agent(runtime, models)
    assert all(command[1] == "print" for command in runtime.commands[len(before):])
    helper.write_text(helper_text + "# drift\n")
    assert login_classification(runtime, models) == "present_but_stale"
    assert not install_login_agent(runtime, models)
    assert not any("bootout" in command or "unload" in command for command in runtime.commands)


class MissingGuiRuntime(FixtureRuntime):
    def _launchctl(self, argv: tuple[str, ...]) -> CommandOutcome:
        assert argv[1] == "print", argv
        return outcome(code=112, error=f"Bad request.\nCould not find domain for user gui: {os.getuid()}")


class GuiSessionLostRuntime(FixtureRuntime):
    def __init__(self, home: Path, absent_prints: int = 1) -> None:
        super().__init__(home)
        self.absent_prints = absent_prints
        self.print_count = 0

    def _launchctl(self, argv: tuple[str, ...]) -> CommandOutcome:
        if argv[1] == "print":
            self.print_count += 1
            if self.print_count <= self.absent_prints:
                return outcome(code=113, error=f'Could not find service "{LABEL}"')
        return outcome(code=112, error=f"Bad request.\nCould not find domain for user gui: {os.getuid()}")


class TruncatedSuccessRuntime(FixtureRuntime):
    def _launchctl(self, argv: tuple[str, ...]) -> CommandOutcome:
        assert argv[1] == "print", argv
        return bounded_command_outcome(
            returncode=0,
            timed_out=False,
            duration_ms=1,
            stdout=f"{LABEL} = {{\n state = running\n}}\n" + "x" * 5000,
            stderr="",
        )


class AmbiguousDomainRuntime(FixtureRuntime):
    def _launchctl(self, argv: tuple[str, ...]) -> CommandOutcome:
        return outcome(
            code=112,
            error=(
                f'Could not find service "{LABEL}"\n'
                f"Could not find domain for user gui: {os.getuid()}"
            ),
        )


class UnreadableGuiRuntime(FixtureRuntime):
    def _launchctl(self, argv: tuple[str, ...]) -> CommandOutcome:
        assert argv[1] == "print", argv
        return outcome(code=1, error="launchctl state unavailable")


def write_fixture_cli(home: Path) -> None:
    cli = cli_path(home)
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("fixture executable\n")
    cli.chmod(0o700)


def check_gui_session_required() -> None:
    reference = "setup::lm_studio.install_login_agent"
    with tempfile.TemporaryDirectory(prefix="lm-login-gui-") as temporary:
        home = Path(temporary)
        models = reviewed_models(ROOT)
        missing_gui = MissingGuiRuntime(home)
        assert login_classification(missing_gui, models) == "gui_session_absent"
        blocked = provision(request(reference, phase="apply"), missing_gui)
        assert blocked["checkpoint_status"] == "blocked"
        assert blocked["error_kind"] == "lm_studio_login_agent_gui_session_required"
        assert "graphical desktop" in blocked["repair"]
        assert not any(path.exists() for path in login_paths(home))
        assert all(command[1] == "print" for command in missing_gui.commands)


def check_gui_session_lost_during_install() -> None:
    reference = "setup::lm_studio.install_login_agent"
    with tempfile.TemporaryDirectory(prefix="lm-login-gui-loss-") as temporary:
        home = Path(temporary)
        models = reviewed_models(ROOT)
        write_fixture_cli(home)
        lost_gui = GuiSessionLostRuntime(home)
        blocked = provision(request(reference, phase="apply"), lost_gui)
        assert blocked["checkpoint_status"] == "blocked"
        assert blocked["error_kind"] == "lm_studio_login_agent_unavailable"
        assert not any(path.exists() for path in login_paths(home))
        assert [command[1] for command in lost_gui.commands] == ["print", "print"]

        missing_gui = MissingGuiRuntime(home)
        assert not install_login_agent(missing_gui, models)
        assert not any(path.exists() for path in login_paths(home))
        assert [command[1] for command in missing_gui.commands] == ["print"]


def check_stale_gui_session_lost(*, direct: bool) -> None:
    reference = "setup::lm_studio.install_login_agent"
    with tempfile.TemporaryDirectory(prefix="lm-login-stale-gui-loss-") as temporary:
        home = Path(temporary)
        models = reviewed_models(ROOT)
        assert install_login_agent(FixtureRuntime(home), models)
        plist, helper = login_paths(home)
        plist.write_text("stale plist\n")
        helper.write_text("stale helper\n")
        before = (plist.read_bytes(), helper.read_bytes())
        write_fixture_cli(home)
        lost_gui = GuiSessionLostRuntime(home, absent_prints=1 if direct else 2)
        if direct:
            assert not install_login_agent(lost_gui, models)
        else:
            blocked = provision(request(reference, phase="apply"), lost_gui)
            assert blocked["checkpoint_status"] == "blocked"
            assert blocked["error_kind"] == "lm_studio_login_agent_unavailable"
        assert (plist.read_bytes(), helper.read_bytes()) == before
        assert [command[1] for command in lost_gui.commands] == ["print"] * (2 if direct else 3)


def check_truncated_login_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="lm-login-truncated-") as temporary:
        home = Path(temporary)
        models = reviewed_models(ROOT)
        assert install_login_agent(FixtureRuntime(home), models)
        assert_unknown_login_probe(TruncatedSuccessRuntime(home), models)


def check_ambiguous_gui_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="lm-login-ambiguous-") as temporary:
        home = Path(temporary)
        models = reviewed_models(ROOT)
        write_fixture_cli(home)
        assert_unknown_login_probe(AmbiguousDomainRuntime(home), models)


def check_unreadable_login_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="lm-login-unknown-") as temporary:
        home = Path(temporary)
        models = reviewed_models(ROOT)
        assert_unknown_login_probe(UnreadableGuiRuntime(home), models)


def assert_unknown_login_probe(runtime: FixtureRuntime, models: dict[str, ModelArtifact]) -> None:
    paths = login_paths(runtime.home)
    before = tuple(path.read_bytes() if path.exists() else None for path in paths)
    assert login_classification(runtime, models) == "unknown"
    blocked = provision(request("setup::lm_studio.install_login_agent", phase="apply"), runtime)
    assert blocked["checkpoint_status"] == "blocked"
    assert blocked["error_kind"] == "lm_studio_login_agent_valid_unknown"
    assert tuple(path.read_bytes() if path.exists() else None for path in paths) == before
    assert all(command[1] == "print" for command in runtime.commands)


def check_valid_and_invalid_login_item() -> None:
    reference = "setup::lm_studio.install_login_agent"
    with tempfile.TemporaryDirectory(prefix="lm-login-item-") as temporary:
        home = Path(temporary)
        models = reviewed_models(ROOT)
        valid = FixtureRuntime(home)
        assert install_login_agent(valid, models)
        before = tuple(valid.commands)
        assert provision(request(reference, phase="apply"), valid)["checkpoint_status"] == "applied"
        assert all(command[1] == "print" for command in valid.commands[len(before):])
        write_fixture_cli(home)
        login_paths(home)[1].write_text("stale helper\n")
        before = tuple(valid.commands)
        invalid = provision(request(reference, phase="apply"), valid)
        assert invalid["checkpoint_status"] == "blocked"
        assert invalid["error_kind"] == "lm_studio_login_agent_unavailable"
        assert login_paths(home)[1].read_text() == "stale helper\n"
        assert all(command[1] == "print" for command in valid.commands[len(before):])


class ModelsHandler(BaseHTTPRequestHandler):
    state: str | None = "not-loaded"

    def do_GET(self) -> None:  # noqa: N802
        row: JsonObject = {"id": "qwen3-14b"}
        if self.path == "/api/v0/models" and self.state is not None:
            row["state"] = self.state
        body = json.dumps({"data": [row]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class LoopbackRuntime(SystemRuntime):
    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port

    def http_json(self, url: str, *, timeout_seconds: int, payload: JsonObject | None = None) -> tuple[int, JsonValue]:
        assert url.startswith("http://127.0.0.1:1234/")
        return super().http_json(url.replace(":1234/", f":{self.port}/"), timeout_seconds=timeout_seconds, payload=payload)


def check_loopback() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime = LoopbackRuntime(server.server_port)
        assert served_models(runtime) == ("qwen3-14b",)
        assert model_loaded(runtime, "qwen3-14b") is False
        ModelsHandler.state = "loaded"
        assert model_loaded(runtime, "qwen3-14b") is True
        assert model_loaded(runtime, "qwen/qwen3-14b") is False
        ModelsHandler.state = None
        assert model_loaded(runtime, "qwen3-14b") is None
        ModelsHandler.state = "unexpected"
        assert model_loaded(runtime, "qwen3-14b") is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def main() -> int:
    check_singleton()
    check_gui_session_required()
    check_gui_session_lost_during_install()
    check_stale_gui_session_lost(direct=True)
    check_stale_gui_session_lost(direct=False)
    check_truncated_login_probe()
    check_ambiguous_gui_probe()
    check_unreadable_login_probe()
    check_valid_and_invalid_login_item()
    check_loopback()
    check_executed_helper()
    print("lm_studio_login_item_smoke: singleton, valid/stale/unknown GUI states, missing GUI repair, race and stale-definition guards, truncated and ambiguous probes, no teardown, cold boot and invalid-state controls passed")
    return 0


def check_executed_helper() -> None:
    with tempfile.TemporaryDirectory(prefix="lm-helper-") as temporary:
        runtime = FixtureRuntime(Path(temporary))
        models = reviewed_models(ROOT)
        for model in models.values():
            write_artifact(runtime.home, model)
        assert disable_jit(runtime)
        helper, responses, trace = _fixture_helper(runtime, models)
        for state in ("loaded", "not-loaded", "empty", "unknown", "missing-state"):
            responses.write_text(json.dumps(_helper_payload(models, state)))
            trace.write_text("")
            completed = subprocess.run(("/bin/sh", str(helper)), capture_output=True, text=True, check=False, timeout=10)
            commands = [json.loads(line) for line in trace.read_text().splitlines()]
            loads = [command for command in commands if command[0] == "load"]
            _check_helper_state(state, completed.returncode, loads, models, completed.stderr)
        helper_text = helper.read_text()
        assert "/api/v0/models/$1" not in helper_text
        assert 'http://127.0.0.1:1234/api/v0/models"' in helper_text
        _check_string_jit_refusal(runtime, models, helper, responses, trace)


def _check_string_jit_refusal(runtime: FixtureRuntime, models: dict[str, ModelArtifact], helper: Path, responses: Path, trace: Path) -> None:
    settings_path(runtime.home).write_text('{"justInTimeModelLoading":"false"}')
    responses.write_text(json.dumps(_helper_payload(models, "not-loaded")))
    trace.write_text("")
    completed = subprocess.run(("/bin/sh", str(helper)), capture_output=True, text=True, check=False, timeout=10)
    assert completed.returncode != 0 and trace.read_text() == "", "a string JIT value must refuse before any daemon or server command"


def _helper_payload(models: dict[str, ModelArtifact], state: str) -> dict[str, JsonObject]:
    states: dict[str, str] = {model.api_identifier: state for model in models.values()}
    if state == "empty":
        states = {}
    if state == "missing-state":
        return {"rows": [{"id": model.api_identifier} for model in models.values()]}
    return {"states": states}


def _check_helper_state(state: str, code: int, loads: list[list[str]], models: dict[str, ModelArtifact], stderr: str) -> None:
    if state == "loaded":
        assert code == 0 and loads == [], f"bootstrap must not create duplicate model instances: {stderr}"
    elif state in ("not-loaded", "empty"):
        assert code == 0 and loads == [list(model.load_argv) for model in models.values()]
    else:
        assert code != 0 and loads == [], "unknown state cannot trigger speculative loading"


def _fixture_helper(runtime: FixtureRuntime, models: dict[str, ModelArtifact]) -> tuple[Path, Path, Path]:
    responses, trace = runtime.home / "responses.json", runtime.home / "commands.jsonl"
    lms, curl = cli_path(runtime.home), runtime.home / "curl-fixture"
    lms.parent.mkdir(parents=True, exist_ok=True)
    header = f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
    lms.write_text(
        header
        + f"responses=Path({str(responses)!r})\noutput=Path({str(trace)!r})\n"
        + "payload=json.loads(responses.read_text())\n"
        + "if sys.argv[1:2] == ['load']:\n    payload.setdefault('states', {})[sys.argv[2]] = 'loaded'\n    responses.write_text(json.dumps(payload))\n"
        + "output.write_text(output.read_text() + json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    curl.write_text(
        header
        + f"payload=json.loads(Path({str(responses)!r}).read_text())\nurl=sys.argv[-1]\n"
        + "if url.endswith('/api/v0/models'):\n"
        + "    rows=payload.get('rows')\n"
        + "    if rows is None:\n        rows=[{'id': identifier, 'state': state} for identifier, state in payload.get('states', {}).items()]\n"
        + "    print(json.dumps({'data': rows}))\n"
        + "elif '/api/v0/models/' in url:\n    print('per-item endpoint is unavailable during cold start', file=sys.stderr)\n    raise SystemExit(22)\n"
        + "else:\n    print(json.dumps({'data': []}))\n"
    )
    lms.chmod(0o700)
    curl.chmod(0o700)
    _, text = render_login_agent(runtime.home, models)
    helper = runtime.home / "start-fixture.sh"
    helper.write_text(text.replace("/usr/bin/curl", shlex.quote(str(curl))))
    return helper, responses, trace


if __name__ == "__main__":
    raise SystemExit(main())
