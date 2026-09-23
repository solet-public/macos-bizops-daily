"""Slice F — simple LaunchAgent autostart renderer (Layer 1, in-venv).

RISKY / boot-path: gets a Codex/Rev pass before landing (build spec §9).

Forks the SIMPLE render out of
`macos_self_deployment_plugin/autostart_manager.py`, dropping the
supervisor / `current`-release / `PathState` machinery (build spec
§6.2): genesis has no self-deployment blue-green rotation to resolve,
so the plist launches `ananta.cli` directly at a STATIC clone path, not
a `current` symlink indirection. Own-copy per convention (genesis's
profile does not bind `macos_self_deployment_plugin` at all).

Plist requirements (build spec §6.2 — every field here is load-bearing
on the boot path):
  * `Label`: `local.solet.<name>` (mirrors the existing convention).
  * `EnvironmentVariables`: `{SOLET_NAME: <name>, PATH: <fixed>}`.
    Boot FAST-FAILS without `SOLET_NAME` (`environment_config.py`
    identity/schema routing + the DB-password vault-key resolution both
    key off it). `PATH` is a deterministic Homebrew-inclusive literal
    (`_PATH_ENV`, §39.2): a launchd process with no `PATH` key gets the
    bare `/usr/bin:/bin:/usr/sbin:/sbin`, so `shutil.which("tmux")`
    fails in-daemon even with tmux installed under `/opt/homebrew/bin`.
  * `WorkingDirectory`: `~/.ananta/runtime/<name>` — NOT the clone
    root. A launchd-managed process's CWD must never be a code tree: a
    stray relative-path write (an error log, a library temp/cache)
    would mutate the git working tree. Deliberately NOT
    `ananta.core.runtime.get_runtime_dir()` — that helper (checked
    2026-07-09) returns a single SHARED `~/.ananta/runtime` directory
    (or `$XDG_RUNTIME_DIR/ananta`) with NO per-solet-name segment
    at all, despite accepting a `solet_name` parameter it never
    uses in the returned path. This module computes the exact spec'd
    path directly instead.
  * `ProgramArguments`: `[<clone>/.venv/bin/python3, -m, ananta.cli,
    --app-home, <clone>/profile]` — full absolute paths, matching the
    canonical invocation in the paired root bootstrap files. A bare
    `"profile"` argument (the
    build spec's shorthand) would resolve against `WorkingDirectory`
    (the runtime dir), not the clone — wrong; this renders the
    absolute path instead.
  * `RunAtLoad`: `true`.
  * `KeepAlive`: the SIMPLE crash-restart form — `{SuccessfulExit:
    false}` (restart on a crash / non-zero exit; do NOT respawn a
    clean, intentional exit). The reference's unconditional `<true/>`
    is specific to ITS infinite-loop supervisor (Option B); genesis
    launches `ananta.cli` directly, which DOES have a meaningful
    clean-exit-vs-crash distinction, so the simple form is correct here.

Avoids stdlib `plistlib` for the same reason the reference does: that
module imports `xml.parsers.expat` at load time, and a botched Homebrew
Python install can leave `pyexpat` ABI-mismatched against the system
`libexpat`. Hand-rolled XML (via `xml.sax.saxutils.escape`, NOT
`xml.parsers.expat`) stays operational even on a broken interpreter.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

_TEMPLATE_REF = "plugins/github_midwife_plugin/knowledge_base/hydration_templates/launchagent.plist.template"
_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "knowledge_base" / "hydration_templates" / "launchagent.plist.template"
_STAMP = "<!-- rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST} -->"

_LABEL_PREFIX = "local.solet"
# PATH written into the plist's EnvironmentVariables (§39.2, reported and
# field-verified by a seed adopter). A launchd-spawned process inherits no login
# shell: with no PATH key it gets launchd's bare ``/usr/bin:/bin:/usr/sbin:/sbin``,
# which excludes both Homebrew prefixes, so in-daemon ``shutil.which("tmux")``
# returns None on a machine where tmux IS installed at ``/opt/homebrew/bin/tmux``
# -- present but invisible, and tmux is the substrate of the swap-durable fleet
# host. A born clone hits this wall on its first tmux-hosted spawn.
#
# Fixed literal, never the operator's live ``$PATH``: capturing the interactive
# PATH would make the render vary by whoever ran genesis (breaking the
# byte-comparison staleness check in ``_classify_install_prior``) and would leak
# the operator's local layout into a generated artifact. Both Homebrew prefixes
# ship unconditionally (``/opt/homebrew`` Apple Silicon, ``/usr/local`` Intel)
# rather than arch-detected -- a non-existent directory on PATH is inert, and
# one literal keeps the render arch-independent.
#
# Own-copy of macos_self_deployment_plugin.constants.AUTOSTART_PATH_ENV, per
# this module's stated fork convention (genesis's profile does not bind that
# plugin at all -- see the module docstring). Keep the two values in lock-step.
_PATH_ENV = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:"
    "/usr/bin:/bin:/usr/sbin:/sbin"
)
_LAUNCHCTL_TIMEOUT_S = 10.0
_LAUNCHCTL_NOT_FOUND_MARKER = "Could not find service"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class AutostartError(RuntimeError):
    """Raised when a launchctl or filesystem operation fails."""


class LaunchctlObservationError(AutostartError):
    """Raised when the `launchctl list <label>` PROBE itself cannot
    produce a legitimate load-state observation (Codex F must-fix,
    2026-07-09).

    Distinct from `AutostartError`'s other raise site (`_launchctl`,
    a REQUESTED load/unload verb failing): this is the read-only probe
    failing to observe anything at all -- an execution failure
    (OSError/timeout/launchctl missing) or an exit shape that doesn't
    match launchctl's normal not-loaded signal. The prior behavior
    silently coerced both into `launchctl_knows=False`, which made
    `uninstall()` skip the unload step on a transient failure and
    orphan a live launchd job with no plist backing it, and made
    `status()` report "not loaded" for a state it never actually
    observed. This is a subclass of `AutostartError` specifically so
    it is NEVER caught and re-coerced by a bare `except
    (OSError, subprocess.TimeoutExpired)` anywhere in this class --
    it propagates through `_observe()` untouched, which makes
    `status()` surface it verbatim (no silent normalization to
    `not_installed`) and `uninstall()` refuse to act (raises before
    any unload/unlink is attempted -- fail-loud, plist left in place)
    for free, without either method needing its own special-case.
    """


@dataclass(frozen=True, slots=True)
class _ObservedState:
    plist_exists: bool
    launchctl_knows: bool


@dataclass(frozen=True, slots=True)
class AutostartResult:
    status: str
    verb: str
    label: str
    plist_path: str
    prior_state: str
    message: str


@dataclass
class SimpleAutostartRenderer:
    """Render + load + unload + introspect the genesis LaunchAgent.

    `plist_dir`/`home_dir`/`run` are constructor-injectable so a smoke
    can point them at a tmpfs scratch dir + a fake subprocess runner —
    the round-trip never touches the operator's real
    `~/Library/LaunchAgents/`, `~/.ananta/`, or `launchctl`.
    """

    solet_name: str
    clone_root: Path
    plist_dir: Path
    home_dir: Path
    run: Runner
    launchctl_path: str = "/bin/launchctl"
    label: str = field(init=False)

    def __post_init__(self) -> None:
        self.label = f"{_LABEL_PREFIX}.{self.solet_name}"

    @property
    def plist_path(self) -> Path:
        return self.plist_dir / f"{self.label}.plist"

    @property
    def runtime_dir(self) -> Path:
        return self.home_dir / ".ananta" / "runtime" / self.solet_name

    # ── install ──────────────────────────────────────────────────────

    def install(self) -> AutostartResult:
        """Render + load the LaunchAgent. Idempotent: unload-then-reload."""
        observed = self._observe()
        prior_state = self._classify_install_prior(observed)

        if observed.launchctl_knows:
            # Unload before rewrite -- launchctl does NOT auto-reload on
            # disk change; loading over a stale in-memory definition
            # would leave the OLD plist's settings live.
            self._launchctl(["unload", "-w", str(self.plist_path)])

        self.plist_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(self._render_plist())
        self._launchctl(["load", "-w", str(self.plist_path)])

        return AutostartResult(
            status="success", verb="install_autostart", label=self.label,
            plist_path=str(self.plist_path), prior_state=prior_state,
            message=f"LaunchAgent {self.label} installed + loaded",
        )

    # ── uninstall ────────────────────────────────────────────────────

    def uninstall(self) -> AutostartResult:
        """Unload (if loaded) + remove the plist. Idempotent: absent -> success."""
        observed = self._observe()
        prior_state = self._classify_uninstall_prior(observed)

        if not observed.plist_exists and not observed.launchctl_knows:
            return AutostartResult(
                status="success", verb="uninstall_autostart", label=self.label,
                plist_path=str(self.plist_path), prior_state="absent",
                message="LaunchAgent already absent; no-op",
            )

        if observed.launchctl_knows:
            self._launchctl(["unload", "-w", str(self.plist_path)])
        if observed.plist_exists:
            self.plist_path.unlink()

        return AutostartResult(
            status="success", verb="uninstall_autostart", label=self.label,
            plist_path=str(self.plist_path), prior_state=prior_state,
            message=f"LaunchAgent {self.label} uninstalled",
        )

    # ── status ───────────────────────────────────────────────────────

    def status(self) -> AutostartResult:
        """Read-only introspection. No side effects."""
        observed = self._observe()
        status_token = self._classify_status(observed)
        return AutostartResult(
            status=status_token, verb="status_autostart", label=self.label,
            plist_path=str(self.plist_path), prior_state=status_token,
            message=(
                f"plist_exists={observed.plist_exists} "
                f"launchctl_knows={observed.launchctl_knows} label={self.label}"
            ),
        )

    # ── internals ────────────────────────────────────────────────────

    def _observe(self) -> _ObservedState:
        return _ObservedState(
            plist_exists=self.plist_path.is_file(),
            launchctl_knows=self._launchctl_knows_label(),
        )

    def _launchctl_knows_label(self) -> bool:
        """True if launchctl reports the label loaded; False if launchctl
        LEGITIMATELY reports it not loaded ("Could not find service" on
        stderr, the normal not-found signal). Raises
        `LaunchctlObservationError` for everything else -- the probe
        itself failing to run, or a nonzero exit that doesn't match the
        normal not-found shape -- rather than guessing (see that
        exception's docstring for why this discrimination matters).
        """
        try:
            result = self.run(
                [self.launchctl_path, "list", self.label],
                capture_output=True, text=True, timeout=_LAUNCHCTL_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LaunchctlObservationError(
                f"launchctl list {self.label!r} could not be executed: {exc}"
            ) from exc
        if result.returncode == 0:
            return True
        if _LAUNCHCTL_NOT_FOUND_MARKER in (result.stderr or ""):
            return False
        raise LaunchctlObservationError(
            f"launchctl list {self.label!r} exited {result.returncode} with "
            "an unrecognized failure shape (not the normal not-found "
            f"signal, {_LAUNCHCTL_NOT_FOUND_MARKER!r}) -- cannot determine "
            "load state."
        )

    def _launchctl(self, args: list[str]) -> None:
        try:
            result = self.run(
                [self.launchctl_path, *args],
                capture_output=True, text=True, timeout=_LAUNCHCTL_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutostartError(f"launchctl {args[0]} failed to run: {exc}") from exc
        if result.returncode != 0:
            raise AutostartError(f"launchctl {args[0]} failed: {result.stderr.strip()}")

    def _render_plist(self) -> bytes:
        """The stamped render of the shipped ``launchagent.plist.template`` for this instance.

        The template is the byte-for-byte layout this method used to hand-build
        (every field below stays load-bearing on the boot path); rendering it
        from the shipped template is what lets a later release identify a
        genesis-written plist by its ``rendered-from`` stamp instead of
        guessing (existing-install design section 6.1).  Values are
        XML-escaped before literal substitution.
        """
        return render_launchagent_plist(self.solet_name, self.clone_root, self.home_dir, template_text=None, stamp=None, stamped=True)

    def _classify_install_prior(self, observed: _ObservedState) -> str:
        if not observed.plist_exists and not observed.launchctl_knows:
            return "absent"
        if observed.plist_exists and observed.launchctl_knows:
            return (
                "present_already_current"
                if self.plist_path.read_bytes() == self._render_plist()
                else "present_but_stale"
            )
        if observed.plist_exists and not observed.launchctl_knows:
            return "present_not_loaded"
        return "present_but_stale"  # launchctl knows but no plist on disk -- treat as stale

    def _classify_uninstall_prior(self, observed: _ObservedState) -> str:
        if observed.launchctl_knows and observed.plist_exists:
            return "present_and_loaded"
        if observed.plist_exists:
            return "present_not_loaded"
        return "absent"

    def _classify_status(self, observed: _ObservedState) -> str:
        if not observed.plist_exists and not observed.launchctl_knows:
            return "not_installed"
        if observed.plist_exists and observed.launchctl_knows:
            return "installed_loaded"
        return "installed_not_loaded"


def render_launchagent_plist(
    solet_name: str,
    clone_root: Path,
    home_dir: Path,
    *,
    template_text: str | None,
    stamp: str | None,
    stamped: bool = True,
) -> bytes:
    """Render the LaunchAgent plist from the shipped template with XML-escaped values.

    ``template_text`` defaults to the installed template; ``stamp`` defaults to
    the ``rendered-from`` line for that template's digest and is inserted as
    the second line (after the XML declaration).  ``stamped=False`` renders the
    exact legacy, unstamped bytes so an existing-install adapter can recognise a
    plist genesis wrote before stamping existed.
    """
    text = _TEMPLATE_PATH.read_text(encoding="utf-8") if template_text is None else template_text
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    runtime_dir = home_dir / ".ananta" / "runtime" / solet_name
    values = {
        "{{SOLET_NAME}}": _xml_escape(solet_name),
        "{{CLONE_DIR}}": _xml_escape(str(clone_root)),
        "{{RUNTIME_DIR}}": _xml_escape(str(runtime_dir)),
        "{{PATH_ENV}}": _xml_escape(_PATH_ENV),
    }
    rendered = text
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    if "{{" in rendered:
        raise AutostartError("launchagent.plist.template carries an unresolved token")
    if not stamped:
        return rendered.encode("utf-8")
    line = _STAMP.replace("{TEMPLATE_REF}", _TEMPLATE_REF).replace("{TEMPLATE_DIGEST}", digest) if stamp is None else stamp
    lines = rendered.split("\n")
    lines.insert(1, line)
    return "\n".join(lines).encode("utf-8")


__all__ = [
    "AutostartError",
    "AutostartResult",
    "LaunchctlObservationError",
    "SimpleAutostartRenderer",
    "render_launchagent_plist",
]
