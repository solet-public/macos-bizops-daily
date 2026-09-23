#!/usr/bin/env python3
"""T2 reconciliation-cutover controller smoke — CAS legs refuse before any spawn.

The killing mutation for the CAS checks is deleting any leg from
``_require_compare_and_swap``: the matching stale-* check below goes red because
the exploding executor is reached (verified — deleting the active-instance leg
turns that one check red and leaves the rest green).  The killing mutation for
the ordering guarantee is moving the executor call above the CAS block: every
stale-* check goes red at once.

Deliberately NOT claimed: that these checks would catch ``CutoverProvenance``
being built from the request's expected digests instead of the observed ones.
They would not, and no test can — the surface-digest CAS legs have already
proven the two equal by the time provenance is constructed.  What the
provenance checks actually pin is that the digests and the reconciliation id
reach the executor and the outcome at all.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))

from macos_self_deployment_plugin.reconciliation_cutover import (  # noqa: E402
    CUTOVER_REASON,
    REFUSAL_ACTIVE_INSTANCE,
    REFUSAL_ACTIVE_START_TOKEN,
    REFUSAL_ATTEMPT_IN_FLIGHT,
    REFUSAL_CURRENT_RELEASE,
    REFUSAL_MANIFEST_ETAG,
    REFUSAL_NOTHING_TO_RECOVER,
    REFUSAL_RELEASE_SURFACE,
    REFUSAL_SOURCE_SURFACE,
    CutoverOutcome,
    CutoverProvenance,
    CutoverRefusalError,
    ReconciliationCutoverController,
    ReconciliationCutoverRequest,
    SwapEvidence,
    TargetRuntimeObservation,
    observation_from_attestation,
)

_SOURCE = "sha256:" + "a" * 64
_RELEASE = "sha256:" + "b" * 64
_OTHER = "sha256:" + "c" * 64


def _observation(**overrides: object) -> TargetRuntimeObservation:
    fields = {
        "current_release_id": "rel-1",
        "active_instance_id": "inst-1",
        "active_pid": 701,
        "active_color": "blue",
        "active_start_token": "tok-1",
        "manifest_etag": "etag-1",
        "source_surface_sha256": _SOURCE,
        "release_surface_sha256": _RELEASE,
    }
    fields.update(overrides)
    return TargetRuntimeObservation(**fields)  # type: ignore[arg-type]


def _request() -> ReconciliationCutoverRequest:
    return ReconciliationCutoverRequest(
        reconciliation_id="rec_smoke1",
        expected_source_surface_sha256=_SOURCE,
        expected_release_surface_sha256=_RELEASE,
        expected_manifest_etag="etag-1",
        expected_current_release_id="rel-1",
        expected_active_instance_id="inst-1",
        expected_active_start_token="tok-1",
    )


class _ExplodingExecutor:
    """Reaching this IS the failure: a refused CAS must never spawn."""

    def __call__(self, **_: object) -> SwapEvidence:
        raise AssertionError("executor invoked after a CAS refusal")


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> SwapEvidence:
        self.calls.append(kwargs)
        return SwapEvidence(status="queued", reason_code="none")


def _refusal_code(observation: TargetRuntimeObservation) -> str:
    controller = ReconciliationCutoverController(
        observe=lambda: observation,
        execute_swap=_ExplodingExecutor(),
    )
    try:
        controller.cutover(_request())
    except CutoverRefusalError as refusal:
        return refusal.code
    except AssertionError:
        # The exploding executor fired: a CAS leg is missing.  Reported as a
        # value so the smoke prints a legible FAIL instead of dying mid-run.
        return "spawned_without_refusing"
    return "no_refusal"


def _stale_leg_checks() -> list[tuple[bool, str]]:
    cases = (
        ("current_release_id", "rel-2", REFUSAL_CURRENT_RELEASE),
        ("active_instance_id", "inst-2", REFUSAL_ACTIVE_INSTANCE),
        ("active_start_token", "tok-2", REFUSAL_ACTIVE_START_TOKEN),
        ("manifest_etag", "etag-2", REFUSAL_MANIFEST_ETAG),
        ("source_surface_sha256", _OTHER, REFUSAL_SOURCE_SURFACE),
        ("release_surface_sha256", _OTHER, REFUSAL_RELEASE_SURFACE),
    )
    return [
        (
            _refusal_code(_observation(**{field: drifted})) == expected,
            f"stale {field} refuses as {expected} with no spawn",
        )
        for field, drifted, expected in cases
    ]


def _happy_path_checks() -> list[tuple[bool, str]]:
    executor = _RecordingExecutor()
    controller = ReconciliationCutoverController(
        observe=_observation,
        execute_swap=executor,
    )
    outcome = controller.cutover(_request())
    call = executor.calls[0]
    provenance = call["provenance"]
    assert isinstance(provenance, CutoverProvenance)
    return [
        (len(executor.calls) == 1, "matched CAS invokes the shared executor exactly once"),
        (call["reason"] == CUTOVER_REASON, "reason reaches the executor verbatim"),
        (call["expected_etag"] == "etag-1", "manifest etag CAS is handed to the swap layer"),
        (call["dry_run"] is False, "default run is not a dry run"),
        (
            provenance.reconciliation_id == "rec_smoke1"
            and provenance.source_surface_sha256 == _SOURCE
            and provenance.release_surface_sha256 == _RELEASE,
            "provenance carries reconciliation id + both observed surface digests",
        ),
        (outcome.prior_release_id == "rel-1", "prior release captured pre-swap"),
        (outcome.prior_instance_id == "inst-1", "prior instance id captured pre-swap (D-1.6)"),
        (outcome.prior_pid == 701, "prior PID is captured from the pre-swap attestation"),
        (
            outcome.prior_start_token == "tok-1",
            "prior start token is captured from the pre-swap attestation",
        ),
        (outcome.prior_color == "blue", "prior color captured pre-swap"),
        (outcome.status == "queued", "swap verdict is returned, not reinterpreted"),
        (
            outcome.to_dict()["provenance"] == provenance.to_dict(),
            "outcome serializes the same provenance the swap received",
        ),
    ]


def _dry_run_checks() -> list[tuple[bool, str]]:
    executor = _RecordingExecutor()
    controller = ReconciliationCutoverController(
        observe=_observation,
        execute_swap=executor,
    )
    outcome = controller.cutover(_request(), dry_run=True)
    stale = _observation(current_release_id="rel-9")
    dry_refusal = ReconciliationCutoverController(
        observe=lambda: stale,
        execute_swap=_ExplodingExecutor(),
    )
    try:
        dry_refusal.cutover(_request(), dry_run=True)
        refused = False
    except CutoverRefusalError:
        refused = True
    return [
        (executor.calls[0]["dry_run"] is True, "dry_run reaches the executor"),
        (outcome.dry_run is True, "dry_run is echoed on the outcome"),
        (refused, "a dry run runs the same CAS legs as a real run"),
    ]


class _MemoryJournal:
    """In-memory M1 boundary that exposes every replay/recovery result."""

    def __init__(
        self,
        *,
        terminal: CutoverOutcome | None = None,
        active: bool = False,
        resumable: CutoverOutcome | None = None,
    ) -> None:
        self._terminal = terminal
        self._active = active
        self._resumable = resumable
        self.intent_ids: list[str] = []
        self.terminal_ids: list[str] = []

    def terminal_outcome(self, _: str) -> CutoverOutcome | None:
        return self._terminal

    def active_attempt(self, _: str) -> bool:
        return self._active

    def record_intent(self, reconciliation_id: str) -> None:
        self.intent_ids.append(reconciliation_id)
        self._active = True

    def resume_active(self, _: str) -> CutoverOutcome | None:
        return self._resumable

    def record_terminal(self, reconciliation_id: str, outcome: CutoverOutcome) -> None:
        self.terminal_ids.append(reconciliation_id)
        self._terminal = outcome
        self._active = False


def _replay_refusal_code(
    controller: ReconciliationCutoverController,
    *,
    recover: bool,
) -> str:
    """Return a replay refusal code without turning an expected refusal into a crash."""
    try:
        controller.cutover(_request(), recover=recover)
    except CutoverRefusalError as refusal:
        return refusal.code
    return "fresh_candidate_created"


def _journal_replay_checks() -> list[tuple[bool, str]]:
    """Item 4: no repeated apply/recover can create a second candidate.

    Killing mutations: remove durable replay, the in-flight check, or the
    recover-only branch in ``_replay_or_resume``.  Each makes one of these
    checks red while the fresh-cutover checks remain green.
    """
    executor = _RecordingExecutor()
    journal = _MemoryJournal()
    controller = ReconciliationCutoverController(
        observe=_observation,
        execute_swap=executor,
        journal=journal,
    )
    initial = controller.cutover(_request())
    repeated_apply = controller.cutover(_request())
    repeated_recover = controller.cutover(_request(), recover=True)

    active_executor = _RecordingExecutor()
    active_journal = _MemoryJournal(active=True, resumable=initial)
    active_controller = ReconciliationCutoverController(
        observe=_observation,
        execute_swap=active_executor,
        journal=active_journal,
    )
    active_apply_code = _replay_refusal_code(active_controller, recover=False)
    resumed_active = active_controller.cutover(_request(), recover=True)

    absent_controller = ReconciliationCutoverController(
        observe=_observation,
        execute_swap=_ExplodingExecutor(),
        journal=_MemoryJournal(),
    )
    absent_recover_code = _replay_refusal_code(absent_controller, recover=True)

    unresumable_controller = ReconciliationCutoverController(
        observe=_observation,
        execute_swap=_ExplodingExecutor(),
        journal=_MemoryJournal(active=True),
    )
    unresumable_recover_code = _replay_refusal_code(unresumable_controller, recover=True)

    return [
        (
            len(executor.calls) == 1
            and journal.intent_ids == ["rec_smoke1"]
            and journal.terminal_ids == ["rec_smoke1"],
            "fresh cutover records durable intent before one shared executor call",
        ),
        (
            repeated_apply.resumed and len(executor.calls) == 1,
            "repeated apply replays the durable terminal result without a candidate",
        ),
        (
            repeated_recover.resumed and len(executor.calls) == 1,
            "recover after a terminal result replays without a candidate",
        ),
        (
            active_apply_code == REFUSAL_ATTEMPT_IN_FLIGHT and not active_executor.calls,
            "apply during a durable active attempt refuses before a second candidate",
        ),
        (
            resumed_active.resumed
            and not active_executor.calls
            and active_journal.terminal_ids == ["rec_smoke1"],
            "recover resumes the durable active attempt without a second candidate",
        ),
        (
            absent_recover_code == REFUSAL_NOTHING_TO_RECOVER,
            "recover with no durable attempt refuses instead of starting a candidate",
        ),
        (
            unresumable_recover_code == REFUSAL_NOTHING_TO_RECOVER,
            "unresumable active recovery refuses instead of starting a candidate",
        ),
    ]


def _validation_checks() -> list[tuple[bool, str]]:
    bad = (
        ({"reconciliation_id": "smoke1"}, "unprefixed reconciliation_id"),
        ({"expected_source_surface_sha256": "deadbeef"}, "bare-hex source digest"),
        ({"expected_release_surface_sha256": "sha256:zz"}, "malformed release digest"),
        ({"expected_manifest_etag": ""}, "empty manifest etag"),
        ({"expected_current_release_id": ""}, "empty current release id"),
        ({"expected_active_instance_id": ""}, "empty active instance id"),
        ({"expected_active_start_token": ""}, "empty active start token"),
        ({"reason": "routine_deploy"}, "reason that disguises a reconciliation"),
    )
    checks: list[tuple[bool, str]] = []
    for overrides, label in bad:
        fields: dict[str, str] = {
            "reconciliation_id": "rec_smoke1",
            "expected_source_surface_sha256": _SOURCE,
            "expected_release_surface_sha256": _RELEASE,
            "expected_manifest_etag": "etag-1",
            "expected_current_release_id": "rel-1",
            "expected_active_instance_id": "inst-1",
            "expected_active_start_token": "tok-1",
        }
        fields.update(overrides)
        try:
            ReconciliationCutoverRequest(**fields)  # type: ignore[arg-type]
            checks.append((False, f"request refuses {label}"))
        except ValueError:
            checks.append((True, f"request refuses {label}"))
    return checks


def _attestation_derivation_checks() -> list[tuple[bool, str]]:
    """The observation must come from T1's payload, not a second identity read."""
    payload: dict[str, object] = {
        "current_release_id": "rel-1",
        "router_active_instance_id": "inst-1",
        "self_pid": 701,
        "router_active_color": "blue",
        "self_start_token": "tok-1",
        "manifest_etag": "etag-1",
        "source_surface_sha256": _SOURCE,
        "release_surface_sha256": _RELEASE,
    }
    derived = observation_from_attestation(payload)
    checks = [(derived == _observation(), "attestation payload maps onto the CAS observation")]
    for key in sorted(payload):
        missing = {k: v for k, v in payload.items() if k != key}
        try:
            observation_from_attestation(missing)
            checks.append((False, f"missing {key} is refused, not defaulted"))
        except (KeyError, ValueError):
            checks.append((True, f"missing {key} is refused, not defaulted"))
        blanked = dict(payload)
        blanked[key] = ""
        try:
            observation_from_attestation(blanked)
            checks.append((False, f"empty {key} is refused, not compared"))
        except ValueError:
            checks.append((True, f"empty {key} is refused, not compared"))
    return checks


