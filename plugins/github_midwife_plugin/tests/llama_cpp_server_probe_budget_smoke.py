"""The llama-server availability probe spends the request budget, not a fixed 10 s.

On a fresh macOS 26 guest the first `llama-server --version` initializes
ggml's Metal backend and took longer than 10 s, so setup stopped at
install_llama_cpp although the package was installed (iss_e8d7c188).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "plugins/github_midwife_plugin/src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.installation_doctor import _llama_server_available  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject, JsonValue  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402

_SERVER = "/opt/homebrew/bin/llama-server"


class _RecordingRuntime:
    """Resolves llama-server and answers its version call after a fixed duration."""

    def __init__(self, version_seconds: float) -> None:
        self.home = Path("/fixture/home")
        self.version_seconds = version_seconds
        self.version_timeouts: list[int] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = 65536,
    ) -> CommandOutcome:
        del cwd, extra_env, input_text, output_limit
        if argv[0] == "/usr/bin/which":
            found = argv[1] in {"llama-server", _SERVER}
            return CommandOutcome(0 if found else 1, False, 1, f"{_SERVER}\n" if found else "", "")
        if argv == (_SERVER, "--version"):
            self.version_timeouts.append(timeout_seconds)
            timed_out = self.version_seconds > timeout_seconds
            duration = int(min(self.version_seconds, timeout_seconds) * 1000)
            return CommandOutcome(None if timed_out else 0, timed_out, duration, "" if timed_out else "version: 0.4.0\n", "")
        raise AssertionError(f"unexpected command {argv!r}")

    def http_json(self, url: str, *, timeout_seconds: int, payload: JsonObject | None = None) -> tuple[int, JsonValue]:
        raise AssertionError(f"unexpected http call {url!r} {timeout_seconds} {payload!r}")

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        raise AssertionError(f"unexpected write {path} {len(content)} {mode}")


def _request(timeout_seconds: int) -> AdapterRequest:
    return AdapterRequest(
        "11111111-1111-4111-8111-111111111111",
        "llama_cpp_server_available",
        "setup::llama_cpp.server_available",
        "probe",
        "postcondition",
        1,
        "fixture",
        Path("/fixture/target"),
        "a" * 40,
        "sha256:" + "b" * 64,
        None,
        True,
        timeout_seconds,
        {},
    )


def _check(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def main() -> int:
    # The r54 guest: about 15 s for the first --version, under the default 30 s budget.
    slow = _RecordingRuntime(version_seconds=15.0)
    result = _llama_server_available(_request(30), slow)
    _check(slow.version_timeouts == [25], f"30 s budget leaves 25 s for --version: {slow.version_timeouts}")
    _check(result["checkpoint_status"] == "verified", f"15 s first launch verifies: {result['checkpoint_status']}")

    # A server that never answers inside the budget still blocks, with no retry.
    hung = _RecordingRuntime(version_seconds=600.0)
    blocked = _llama_server_available(_request(30), hung)
    _check(hung.version_timeouts == [25], "a hung server is run once")
    _check(blocked["checkpoint_status"] == "blocked", "a hung server still blocks")

    # A tiny budget never becomes a zero or negative subprocess timeout.
    tiny = _RecordingRuntime(version_seconds=0.1)
    _llama_server_available(_request(3), tiny)
    _check(tiny.version_timeouts == [1], f"budget floor is 1 s: {tiny.version_timeouts}")
    print("llama_cpp_server_probe_budget_smoke: 6 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
