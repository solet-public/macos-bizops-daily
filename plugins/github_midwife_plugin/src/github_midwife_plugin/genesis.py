"""Layer 1 entrypoint — `python -m github_midwife_plugin.genesis`.

Invoked by `bootstrap.py`'s handoff step (Layer 0 -> Layer 1). Composes
Slices B/C/D/F into the genesis flow (design doc §2 Layer 1
responsibilities, §3 end-to-end flow):
  1. profile-driven allowlist install (Slice B) -- fail-loud.
  2. the 6-step spine: validate_name -> resolve_target ->
     materialize_configs -> seed_root_manifest ->
     materialize_kb_symlinks -> write_manifest_marker (Slice D).
  3. seed the scram `db_password` (Slice C).
  4. install autostart (Slice F).
  5. install the blue-green router if the newborn is blue-green-capable
     (macos_self_deployment_plugin in the profile allowlist) — SEED-06;
     free-tier newborns skip it (single-color by design).

Simplification flagged for review: design doc §2 Layer 1 lists "First
launch" (item 4) as a step separate from "install autostart" (item 5).
This module does NOT separately spawn-and-kill a foreground
verification launch: `launchctl load -w` on a `RunAtLoad=true` job
starts it immediately (not just at next login) per macOS launchd
semantics, so `install_autostart` (step 4 here) performs the actual
first launch as a side effect. Adding a separate synchronous
spawn/poll-readiness/terminate dance felt like real complexity risk on
a boot-path file without a concrete readiness signal to poll against;
this simpler shape is the v1 cut.

No interactive prompting anywhere in this module: this process is
invoked via a captured-output subprocess (`bootstrap.py`'s handoff), so
stdin is not usable for free-text input. `SOLET_NAME` is read from
the environment -- the driving agent obtains the name from the user in
conversation (build spec §10.1, Layer -1) BEFORE ever invoking
`bootstrap.py`, and sets `SOLET_NAME` for the whole
`bootstrap.py` -> `genesis.py` chain, mirroring the platform's own
canonical invocation shape (`SOLET_NAME=<name> python -m ananta.cli ...`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from macos_vault_plugin.keychain import MASTER_KEY_ACCOUNT, PerCredentialKeychain, SystemKeychain

from .autostart import AutostartError, AutostartResult, Runner, SimpleAutostartRenderer
from .command_launcher import (
    DEFAULT_BIN_DIR,
    CommandLauncherError,
    CommandLauncherResult,
    install_command_launcher_at_birth,
)
from .credential_seed import CredentialSeedError, seed_db_password
from .git_init import GitInitError, git_init_worktree
from .manifest_marker import build_marker_payload, write_marker
from .profile_install import ProfileInstallError, install_profile_allowlist, load_plugin_allowlist
from .router_install import RouterInstallError, RouterInstallResult, install_router_at_birth
from .steps import GenesisContext, run_steps
from .vault_passphrase_seed import (
    clear_vault_passphrase_stale_check_pending,
    seed_vault_passphrase,
    vault_passphrase_stale_check_is_pending,
)

_DEFAULT_PROFILE_NAME = "macos-free-solet"
_PROFILE_ENV_VAR = "SOLET_PROFILE"
_AUTOSTART_ENV_VAR = "SOLET_AUTOSTART"
_OPERATION_REF_ENV_VAR = "SOLET_OPERATION_REF"
_FULL_GENESIS_OPERATION_REF = "genesis::solet.run"
_AUTOSTART_INSTALL_OPERATION_REF = "genesis::autostart.install"
_STALE_VAULT_PRECHECK_ARGUMENT = "--check-vault-stale-state"
_PROVENANCE_FILENAME = "PROVENANCE.json"
_PROFILE_TEMPLATE_BY_BUNDLE = {
    "macos_free_minimal": "macos-free-solet",
    "macos-bizops": "macos-bizops",
    "macos_samantha": "macos-samantha-solet",
}


class GenesisError(RuntimeError):
    """Raised when the genesis flow cannot complete."""


def _resolve_clone_root() -> Path:
    """Walk up from this file to find the clone root (sibling `ananta/` + `plugins/` dirs).

    Mirrors `macos_midwife_plugin`'s own `_resolve_local_repo_hint`
    pattern -- robust to exactly where in the tree this installed
    module's `__file__` ends up (editable install keeps source in
    place under `<clone>/plugins/github_midwife_plugin/src/...`).
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "ananta").is_dir() and (ancestor / "plugins").is_dir():
            return ancestor
    raise GenesisError(
        f"could not resolve the clone root by walking up from {here} -- "
        "expected an ancestor directory with both ananta/ and plugins/."
    )


