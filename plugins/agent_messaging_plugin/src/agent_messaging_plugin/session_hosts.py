"""Fleet session-management Phase B, D1+D2 (§5) — the ``HostDriver`` Protocol
+ the ``operator`` driver + the ``headless`` driver (``headless_adapter.py``,
the universal floor, hardening WS-6 prior art) + the ``tmux`` driver
(``tmux_adapter.py``, D2, hardening the R1 spike). All three ship REGISTERED
in this build. Host drivers bind through a declared, registered Protocol
(per §1.1 point 2), never duck-typed discovery.

NAMING (A3 ruling, `workbench/2026-08-03_phase_c_brief_and_d4_sitting_agenda_
coordinator_dawn.md`): the L2 mechanism is called "host driver" in prose from
this point on — "adapter" is retired terminology. The rename is vocabulary
only (this module's Protocol/classes/functions, docstrings, error text); the
§5 contract (spawn/alive/terminate/driver_channel/capability_report/
verify_config) and every module/file NAME are unchanged (``session_hosts.py``,
``headless_adapter.py``, ``tmux_adapter.py`` keep their existing paths — only
the identifiers and prose inside them moved).

Selection is DECLARED, never probed (C3): ``FLEET_SESSION_HOST`` env var (or
a per-spawn override), default ``tmux``. Every registered driver
is still config-gated fail-closed — an unconfigured environment (missing
binary, no ``SOLET_NAME``, no permission mode, no ``.mcp.json``, or for
``tmux`` an unsupported tmux version) gets ``host_cannot_spawn`` with the
exact remedies from that driver's ``verify_config``, never
``host_mechanism_missing`` (that token now means ONLY "no driver registered
at all in this build" — there is no such name left among ``operator``/
``headless``/``tmux``; a caller must request one of those or wait for the
next driver to land).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

_ENV_FLEET_SESSION_HOST = "FLEET_SESSION_HOST"
DEFAULT_HOST = "tmux"

OPERATOR_HOST = "operator"
SYNTHETIC_HOST = "synthetic"
QUALIFICATION_HOST = "qualification"
"""GAU-15 item 4 / GAU-24 — the gauge tamper canary's declared host. A
canary has no process, so this driver is registered but DEGENERATE exactly
like :class:`OperatorHostDriver`: ``spawn``/``terminate`` both refuse with
``HostCannotSpawnError`` rather than touching anything. Registering it
(rather than leaving the name unresolved) is deliberate and narrow —
``resolve_host_driver`` still raises :class:`HostMechanismMissingError` for
every host name that is genuinely unregistered (a typo, a future host not
yet built), so a real orphaned session on an unresolvable host is never
conflated with this literal, explicitly-declared name. The distinction that
matters is FAILED-TO-RESOLVE vs DECLARED-AND-NO-OP — collapsing those two
would let a real orphaned session be marked retired without anything being
torn down, which is exactly the ambiguous case GAU-24 named as unsafe."""
AGENT_RUNTIME_CLAUDE_CODE = "claude_code"
AGENT_RUNTIME_CODEX = "codex"
DEFAULT_AGENT_RUNTIME = AGENT_RUNTIME_CLAUDE_CODE
SUPPORTED_AGENT_RUNTIMES = frozenset(
    {AGENT_RUNTIME_CLAUDE_CODE, AGENT_RUNTIME_CODEX},
)


class DriverChannel(Protocol):
    """A live stdin/stream-json handle for clear/compact/drive (§5).
    ``clear_session``/``compact_session`` (AMEND 5b) send a slash-command and
    return — fire-and-forget, they do not read/await the resulting turn (the
    running session processes it on its own next event-loop pass; consuming
    the turn is a separate, later concern). ``send()`` is the whole contract
    for this slice; only the ``headless`` and ``tmux`` host drivers provide a
    real one (the ``operator`` driver's ``driver_channel()`` still returns
    ``None`` — ``unsupported_on_host``, never a silent degradation)."""

    def insert(self, text: str) -> None: ...

    def submit(self) -> None: ...

    def send(self, text: str) -> None: ...


@runtime_checkable
class ParkInterruptingDriverChannel(Protocol):
    """A channel that can interrupt a parked interactive pane safely.

    This intentionally stays separate from :class:`DriverChannel`: only a
    host with an interrupt key and a positive idle-prompt read-back may claim
    it.  The durable lifecycle ``parked`` row, not pane prose, selects it.
    """

    def interrupt_park(self) -> str: ...


@runtime_checkable
class ClearVerifyingDriverChannel(Protocol):
    """A ``DriverChannel`` that can positively READ BACK whether a
    ``/clear`` actually took effect (GAU-09, 2026-08-18).

    THE DEFECT THIS EXISTS TO CLOSE. ``clear_session`` used to report the
    SEND in a return value shaped like a verdict — measured returning
    ``success TRUE`` for a ``/clear`` that provably never happened, because
    ``send()`` alone can only ever mean "bytes left". ``ARMED != FIRED``. A
    channel implementing this protocol converts that guess into a
    measurement; a channel that does NOT implement it makes
    ``clear_session`` say so explicitly rather than quietly imply success.

    DELIBERATELY A SEPARATE PROTOCOL, NOT A WIDENED ``DriverChannel``. The
    capability is genuinely partial and always will be: a tmux pane can be
    read with ``capture-pane``, a headless stream-json stdin pipe has no
    pane to read at all, and the ``operator`` driver has no channel
    whatsoever. Folding an optional method into ``DriverChannel`` would
    force every implementation to grow a stub, and the only honest stub is
    one that returns "I do not know" — which is exactly the ambiguity the
    caller must be able to SEE. Keeping it a distinct, ``runtime_checkable``
    protocol makes the capability probe a real discrimination
    (``isinstance``) instead of a flag someone can set wrongly.

    ``verify_cleared`` returns ``True`` only on a POSITIVE observation of a
    cleared state, and ``False`` on a deadline passing without one. It must
    FAIL CLOSED, and it must NEVER re-send anything: each ``/clear`` fire
    deposits real text into a live input buffer, so a verification step that
    retried the send would be worse than the defect it closes.
    """

    def verify_cleared(self) -> bool: ...


@runtime_checkable
class SubmitVerifyingDriverChannel(Protocol):
    """A channel that can positively verify that submitted text became input."""

    def verify_submitted(self, text: str) -> bool | None: ...


@runtime_checkable
class DriveVerifyingDriverChannel(Protocol):
    """A ``DriverChannel`` that can positively READ BACK what happened to a
    ``drive_session`` dispatch (public issue #9, 2026-08-19) -- the
    ``drive_session`` sibling of :class:`ClearVerifyingDriverChannel`.

    THE DEFECT THIS EXISTS TO CLOSE. ``drive_session`` used to report the
    SEND in a return value shaped like a verdict -- measured returning
    ``success TRUE`` for a drive whose text sat unsubmitted in the target's
    input buffer, because ``send()`` alone can only ever mean "bytes left".
    ``ARMED != FIRED``, same class as GAU-09, one layer up.

    ``verify_driven`` differs from ``verify_cleared`` in what "positive"
    means: a cleared composer is unambiguous evidence of ITS effect, but an
    idle composer is evidence of BOTH a genuine submit and a drive that
    never landed at all -- so this method's own success path is not
    "composer went idle", it is "the driven text was never observed
    STRANDED, and the composer then went idle". The load-bearing check is
    the positive stranded-input detector (bright-white SGR 97 on the
    composer row, per the measured GAU-09/#9 pane read), not idle-by-
    absence. Returns ``True`` on a confirmed submit, ``False`` on a
    POSITIVE stranded-input observation, and ``None`` when a deadline
    passes with neither observed (could-not-determine, distinct from
    looked-and-failed). Like ``verify_cleared``, it must NEVER re-send
    anything: a drive fires real text into a live input buffer, so a
    verification step that retried would deposit a second copy.
    """

    def verify_driven(self, text: str) -> bool | None: ...


class DriverChannelSendError(Exception):
    """A host channel could not confirm that it accepted a dispatch.

    Claude's established channels remain best-effort and do not raise this
    error.  Runtime drivers that have an acknowledgement surface (Codex's
    app-server JSON-RPC response, or tmux pane-state verification) use it so
    ``first_turn_delivered=True`` and lifecycle-verb success never mean only
    "bytes were written somewhere".
    """


class HostDriver(Protocol):
    """Per-§5: each host implements this; the core layer never knows
    terminal specifics."""

    def spawn(self, spec: Mapping[str, object]) -> str:
        """Returns ``host_ref`` (tmux session name / driver pid / ...)."""
        ...

    def alive(self, host_ref: str) -> bool: ...

    def terminate(self, host_ref: str, grace_seconds: int) -> None: ...

    def driver_channel(self, host_ref: str) -> DriverChannel | None: ...

    def capability_report(self) -> dict[str, object]: ...

    def verify_config(self) -> list[str]:
        """Config-time, fail-closed remedies; empty = ready to spawn."""
        ...


class HostNotDeclaredError(Exception):
    """No host resolved at all: no per-spawn override, no
    ``FLEET_SESSION_HOST`` env, and no built-in default — cannot happen with
    :data:`DEFAULT_HOST` set, but the token exists for an explicit blank
    override (a caller passing ``host=""`` on purpose)."""

    def __init__(self) -> None:
        super().__init__(
            "host_not_declared: no host resolved (no per-spawn override, no "
            f"{_ENV_FLEET_SESSION_HOST} env, no default).",
        )


class HostMechanismMissingError(Exception):
    """The resolved host name has no host driver registered in THIS build —
    a known-missing mechanism (a future D-step driver, or a typo'd host
    name), never a silent substitute."""

    def __init__(self, host: str, remedy: str) -> None:
        self.host = host
        self.remedy = remedy
        super().__init__(f"host_mechanism_missing: {host!r} — {remedy}")


class AgentRuntimeNotSupportedError(Exception):
    """The requested peer-runtime vocabulary value is not implemented."""

    def __init__(self, agent_runtime: str) -> None:
        self.agent_runtime = agent_runtime
        super().__init__(
            f"agent_runtime_unsupported: {agent_runtime!r} is not one of "
            f"{sorted(SUPPORTED_AGENT_RUNTIMES)}; agent_runtime uses the exact "
            "peer-registry agent_id vocabulary.",
        )


class HostCannotSpawnError(Exception):
    """The resolved host driver is registered but DEGENERATE — it observes
    via registration only, never spawns (the ``operator`` driver, §5)."""

    def __init__(self, remedy: str) -> None:
        self.remedy = remedy
        super().__init__(f"host_cannot_spawn: {remedy}")


class OperatorHostDriver:
    """The degenerate v1 host driver (§5): explicit "no automation" state.
    Observes via registration (the normal ``peer_register`` flow already
    gives an operator-launched session a ``managed_session`` row, §3.2) —
    it is never dispatched THROUGH ``spawn_session``."""

    def spawn(self, spec: Mapping[str, object]) -> str:
        del spec
        raise HostCannotSpawnError(
            "the operator host driver cannot spawn; launch manually: "
            "SOLET_NAME=<name> python -m ananta.cli --app-home <profile>",
        )

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds
        raise HostCannotSpawnError(
            "the operator host driver cannot terminate a session it did not "
            "spawn; stop the process manually.",
        )

    def driver_channel(self, host_ref: str) -> DriverChannel | None:
        del host_ref
        return None

    def capability_report(self) -> dict[str, object]:
        return {"host": OPERATOR_HOST, "topology": "manual", "inspectable_via": []}

    def verify_config(self) -> list[str]:
        return []


class SyntheticHostDriver:
    """GAU-24 — the degenerate driver for a gauge tamper canary's lifecycle
    row (§5 pattern, alongside :class:`OperatorHostDriver`). A canary is
    never dispatched through ``spawn_session``; its row is minted directly
    by ``register_synthetic_session`` in ``spawning``, and no process ever
    exists behind it. ``terminate`` therefore has nothing to tear down and
    says so the same way the operator driver does — ``HostCannotSpawnError``
    is caught by ``terminate_session``'s own ``_terminate_host`` and treated
    as "no host action available", letting the ledger transition land
    without inventing a second no-op convention there."""

    def spawn(self, spec: Mapping[str, object]) -> str:
        del spec
        raise HostCannotSpawnError(
            "the synthetic host driver cannot spawn; a canary's lifecycle "
            "row is minted directly by register_synthetic_session, never "
            "through spawn_session.",
        )

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return False

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds
        raise HostCannotSpawnError(
            "the synthetic host driver has no process to terminate; a "
            "canary's row has none behind it by construction.",
        )

    def driver_channel(self, host_ref: str) -> DriverChannel | None:
        del host_ref
        return None

    def capability_report(self) -> dict[str, object]:
        return {"host": SYNTHETIC_HOST, "topology": "synthetic", "inspectable_via": []}

    def verify_config(self) -> list[str]:
        return []


class QualificationHostDriver:
    """A bounded non-LLM worker which exercises the ordinary watch bridge path."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def spawn(self, spec: Mapping[str, object]) -> str:
        instance_id = str(spec.get("agent_instance_id") or "")
        role = str(spec.get("local_name") or "")
        if not instance_id or not role or str(spec.get("worktree_path") or ""):
            raise HostCannotSpawnError(
                "qualification host requires an identity, role, and no worktree"
            )
        if shutil.which("solet-bridge") is None:
            raise HostCannotSpawnError("solet-bridge is required for qualification host")
        env = dict(os.environ)
        env["AGENT_INSTANCE_ID"] = instance_id
        env["AGENT_SESSION_ID"] = f"ases-{instance_id}"
        process = subprocess.Popen(
            ["solet-bridge", "watch", "--role", role, "--agent-id", "qualification", "--no-spool"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._processes[str(process.pid)] = process
        return str(process.pid)

    def alive(self, host_ref: str) -> bool:
        process = self._processes.get(host_ref)
        return process is not None and process.poll() is None

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        process = self._processes.get(host_ref)
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=max(1, grace_seconds))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        finally:
            self._processes.pop(host_ref, None)

    def driver_channel(self, host_ref: str) -> DriverChannel | None:
        del host_ref
        return None

    def capability_report(self) -> dict[str, object]:
        return {"host": QUALIFICATION_HOST, "topology": "bounded-watch", "inspectable_via": ["pid"]}

    def verify_config(self) -> list[str]:
        return [] if shutil.which("solet-bridge") else [
            "solet-bridge is required for qualification host"
        ]


RegistryKey = str | tuple[str, str]


def _build_registry() -> dict[RegistryKey, HostDriver]:
    # Deferred import: headless_adapter/tmux_adapter import HostCannotSpawnError
    # from THIS module, so a top-level import here would be circular.
    from .codex_adapter import CodexAppServerHostDriver, CodexTmuxHostDriver  # noqa: PLC0415
    from .headless_adapter import HeadlessHostDriver  # noqa: PLC0415
    from .tmux_adapter import TmuxHostDriver  # noqa: PLC0415

    return {
        (AGENT_RUNTIME_CLAUDE_CODE, OPERATOR_HOST): OperatorHostDriver(),
        (AGENT_RUNTIME_CLAUDE_CODE, "headless"): HeadlessHostDriver(),
        (AGENT_RUNTIME_CLAUDE_CODE, "tmux"): TmuxHostDriver(),
        (AGENT_RUNTIME_CLAUDE_CODE, SYNTHETIC_HOST): SyntheticHostDriver(),
        (AGENT_RUNTIME_CLAUDE_CODE, QUALIFICATION_HOST): QualificationHostDriver(),
        (AGENT_RUNTIME_CODEX, OPERATOR_HOST): OperatorHostDriver(),
        (AGENT_RUNTIME_CODEX, "headless"): CodexAppServerHostDriver(),
        (AGENT_RUNTIME_CODEX, "tmux"): CodexTmuxHostDriver(),
        (AGENT_RUNTIME_CODEX, SYNTHETIC_HOST): SyntheticHostDriver(),
        (AGENT_RUNTIME_CODEX, QUALIFICATION_HOST): QualificationHostDriver(),
    }


_REGISTRY: dict[RegistryKey, HostDriver] = _build_registry()


def shutdown_all_drivers() -> None:
    """Terminate every tracked worker across every registered host driver
    that has a ``shutdown`` method (currently just ``headless``) — wired
    into ``plugin.py``'s ``stop_services`` so a graceful shutdown/restart
    never leaves an orphaned worker process burning tokens with nothing
    tracking it."""
    seen: set[int] = set()
    for driver in _REGISTRY.values():
        # The degenerate operator driver may be registered for more than one
        # runtime.  A shared future driver must still be shut down once.
        if id(driver) in seen:
            continue
        seen.add(id(driver))
        shutdown = getattr(driver, "shutdown", None)
        if callable(shutdown):
            shutdown()


def _resolve_host_name(requested_host: str | None) -> str:
    if requested_host is not None and not requested_host.strip():
        raise HostNotDeclaredError()
    return (requested_host or os.environ.get(_ENV_FLEET_SESSION_HOST) or DEFAULT_HOST).strip()


def _driver_for(agent_runtime: str, host: str) -> HostDriver | None:
    driver = _REGISTRY.get((agent_runtime, host))
    if driver is not None or agent_runtime != DEFAULT_AGENT_RUNTIME:
        return driver
    # Test/embedding compatibility for the pre-runtime registry injection
    # seam. Production _build_registry returns tuple keys.
    return _REGISTRY.get(host)


def _registered_hosts(agent_runtime: str) -> list[str]:
    hosts: set[str] = set()
    for key in _REGISTRY:
        if isinstance(key, str):
            hosts.add(key)
        elif key[0] == agent_runtime:
            hosts.add(key[1])
    return sorted(hosts)


def resolve_host_driver(
    requested_host: str | None,
    agent_runtime: str = DEFAULT_AGENT_RUNTIME,
) -> tuple[HostDriver, str]:
    """Resolve ``requested_host`` (per-spawn override) or the
    ``FLEET_SESSION_HOST`` env default to a registered host driver.

    An explicitly BLANK override (``""``/whitespace — distinct from
    ``None``, "no override supplied") raises :class:`HostNotDeclaredError`
    rather than silently falling through to the env/default chain: a caller
    that explicitly declared "no host" gets refused, never a substitute it
    never asked for (C3). ``None`` (the ordinary case — no per-spawn
    override) falls through to the env var, then :data:`DEFAULT_HOST`.
    Raises :class:`HostMechanismMissingError` when the resolved name has no
    driver registered in this build.
    """
    if agent_runtime not in SUPPORTED_AGENT_RUNTIMES:
        raise AgentRuntimeNotSupportedError(agent_runtime)
    host = _resolve_host_name(requested_host)
    driver = _driver_for(agent_runtime, host)
    if driver is None:
        registered = _registered_hosts(agent_runtime)
        raise HostMechanismMissingError(
            host,
            f"no host driver registered for agent_runtime={agent_runtime!r}, "
            f"host={host!r} in this build "
            f"(registered: {registered}) — pass one of those or wait for a "
            "new host driver to land.",
        )
    return driver, host


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_AGENT_RUNTIME",
    "AGENT_RUNTIME_CLAUDE_CODE",
    "AGENT_RUNTIME_CODEX",
    "SUPPORTED_AGENT_RUNTIMES",
    "OPERATOR_HOST",
    "SYNTHETIC_HOST",
    "AgentRuntimeNotSupportedError",
    "ClearVerifyingDriverChannel",
    "SubmitVerifyingDriverChannel",
    "DriveVerifyingDriverChannel",
    "DriverChannel",
    "DriverChannelSendError",
    "HostCannotSpawnError",
    "HostDriver",
    "HostMechanismMissingError",
    "HostNotDeclaredError",
    "OperatorHostDriver",
    "SyntheticHostDriver",
    "resolve_host_driver",
    "shutdown_all_drivers",
]
