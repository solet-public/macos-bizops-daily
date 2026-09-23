"""Task #19 regression: lazy SwapOrchestrator picks up deferred action_factory.

2026-06-06 PM the local blue-green swap activated green but never drained
blue. Both adas served traffic in parallel until Coordinator-Day manually
SIGTERM'd blue. Root cause: ``MacosSelfDeploymentPlugin.prepare_for_readiness``
captured ``self.action_factory`` (then ``None``) into ``SwapOrchestrator``'s
constructor; the platform's ``init_actions`` step calls
``set_action_factory`` AFTER ``start_service_plugins`` (which ran
``prepare_for_readiness``), so the stored reference stayed ``None`` for the
orchestrator's lifetime. ``_enqueue_complete_swap`` then hit the None-guard
on every real swap and returned a synthetic audit token instead of
submitting the durable action. Green never got the SIGTERM-blue signal.

Option B fix (this commit): lazy orchestrator construction in
``_require_orchestrator``. Mirrors the cloud sibling's ``_build_deployer``
pattern; matches ``action_processor.py:_setup_plugin_context``'s
architectural intent ("ActionProcessor is the actual execution environment
and must provide all dependencies"). The orchestrator is built on first
verb-invocation, after ``set_action_factory`` has fired, so it always sees
the live factory.

This smoke regresses the specific bug:

* **Scenario 1** — orchestrator NOT pre-built. ``set_action_factory`` is
  called after readiness. ``_require_orchestrator`` returns a built
  orchestrator whose executor's ``_action_factory`` is the injected one
  (not the ``None`` it would have been under the buggy path). The
  enqueue spine — including the captured factory — lives on the
  orchestrator's ``SwapExecutor`` collaborator since the 2026-06-28
  rollback-verb extraction.

* **Scenario 2** — the executor's ``_enqueue_complete_swap`` calls
  ``submit_action_definition`` exactly once on the mock factory and
  returns the real action id (not a synthetic ``local_bg_restart_*``
  token). Asserts the bug's symptom is gone.

* **Scenario 3** — smoke-injected orchestrator override wins. Direct
  assignment to ``plugin._orchestrator`` keeps the existing
  ``swap_round_trip_smoke.py`` harness pattern working.

* **Scenario 4** — pre-injection guard. Calling ``_require_orchestrator``
  BEFORE ``set_action_factory`` fires raises ``RuntimeError`` with a
  clear message — fail-fast under operator misconfiguration.

No ``pytest``; runs directly via
``.venv/bin/python3 plugins/macos_self_deployment_plugin/tests/task19_action_factory_lazy_orchestrator_smoke.py``
and exits 0 on success, 1 on any failure with stderr detail.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Hermeticity: scenarios 1 & 2 reach `_get_release_manager` →
# `_resolve_project_root_for_autostart`, which reads APP_HOME to locate the
# live working-tree root. Outside the running platform APP_HOME is unset →
# KeyError. Point it at the repo's shared profile (its parent is the repo
# root, matching production's `<repo>/profile` shape) so the real
# ReleaseManager construction path stays exercised, hermetically. `setdefault`
# respects a real platform-provided APP_HOME when the smoke runs in-process.
os.environ.setdefault("APP_HOME", str(_PROJECT_ROOT / "profile"))

from macos_self_deployment_plugin.plugin import (  # noqa: E402
    MacosSelfDeploymentPlugin,
)
from macos_self_deployment_plugin.preflight_probe_runner import ProbeOutcome  # noqa: E402
from macos_self_deployment_plugin.release_manager import CandidatePaths  # noqa: E402
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    PreflightVerdict,
)
from macos_self_deployment_plugin.swap_orchestrator import (  # noqa: E402
    SwapOrchestrator,
)


class _MockActionFactory:
    """Records each submit_action_definition call."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []
        self._counter = 0

    def submit_action_definition(
        self,
        action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str:
        del context
        self.submissions.append(dict(action_definition))
        self._counter += 1
        return f"ae-task19-{self._counter}"



def _smoke_green_probe(*, candidate: CandidatePaths, app_home: Path) -> ProbeOutcome:
    """GTE-06 seam: a GREEN probe so this smoke's pre-existing flow is unchanged."""
    del app_home
    return ProbeOutcome(
        ok=True,
        payload={"ok": True, "duration_ms": 0, "release_id": candidate.release_id},
    )


def _build_plugin_post_readiness() -> MacosSelfDeploymentPlugin:
    """Plugin in the production post-readiness state: orchestrator NOT yet built.

    Mirrors what the platform looks like AFTER `prepare_for_readiness` ran
    (router client + identity set, NO _orchestrator yet) but BEFORE
    `set_action_factory` (`init_actions` step) fires.
    """
    plugin = MacosSelfDeploymentPlugin()
    plugin._solet_name = "example"
    plugin._self_color = "blue"
    plugin._self_instance_id = "example-blue-task19"
    plugin._router_client = MagicMock()
    # Orchestrator ref needed so _create_swap_session can mint a session
    # via state_service (post P0-A blue-green ironclad fix).
    plugin.orchestrator_ref = SimpleNamespace(  # type: ignore[assignment]
        create_session=lambda namespace, context_type, metadata: (  # noqa: ARG005
            "sess-task19-mock"
        ),
    )
    return plugin


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK  {message}")


def _scenario_lazy_orchestrator_picks_up_late_action_factory() -> None:
    print(
        "Scenario 1: lazy orchestrator captures action_factory injected "
        "AFTER readiness",
    )
    plugin = _build_plugin_post_readiness()
    _expect(
        plugin._orchestrator is None,
        "orchestrator is None at post-readiness pre-injection (matches production)",
    )
    factory = _MockActionFactory()
    plugin.set_action_factory(factory)  # type: ignore[arg-type]

    orchestrator = plugin._require_orchestrator()
    _expect(
        orchestrator._executor._action_factory is factory,
        "orchestrator's executor captured the FACTORY (not None — the historical bug)",
    )


def _scenario_enqueue_returns_real_action_id() -> None:
    print(
        "Scenario 2: _enqueue_complete_swap submits a real action and "
        "returns the real id",
    )
    plugin = _build_plugin_post_readiness()
    factory = _MockActionFactory()
    plugin.set_action_factory(factory)  # type: ignore[arg-type]
    orchestrator = plugin._require_orchestrator()

    action_id = orchestrator._executor._enqueue_complete_swap(
        prior_pid=12345,
        prior_instance_id="example-blue-prior",
        prior_color="blue",
        reason="task19-smoke",
    )
    _expect(
        len(factory.submissions) == 1,
        f"submit_action_definition called exactly once (calls={len(factory.submissions)})",
    )
    _expect(
        action_id == "ae-task19-1",
        f"real action id returned (got {action_id!r}; would be synthetic 'local_bg_restart_*' under the bug)",
    )
    _expect(
        not action_id.startswith("local_bg_restart_"),
        "action id is NOT the synthetic audit-token shape (regresses the bug)",
    )
    submission = factory.submissions[0]
    _expect(
        submission.get("name") == "complete_swap",
        f"submission name=complete_swap (got {submission.get('name')!r})",
    )
    _expect(
        submission.get("arguments", {}) == {  # type: ignore[union-attr]
            "prior_pid": 12345,
            "prior_instance_id": "example-blue-prior",
            "prior_color": "blue",
        },
        f"submission carries prior_(pid/instance_id/color) (got {submission.get('arguments')!r})",
    )


def _scenario_smoke_injected_orchestrator_wins() -> None:
    print("Scenario 3: smoke-injected orchestrator overrides the lazy build")
    plugin = _build_plugin_post_readiness()
    factory = _MockActionFactory()
    plugin.set_action_factory(factory)  # type: ignore[arg-type]
    injected = SwapOrchestrator(
        router_client=plugin._router_client,  # type: ignore[arg-type]
        action_factory=factory,  # type: ignore[arg-type]
        session_factory=lambda: "sess-smoke-task19",
        solet_name="smoke",
        release_manager=MagicMock(),  # type: ignore[arg-type]
        schema_preflight=lambda _c, **_kw: PreflightVerdict(is_additive=True, breaking_changes=()),
        preflight_probe=_smoke_green_probe,
        set_color_active=lambda _a: None,
    )
    plugin._orchestrator = injected
    _expect(
        plugin._require_orchestrator() is injected,
        "smoke-injected orchestrator wins over the lazy build path",
    )


def _scenario_pre_injection_guard() -> None:
    print(
        "Scenario 4: pre-injection guard raises RuntimeError with a clear message",
    )
    plugin = _build_plugin_post_readiness()
    _expect(
        plugin.action_factory is None,
        "preconditions: action_factory None (set_action_factory not yet called)",
    )
    try:
        plugin._require_orchestrator()
    except RuntimeError as exc:
        _expect(
            "action_factory not yet injected" in str(exc),
            f"RuntimeError mentions action_factory injection (got {exc!r})",
        )
        return
    _expect(False, "_require_orchestrator should have raised RuntimeError")


def main() -> int:
    print(
        "Task #19 smoke: lazy SwapOrchestrator picks up deferred action_factory\n",
    )
    _scenario_lazy_orchestrator_picks_up_late_action_factory()
    print()
    _scenario_enqueue_returns_real_action_id()
    print()
    _scenario_smoke_injected_orchestrator_wins()
    print()
    _scenario_pre_injection_guard()
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
