"""Midwife service interface — birth a new solet from zero-state.

Lifecycle's birth verb. Implemented by environment-specific plugins:
``aws_midwife_plugin`` for cloud (Step 4 of the matrix design);
``macos_midwife_plugin`` for the operator's local box (Step 6 future
work). The verb takes a name + a profile template + a backend-specific
``environment_config`` bag and provisions every per-solet resource
the newborn needs to boot + reach + serve.

Per the D1 architectural mandate
(``ananta_platform/08_service_architecture/SERVICE_ARCHITECTURE.md``),
the matching ``@service_interface_process``-decorated public-API
surface lives at
``ananta/src/ananta/services/midwife_service/interfaces/public.py``
with KB JSONs under
``ananta/knowledge_base/processes/midwife_service/``. The registry
refuses to build at startup if a decorated method is missing its JSON.

Per D6' / Architect's matrix design §17 (``(α)`` closure), the verb
is **sync** — backgrounding is the caller's concern via
``scheduling_service::execute_in_seconds``. The platform has no
async dispatch path for ``@service_interface_process``-decorated
methods.

Per D8' (idempotency model in Step 4 design §4.2), implementations
MUST be idempotent under mixed-state cleanup: every step probes
presence via the backend's Describe/List/Get verb and skips the
corresponding Create when already present, rather than blind-creating
and catching already-exists errors. AWS resource tags
(``HomunculusName=<name>`` + ``MidwifeAttempt=<idempotency_key>``)
are the durable cross-step ledger.

Per D33' (Step 4 §2.5), the a-birthed path is dynamic: midwife always
creates ``<newborn>-task`` + ``<newborn>-ecs-task-execution-role`` +
``<newborn>-self-deployment``; conditionally creates
``<newborn>-{admin,midwife,undertaker}`` IFF the newborn's profile
manifest declares the plugins requiring those roles.

See ``workbench/2026-06-02_aws_midwife_plugin_design.md`` §3, §4, §5.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from ananta.interfaces.lifecycle_result_types import BirthResult, ImageBuildResult


class MidwifeServiceInterface(ABC):
    """Bring a new solet from zero-state to live + reachable."""

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def birth_solet(
        self,
        *,
        name: str,
        profile_template: str,
        environment_config: dict[str, Any],
        dry_run: bool = False,
    ) -> BirthResult:
        """Provision every per-solet resource the newborn needs.

        Contract:

        - **Validated name** — ``name`` MUST match
          ``[a-z][a-z0-9_-]{1,62}``. The midwife discovers + tags
          resources by name; an unvalidated name could collide with
          existing resources or fail downstream AWS API validators.

        - **Backend-specific ``environment_config``** — the
          ``aws_midwife_plugin`` consumes keys like ``region``,
          ``account_id``, ``dns_zone_id``, ``shared_vpc``,
          ``shared_alb_arn``, ``shared_cluster``, ``birther_name``
          (per design §4.1). Other future midwife plugins (macOS,
          Linux) define their own contract. Missing required keys
          MUST raise ``ValueError`` at the validation seam.

        - **Idempotent under re-runs** — re-issuing the call with the
          same arguments after a partial-completion failure resumes
          from the last-completed step rather than re-creating
          resources. ``BirthStatus.PARTIAL`` indicates a recoverable
          mid-flight failure; ``BirthStatus.FAILED`` indicates the
          verb could not begin (config / address-book seam failure).

        - **``dry_run=True``** — plan + report without issuing any
          state-changing backend calls. Re-running the same arguments
          with ``dry_run=False`` MUST attempt the same steps; no
          divergence allowed.

        Args:
            name: Lowercase first-name of the newborn (validated regex
                above). The unique identifier the platform uses for
                schema namespacing, resource tagging, hostname
                composition, and address-book entries.
            profile_template: Profile template the newborn boots
                under (e.g. ``"cloud"`` resolves against
                ``initialization/profiles/cloud.yaml``). Determines
                which plugins the newborn loads + which IAM roles
                the midwife conditionally creates per the a-birthed
                path.
            environment_config: Backend-specific dict of configuration
                the midwife consumes. Keys validated by the
                implementation per its own contract.
            dry_run: Plan-only mode. When ``True``, no backend state
                is mutated; the result enumerates the planned steps.

        Returns:
            :class:`BirthResult` carrying the terminal status, the
            audit-trail manifest path, IAM role ARNs created, RDS +
            KMS identifiers (for subsequent teardown discovery), the
            new solet's HTTPS endpoint, and a per-step audit
            log.
        """

    @abstractmethod
    def build_and_push(
        self,
        *,
        newborn_name: str,
        image_tag: str,
        profile_template: str = "cloud",
        dry_run: bool = False,
    ) -> ImageBuildResult:
        """Build and push the newborn container image before birth.

        Contract:

        - The bound midwife implementation stages the birther's source
          tree for ``profile_template``, triggers its backend build
          mechanism, and returns the resulting image URI/digest.

        - The caller passes the returned ``image_uri`` into
          ``birth_solet`` via ``environment_config``. The build
          verb does not birth or mutate newborn infrastructure beyond
          the image-build staging/build resources.

        - ``dry_run=True`` reports the planned staging/build envelope
          without writing S3 objects, starting CodeBuild, or pushing an
          image.

        Args:
            newborn_name: Solet name whose image is being built.
            image_tag: Operator-selected version token for the build.
            profile_template: Profile template controlling source
                filtering and build inputs. Defaults to ``"cloud"``.
            dry_run: Plan-only mode.

        Returns:
            :class:`ImageBuildResult` carrying image URI, digest,
            CodeBuild/log identifiers, and status.
        """
