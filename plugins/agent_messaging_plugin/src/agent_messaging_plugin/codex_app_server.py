"""Persistent app-server host driver for managed Codex sessions."""

from __future__ import annotations

import contextlib
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_app_client import _CodexAppServerClient, _RpcError
from .codex_common import (
    _CODEX_AGENT_ID,
    _TOML_BARE_KEY_RE,
    _codex_config_overrides,
    _codex_home,
    _coordination_plugin_enabled,
    _identity_env,
    _mcp_server_config,
    _read_codex_config,
    _refuse_claude_provider_overlay,
    _toml_string,
)
from .headless_adapter import (
    _authority_system_prompt,
    _pid_alive,
    _resolve_default_cwd,
    _sigterm_then_kill,
)
from .lane_worktrees import LaneWorktreeError, spawn_worktree_cwd, worktree_pythonpath
from .solet_cli import WakeCliResolver


@dataclass(slots=True)
class _TrackedCodexProcess:
    client: _CodexAppServerClient
    watcher: subprocess.Popen[str] | None


@dataclass(frozen=True, slots=True)
class _SpawnIdentity:
    agent_instance_id: str
    agent_session_id: str
    label: str


def _binary_remedies(codex_bin: str) -> list[str]:
    if codex_bin and os.access(codex_bin, os.X_OK):
        return []
    return [
        f"no executable 'codex' binary found at {codex_bin!r} — "
        "install Codex or pass codex_bin explicitly.",
    ]


_CODE_MODE_HOST_NAME = "codex-code-mode-host"
_CODE_MODE_HOST_PROBE_SECONDS = 5.0
_CODE_MODE_HOST_INDIRECTION = (
    "re-apply the symlink indirection — "
    "mv {host} {host}.real && ln -s {host}.real {host} — so the exec resolves "
    "through a path that carries no deny record."
)


def _code_mode_host_path(codex_bin: str) -> Path | None:
    """The helper Codex spawns per code-mode tool call, or ``None``.

    Resolved through ``codex_bin`` rather than PATH because the launcher is
    typically a symlink (``/opt/homebrew/bin/codex``) into a versioned cask
    directory that holds the helper as a sibling; the helper is never on PATH.
    """
    if not (codex_bin and os.access(codex_bin, os.X_OK)):
        return None
    return Path(codex_bin).resolve().parent / _CODE_MODE_HOST_NAME