def _capability_gate_check() -> tuple[bool, str]:
    """The production gate remains fail-closed until its D-track attests it."""
    from macos_self_deployment_plugin.cutover_capability import (  # noqa: PLC0415
        CutoverCapabilityError,
    )

    controller = ReconciliationCutoverController(
        observe=_observation,
        execute_swap=_ExplodingExecutor(),
    )
    try:
        controller.cutover(_request())
    except CutoverCapabilityError as exc:
        return exc.reason_code == "cutover_capability_absent", "absent capability refuses before CAS or swap"
    return False, "absent capability refuses before CAS or swap"


def _install_test_capability() -> tuple[object | None, object | None]:
    """Enable controller-only checks without claiming the production gate landed."""
    import macos_self_deployment_plugin as package  # noqa: PLC0415
    from macos_self_deployment_plugin.cutover_capability import (  # noqa: PLC0415
        CUTOVER_PROTOCOL_VERSION,
        REQUIRED_PROPERTIES,
    )

    name = "macos_self_deployment_plugin.cutover_capability_attestation"
    prior_module = sys.modules.get(name)
    prior_attribute = getattr(package, "cutover_capability_attestation", None)
    attestation = types.ModuleType(name)
    attestation.PROTOCOL_VERSION = CUTOVER_PROTOCOL_VERSION
    attestation.ATTESTED_PROPERTIES = REQUIRED_PROPERTIES
    sys.modules[name] = attestation
    setattr(package, "cutover_capability_attestation", attestation)  # noqa: B010
    return prior_module, prior_attribute