def _resolve_kb_root(clone_root: Path) -> Path:
    return clone_root / "plugins" / "github_midwife_plugin" / "knowledge_base"


def _declared_bundle_name(clone_root: Path) -> str | None:
    provenance_path = clone_root / _PROVENANCE_FILENAME
    if not provenance_path.is_file():
        return None
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GenesisError(f"{_PROVENANCE_FILENAME} is not valid JSON: {exc}") from exc
    if not isinstance(provenance, dict):
        raise GenesisError(f"{_PROVENANCE_FILENAME} must contain a JSON object")

    bundle = provenance.get("bundle")
    if not isinstance(bundle, dict):
        return None
    bundle_name = bundle.get("name")
    return bundle_name.strip() if isinstance(bundle_name, str) and bundle_name.strip() else None


def resolve_profile_name(
    clone_root: Path,
    requested_profile: str | None = None,
) -> str:
    """Resolve and validate one profile without mutating the target.

    Older seeds had no provenance and were free-tier by default. Sealed named
    bundles now declare their bundle in ``PROVENANCE.json``; birthing such a seed
    under a different profile silently drops or adds capabilities. A declared
    bundle therefore selects its matching profile and cannot be overridden.
    Provenance-less source trees retain the free default and may select an
    explicit custom template.
    """
    requested = (requested_profile or "").strip()

    bundle_name = _declared_bundle_name(clone_root)
    if bundle_name is None:
        return requested or _DEFAULT_PROFILE_NAME

    profile_name = _PROFILE_TEMPLATE_BY_BUNDLE.get(bundle_name)
    if profile_name is None:
        known = ", ".join(sorted(_PROFILE_TEMPLATE_BY_BUNDLE))
        raise GenesisError(
            f"{_PROVENANCE_FILENAME} declares unknown bundle {bundle_name!r}; "
            f"known bundles: {known}. Declared seed bundles cannot be overridden."
        )
    if requested and requested != profile_name:
        raise GenesisError(
            "seed profile identity mismatch: "
            f"{_PROVENANCE_FILENAME} bundle {bundle_name!r} requires "
            f"profile {profile_name!r}, but {requested!r} was requested"
        )
    return profile_name


