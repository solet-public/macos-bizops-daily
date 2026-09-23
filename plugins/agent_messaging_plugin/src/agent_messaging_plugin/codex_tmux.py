"""Detached-tmux host driver and verified TUI channel for managed Codex."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .codex_app_server import _CODE_MODE_HOST_PROBE_SECONDS, CodexAppServerHostDriver
from .codex_common import (
    _ANSI_ESCAPE_RE,
    _CODEX_AGENT_ID,
    _CODEX_COMPOSER_PROMPT,
    _CODEX_IDLE_COMPOSER_PLACEHOLDER,
    _DEFAULT_TMUX_POLL_INTERVAL_SECONDS,
    _DEFAULT_TMUX_STABLE_SAMPLES,
    _DEFAULT_TMUX_VERIFY_TIMEOUT_SECONDS,
    _MIN_TMUX_VERSION,
    _codex_config_overrides,
    _codex_home,
    _command_succeeded,
    _identity_env,
    _read_codex_config,
    _refuse_claude_provider_overlay,
    _toml_string,
    _without_parent_runtime_env,
    codex_busy_reason,
)
from .headless_adapter import (
    _authority_system_prompt,
    _pid_alive,
    _resolve_default_cwd,
    _sigterm_then_kill,
)
from .lane_worktrees import LaneWorktreeError, spawn_worktree_cwd, worktree_pythonpath
from .solet_cli import WakeCliResolver
from .submit_conventions import TMUX_CODEX_SUBMIT_CONVENTION
from .tmux_adapter import (
    DEFAULT_PANE_HEIGHT,
    DEFAULT_PANE_WIDTH,
    _emit_role_tag_path,
    _parse_tmux_version,
    _sanitize_session_name,
    _sigterm_then_kill_process_group,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def _resolve_host_binary(explicit: str | None, name: str) -> str:
    """Resolve a host binary without assuming launchd inherited a login PATH.

    The LaunchAgent's deterministic PATH includes both Homebrew prefixes but
    intentionally does not inherit the operator's shell configuration. Codex
    itself commonly lives in ``~/.local/bin``; retain an explicit injection as
    authoritative, then prefer PATH and finally that standard per-user location.
    ``verify_config`` remains the executable-presence authority for a missing
    fallback path.
    """
    if explicit is not None:
        return explicit
    return shutil.which(name) or str(Path.home() / ".local" / "bin" / name)


@dataclass(frozen=True, slots=True)
class _TmuxSpawnIdentity:
    agent_instance_id: str
    agent_session_id: str
    label: str


class _CodexTmuxDriverChannel:
    """Verified send-keys channel for the interactive Codex TUI."""

    def __init__(
        self,
        *, tmux_bin: str, session: str, run_fn: Callable[..., Any],
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = _DEFAULT_TMUX_POLL_INTERVAL_SECONDS,
        stable_samples: int = _DEFAULT_TMUX_STABLE_SAMPLES,
        verify_timeout_seconds: float = _DEFAULT_TMUX_VERIFY_TIMEOUT_SECONDS,
    ) -> None:
        self._tmux_bin = tmux_bin
        self._session = session
        self._run_fn = run_fn
        self._sleep_fn = sleep_fn
        self._now_fn = now_fn
        self._poll_interval_seconds = poll_interval_seconds
        self._stable_samples = stable_samples
        self._verify_timeout_seconds = verify_timeout_seconds
        self._composed: str | None = None

    convention = TMUX_CODEX_SUBMIT_CONVENTION

    def insert(self, text: str) -> None:
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        # _wait_until_ready's return value is the idle prompt it observed
        # right before the literal send -- the genuine pre-send baseline,
        # reused rather than re-captured (driver-channel strand fix,
        # 2026-08-14, mirrored from the tmux twin's own fix: see
        # tmux_adapter.py's _TmuxSendKeysDriverChannel.send/
        # _wait_for_paste_stable docstrings for the shared defect and fix
        # shape, hermetically reproduced against each class independently).
        baseline = self._wait_until_ready()
        literal = self._run(
            [self._tmux_bin, "send-keys", "-t", self._session, "-l", "--", text],
        )
        if not _command_succeeded(literal):
            raise DriverChannelSendError(
                f"tmux literal send failed for Codex session {self._session!r}",
            )
        try:
            self._composed = self._wait_until_stable(baseline)
        except DriverChannelSendError as exc:
            self._composed = None
            rollback_detail = self._rollback_failed_insert(text)
            if rollback_detail is None:
                raise
            raise DriverChannelSendError(f"{exc} {rollback_detail}") from exc

    def submit(self) -> None:
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        if self._composed is None:
            raise DriverChannelSendError("Codex tmux submit called before a successful insert")
        if self._submit_and_observe_change(self._composed):
            return
        # A same-burst Enter can be absorbed by the TUI.  One separately
        # timed retry is allowed; two no-op Enters are evidence of no pickup.
        if self._submit_and_observe_change(self._composed):
            return
        raise DriverChannelSendError(
            f"Codex tmux session {self._session!r} showed no styled pane-state "
            "change after two separate Enter submissions; refusing to call "
            "ghost/composed text a delivered turn.",
        )

    def send(self, text: str) -> None:
        self.insert(text)
        self.submit()

    def interrupt_park(self) -> str:
        """Interrupt a Stop-hook park and prove the pane became idle again."""
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        escaped = self._run(
            [self._tmux_bin, "send-keys", "-t", self._session, "Escape"],
        )
        if not _command_succeeded(escaped):
            raise DriverChannelSendError(
                f"Codex tmux pane {self._session!r} rejected the parked-pane Escape interrupt.",
            )
        self._composed = None
        self._wait_until_ready()
        return "interrupted_park"

    def _run(self, argv: list[str]) -> Any:
        try:
            return self._run_fn(
                argv, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            from .session_hosts import DriverChannelSendError  # noqa: PLC0415

            raise DriverChannelSendError(
                f"tmux command failed for Codex session {self._session!r}: {exc}",
            ) from exc

    def _capture_styled(self) -> str | None:
        result = self._run(
            [self._tmux_bin, "capture-pane", "-e", "-p", "-t", self._session],
        )
        return str(getattr(result, "stdout", "") or "") if _command_succeeded(result) else None

    @staticmethod
    def _visible_text(styled: str) -> str:
        return _ANSI_ESCAPE_RE.sub("", styled)

    def _wait_until_ready(self) -> str:
        """Wait for an idle Codex prompt before putting text in its composer.

        Keys on ``_CODEX_IDLE_COMPOSER_PLACEHOLDER``, the empty-composer
        placeholder line, not the ``OpenAI Codex`` startup banner the
        previous check required: that banner is a one-time box printed at
        session start which scrolls out of the visible pane as soon as a
        turn or two of output has pushed it off-screen, after which the old
        condition could never be satisfied again -- driver_delivery_failed
        against a pane sitting right at its idle prompt (measured live,
        2026-08-23, workbench/2026-08-23_dispatch_lane_m_codex_drive_idle_
        detector.md). The placeholder reappears every time the composer goes
        idle, independent of scroll position.

        The placeholder is necessary but NOT sufficient, which is the
        correction this method carries since 2026-09-09: Codex 0.153.4 renders
        the empty-composer placeholder WHILE A TURN IS RUNNING as well (live
        capture, a lane 12m into a turn showed the placeholder and its
        ``Working (...)`` status line on screen together). The placeholder
        tracks an empty composer, not an idle session, so readiness is the
        conjunction of the placeholder and the ABSENCE of a busy status line;
        see :func:`codex_busy_reason` for why that second half matches the
        status line's shape rather than any bare word it contains.
        """
        deadline = self._now_fn() + self._verify_timeout_seconds
        busy_reason: str | None = None
        saw_composer = False
        saw_prompt = False
        captured_any = False
        while self._now_fn() <= deadline:
            current = self._capture_styled()
            if current is not None:
                captured_any = True
                visible = self._visible_text(current)
                composer = _CODEX_IDLE_COMPOSER_PLACEHOLDER in visible
                saw_composer = saw_composer or composer
                saw_prompt = saw_prompt or _CODEX_COMPOSER_PROMPT in visible
                reason = codex_busy_reason(visible)
                if reason is not None:
                    busy_reason = reason
                if composer and reason is None:
                    return current
            self._sleep_fn(self._poll_interval_seconds)
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        raise DriverChannelSendError(self._not_ready_detail(
            busy_reason=busy_reason,
            saw_composer=saw_composer,
            saw_prompt=saw_prompt,
            captured_any=captured_any,
        ))

    def _not_ready_detail(
        self, *, busy_reason: str | None, saw_composer: bool, saw_prompt: bool,
        captured_any: bool,
    ) -> str:
        """Say WHY the pane was not driveable, not merely that it was not.

        Every readiness timeout used to raise the same sentence -- "never
        reached an idle prompt" -- whether the pane was wedged, gone, or a
        perfectly healthy lane simply working on its previous turn. That
        conflation is what made a busy lane read as a broken one: a
        ``drive_session`` issued straight after ``spawn_session`` is racing the
        bootstrap turn that ``spawn_session`` itself just delivered, so the
        pane is busy BY CONSTRUCTION for as long as that turn runs (measured
        live 2026-09-09: a lane 12m into a turn, against this check's 10s
        budget) and the caller was told delivery had failed.
        """
        budget = f"{self._verify_timeout_seconds:g}s"
        if busy_reason is not None:
            return (
                f"Codex tmux pane {self._session!r} was still mid-turn after {budget} "
                f"({busy_reason!r}); text was not pasted. The pane is healthy and busy, "
                "not wedged -- retry once the turn ends, or queue the text instead of "
                "driving it."
            )
        if not captured_any:
            return (
                f"Codex tmux pane {self._session!r} could not be captured within "
                f"{budget}; text was not pasted."
            )
        if not saw_composer and saw_prompt:
            return (
                f"Codex tmux pane {self._session!r} never reached an idle prompt within "
                f"{budget}: its composer already holds other text; text was not pasted. "
                "Driving would have appended to someone else's draft, so nothing was sent."
            )
        if not saw_composer:
            return (
                f"Codex tmux pane {self._session!r} never reached an idle prompt within "
                f"{budget}: no Codex composer was on screen at all (is this pane running "
                "Codex?); text was not pasted."
            )
        return (
            f"Codex tmux pane {self._session!r} never reached an idle prompt within "
            f"{budget}; text was not pasted."
        )

    def _wait_until_stable(self, baseline: str | None) -> str:
        """Poll until the styled pane is IDENTICAL across ``stable_samples``
        consecutive captures AND differs from ``baseline`` (the idle prompt
        :meth:`_wait_until_ready` observed right before the literal send).
        Without the baseline gate, a slow-rendering composer can hold that
        same idle screen across every sample in the window, and N identical
        PRE-paste samples satisfy "stable" exactly as well as N identical
        POST-render ones -- Enter then fires against a composer that still
        visibly shows nothing but the idle prompt, and
        :meth:`_submit_and_observe_change`'s own post-Enter check can be
        fooled into a false "delivered" signal by that same paste finally
        rendering on its own schedule (driver-channel strand fix, 2026-08-14,
        hermetically reproduced -- workbench/2026-08-14_driver_channel_
        strand_fix_report_lane_d.md). Still fails CLOSED on timeout (raises,
        never sends Enter for un-confirmed composed text) -- this class's own
        established contract, unchanged by this fix; a composer whose full
        render genuinely never differs from the pre-send idle screen raises
        here exactly as before, rather than silently proceeding with a stale
        ``composed`` value."""
        deadline = self._now_fn() + self._verify_timeout_seconds
        previous: str | None = None
        count = 0
        while self._now_fn() <= deadline:
            current = self._capture_styled()
            if current is not None and current == previous and current != baseline:
                count += 1
                if count >= self._stable_samples:
                    return current
            else:
                count = 0
            previous = current
            self._sleep_fn(self._poll_interval_seconds)
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        raise DriverChannelSendError(
            f"Codex tmux pane {self._session!r} did not stabilize before Enter; "
            "submission was not attempted.",
        )

    def _rollback_failed_insert(self, text: str) -> str | None:
        """Clear a failed literal send only after proving this call owns it.

        A stable-gate miss happens after ``send-keys -l`` has already put text
        in the composer but before any Enter.  Clearing blindly would be worse
        than leaving the failure visible: another writer may have taken the
        composer in that interval.  Require an exact active-composer match to
        this call's text, use ``C-u`` (never Enter), then positively re-read
        the idle placeholder.  Any uncertainty leaves the pane untouched and
        is carried beside the original failure for a steward to diagnose.
        """
        from .session_hosts import DriverChannelSendError  # noqa: PLC0415

        try:
            captured = self._capture_styled()
        except DriverChannelSendError as exc:
            return f"Rollback could not inspect pane {self._session!r}: {exc}"
        if captured is None:
            return f"Rollback could not inspect pane {self._session!r}; text may be stranded."
        if not self._composer_is_exactly(captured, text):
            return (
                f"Rollback refused for pane {self._session!r}: it did not positively show "
                "only this call's inserted text; text may be stranded."
            )
        try:
            cleared = self._run(
                [self._tmux_bin, "send-keys", "-t", self._session, "C-u"],
            )
        except DriverChannelSendError as exc:
            return f"Rollback clear failed for pane {self._session!r}: {exc}"
        if not _command_succeeded(cleared):
            return f"Rollback clear failed for pane {self._session!r}; text may be stranded."
        try:
            self._wait_until_ready()
        except DriverChannelSendError as exc:
            return f"Rollback could not verify idle composer for pane {self._session!r}: {exc}"
        return None

    def _composer_is_exactly(self, styled: str, text: str) -> bool:
        """Whether the visible active-composer row contains only ``text``."""
        if not text or "\n" in text:
            return False
        expected = f"› {text}"
        return any(line.strip() == expected for line in self._visible_text(styled).splitlines())

    def _submit_and_observe_change(self, composed: str) -> bool:
        submit_value = self.convention.submit_value
        if submit_value is None:
            raise RuntimeError("Codex tmux convention has no terminal submit value")
        enter = self._run(
            [self._tmux_bin, "send-keys", "-t", self._session, submit_value],
        )
        if not _command_succeeded(enter):
            return False
        deadline = self._now_fn() + self._verify_timeout_seconds
        while self._now_fn() <= deadline:
            current = self._capture_styled()
            if (
                current is not None
                and current != composed
                and self._visible_text(current) != self._visible_text(composed)
            ):
                return True
            self._sleep_fn(self._poll_interval_seconds)
        return False


class CodexTmuxHostDriver:
    """The ``("codex", "tmux")`` swap-durable interactive driver."""

    def __init__(
        self,
        *, codex_bin: str | None = None,
        tmux_bin: str | None = None,
        solet_bin: str | None = None,
        solet_name: str | None = None,
        codex_home: Path | None = None,
        cwd: Path | None = None,
        python_executable: str | None = None,
        transport: str | None = None,
        run_fn: Callable[..., Any] = subprocess.run,
        pane_width: int = DEFAULT_PANE_WIDTH,
        pane_height: int = DEFAULT_PANE_HEIGHT,
        grace_seconds: float = 10.0,
        code_mode_probe_seconds: float = _CODE_MODE_HOST_PROBE_SECONDS,
    ) -> None:
        self._codex_bin = _resolve_host_binary(codex_bin, "codex")
        self._tmux_bin = _resolve_host_binary(tmux_bin, "tmux")
        # R11 (2026-08-17): UNRESOLVED override; resolved per read by the
        # `_solet_bin` property. See resolve_solet_bin's own note.
        self._python_executable = python_executable
        self._cli_resolver = WakeCliResolver(
            solet_bin, python_executable=self._python_executable,
        )
        self._solet_name = (
            solet_name if solet_name is not None
            else os.environ.get("SOLET_NAME", "")
        )
        self._codex_home = _codex_home(codex_home)
        self._cwd = cwd if cwd is not None else _resolve_default_cwd()
        self._transport = transport if transport is not None else ""
        self._run_fn = run_fn
        self._pane_width = pane_width
        self._pane_height = pane_height
        self._grace_seconds = grace_seconds
        self._code_mode_probe_seconds = code_mode_probe_seconds

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
        base = CodexAppServerHostDriver(
            codex_bin=self._codex_bin,
            solet_bin=self._solet_bin,
            solet_name=self._solet_name,
            codex_home=self._codex_home,
            cwd=self._cwd,
            python_executable=self._python_executable,
            transport=self._transport,
            code_mode_probe_seconds=self._code_mode_probe_seconds,
        ).verify_config(transport=transport)
        if not (self._tmux_bin and os.access(self._tmux_bin, os.X_OK)):
            base.append("no executable tmux binary found — install tmux>=3.3.")
            return base
        try:
            result = self._run_fn(
                [self._tmux_bin, "-V"], capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            base.append(f"tmux -V failed: {exc}")
            return base
        version = _parse_tmux_version(str(getattr(result, "stdout", "") or ""))
        if not _command_succeeded(result) or version is None or version < _MIN_TMUX_VERSION:
            base.append(
                f"tmux>=3.3 is required; observed {getattr(result, 'stdout', '')!r}.",
            )
        return base

    def capability_report(self) -> dict[str, object]:
        return {
            "host": "tmux",
            "agent_runtime": _CODEX_AGENT_ID,
            "topology": "detached-pty",
            "inspectable_via": ["tmux", "codex_thread", "transcript"],
            "driver_channel": "verified-send-keys",
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
        codex_cmd = self._codex_command(spec, identity, transport, cwd)
        session_name = _sanitize_session_name(
            f"fleet-{identity.label}-{identity.agent_instance_id[-8:]}",
        )
        command = self._new_session_command(
            session_name=session_name,
            pane_command=self._pane_command(
                codex_cmd, label=identity.label, transport=transport,
            ),
            env=env, cwd=cwd,
        )
        self._launch_tmux(command)
        self._run_fn(
            [self._tmux_bin, "set", "-g", "allow-passthrough", "on"],
            capture_output=True, text=True, timeout=5,
        )
        return session_name

    def _require_ready(self, transport: str) -> None:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        remedies = self.verify_config(transport=transport)
        if remedies:
            raise HostCannotSpawnError("; ".join(remedies))

    @staticmethod
    def _spawn_identity(spec: Mapping[str, object]) -> _TmuxSpawnIdentity:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        agent_instance_id = str(spec.get("agent_instance_id") or "")
        if not agent_instance_id:
            raise HostCannotSpawnError("spawn spec is missing agent_instance_id for Codex tmux.")
        return _TmuxSpawnIdentity(
            agent_instance_id=agent_instance_id,
            agent_session_id=f"ases-{agent_instance_id}",
            label=(
                str(spec.get("local_name") or "")
                or str(spec.get("lane_id") or "")
                or agent_instance_id
            ),
        )

    def _spawn_env(
        self, identity: _TmuxSpawnIdentity, transport: str, cwd: Path | None = None,
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

    def _codex_command(
        self, spec: Mapping[str, object], identity: _TmuxSpawnIdentity,
        transport: str, cwd: Path | None = None,
    ) -> list[str]:
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
        overrides.append(
            f"developer_instructions={_toml_string(_authority_system_prompt(spec))}",
        )
        codex_cmd = [
            self._codex_bin,
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "-C", str(cwd or self._cwd),
        ]
        model = str(spec.get("model") or "")
        if model:
            codex_cmd += ["-m", model]
        for override in overrides:
            codex_cmd += ["-c", override]
        return codex_cmd

    def _new_session_command(
        self, *, session_name: str, pane_command: str, env: Mapping[str, str], cwd: Path | None = None,
    ) -> list[str]:
        new_session_cmd = [
            self._tmux_bin, "new-session", "-d", "-s", session_name,
            "-x", str(self._pane_width), "-y", str(self._pane_height),
        ]
        for key, value in env.items():
            if key in {
                "SOLET_NAME", "AGENT_IDENTITY", "AGENT_INSTANCE_ID",
                "AGENT_SESSION_ID", "AGENT_SESSION_LABEL", "AGENT_WAKE_CLI",
                "FLEET_TRANSPORT", "PATH", "PYTHONPATH",
            }:
                new_session_cmd += ["-e", f"{key}={value}"]
        new_session_cmd += ["-c", str(cwd or self._cwd), "sh", "-c", pane_command]
        return new_session_cmd

    def _launch_tmux(self, new_session_cmd: list[str]) -> None:
        from .session_hosts import HostCannotSpawnError  # noqa: PLC0415

        try:
            result = self._run_fn(
                new_session_cmd, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostCannotSpawnError(f"tmux new-session raised: {exc}") from exc
        if not _command_succeeded(result):
            raise HostCannotSpawnError(
                f"tmux new-session failed: {getattr(result, 'stderr', '')}",
            )

    def _pane_command(self, codex_cmd: list[str], *, label: str, transport: str) -> str:
        emit = _emit_role_tag_path()
        parts = [
            f"sh {shlex.quote(str(emit))} {shlex.quote(label)}; " if emit.exists() else "",
        ]
        if transport == "watch":
            # CDX-06 (2026-08-24): the spool is RE-ARMED. It was disabled by
            # codex-0147-dead-spool-retirement (2026-08-13) because stock
            # Codex had no consumer for it at all -- an armed spool would
            # just accumulate an unread file for the pane's lifetime. That
            # premise no longer holds: `inbox_consumer.py`
            # (plugins/github_midwife_plugin/codex_plugin/coordination-hooks/
            # hooks/inbox_consumer.py), a SYNCHRONOUS Stop hook, now calls
            # `solet-bridge wake --max-wait <a few seconds>` on every turn boundary
            # specifically to read this spool -- an armed-but-unread spool is
            # exactly the CDX-06 defect this consumer exists to fix, not a
            # reason to leave the spool disabled.
            watch_cmd = shlex.join(
                _without_parent_runtime_env(
                    [
                        self._solet_bin,
                        "watch",
                        "--agent-id", _CODEX_AGENT_ID,
                        "--no-claim",
                    ],
                ),
            )
            # $$ is deliberately shell-expanded before exec; the shell pid is
            # retained by the exec'd Codex process, giving the watcher a true
            # parent-liveness target without adding a second identity mint.
            parts.append(
                f"{watch_cmd} --exit-with-parent $$ >/dev/null 2>&1 & ",
            )
        parts.append(f"exec {shlex.join(_without_parent_runtime_env(codex_cmd))}")
        return "".join(parts)

    def alive(self, host_ref: str) -> bool:
        try:
            result = self._run_fn(
                [self._tmux_bin, "has-session", "-t", host_ref],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return _command_succeeded(result)

    def _pane_pid(self, host_ref: str) -> int | None:
        try:
            result = self._run_fn(
                [self._tmux_bin, "list-panes", "-t", host_ref, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not _command_succeeded(result):
            return None
        try:
            return int(str(getattr(result, "stdout", "") or "").strip().splitlines()[0])
        except (IndexError, ValueError):
            return None

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        pane_pid = self._pane_pid(host_ref)
        grace = grace_seconds if grace_seconds > 0 else self._grace_seconds
        if pane_pid is not None and _pid_alive(pane_pid):
            try:
                pgid = os.getpgid(pane_pid)
            except ProcessLookupError:
                pgid = None
            if pgid is not None:
                _sigterm_then_kill_process_group(pgid, grace)
            else:
                _sigterm_then_kill(pane_pid, None, grace)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self._run_fn(
                [self._tmux_bin, "kill-session", "-t", host_ref],
                capture_output=True, text=True, timeout=5,
            )

    def driver_channel(self, host_ref: str) -> _CodexTmuxDriverChannel | None:
        if not self.alive(host_ref):
            return None
        return _CodexTmuxDriverChannel(
            tmux_bin=self._tmux_bin,
            session=host_ref,
            run_fn=self._run_fn,
        )
