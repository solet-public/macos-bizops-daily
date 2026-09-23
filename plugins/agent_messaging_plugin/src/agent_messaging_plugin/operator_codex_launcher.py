"""Operator-present interactive Codex launcher with qualified native waking.

The operator's named shell function invokes this module. It never provisions a
worker worktree or replaces an existing role holder. Logs and failed-launch
receipts are retained; only this invocation's newly created host may be stopped
when qualification fails. Existing sessions require an explicit later handoff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .codex_common import _without_parent_runtime_env
from .solet_cli import expose_worker_cli


class OperatorLaunchError(RuntimeError):
    """The operator launch did not establish the declared runtime contract."""


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Operator-supplied launch intent; models and effort are never inherited."""

    role: str
    solet: str
    cwd: Path
    bridge_cli: Path
    codex_binary: Path
    codex_args: tuple[str, ...]
    claim_role: bool = True


def _explicit_model_effort(arguments: tuple[str, ...]) -> tuple[str, str]:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("-m", "--model")
    parser.add_argument("-c", "--config", action="append", default=[])
    known, _ = parser.parse_known_args(arguments)
    effort = ""
    for item in known.config:
        values = tomllib.loads(item)
        if "model_reasoning_effort" in values:
            effort = str(values["model_reasoning_effort"])
    if not known.model or effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise OperatorLaunchError("operator launcher requires an explicit model and reasoning effort")
    return str(known.model), effort


def _host_name(solet: str, role: str) -> str:
    digest = hashlib.sha256(json.dumps([solet, role]).encode()).hexdigest()[:32]
    return "operator-codex-" + digest


def _binary(path: Path) -> str:
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise OperatorLaunchError(f"required absolute executable is unavailable: {path}")
    return str(path)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorLaunchError(f"launcher command failed: {argv[0]}: {exc}") from exc


def _checked(argv: list[str]) -> str:
    result = _run(argv)
    if result.returncode:
        raise OperatorLaunchError(f"launcher command failed: {result.stderr.strip()}")
    return result.stdout


def _preflight(spec: LaunchSpec) -> tuple[str, str, str]:
    if not spec.role.strip() or not spec.solet.strip() or not spec.cwd.is_dir():
        raise OperatorLaunchError("operator role, solet and existing checkout are required")
    _binary(spec.codex_binary)
    _binary(spec.bridge_cli)
    tmux = shutil.which("tmux")
    if tmux is None:
        raise OperatorLaunchError("tmux is required for interactive Codex waking")
    help_text = _checked([str(spec.bridge_cli), "watch", "--help"])
    if "--operator-tmux-host" not in help_text:
        raise OperatorLaunchError("installed bridge CLI lacks operator host qualification; update it first")
    model, effort = _explicit_model_effort(spec.codex_args)
    _validate_codex_argv(spec)
    return tmux, model, effort


def _launch_environment(
    spec: LaunchSpec, *, instance_id: str, session_id: str, model: str, effort: str,
) -> dict[str, str]:
    """Replace borrowed session identity with a fresh, complete operator pair."""
    environment = {
        "SOLET_NAME": spec.solet,
        "AGENT_INSTANCE_ID": instance_id,
        "AGENT_SESSION_ID": session_id,
        "AGENT_SESSION_LABEL": spec.role,
        "AGENT_ROLE": spec.role,
        "AGENT_IDENTITY": "codex",
        "AGENT_ROLE_AUTOBIND": "0",
        "AGENT_WAKE_CLI": str(spec.bridge_cli),
        "FLEET_TRANSPORT": "watch",
        "GIT_CONTROLLER_NAME": "Git-Controller",
        "OPERATOR_CODEX_MODEL": model,
        "OPERATOR_CODEX_EFFORT": effort,
        "OPERATOR_CODEX_CWD": str(spec.cwd.resolve()),
    }
    expose_worker_cli(environment, str(spec.bridge_cli))
    return environment


def _codex_argv(spec: LaunchSpec) -> list[str]:
    """Place launcher-owned Codex options before a caller's ``--`` separator."""
    owned = [
        "-c", f"mcp_servers.{spec.solet}.enabled=false",
        "--cd", str(spec.cwd.resolve()),
    ]
    args = list(spec.codex_args)
    try:
        separator = args.index("--")
    except ValueError:
        return [str(spec.codex_binary), *args, *owned]
    return [str(spec.codex_binary), *args[:separator], *owned, *args[separator:]]


def _validate_codex_argv(spec: LaunchSpec) -> None:
    """Exercise the exact launch argv through Codex's safe parser-help path."""
    command = _codex_argv(spec)
    try:
        separator = command.index("--")
    except ValueError:
        separator = len(command)
    _checked([*command[:separator], "--help", *command[separator:]])


