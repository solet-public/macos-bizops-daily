"""LaunchAgent autostart manager for macos_self_deployment_plugin.

Implements the macOS launchd integration for the three autostart verbs
exposed by ``MacosSelfDeploymentPlugin``:

- ``install_autostart`` — renders the per-solet LaunchAgent plist
  + ``launchctl load``s it. Idempotent across re-runs: an already-loaded
  agent is unloaded, the plist is rewritten, then re-loaded so an
  in-flight in-memory definition cannot go stale against a refreshed
  plist on disk.
- ``uninstall_autostart`` — unloads (if loaded) + removes the plist.
  Idempotent: absent plist returns success with prior_state=absent.
- ``status_autostart`` — read-only. Reports whether the plist exists
  on disk AND whether launchd knows the label, plus the
  ``LastExitStatus`` if any.

The manager exists as its own module (rather than methods on the
plugin class) for the same reason ``swap_orchestrator.py`` does: it
keeps the plugin's surface thin + lets the round-trip smoke wire a
sandbox-friendly ``plist_dir`` without monkeypatching plugin internals.

Sandbox-friendliness: ``plist_dir`` is a constructor parameter. The
default is ``~/Library/LaunchAgents/`` (canonical user-domain location
per Apple's launchd docs); smokes override to a sandbox scratch dir
(under ``~/.ananta/``, never ``/tmp`` per the operator no-/tmp rule)
so they never touch the operator's real LaunchAgents.

Option B (2026-06-28): the plist runs the colour-agnostic crash-
supervisor (``-m macos_self_deployment_plugin.supervisor``), NOT
``ananta.cli`` directly, so the launchd-managed process is never a solet
colour. ``KeepAlive`` is therefore an unconditional ``<true/>``: the
supervisor is an infinite poll loop with no intentional clean exit — if
it ever exits while loaded, restart it. Because no solet colour is launchd-
managed, a drained/SIGTERM'd colour is never respawned by launchd: the
ghost-respawn class is structurally impossible, so the interim
``Crashed``/``SuccessfulExit`` exit-code dance (and the earlier Slice-4
``PathState`` predicate) are both gone from the plist. Respawn of the
active colour is the supervisor's job (poll the router; spawn from
``current`` when no active colour); ``stop_self``'s persistent
``.draining`` sentinel suppresses that respawn for "operator wants the solet
off". See ``supervisor.py``.

The plist ``WorkingDirectory`` is the out-of-tree runtime dir
(``get_runtime_dir(name)``), NOT the repo root: a managed process must
never have its CWD set to a code tree, or a stray relative-path write
would mutate it (design ``2026-06-27_true_local_blue_green...`` §5).
Imports resolve via the venv ``.pth`` files, not CWD, so this is safe.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from xml.sax.saxutils import escape as _xml_escape

from ananta.core.runtime import get_runtime_dir
from ananta.interfaces.lifecycle_result_types import AutostartResult, AutostartStatus

from macos_self_deployment_plugin.constants import (
    AUTOSTART_LABEL_PREFIX,
    AUTOSTART_LOG_DIR_DEFAULT,
    AUTOSTART_PATH_ENV,
    AUTOSTART_PLIST_DIR_DEFAULT,
    AUTOSTART_SUPERVISOR_MODULE,
    PLUGIN_NAME,
)
from macos_self_deployment_plugin.release_manager import (
    CURRENT_LINK_NAME,
    RELEASES_ROOT_DEFAULT,
    VENV_DIRNAME,
)


@dataclass(frozen=True, slots=True)
class _ObservedState:
    """Pre-mutation observed state of a LaunchAgent."""

    plist_exists: bool
    launchctl_knows: bool
    last_exit_at: str  # ISO-8601 UTC, empty if unknown


class AutostartManager:
    """Render + load + unload + introspect a per-solet LaunchAgent.

    All filesystem + ``launchctl`` interactions live here. Smokes
    instantiate with a tmpfs ``plist_dir`` to validate the install +
    uninstall + status round-trip without touching the operator's real
    ``~/Library/LaunchAgents/``.
    """

    _DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS: ClassVar[float] = 10.0

    def __init__(
        self,
        *,
        solet_name: str,
        project_root: Path,
        plist_dir: Path | None = None,
        releases_root: Path | None = None,
        label_prefix: str = AUTOSTART_LABEL_PREFIX,
        log_dir: Path | None = None,
        launchctl_path: str = "/bin/launchctl",
        logger: logging.Logger | None = None,
    ) -> None:
        self._solet_name = solet_name
        self._project_root = project_root
        self._plist_dir = (
            plist_dir if plist_dir is not None
            else Path(AUTOSTART_PLIST_DIR_DEFAULT).expanduser()
        )
        # §4.5 role 1: the canonical materialized-release root whose
        # ``current`` symlink the cold-start plist launches. Injectable so
        # smokes point it at a throwaway scratch dir.
        self._releases_root = (
            releases_root if releases_root is not None
            else Path(RELEASES_ROOT_DEFAULT).expanduser() / solet_name
        )
        self._label = f"{label_prefix}.{solet_name}"
        self._log_dir = (
            log_dir if log_dir is not None
            else Path(AUTOSTART_LOG_DIR_DEFAULT).expanduser()
        )
        self._launchctl_path = launchctl_path
        self._logger = logger or logging.getLogger(PLUGIN_NAME)

    @property
    def label(self) -> str:
        return self._label

    @property
    def plist_path(self) -> Path:
        return self._plist_dir / f"{self._label}.plist"

    # ------------------------------------------------------------------
    # install_autostart
    # ------------------------------------------------------------------

    def install(self, *, dry_run: bool = False) -> AutostartResult:
        """Render + load the LaunchAgent. Idempotent unload-then-reload."""
        observed = self._observe()
        prior_state = self._classify_install_prior(observed)
        if dry_run:
            return self._result(
                status=AutostartStatus.DRY_RUN,
                verb="install_autostart",
                prior_state=prior_state,
                last_run_at=observed.last_exit_at,
                dry_run=True,
                message=(
                    f"dry_run=True; would write {self.plist_path} + "
                    f"launchctl load -w (prior_state={prior_state})"
                ),
            )

        try:
            if observed.launchctl_knows:
                # Unload before rewrite — launchctl does NOT auto-reload
                # on disk change, so writing then loading would leave
                # stale in-memory definition.
                self._launchctl(["unload", "-w", str(self.plist_path)])

            self._plist_dir.mkdir(parents=True, exist_ok=True)
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self.plist_path.write_bytes(self._render_plist())

            self._launchctl(["load", "-w", str(self.plist_path)])
        except subprocess.CalledProcessError as exc:
            return self._result(
                status=AutostartStatus.FAILED,
                verb="install_autostart",
                prior_state=prior_state,
                last_run_at=observed.last_exit_at,
                dry_run=False,
                message=f"launchctl failed: {exc.stderr or exc}",
            )
        except OSError as exc:
            return self._result(
                status=AutostartStatus.FAILED,
                verb="install_autostart",
                prior_state=prior_state,
                last_run_at=observed.last_exit_at,
                dry_run=False,
                message=f"filesystem error: {exc}",
            )

        return self._result(
            status=AutostartStatus.SUCCESS,
            verb="install_autostart",
            prior_state=prior_state,
            last_run_at=observed.last_exit_at,
            dry_run=False,
            message=(
                f"LaunchAgent {self._label} installed + loaded; "
                f"fires at next operator login (plist at {self.plist_path})"
            ),
        )

    # ------------------------------------------------------------------
    # uninstall_autostart
    # ------------------------------------------------------------------

    def uninstall(self, *, dry_run: bool = False) -> AutostartResult:
        """Unload (if loaded) + remove the plist. Idempotent."""
        observed = self._observe()
        prior_state = self._classify_uninstall_prior(observed)
        if dry_run:
            return self._result(
                status=AutostartStatus.DRY_RUN,
                verb="uninstall_autostart",
                prior_state=prior_state,
                last_run_at=observed.last_exit_at,
                dry_run=True,
                message=(
                    f"dry_run=True; would launchctl unload + unlink "
                    f"{self.plist_path} (prior_state={prior_state})"
                ),
            )
        if not observed.plist_exists and not observed.launchctl_knows:
            return self._result(
                status=AutostartStatus.SUCCESS,
                verb="uninstall_autostart",
                prior_state="absent",
                last_run_at="",
                dry_run=False,
                message="LaunchAgent already absent; no-op",
            )

        try:
            if observed.launchctl_knows:
                self._launchctl(["unload", "-w", str(self.plist_path)])
            if observed.plist_exists:
                self.plist_path.unlink()
        except subprocess.CalledProcessError as exc:
            return self._result(
                status=AutostartStatus.FAILED,
                verb="uninstall_autostart",
                prior_state=prior_state,
                last_run_at=observed.last_exit_at,
                dry_run=False,
                message=f"launchctl unload failed: {exc.stderr or exc}",
            )
        except OSError as exc:
            return self._result(
                status=AutostartStatus.FAILED,
                verb="uninstall_autostart",
                prior_state=prior_state,
                last_run_at=observed.last_exit_at,
                dry_run=False,
                message=f"plist unlink failed: {exc}",
            )

        return self._result(
            status=AutostartStatus.SUCCESS,
            verb="uninstall_autostart",
            prior_state=prior_state,
            last_run_at=observed.last_exit_at,
            dry_run=False,
            message=f"LaunchAgent {self._label} uninstalled",
        )

    # ------------------------------------------------------------------
    # status_autostart
    # ------------------------------------------------------------------

    def status(self) -> AutostartResult:
        """Read-only introspection. No side effects."""
        observed = self._observe()
        status_token = self._classify_status(observed)
        return self._result(
            status=status_token,
            verb="status_autostart",
            prior_state=status_token.value,
            last_run_at=observed.last_exit_at,
            dry_run=False,
            message=(
                f"plist_exists={observed.plist_exists} "
                f"launchctl_knows={observed.launchctl_knows} "
                f"label={self._label}"
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _observe(self) -> _ObservedState:
        plist_exists = self.plist_path.is_file()
        knows, last_at = self._launchctl_list()
        return _ObservedState(
            plist_exists=plist_exists,
            launchctl_knows=knows,
            last_exit_at=last_at,
        )

    def _launchctl_list(self) -> tuple[bool, str]:
        """Return (launchctl_knows_label, last_exit_iso). Best-effort."""
        try:
            completed = subprocess.run(
                [self._launchctl_path, "list", self._label],
                capture_output=True,
                text=True,
                timeout=self._DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, ""
        # launchctl list <label> exits non-zero when the label is unknown.
        if completed.returncode != 0:
            return False, ""
        # launchctl list <label> dumps a plist-like dict but does NOT
        # include a wall-clock timestamp of the last exit (only
        # LastExitStatus int). Surface empty until we plumb launchd's
        # unified log; honest beats fabricated.
        return True, ""

    def _launchctl(self, args: list[str]) -> None:
        subprocess.run(
            [self._launchctl_path, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=self._DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS,
        )

    def _resolve_autostart_interpreter(self) -> str:
        """The literal ``current`` release interpreter path baked into the plist.

        §4.5 role 1 (Architect verdict 2026-06-28): ALWAYS the
        ``<releases_root>/current/venv/bin/python3`` **symlink** path —
        emitted literally, NOT resolved here. launchd resolves the
        ``current`` symlink at EXEC time on every cold-start/reboot, so the
        plist always launches the durably-active release and is robust to
        every cutover/rollback flip with ZERO re-render.

        There is intentionally NO dev-venv / ``sys.executable`` fallback:
        resolving ``current`` at render time and falling back to the dev
        tree was a fast-fail violation (it would silently boot the WRONG
        tree forever if the plist was rendered before the first release).
        A missing ``current`` must fail loud at boot (launchd cannot exec a
        dangling symlink), never silently boot the wrong code. The plugin's
        install verb GUARANTEES ``current`` exists (seeds release-0) before
        the LaunchAgent is loaded — this method only DEPENDS on it.
        """
        return str(
            self._releases_root / CURRENT_LINK_NAME / VENV_DIRNAME / "bin" / "python3"
        )

    def _render_plist(self) -> bytes:
        """Render the LaunchAgent plist as hand-rolled XML.

        Avoids stdlib :mod:`plistlib` because that module imports
        :mod:`xml.parsers.expat` at module-load and a botched Homebrew
        Python install can leave pyexpat with an ABI mismatch against
        the system libexpat (observed on the operator's 2026-06-04
        ``python@3.13/3.13.13_1`` cellar). Hand-rolling keeps the verb
        operational even on broken interpreter installs. Inputs are
        operator-controlled and structurally constrained
        (``solet_name`` matches ``[a-z][a-z0-9_]*``, paths come
        from the operator's filesystem), but we XML-escape regardless
        per defensive-encoding hygiene.
        """
        interpreter = self._resolve_autostart_interpreter()
        profile_dir = self._project_root / "profile"
        stdout_log = self._log_dir / f"{self._solet_name}_autostart.log"
        # §5 CWD hygiene: WorkingDirectory must be out-of-tree, NOT the repo
        # root — else a relative-path write (error.log, a library temp/cache)
        # lands in the git working tree (.gitignore:49 /error.log confirms it
        # happened once). The runtime dir is the canonical out-of-tree home;
        # imports resolve via the venv .pth, not CWD.
        working_dir = get_runtime_dir(self._solet_name)
        # Option B: launch the colour-agnostic supervisor module (NOT
        # ``ananta.cli`` directly). The LaunchAgent has no shell to default
        # ``--app-home``, so explicit-pass is the only correct shape. The
        # interpreter is still the literal ``current/venv/bin/python3`` (§4.5
        # role 1) — the supervisor itself runs from ``current`` and re-resolves
        # it on every spawn. ``KeepAlive`` is an unconditional ``<true/>``:
        # the supervisor is an infinite loop, so any exit while loaded means
        # restart it. No solet colour is launchd-managed under this model, so
        # the ``Crashed``/``SuccessfulExit`` exit-code dance is obsolete (the
        # ghost-respawn class is structurally impossible); respawn of the
        # active colour is the supervisor's job, gated by ``stop_self``'s
        # ``.draining`` sentinel.
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            f'  <key>Label</key>\n  <string>{_xml_escape(self._label)}</string>\n'
            '  <key>ProgramArguments</key>\n  <array>\n'
            f'    <string>{_xml_escape(interpreter)}</string>\n'
            '    <string>-m</string>\n'
            f'    <string>{_xml_escape(AUTOSTART_SUPERVISOR_MODULE)}</string>\n'
            '    <string>--app-home</string>\n'
            f'    <string>{_xml_escape(str(profile_dir))}</string>\n'
            '  </array>\n'
            f'  <key>WorkingDirectory</key>\n  <string>{_xml_escape(str(working_dir))}</string>\n'
            '  <key>EnvironmentVariables</key>\n  <dict>\n'
            f'    <key>SOLET_NAME</key>\n    <string>{_xml_escape(self._solet_name)}</string>\n'
            '    <key>FLEET_SESSION_HOST</key>\n    <string>tmux</string>\n'
            # §39.2: without this key the daemon gets launchd's bare PATH and
            # cannot see Homebrew binaries (tmux) even when installed. See
            # AUTOSTART_PATH_ENV for why it is a fixed literal.
            f'    <key>PATH</key>\n    <string>{_xml_escape(AUTOSTART_PATH_ENV)}</string>\n'
            '  </dict>\n'
            '  <key>RunAtLoad</key>\n  <true/>\n'
            '  <key>KeepAlive</key>\n  <true/>\n'
            f'  <key>StandardOutPath</key>\n  <string>{_xml_escape(str(stdout_log))}</string>\n'
            f'  <key>StandardErrorPath</key>\n  <string>{_xml_escape(str(stdout_log))}</string>\n'
            '</dict>\n'
            '</plist>\n'
        )
        return body.encode("utf-8")

    def _classify_install_prior(self, observed: _ObservedState) -> str:
        if not observed.plist_exists and not observed.launchctl_knows:
            return "absent"
        if observed.plist_exists and observed.launchctl_knows:
            existing_bytes = self.plist_path.read_bytes()
            return (
                "present_already_current"
                if existing_bytes == self._render_plist()
                else "present_but_stale"
            )
        if observed.plist_exists and not observed.launchctl_knows:
            return "present_not_loaded"
        # launchctl knows label but no plist on disk — operator
        # tampered with the file. Treat as stale; re-write recovers.
        return "present_but_stale"

    def _classify_uninstall_prior(self, observed: _ObservedState) -> str:
        if observed.launchctl_knows and observed.plist_exists:
            return "present_and_loaded"
        if observed.plist_exists:
            return "present_not_loaded"
        return "absent"

    def _classify_status(self, observed: _ObservedState) -> AutostartStatus:
        if not observed.plist_exists and not observed.launchctl_knows:
            return AutostartStatus.NOT_INSTALLED
        if observed.plist_exists and observed.launchctl_knows:
            return AutostartStatus.INSTALLED_LOADED
        return AutostartStatus.INSTALLED_NOT_LOADED

    def _result(
        self,
        *,
        status: AutostartStatus,
        verb: str,
        prior_state: str,
        last_run_at: str,
        dry_run: bool,
        message: str,
    ) -> AutostartResult:
        self._logger.info(
            "%s %s: status=%s prior=%s message=%s",
            PLUGIN_NAME, verb, status.value, prior_state, shlex.quote(message),
        )
        return AutostartResult(
            status=status,
            verb=verb,
            solet_name=self._solet_name,
            label=self._label,
            plist_path=str(self.plist_path),
            prior_state=prior_state,
            last_run_at=last_run_at,
            dry_run=dry_run,
            message=message,
        )


__all__ = ["AutostartManager"]