def _code_mode_host_remedies(
    codex_bin: str,
    *,
    probe_seconds: float = _CODE_MODE_HOST_PROBE_SECONDS,
    run_fn: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Refuse the spawn when the code-mode host helper cannot actually exec.

    A code-mode model runs EVERY tool call through this helper, spawned per
    invocation over pipes.  When macOS AppleSystemPolicy holds a deny record for
    the helper's exact full path string, that exec blocks in ``_dyld_start``
    forever — so the lane spawns, registers, and then times out on every single
    tool call without ever being able to report why.  A silent wedge from birth
    costs a whole dispatch, and one bounded exec (0.03s on a healthy install,
    measured 2026-08-30) buys a loud refusal instead.

    The discriminator is whether the helper RETURNS, not what it returns: a
    wedged helper never exits, a healthy one prints usage.  Exit status is
    deliberately not read, so a future non-zero ``--help`` cannot start refusing
    healthy spawns.
    """
    host = _code_mode_host_path(codex_bin)
    if host is None:
        return []
    if not host.exists():
        if not host.is_symlink():
            return []
        return [
            f"{host} is a dangling symlink, so every code-mode Codex turn will "
            "fail to spawn its host — restore the helper it points at, or "
            "reinstall the Codex cask.",
        ]
    try:
        run_fn(
            [str(host), "--help"],
            capture_output=True, text=True, timeout=probe_seconds,
        )
    except subprocess.TimeoutExpired:
        return [
            f"{host} did not return within {probe_seconds:g}s — the platform "
            "denies exec of that exact path string, which wedges every "
            "code-mode Codex turn from birth; "
            + _CODE_MODE_HOST_INDIRECTION.format(host=host),
        ]
    except OSError as exc:
        return [
            f"{host} could not be executed ({exc}); "
            + _CODE_MODE_HOST_INDIRECTION.format(host=host),
        ]
    return []


def _solet_name_remedies(solet_name: str) -> list[str]:
    if not solet_name:
        return [
            "SOLET_NAME is not set — the Codex worker cannot resolve its solet instance.",
        ]
    if _TOML_BARE_KEY_RE.fullmatch(solet_name) is not None:
        return []
    return [
        f"SOLET_NAME {solet_name!r} is not a TOML bare key — "
        "managed Codex overrides require only letters, digits, '_' or '-'.",
    ]


def _transport_remedies(transport: str) -> list[str]:
    if transport in {"mcp", "watch"}:
        return []
    return [f"unsupported Codex fleet transport {transport!r}; use 'mcp' or 'watch'."]


def _installed_config_remedies(
    *, config: Mapping[str, object], solet_name: str,
    solet_bin: str, transport: str,
) -> list[str]:
    remedies: list[str] = []
    if not _coordination_plugin_enabled(config):
        remedies.append(
            "coordination-hooks is not installed and enabled in Codex config — install "
            "coordination-hooks@<solet-marketplace> before managed spawning.",
        )
    if transport == "mcp" and _mcp_server_config(config, solet_name) is None:
        remedies.append(
            f"Codex config has no mcp_servers.{solet_name} table, required "
            "for transport='mcp'; add that Codex-native MCP server config or use watch.",
        )
    if transport == "watch" and not (solet_bin and os.access(solet_bin, os.X_OK)):
        remedies.append(
            "transport='watch' requires an executable solet CLI so the managed "
            "worker can register without binding a role and arm its watcher.",
        )
    return remedies


class CodexAppServerHostDriver:
    """The ``("codex", "headless")`` persistent app-server driver."""

    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        solet_bin: str | None = None,
        solet_name: str | None = None,
        codex_home: Path | None = None,
        cwd: Path | None = None,
        python_executable: str | None = None,
        transport: str | None = None,
        popen_fn: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        client_factory: Callable[..., _CodexAppServerClient] = _CodexAppServerClient,
        grace_seconds: float = 10.0,
        code_mode_probe_seconds: float = _CODE_MODE_HOST_PROBE_SECONDS,
    ) -> None:
        self._codex_bin = (
            codex_bin if codex_bin is not None
            else shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")
        )
        # R11 (2026-08-17): UNRESOLVED override; resolved per read by the
        # `_solet_bin` property. See resolve_solet_bin's own note.
        self._cli_resolver = WakeCliResolver(
            solet_bin, python_executable=python_executable,
        )
        self._solet_name = (
            solet_name if solet_name is not None
            else os.environ.get("SOLET_NAME", "")
        )
        self._codex_home = _codex_home(codex_home)
        self._cwd = cwd if cwd is not None else _resolve_default_cwd()
        self._transport = transport if transport is not None else ""
        self._popen_fn = popen_fn
        self._client_factory = client_factory
        self._grace_seconds = grace_seconds
        self._code_mode_probe_seconds = code_mode_probe_seconds
        self._lock = threading.RLock()
        self._processes: dict[str, _TrackedCodexProcess] = {}

    @property
    def _solet_bin(self) -> str:
        """The wake CLI, resolved FRESH on every read (R11, 2026-08-17).

        Was resolved once in ``__init__``, caching a snapshot of the ``current``
        symlink's target — mutable state that cutover moves under a long-lived
        process — and thereby pinning every later spawn into a reapable versioned
        release directory. Full account in :func:`resolve_solet_bin`.

        Per READ rather than per spawn; the bounded residual is documented on
        ``TmuxHostDriver._solet_bin`` and applies identically here.
        """
        return self._cli_resolver.resolve()

    @property
    def _config_path(self) -> Path:
        return self._codex_home / "config.toml"

    def _resolve_transport(self, spec: Mapping[str, object]) -> str:
        return str(spec.get("transport") or "") or self._transport or "watch"

    def verify_config(self, *, transport: str | None = None) -> list[str]:
        resolved_transport = transport if transport is not None else (self._transport or "watch")
        remedies = (
            _binary_remedies(self._codex_bin)
            + _code_mode_host_remedies(
                self._codex_bin, probe_seconds=self._code_mode_probe_seconds,
            )
            + _solet_name_remedies(self._solet_name)
            + _transport_remedies(resolved_transport)
        )
        if not self._config_path.is_file():
            remedies.append(
                f"Codex config is missing at {self._config_path}; managed Codex spawns "
                "require an installed/enabled coordination-hooks plugin.",
            )
            return remedies
        return remedies + _installed_config_remedies(
            config=_read_codex_config(self._config_path),
            solet_name=self._solet_name,
            solet_bin=self._solet_bin,
            transport=resolved_transport,
        )

    def capability_report(self) -> dict[str, object]:
        return {
            "host": "headless",
            "agent_runtime": _CODEX_AGENT_ID,
            "topology": "subprocess",
            "inspectable_via": ["codex_thread", "transcript"],
            "driver_channel": "app-server-json-rpc",
        }

    def spawn(self, spec: Mapping[str, object]) -> str:
        _refuse_claude_provider_overlay(spec)
        transport = self._resolve_transport(spec)
        self._require_ready(transport)
        identity = self._spawn_identity(spec)
        try:
            cwd = spawn_worktree_cwd(spec.get("worktree_path"), self._cwd)
        except LaneWorktreeError as exc:
            from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

            raise HostCannotSpawnError(str(exc)) from exc
        env = self._spawn_env(identity, transport, cwd)
        client = self._build_client(spec, identity, transport, env, cwd)
        self._start_client(client)
        watcher = self._watcher_for_transport(client, env, transport, cwd)
        host_ref = str(client.pid)
        with self._lock:
            self._processes[host_ref] = _TrackedCodexProcess(client=client, watcher=watcher)
        return host_ref

    def _require_ready(self, transport: str) -> None:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        remedies = self.verify_config(transport=transport)
        if remedies:
            raise HostCannotSpawnError("; ".join(remedies))

    @staticmethod
    def _spawn_identity(spec: Mapping[str, object]) -> _SpawnIdentity:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        agent_instance_id = str(spec.get("agent_instance_id") or "")
        if not agent_instance_id:
            raise HostCannotSpawnError(
                "spawn spec is missing agent_instance_id — the Codex process cannot "
                "register against the managed-session lineage without it.",
            )
        return _SpawnIdentity(
            agent_instance_id=agent_instance_id,
            agent_session_id=f"ases-{agent_instance_id}",
            label=(
                str(spec.get("local_name") or "")
                or str(spec.get("lane_id") or "")
                or agent_instance_id
            ),
        )

    def _spawn_env(
        self, identity: _SpawnIdentity, transport: str, cwd: Path | None = None,
    ) -> dict[str, str]:
        env = _identity_env(
            agent_instance_id=identity.agent_instance_id,
            agent_session_id=identity.agent_session_id,
            label=identity.label,
            solet_name=self._solet_name,
            solet_bin=self._solet_bin,
            transport=transport,
        )
        env["PYTHONPATH"] = worktree_pythonpath(cwd or self._cwd, env.get("PYTHONPATH", ""))
        return env

    def _build_client(
        self, spec: Mapping[str, object], identity: _SpawnIdentity,
        transport: str, env: Mapping[str, str], cwd: Path,
    ) -> _CodexAppServerClient:
        config = _read_codex_config(self._config_path)
        overrides = _codex_config_overrides(
            config=config,
            solet_name=self._solet_name,
            transport=transport,
            agent_instance_id=identity.agent_instance_id,
            agent_session_id=identity.agent_session_id,
            label=identity.label,
            solet_bin=self._solet_bin,
        )
        effort = str(spec.get("effort") or "")
        if effort:
            overrides.append(f"model_reasoning_effort={_toml_string(effort)}")
        argv = [self._codex_bin, "--dangerously-bypass-hook-trust"]
        for override in overrides:
            argv += ["-c", override]
        argv += ["app-server", "--listen", "stdio://"]
        return self._client_factory(
            argv=argv,
            cwd=cwd,
            env=env,
            developer_instructions=_authority_system_prompt(spec),
            model=str(spec.get("model") or ""),
            popen_fn=self._popen_fn,
        )

    def _start_client(self, client: _CodexAppServerClient) -> None:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        try:
            client.start()
        except (OSError, _RpcError, queue.Empty, ValueError) as exc:
            client.close(self._grace_seconds)
            raise HostCannotSpawnError(f"Codex app-server boot failed: {exc}") from exc

    def _watcher_for_transport(
        self, client: _CodexAppServerClient, env: Mapping[str, str], transport: str, cwd: Path,
    ) -> subprocess.Popen[str] | None:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        if transport != "watch":
            return None
        try:
            return self._start_watcher(client.pid, env, cwd)
        except HostCannotSpawnError:
            client.close(self._grace_seconds)
            raise

    def _start_watcher(
        self, parent_pid: int, env: Mapping[str, str], cwd: Path,
    ) -> subprocess.Popen[str]:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        try:
            # CDX-06 (2026-08-24): the spool is RE-ARMED. It was disabled by
            # codex-0147-dead-spool-retirement (2026-08-13) because stock
            # Codex had no consumer for it at all -- an armed spool would
            # just accumulate an unread file for the process's lifetime. That
            # premise no longer holds: `inbox_consumer.py`
            # (plugins/github_midwife_plugin/codex_plugin/coordination-hooks/
            # hooks/inbox_consumer.py), a SYNCHRONOUS Stop hook, now calls
            # `solet-bridge wake --max-wait <a few seconds>` on every turn boundary
            # specifically to read this spool -- an armed-but-unread spool is
            # exactly the CDX-06 defect this consumer exists to fix, not a
            # reason to leave the spool disabled.
            watcher = self._popen_fn(
                [
                    self._solet_bin,
                    "watch",
                    "--agent-id", _CODEX_AGENT_ID,
                    "--no-claim",
                    "--exit-with-parent", str(parent_pid),
                ],
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise HostCannotSpawnError(f"Codex watch sidecar boot failed: {exc}") from exc
        self._drain_watcher_pipe(watcher.stdout)
        self._drain_watcher_pipe(watcher.stderr)
        return watcher

    @staticmethod
    def _drain_watcher_pipe(pipe: Any) -> None:
        if pipe is None:
            return

        def _drain() -> None:
            with contextlib.suppress(OSError, ValueError):
                for _line in pipe:
                    pass

        threading.Thread(target=_drain, daemon=True).start()

    def alive(self, host_ref: str) -> bool:
        with self._lock:
            tracked = self._processes.get(host_ref)
        if tracked is not None:
            return tracked.client.alive()
        try:
            return _pid_alive(int(host_ref))
        except ValueError:
            return False

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        with self._lock:
            tracked = self._processes.pop(host_ref, None)
        if tracked is None:
            try:
                pid = int(host_ref)
            except ValueError:
                return
            _sigterm_then_kill(pid, None, grace_seconds or self._grace_seconds)
            return
        grace = grace_seconds if grace_seconds > 0 else self._grace_seconds
        tracked.client.close(grace)
        if tracked.watcher is not None:
            _sigterm_then_kill(tracked.watcher.pid, tracked.watcher, grace)

    def driver_channel(self, host_ref: str) -> _CodexAppServerClient | None:
        with self._lock:
            tracked = self._processes.get(host_ref)
        if tracked is None or not tracked.client.alive():
            return None
        return tracked.client

    def shutdown(self) -> None:
        with self._lock:
            refs = list(self._processes)
        for host_ref in refs:
            self.terminate(host_ref, int(self._grace_seconds))
