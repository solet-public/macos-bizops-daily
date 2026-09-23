"""Plugin entry point — `GithubMidwifePlugin`.

Implements `MidwifeServiceInterface` for the GitHub-genesis source axis
(mirrors `macos_midwife_plugin.plugin` / `aws_midwife_plugin.plugin` so
callers branching on the interface contract see identical shapes across
substrates — see the GitHub/macOS/AWS midwife overview articles; there is no
exclusive `midwife_service` binding.

BIRTH-ONLY since the 2026-07-20 split ruling: the seed FACTORY verbs
(`assemble_seed`/`validate_and_seal_seed_bundle`/`publish_seed`) moved to the
new origin-only `seed_factory_plugin`. This plugin is the BIRTH SPINE — it ships
in every seed so a downloaded seed can come alive, and it exposes exactly one
EDGE verb, `birth_solet`. The bootstrap handoff target
(`python -m github_midwife_plugin.genesis`) is unchanged.

Dual-use (design doc §2): the SAME `birth_solet` verb serves two
callers —
  1. `genesis.py`'s CLI entrypoint calls `run_genesis()` directly
     (no EDGE dispatch) against the CURRENT clone (`bootstrap.py`'s
     handoff case).
  2. This EDGE verb, callable from an ALREADY-RUNNING solet, completes
     genesis against an EXISTING clone at `environment_config["target"]`
     (a seed folder from `assemble_seed`, or any clone). Acquisition mode --
     cloning a pinned upstream into an absent/empty target -- was RETIRED
     2026-07-18; the Seed Factory replaces it (assemble a seed, then birth
     it). `provision_venv=True` selects the §7 birth variant that builds the
     source-only seed folder's `.venv` explicitly before genesis; the
     `mint_and_birth_local` joseki chains `assemble_seed` -> `birth_solet`
     that way.

Per-solet credential isolation (operator override, 2026-07-12): each
solet has its OWN non-superuser role (name = SOLET_NAME) and its
OWN password; no credential ever crosses a solet namespace. Verb-mode
genesis runs in the PARENT's process, but the credential must land in the
NEWBORN's own Keychain namespace, so it does NOT call
`credential_seed.seed_db_password` in-process (that would bind the PARENT's
namespace). Instead it builds a `credential_provisioner` closure that: (1)
pre-seed scram-verifies the newborn's db, (2) runs
`venv_provision.seed_newborn_credential` -- a subprocess in the newborn's OWN
venv (SOLET_NAME=<newborn>) that self-seeds the newborn's OWN role via
the same `seed_db_password` path the CLI uses -- and (3) post-seed proves the
newborn's role cannot reach the parent's db (isolation self-proof). There is
no parent-provisions-child credential copy. Only the CLI path
(`genesis.main`, fresh-machine self-seed, running in the newborn's OWN
process) calls `seed_db_password` directly.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from ananta.interfaces.lifecycle_result_types import (
    BirthResult,
    BirthStatus,
    ImageBuildResult,
    ImageBuildStatus,
)
from ananta.interfaces.midwife_service_interface import MidwifeServiceInterface

from . import venv_provision
from .constants import REQUIRED_CLONE_MARKERS
from .genesis import GenesisError, resolve_profile_name, run_genesis

_PLUGIN_NAME = "github_midwife_plugin"

# ── Parameter metadata (verb signature) ─────────────────────────────

_NAME_PARAM = ParameterMetadata(
    description="Lowercase first-name of the newborn (regex [a-z][a-z0-9_-]{1,62}).",
    required=True,
    type=ParameterType.STRING,
)
_PROFILE_TEMPLATE_PARAM = ParameterMetadata(
    description=(
        "Profile template the newborn boots under (resolves against this "
        "plugin's knowledge_base/profile_templates/<name>.yaml, e.g. "
        "'macos-free-solet')."
    ),
    required=True,
    type=ParameterType.STRING,
)
_ENVIRONMENT_CONFIG_PARAM = ParameterMetadata(
    description=(
        "Config dict. Required key: 'target' (str) -- an EXISTING, "
        "fully-formed platform clone (ananta/ + plugins/), e.g. a seed folder "
        "produced by assemble_seed. Genesis runs against it directly. "
        "Acquisition mode (cloning a pinned upstream into an absent/empty "
        "target) was RETIRED 2026-07-18 -- the Seed Factory replaces it, so an "
        "absent/empty target raises a validation error (assemble a seed into "
        "it first). The newborn SELF-SEEDS its own per-solet role's "
        "credential in its own Keychain namespace via a subprocess in its own "
        "venv -- no credential ever crosses a solet namespace (per-role "
        "isolation, 2026-07-12). A non-empty target that is not a valid clone "
        "raises a validation error (refuses to guess or clobber). Standard mode "
        "requires a pre-existing <target>/.venv; pass provision_venv=True (the "
        "local-birth-chain variant) to build it first."
    ),
    required=True,
    type=ParameterType.OBJECT,
)
_DRY_RUN_PARAM = ParameterMetadata(
    description=(
        "When true, returns a planned BirthResult (status='dry_run', message "
        "names the resolved existing-clone target and whether the venv would be "
        "provisioned) without mutating filesystem, Postgres, venv, or launchd "
        "state."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)
_PROVISION_VENV_PARAM = ParameterMetadata(
    description=(
        "When True, birth runs create_venv_and_install_seed EXPLICITLY and "
        "UNCONDITIONALLY before genesis -- the §7 birth VARIANT for the "
        "local-birth chain (the mint_and_birth_local joseki), where target is a "
        "source-only seed folder with no .venv. When False (default) the "
        "standard existing-clone contract is unchanged: genesis skips venv/seed "
        "and a missing <target>/.venv fails loud downstream."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)


def _birth_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="birth_solet outcome envelope.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "solet_name": ParameterMetadata(type=ParameterType.STRING, description="."),
            "idempotency_key": ParameterMetadata(type=ParameterType.STRING, description="."),
            "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN, description="."),
            "steps": ParameterMetadata(type=ParameterType.LIST, description="."),
            "new_solet_endpoint": ParameterMetadata(type=ParameterType.STRING, description="."),
            "manifest_path": ParameterMetadata(type=ParameterType.STRING, description="."),
            "iam_roles_created": ParameterMetadata(type=ParameterType.LIST, description="."),
            "rds_endpoint": ParameterMetadata(type=ParameterType.STRING, description="."),
            "kms_key_arn": ParameterMetadata(type=ParameterType.STRING, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _compute_idempotency_key(
    name: str, profile_template: str, environment_config: dict[str, Any],
) -> str:
    payload = f"{name}|{profile_template}|{sorted(environment_config.items())}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


class GithubMidwifePlugin(PluginBase, EdgeProcessProvider, MidwifeServiceInterface):
    """GitHub-genesis midwife — births a solet from a public clone."""

    name: str = _PLUGIN_NAME

    service_interfaces: ClassVar[tuple[type, ...]] = (MidwifeServiceInterface,)
    supported_interface_versions: ClassVar[dict[type, str]] = {
        MidwifeServiceInterface: MidwifeServiceInterface.INTERFACE_VERSION,
    }

    def __init__(self) -> None:
        super().__init__()
        self.name = _PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)

    # ── Lifecycle ────────────────────────────────────────────────────

    def get_default_config(self) -> dict[str, Any]:
        # No plugin config: the seed source is the committed ref `assemble_seed`
        # reads (never a caller-supplied clone URL), and acquisition-mode
        # clone-of-pinned-upstream was retired 2026-07-18.
        return {}

    def prepare_for_readiness(self) -> None:
        """Trivially ready -- the birth verb validates its own prerequisites
        at invocation time (fast-fail-development-strategy), matching
        `macos_midwife_plugin`'s reasoning: no external services needed just to
        load, and this plugin holds no config.
        """
        self.logger.info("%s: ready.", self.name)
        self.set_ready()

    # ── EdgeProcessProvider ──────────────────────────────────────────

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        # One entry per EDGE-category @platform_process method. birth does not
        # return a blob_key, so field_sensitivities are empty (§EDGE trap: a
        # missing entry FATALs the registry build). birth is not retryable
        # (side-effecting genesis). The seed-factory verbs moved to
        # seed_factory_plugin (2026-07-20 split) -- this plugin is birth-only.
        return {
            "birth_solet": EdgeProcessDefinition(
                name="birth_solet",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    # ── Interface verb ───────────────────────────────────────────────

    def birth_solet(
        self,
        *,
        name: str,
        profile_template: str,
        environment_config: dict[str, Any],
        dry_run: bool = False,
        provision_venv: bool = False,
    ) -> BirthResult:
        """Run genesis against `environment_config["target"]`, which MUST be an
        existing, fully-formed platform clone (a seed folder from `assemble_seed`,
        or any clone). Acquisition mode -- cloning a pinned upstream into an
        absent/empty `target` -- was RETIRED 2026-07-18; the Seed Factory
        replaces it (assemble a seed first, then birth it).

        `provision_venv` selects the §7 birth VARIANT for the local-birth chain
        (the `mint_and_birth_local` joseki): when True,
        `create_venv_and_install_seed` runs EXPLICITLY and UNCONDITIONALLY before
        genesis (a seed folder ships source-only, no `.venv`). When False
        (default) the standard existing-clone contract is UNCHANGED -- genesis
        skips venv/seed and a missing `.venv` fails loud downstream. A `target`
        that is absent/empty or not a valid clone raises `ValueError` (fail loud,
        never guess or clobber).
        """
        target_raw = environment_config.get("target")
        if not target_raw:
            raise ValueError(
                "github_midwife_plugin.birth_solet requires "
                "environment_config['target'] (an existing seed/clone directory "
                "-- assemble a seed into it first if it does not exist yet)."
            )
        target = Path(str(target_raw)).expanduser().resolve()
        self._require_existing_clone(target)
        idempotency_key = _compute_idempotency_key(name, profile_template, environment_config)
        try:
            resolved_profile = resolve_profile_name(target, profile_template)
        except GenesisError as exc:
            return BirthResult(
                status=BirthStatus.FAILED,
                solet_name=name,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
                steps=(),
                new_solet_endpoint="",
                manifest_path="",
                iam_roles_created=(),
                rds_endpoint="",
                kms_key_arn="",
                message=str(exc),
            )

        if dry_run:
            return BirthResult(
                status=BirthStatus.DRY_RUN,
                solet_name=name,
                idempotency_key=idempotency_key,
                dry_run=True,
                steps=(),
                new_solet_endpoint="",
                manifest_path="",
                iam_roles_created=(),
                rds_endpoint="",
                kms_key_arn="",
                message=(
                    f"dry_run=True; would run genesis against existing clone {target}"
                    + (" (provisioning the venv first)" if provision_venv else "")
                ),
            )

        try:
            result = self._run_genesis_against_clone(
                name,
                target,
                resolved_profile,
                provision_venv,
            )
        except (GenesisError, venv_provision.VerbModeProvisionError) as exc:
            return BirthResult(
                status=BirthStatus.FAILED,
                solet_name=name,
                idempotency_key=idempotency_key,
                dry_run=False,
                steps=(),
                new_solet_endpoint="",
                manifest_path="",
                iam_roles_created=(),
                rds_endpoint="",
                kms_key_arn="",
                message=str(exc),
            )

        steps = tuple(result["steps"])
        manifest_step = next((s for s in steps if s.get("step_name") == "write_manifest_marker"), {})
        return BirthResult(
            status=BirthStatus.SUCCESS,
            solet_name=name,
            idempotency_key=idempotency_key,
            dry_run=False,
            steps=steps,
            new_solet_endpoint="",
            manifest_path=str(manifest_step.get("manifest_path", "")),
            iam_roles_created=(),
            rds_endpoint="",
            kms_key_arn="",
            message=f"genesis complete for {name!r} at {target}",
        )

    def _require_existing_clone(self, target: Path) -> None:
        """Fail loud unless `target` is a fully-formed platform clone (ananta/ +
        plugins/ both present — knowledge_bases/ is NOT a marker; a seed-born
        clone ships without it, see `constants.REQUIRED_CLONE_MARKERS`).

        An absent/empty `target` is NOT birthable: acquisition-mode
        clone-of-pinned-upstream was retired 2026-07-18 (the Seed Factory
        replaces it). Assemble a seed into `target` first (`assemble_seed` /
        the `mint_and_birth_local` joseki). A non-empty, non-clone-shaped
        directory is refused rather than guessed at or clobbered (genesis
        would otherwise fail confusingly deep inside the spine instead of
        here, at the door).
        """
        if venv_provision.probe_target_absent_or_empty(target):
            raise ValueError(
                f"{target} is absent or empty -- birth_solet no longer "
                "clones a pinned upstream (acquisition mode retired 2026-07-18); "
                "assemble a seed into it first (assemble_seed / mint_and_birth_local)."
            )
        if not all((target / marker).is_dir() for marker in REQUIRED_CLONE_MARKERS):
            raise ValueError(
                f"{target} is not a valid platform clone (missing one of "
                f"{REQUIRED_CLONE_MARKERS}) -- refusing to guess or clobber."
            )

    def _run_genesis_against_clone(
        self, name: str, target: Path, profile_template: str, provision_venv: bool,
    ) -> dict[str, Any]:
        if provision_venv:
            # §7 birth VARIANT (the mint_and_birth_local local-birth chain): a
            # seed folder ships source-only (no `.venv`). This is the EXPLICIT,
            # UNCONDITIONAL venv placement the design mandates -- an opt-in that
            # ALWAYS provisions, never a lazy create-if-absent on the standard path.
            newborn_venv = venv_provision.create_venv_and_install_seed(target, run=subprocess.run)
        else:
            # Standard existing-clone mode (UNCHANGED, §7): the venv MUST
            # pre-exist (`install_profile_allowlist` fail-louds without it).
            # Genesis SKIPS venv/seed here by contract.
            newborn_venv = target / ".venv"

        # Per-solet credential isolation (operator override, 2026-07-12):
        # the newborn seeds its OWN role in its OWN Keychain namespace -- no
        # credential crosses a namespace, no shared role. Verb-mode genesis runs
        # in the PARENT's process, so the seed runs as a subprocess in the
        # newborn's OWN venv (SOLET_NAME=<newborn>). The sibling database
        # the newborn must prove it is isolated FROM is the parent's own db ==
        # the parent's SOLET_NAME (this verb runs in the parent's process).
        parent_db = os.environ.get("SOLET_NAME", "").strip()
        if not parent_db:
            raise GenesisError(
                "SOLET_NAME is not set in the birthing (parent) process -- "
                "cannot determine the sibling database the newborn must prove it "
                "is isolated from."
            )

        def _credential_provisioner() -> None:
            # The newborn's OWN database + pgvector extension + its own role +
            # localhost scram gate are created by WIZARD STEP 1 (agent-run,
            # pre-launch); genesis ASSUMES them present. Order (Architect C3,
            # 2026-07-12): (1) verify the one INVISIBLE failure mode -- an
            # ungated (passwordless) db -- fail-loud BEFORE seeding; (2) the
            # newborn self-seeds its OWN role via its own venv subprocess; (3)
            # that same subprocess proves the newborn's role cannot reach the
            # parent's db (post-seed isolation self-proof). Runs after
            # validate_name, before first boot.
            venv_provision.verify_newborn_db_scram_gated(name, run=subprocess.run)
            venv_provision.seed_newborn_credential(name, newborn_venv, parent_db, run=subprocess.run)

        return run_genesis(
            name=name, clone_root=target, profile_name=profile_template,
            credential_provisioner=_credential_provisioner,
        )

    def build_and_push(
        self,
        *,
        newborn_name: str,
        image_tag: str,
        profile_template: str = "cloud",
        dry_run: bool = False,
    ) -> ImageBuildResult:
        """Not supported — a genesis clone runs in-place; there is no
        container image to build. Matches `macos_midwife_plugin`'s
        REJECTED stub for the same reason (local, non-containerized
        birth). Not registered as a `@platform_process`.
        """
        return ImageBuildResult(
            status=ImageBuildStatus.REJECTED,
            image_uri="",
            image_digest="",
            build_id="",
            build_run_id="",
            duration_seconds=0.0,
            build_log_pointer="",
            newborn_name=newborn_name,
            image_tag=image_tag,
            profile_template=profile_template,
            dry_run=dry_run,
            message="not_supported: genesis clones run in-place; no container image is produced",
        )

    # ── Action-method wrapper ────────────────────────────────────────

    @platform_process(
        name="birth_solet",
        context_handling=ContextHandling.NONE,
        parameters={
            "name": _NAME_PARAM,
            "profile_template": _PROFILE_TEMPLATE_PARAM,
            "environment_config": _ENVIRONMENT_CONFIG_PARAM,
            "dry_run": _DRY_RUN_PARAM,
            "provision_venv": _PROVISION_VENV_PARAM,
        },
        output_type="object",
        output_description="Birth envelope.",
        return_value_schema=_birth_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def birth_solet_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        validation_error = self._validate_action_params(params)
        if validation_error is not None:
            code, message = validation_error
            return self._error_envelope(code, message)

        try:
            result = self.birth_solet(
                name=str(params.get("name") or ""),
                profile_template=str(params.get("profile_template") or ""),
                environment_config=self._coerce_environment_config(params),
                dry_run=bool(params.get("dry_run") or False),
                provision_venv=bool(params.get("provision_venv") or False),
            )
        except ValueError as err:
            return self._error_envelope("invalid_environment_config", str(err))
        except Exception as err:  # noqa: BLE001 -- return structured failure
            self.logger.exception("birth_solet crashed")
            return self._error_envelope("birth_solet_crashed", str(err))
        return self._success_envelope(self._birth_result_to_dict(result))

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _validate_action_params(params: dict[str, Any]) -> tuple[str, str] | None:
        if not params.get("name"):
            return "missing_name", "birth_solet requires 'name'."
        if not params.get("profile_template"):
            return "missing_profile_template", "birth_solet requires 'profile_template'."
        return None

    @staticmethod
    def _coerce_environment_config(params: dict[str, Any]) -> dict[str, Any]:
        env_raw = params.get("environment_config") or {}
        return dict(env_raw) if isinstance(env_raw, dict) else {}

    @staticmethod
    def _birth_result_to_dict(result: BirthResult) -> dict[str, Any]:
        return {
            "status": result.status.value,
            "solet_name": result.solet_name,
            "idempotency_key": result.idempotency_key,
            "dry_run": result.dry_run,
            "steps": list(result.steps),
            "new_solet_endpoint": result.new_solet_endpoint,
            "manifest_path": result.manifest_path,
            "iam_roles_created": list(result.iam_roles_created),
            "rds_endpoint": result.rds_endpoint,
            "kms_key_arn": result.kms_key_arn,
            "message": result.message,
        }

    def _success_envelope(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _error_envelope(self, code: str, message: str) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message, "details": {}},
            "timestamp": datetime.now(UTC).isoformat(),
        }
