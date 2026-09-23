"""Typed result envelopes for the lifecycle-plugin matrix.

The four lifecycle service interfaces (self-deployment, midwife, undertaker,
and the cloud-extension surfaces) return structured result records rather
than open-ended ``dict[str, Any]`` envelopes. This module owns those record
types so the interfaces import the same canonical shapes and so any caller
(``apply_manifest``, smokes, downstream peer dispatchers) destructures a
typed value rather than guessing at a free-form dict.

Per workbench/2026-06-02_lifecycle_interfaces_design.md §15, the result
records are frozen dataclasses with slots. They carry only fields the
interface contract guarantees populated; backend-specific extras (AWS
finisher action ids, macOS watchdog pids, etc.) live on the per-record
optional fields the interface promises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RestartStatus(StrEnum):
    """Terminal status returned by ``restart_with_manifest`` and the
    durable-rollback trigger verb (``rollback_release``).

    ``queued`` — the restart/rollback was scheduled (cloud blue-green path;
    the local materialized-release path: the reactivated color's poller will
    SIGTERM the prior process. The operator polls the finisher action id for
    completion). ``completed`` — the restart finished synchronously (no
    implementation does this in v1; reserved for a future in-process
    restart). ``failed`` — the operation could not be scheduled AND the
    system is left UNCHANGED + coherent, so the caller may safely RETRY
    (e.g. no rollback target, ETag mismatch, a refused activate that left the
    prior color authoritative, or a swap that failed and was cleanly
    compensated back to the pre-swap pair). ``needs_intervention`` —
    automated recovery is EXHAUSTED and a human must act before the next
    automated attempt is safe (e.g. the rollback target failed to boot — the
    safety net itself is void — or a swap's compensation could not complete,
    so the durable ``current``/``previous`` pair MAY be incoherent). The
    distinction between ``failed`` and ``needs_intervention`` is the
    retryable/escalate partition: ``failed`` is self-healing on retry,
    ``needs_intervention`` is not. Implementations whose restart kills the
    calling process (macOS watchdog-driven re-launch) return ``queued`` once
    the watchdog has been detached and the parent is about to die.

    Consumer note: ``lifecycle_management_service`` treats every status
    outside ``{queued, completed}`` as "the restart did NOT apply the
    manifest" via a deny-list (NOT an exhaustive match), so
    ``needs_intervention`` is correctly surfaced as a non-applied failure
    there without code change.

    ``probe_failed`` — the implementation's L2 fresh-source preflight
    probe rejected the just-committed manifest BEFORE any spawn/cutover
    (GTE-06). The system is unchanged and the caller-side
    ``lifecycle_management_service`` reacts by rolling the on-disk
    manifest bytes back (``_probe_failed_rollback_envelope``); the
    rejection detail rides ``RestartResult.probe``.
    """

    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_INTERVENTION = "needs_intervention"
    PROBE_FAILED = "probe_failed"


@dataclass(frozen=True, slots=True)
class RestartResult:
    """Typed envelope returned by ``restart_with_manifest`` (the lifecycle base verb).

    Attributes:
        status: Terminal status of the restart attempt.
        restart_action_id: Populated when ``status=QUEUED`` for the cloud
            path so the operator can poll a backend-specific status verb
            (``cloud_self_deployment_service::deploy_status``). Empty
            string when no follow-on row exists (synchronous
            ``COMPLETED`` or ``FAILED``, or macOS path where the watchdog
            is local-only).
        message: Human-readable detail surfaced to the operator and in
            audit trails.
        reason: Echoes the operator-supplied ``reason`` argument for
            audit correlation.
        expected_etag: The ETag the caller asserted when invoking the
            verb (CAS lock; see lifecycle_interfaces_design §13.2).
            Returned verbatim so the response carries the
            optimistic-lock cursor the caller can re-use for retry
            sequencing.
        dry_run: Echoes the ``dry_run`` argument. Implementations honor
            ``dry_run=True`` by planning + reporting without mutating
            any AWS / local-process state.
        reason_code: A stable, machine-readable token classifying the
            outcome (paired with ``status``) so callers can branch on a
            specific cause without parsing ``message`` prose — e.g.
            ``no_previous`` / ``etag_mismatch`` (``failed``),
            ``rollback_target_unbootable`` / ``compensation_incomplete``
            (``needs_intervention``). The vocabulary is implementation-owned
            (the macOS plugin's ``RestartReasonCode``); this field is a plain
            ``str`` so the core type stays backend-agnostic. Defaults to the
            empty string — a trailing default keeps every existing
            (positional or keyword) construction valid (the field was added
            additively for the durable-rollback verb).
        probe: The GTE-06 L2 probe payload. On ``status=PROBE_FAILED`` it
            carries the rejection detail (``failing_step`` /
            ``error_class`` / ``detail`` / ``failures``); on a successful
            probed cutover (``QUEUED``) it carries the success evidence
            (``{ok: true, duration_ms, release_id}`` — Q5, read by the
            live verify sequence). ``None`` on implementations without a
            probe (cloud) and on paths that never reached it. Trailing
            default per the ``reason_code`` additive-field precedent.
    """

    status: RestartStatus
    restart_action_id: str
    message: str
    reason: str
    expected_etag: str
    dry_run: bool
    reason_code: str = ""
    probe: dict[str, object] | None = None


class TeardownStatus(StrEnum):
    """Terminal status returned by ``teardown_solet``.

    ``completed`` — every phase ran to its expected terminal state for
    this attempt (resources gone where applicable, scheduled-deletion
    state set where applicable). ``partial`` — one or more phase steps
    surfaced a recoverable error (e.g. a resource in an unexpected
    state from concurrent operator action); the per-step
    ``StepResult.status`` records which steps deviated. ``failed`` —
    teardown could not begin (config/readiness/role assumption failed)
    or hit an unrecoverable error before reaching Phase D's audit
    write. ``dry_run`` — ``dry_run=True`` returned the planned step
    list without executing any state-changing AWS calls.
    """

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class TeardownResult:
    """Typed envelope returned by ``teardown_solet``.

    Carries the identifiers an operator needs to perform AWS-console-side
    recovery (per D17' drop of the platform-side ``cancel_teardown``
    verb): RDS final-snapshot id (restore-from-snapshot key), scheduled
    secret-deletion timestamps (Secrets Manager recovery window), and
    the KMS key-deletion schedule (KMS pending-window cancel key).

    Per workbench/2026-06-02_aws_undertaker_plugin_design.md §10.

    Attributes:
        status: Terminal status of the teardown attempt.
        solet_name: The target solet.
        idempotency_key: 16-hex sha256 derived from
            ``(name, snapshot_data, snapshot_retention_days,
            secret_recovery_days)``; used to tag the snapshot + the
            scheduled-deletion resources for resume-time identification.
        dry_run: Echoes the operator-supplied ``dry_run`` flag.
        steps: Per-step audit records. Each entry has at minimum
            ``step_name`` and ``status`` (e.g. ``completed``,
            ``skipped``, ``dry_run_planned``, ``failed``); per-step
            output detail is implementation-defined.
        snapshots: RDS snapshot identifiers created during Phase C1.
            Operator-recoverable via
            ``aws rds restore-db-instance-from-db-snapshot``.
        snapshot_retention_until: ISO-8601 UTC timestamp at which the
            scheduled ``rds:DeleteDBSnapshot`` follow-on action will
            fire (per design §5 path (a)).
        scheduled_secret_deletions: One entry per secret scheduled for
            deletion in Phase C4. Shape: ``{"secret_id": str,
            "scheduled_deletion_at": "<ISO>"}``. Operator-recoverable
            via ``aws secretsmanager restore-secret`` inside the
            ``secret_recovery_days`` window.
        scheduled_kms_deletion: The per-solet KMS key scheduled
            for deletion in Phase C5. Shape: ``{"key_id": str,
            "scheduled_deletion_at": "<ISO>"}``. Operator-recoverable
            via ``aws kms cancel-key-deletion`` inside the pending
            window.
        teardown_record_path: Filesystem path of the Phase D1 audit
            record (``~/Workspace/<name>/teardown_manifest.json``).
        message: Human-readable detail surfaced to the operator and
            captured in audit trails.
    """

    status: TeardownStatus
    solet_name: str
    idempotency_key: str
    dry_run: bool
    steps: tuple[dict[str, object], ...] = field(default_factory=tuple)
    snapshots: tuple[str, ...] = field(default_factory=tuple)
    snapshot_retention_until: str = ""
    scheduled_secret_deletions: tuple[dict[str, str], ...] = field(default_factory=tuple)
    scheduled_kms_deletion: dict[str, str] = field(default_factory=dict)
    teardown_record_path: str = ""
    message: str = ""


class AdminStatus(StrEnum):
    """Terminal status returned by ``IamProvisioningServiceInterface`` verbs.

    ``success`` — every per-role step reached its expected terminal state
    (created / updated / skipped_already_current). ``partial`` — one or
    more per-role steps surfaced a recoverable error (e.g. an unexpected
    drift the operator must reconcile manually); the per-step
    ``steps[*].status`` records which roles deviated. ``failed`` — the
    verb could not begin (AssumeRole / address-book / config readiness
    failure) or hit an unrecoverable error before any state was changed.
    ``rejected`` — the request was rejected at validation time (e.g.
    unsupported ``policy_template_version``, empty ``role_names_required``)
    BEFORE any AWS calls fired. ``dry_run`` — ``dry_run=True`` returned
    the planned step list without issuing any state-changing IAM calls.

    Per the herky-jerky load semantics (workbench/2026-06-02_aws_account
    _admin_plugin_design.md §5), the admin plugin is structurally absent
    from the process registry except during operator-initiated transient
    cycles; ``rejected`` is the verbal surface for caller-side validation
    failures during such a cycle.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    REJECTED = "rejected"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class AdminResult:
    """Typed envelope returned by ``IamProvisioningServiceInterface`` verbs.

    Per workbench/2026-06-02_aws_account_admin_plugin_design.md §3.1 + §6.

    Attributes:
        status: Terminal status of the admin verb attempt.
        birther_name: Echo of the operator-supplied birther name.
        idempotency_key: 16-hex sha256 derived from
            ``(birther_name, sorted(role_names_required), policy_template_version)``
            for ``provision_lifecycle_roles``; for ``audit_lifecycle_roles``
            an empty string is returned (audit is read-only and idempotent
            without tagging).
        dry_run: Echoes the operator-supplied ``dry_run`` flag.
        verb: Short identifier of which interface verb produced this result
            (e.g. ``provision_lifecycle_roles``). Lets callers branch on
            the same result type without unpacking per-verb fields by
            shape.
        steps: Per-role audit records. Each entry has at minimum
            ``role`` (``midwife`` or ``undertaker``), ``status`` (e.g.
            ``created``, ``updated_to_current``, ``skipped_already_current``,
            ``dry_run_planned``, ``failed``, ``drift_detected``), and a
            ``role_arn`` populated when the role exists or was created.
            Per-step output detail is implementation-defined.
        roles_provisioned: Tuple of role ARNs the verb created or
            updated this attempt. Empty when no state changed (audit,
            dry_run, fully-current re-runs).
        policy_template_version: Echo of the operator-supplied policy
            template version the admin verb planned against. Empty string
            for ``audit_lifecycle_roles`` (audit reports the version each
            role currently carries via per-step output, not a single
            top-level value).
        message: Human-readable detail surfaced to the operator and
            captured in audit trails.
    """

    status: AdminStatus
    birther_name: str
    idempotency_key: str
    dry_run: bool
    verb: str
    steps: tuple[dict[str, object], ...] = field(default_factory=tuple)
    roles_provisioned: tuple[str, ...] = field(default_factory=tuple)
    policy_template_version: str = ""
    message: str = ""


class BirthStatus(StrEnum):
    """Terminal status returned by ``MidwifeServiceInterface.birth_solet``.

    ``success`` — every step of the canonical AWS provisioning sequence
    reached its expected terminal state (created / updated /
    skipped_already_current). The newborn is reachable at the recorded
    endpoint. ``partial`` — one or more steps surfaced a recoverable
    error (e.g. a resource in an unexpected state from concurrent
    operator action); the per-step ``steps[*].status`` records which
    steps deviated. The midwife is re-runnable with the same
    ``name`` + ``environment_config`` to resume from the last-completed
    step (idempotent per ``MidwifeAttempt=<idempotency_key>`` tag).
    ``failed`` — the verb could not begin (config readiness /
    AssumeRole / address-book seam failure) or hit an unrecoverable
    error before any AWS state was changed. ``dry_run`` —
    ``dry_run=True`` returned the planned step list without issuing
    any state-changing AWS calls.

    Per the v2 lifecycle interfaces design (Step 1 §15), the matrix's
    four lifecycle envelopes (Restart / Birth / Teardown / Admin)
    share the same StrEnum + frozen-dataclass shape so callers can
    branch on ``status`` uniformly.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class BirthResult:
    """Typed envelope returned by ``MidwifeServiceInterface.birth_solet``.

    Per the Step 4 ``aws_midwife_plugin`` design (§4.2) carrying the
    Step 1 v2 §15 inventory: the operator needs the new solet's
    endpoint, the audit-trail manifest path, the IAM roles created
    (so subsequent `aws_account_admin_plugin` reconciliation has the
    inventory), and the RDS + KMS identifiers (used by
    `aws_undertaker_plugin` discovery + by manual recovery during the
    snapshot / secret / KMS windows).

    Attributes:
        status: Terminal status of the birth attempt.
        solet_name: Echo of the operator-supplied newborn name.
        idempotency_key: 16-hex sha256 derived from
            ``(name, profile_template, sorted(environment_config))``
            and stamped onto every AWS resource as
            ``MidwifeAttempt=<idempotency_key>`` so a resume can
            identify its own prior work via tag.
        dry_run: Echoes the operator-supplied ``dry_run`` flag.
        steps: Per-step audit records. Each entry has at minimum
            ``step_name`` and ``status`` (e.g. ``completed``,
            ``skipped_already_current``, ``dry_run_planned``,
            ``failed``); per-step output detail is
            implementation-defined.
        new_solet_endpoint: HTTPS URL the newborn is reachable
            at once the ALB listener rule + Route53 record + ACM cert
            are live (e.g. ``https://<name>.acute-focus.com``). Empty
            string until ``status=SUCCESS``.
        manifest_path: Filesystem path of the Phase D audit record
            (``~/Workspace/<name>/manifest.json``) capturing every
            ARN/ID for reference, teardown, and audit. The same
            manifest is the ``aws_undertaker_plugin``
            ``resourcegroupstaggingapi`` fallback inventory.
        iam_roles_created: Tuple of role ARNs the midwife created
            this attempt (``<name>-task`` + ``<name>-ecs-task-execution-role``
            + ``<name>-self-deployment`` always; ``<name>-{admin,midwife,undertaker}``
            conditionally per the a-birthed path).
        rds_endpoint: Hostname:port of the newborn's RDS instance
            (``<name>-pg``). Empty until step 6a (RDS-available wait)
            completes.
        kms_key_arn: ARN of the per-newborn CMK
            (``alias/<name>-vault``). Empty until step 2 completes.
        message: Human-readable detail surfaced to the operator and
            captured in audit trails.
    """

    status: BirthStatus
    solet_name: str
    idempotency_key: str
    dry_run: bool
    steps: tuple[dict[str, object], ...] = field(default_factory=tuple)
    new_solet_endpoint: str = ""
    manifest_path: str = ""
    iam_roles_created: tuple[str, ...] = field(default_factory=tuple)
    rds_endpoint: str = ""
    kms_key_arn: str = ""
    message: str = ""


class AutostartStatus(StrEnum):
    """Terminal status returned by the autostart verbs on macos_self_deployment_plugin.

    ``success`` — install_autostart or uninstall_autostart reached the
    intended state (plist on disk + loaded for install; absent for
    uninstall). ``not_installed`` — status_autostart found no plist on
    disk; or uninstall_autostart was called against an already-absent
    plist (still terminally successful, but the state distinction is
    surfaced for operator visibility). ``installed_not_loaded`` — plist
    on disk but ``launchctl list <label>`` does not know it; typically
    means an operator manually unloaded the LaunchAgent without
    deleting the plist file. ``installed_loaded`` — plist on disk AND
    launchd knows it; the EXPECTED steady state with KeepAlive=false
    (the LaunchAgent fires at next login and exits cleanly after
    handing off to the running solet). ``failed`` — the verb hit
    an error before reaching the target state (filesystem permission,
    launchctl reject, etc.). ``dry_run`` — ``dry_run=True`` returned
    the planned actions without writing the plist or invoking
    launchctl.
    """

    SUCCESS = "success"
    NOT_INSTALLED = "not_installed"
    INSTALLED_NOT_LOADED = "installed_not_loaded"
    INSTALLED_LOADED = "installed_loaded"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class AutostartResult:
    """Typed envelope returned by macos_self_deployment_plugin's autostart verbs.

    Attributes:
        status: Terminal status. See :class:`AutostartStatus` for the
            distinction between ``installed_loaded`` (expected steady
            state under ``KeepAlive=false``) and the other states.
        verb: Short identifier of which verb produced this result
            (``install_autostart``, ``uninstall_autostart``,
            ``status_autostart``). Lets callers branch without
            unpacking shape.
        solet_name: The target solet.
        label: macOS LaunchAgent label (e.g.
            ``local.solet.example``). Operator-neutral scheme
            (``local.solet.<name>``) avoids hardcoding any
            organization prefix.
        plist_path: Resolved path of the LaunchAgent plist (e.g.
            ``~/Library/LaunchAgents/local.solet.example.plist``).
            Returned even when no plist exists on disk so the operator
            knows where it WOULD have been written.
        prior_state: The state observed BEFORE the verb mutated
            anything. For install: ``absent`` / ``present_already_current``
            / ``present_but_stale`` / ``present_not_loaded``. For
            uninstall: ``absent`` / ``present_not_loaded`` /
            ``present_and_loaded``. For status: same vocabulary as
            ``status`` (echoes the observed steady state).
        last_run_at: ISO-8601 UTC of the LaunchAgent's last invocation
            per ``launchctl list``. Empty string when launchctl has no
            ``LastExitStatus`` record (never run yet, OR plist absent).
        dry_run: Echoes the ``dry_run`` argument.
        message: Human-readable detail surfaced to the operator and
            captured in audit trails.
    """

    status: AutostartStatus
    verb: str
    solet_name: str
    label: str
    plist_path: str
    prior_state: str
    last_run_at: str
    dry_run: bool
    message: str


class ImageBuildStatus(StrEnum):
    """Terminal status returned by ``aws_midwife_plugin::build_and_push``.

    ``success`` — CodeBuild reached SUCCEEDED, image pushed to ECR,
    digest fetched. ``build_failed`` — CodeBuild reached FAILED /
    FAULT / TIMED_OUT / STOPPED; ``build_log_pointer`` carries the
    CloudWatch Logs stream for diagnosis. ``staging_failed`` — local
    filesystem error during the tar / upload phase, before CodeBuild
    was triggered. ``rejected`` — caller-side validation failed
    (unknown profile_template, malformed allowlist, missing
    env_config keys) BEFORE any S3 / CodeBuild call fired.
    ``dry_run`` — ``dry_run=True`` reported the planned tar + upload
    + start_build envelope without mutating S3 or starting CodeBuild.

    Per the v2 lifecycle interfaces design (Step 1 §15), the matrix's
    lifecycle envelopes share the same StrEnum + frozen-dataclass
    shape so callers can branch on ``status`` uniformly.
    """

    SUCCESS = "success"
    BUILD_FAILED = "build_failed"
    STAGING_FAILED = "staging_failed"
    REJECTED = "rejected"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class ImageBuildResult:
    """Typed envelope returned by ``aws_midwife_plugin::build_and_push``.

    Per workbench/2026-06-04_aws_midwife_running_ananta_gap_design.md
    Piece 2 §2.3: the verb stages the birther's local platform tree
    (filtered to the newborn's profile_template plugin allowlist),
    uploads to S3, triggers CodeBuild, polls until terminal status,
    fetches the resulting ECR digest. Verb is sync.

    Attributes:
        status: Terminal status. See :class:`ImageBuildStatus`.
        image_uri: ECR URI of the pushed image
            (``<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>``).
            Empty until ``status=SUCCESS``.
        image_digest: SHA256 digest from
            ``ecr.describe_images`` after CodeBuild's push completes.
            Empty until ``status=SUCCESS``.
        build_id: CodeBuild build id (``<project>:<uuid>``); load-bearing
            for log lookups + idempotency. Empty when staging failed
            before CodeBuild was triggered.
        build_run_id: 16-hex sha256 prefix derived from
            ``(newborn_name, image_tag, profile_template, build_start_iso)``.
            Plugin-side audit token. Tags the staged S3 tarball + the
            CodeBuild run via the ``ImageBuildAttempt`` tag so a
            resume can identify its own prior work.
        duration_seconds: Wall-clock seconds from
            ``start_build`` to terminal status. 0.0 for dry_run /
            rejected / staging_failed.
        build_log_pointer: CloudWatch Logs pointer
            (``<log_group>:<log_stream>``) for the CodeBuild run.
            Operator uses ``aws logs get-log-events --log-group-name
            <log_group> --log-stream-name <log_stream>`` to read the
            full build trace. Empty when CodeBuild was never started.
        newborn_name: Echo of the input newborn name.
        image_tag: Echo of the input image tag.
        profile_template: Echo of the resolved profile template name
            (default ``cloud``).
        dry_run: Echoes the ``dry_run`` argument.
        message: Human-readable detail surfaced to the operator and
            captured in audit trails.
    """

    status: ImageBuildStatus
    image_uri: str
    image_digest: str
    build_id: str
    build_run_id: str
    duration_seconds: float
    build_log_pointer: str
    newborn_name: str
    image_tag: str
    profile_template: str
    dry_run: bool
    message: str


class StopSelfStatus(StrEnum):
    """Terminal status returned by ``stop_self``.

    ``success`` — the solet's primary serving process has been
    asked to terminate. On macOS, the detached watchdog has been
    spawned and the drain sentinel is on disk; the solet child receives
    SIGTERM shortly after the verb returns. On cloud, ECS
    ``UpdateService`` set ``DesiredCount=0`` and ``DescribeServices``
    polling confirmed ``runningCount=0``. ``already_stopped`` — the
    solet was already in the stopped state (cloud: desiredCount
    already 0 at the configured service; macOS: sentinel already on
    disk from a prior stop_self that the operator never cleaned up).
    Idempotent return so re-invocation is safe. ``failed`` — the verb
    could not complete (cloud: ECS call rejected, polling timed out;
    macOS: filesystem error writing the sentinel, watchdog spawn
    failed). ``dry_run`` — ``dry_run=True`` returned the planned
    actions without writing the sentinel or spawning the watchdog or
    calling ECS.
    """

    SUCCESS = "success"
    ALREADY_STOPPED = "already_stopped"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class StopSelfResult:
    """Typed envelope returned by ``SelfDeploymentServiceInterface.stop_self``.

    Per workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md
    §6 Slice 4.5 (operator-scoped stop_self verb): leaves all infra in
    place, sets only the live serving capacity to zero. Cloud
    operators bring the solet back with
    ``ecs update-service --desired-count 1``; macOS operators run
    ``./launch.py``, which scrubs the drain sentinel from
    ``_cleanup_stale_runtime_files`` and re-spawns the solet.

    Attributes:
        status: Terminal status. See :class:`StopSelfStatus`.
        reason: Echo of the operator-supplied reason argument for audit
            correlation.
        duration_seconds: Wall-clock seconds the verb spent. For macOS
            this is sub-second (the watchdog runs out-of-process after
            the verb returns); for cloud this can be minutes (the
            describe_services polling loop waits for runningCount=0).
            0.0 for dry_run.
        stopped_at: ISO-8601 UTC timestamp of confirmed-stopped on
            cloud (runningCount=0). On macOS this is the moment the
            watchdog was spawned (the SIGTERM fires shortly after).
            Empty string for ``DRY_RUN`` / ``FAILED`` before any state
            mutation.
        backend_action_id: Backend-specific audit identifier. macOS:
            spawned watchdog pid as a string (operator can ``ps`` to
            confirm the watchdog is queued). Cloud: ECS service ARN.
            Empty when no backend action fired.
        dry_run: Echoes the ``dry_run`` argument.
        message: Human-readable detail surfaced to the operator and
            captured in audit trails.
    """

    status: StopSelfStatus
    reason: str
    duration_seconds: float
    stopped_at: str
    backend_action_id: str
    dry_run: bool
    message: str


__all__ = [
    "AdminResult",
    "AdminStatus",
    "AutostartResult",
    "AutostartStatus",
    "BirthResult",
    "BirthStatus",
    "ImageBuildResult",
    "ImageBuildStatus",
    "RestartResult",
    "RestartStatus",
    "StopSelfResult",
    "StopSelfStatus",
    "TeardownResult",
    "TeardownStatus",
]
