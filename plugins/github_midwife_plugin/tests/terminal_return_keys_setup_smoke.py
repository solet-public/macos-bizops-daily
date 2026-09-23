"""Offline preview/apply contract for the managed terminal Return-key setup."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))

from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_operations import (  # noqa: E402
    _ITERM_RETURN_KEY_PROFILE_RELATIVE,
    _TMUX_RETURN_KEY_BLOCK,
    operation_handlers,
)

_CHECKS = 0


class _Runtime:
    def __init__(self, home: Path) -> None:
        self.home = home

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)


def _check(condition: bool, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {label}")


def _request(*, phase: str) -> AdapterRequest:
    return AdapterRequest.from_dict(
        {
            "protocol_version": 1,
            "kind": "operation_request",
            "request_id": "8f2f3ed3-03fc-4f58-915e-eb400a172a67",
            "operation_id": "configure-terminal-return-keys",
            "operation_ref": "setup::terminal.configure_return_keys",
            "phase": phase,
            "probe_purpose": "preview" if phase == "probe" else None,
            "attempt": 1,
            "name": "terminal-census",
            "target": "/Users/example/Solets/terminal-census",
            "flow_id": "macos.repository_setup",
            "flow_source_revision": "a" * 40,
            "answers_fingerprint": "sha256:" + "2" * 64,
            "approval_fingerprint": None if phase == "probe" else "sha256:" + "3" * 64,
            "dry_run": phase == "probe",
            "timeout_seconds": 30,
            "public_inputs": {},
        }
    )


def main() -> int:
    handler = operation_handlers().get("setup::terminal.configure_return_keys")
    _check(handler is not None, "flow operation has a registered implementation")
    flow = json.loads(
        (
            _ROOT
            / "plugins"
            / "github_midwife_plugin"
            / "knowledge_base"
            / "macos_setup_flow.json"
        ).read_text(encoding="utf-8")
    )
    operation_refs = flow["stages"]["system_dependencies"]["operation_refs"]
    _check(
        operation_refs.index("configure_terminal_return_keys")
        < operation_refs.index("install_tmux"),
        "terminal Return-key operation precedes tmux so the LM Studio paired-flow suffix stays intact",
    )
    assert handler is not None
    with tempfile.TemporaryDirectory() as temporary:
        runtime = _Runtime(Path(temporary))
        tmux_path = runtime.home / ".tmux.conf"
        tmux_path.write_text("set -g mouse on\n", encoding="utf-8")
        preview = handler(_request(phase="probe"), runtime)
        _check(preview["checkpoint_status"] == "pending", "drift receives a reviewed preview")
        _check(len(preview["planned_actions"]) == 2, "preview names tmux and iTerm writes")
        _check("Restart iTerm2" in str(preview["repair"]), "preview carries restart prompt")

        applied = handler(_request(phase="apply"), runtime)
        _check(applied["checkpoint_status"] == "applied", "approved operation applies")
        tmux_text = tmux_path.read_text(encoding="utf-8")
        _check("set -g mouse on" in tmux_text, "existing tmux settings survive merge")
        _check(_TMUX_RETURN_KEY_BLOCK in tmux_text, "exact managed tmux block written")
        profile = json.loads((runtime.home / _ITERM_RETURN_KEY_PROFILE_RELATIVE).read_text())
        _check(profile["Profiles"][0]["Option Key Sends"] == 2, "iTerm profile sends Escape")

        verified = handler(_request(phase="probe"), runtime)
        _check(verified["checkpoint_status"] == "verified", "repeat preview is idempotently verified")
        _check(verified["planned_actions"] == [], "verified preview names no writes")
    print(f"terminal_return_keys_setup_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