def _pane_command(spec: LaunchSpec, host: str, log_path: Path) -> str:
    watcher = shlex.join(_without_parent_runtime_env([
        "/usr/bin/env", f"AGENT_ROLE={spec.role}",
        str(spec.bridge_cli), "watch", "--agent-id", "codex", "--role", spec.role,
        "--operator-tmux-host", host,
        *([] if spec.claim_role else ["--no-claim"]),
    ]))
    # The watcher must be a child of the pane's exec'd Codex process. Its
    # first registration can retry while the shell is still entering exec.
    watch = f"{watcher} --exit-with-parent $$ >{shlex.quote(str(log_path))} 2>&1 & "
    # Reuse managed Codex's parent-runtime cleanup, then restore only the
    # operator's explicitly selected role. In particular, never carry a
    # predecessor's CODEX_THREAD_ID into a newly minted logical session.
    command = _without_parent_runtime_env([
        "/usr/bin/env", f"AGENT_ROLE={spec.role}",
        *_codex_argv(spec),
    ])
    return watch + "exec " + shlex.join(command)


def _new_host_command(
    tmux: str, spec: LaunchSpec, host: str, environment: dict[str, str], log_path: Path,
) -> list[str]:
    command = [tmux, "new-session", "-d", "-s", host, "-c", str(spec.cwd.resolve())]
    for key, value in environment.items():
        command.extend(["-e", f"{key}={value}"])
    command.append(_pane_command(spec, host, log_path))
    return command


def _armed(log_path: Path, instance_id: str) -> bool:
    if not log_path.exists():
        return False
    for line in log_path.read_text().splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("watch") == "armed":
            if value.get("agent_instance_id") != instance_id:
                raise OperatorLaunchError("watcher armed under an unexpected instance identity")
            return True
    return False


def _wait_for_arm(tmux: str, host: str, instance_id: str, log_path: Path) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if _run([tmux, "has-session", "-t", "=" + host]).returncode:
            raise OperatorLaunchError(f"Codex exited before watcher qualification; see {log_path}")
        if _armed(log_path, instance_id):
            return
        time.sleep(0.2)
    raise OperatorLaunchError(f"watcher did not qualify within 45 seconds; see {log_path}")


def _stop_failed_host(tmux: str, host: str, instance_id: str) -> None:
    """Clean up only the host this invocation created, with fresh identity proof."""
    identity = _run([
        tmux, "show-environment", "-t", "=" + host, "AGENT_INSTANCE_ID",
    ])
    if identity.returncode == 0 and identity.stdout.strip() == f"AGENT_INSTANCE_ID={instance_id}":
        _checked([tmux, "kill-session", "-t", "=" + host])


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    with path.open("x") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def launch(spec: LaunchSpec, *, output: TextIO = sys.stdout) -> str:
    """Create a fresh qualified operator host; refuse all duplicate hosts."""
    tmux, model, effort = _preflight(spec)
    host = _host_name(spec.solet, spec.role)
    if _run([tmux, "has-session", "-t", "=" + host]).returncode == 0:
        raise OperatorLaunchError(
            f"this role already has an operator host; inspect or attach with: tmux attach -t ={host}",
        )
    instance_id = "agi-" + uuid.uuid4().hex
    session_id = "ases-" + uuid.uuid4().hex
    environment = _launch_environment(
        spec, instance_id=instance_id, session_id=session_id, model=model, effort=effort,
    )
    directory = Path.home() / ".local" / "state" / "solet-operator-codex" / instance_id
    directory.mkdir(parents=True, mode=0o700)
    log_path = directory / "watch.log"
    _write_receipt(directory / "launch.json", {
        "host_ref": host, "environment": environment,
        "codex_args": list(spec.codex_args), "status": "qualification_pending",
    })
    # new-session is the atomic duplicate guard; a racing refusal must not
    # terminate the winner. Cleanup starts only after our own create succeeds.
    _checked(_new_host_command(tmux, spec, host, environment, log_path))
    try:
        _wait_for_arm(tmux, host, instance_id, log_path)
    except (OperatorLaunchError, KeyboardInterrupt) as exc:
        _write_receipt(directory / "failure.json", {"error": str(exc)})
        _stop_failed_host(tmux, host, instance_id)
        raise
    _write_receipt(directory / "qualified.json", {
        "host_ref": host, "agent_instance_id": instance_id, "agent_session_id": session_id,
        "qualification": "watcher_armed_after_verified_host_registration",
    })
    print(f"{spec.role}: watcher and native host registered; {directory}", file=output, flush=True)
    return host


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--role", required=True)
    parser.add_argument("--solet", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--bridge-cli", required=True, type=Path)
    parser.add_argument("--codex-binary", required=True, type=Path)
    parser.add_argument("--no-role-claim", action="store_true")
    parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    codex_args = args.codex_args[1:] if args.codex_args[:1] == ["--"] else args.codex_args
    spec = LaunchSpec(
        role=args.role, solet=args.solet, cwd=args.cwd,
        bridge_cli=args.bridge_cli, codex_binary=args.codex_binary,
        codex_args=tuple(codex_args),
        claim_role=not args.no_role_claim,
    )
    try:
        host = launch(spec)
        tmux = shutil.which("tmux")
        if tmux is None:
            raise OperatorLaunchError("tmux disappeared after qualification")
        os.execv(tmux, [tmux, "attach-session", "-t", "=" + host])
    except (OperatorLaunchError, ValueError, OSError) as exc:
        print(f"operator Codex launch refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
