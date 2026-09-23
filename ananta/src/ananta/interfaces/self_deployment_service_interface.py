"""Self-deployment lifecycle base — ``restart_with_manifest`` verb.

The platform's ``lifecycle_management_service::apply_manifest`` verb writes
a new manifest to disk, then delegates the actual restart to whichever
plugin is bound to ``self_deployment_service`` in the live profile:

- **``aws_self_deployment_plugin``** (cloud profile) runs the 12-step
  blue-green ECS swap with the new manifest in place — same image, new
  env. The cloud plugin additionally inherits
  :class:`CloudSelfDeploymentServiceInterface` for the four cloud-only
  operational verbs (``deploy_self`` / ``complete_deploy`` /
  ``deploy_status`` / ``deploy_rollback``).
- **``macos_self_deployment_plugin``** (macOS profile) runs the
  router-mediated blue/green swap — spawning the next color via
  ``python -m ananta.cli``, polling the router until green registers,
  swapping atomically, and quiescing blue for the drain window.
  Implements only the base verb; the cloud-extension surface is not
  bound on macOS profiles.

The interface is one verb intentionally: cloud-specific multi-verb
mechanics live on :class:`CloudSelfDeploymentServiceInterface`. The
matrix plan's lifecycle row (Architect's 2026-06-02 design record, §6
Step 3, dev-checkout workbench — not part of the shipped tree) unifies
the macOS and cloud restart surfaces under this single name.

Per the D1 architectural mandate
(``ananta_platform/08_service_architecture/SERVICE_ARCHITECTURE.md``),
the matching ``@service_interface_process``-decorated public-API
surface lives at
``ananta/src/ananta/services/self_deployment_service/interfaces/public.py``
with KB JSONs under
``ananta/knowledge_base/processes/self_deployment_service/``.

Verb semantics: per D6' (operator-confirmed 2026-06-02), the verb is
**sync** — backgrounding is the caller's concern via
``scheduling_service::execute_in_seconds``. Implementations whose
restart kills the calling process (macOS watchdog-driven re-launch)
return ``RestartStatus.QUEUED`` once the watchdog is detached and the
parent is about to die. Cloud implementations return ``QUEUED`` with
the finisher action id so the operator can poll a backend-specific
status verb (``cloud_self_deployment_service::deploy_status``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from ananta.interfaces.lifecycle_result_types import (
    RestartResult,
    StopSelfResult,
)


class SelfDeploymentServiceInterface(ABC):
    """Lifecycle base verbs: restart with a new manifest, stop self."""

    INTERFACE_VERSION: ClassVar[str] = "2.1.0"

    @abstractmethod
    def restart_with_manifest(
        self,
        *,
        new_manifest: dict[str, Any],
        expected_etag: str,
        reason: str,
        dry_run: bool = False,
    ) -> RestartResult:
        """Trigger a restart of this solet with the new manifest active.

        Contract:

        - The caller (``lifecycle_management_service::apply_manifest``) has
          already validated the new manifest and written it to disk at the
          well-known location (``<APP_HOME>/config/manifest.yaml`` plus
          ``<APP_HOME>/config/service_bindings.json``). Implementations
          MUST NOT re-validate the manifest; the caller's pre-flight is
          authoritative.

        - ``expected_etag`` is the CAS lock from the lifecycle-interfaces
          design record §13.2 (dev-checkout workbench — not part of the
          shipped tree). Implementations re-read the on-disk manifest's stored ETag at
          dispatch time and refuse the restart when it differs from
          ``expected_etag``. This guards against a second
          ``apply_manifest`` racing the first between manifest-write and
          restart-dispatch.

        - Implementations choose HOW the restart happens: cloud sibling
          uses blue-green ECS swap (returns ``RestartStatus.QUEUED``
          with the finisher action id); macOS sibling uses
          watchdog-driven re-launch (returns ``RestartStatus.QUEUED``
          once the watchdog is detached).

        Args:
            new_manifest: The manifest dict that was just written.
                Implementations may consult it for routing decisions but
                the on-disk copy is the source of truth.
            expected_etag: The manifest's ETag the caller observed at
                write time. The implementation refuses the restart when
                the stored ETag has moved on since.
            reason: Operator-supplied reason string. Recorded for audit;
                surfaced in deployment-result messages.
            dry_run: When ``True``, the implementation plans + reports
                without mutating any cloud / local-process state.

        Returns:
            :class:`RestartResult` carrying the terminal status, the
            backend-specific follow-on action id (if any), and the
            echoed ``reason`` / ``expected_etag`` / ``dry_run`` for
            audit correlation.
        """

    @abstractmethod
    def stop_self(
        self,
        *,
        reason: str,
        dry_run: bool = False,
    ) -> StopSelfResult:
        """Stop this solet without tearing down its infrastructure.

        Per Slice 4.5 of the bridge-port-routing-and-session-lifecycle
        design record (dev-checkout workbench — not part of the shipped
        tree). Distinct from ``aws_undertaker_plugin::teardown_solet``:
        teardown DESTROYS infra (RDS, ALB, ACM, KMS, S3, ECS service
        definitions); stop_self leaves ALL of that in place and only
        sets the live serving capacity to zero. Operators bring the
        solet back later without re-provisioning.

        Contract:

        - **macOS:** writes a drain sentinel that persists across the
          stop (caller does NOT auto-clean it; ``launch.py``'s
          ``_cleanup_stale_runtime_files`` scrubs it on the next cold
          start). Then spawns a detached watchdog subprocess that
          SIGTERMs the solet child shortly after the verb returns
          (delay long enough for the verb's response to flush to the
          caller). The watchdog escalates to SIGKILL after a bounded
          window if SIGTERM is ignored. The verb returns
          ``StopSelfStatus.SUCCESS`` once the watchdog is detached.

        - **Cloud:** assumes the self-deployment role,
          ``ecs.update_service(desiredCount=0)`` on the currently-active
          ECS service (resolved via the existing listener-rule + TG +
          version-parsing chain), polls ``describe_services`` until
          ``runningCount=0`` within a bounded window. Idempotent:
          returns ``ALREADY_STOPPED`` when ``desiredCount`` is already
          0 at dispatch time.

        ``dry_run=True`` plans + reports without writing the sentinel,
        spawning the watchdog, or calling ECS; returns
        ``StopSelfStatus.DRY_RUN``.

        Args:
            reason: Operator-supplied audit string. Required — a
                stop without an audit message is undisciplined operator
                action; the verb refuses to default it. Recorded on
                the LaunchAgent log / CloudWatch + echoed in the
                response.
            dry_run: When ``True``, plans + reports without mutating
                any state.

        Returns:
            :class:`StopSelfResult` carrying the terminal status, echo
            of ``reason`` / ``dry_run``, the backend-specific action
            id (watchdog pid for macOS, service ARN for cloud), and
            the wall-clock duration.
        """
