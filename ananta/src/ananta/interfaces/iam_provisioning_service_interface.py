"""IAM Provisioning service interface — herky-jerky-loaded admin contract.

The fourth AWS lifecycle plugin's contract per Step 1 v2 §11 + Step 7 design.
Provisions the per-birther IAM lifecycle role quartet's create-able members
(``<birther>-midwife`` + ``<birther>-undertaker``). The ``<solet>-self-
deployment`` role is midwife scope (created per-solet at birth time);
the ``<birther>-admin`` role itself is operator-manual (chicken-and-egg).

Per the D1 architectural mandate
(``ananta_platform/08_service_architecture/SERVICE_ARCHITECTURE.md``), the
``@service_interface_process`` registration surface lives at
``ananta/src/ananta/services/iam_provisioning_service/interfaces/public.py``
with matching JSONs at
``ananta/knowledge_base/processes/iam_provisioning_service/<verb>.json``.

Per D30' closure (operator decision 2026-06-02 PM): v1 verb subset is
``provision_lifecycle_roles`` + ``audit_lifecycle_roles`` ONLY.
``rotate_role_policies`` is redundant per the idempotency-as-rotation
insight in design §6.2 (``provision_lifecycle_roles`` with a new
``policy_template_version`` detects drift and applies the update);
``decommission_role`` is rare operator-action acceptable via AWS console.
Fast-fail / ``[[no-phantom-abstractions]]`` discipline.

Per D-LANDMINE-ASYNC (α) closure, both verbs are SYNC ``def``. The platform
has no async dispatch path for ``@service_interface_process``-decorated
methods; long-running concerns (an admin verb that creates 2 IAM roles
takes ~5–15 seconds, well within sync tolerance) handled inline.

Per the herky-jerky load semantics (Step 7 design §5), this interface's
binding is structurally absent from the steady-state process registry; the
operator initiates a transient cycle via
``self_deployment_service::restart_with_manifest`` to ADD the bound plugin,
runs the admin verbs, then REMOVEs the plugin via another manifest cycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.interfaces.lifecycle_result_types import AdminResult


class IamProvisioningServiceInterface(ABC):
    """Provision + audit the per-birther IAM lifecycle role quartet's create-able members."""

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def provision_lifecycle_roles(
        self,
        *,
        birther_name: str,
        role_names_required: list[str],
        policy_template_version: str,
        dry_run: bool = False,
    ) -> AdminResult:
        """Create or update ``<birther>-midwife`` and ``<birther>-undertaker`` IAM roles.

        Contract:

        - Idempotent under re-runs. For each requested role, the
          implementation probes via ``iam:GetRole`` and skips when the
          trust policy hash + inline policy hash + tag set already match
          the expected shape for the supplied ``policy_template_version``.
          Drift triggers ``iam:UpdateAssumeRolePolicy`` +
          ``iam:PutRolePolicy`` + ``iam:TagRole``.

        - Same call IS the rotation path. Operator calling this verb with
          a new ``policy_template_version`` against existing roles
          detects the version mismatch via the tag set and updates the
          inline policies. No separate ``rotate_role_policies`` verb
          (D30' closure).

        - ``dry_run=True`` MUST plan + report without issuing any
          ``iam:CreateRole`` / ``iam:PutRolePolicy`` /
          ``iam:UpdateAssumeRolePolicy`` / ``iam:DeleteRole`` /
          ``iam:TagRole`` / ``iam:UntagRole`` calls. Per Step 1 v2 §14,
          the same call with ``dry_run=False`` attempts the steps
          surfaced in the dry-run output — no divergence allowed.

        Args:
            birther_name: The birther solet whose midwife/undertaker
                roles are being provisioned. Validated against
                ``[a-z][a-z0-9_-]{1,62}``. Used to compose role names:
                ``<birther_name>-midwife``, ``<birther_name>-undertaker``.
            role_names_required: Subset of ``["midwife", "undertaker"]``;
                controls which roles to provision. Empty list rejects
                with ``status=AdminStatus.REJECTED`` and
                ``message="no_roles_specified"``. Allows callers to
                provision just midwife OR just undertaker if the birther
                is single-purpose.
            policy_template_version: Opaque semver string (e.g.
                ``"v1.0.0"``) the caller pins. The plugin's policy
                rendering code carries a constant of "blessed" versions;
                mismatches reject with
                ``message="policy_template_version_unsupported"``.
                Forces operator to update the plugin before claiming
                a new version.
            dry_run: Plan-only mode (see contract above).

        Returns:
            :class:`~ananta.interfaces.lifecycle_result_types.AdminResult`
            carrying ``verb="provision_lifecycle_roles"``, the terminal
            status, per-role audit ``steps``, and the
            ``roles_provisioned`` ARNs for roles that were created or
            updated this attempt.
        """

    @abstractmethod
    def audit_lifecycle_roles(
        self,
        *,
        birther_name: str,
    ) -> AdminResult:
        """Read-only inventory of the birther's lifecycle role quartet members.

        Reports on each role's existence, trust policy hash, inline
        policy hash, attached managed policies, and tag set. Detects
        drift between the expected shape (per the plugin's pinned policy
        template versions) and live AWS state. Detects orphaned roles
        (``<birther>-midwife`` exists but trust policy doesn't permit
        the birther's task role anymore).

        The verb returns the same per-step drift-detection fields that
        ``provision_lifecycle_roles(dry_run=True)`` would surface, so
        operators have a clean "audit then provision" workflow without
        duplicate state machines. Audit is read-only — no IAM mutations
        of any kind, no resource tags written.

        Args:
            birther_name: The birther solet whose role quartet is
                inspected. Same validation as
                :meth:`provision_lifecycle_roles`.

        Returns:
            :class:`~ananta.interfaces.lifecycle_result_types.AdminResult`
            carrying ``verb="audit_lifecycle_roles"``,
            ``status=AdminStatus.SUCCESS`` when all probe calls
            completed (regardless of role-existence outcomes), and per-
            role audit ``steps`` reporting found / missing / drift
            status. ``policy_template_version`` is empty at the
            envelope level because each role can carry a different
            version; per-step output records the per-role version.
        """
