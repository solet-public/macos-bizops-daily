"""Cloud Self-Deployment Service Interface — blue-green ECS swap verbs.

Extension of :class:`SelfDeploymentServiceInterface` carrying the four
cloud-specific operational verbs. A cloud solet owns her own deploy
lifecycle: the operator pushes a new image to ECR via CodeBuild
(operator-side build), then sends one MCP message to the solet:

    service_interface::cloud_self_deployment_service::deploy_self(image_tag="v4")

The solet's own process registry orchestrates the blue-green swap
using her own IAM task role. the solet (the operator's solet) is not in
the chain except as an MCP client transport.

Four verbs are exposed:

- ``deploy_self``     — runs steps 1–7 (detect, birth sibling, health,
                        smoke, cutover, drain), enqueues
                        ``complete_deploy`` as a durable action for the
                        new color, returns.
- ``complete_deploy`` — internal v(N+1)-side finisher; runs steps 8–10
                        (observation window, teardown of old,
                        post-cutover schema actions). Not invoked by
                        operators directly; picked up by the new
                        color's action_queue_poller.
- ``deploy_status``   — read-only AWS introspection; always callable.
- ``deploy_rollback`` — operator abort during the observation window;
                        cancels the enqueued ``complete_deploy`` and
                        swaps the listener rule back.

The split between ``deploy_self`` and ``complete_deploy`` exists because
the v(N) container runs ``deploy_self`` and the same container's ECS
task is terminated by step 9 — running steps 8–10 inline would race
teardown against result delivery. The durable action ensures v(N+1)
picks up cleanly via the shared Postgres action queue.

Implementations inherit from this class (which transitively inherits
from :class:`SelfDeploymentServiceInterface`), so the AWS plugin
satisfies both the cloud-extension surface AND the base lifecycle
surface from a single class hierarchy. The platform's
``validate_service_provider`` checks strict membership against the
plugin's declared ``service_interfaces`` tuple
(``ananta/src/ananta/core/orchestration/service_bindings.py``
``SERVICE_INTERFACE_MAP``), so plugins MUST declare BOTH interfaces in
their ``service_interfaces`` tuple even though Python's MRO already
satisfies ``isinstance`` for both — the platform does not walk the MRO
to discover implicitly-inherited interfaces (D16' verification,
2026-06-02).

Verb semantics: per D6' (operator-confirmed 2026-06-02), all verbs on
this extension are **sync** — backgrounding is the caller's concern.
Signatures preserve the pre-rename behaviour of the previous
``SelfDeploymentServiceInterface`` (4-verb cloud surface) unchanged
per the AWS-self-deployment-plugin design record §10.2
("no behavioral change to deploy_self / complete_deploy / deploy_status
/ deploy_rollback").

See the same design record §3.3 (extension-interface pattern) and the
self-deployment-plugin design record for the blue-green mechanics this
extension carries (both dev-checkout workbench records — not part of
the shipped tree).
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar

from ananta.interfaces.self_deployment_service_interface import (
    SelfDeploymentServiceInterface,
)


class CloudSelfDeploymentServiceInterface(SelfDeploymentServiceInterface):
    """Cloud extension: blue-green ECS swap on top of restart-with-manifest."""

    CLOUD_INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def deploy_self(
        self,
        image_tag: str,
        timeout_seconds: int = 300,
        observation_window_seconds: int = 60,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run steps 1–7 of the blue-green swap on the current container.

        Reads the live ALB listener rule to discover the current
        ``<self>-v<N>``, verifies the new image exists in ECR, births
        ``<self>-v<N+1>`` (task definition, target group, ECS service),
        waits for health, optionally smoke-probes, atomically swaps the
        listener rule, drains the old TG.

        Returns at the end of drain with ``status='cutover_complete'``
        and ``finisher_action_id`` pointing at the durable
        ``complete_deploy`` row enqueued for the new color. Steps 8–10
        run there.

        ``dry_run=True`` emits the planned AWS calls without executing
        any of them. Resource tags (``HomunculusName``, ``Version``)
        make every step idempotent — a re-invocation with the same
        ``image_tag`` resumes cleanly from whichever step last
        completed.
        """

    @abstractmethod
    def complete_deploy(
        self,
        finisher_action_id: str,
        prior_version: str,
        observation_window_seconds: int = 60,
    ) -> dict[str, Any]:
        """Run steps 8–10 on the new color after cutover (v(N+1)-side finisher).

        Internal-only. Picked up from the platform action queue by the
        new color's ``action_queue_poller`` after ``deploy_self`` enqueued
        the row. Observes for ``observation_window_seconds`` (cancellable
        via the row status set by ``deploy_rollback``), tears down
        ``<self>-v<N>``, then enqueues any post-cutover destructive
        schema actions for the ``plugin_schema_service``.

        ``prior_version`` is the version label of the now-old color
        whose service + target group get deleted in step 9.
        """

    @abstractmethod
    def deploy_status(self, detail: bool = False) -> dict[str, Any]:
        """Read AWS state and report the live version + any in-flight deploy.

        Always callable. Reads ALB listener rule + ECS service
        descriptors; does not consult plugin in-memory state. Returns
        the current live version (from the listener rule's forward
        target group), and when a sibling ``<self>-v<N+1>`` is
        mid-flight, returns the current step, target version, and
        observation-window timestamps.

        Setting ``detail=True`` includes the resolved ARNs in the
        response.
        """

    @abstractmethod
    def deploy_rollback(self, reason: str) -> dict[str, Any]:
        """Swap the ALB listener rule back to the previous version.

        Callable only between cutover (step 6 of ``deploy_self``) and
        the end of the observation window (step 8 inside
        ``complete_deploy``). The verb verifies the old target group
        still exists, atomically modifies the listener rule back to it,
        and cancels the enqueued ``complete_deploy`` row by writing
        ``status='cancelled'``. The new color's poller observes the
        cancellation and skips its teardown work.

        Outside the window — before cutover, or after the old service
        has been deleted — returns ``status='rollback_not_applicable'``.
        """
