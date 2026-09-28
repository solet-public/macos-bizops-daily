"""Fixture runtime and requests shared by the llama.cpp setup smokes (iss_3a2a74ea).

launchd, executable lookup and loopback health are in memory; files land in the
caller's temporary home and target.  No host command runs.
"""

from __future__ import annotations

import os
import plistlib
import uuid
from pathlib import Path

from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject, JsonValue
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome

SERVER = "/opt/homebrew/bin/llama-server"
BOTH: JsonObject = {"embeddings_implementation": "llama_cpp", "inference_implementation": "llama_cpp"}


class FixtureRuntime:
    """launchd, executable lookup and loopback health, all in memory."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.loaded: set[str] = set()
        self.serving: set[int] = set()
        self.gui_session = True
        self.server_installed = True
        self.commands: list[tuple[str, ...]] = []

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
        self.commands.append(argv)
        if argv[0] == "/usr/bin/which":
            found = self.server_installed and argv[1] in {"llama-server", SERVER}
            return CommandOutcome(0 if found else 1, False, 1, SERVER + "\n" if found else "", "")
        if argv[:2] == ("/bin/launchctl", "print"):
            return self._print(argv[2])
        if argv[:2] == ("/bin/launchctl", "bootstrap"):
            self.loaded.add(plistlib.loads(Path(argv[3]).read_bytes())["Label"])
            return CommandOutcome(0, False, 1, "", "")
        if argv[:2] == ("/bin/launchctl", "bootout"):
            self.loaded.discard(argv[2].rsplit("/", 1)[1])
            return CommandOutcome(0, False, 1, "", "")
        if argv[:2] == ("/bin/launchctl", "enable"):
            return CommandOutcome(0, False, 1, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def _print(self, service: str) -> CommandOutcome:
        uid = os.getuid()
        if not self.gui_session:
            return CommandOutcome(112, False, 1, "", f"Could not find domain for user gui: {uid}\n")
        label = service.rsplit("/", 1)[1]
        if label in self.loaded:
            return CommandOutcome(0, False, 1, f"{service} = {{\n\tstate = running\n\tlabel = {label}\n}}\n", "")
        return CommandOutcome(113, False, 1, "", f"Could not find service \"{label}\" in domain for user gui: {uid}\n")

    def http_json(self, url: str, *, timeout_seconds: int, payload: JsonObject | None = None) -> tuple[int, JsonValue]:
        del timeout_seconds, payload
        port = int(url.split(":")[2].split("/")[0])
        if port not in self.serving:
            raise ConnectionRefusedError(url)
        return 200, {"status": "ok"}

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)

    def mutations(self) -> list[tuple[str, ...]]:
        return [argv for argv in self.commands if argv[:2] in {("/bin/launchctl", "bootstrap"), ("/bin/launchctl", "bootout")}]


def request(target: Path, reference: str, *, phase: str, inputs: JsonObject | None = None) -> AdapterRequest:
    return AdapterRequest(
        request_id=str(uuid.uuid4()),
        operation_id="llama_cpp_fixture",
        operation_ref=reference,
        phase=phase,
        probe_purpose="preview" if phase == "probe" else None,
        attempt=1,
        name="tahoe-fixture",
        target=target,
        flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64,
        approval_fingerprint=None,
        dry_run=phase == "probe",
        timeout_seconds=60,
        public_inputs=dict(BOTH if inputs is None else inputs),
    )