def _remove_test_capability(prior_module: object | None, prior_attribute: object | None) -> None:
    import macos_self_deployment_plugin as package  # noqa: PLC0415

    name = "macos_self_deployment_plugin.cutover_capability_attestation"
    if prior_module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = prior_module  # type: ignore[assignment]
    if prior_attribute is None:
        delattr(package, "cutover_capability_attestation")
    else:
        setattr(package, "cutover_capability_attestation", prior_attribute)  # noqa: B010


def _target_local_factory_checks() -> list[tuple[bool, str]]:
    """The refreshed adapter factory uses the shared executor with T1 identity.

    The fake sits below the construction seam: it records the orchestrator
    invocation while the real factory still creates its typed unavailable
    queue collaborators, observation mapping, controller, and target-local
    poller gate.  Removing either prior-PID forwarding or the target-local
    no-op gate turns these checks red.
    """
    import logging  # noqa: PLC0415

    from ananta.interfaces.lifecycle_result_types import (  # noqa: PLC0415
        RestartResult,
        RestartStatus,
    )
    from macos_self_deployment_plugin import target_local_cutover as factory  # noqa: PLC0415
    from macos_self_deployment_plugin.swap_executor import (  # noqa: PLC0415
        DurableQueueUnavailableAtTargetLocalEntryError,
        TargetLocalUnavailableActionFactory,
    )

    class FakeJournal:
        def __init__(self, *_: object, **__: object) -> None:
            self.intent_ids: list[str] = []
            self.outcomes: list[object] = []

        def terminal_outcome(self, _: str) -> None:
            return None

        def active_attempt(self, _: str) -> bool:
            return False

        def record_intent(self, reconciliation_id: str) -> None:
            self.intent_ids.append(reconciliation_id)

        def resume_active(self, _: str) -> None:
            return None

        def record_terminal(self, _: str, outcome: object) -> None:
            self.outcomes.append(outcome)

    class FakeOrchestrator:
        created: list[dict[str, object]] = []
        restart_calls: list[dict[str, object]] = []

        def __init__(self, **kwargs: object) -> None:
            self.created.append(kwargs)

        def restart(self, **kwargs: object) -> RestartResult:
            self.restart_calls.append(kwargs)
            return RestartResult(
                status=RestartStatus.QUEUED,
                restart_action_id="",
                message="shared executor completed target-local handoff",
                reason=str(kwargs["reason"]),
                expected_etag=str(kwargs["expected_etag"]),
                dry_run=bool(kwargs["dry_run"]),
                reason_code="",
                probe={
                    "cutover_evidence": {
                        "candidate_release_id": "rel-2",
                        "candidate_instance_id": "inst-2",
                        "candidate_color": "green",
                        "router_transitions": ["registered:green:inst-2"],
                        "finisher": "backstop",
                        "poller_gate": "unreachable_from_target_local",
                    },
                },
            )

    attestation: dict[str, object] = {
        "current_release_id": "rel-1",
        "router_active_instance_id": "inst-1",
        "router_active_color": "blue",
        "self_pid": 701,
        "self_start_token": "tok-1",
        "manifest_etag": "etag-1",
        "source_surface_sha256": _SOURCE,
        "release_surface_sha256": _RELEASE,
    }
    dependencies = factory._TargetLocalDependencies(
        target=Path("/tmp/target-local-cutover-smoke"),
        solet_name="smoke",
        app_home=Path("/tmp/target-local-cutover-smoke/profile"),
        runtime_dir=Path("/tmp/target-local-cutover-smoke/runtime"),
        router_client=object(),  # type: ignore[arg-type]
        release_manager=object(),  # type: ignore[arg-type]
        attest=lambda: dict(attestation),
        logger=logging.getLogger("target-local-cutover-smoke"),
    )
    prior_orchestrator = factory.SwapOrchestrator
    prior_journal = factory.ReceiptJournalStore
    factory.SwapOrchestrator = FakeOrchestrator  # type: ignore[assignment]
    factory.ReceiptJournalStore = FakeJournal  # type: ignore[assignment]
    try:
        outcome = factory._controller_from_dependencies(dependencies).cutover(_request())
    finally:
        factory.SwapOrchestrator = prior_orchestrator
        factory.ReceiptJournalStore = prior_journal
    construction = FakeOrchestrator.created[0]
    call = FakeOrchestrator.restart_calls[0]
    unavailable_session = construction["session_factory"]
    try:
        unavailable_session()  # type: ignore[operator]
        session_refused = False
    except DurableQueueUnavailableAtTargetLocalEntryError:
        session_refused = True
    return [
        (
            isinstance(construction["action_factory"], TargetLocalUnavailableActionFactory),
            "target-local factory injects an unavailable queue collaborator, not platform state",
        ),
        (session_refused, "target-local factory refuses synthetic session creation"),
        (call["prior_pid"] == 701, "target-local factory forwards T1 self_pid to the shared executor"),
        (
            call["prior_start_token"] == "tok-1",
            "target-local factory forwards T1 start token to the shared executor",
        ),
        (
            call["poller_gate"] == "unreachable_from_target_local"
            and call["set_active_targets"] == (),
            "target-local entry cannot quiesce the old process poller",
        ),
        (
            outcome.evidence is not None and outcome.evidence.finisher == "backstop",
            "shared executor evidence preserves the target-local backstop handoff",
        ),
    ]


