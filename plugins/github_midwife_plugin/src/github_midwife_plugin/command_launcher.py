"""SEED — install the per-solet command launcher at birth.

A genesis spine phase that puts a ``<name>`` command on the operator's PATH,
pointing at the newborn's own ``solet-bridge`` console script. This is the
no-MCP-first primary interface: after birth, ``<name> search ...`` and
``<name> call ...`` drive the solet over its localhost bridge with NO
MCP required.

Bare symlink by design: the ``solet-bridge`` CLI derives its identity from its OWN
install location (``local_cli.client.resolve_solet_name`` walks to the
clone root), so a symlink from anywhere on PATH resolves into THIS newborn's
venv and pins THIS newborn — reaching no sibling. UNCONDITIONAL (every profile
ships ``agent_messaging_plugin``, so every newborn has the console script).
The same phase appends the newborn's Codex-native MCP bridge table. A correct
existing symlink is a no-op; a stale/wrong symlink is repointed; a NON-symlink
already at the path is a fail-loud refusal. An existing same-name MCP table is
also a fail-loud refusal: it may contain an operator hand-edit and is never
overwritten.

Ensuring ``bin_dir`` is ON the operator's PATH is the hydration shell step's job,
not this phase's — this phase only creates the launcher in a well-known dir.
"""

from __future__ import annotations

import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from json import dumps as json_dumps
from pathlib import Path

from .constants import NAME_PATTERN, is_valid_solet_name

# The generic console-script name the launcher points at (the
# ``[project.scripts]`` entry of ``agent_messaging_plugin``). One generic name
# on disk; the per-solet name is the SYMLINK, resolved to identity by
# install location — so no shipped surface carries a specific solet name.
CONSOLE_SCRIPT_NAME = "solet-bridge"

# Default operator-PATH bin dir for the launcher: user-writable, no sudo. The
# hydration shell step ensures it is on PATH.
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"


class CommandLauncherError(RuntimeError):
    """The per-solet command launcher could not be installed."""


@dataclass(frozen=True, slots=True)
class CommandLauncherResult:
    status: str  # "installed" | "repointed" | "already_installed"
    reason: str
    launcher_path: str
    target: str
    mcp_config_path: str
    mcp_status: str = ""  # "installed" | "already_installed"
    mcp_reason: str = ""


@dataclass(frozen=True, slots=True)
class _McpConfigRead:
    content: str
    already_installed: bool
    reason: str


def _mcp_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _mcp_block(name: str, clone_root: Path) -> str:
    python = clone_root / ".venv" / "bin" / "python3"
    return (
        f"[mcp_servers.{name}]\n"
        f"command = {_toml_basic_string(str(python))}\n"
        'args = ["-m", "agent_messaging_plugin.mcp_bridge"]\n'
        'env_vars = ["CODEX_THREAD_ID"]\n\n'
        f"[mcp_servers.{name}.env]\n"
        f'SOLET_NAME = "{name}"\n'
        'AGENT_IDENTITY = "codex"\n'
        'AGENT_SESSION_LABEL = "Codex-Ambient"\n'
    )


def _toml_basic_string(value: str) -> str:
    """Encode a string using TOML's JSON-compatible basic-string escapes."""
    # TOML basic strings accept UTF-8 Unicode scalars, while JSON's ASCII-only
    # encoding represents supplementary-plane characters as surrogate pairs.
    # TOML rejects those pairs because they are not Unicode scalars.
    return json_dumps(value, ensure_ascii=False)


def _mcp_server_table(name: str, clone_root: Path) -> dict[str, object]:
    generated = tomllib.loads(_mcp_block(name, clone_root))
    servers = generated.get("mcp_servers")
    if not isinstance(servers, dict):  # pragma: no cover - _mcp_block is local and fixed-shape.
        raise CommandLauncherError("generated Codex MCP configuration has no mcp_servers table")
    server = servers.get(name)
    if not isinstance(server, dict):  # pragma: no cover - _mcp_block is local and fixed-shape.
        raise CommandLauncherError(f"generated Codex MCP configuration has no server {name!r}")
    return server


