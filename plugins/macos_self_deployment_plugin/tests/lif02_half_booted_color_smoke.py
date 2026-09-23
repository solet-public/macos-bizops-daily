"""LIF-02 regression: restart delegation must not depend on this color's own readiness.

2026-07-16 live incident (register D4 / LIF-02, Coordinator-Dusk): a color
whose ``prepare_for_readiness`` had not yet reached the router-client
assignment received an ``apply_manifest``-delegated ``restart_with_manifest``
call. ``_require_orchestrator`` raised ``RuntimeError("router client not
initialized — prepare_for_readiness not called?")`` before the swap machinery
ever ran. Recovery required SIGKILL (SIGTERM ignored 180s) plus a manual
``python -m ananta.cli`` relaunch — apply_manifest wrote the new manifest to
disk but left the color stuck, unable to either complete or cleanly refuse
its own restart.

Root cause: ``self._router_client`` is a pure ordering flag, set only at one
specific line inside ``prepare_for_readiness``. ``RouterClient.__init__`` does
no I/O (see its own docstring: "no connection cached") — building one needs
only the solet name, which is in the launch environment (``SOLET_NAME``) long
before readiness completes. The gate was checking "has readiness reached this
bookkeeping assignment yet?" instead of "can a router client actually be
built?" — the wrong question for a self-deployment verb whose whole point is
to run against a color that may not be fully up.

The fix (``_lazy_router_client``, used by both ``_require_client`` and
``_require_orchestrator``): build a ``RouterClient`` on demand from
``self._solet_name or os.environ[SOLET_NAME]`` when ``self._router_client``
is still ``None``, instead of raising. This lets the call reach
``SwapOrchestrator._prepare_swap``, whose own first step (``router.status()``
to confirm self is the active color) already has a clean, typed FAILED path
for exactly this situation (``RestartReasonCode.NOT_ACTIVE_INSTANCE`` /
``ROUTER_UNREACHABLE``) — turning an opaque crash requiring manual SIGKILL
into the same graceful refusal every other pre-activate failure already gets.

Four scenarios:

1. **Half-booted color still gets an orchestrator.** ``_router_client=None``,
   ``_solet_name`` set, ``action_factory`` set → ``_require_orchestrator()``
   does not raise, and the orchestrator's router client points at the right
   socket path. Reverting the fix (restoring the old unconditional raise on
   ``self._router_client is None``) fails this scenario.
2. **Same for ``_require_client``** (the ``swap_status`` / ``swap_rollback``
   verb path) — no raise, correct socket path.
3. **Lazy build never mutates ``self._router_client``.** The heartbeat-thread
   spawn guard (``plugin.py``: ``if ... or self._router_client is None:
   return``) depends on that field staying ``None`` until real readiness sets
   it; a lazy build that cached its result onto ``self._router_client`` would
   silently defeat that guard and let the heartbeat thread spawn against an
   unready environment.
4. **Truly unresolvable case still raises, with the RIGHT message.** No
   ``_solet_name``, no ``SOLET_NAME`` env var → both helpers still raise
   ``RuntimeError``, but the message names the actual unresolvable
   dependency (``SOLET_NAME`` unset) instead of the misleading "readiness"
   framing — the one case where nothing lazy can help.

No ``pytest``; runs directly via
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/lif02_half_booted_color_smoke.py
and exits 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("APP_HOME", str(_PROJECT_ROOT / "profile"))

from macos_self_deployment_plugin.constants import ENV_SOLET_NAME  # noqa: E402
from macos_self_deployment_plugin.plugin import (  # noqa: E402
    MacosSelfDeploymentPlugin,
    _router_socket_path,
)


class _MockActionFactory:
    def submit_action_definition(
        self,
        action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        del action_definition, context
        return "ae-lif02-1"


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK  {message}")


def _build_half_booted_plugin(*, solet_name: str) -> MacosSelfDeploymentPlugin:
    """A color stuck BEFORE prepare_for_readiness sets ``_router_client``.

    Unlike ``task19``'s ``_build_plugin_post_readiness`` (router client
    already a MagicMock, matching production AFTER readiness), this leaves
    ``_router_client`` at its ``__init__`` default of ``None`` — the exact
    field state the register's live incident measured.
    """
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = solet_name
    plugin._self_color = "blue"
    plugin._self_instance_id = "example-blue-lif02"
    plugin.orchestrator_ref = SimpleNamespace(  # type: ignore[assignment]
        create_session=lambda namespace, context_type, metadata: (  # noqa: ARG005
            "sess-lif02-mock"
        ),
    )
    return plugin


def _scenario_orchestrator_survives_half_boot() -> None:
    print("Scenario 1: half-booted color still gets an orchestrator")
    plugin = _build_half_booted_plugin(solet_name="lif02-smoke")
    _expect(plugin._router_client is None, "precondition: router client unset (half-booted)")
    plugin.set_action_factory(_MockActionFactory())  # type: ignore[arg-type]

    try:
        orchestrator = plugin._require_orchestrator()
    except RuntimeError as exc:
        _expect(False, f"_require_orchestrator raised on a half-booted color: {exc!r}")
        return
    _expect(
        orchestrator._router.socket_path == _router_socket_path("lif02-smoke"),
        "orchestrator's router client points at the solet's own socket path",
    )


def _scenario_require_client_survives_half_boot() -> None:
    print("Scenario 2: swap_status/swap_rollback path (_require_client) survives half-boot")
    plugin = _build_half_booted_plugin(solet_name="lif02-smoke")

    try:
        client = plugin._require_client()
    except RuntimeError as exc:
        _expect(False, f"_require_client raised on a half-booted color: {exc!r}")
        return
    _expect(
        client.socket_path == _router_socket_path("lif02-smoke"),
        "lazily-built client points at the solet's own socket path",
    )


def _scenario_lazy_build_does_not_mutate_field() -> None:
    print("Scenario 3: lazy build never caches onto self._router_client")
    plugin = _build_half_booted_plugin(solet_name="lif02-smoke")
    plugin.set_action_factory(_MockActionFactory())  # type: ignore[arg-type]
    plugin._require_client()
    plugin._require_orchestrator()
    _expect(
        plugin._router_client is None,
        "self._router_client still None after two lazy builds — heartbeat "
        "spawn guard (`... or self._router_client is None: return`) is untouched",
    )


def _scenario_truly_unresolvable_still_raises() -> None:
    print("Scenario 4: no solet_name anywhere still raises, with the honest message")
    saved = os.environ.pop(ENV_SOLET_NAME, None)
    try:
        plugin = _build_half_booted_plugin(solet_name="")
        try:
            plugin._require_client()
        except RuntimeError as exc:
            _expect(
                ENV_SOLET_NAME in str(exc),
                f"RuntimeError names {ENV_SOLET_NAME} as the unresolved dependency (got {exc!r})",
            )
            _expect(
                "prepare_for_readiness not called" not in str(exc),
                f"message no longer blames readiness ordering (got {exc!r})",
            )
        else:
            _expect(False, "_require_client should have raised with no solet_name resolvable")
    finally:
        if saved is not None:
            os.environ[ENV_SOLET_NAME] = saved


def main() -> int:
    print("LIF-02 smoke: apply_manifest restart delegation on a half-booted color\n")
    _scenario_orchestrator_survives_half_boot()
    print()
    _scenario_require_client_survives_half_boot()
    print()
    _scenario_lazy_build_does_not_mutate_field()
    print()
    _scenario_truly_unresolvable_still_raises()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
