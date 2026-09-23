# pyright: reportUnusedFunction=false
"""Shared configuration and identity helpers for managed Codex drivers."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Hoisted to the runner-neutral module (2026-08-14, registration-loss RCA):
# these were never Codex-specific, and every spawned CLAUDE worker needed the
# identical treatment -- that asymmetry is what left Claude workers
# unregistered. Aliased to the established private names so no Codex call site
# moves; `resolve_solet_bin` is now imported from `solet_cli` directly by the
# drivers that need it, since a re-export through here reads as ownership it
# no longer has.
from .solet_cli import expose_worker_cli as _expose_worker_cli
from .solet_cli import worker_path as _worker_path

_CODEX_AGENT_ID = "codex"
_COORDINATION_PLUGIN_PREFIX = "coordination-hooks@"
_DEFAULT_RPC_TIMEOUT_SECONDS = 30.0
_DEFAULT_TMUX_VERIFY_TIMEOUT_SECONDS = 10.0
_DEFAULT_TMUX_POLL_INTERVAL_SECONDS = 0.25
_DEFAULT_TMUX_STABLE_SAMPLES = 3
_MIN_TMUX_VERSION = (3, 3)
_MCP_ROLE_AUTOBIND_ENV = "AGENT_ROLE_AUTOBIND"
_CODEX_PARENT_SESSION_ENV = "CODEX_THREAD_ID"
_CLAUDE_ONLY_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "CLAUDE_CODE_",
    "CLOUD_ML_",
    "GOOGLE_",
    "VERTEX_",
)
_CLAUDE_ONLY_ENV_NAMES = {
    "CLAUDECODE",
    "FLEET_HEADLESS_PERMISSION_MODE",
    "FLEET_HEADLESS_TOOL_ALLOWLIST",
}
_CLAUDE_PROVIDER_ENV_NAMES = {
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
}
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_TOML_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_CODEX_BUSY_STATUS_RE = re.compile(
    r"^[ \t]*\u2022[ \t]+[^\n]*\([^()\n]*\d[^()\n]*esc to interrupt[^()\n]*\)[^\n]*",
    re.MULTILINE,
)
"""The Codex TUI's live busy status line, matched by SHAPE rather than by any
bare word it happens to contain -- live-measured 2026-09-09 against a running
``fleet-lane-cdx-fix-lm-studio-provisioning-2026-09-08-f0f3ca54`` pane
(Codex 0.153.4), which rendered exactly::

    \u2022 Working (12m 07s \u00b7 esc to interrupt) \u00b7 Checking peer inbox before ending turn

The previous check was a tuple of bare substrings, one of which was the word
``Working``. That word is matched anywhere in the visible pane, transcript
prose included, so an IDLE pane whose visible text merely CONTAINED the word
was judged busy forever: measured against this lane's own dispatch brief, the
line ``**Working dir:** ~/Workspace/example`` was enough to make a healthy
idle pane permanently undriveable, silently, while it stayed registered,
heartbeat, and read ``live``. A pane displaying this very module's source was
self-wedging for the same reason. This pattern instead requires the leading
status bullet AND a parenthetical carrying both an elapsed-time digit and the
interrupt hint -- a shape the TUI renders and ordinary prose does not.

Narrowing a busy check is normally the risky direction, so note which way this
one fails. A false BUSY is unrecoverable: the pane can never be driven again.
A false READY is caught one step later and undone -- ``_wait_until_stable``
requires N identical consecutive captures, and a genuinely busy pane's own
ticking elapsed-time counter changes the screen every second, so the insert
raises and ``_rollback_failed_insert`` clears the composer. The asymmetry is
what justifies matching precisely here rather than broadly."""
_CODEX_STARTUP_STATUS_RE = re.compile(
    r"^[ \t]*[\u2022\u00b7]?[ \t]*Starting MCP servers\b[^\n]*",
    re.MULTILINE,
)
"""Codex's pre-prompt startup phase, anchored to its own status line for the
same reason as :data:`_CODEX_BUSY_STATUS_RE`."""
_CODEX_COMPOSER_PROMPT = "› "
_CODEX_IDLE_COMPOSER_PLACEHOLDER = "› Ask Codex to do anything"
"""The empty-composer placeholder line the Codex TUI renders only while idle
and waiting for input -- live-measured 2026-08-23 against a running
``fleet-lane-c-inspect-target-3ab14add`` pane (Codex v0.149.0), captured both
at its first idle turn (banner still visible) and after several turns once
the ``OpenAI Codex (v...)`` startup-banner box had scrolled out of the
visible pane. That banner was the PREVIOUS readiness marker; unlike it, this
placeholder reappears every time the composer goes idle regardless of scroll
position, and was not observed anywhere else in over 900 lines of that pane's
scrollback (no false-positive risk from quoted/transcript text).