def _process_surface_checks() -> list[tuple[bool, str]]:
    """The verb is only real if the platform can actually dispatch it.

    Killing mutation: drop the edge-process definition, or the
    @platform_process wrapper, and these go red while every controller check
    above stays green — which is exactly the gap a controller-only smoke leaves.
    """
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "ananta" / "src"))
    from ananta.interfaces.local_self_deployment_service_interface import (  # noqa: PLC0415
        LocalSelfDeploymentServiceInterface,
    )
    from macos_self_deployment_plugin.plugin import (  # noqa: PLC0415
        MacosSelfDeploymentPlugin,
    )

    plugin = MacosSelfDeploymentPlugin()
    definitions = plugin.get_edge_process_definitions()
    return [
        ("cutover_release" in definitions, "cutover_release has an edge process definition"),
        (
            definitions["cutover_release"].name == "cutover_release",
            "the edge definition names the verb it dispatches",
        ),
        (
            hasattr(MacosSelfDeploymentPlugin, "cutover_release_action"),
            "cutover_release exposes a platform action wrapper",
        ),
        (
            "cutover_release" in LocalSelfDeploymentServiceInterface.__abstractmethods__,
            "cutover_release is an abstractmethod on the local interface",
        ),
        (
            LocalSelfDeploymentServiceInterface.LOCAL_INTERFACE_VERSION == "1.2.0",
            "LOCAL_INTERFACE_VERSION was bumped for the added verb",
        ),
    ]


