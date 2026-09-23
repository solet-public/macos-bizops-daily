"""Swap orchestration — the spawn-green + wait-ready + activate sequence.

The orchestrator is the only piece of the plugin that knows the swap
choreography. ``MacosSelfDeploymentPlugin`` keeps its surface thin
by delegating ``restart_with_manifest`` here.

Responsibility split:

- ``MacosSelfDeploymentPlugin`` owns the platform-facing
  ``@platform_process`` action surface, the service-interface methods,
  and the ``RouterClient`` lifecycle (one client per plugin instance).
- ``SwapOrchestrator`` owns the actual swap sequence: probe the live
  router, decide the next color, mint the next instance id, spawn the
  child, poll for ready, activate, quiesce the local plugins, enqueue
  the durable ``complete_swap`` finisher.

The orchestrator NEVER touches Postgres directly — it submits the
durable handoff action through an injected ``ActionFactoryProtocol``,
mirroring the cloud sibling's pattern in
``plugins/aws_self_deployment_plugin/.../deployer.py``.

The spawn helper is also pulled out so smokes can override it with a
fake that records the call but doesn't actually launch another the solet.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
from ananta.core.runtime import get_runtime_dir
from ananta.interfaces.lifecycle_result_types import RestartResult, RestartStatus

from macos_self_deployment_plugin import process_identity
from macos_self_deployment_plugin.child_spawn import spawn_solet_child
from macos_self_deployment_plugin.constants import (
    AUDIT_TOKEN_PREFIX,
    COLOR_BLUE,
    COLOR_GREEN,
    DEFAULT_GREEN_READY_POLL_INTERVAL_SECONDS,
    DEFAULT_GREEN_READY_TIMEOUT_SECONDS,
    ENV_SOLET_COLOR,
    ENV_SOLET_INSTANCE_ID,
    ENV_SOLET_RELEASE_ID,
    FINISHER_ESCALATION_NEEDS_INTERVENTION,
    FINISHER_ESCALATION_SIGKILL_AFTER_GRACE,
    PLUGIN_NAME,
    STATUS_FAILED,
    STATUS_QUEUED,
    RestartReasonCode,
    is_valid_color,
    opposite_color,
    resolve_project_root,
)
from macos_self_deployment_plugin.preflight_probe_runner import (
    CHECK_ROOT_MANIFEST,
    PROBE_ERROR_HARNESS,
    ProbeOutcome,
)
from macos_self_deployment_plugin.release_manager import (
    CandidatePaths,
    GcResult,
    ReleaseManagerError,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import (
    RouterClient,
    RouterClientError,
)
from macos_self_deployment_plugin.schema_preflight import PreflightVerdict
from macos_self_deployment_plugin.schema_snapshot_producer import (
    SchemaSnapshotFn,
    build_schema_snapshot_fn,
)
from macos_self_deployment_plugin.swap_executor import (
    ActionFactoryProtocol,
    SetActiveTarget,
    SetColorActiveFn,
    SpawnFn,
    SwapExecutor,
    build_failed_result,
)


# A schema-preflight check (design §3): given the freshly-built candidate,
# return the additive/non-additive verdict. The orchestrator aborts the
# swap before spawning when the verdict is non-additive (durable code
# rollback only holds over an unchanged/additive schema). Injected so the
# plugin owns the extraction (current-release VERSION vs candidate VERSION
# snapshot) and the orchestrator stays free of the platform schema deps.
#
# The spawn/quiesce spine types — ``SpawnFn``, ``SetActiveTarget``,
# ``ActionFactoryProtocol``, ``SetColorActiveFn`` — live in
# ``swap_executor`` (the executor owns the spine) and are imported above +
# re-exported in ``__all__`` so callers that import them from this module
# (plugin.py, smokes) keep working.
class SchemaPreflightFn(Protocol):
    """The §3 gate: classify the candidate against the resolved current snapshot.

    ``current_snapshot`` is the orchestrator-resolved OLD side (read from the
    current ``VERSION`` or DERIVED from ``current/code``); ``current_release_exists``
    distinguishes a genuine bootstrap from a derive that failed to a None.
    """

    def __call__(
        self,
        candidate: CandidatePaths,
        *,
        current_snapshot: dict[str, object] | None,
        current_release_exists: bool,
    ) -> PreflightVerdict: ...


class PreflightProbeFn(Protocol):
    """GTE-06 L2 gate: fresh-source-probe the candidate with its OWN interpreter.

    Returns a classified :class:`ProbeOutcome`; the production seam
    (:func:`preflight_probe_runner.run_preflight_probe`) NEVER raises
    (A5a). The orchestrator still wraps the call defensively so an
    injected seam that violates the contract is contained to a
    ``PROBE_FAILED`` result rather than escaping as ``plugin_raised``
    core-side (which would leave the committed manifest bytes with no
    rollback).
    """

    def __call__(
        self, *, candidate: CandidatePaths, app_home: Path
    ) -> ProbeOutcome: ...


class ReleaseManagerProtocol(Protocol):
    """The subset of :class:`~.release_manager.ReleaseManager` the swap uses.

    Declared structurally so smokes can inject a fake that returns a
    synthetic :class:`CandidatePaths` and records the ``cutover`` /
    ``rollback`` calls without CoW-cloning the real 1.8 GB tree — mirroring
    the ``spawn_fn`` injection seam. Carries both the forward-cutover surface
    (``build_candidate`` / ``cutover``) and the durable-rollback surface
    (``previous_release`` / ``candidate_for`` / ``rollback``).
    """

    @property
    def current_release(self) -> str | None: ...

    @property
    def previous_release(self) -> str | None: ...

    def build_candidate(
        self,
        *,
        manifest_etag: str = ...,
        manifest_plugins: tuple[str, ...] | None = ...,
        schema_snapshot_fn: Callable[[Path], dict[str, object]] | None = ...,
        reconciliation_provenance: dict[str, str] | None = ...,
    ) -> CandidatePaths: ...

    def candidate_for(self, release_id: str) -> CandidatePaths: ...

    def current_schema_snapshot(self) -> dict[str, object] | None: ...

    def cutover(self, candidate: CandidatePaths) -> SwapResult: ...

    def rollback(self) -> SwapResult: ...

    def gc(self, *, keep: int | None = ...) -> GcResult: ...


@dataclass(frozen=True, slots=True)
class _SwapPlan:
    """The validated pre-spawn plan: which color to bring up, on which release.

    ``probe_evidence`` is the GREEN L2 probe's success payload (Q5); the
    forward cutover attaches it to the QUEUED result so the applied
    envelope carries positive proof the probe executed.
    """

    next_color: str
    candidate: CandidatePaths
    probe_evidence: dict[str, Any] | None = None


def _now_audit_token() -> str:
    return f"{AUDIT_TOKEN_PREFIX}{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _contained_probe_outcome(
    probe: PreflightProbeFn, *, candidate: CandidatePaths, app_home: Path,
    logger: logging.Logger,
) -> ProbeOutcome:
    """A5a: nothing escapes — a raising probe seam is a RED probe.

    An exception escaping ``restart_with_manifest`` is classified
    core-side as ``plugin_raised``, which routes to the
    leave-the-committed-bytes envelope — bypassing the manifest rollback
    exactly when the probe is least trustworthy. Containing the seam
    here guarantees every probe-path failure lands on the
    ``PROBE_FAILED`` → rollback path.
    """
    try:
        return probe(candidate=candidate, app_home=app_home)
    except Exception as exc:  # noqa: BLE001 — A5a containment is the contract
        logger.exception("preflight probe seam raised — contained to PROBE_FAILED")
        return ProbeOutcome(ok=False, payload={
            "failing_step": "harness",
            "error_class": PROBE_ERROR_HARNESS,
            "detail": f"probe seam raised {type(exc).__name__}: {exc}",
            "failures": [],
            "release_id": candidate.release_id,
        })


def _root_manifest_restart_result(
    outcome: ProbeOutcome, *, reason: str, expected_etag: str,
    logger: logging.Logger,
) -> RestartResult | None:
    """F1 drift found by the probe → the F1 classification, not the probe's.

    ``None`` when this rejection is not root-manifest drift.

    §46.1 moved WHERE this gate executes (the outgoing process's stale
    imports → the candidate's own interpreter) and deliberately changed
    NOTHING about what a refusal means. That distinction is load-bearing
    and is why this function exists:

    * ``status`` is what core actually routes on —
      ``PROBE_FAILED`` triggers the manifest-bytes rollback, ``FAILED``
      does not. Root-manifest drift is a property of the DEPLOYMENT ROOT,
      not of the manifest bytes being applied; rolling those bytes back
      because the repo root carries a stray entry would punish the wrong
      artifact. It stays ``FAILED`` — system unchanged, coherent,
      retryable — exactly as it was before the move.
    * ``reason_code`` is reported rather than routed on, and is preserved
      for observability: an operator keeps seeing ``root_manifest_drift``
      instead of a generic ``probe_rejected``.

    Letting either flatten into the probe's own classification would have
    changed live behaviour as a side effect of a bug fix.
    """
    failures = outcome.payload.get("failures")
    if not isinstance(failures, list):
        return None
    drift = next(
        (
            failure for failure in failures
            if isinstance(failure, dict)
            and failure.get("check") == CHECK_ROOT_MANIFEST
        ),
        None,
    )
    if drift is None:
        return None
    envelope = str(drift.get("message", ""))
    logger.warning("cutover preflight refused green spawn:\n%s", envelope)
    return build_failed_result(
        status=RestartStatus.FAILED,
        reason_code=RestartReasonCode.ROOT_MANIFEST_DRIFT,
        message="cutover preflight blocked on root_manifest drift:\n" + envelope,
        reason=reason,
        expected_etag=expected_etag,
        logger=logger,
    )


def _probe_failed_restart_result(
    outcome: ProbeOutcome, *, reason: str, expected_etag: str,
    logger: logging.Logger,
) -> RestartResult:
    """Typed PROBE_FAILED result carrying the rejection payload.

    EXCEPT for root-manifest drift, which keeps its own classification —
    see :func:`_root_manifest_restart_result`.
    """
    root_manifest_failure = _root_manifest_restart_result(
        outcome, reason=reason, expected_etag=expected_etag, logger=logger,
    )
    if root_manifest_failure is not None:
        return root_manifest_failure
    error_class = outcome.payload.get("error_class")
    detail = outcome.payload.get("detail")
    logger.error(
        "L2 preflight probe REJECTED the deploy [%s]: %s", error_class, detail,
    )
    return RestartResult(
        status=RestartStatus.PROBE_FAILED,
        restart_action_id="",
        message=(
            "L2 fresh-source preflight probe rejected the deploy: "
            f"{error_class}: {detail}"
        ),
        reason=reason,
        expected_etag=expected_etag,
        dry_run=False,
        reason_code=RestartReasonCode.PROBE_REJECTED,
        probe=outcome.payload,
    )


def _rollback_cas_check(
    *,
    release_manager: ReleaseManagerProtocol,
    expected_current_release: str,
    expected_etag: str,
    reason: str,
    logger: logging.Logger,
) -> RestartResult | None:
    """Concurrency CAS for ``rollback_release`` (Architect ruling (c)).

    ``rollback_release`` is an operational verb invoked DIRECTLY (no
    ``apply_manifest`` wrapper performs the CAS upstream as it does for
    ``restart_with_manifest``), so the verb self-CASes against the ALREADY-
    injected release state: the caller passes the ``rel-<id>`` it observed as
    ``current``, and this confirms it still matches
    ``ReleaseManager.current_release`` before any spawn. ``current`` and
    ``previous`` move atomically, so CASing ``current`` ALONE is sufficient
    (never CAS ``previous``). A mismatch — a concurrent deploy/rollback moved
    ``current`` since the caller observed it — returns
    ``FAILED(stale_current_release)`` with the ACTUAL ``current`` echoed for
    re-read/retry; the system is left unchanged. Stateless (release state +
    logger only), so it lives at module level alongside ``default_spawn``.
    """
    actual_current = release_manager.current_release
    if expected_current_release == actual_current:
        return None
    return build_failed_result(
        status=RestartStatus.FAILED,
        reason_code=RestartReasonCode.STALE_CURRENT_RELEASE,
        message=(
            f"stale current release: caller asserted current="
            f"{expected_current_release!r} but the live current is "
            f"{actual_current!r}; re-read current_release and retry."
        ),
        reason=reason, expected_etag=expected_etag, logger=logger,
    )


def _capture_manifest_plugins(app_home: Path) -> tuple[str, ...] | None:
    """Snapshot the LIVE profile plugin set for the release being built (BLG-02).

    Called at build time because that is the one moment the live manifest still
    describes the tree being built: ``apply_manifest`` commits the new manifest
    before delegating a restart, and the NEXT deploy overwrites it again. The §3
    derive path therefore cannot read it back later and must consult the
    release's own persisted copy instead.

    ``None`` is preserved rather than flattened to an empty tuple: ``None``
    means "ungated — load everything installed", while ``()`` would claim the
    release had NO plugins and gate a later derive to an empty discovery, which
    would silently produce an empty snapshot. Module-level alongside
    ``_resolve_current_snapshot`` so ``SwapOrchestrator`` stays under the
    god-class LOC bound.
    """
    live = load_manifest_plugin_set(app_home)
    return None if live is None else tuple(sorted(live))


def _resolve_current_snapshot(
    release_manager: ReleaseManagerProtocol,
    snapshot_fn: SchemaSnapshotFn,
) -> dict[str, object] | None:
    """Resolve the OLD side of the §3 diff — read it, or DERIVE it (B1·1).

    The current release's persisted snapshot is the OLD side. When the current
    release predates the producer (its ``VERSION`` has none) but a current
    release EXISTS, DERIVE the snapshot by running the SAME collector
    (``snapshot_fn``) against ``current/code`` — so a pre-producer current (e.g.
    the live ``af157a1fe`` transition) is still gated.

    The derive passes the OLD release's OWN captured plugin set as the
    collector's manifest (BLG-02). By the time this runs, ``apply_manifest``
    has already written the INCOMING deploy's manifest over the live one, so
    letting the collector read ``APP_HOME`` would gate the old tree by the new
    profile — and a deploy that GROWS the manifest would then false-REFUSE on a
    plugin the old tree never had. ``None`` (a release built before capture
    landed) preserves today's live-manifest behaviour rather than inventing a
    more permissive one; it is a compat rung that stops firing once every
    release in the chain postdates this.

    FAIL-CLOSED on both legs: ``current_schema_snapshot`` raises on a torn
    ``VERSION`` and the derive raises on any collect failure, so the only
    outcomes are a snapshot or a ``ReleaseManagerError`` — NEVER a silent None
    that would let the gate read a non-additive change as a bootstrap. A ``None``
    return therefore means, unambiguously, "no current release". Module-level
    (stateless beyond release state + the fn) alongside ``_rollback_cas_check``,
    so the orchestrator class stays under the god-class LOC bound.
    """
    current_release = release_manager.current_release
    current_snapshot = release_manager.current_schema_snapshot()
    if current_snapshot is None and current_release is not None:
        old_candidate = release_manager.candidate_for(current_release)
        # THE point of use, and therefore the only place a rejected capture may
        # refuse. Rehydrate deliberately reports rather than raises, so that a
        # garbage schema-gate field cannot make a bootable release
        # un-rollback-able; the cost of that choice is paid here, loudly.
        if old_candidate.manifest_plugins_error is not None:
            msg = (
                f"§3 derive refused: {old_candidate.manifest_plugins_error} — refusing to "
                "snapshot an old tree against an untrustworthy manifest"
            )
            raise ReleaseManagerError(msg)
        current_snapshot = snapshot_fn(
            old_candidate.code_root, old_candidate.manifest_plugins,
        )
    return current_snapshot


def default_spawn(
    app_home: Path,
    next_color: str,
    next_instance_id: str,
    solet_name: str,
    candidate: CandidatePaths,
) -> int:
    """Spawn the next-color solet FROM the materialized candidate release.

    §4.5 spawn invariant (design 2026-06-27): the green child is launched
    from the **candidate ``rel-<id>`` directly** — its own
    ``venv/bin/python3``, whose re-pointed ``.pth`` resolve imports to the
    candidate's ``code/`` — **not** from ``current`` (which still points
    at the live old release at spawn time, and is only flipped to the
    candidate by ``cutover`` after this child passes register + activate).
    So the candidate interpreter replaces the old repo-root
    ``_resolve_interpreter`` resolution.

    The entrypoint stays ``python -m ananta.cli`` (NOT ``launch.py``,
    which would terminate the running blue before green is up). The child
    runs in its own POSIX session (``start_new_session=True``) so a future
    SIGTERM to blue (the green-side ``complete_swap`` finisher) cannot
    cascade to green.

    Args:
        app_home: ``<APP_HOME>`` for the new instance (shared profile;
            ``profile/data/`` is shared across releases per §4.3).
        next_color: ``"blue"`` or ``"green"`` — the router routing/identity
            axis (independent of the release axis).
        next_instance_id: Pre-minted id the new the solet uses when it calls
            ``router.register_color``.
        solet_name: The shared solet name, propagated so the
            child can resolve the runtime dir.
        candidate: The freshly-built release this child runs — its
            ``venv_python`` is the interpreter and ``release_id`` is
            recorded in ``SOLET_RELEASE_ID`` (audit-only, §4.8).

    Returns:
        The OS pid of the spawned child.
    """
    # Capture green's stdout+stderr to a per-spawn log. Previously
    # DEVNULL'd, so a green that failed to register within the ready
    # timeout left no diagnostic — caller couldn't tell whether green
    # crashed at import, hung in kb_lifecycle, blocked on LM Studio,
    # etc. Per-spawn file (vs shared) keeps each boot trace isolated.
    log_path = app_home / "data" / "logs" / f"green_spawn_{next_color}_{next_instance_id}.log"
    # Shared spawn mechanics (cmd shape, out-of-tree §5 CWD,
    # start_new_session, stdio) live in ``child_spawn`` so this swap path
    # and the supervisor's cold-start/crash path cannot drift. The candidate
    # interpreter + explicit colour/instance/release env is the swap-specific
    # part: register + activate target this exact child.
    proc = spawn_solet_child(
        interpreter=str(candidate.venv_python),
        app_home=app_home,
        solet_name=solet_name,
        log_path=log_path,
        extra_env={
            ENV_SOLET_COLOR: next_color,
            ENV_SOLET_INSTANCE_ID: next_instance_id,
            # Audit-only (§4.8): which immutable release this child runs, so
            # the colour axis and the release axis stay separate, auditable
            # fields.
            ENV_SOLET_RELEASE_ID: candidate.release_id,
        },
    )
    return proc.pid


def _derive_next_color(
    status_snap: dict[str, Any],
    self_color: str,
    self_instance_id: str,
    logger: logging.Logger,
) -> str | None:
    """Confirm ``self`` is THE router-active instance; return opposite color or None.

    C3: require both ``active_color == self_color`` AND ``active_instance_id ==
    self_instance_id``. Color alone is not identity — a stale same-color process
    (not the router-active instance) must not drive a swap and enqueue a
    ``complete_swap`` for its own stale id. Module-level (pure but for the
    injected logger) so :class:`SwapOrchestrator` stays under the god-class bound.
    """
    active_color = status_snap.get("active_color")
    active_instance_id = status_snap.get("active_instance_id")
    if not isinstance(active_color, str) or not isinstance(active_instance_id, str):
        return None
    if active_color != self_color:
        logger.warning(
            "self_color=%s but router.active_color=%s; refusing swap",
            self_color, active_color,
        )
        return None
    if active_instance_id != self_instance_id:
        logger.warning(
            "self_instance_id=%s but router.active_instance_id=%s "
            "(stale non-active same-color process); refusing swap",
            self_instance_id, active_instance_id,
        )
        return None
    if not is_valid_color(self_color):
        return None
    return opposite_color(self_color)


def _provenance_kwargs(
    reconciliation_provenance: dict[str, str] | None,
) -> dict[str, dict[str, str]]:
    """Pass the provenance kwarg ONLY on the reconciliation path.

    An ordinary deploy must call ``build_candidate`` with exactly the argument
    list it always did, so widening that seam cannot silently change deploy
    behaviour — and a release-manager stand-in that does not understand
    provenance then fails loudly the moment a reconciliation actually uses it,
    instead of quietly dropping the one field that records which approved act
    produced these bytes.

    Provenance reaches the build and nothing else: it never influences colour
    selection, preflight, or any refusal, so a reconciliation swap and a deploy
    swap remain the same operation with the same safety properties.
    """
    if reconciliation_provenance is None:
        return {}
    return {"reconciliation_provenance": reconciliation_provenance}


def _finisher_escalation_for(reconciliation_provenance: dict[str, str] | None) -> str:
    """The finisher's SIGTERM-overrun policy for this swap (iss_8d1ec833).

    A reconciliation swap (``cutover_release`` / target-local entry — the
    gated channel) must never reach signal 9, so its durable finisher record
    says ``needs_intervention``; an ordinary deploy keeps today's ladder.
    Provenance is the discriminator the executor already receives, so the
    policy cannot drift from the channel it protects.
    """
    if reconciliation_provenance is None:
        return FINISHER_ESCALATION_SIGKILL_AFTER_GRACE
    return FINISHER_ESCALATION_NEEDS_INTERVENTION


def _prior_identity(
    prior_pid: int | None,
    prior_start_token: str | None,
) -> tuple[int, str | None]:
    """Supply service-local identity only for direct compatibility callers."""
    resolved_pid = os.getpid() if prior_pid is None else prior_pid
    return (
        resolved_pid,
        process_identity.start_token(resolved_pid)
        if prior_start_token is None
        else prior_start_token,
    )


def _gc_releases(manager: ReleaseManagerProtocol, logger: logging.Logger) -> None:
    """Best-effort release cleanup that never aborts a healthy swap."""
    try:
        result = manager.gc()
    except (ReleaseManagerError, OSError) as exc:
        logger.warning("release gc failed (non-fatal): %s", exc)
        return
    if result.deleted:
        logger.info("release gc reaped %d: %s", len(result.deleted), result.deleted)


class SwapOrchestrator:
    """Stateless coordinator for one ``restart_with_manifest`` turn.

    ``stateless`` means: every public call is reentrant. The
    orchestrator caches no in-flight bookkeeping between calls; per-call
    state lives in local variables. The plugin keeps a single
    orchestrator instance and reuses it; concurrent calls would step on
    each other, but the lifecycle contract only fires
    ``restart_with_manifest`` from inside ``apply_manifest`` which
    holds the platform's manifest CAS lock — concurrent firings are
    blocked at the apply-manifest layer.
    """
    def __init__(
        self,
        *,
        router_client: RouterClient,
        action_factory: ActionFactoryProtocol,
        session_factory: Callable[[], str],
        solet_name: str,
        release_manager: ReleaseManagerProtocol,
        schema_preflight: SchemaPreflightFn,
        preflight_probe: PreflightProbeFn,
        set_color_active: SetColorActiveFn,
        spawn_fn: SpawnFn = default_spawn,
        logger: logging.Logger | None = None,
        ready_timeout_seconds: int = DEFAULT_GREEN_READY_TIMEOUT_SECONDS,
        ready_poll_interval_seconds: float = DEFAULT_GREEN_READY_POLL_INTERVAL_SECONDS,
        runtime_dir: Path | None = None,
    ) -> None:
        self._router = router_client
        self._release_manager = release_manager
        self._schema_preflight = schema_preflight
        # GTE-06: REQUIRED, deliberately without a default — a defaulted
        # no-op probe would be a silent fail-open bypass of the L2 gate
        # (operator-confirmed fail-closed posture, design §11 Q2).
        self._preflight_probe = preflight_probe
        self._solet_name = solet_name
        self._logger = logger or logging.getLogger(PLUGIN_NAME)
        # B2: pending-finisher record dir (smokes inject a scratch dir so live
        # state is never touched); defaults to the live runtime dir.
        resolved_runtime_dir = runtime_dir or get_runtime_dir(solet_name)
        # The spawn → wait → activate → symlink-swap → quiesce → enqueue spine
        # (incl. the GreenCandidate router lifecycle) lives in the executor,
        # shared verbatim by the forward cutover and the durable rollback. The
        # orchestrator owns only the pre-spawn phase (status probe, color
        # derivation, build-or-rehydrate, schema preflight) and hands the
        # executor a candidate + the symlink op.
        self._executor = SwapExecutor(
            router_client=router_client,
            action_factory=action_factory,
            session_factory=session_factory,
            solet_name=solet_name,
            runtime_dir=resolved_runtime_dir,
            set_color_active=set_color_active,
            spawn_fn=spawn_fn,
            logger=self._logger,
            ready_timeout_seconds=ready_timeout_seconds,
            ready_poll_interval_seconds=ready_poll_interval_seconds,
        )

    # ------------------------------------------------------------------
    # Entry point: restart_with_manifest
    # ------------------------------------------------------------------

    def restart(
        self,
        *,
        reason: str,
        expected_etag: str,
        dry_run: bool,
        app_home: Path,
        self_instance_id: str,
        self_color: str,
        set_active_targets: Iterable[SetActiveTarget],
        reconciliation_provenance: dict[str, str] | None = None,
        prior_pid: int | None = None,
        prior_start_token: str | None = None,
        poller_gate: str = "local_service_quiesced",
    ) -> RestartResult:
        """Drive one swap end-to-end and return the typed RestartResult.

        Sequence (L3 plan §3.3 + §4.2, extended by the 2026-06-27
        materialized-release design §4.5/§4.7/§3):

        1. Read router.status() to confirm self is active + derive
           next color.
        2. Build the immutable candidate release (CoW clone + .pth
           re-point); §3 schema preflight on the candidate. (Both before
           any spawn — a build failure or non-additive schema diff leaves
           ``current``/``previous`` + the live router untouched.)
        3. Mint next-color instance id.
        4. Spawn next color FROM the candidate (§4.5: its own
           ``venv``/``code``, NOT ``current``) via ``self._spawn_fn``.
        5. Poll router.status() until next color reports registered.
        6. router.activate(next_color, next_instance_id).
        7. ``ReleaseManager.cutover(candidate)`` — flip the durable
           ``previous``/``current`` symlinks (§4.7, immediately after a
           successful activate, before quiesce). On failure: roll the router
           back to the prior (still-draining) color; the candidate is
           SIGTERM'd (bounded, never SIGKILL) + unregistered ONLY if that
           rollback is CONFIRMED — if it does NOT take (RPC error/refusal)
           the router may still route to the candidate, so it is LEFT ALIVE
           with a distinct 'manual intervention required' status (never a
           dead-color route). Either way do NOT enqueue complete_swap.
        8. Iterate set_active_targets → set_active(False) on each.
        9. Enqueue durable complete_swap action for green's poller.
        10. Return RestartResult(status=QUEUED, restart_action_id=<id>).

        Failure of any pre-activate step (1–5) leaves the prior color
        untouched and routing-side unchanged; we SIGTERM (bounded, never
        SIGKILL — iss_8d1ec833) + unregister a started-but-not-registered
        child: ``FAILED``, or ``NEEDS_INTERVENTION`` past the grace window.

        ``dry_run`` short-circuits at step 0 and returns a
        ``status=QUEUED`` envelope with a synthetic restart_action_id
        and an explanatory message — used by smoke tests + operator
        previews.
        """
        if dry_run:
            return self._dry_run_envelope(reason, expected_etag, self_color)

        # F1 cutover preflight — refuses to spawn green on root-manifest drift
        # (design memo §6.1) — no longer runs HERE. §46.1: validating in this
        # process validated against whatever it imported at its own last
        # start, so it refused valid manifests right after an update. It now
        # runs inside the L2 fresh-source probe under the CANDIDATE's
        # interpreter (see _prepare_swap). The candidate does not exist yet at
        # this point in the sequence, which is why the gate had to move rather
        # than be re-pointed. Its refusal classification is unchanged —
        # see _root_manifest_restart_result.

        # Confirm self is the active color and materialize + schema-preflight
        # the candidate — all BEFORE any spawn (§4.5/§4.7/§3). A failure here
        # leaves the live router + ``current``/``previous`` untouched.
        plan = self._prepare_swap(
            reason=reason, expected_etag=expected_etag, app_home=app_home,
            self_color=self_color, self_instance_id=self_instance_id,
            reconciliation_provenance=reconciliation_provenance,
        )
        if isinstance(plan, RestartResult):
            return plan
        next_color = plan.next_color
        candidate = plan.candidate

        # Steps 3–10 — the spawn → wait → activate → durable cutover (§4.7,
        # F1/F2-gated compensation) → grace → quiesce → C2 gate-off → enqueue
        # complete_swap spine — are delegated to the shared executor. The
        # symlink op is ``cutover`` (the durable rollback verb passes
        # ``rollback`` instead); a register-timeout here is a plain FAILED
        # (system unchanged, retryable).
        resolved_prior_pid, resolved_prior_start_token = _prior_identity(
            prior_pid, prior_start_token,
        )
        result = self._executor.execute(
            app_home=app_home, candidate=candidate, next_color=next_color,
            reason=reason, expected_etag=expected_etag,
            self_instance_id=self_instance_id, self_color=self_color,
            prior_pid=resolved_prior_pid,
            prior_start_token=resolved_prior_start_token,
            poller_gate=poller_gate,
            set_active_targets=set_active_targets,
            symlink_swap=self._release_manager.cutover,
            spawn_failure=(RestartStatus.FAILED, RestartReasonCode.SPAWN_FAILED),
            register_failure=(RestartStatus.FAILED, RestartReasonCode.REGISTER_TIMEOUT),
            compensation_codes=(
                RestartReasonCode.CUTOVER_COMPENSATED,
                RestartReasonCode.CUTOVER_ROUTER_ROLLBACK_FAILED,
            ),
            finisher_escalation=_finisher_escalation_for(reconciliation_provenance),
        )
        if result.status is RestartStatus.QUEUED and plan.probe_evidence is not None:
            # Q5: the GREEN probe's evidence rides the QUEUED result so the
            # applied envelope carries positive proof the L2 probe executed.
            merged_probe = dict(plan.probe_evidence)
            if result.probe is not None:
                merged_probe.update(result.probe)
            return replace(result, probe=merged_probe)
        return result

    # ------------------------------------------------------------------
    # Entry point: rollback_release (durable code rollback — the escape hatch)
    # ------------------------------------------------------------------

    def rollback_release(
        self,
        *,
        reason: str,
        expected_etag: str,
        expected_current_release: str,
        app_home: Path,
        self_instance_id: str,
        self_color: str,
        set_active_targets: Iterable[SetActiveTarget],
        prior_pid: int | None = None,
        prior_start_token: str | None = None,
    ) -> RestartResult:
        """Durably roll back to the prior release (design §4.5, PATH A).

        Rollback IS a swap whose target is the existing ``previous`` release,
        so it reuses the shared executor choreography VERBATIM, with two
        deltas only:

        - the candidate is the REHYDRATED ``previous`` release
          (``candidate_for``), not a freshly-built one — no
          ``build_candidate``, no §3 schema preflight (a release that was
          already ``current`` is schema-safe to return to by construction);
        - the symlink op is ``ReleaseManager.rollback`` (swap
          ``current``↔``previous``), not ``cutover``.

        Deliberately does NOT run the root-manifest preflight: rollback is the
        escape hatch FROM a bad current tree, so gating it on current-tree
        drift would defeat its purpose.

        Status partition (Architect §4.5):

        - ``FAILED`` (system unchanged + coherent + retryable): a stale
          ``expected_current_release`` (the concurrency CAS — current moved
          under the caller); no ``previous`` rollback target; self not the
          router-active instance; activate refused (current still
          authoritative); a ``rollback()`` that failed and whose compensation
          cleanly restored the pre-swap pair.
        - ``NEEDS_INTERVENTION`` (automated recovery exhausted, human must
          act): the rollback target cannot be brought up — its release dir is
          missing/corrupt, OR it never registers/health-checks within the
          timeout (the safety net itself is void); OR a compensation that could
          not complete (the durable pair MAY be incoherent).

        C2 is sidestepped: the reactivated release is a FRESH SPAWN (its
        poller is born ``is_active_color=True``), so no cross-process flip is
        needed — the broken current quiesces its OWN poller before
        ``complete_swap`` is enqueued (the landed C2 fix), which is necessary
        and sufficient. A second ``rollback_release`` rolls forward again
        (``rollback`` toggles ``current``↔``previous``) — undo/redo.

        Cold-boot fallback (DOCUMENTED, out of band — NOT this verb): if the solet
        is DEAD and so cannot serve this verb, the operator rolls back
        directly against the on-disk releases — call
        ``ReleaseManager.rollback()`` (flips the durable ``current``/``previous``
        symlinks) and re-launch via ``ananta.cli``, which cold-boots whatever
        ``current`` now points at. This verb is the zero-downtime *live* path;
        the cold-boot path needs no live router and is covered by the
        materialized-release foundation itself.
        """
        # CAS FIRST (Architect ruling (c)): confirm the caller is acting on the
        # ``current`` release they observed before anything else — ``current``
        # and ``previous`` move atomically, so CASing ``current`` alone is
        # sufficient. A stale assertion (a concurrent deploy/rollback moved
        # ``current``) → FAILED, system unchanged, no spawn.
        cas_failure = _rollback_cas_check(
            release_manager=self._release_manager,
            expected_current_release=expected_current_release,
            expected_etag=expected_etag, reason=reason, logger=self._logger,
        )
        if cas_failure is not None:
            return cas_failure
        previous = self._release_manager.previous_release
        if previous is None:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message="no previous release to roll back to",
                reason_code=RestartReasonCode.NO_ROLLBACK_TARGET,
            )
        # Same identity gate as a forward swap (only the live router-active
        # instance may initiate) — and it yields the OPPOSITE color to bring
        # the previous release up on.
        try:
            status_snap = self._router.status()
        except RouterClientError as exc:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message=f"router status() failed: {exc}",
                reason_code=RestartReasonCode.ROUTER_UNREACHABLE,
            )
        next_color = _derive_next_color(
            status_snap, self_color, self_instance_id, self._logger,
        )
        if next_color is None:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message=(
                    "router status() did not show this instance as the active "
                    "color; refusing to roll back from a non-active instance."
                ),
                reason_code=RestartReasonCode.NOT_ACTIVE_INSTANCE,
            )
        # Rehydrate the previous release's spawn paths. A raise means the target
        # release dir is missing/corrupt — the safety net is VOID, and retrying
        # won't restore a gone artifact → NEEDS_INTERVENTION (not retryable
        # FAILED). Mirrors the register-timeout target-unbootable case below.
        try:
            candidate = self._release_manager.candidate_for(previous)
        except ReleaseManagerError as exc:
            return build_failed_result(
                status=RestartStatus.NEEDS_INTERVENTION,
                reason_code=RestartReasonCode.ROLLBACK_TARGET_UNBOOTABLE,
                message=f"rollback target {previous} is not materializable: {exc}",
                reason=reason, expected_etag=expected_etag, logger=self._logger,
            )
        self._logger.info(
            "rolling back: bringing up previous release=%s as color=%s",
            previous, next_color,
        )
        resolved_prior_pid, resolved_prior_start_token = _prior_identity(
            prior_pid, prior_start_token,
        )
        return self._executor.execute(
            app_home=app_home,
            candidate=candidate,
            next_color=next_color,
            reason=reason,
            expected_etag=expected_etag,
            self_instance_id=self_instance_id,
            self_color=self_color,
            prior_pid=resolved_prior_pid,
            prior_start_token=resolved_prior_start_token,
            poller_gate="local_service_quiesced",
            set_active_targets=set_active_targets,
            # rollback ignores the candidate (its target is the ledger's
            # previous); the executor still spawns FROM candidate's venv/code.
            symlink_swap=lambda _candidate: self._release_manager.rollback(),
            # A corrupt-executable previous (candidate_for passed dir+VERSION but
            # Popen raises) OR a previous that never health-checks both mean the
            # safety net itself is non-functional → NEEDS_INTERVENTION, not a
            # retryable spawn/register hiccup.
            spawn_failure=(
                RestartStatus.NEEDS_INTERVENTION,
                RestartReasonCode.ROLLBACK_TARGET_UNBOOTABLE,
            ),
            register_failure=(
                RestartStatus.NEEDS_INTERVENTION,
                RestartReasonCode.ROLLBACK_TARGET_UNBOOTABLE,
            ),
            compensation_codes=(
                RestartReasonCode.ROLLBACK_COMPENSATED,
                RestartReasonCode.COMPENSATION_INCOMPLETE,
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prepare_swap(
        self, *, reason: str, expected_etag: str, app_home: Path,
        self_color: str, self_instance_id: str,
        reconciliation_provenance: dict[str, str] | None = None,
    ) -> _SwapPlan | RestartResult:
        """Pre-spawn phase: confirm self active + materialize + preflight candidate.

        Returns a :class:`_SwapPlan` (next color + the built candidate
        release) on success, or a FAILED :class:`RestartResult` on any
        pre-spawn refusal — router unreachable, self not the router-active
        instance (C3: color AND instance must match), a release build
        failure, or a non-additive schema diff (§3). On every failure path
        nothing is spawned and the live router + ``current``/``previous``
        stay untouched.

        ``reconciliation_provenance`` reaches the candidate build and nothing
        else — see :func:`_provenance_kwargs`.
        """
        try:
            status_snap = self._router.status()
        except RouterClientError as exc:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message=f"router status() failed: {exc}",
                reason_code=RestartReasonCode.ROUTER_UNREACHABLE,
            )
        next_color = _derive_next_color(
            status_snap, self_color, self_instance_id, self._logger,
        )
        if next_color is None:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message=(
                    "router status() did not show this instance as the active "
                    "color; refusing to spawn a parallel green from a "
                    "non-active blue."
                ),
                reason_code=RestartReasonCode.NOT_ACTIVE_INSTANCE,
            )
        # §4.5/§4.7: materialize the immutable candidate BEFORE the spawn so the
        # green child is launched from the candidate's own venv/code — NOT
        # ``current``, which still names the live old release until ``cutover``
        # flips it post-activate.
        snapshot_fn = build_schema_snapshot_fn(
            solet_name=self._solet_name,
            app_home=app_home,
            source_root=resolve_project_root(app_home),
        )
        try:
            candidate = self._release_manager.build_candidate(
                manifest_etag=expected_etag,
                manifest_plugins=_capture_manifest_plugins(app_home),
                schema_snapshot_fn=snapshot_fn,
                **_provenance_kwargs(reconciliation_provenance),
            )
        except ReleaseManagerError as exc:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message=f"build candidate release failed: {exc}",
                reason_code=RestartReasonCode.BUILD_FAILED,
            )
        self._logger.info(
            "built candidate release=%s missing_pth=%d",
            candidate.release_id, len(candidate.missing_pth_targets),
        )
        # §4.7 lifecycle GC: having materialized a new release, reap the stale
        # tail (keep last K; §4.6 GC-safety never reaps current/previous/
        # in_progress). Running it on EVERY build — before the preflight that
        # may refuse this candidate — bounds BOTH accumulation sources to ~K:
        # superseded releases AND rejected candidates from a run of
        # build-then-refuse / post-build failures (the just-built candidate is
        # newest, so it survives; the next build reaps it if it never landed).
        _gc_releases(self._release_manager, self._logger)
        # §3 preflight DDL-free gate: durable code rollback only holds over an
        # unchanged/additive schema. A non-additive diff is refused HERE, before
        # any spawn. The rejected candidate dir is left for the NEXT build's GC.
        try:
            current_snapshot = _resolve_current_snapshot(self._release_manager, snapshot_fn)
        except ReleaseManagerError as exc:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message=f"schema preflight could not resolve the current snapshot: {exc}",
                reason_code=RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED,
            )
        verdict = self._schema_preflight(
            candidate,
            current_snapshot=current_snapshot,
            current_release_exists=self._release_manager.current_release is not None,
        )
        if not verdict.is_additive:
            return self._failure(
                reason=reason, expected_etag=expected_etag,
                message=(
                    "schema preflight refused deploy — durable rollback only "
                    f"protects code over an additive schema. {verdict.summary()}"
                ),
                reason_code=RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED,
            )
        # GTE-06 L2 fresh-source probe — the LAST pre-spawn gate (design
        # §3.1: cheap gates first; the probe pays a subprocess + full
        # import pass, so it runs only on candidates that passed
        # everything cheaper). RED ⇒ PROBE_FAILED before any spawn; core
        # reacts by rolling the committed manifest bytes back.
        probe_outcome = _contained_probe_outcome(
            self._preflight_probe, candidate=candidate, app_home=app_home,
            logger=self._logger,
        )
        if not probe_outcome.ok:
            return _probe_failed_restart_result(
                probe_outcome, reason=reason, expected_etag=expected_etag,
                logger=self._logger,
            )
        return _SwapPlan(
            next_color=next_color, candidate=candidate,
            probe_evidence=probe_outcome.payload,
        )

    def _dry_run_envelope(
        self, reason: str, expected_etag: str, self_color: str
    ) -> RestartResult:
        next_color = (
            opposite_color(self_color) if is_valid_color(self_color) else COLOR_GREEN
        )
        message = (
            f"dry_run=True; would spawn next_color={next_color}, "
            f"wait for register, activate, quiesce, enqueue complete_swap. "
            f"Reason: {reason}"
        )
        return RestartResult(
            status=RestartStatus.QUEUED,
            restart_action_id=_now_audit_token(),
            message=message,
            reason=reason,
            expected_etag=expected_etag,
            dry_run=True,
        )

    def _failure(
        self, *, reason: str, expected_etag: str, message: str,
        reason_code: str = RestartReasonCode.NONE,
    ) -> RestartResult:
        """Build a pre-spawn FAILED result (router/build/preflight refusals).

        Always FAILED — every pre-spawn refusal leaves the system unchanged
        and is retryable. Delegates to ``build_failed_result`` so the failure
        envelope (status, reason_code, message, error log) is constructed in
        exactly one place, shared with the executor's in-flight failures.
        """
        return build_failed_result(
            status=RestartStatus.FAILED,
            reason_code=reason_code,
            message=message,
            reason=reason,
            expected_etag=expected_etag,
            logger=self._logger,
        )


__all__ = [
    "ActionFactoryProtocol",
    "COLOR_BLUE",
    "COLOR_GREEN",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "PreflightProbeFn",
    "ReleaseManagerProtocol",
    "SchemaPreflightFn",
    "SetActiveTarget",
    "SetColorActiveFn",
    "SpawnFn",
    "SwapOrchestrator",
    "default_spawn",
]
