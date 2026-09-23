"""Local Self-Deployment Service Interface — blue-green-on-localhost swap verbs.

Extension of :class:`SelfDeploymentServiceInterface` carrying the three
local-blue-green-specific operational verbs. The local-blue-green
plugin (``plugins/macos_self_deployment_plugin/``) orchestrates a
router-mediated swap between two co-resident the solet instances on the same
machine, using the standalone router process under
``plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/router.py``.

Three verbs are exposed on top of the base
:meth:`SelfDeploymentServiceInterface.restart_with_manifest`:

- ``complete_swap``  — internal v(N+1)-side finisher; SIGTERMs the
                       prior color, then unregisters it from the
                       router. Picked up via the durable action
                       queue, mirroring the cloud sibling's
                       ``complete_deploy`` split.
- ``swap_status``    — read-only introspection. Returns the router's
                       ``status()`` payload plus any plugin-local
                       in-flight swap state. Touches no state.
- ``swap_rollback``  — operator-triggered. Re-points the router back
                       to the previously-active color when that color
                       is still inside its drain window.

Implementations inherit from this class (which transitively inherits
:class:`SelfDeploymentServiceInterface`), so the local plugin satisfies
BOTH the local-extension surface AND the base lifecycle surface from a
single class hierarchy. Per D16' (2026-06-02 verification), the
platform's ``validate_service_provider`` checks strict tuple membership
against the plugin's declared ``service_interfaces`` tuple — it does
NOT walk MRO to discover implicitly-inherited interfaces — so plugins
MUST declare BOTH interfaces in their ``service_interfaces`` tuple.

The split between ``restart_with_manifest`` and ``complete_swap``
mirrors the cloud sibling's ``deploy_self`` / ``complete_deploy`` split
and exists for the same reason: the actor that initiates the swap is
the actor being torn down (blue spawns green, blue activates green,
blue must SIGTERM-itself last). Running the SIGTERM step inline inside
``restart_with_manifest`` would kill blue while it is still answering
the caller; the durable action handoff via the platform action queue
lets green's ``action_queue_poller`` pick up the finisher cleanly,
issue the SIGTERM, and the (now-active) green proceeds to serve.

Verb semantics: all verbs on this extension are **sync** —
backgrounding is the caller's concern via
``scheduling_service::execute_in_seconds``.

See the L3-implementation-plan design record §3.3 (Slice E spec) and the
blue-green-availability design record §4 (both dev-checkout workbench —
not part of the shipped tree) for the design framing.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar

from ananta.interfaces.self_deployment_service_interface import (
    SelfDeploymentServiceInterface,
)


class LocalSelfDeploymentServiceInterface(SelfDeploymentServiceInterface):
    """Local extension: blue-green-on-localhost swap on top of restart-with-manifest."""

    INTERFACE_VERSION: ClassVar[str] = "1.2.0"
    LOCAL_INTERFACE_VERSION: ClassVar[str] = INTERFACE_VERSION

    @abstractmethod
    def attest_runtime_code(
        self,
        *,
        reconciliation_id: str,
        verification_modules: tuple[str, ...],
    ) -> dict[str, Any]:
        """Attest the immutable release and already-loaded module provenance.

        This read-only verb inspects ``sys.modules`` without importing a
        requested module.  It is the independent router-served witness used by
        reconciliation, and is also useful to doctor shipped-but-unread code.
        """

    @abstractmethod
    def cutover_release(
        self,
        *,
        reconciliation_id: str,
        expected_source_surface_sha256: str,
        expected_release_surface_sha256: str,
        expected_manifest_etag: str,
        expected_current_release_id: str,
        expected_active_instance_id: str,
        expected_active_start_token: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Cut over to a candidate built from already-refreshed bytes.

        The public face of the shared cutover machinery, for targets that
        already expose this interface — idempotent re-runs and cutover-only
        plans.  The reconciliation path itself does NOT depend on this verb: a
        pre-interface target cannot answer a verb it has never heard of, and
        requiring one would leave exactly the stranded targets that motivated
        this channel unrepairable.  Those go through the in-process controller
        invoked by the refreshed target-local adapter instead.  One state
        machine, two entry points.

        Every compare-and-swap leg is checked before anything is spawned, so a
        stale-approval return means nothing was built and nothing moved.
        """

    @abstractmethod
    def complete_swap(
        self,
        prior_pid: int,
        prior_instance_id: str,
        prior_color: str,
    ) -> dict[str, Any]:
        """Tear down the prior color after activation completes.

        Internal-only. Picked up from the platform action queue by the
        new (now-active) color's ``action_queue_poller`` after
        ``restart_with_manifest`` enqueued the row. The new color
        SIGTERMs ``prior_pid``, waits for the process to exit (a short
        bounded wait, then SIGKILL as last resort), then unregisters
        ``prior_instance_id`` from the router so the router's
        ``status()`` no longer lists the dead binding.

        Idempotent — a repeated invocation finds the prior process
        already gone + the prior instance already unregistered and
        returns success without error.

        Args:
            prior_pid: OS pid of the previously-active solet process,
                recorded at enqueue time inside the prior color.
            prior_instance_id: Router-side instance id of the prior
                color, recorded at enqueue time and unregistered at
                tear-down time.
            prior_color: Color token (``"blue"`` or ``"green"``) of the
                prior color. Carried for audit + diagnostic surfacing;
                not load-bearing for the tear-down mechanic.

        Returns:
            Envelope with ``status`` (``"completed"`` or ``"failed"``),
            ``prior_instance_id``, ``prior_color``, and ``steps_completed``.
        """

    @abstractmethod
    def swap_status(self) -> dict[str, Any]:
        """Read router state + the plugin's own swap-in-progress state.

        Always callable. Returns the router's ``status()`` payload
        (active color, drain entries, registered colors) plus any
        plugin-local state about an in-flight swap. Touches no state.

        Returns:
            Envelope with ``router_status`` (the full router status
            snapshot) and ``swap_in_progress`` (``True`` while a
            restart_with_manifest call is mid-flight in this instance).
        """

    @abstractmethod
    def swap_rollback(self, reason: str) -> dict[str, Any]:
        """Re-point the router back to the previously-active color.

        Operator-triggered. Callable only while the prior color is
        still within its drain window (per
        ``router_state.DEFAULT_DRAIN_WINDOW_SECONDS``). Calls
        ``router.rollback(prior_color)``, which atomically swaps the
        router's active binding back to the prior color. Outside the
        window — before activate fired, or after drain expired —
        returns ``status='rollback_not_applicable'``.

        Args:
            reason: Operator-supplied audit string recorded in the
                rollback result envelope.

        Returns:
            Envelope with ``status``, ``rolled_back_to`` (the now-active
            color), ``reason``, and any error detail from the router.
        """