Necessary but NOT sufficient on its own -- see
:meth:`_CodexTmuxDriverChannel._wait_until_ready`. Re-measured 2026-09-09
against Codex 0.153.4: this placeholder is rendered whenever the composer is
EMPTY, mid-turn included, so it cannot decide readiness by itself; the
absence of a busy status line is the other half."""


def codex_busy_reason(visible: str) -> str | None:
    """The Codex status line proving the pane is mid-turn, or ``None`` if idle.

    Returns the matched text so callers can say WHY a pane was not driveable
    instead of reporting the same opaque failure for a busy pane and a broken
    one -- the ambiguity that made a healthy, working lane read as a delivery
    failure.
    """
    for pattern in (_CODEX_BUSY_STATUS_RE, _CODEX_STARTUP_STATUS_RE):
        match = pattern.search(visible)
        if match is not None:
            return match.group(0).strip()
    return None


def _toml_string(value: str) -> str:
    """JSON string syntax is valid TOML basic-string syntax for this input."""
    return json.dumps(value)


def _codex_home(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".codex"


def _read_codex_config(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data


def _coordination_plugin_enabled(config: Mapping[str, object]) -> bool:
    plugins = config.get("plugins")
    if not isinstance(plugins, Mapping):
        return False
    return any(
        str(name).startswith(_COORDINATION_PLUGIN_PREFIX)
        and isinstance(settings, Mapping)
        and settings.get("enabled") is True
        for name, settings in plugins.items()
    )


def _mcp_server_config(
    config: Mapping[str, object], solet_name: str,
) -> Mapping[str, object] | None:
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return None
    server = servers.get(solet_name)
    return server if isinstance(server, Mapping) else None


def _identity_env(
    *, agent_instance_id: str, agent_session_id: str, label: str,
    solet_name: str, solet_bin: str, transport: str,
) -> dict[str, str]:
    env = dict(os.environ)
    # A managed worker is a new logical Codex session, never a continuation of
    # the operator process that happened to launch it.  Claude-only provider
    # and host policy variables are equally outside the Codex runtime contract.
    for key in tuple(env):
        if (
            key == _CODEX_PARENT_SESSION_ENV
            or key in _CLAUDE_ONLY_ENV_NAMES
            or key.startswith(_CLAUDE_ONLY_ENV_PREFIXES)
        ):
            env.pop(key)
    env.update(
        {
            "SOLET_NAME": solet_name,
            "AGENT_IDENTITY": _CODEX_AGENT_ID,
            "AGENT_INSTANCE_ID": agent_instance_id,
            "AGENT_SESSION_ID": agent_session_id,
            "AGENT_SESSION_LABEL": label,
            "FLEET_TRANSPORT": transport,
        },
    )
    _expose_worker_cli(env, solet_bin)
    # role_name is intentionally absent.  spawn_session.role_name authorizes
    # a later claim; it does not itself bind a role or grant AGENT_ROLE.
    env.pop("AGENT_ROLE", None)
    return env


def _parent_runtime_env_names_to_unset() -> list[str]:
    """Names that a pre-existing tmux server must not leak into Codex."""
    inherited = {
        key
        for key in os.environ
        if key in _CLAUDE_ONLY_ENV_NAMES
        or key.startswith(_CLAUDE_ONLY_ENV_PREFIXES)
    }
    inherited.update(_CLAUDE_PROVIDER_ENV_NAMES)
    inherited.update({_CODEX_PARENT_SESSION_ENV, "AGENT_ROLE"})
    return sorted(inherited)


def _without_parent_runtime_env(argv: list[str]) -> list[str]:
    command = ["env"]
    for key in _parent_runtime_env_names_to_unset():
        command += ["-u", key]
    return [*command, *argv]


def _codex_config_overrides(
    *, config: Mapping[str, object], solet_name: str, transport: str,
    agent_instance_id: str, agent_session_id: str, label: str, solet_bin: str,
) -> list[str]:
    """Return Codex-native ``-c`` overrides for the selected transport."""
    overrides = [
        "shell_environment_policy.set.PATH="
        f"{_toml_string(_worker_path(solet_bin))}",
    ]
    server = _mcp_server_config(config, solet_name)
    if server is None:
        return overrides
    prefix = f"mcp_servers.{solet_name}"
    if transport == "watch":
        return [*overrides, f"{prefix}.enabled=false"]
    identity = {
        "SOLET_NAME": solet_name,
        "AGENT_IDENTITY": _CODEX_AGENT_ID,
        "AGENT_INSTANCE_ID": agent_instance_id,
        "AGENT_SESSION_ID": agent_session_id,
        "AGENT_SESSION_LABEL": label,
        "AGENT_WAKE_CLI": solet_bin or "solet-bridge",
        "FLEET_TRANSPORT": transport,
        # A managed-session lane label is cosmetic.  Role ownership remains a
        # model-initiated peer_claim_role action after the bootstrap turn.
        _MCP_ROLE_AUTOBIND_ENV: "0",
    }
    overrides.append(f"{prefix}.enabled=true")
    overrides.extend(
        f"{prefix}.env.{key}={_toml_string(value)}"
        for key, value in identity.items()
    )
    return overrides


def _refuse_claude_provider_overlay(spec: Mapping[str, object]) -> None:
    from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

    if str(spec.get("provider") or "").strip() or spec.get("provider_env"):
        raise HostCannotSpawnError(
            "provider_unsupported_for_runtime: agent_runtime='codex' does not "
            "accept Claude provider selection or provider_env overlays; omit "
            "provider, or add a separately reviewed Codex-native provider contract.",
        )


def _command_succeeded(result: object) -> bool:
    return int(getattr(result, "returncode", 1)) == 0