def run_genesis(
    *,
    name: str,
    clone_root: Path,
    profile_name: str = _DEFAULT_PROFILE_NAME,
    autostart: bool = True,
    keychain: PerCredentialKeychain | None = None,
    alter_role_password: Callable[[str], None] | None = None,
    role_authenticates: Callable[[str], bool] | None = None,
    role_exists: Callable[[], bool] | None = None,
    credential_provisioner: Callable[[], None] | None = None,
    plist_dir: Path | None = None,
    home_dir: Path | None = None,
    launchctl_run: Runner | None = None,
    router_install_run: Runner | None = None,
    git_init_run: Runner | None = None,
    command_launcher_bin_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the full Layer 1 genesis flow against an EXISTING clone at `clone_root`.

    Shared by the CLI entrypoint (`main()`, always operates on the
    current clone with every override left at its real default) and
    the `birth_solet` EDGE verb (`plugin.py`), which always passes
    an existing clone. Genesis never clones a target itself -- the caller
    provides one (acquisition-mode clone-of-pinned-upstream was retired
    2026-07-18; the Seed Factory assembles the clone first).

    `credential_provisioner`, when supplied, REPLACES the normal
    in-process `seed_db_password` call -- this is verb-mode's hook
    (per-role isolation, operator override 2026-07-12). Verb-mode genesis
    runs in the PARENT's process, but the credential must be seeded in the
    NEWBORN's own Keychain namespace, so `plugin.py`'s closure runs the seed
    as a subprocess in the newborn's OWN venv
    (`venv_provision.seed_newborn_credential`: pre-seed scram-verify -> the
    newborn self-seeds its OWN role via the same `seed_db_password` path the
    CLI uses -> post-seed isolation self-proof). No credential ever crosses a
    solet namespace; there is no parent-provisions-child copy. When
    `None` (the CLI/fresh-machine entrypoint, running in the newborn's OWN
    process), `keychain`/`alter_role_password`/`role_authenticates`/
    `role_exists` pass straight through to `credential_seed.seed_db_password`.

    `plist_dir`/`home_dir`/`launchctl_run` pass straight through to
    `autostart.SimpleAutostartRenderer`. All default to the real
    implementations (real Keychain, real psql, real
    `~/Library/LaunchAgents`, real `~/.ananta/runtime`, real
    `subprocess.run`) -- injectable ONLY so the integration smoke (build
    spec §8) can drive this function end-to-end against a FakeKeychain +
    tmpfs plist_dir + tmpfs home_dir, never touching the real Keychain,
    Postgres, or `~/Library/LaunchAgents`.
    """
    profile_name = resolve_profile_name(clone_root, profile_name)
    kb_root = _resolve_kb_root(clone_root)
    venv_dir = clone_root / ".venv"

    try:
        allowlist = load_plugin_allowlist(
            kb_root / "profile_templates" / f"{profile_name}.yaml"
        )
        install_profile_allowlist(venv_dir=venv_dir, target=clone_root, plugin_allowlist=allowlist)
    except ProfileInstallError as exc:
        raise GenesisError(f"profile-driven allowlist install failed: {exc}") from exc

    ctx = GenesisContext(name=name, profile_name=profile_name, target=clone_root, kb_root=kb_root)
    steps = run_steps(ctx)
    last_step = steps[-1] if steps else {}
    if last_step.get("status") == "failed":
        raise GenesisError(
            f"genesis step machine failed at {last_step.get('step_name')}: {last_step.get('error')}"
        )

    # Post-spine phase records ride the SAME attempt marker as the spine steps
    # (cold-run D4, 2026-07-13): the spine's marker says "spine_complete"; only
    # this function, after the phases below, stamps the final success/failed
    # status -- so a genesis that dies here is never misread as a success.
    phases: list[dict[str, Any]] = []

    def _finalize_marker(status: str) -> None:
        write_marker(clone_root, build_marker_payload(
            name=name, profile_name=profile_name, steps=steps + phases, status=status,
        ))

    try:
        if credential_provisioner is not None:
            credential_provisioner()
        else:
            seed_db_password(
                keychain=keychain,
                alter_role_password=alter_role_password,
                role_authenticates=role_authenticates,
                role_exists=role_exists,
            )
    except CredentialSeedError as exc:
        phases.append({"step_name": "credential_seed", "status": "failed", "error": str(exc)})
        _finalize_marker("failed")
        raise GenesisError(f"credential seed failed: {exc}") from exc
    except RuntimeError as exc:
        # credential_provisioner's own error class (e.g.
        # venv_provision.VerbModeProvisionError) is a RuntimeError
        # subtype, not a CredentialSeedError -- this module does not
        # import venv_provision.py (no dependency in that direction) so it
        # catches generically rather than by specific type.
        phases.append({"step_name": "credential_seed", "status": "failed", "error": str(exc)})
        _finalize_marker("failed")
        raise GenesisError(f"credential provisioning failed: {exc}") from exc
    phases.append({
        "step_name": "credential_seed", "status": "completed",
        "mode": "verb_provisioner" if credential_provisioner is not None else "self_seed",
    })

    # Finding F10 (2026-07-11): seed the vault passphrase FILE before
    # autostart loads the plist. `launchctl load -w` on a RunAtLoad job
    # starts the newborn immediately, and its macos_vault_plugin fail-fasts
    # at readiness ("vault not initialized and no passphrase available")
    # unless the passphrase file already exists. Idempotent write-if-absent;
    # the generated value never touches this function's logs or return value.
    vault_passphrase_created = _run_vault_passphrase_phase(
        clone_root, phases, _finalize_marker,
    )
    phases.append({
        "step_name": "vault_passphrase", "status": "completed",
        "seeded": vault_passphrase_created,
    })

    vault_stale_record = _run_vault_stale_check_phase(
        name, clone_root, keychain, vault_passphrase_created, phases, _finalize_marker,
    )
    phases.append(vault_stale_record)

    autostart_status, autostart_label = _run_autostart_phase(
        name=name,
        clone_root=clone_root,
        enabled=autostart,
        plist_dir=plist_dir,
        home_dir=home_dir,
        launchctl_run=launchctl_run,
        phases=phases,
        finalize_marker=_finalize_marker,
    )

    # SEED-06: install the blue-green router right after the main autostart
    # LaunchAgent (design 2026-07-18 §4 D1, Q1 RULED — genesis auto-step, zero
    # operator action). Conditional on the newborn being blue-green-capable
    # (macos_self_deployment_plugin in the profile allowlist): a free-tier
    # newborn skips cleanly, a capable newborn is fail-loud (booting believing
    # it can blue-green but silently can't is worse than a loud birth failure).
    try:
        router_result = _install_router(name, clone_root, allowlist, router_install_run)
    except GenesisError as exc:
        phases.append({"step_name": "install_router", "status": "failed", "error": str(exc)})
        _finalize_marker("failed")
        raise
    phases.append({
        "step_name": "install_router", "status": "completed",
        "router_status": router_result.status, "reason": router_result.reason,
    })

    # SEED — install the per-solet command launcher on the operator's PATH:
    # the no-MCP-first primary interface (`<name> search/call`). UNCONDITIONAL
    # (every profile ships agent_messaging_plugin's `solet` console script);
    # a bare symlink `<bin_dir>/<name>` -> the newborn's console script, whose
    # identity resolves by install location so it reaches only its own solet.
    # Fail-loud if the console script is missing or a real file blocks the path.
    launcher_result = _run_command_launcher_phase(
        name,
        clone_root,
        command_launcher_bin_dir,
        (home_dir or Path.home()) / ".codex" / "config.toml",
        phases,
        _finalize_marker,
    )
    phases.append({
        "step_name": "install_command_launcher", "status": "completed",
        "launcher_status": launcher_result.status, "launcher_path": launcher_result.launcher_path,
    })

    # Workstream A (2026-07-20): git-init the born tree LAST — every worktree-
    # writing step (materialize_configs / seed_root_manifest / kb_symlinks) has
    # run, so the initial commit captures the fully-materialized tree. FRESH empty
    # history (never the source tree's .git — the "no contaminated history travels" invariant
    # is why .git is never-copied; this starts a clean local one, it does not
    # import). Unblocks platform_dev_surface_plugin readiness in seed-born
    # solets (a plain source tree fail-louds `git rev-parse --is-inside-work-tree`).
    git_init_record = _run_git_init_phase(name, clone_root, git_init_run, phases, _finalize_marker)
    phases.append(git_init_record)

    _finalize_marker("success")

    return {
        "name": name,
        "clone_root": str(clone_root),
        "steps": steps,
        "vault_passphrase": {"seeded": vault_passphrase_created},
        "autostart": {"status": autostart_status, "label": autostart_label},
        "router": {"status": router_result.status, "reason": router_result.reason},
        "command_launcher": {
            "status": launcher_result.status, "path": launcher_result.launcher_path,
        },
        "git_init": {"status": git_init_record["status"]},
    }


def run_autostart_install(
    *,
    name: str,
    clone_root: Path,
    plist_dir: Path | None = None,
    home_dir: Path | None = None,
    launchctl_run: Runner | None = None,
) -> AutostartResult:
    """Install only the main LaunchAgent for the scoped autostart operation.

    ``genesis::autostart.install`` is a repair operation, not a request to
    replay birth.  In particular it must not run the profile installer, spine,
    router installer, command-launcher writer, marker writer, or git init.
    """
    return _install_autostart(name, clone_root, plist_dir, home_dir, launchctl_run)


def _install_autostart(
    name: str,
    clone_root: Path,
    plist_dir: Path | None,
    home_dir: Path | None,
    launchctl_run: Runner | None,
) -> AutostartResult:
    renderer = SimpleAutostartRenderer(
        solet_name=name,
        clone_root=clone_root,
        plist_dir=plist_dir if plist_dir is not None else Path.home() / "Library" / "LaunchAgents",
        home_dir=home_dir if home_dir is not None else Path.home(),
        run=launchctl_run if launchctl_run is not None else subprocess.run,
    )
    try:
        return renderer.install()
    except AutostartError as exc:
        raise GenesisError(f"autostart install failed: {exc}") from exc


def _run_autostart_phase(
    *,
    name: str,
    clone_root: Path,
    enabled: bool,
    plist_dir: Path | None,
    home_dir: Path | None,
    launchctl_run: Runner | None,
    phases: list[dict[str, Any]],
    finalize_marker: Callable[[str], None],
) -> tuple[str, str | None]:
    if not enabled:
        phases.append({
            "step_name": "install_autostart",
            "status": "skipped",
            "reason": "autostart_disabled",
        })
        return "not_requested", None
    try:
        installed = _install_autostart(name, clone_root, plist_dir, home_dir, launchctl_run)
    except GenesisError as exc:
        phases.append({"step_name": "install_autostart", "status": "failed", "error": str(exc)})
        finalize_marker("failed")
        raise
    phases.append({
        "step_name": "install_autostart",
        "status": "completed",
        "autostart_status": installed.status,
        "label": installed.label,
    })
    return installed.status, installed.label


def _run_git_init_phase(
    name: str,
    clone_root: Path,
    git_init_run: Runner | None,
    phases: list[dict[str, Any]],
    finalize: Callable[[str], None],
) -> dict[str, Any]:
    """Workstream A (2026-07-20): git-init the born tree as the final genesis
    phase. On failure, record it, finalize the attempt marker as failed, and
    re-raise as GenesisError — the same marker-on-failure discipline the other
    post-spine phases use, extracted so `run_genesis` stays low-complexity.
    """
    try:
        return git_init_worktree(
            clone_root, name,
            run=git_init_run if git_init_run is not None else subprocess.run,
        )
    except GitInitError as exc:
        phases.append({"step_name": "git_init", "status": "failed", "error": str(exc)})
        finalize("failed")
        raise GenesisError(f"git-init of the born worktree failed: {exc}") from exc


def _install_router(
    name: str,
    clone_root: Path,
    plugin_allowlist: list[str],
    router_install_run: Runner | None,
) -> RouterInstallResult:
    try:
        return install_router_at_birth(
            name=name,
            clone_root=clone_root,
            plugin_allowlist=plugin_allowlist,
            run=router_install_run if router_install_run is not None else subprocess.run,
        )
    except RouterInstallError as exc:
        raise GenesisError(f"router install failed: {exc}") from exc


def _run_command_launcher_phase(
    name: str,
    clone_root: Path,
    bin_dir: Path | None,
    codex_config_path: Path,
    phases: list[dict[str, Any]],
    finalize: Callable[[str], None],
) -> CommandLauncherResult:
    """No-MCP-first: put `<bin_dir>/<name>` on the operator's PATH. On failure,
    record it, finalize the attempt marker as failed, and re-raise as
    GenesisError — the same marker-on-failure discipline the other post-spine
    phases use, extracted so `run_genesis` stays low-complexity.
    """
    try:
        return install_command_launcher_at_birth(
            name=name,
            clone_root=clone_root,
            bin_dir=bin_dir if bin_dir is not None else DEFAULT_BIN_DIR,
            codex_config_path=codex_config_path,
        )
    except CommandLauncherError as exc:
        error = GenesisError(f"command launcher install failed: {exc}")
        phases.append({
            "step_name": "install_command_launcher", "status": "failed", "error": str(error),
        })
        finalize("failed")
        raise error from exc


def _run_vault_stale_check_phase(
    name: str,
    clone_root: Path,
    keychain: PerCredentialKeychain | None,
    fresh_passphrase_created: bool,
    phases: list[dict[str, Any]],
    finalize: Callable[[str], None],
) -> dict[str, Any]:
    try:
        return _check_for_stale_vault_master(
            name=name,
            clone_root=clone_root,
            keychain=keychain,
            fresh_passphrase_created=fresh_passphrase_created,
        )
    except GenesisError as exc:
        phases.append({"step_name": "vault_stale_check", "status": "failed", "error": str(exc)})
        finalize("failed")
        raise


def _run_vault_passphrase_phase(
    clone_root: Path,
    phases: list[dict[str, Any]],
    finalize: Callable[[str], None],
) -> bool:
    """Seed the vault passphrase and finalize the existing attempt on failure."""
    try:
        return seed_vault_passphrase(clone_root)
    except OSError as exc:
        error = GenesisError(f"vault passphrase seed failed: {exc}")
        phases.append({"step_name": "vault_passphrase", "status": "failed", "error": str(error)})
        finalize("failed")
        raise error from exc


def _check_for_stale_vault_master(
    *,
    name: str,
    clone_root: Path,
    keychain: PerCredentialKeychain | None,
    fresh_passphrase_created: bool,
) -> dict[str, Any]:
    """Refuse a fresh clone paired with an old macOS Keychain vault master key.

    Re-birthing the same solet name after deleting only the clone/database
    leaves service ``<name>-vault`` account ``master-key`` behind. If genesis just
    wrote a new passphrase file, that old wrapped master key cannot be unwrapped
    and launchd will crash-loop. Detect the mismatch before autostart.
    """
    pending_stale_check = vault_passphrase_stale_check_is_pending(clone_root)
    if not fresh_passphrase_created and not pending_stale_check:
        return {
            "step_name": "vault_stale_check",
            "status": "completed",
            "state": "existing_passphrase",
        }

    master_exists = _vault_master_key_exists(keychain)
    if master_exists is None:
        return {
            "step_name": "vault_stale_check",
            "status": "completed",
            "state": "injected_keychain_without_master_probe",
        }

    if master_exists:
        raise GenesisError(_stale_vault_master_message(name, after_passphrase_seed=True))
    if pending_stale_check:
        clear_vault_passphrase_stale_check_pending(clone_root)
    return {
        "step_name": "vault_stale_check",
        "status": "completed",
        "state": "no_existing_master_key",
    }


def vault_stale_state_precheck(
    name: str,
    keychain: PerCredentialKeychain | None = None,
) -> None:
    """Fail before birth when this name already owns a vault master-key item.

    This is deliberately a non-destructive precheck rather than automatic
    Keychain deletion: the item can belong to a live solet with the same name,
    and only a lifecycle-aware teardown can establish that deleting it is safe.
    """

    master_exists = _vault_master_key_exists(keychain)
    if master_exists is None:
        raise GenesisError("vault stale-keychain precheck requires a master-key capable backend")
    if master_exists:
        raise GenesisError(_stale_vault_master_message(name, after_passphrase_seed=False))


def _vault_master_key_exists(keychain: PerCredentialKeychain | None) -> bool | None:
    checker = getattr(keychain, "exists", None)
    if checker is None:
        if keychain is not None:
            return None
        checker = SystemKeychain().exists
    try:
        return bool(checker(MASTER_KEY_ACCOUNT))
    except Exception as exc:  # pragma: no cover - host keychain backend failures are environment-specific.
        raise GenesisError(f"vault stale-keychain check failed: {exc}") from exc


def _stale_vault_master_message(name: str, *, after_passphrase_seed: bool) -> str:
    timing = (
        "but genesis just created a fresh vault passphrase file"
        if after_passphrase_seed
        else "and would conflict with a fresh vault passphrase during genesis"
    )
    return (
        f"stale macOS Keychain vault state for solet {name!r}: "
        f"service {name}-vault account {MASTER_KEY_ACCOUNT!r} already exists {timing}. "
        f"Before re-birthing, run SOLET_NAME={name} .venv/bin/python3 -m "
        f"github_midwife_plugin.genesis {_STALE_VAULT_PRECHECK_ARGUMENT}; "
        "then use a lifecycle-aware teardown if it remains stale."
    )


def _write_genesis_marker(
    *, name: str, clone_root: Path, profile_name: str, result: dict[str, Any]
) -> None:
    """Write ``.solet/genesis.json``, the durable completion fact.

    The manager's ``genesis_artifacts_valid`` probe and the installation
    doctor both key on this file; genesis is its ONLY writer, so a genesis
    that returns 0 without it strands every driven create at a probe that can
    never verify (first measured on a real driven create, 2026-08-25). The
    marker is written after ``run_genesis`` returns and before the summary
    prints -- a summary for a marker-less genesis would announce a completion
    nothing downstream can verify. A write failure is a genesis failure, not
    a warning: the fact is load-bearing, so it fails loud.
    """
    marker_dir = clone_root / ".solet"
    payload = {
        "schema_version": 1,
        "solet_name": name,
        "profile": profile_name,
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "steps": [
            {"step_name": step["step_name"], "status": step["status"]}
            for step in result["steps"]
        ],
    }
    try:
        marker_dir.mkdir(mode=0o700, exist_ok=True)
        marker_dir.chmod(0o700)
        (marker_dir / "genesis.json").write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise GenesisError(f"genesis completion marker write failed: {exc}") from exc


def main() -> int:
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        print(
            "FATAL: SOLET_NAME env var is required. The driving agent "
            "must obtain the solet name from the user and set it "
            "before invoking bootstrap.py.",
            file=sys.stderr,
        )
        return 2

    arguments = sys.argv[1:]
    try:
        if arguments == [_STALE_VAULT_PRECHECK_ARGUMENT]:
            vault_stale_state_precheck(name)
            print(
                f"vault stale-keychain precheck OK: service {name}-vault "
                f"account {MASTER_KEY_ACCOUNT} is absent"
            )
            return 0
        if arguments:
            raise GenesisError(
                f"unsupported genesis arguments: {arguments!r}; expected "
                f"{_STALE_VAULT_PRECHECK_ARGUMENT!r} or no arguments"
            )
        clone_root = _resolve_clone_root()
        operation_ref = _operation_ref_from_environment()
        if operation_ref == _AUTOSTART_INSTALL_OPERATION_REF:
            if not _autostart_from_environment():
                raise GenesisError(
                    "genesis::autostart.install requires SOLET_AUTOSTART=enabled"
                )
            installed = run_autostart_install(name=name, clone_root=clone_root)
            print(f"LaunchAgent {installed.label} {installed.status}")
            return 0
        profile_name = resolve_profile_name(
            clone_root,
            os.environ.get(_PROFILE_ENV_VAR, ""),
        )
        result = run_genesis(
            name=name,
            clone_root=clone_root,
            profile_name=profile_name,
            autostart=_autostart_from_environment(),
        )
        _write_genesis_marker(
            name=name, clone_root=clone_root, profile_name=profile_name, result=result
        )
    except GenesisError as exc:
        print(f"FATAL: genesis failed: {exc}", file=sys.stderr)
        return 1

    print(f"genesis complete for {name!r} at {result['clone_root']}")
    for step in result["steps"]:
        print(f"  [{step['status']}] {step['step_name']}")
    vault_seeded = bool(result["vault_passphrase"]["seeded"])
    print(f"  [{'seeded' if vault_seeded else 'present'}] vault passphrase")
    print(f"  [{result['autostart']['status']}] autostart ({result['autostart']['label']})")
    print(f"  [{result['router']['status']}] router ({result['router']['reason']})")
    print(f"  [{result['git_init']['status']}] git-init (born worktree)")
    print(_mcp_register_suggestion(name, Path(str(result["clone_root"]))))
    return 0


def _autostart_from_environment() -> bool:
    value = os.environ.get(_AUTOSTART_ENV_VAR, "enabled")
    if value == "enabled":
        return True
    if value == "disabled":
        return False
    raise GenesisError(
        f"{_AUTOSTART_ENV_VAR} must be exactly 'enabled' or 'disabled', got {value!r}"
    )


def _operation_ref_from_environment() -> str:
    value = os.environ.get(_OPERATION_REF_ENV_VAR, _FULL_GENESIS_OPERATION_REF)
    if value in {_FULL_GENESIS_OPERATION_REF, _AUTOSTART_INSTALL_OPERATION_REF}:
        return value
    raise GenesisError(
        f"{_OPERATION_REF_ENV_VAR} must be one of "
        f"{_FULL_GENESIS_OPERATION_REF!r} or {_AUTOSTART_INSTALL_OPERATION_REF!r}, got {value!r}"
    )


def _mcp_register_suggestion(name: str, clone_root: Path) -> str:
    """A filled-in starting point for the README's MCP-registration ladder
    (build spec Slice H) -- printed, never executed. Registering the
    bridge mutates the DRIVING AGENT's own Claude Code config, on the
    operator's side of the genesis boundary; this process has no
    business reaching across to do that itself (Coordinator-Dawn ruling,
    2026-07-09 -- see README.md "Registering the MCP bridge"). This is
    purely a copy-paste convenience: no secrets, nothing executed here.
    """
    venv_python = clone_root / ".venv" / "bin" / "python3"
    return (
        "\nNext step (run this yourself, or your driving agent will run it "
        "for you -- see README.md \"Registering the MCP bridge\" for the "
        "full probe/verify ladder):\n"
        f"  claude mcp add --scope user -e SOLET_NAME={name} "
        "-e AGENT_IDENTITY=claude_code "
        f"{name} -- {venv_python} -m agent_messaging_plugin.mcp_bridge"
    )


if __name__ == "__main__":
    sys.exit(main())