def _action_refusal_checks() -> list[tuple[bool, str]]:
    """A malformed call is a caller error, never a crashed cutover."""
    from macos_self_deployment_plugin.plugin import (  # noqa: PLC0415
        MacosSelfDeploymentPlugin,
    )

    plugin = MacosSelfDeploymentPlugin()
    empty = plugin.cutover_release_action({}, {})
    partial = plugin.cutover_release_action(
        {"reconciliation_id": "rec_x", "expected_manifest_etag": "e"}, {},
    )
    return [
        (empty.get("error_code") == "missing_args" or "missing_args" in str(empty),
         "an empty call is refused as missing_args, not attempted"),
        (partial.get("error_code") == "missing_args" or "missing_args" in str(partial),
         "a partially-specified call is refused as missing_args"),
    ]


def main() -> int:
    checks = [_capability_gate_check()]
    prior_module, prior_attribute = _install_test_capability()
    try:
        checks.extend([
            *_stale_leg_checks(),
            *_happy_path_checks(),
            *_dry_run_checks(),
            *_journal_replay_checks(),
            *_validation_checks(),
            *_attestation_derivation_checks(),
            *_target_local_factory_checks(),
            *_process_surface_checks(),
            *_action_refusal_checks(),
        ])
    finally:
        _remove_test_capability(prior_module, prior_attribute)
    for passed, label in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(passed for passed, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