def _differing_mcp_keys(expected: object, actual: object, prefix: str = "") -> list[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        differing: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in expected or key not in actual:
                differing.append(path)
            else:
                differing.extend(_differing_mcp_keys(expected[key], actual[key], path))
        return differing
    return [] if expected == actual else [prefix]


def _read_mcp_config(config_path: Path, name: str, clone_root: Path) -> _McpConfigRead:
    if not config_path.exists():
        return _McpConfigRead(content="", already_installed=False, reason="Codex MCP bridge installed")
    try:
        content = config_path.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CommandLauncherError(f"could not read Codex config {config_path}: {exc}") from exc
    servers = parsed.get("mcp_servers")
    if isinstance(servers, dict) and name in servers:
        expected = _mcp_server_table(name, clone_root)
        actual = servers[name]
        if actual == expected:
            return _McpConfigRead(
                content=content,
                already_installed=True,
                reason="Codex MCP bridge already installed with generated configuration",
            )
        differing = ", ".join(_differing_mcp_keys(expected, actual)) or "table shape"
        raise CommandLauncherError(
            f"{config_path} [mcp_servers.{name}] differs from generated configuration at "
            f"keys: {differing} — refusing to overwrite "
            "an operator-managed Codex MCP configuration."
        )
    return _McpConfigRead(content=content, already_installed=False, reason="Codex MCP bridge installed")


def _write_all(descriptor: int, content: bytes) -> None:
    """Write every byte or fail loud rather than publishing a partial file."""
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("could not write complete Codex MCP configuration")
        offset += written


def _fsync_directory(directory_path: Path) -> None:
    directory_descriptor = os.open(directory_path, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _replace_mcp_config_atomically(config_path: Path, updated_content: str) -> None:
    """Publish validated configuration without mutating the current target first."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.is_symlink():
        raise CommandLauncherError(
            f"refusing to atomically replace symlinked Codex config {config_path}"
        )
    if config_path.exists() and not config_path.is_file():
        raise CommandLauncherError(
            f"refusing to atomically replace non-file Codex config {config_path}"
        )

    existing_mode = stat.S_IMODE(config_path.stat().st_mode) if config_path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{config_path.name}.", dir=config_path.parent)
    temporary: Path | None = Path(temporary_name)
    try:
        try:
            if existing_mode is not None:
                os.fchmod(descriptor, existing_mode)
            _write_all(descriptor, updated_content.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            tomllib.loads(temporary.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CommandLauncherError(
                f"could not verify temporary Codex MCP config {config_path}: {exc}"
            ) from exc
        os.replace(temporary, config_path)
        temporary = None
        _fsync_directory(config_path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                # Cleanup must not hide a write, validation, or replacement failure.
                pass


def _append_mcp_block(config_path: Path, content: str, name: str, clone_root: Path) -> None:
    separator = "" if not content or content.endswith("\n\n") else "\n"
    updated_content = content + separator + _mcp_block(name, clone_root)
    try:
        tomllib.loads(updated_content)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - local block is covered by smoke.
        raise CommandLauncherError(
            f"generated Codex MCP configuration for {config_path} is invalid TOML: {exc}"
        ) from exc
    try:
        _replace_mcp_config_atomically(config_path, updated_content)
    except OSError as exc:
        raise CommandLauncherError(
            f"could not write Codex MCP config {config_path}: {exc}"
        ) from exc


def install_command_launcher_at_birth(
    *,
    name: str,
    clone_root: Path,
    bin_dir: Path = DEFAULT_BIN_DIR,
    codex_config_path: Path | None = None,
) -> CommandLauncherResult:
    """Symlink ``<bin_dir>/<name>`` -> the newborn's ``solet-bridge`` console script.

    Raises :class:`CommandLauncherError` when the console script is missing (the
    venv must be provisioned + the plugin installed first), when a NON-symlink
    file already occupies the launcher path, or when Codex already has a table
    for this solet (never clobber an operator-managed configuration).
    """
    # Defense in depth: genesis validates the name first, but the launcher path
    # is `bin_dir / name`, so a bad name could escape bin_dir — refuse one here.
    if not is_valid_solet_name(name):
        raise CommandLauncherError(
            f"refusing to install a launcher for invalid solet name {name!r} "
            f"(must match {NAME_PATTERN.pattern})."
        )
    target = clone_root / ".venv" / "bin" / CONSOLE_SCRIPT_NAME
    if not target.is_file():
        raise CommandLauncherError(
            f"console script missing at {target} — the venv must be provisioned "
            f"and {CONSOLE_SCRIPT_NAME}'s plugin installed before the launcher."
        )
    config_path = codex_config_path or _mcp_config_path()
    mcp_config = _read_mcp_config(config_path, name, clone_root)
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / name

    if launcher.is_symlink():
        if launcher.readlink() == target:
            launcher_result = CommandLauncherResult(
                status="already_installed",
                reason="launcher already points at this newborn's console script",
                launcher_path=str(launcher), target=str(target), mcp_config_path=str(config_path),
            )
        else:
            launcher.unlink()
            launcher.symlink_to(target)
            launcher_result = CommandLauncherResult(
                status="repointed",
                reason="launcher repointed to this newborn's console script",
                launcher_path=str(launcher), target=str(target), mcp_config_path=str(config_path),
            )
    elif launcher.exists():
        raise CommandLauncherError(
            f"{launcher} already exists and is not a symlink — refusing to clobber "
            "an operator file; move it aside and re-run."
        )
    else:
        launcher.symlink_to(target)
        launcher_result = CommandLauncherResult(
            status="installed",
            reason="per-solet command launcher installed on PATH",
            launcher_path=str(launcher), target=str(target), mcp_config_path=str(config_path),
        )
    if mcp_config.already_installed:
        mcp_status = "already_installed"
        mcp_reason = mcp_config.reason
    else:
        _append_mcp_block(config_path, mcp_config.content, name, clone_root)
        mcp_status = "installed"
        mcp_reason = mcp_config.reason
    return CommandLauncherResult(
        status=launcher_result.status,
        reason=launcher_result.reason,
        launcher_path=launcher_result.launcher_path,
        target=launcher_result.target,
        mcp_config_path=launcher_result.mcp_config_path,
        mcp_status=mcp_status,
        mcp_reason=mcp_reason,
    )


__all__ = [
    "CONSOLE_SCRIPT_NAME",
    "DEFAULT_BIN_DIR",
    "CommandLauncherError",
    "CommandLauncherResult",
    "install_command_launcher_at_birth",
]
