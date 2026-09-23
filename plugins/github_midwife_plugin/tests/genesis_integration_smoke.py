"""Slice G smoke — genesis.py end-to-end, fully sandboxed (build spec §8).

Drives `run_genesis()` -- the same function `main()`'s CLI entrypoint and
the `birth_solet` EDGE verb both call -- against a tmp clone, a
FakeKeychain, a tmpfs plist_dir + home_dir, and a fake launchctl. Asserts
the newborn's materialized config is coherent AND that no plaintext
credential ever appears in captured stdout/stderr across the whole run.

Every external boundary is faked: `subprocess.run` (profile_install's
pip calls) is globally mocked; `seed_db_password`'s
keychain/alter_role_password/role_authenticates/role_exists and the
autostart renderer's plist_dir/home_dir/launchctl_run are all passed
explicitly to `run_genesis()`. No real Postgres, Keychain,
`~/Library/LaunchAgents`, or network touched. This drives the CLI/
fresh-machine path (credential_provisioner=None -> in-process
seed_db_password); the verb-mode self-seed subprocess is exercised by
venv_provision_smoke.

Run directly: ``SOLET_NAME=<name> .venv/bin/python3
plugins/github_midwife_plugin/tests/genesis_integration_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import yaml
from fake_keychain import FakeKeychain  # noqa: E402

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
_SELF_DEPLOYMENT_PLUGIN_ROOT = _PLUGIN_ROOT.parent / "macos_self_deployment_plugin"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.autostart import AutostartResult  # noqa: E402
from github_midwife_plugin.genesis import (  # noqa: E402
    GenesisError,
    _write_genesis_marker,
    resolve_profile_name,
    run_autostart_install,
    run_genesis,
)
from github_midwife_plugin.genesis import (
    main as genesis_main,
)
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import SystemRuntime  # noqa: E402
from github_midwife_plugin.setup_operations import genesis_artifacts_valid  # noqa: E402
from github_midwife_plugin.steps import GENESIS_STEP_RUNNERS  # noqa: E402

_CHECKS_RUN: list[str] = []
_SENTINEL_PW = "INTEGRATION_SENTINEL_PW_do_not_leak_54321"
_VAULT_SENTINEL = "INTEGRATION_SENTINEL_VAULT_PASSPHRASE_do_not_leak_98765"
_PROFILE_NAME = "fixture-genesis-profile"
_GENESIS_STEP_NAMES = tuple(step_name for step_name, _runner in GENESIS_STEP_RUNNERS)


@dataclass(frozen=True)
class BundlePluginSkip:
    """A visible skip caused only by a sealed bundle omitting a plugin."""

    profile: str
    plugin: str

    def render(self) -> str:
        return f"SKIP  {self.plugin} not in bundle {self.profile}; blue-green router leg is not shipped"


def _bundle_plugin_skip(plugin_root: Path, plugin: str) -> BundlePluginSkip | None:
    if plugin_root.is_dir():
        return None
    provenance_path = _PLUGIN_ROOT.parents[1] / "PROVENANCE.json"
    if not provenance_path.is_file():
        raise AssertionError(f"red: {plugin} is absent outside a sealed bundle")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    bundle = provenance.get("bundle")
    profile = bundle.get("name") if isinstance(bundle, dict) else None
    if not isinstance(profile, str) or not profile:
        raise AssertionError("red: sealed bundle has no valid PROVENANCE.json bundle.name")
    return BundlePluginSkip(profile=profile, plugin=plugin)


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _make_fixture_clone(root: Path) -> Path:
    """A minimal but complete clone-shaped tree: satisfies resolve_target's
    marker check (ananta/, plugins/), profile_install's
    package-dir checks (ananta/pyproject.toml, the seed plugin's
    pyproject.toml), and a minimal self-only allowlist profile + baseline.

    Deliberately NO ``knowledge_bases/`` dir: git does not track empty
    directories, so a real seed-born clone arrives without one — genesis
    itself must create+populate it (the materialize_kb_symlinks step). The
    happy-path check asserts it exists AFTER run_genesis.
    """
    clone = root / "clone"
    (clone / "ananta").mkdir(parents=True)
    (clone / "ananta" / "pyproject.toml").write_text("[project]\nname='ananta'\n")
    # The seed ships the MINTING solet's root_manifest.yaml verbatim;
    # the seed_root_manifest spine step must rewrite `solet_name:` to
    # the newborn's name and leave every other line untouched.
    (clone / "root_manifest.yaml").write_text(
        "schema_version: 1\nsolet_name: mintersaurus\nuniversal:\n  files: []\n"
    )
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n")
    # The `solet-bridge` console script the command-launcher birth step symlinks
    # to (installed by `pip install -e` of agent_messaging_plugin in a real venv).
    (clone / ".venv" / "bin" / "solet-bridge").write_text("#!/bin/sh\n")

    plugin_dir = clone / "plugins" / "github_midwife_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pyproject.toml").write_text("[project]\nname='github_midwife_plugin'\n")

    kb_root = plugin_dir / "knowledge_base"
    (kb_root / "profile_templates").mkdir(parents=True)
    (kb_root / "profile_templates" / f"{_PROFILE_NAME}.yaml").write_text(
        yaml.safe_dump({
            "profile_name": _PROFILE_NAME,
            "description": "fixture profile for the genesis integration smoke",
            "plugins": ["github_midwife_plugin"],
            "service_bindings": {},
            "plugin_config_overrides": {},
            "starting_actions": [],
        }, sort_keys=False)
    )
    (kb_root / "profile_baseline").mkdir(parents=True)

    return clone


def _check_profile_resolution_from_provenance(root: Path) -> None:
    clone = _make_fixture_clone(root)
    _check(
        "provenance-less seeds keep the legacy free-profile default",
        resolve_profile_name(clone) == "macos-free-solet",
    )

    (clone / "PROVENANCE.json").write_text(json.dumps({
        "bundle": {"name": "macos-bizops", "platform": "local"},
    }))
    _check(
        "canonical sealed-style provenance selects macos-bizops without an override",
        resolve_profile_name(clone) == "macos-bizops",
    )

    try:
        resolve_profile_name(clone, "macos-bizops-solet")
    except GenesisError as exc:
        _check(
            "a declared canonical bundle cannot be overridden by a retired profile",
            "profile identity mismatch" in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("declared-bundle-override: resolver did not raise")

    (clone / "PROVENANCE.json").write_text(json.dumps({
        "bundle": {"name": "unknown_bundle"},
    }))
    try:
        resolve_profile_name(clone)
    except GenesisError as exc:
        _check(
            "unknown provenance bundle fails loud instead of falling back to free",
            "unknown_bundle" in str(exc) and "cannot be overridden" in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("unknown-provenance-bundle: resolver did not raise")

    (clone / "PROVENANCE.json").unlink()
    _check(
        "an explicit custom profile remains available without declared provenance",
        resolve_profile_name(clone, "fixture-profile") == "fixture-profile",
    )


def _check_run_genesis_refuses_before_allowlist(root: Path) -> None:
    clone = _make_fixture_clone(root)
    (clone / "PROVENANCE.json").write_text(
        json.dumps({"bundle": {"name": "macos-bizops", "platform": "local"}}),
        encoding="utf-8",
    )
    with patch("github_midwife_plugin.genesis.load_plugin_allowlist") as load_allowlist, \
         patch("github_midwife_plugin.genesis.install_profile_allowlist") as install_allowlist:
        try:
            run_genesis(
                name="bizops",
                clone_root=clone,
                profile_name="bizops_standard",
            )
        except GenesisError as exc:
            _check(
                "run_genesis rejects provenance/profile mismatch",
                "profile identity mismatch" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("run-genesis-profile-mismatch: did not raise")
    _check(
        "run_genesis mismatch occurs before allowlist load or installation",
        load_allowlist.call_count == 0 and install_allowlist.call_count == 0,
        f"load={load_allowlist.call_count}, install={install_allowlist.call_count}",
    )


def _fake_pip_subprocess_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    del kwargs
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


class _FakeLaunchctl:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded = False

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        verb = cmd[1] if len(cmd) > 1 else ""
        if verb == "list":
            if self.loaded:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            # Real launchctl's documented not-found signal (2026-07-09
            # Codex F must-fix: autostart.py now raises rather than
            # coercing any nonzero exit into "not loaded").
            label = cmd[2] if len(cmd) > 2 else ""
            return subprocess.CompletedProcess(
                args=cmd, returncode=113, stdout="",
                stderr=f'Could not find service "{label}" in domain for ...',
            )
        if verb == "load":
            self.loaded = True
        elif verb == "unload":
            self.loaded = False
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


class _FakeStaleMasterKeychain(FakeKeychain):
    def exists(self, account: str) -> bool:
        return account == "master-key"


class _FakeCleanMasterKeychain(FakeKeychain):
    def exists(self, account: str) -> bool:
        del account
        return False


def _run_sandboxed_genesis(
    root: Path,
    *,
    autostart: bool = True,
) -> tuple[dict[str, object], Path, FakeKeychain, _FakeLaunchctl, str]:
    clone = _make_fixture_clone(root)
    keychain = FakeKeychain()
    fake_launchctl = _FakeLaunchctl()
    alter_calls: list[str] = []

    captured_out, captured_err = io.StringIO(), io.StringIO()
    with patch("subprocess.run", side_effect=_fake_pip_subprocess_run), \
         patch("github_midwife_plugin.credential_seed.secrets.token_urlsafe", return_value=_SENTINEL_PW), \
         patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_VAULT_SENTINEL), \
         redirect_stdout(captured_out), redirect_stderr(captured_err):
        result = run_genesis(
            name="testhum",
            clone_root=clone,
            profile_name=_PROFILE_NAME,
            keychain=keychain,
            alter_role_password=alter_calls.append,
            role_authenticates=lambda pw: pw == _SENTINEL_PW,
            role_exists=lambda: True,
            plist_dir=root / "LaunchAgents",
            home_dir=root / "home",
            launchctl_run=fake_launchctl,
            command_launcher_bin_dir=root / "bin",
            autostart=autostart,
        )
    combined_output = captured_out.getvalue() + captured_err.getvalue()
    return result, clone, keychain, fake_launchctl, combined_output


def _adapter_request(target: Path) -> AdapterRequest:
    return AdapterRequest(
        request_id="8f2f3ed3-03fc-4f58-915e-eb400a172a67",
        operation_id="genesis_artifacts_valid",
        operation_ref="genesis::solet.verify",
        phase="probe",
        probe_purpose="stage_exit",
        attempt=1,
        name="testhum",
        target=target,
        flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=30,
        public_inputs={"autostart": "enabled"},
    )


def _check_autostart_disabled_skips_install(root: Path) -> None:
    with patch("github_midwife_plugin.genesis._install_autostart") as install:
        result, _clone, _keychain, fake_launchctl, _output = _run_sandboxed_genesis(
            root,
            autostart=False,
        )
    _check(
        install.call_count == 0 and not any(call[1] == "load" for call in fake_launchctl.calls),
        "autostart-disabled Genesis never calls the autostart installer",
        str(fake_launchctl.calls),
    )
    _check(
        result["autostart"] == {"status": "not_requested", "label": None},
        "autostart-disabled Genesis records the selected topology",
        str(result.get("autostart")),
    )


def _check_genesis_artifact_content_validation(root: Path) -> None:
    result, clone, _keychain, _launchctl, _output = _run_sandboxed_genesis(root)
    _write_genesis_marker(
        name="testhum",
        clone_root=clone,
        profile_name=_PROFILE_NAME,
        result=result,
    )
    home = root / "adapter-home"
    launcher = home / ".local/bin/testhum"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(clone / ".venv/bin/solet-bridge")
    request = _adapter_request(clone)
    runtime = SystemRuntime(home=home)
    _check(
        genesis_artifacts_valid(request, runtime),
        "valid final Genesis marker and declared artifacts verify",
    )
    marker = clone / ".solet/genesis.json"
    marker.write_text('{"status":"failed"}\n', encoding="utf-8")
    _check(
        not genesis_artifacts_valid(request, runtime),
        "corrupt Genesis marker content is rejected rather than accepted by existence",
    )
    _write_genesis_marker(
        name="testhum",
        clone_root=clone,
        profile_name=_PROFILE_NAME,
        result=result,
    )
    launcher.unlink()
    _check(
        not genesis_artifacts_valid(request, runtime),
        "missing declared named launcher invalidates Genesis artifacts",
    )


def _check_end_to_end_happy_path(root: Path) -> None:
    result, clone, keychain, fake_launchctl, output = _run_sandboxed_genesis(root)

    _check(
        "run_genesis returns the 6-step spine, all completed",
        [s["step_name"] for s in result["steps"]]  # type: ignore[union-attr]
        == list(_GENESIS_STEP_NAMES)
        and all(s["status"] == "completed" for s in result["steps"]),  # type: ignore[union-attr]
        str(result["steps"]),
    )

    _check(
        "genesis CREATED knowledge_bases/ (a seed-born clone arrives without it)",
        (clone / "knowledge_bases").is_dir(),
        str(sorted(p.name for p in clone.iterdir())),
    )

    root_manifest_text = (clone / "root_manifest.yaml").read_text()
    _check(
        "seed_root_manifest renamed the newborn (minting name gone, newborn name present)",
        "solet_name: testhum\n" in root_manifest_text
        and "mintersaurus" not in root_manifest_text,
        root_manifest_text,
    )
    _check(
        "seed_root_manifest left every other root_manifest line verbatim",
        root_manifest_text.startswith("schema_version: 1\n")
        and root_manifest_text.endswith("universal:\n  files: []\n"),
        root_manifest_text,
    )

    manifest_path = clone / "profile" / "config" / "manifest.yaml"
    _check("manifest.yaml was materialized", manifest_path.is_file(), str(manifest_path))
    manifest = yaml.safe_load(manifest_path.read_text())
    _check(
        "manifest.yaml's plugins list matches the fixture profile",
        manifest.get("plugins") == ["github_midwife_plugin"],
        str(manifest),
    )
    bindings_path = clone / "profile" / "config" / "service_bindings.json"
    _check("service_bindings.json was materialized", bindings_path.is_file())
    _check(
        "service_bindings.json matches the fixture profile (empty)",
        json.loads(bindings_path.read_text()) == {},
    )

    _assert_final_marker_complete(result)

    stored_pw = keychain.retrieve_credential("postgres_state_management_plugin", "db_password")
    _check(
        "the credential seed stored the generated password in the FakeKeychain",
        stored_pw == _SENTINEL_PW.encode("utf-8"),
        f"got {stored_pw!r}",
    )

    # Finding F10: the vault passphrase file must be materialized (mode 0600)
    # so the newborn's macos_vault_plugin can self-initialize at first boot.
    passphrase_path = clone / "profile" / "config" / "plugins" / "macos_vault_plugin" / "passphrase"
    _check("the vault passphrase file was materialized during genesis", passphrase_path.is_file(), str(passphrase_path))
    _check(
        "the vault passphrase file is mode 0600 (owner-only)",
        (passphrase_path.stat().st_mode & 0o777) == 0o600,
        oct(passphrase_path.stat().st_mode & 0o777),
    )
    _check(
        "the vault passphrase file holds the generated passphrase",
        passphrase_path.read_text() == _VAULT_SENTINEL,
        "passphrase file content mismatch",
    )
    _check(
        "run_genesis reports the vault passphrase was seeded",
        result["vault_passphrase"]["seeded"] is True,  # type: ignore[index]
        str(result.get("vault_passphrase")),
    )

    _check(
        "autostart installed via the fake launchctl (load called)",
        any(c[1] == "load" for c in fake_launchctl.calls),
        str(fake_launchctl.calls),
    )
    plist_path = root / "LaunchAgents" / "local.solet.testhum.plist"
    _check("the plist was rendered to the tmpfs plist_dir", plist_path.is_file(), str(plist_path))
    plist_text = plist_path.read_text()
    _check(
        "the rendered plist points at the fixture clone's own venv python3",
        str(clone / ".venv" / "bin" / "python3") in plist_text,
        plist_text,
    )
    _check(
        "the rendered plist's WorkingDirectory is under the tmpfs home_dir, not the real ~",
        str(root / "home" / ".ananta" / "runtime" / "testhum") in plist_text,
        plist_text,
    )

    # SEED-06: the fixture profile ships only github_midwife_plugin (no
    # macos_self_deployment_plugin), so this newborn is single-color by design
    # and the router phase SKIPS cleanly — it must still be recorded, and it
    # must never invoke the installer.
    _check(
        "the router phase skipped for a single-color (no self-deployment) newborn",
        result["router"]["status"] == "skipped",  # type: ignore[index]
        str(result.get("router")),
    )

    # No-MCP-first: the command-launcher phase installed `<bin_dir>/<name>` as a
    # symlink to the fixture clone's own console script (sandboxed bin_dir).
    launcher_link = root / "bin" / "testhum"
    _check(
        "the command-launcher phase installed the per-solet PATH symlink",
        result["command_launcher"]["status"] == "installed"  # type: ignore[index]
        and launcher_link.is_symlink()
        and launcher_link.readlink() == clone / ".venv" / "bin" / "solet-bridge",
        f"{result.get('command_launcher')} link={launcher_link}",
    )

    # Workstream A: the git_init phase ran (under the global subprocess mock it
    # records completed without a real .git; git_init_smoke drives the REAL git).
    _check(
        "the git_init phase ran and completed (born-worktree wiring)",
        result["git_init"]["status"] == "completed",  # type: ignore[index]
        str(result.get("git_init")),
    )

    _check(
        "the generated password never appears anywhere in captured stdout/stderr across the whole run",
        _SENTINEL_PW not in output,
        f"leaked: {output!r}",
    )

    _check(
        "the generated vault passphrase never appears anywhere in captured stdout/stderr across the whole run",
        _VAULT_SENTINEL not in output,
        f"leaked: {output!r}",
    )

    _check(
        "run_genesis's own return value never echoes the generated password",
        _SENTINEL_PW not in json.dumps(result, default=str),
        str(result),
    )

    _check(
        "run_genesis's own return value never echoes the generated vault passphrase",
        _VAULT_SENTINEL not in json.dumps(result, default=str),
        str(result),
    )


def _assert_final_marker_complete(result: dict[str, object]) -> None:
    """RED-FIRST (dorothy D4, 2026-07-13): the marker used to be written once
    at spine-end with status "success", so a genesis that later died at
    credential seed / autostart still read as a success -- and the post-spine
    phases were invisible to the audit trail. The FINAL marker must record
    them all."""
    marker_path = Path(str(result["steps"][-1]["manifest_path"]))  # type: ignore[index]
    _check("the genesis attempt marker was written", marker_path.is_file(), str(marker_path))
    marker = json.loads(marker_path.read_text())
    _check(
        "the attempt marker records the solet name and success status",
        marker.get("solet_name") == "testhum" and marker.get("status") == "success",
        str(marker),
    )
    marker_step_names = [s.get("step_name") for s in marker.get("steps", [])]
    _check(
        "the FINAL attempt marker records the post-spine phases "
        "(credential_seed, vault_passphrase, vault_stale_check, install_autostart, install_router, "
        "install_command_launcher, git_init) after the 6-step spine",
        marker_step_names == [
            "validate_name", "resolve_target", "materialize_configs",
            "seed_root_manifest", "materialize_kb_symlinks", "write_manifest_marker",
            "credential_seed", "vault_passphrase", "vault_stale_check", "install_autostart", "install_router",
            "install_command_launcher", "git_init",
        ]
        and all(s.get("status") == "completed" for s in marker.get("steps", [])),
        str(marker),
    )


def _check_spine_failure_surfaces_as_genesis_error(root: Path) -> None:
    """An incomplete seed tree (missing root_manifest.yaml — which every
    real seed ships) fails at seed_root_manifest inside the spine --
    run_genesis must wrap this as a GenesisError naming the failing step,
    not a silent partial result. (knowledge_bases/ absence is NOT a
    failure any more: a seed-born clone legitimately arrives without it.)
    """
    clone = root / "incomplete_clone"
    (clone / "ananta").mkdir(parents=True)
    (clone / "ananta" / "pyproject.toml").write_text("[project]\nname='ananta'\n")
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n")
    plugin_dir = clone / "plugins" / "github_midwife_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pyproject.toml").write_text("[project]\nname='github_midwife_plugin'\n")
    kb_root = plugin_dir / "knowledge_base"
    (kb_root / "profile_templates").mkdir(parents=True)
    (kb_root / "profile_templates" / f"{_PROFILE_NAME}.yaml").write_text(
        yaml.safe_dump({"profile_name": _PROFILE_NAME, "plugins": ["github_midwife_plugin"]})
    )
    (kb_root / "profile_baseline").mkdir(parents=True)
    # deliberately no root_manifest.yaml (and no knowledge_bases/ — which is
    # now a legitimate seed-clone state, not a failure)

    with patch("subprocess.run", side_effect=_fake_pip_subprocess_run):
        try:
            run_genesis(
                name="testhum2", clone_root=clone, profile_name=_PROFILE_NAME,
                keychain=FakeKeychain(), role_exists=lambda: True,
            )
        except GenesisError as exc:
            _check(
                "an incomplete seed tree raises GenesisError naming seed_root_manifest",
                "seed_root_manifest" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("spine-failure: run_genesis did not raise")


class _FailingLoadLaunchctl(_FakeLaunchctl):
    """A launchctl whose `load` verb hard-fails -- drives the autostart phase
    into its failure path."""

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if len(cmd) > 1 and cmd[1] == "load":
            self.calls.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="Load failed: 5: Input/output error",
            )
        return super().__call__(cmd, **_kwargs)


def _check_phase_failure_writes_failed_marker(root: Path) -> None:
    """RED-FIRST (dorothy D4, 2026-07-13): a genesis that completes the spine
    but dies at a post-spine phase (here: autostart install) must leave the
    attempt marker saying FAILED with a failed phase record -- before the fix
    the spine-end marker said "success" and the runtime death was invisible.
    """
    clone = _make_fixture_clone(root)
    with patch("subprocess.run", side_effect=_fake_pip_subprocess_run), \
         patch("github_midwife_plugin.credential_seed.secrets.token_urlsafe", return_value=_SENTINEL_PW), \
         patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_VAULT_SENTINEL):
        try:
            run_genesis(
                name="testhum3",
                clone_root=clone,
                profile_name=_PROFILE_NAME,
                keychain=FakeKeychain(),
                alter_role_password=lambda _pw: None,
                role_authenticates=lambda pw: pw == _SENTINEL_PW,
                role_exists=lambda: True,
                plist_dir=root / "LaunchAgents",
                home_dir=root / "home",
                launchctl_run=_FailingLoadLaunchctl(),
            )
        except GenesisError as exc:
            _check(
                "an autostart-phase failure surfaces as GenesisError",
                "autostart" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("phase-failure: run_genesis did not raise")

    marker_path = clone / "profile" / "data" / "github_midwife" / "attempt.json"
    _check("the attempt marker exists after the phase failure", marker_path.is_file(), str(marker_path))
    marker = json.loads(marker_path.read_text())
    _check(
        "the attempt marker records FAILED status (not the spine-end 'success') after a phase failure",
        marker.get("status") == "failed",
        str(marker),
    )
    failed_records = [s for s in marker.get("steps", []) if s.get("status") == "failed"]
    _check(
        "the failed install_autostart phase is recorded in the marker",
        len(failed_records) == 1
        and failed_records[0].get("step_name") == "install_autostart"
        and "Load failed" in str(failed_records[0].get("error", "")),
        str(marker.get("steps")),
    )


def _check_vault_passphrase_failure_writes_failed_marker(root: Path) -> None:
    """A vault-passphrase filesystem failure is a classified phase failure."""
    clone = _make_fixture_clone(root)
    with patch("subprocess.run", side_effect=_fake_pip_subprocess_run), \
         patch("github_midwife_plugin.credential_seed.secrets.token_urlsafe", return_value=_SENTINEL_PW), \
         patch("github_midwife_plugin.genesis.seed_vault_passphrase", side_effect=OSError("disk full")):
        try:
            run_genesis(
                name="testhum-vault-failure",
                clone_root=clone,
                profile_name=_PROFILE_NAME,
                keychain=FakeKeychain(),
                alter_role_password=lambda _pw: None,
                role_authenticates=lambda pw: pw == _SENTINEL_PW,
                role_exists=lambda: True,
                plist_dir=root / "LaunchAgents",
                home_dir=root / "home",
                launchctl_run=_FakeLaunchctl(),
            )
        except GenesisError as exc:
            _check(
                "a vault-passphrase filesystem failure surfaces as GenesisError",
                "vault passphrase seed failed" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("vault-passphrase-failure: run_genesis did not raise")

    marker_path = clone / "profile" / "data" / "github_midwife" / "attempt.json"
    _check("the attempt marker exists after vault-passphrase failure", marker_path.is_file(), str(marker_path))
    marker = json.loads(marker_path.read_text())
    failed_records = [step for step in marker.get("steps", []) if step.get("status") == "failed"]
    _check(
        "the vault-passphrase failure finalizes the attempt marker as failed",
        marker.get("status") == "failed"
        and len(failed_records) == 1
        and failed_records[0].get("step_name") == "vault_passphrase"
        and "disk full" in str(failed_records[0].get("error", "")),
        str(marker),
    )


def _check_stale_vault_master_fails_before_autostart(root: Path) -> None:
    clone = _make_fixture_clone(root)
    fake_launchctl = _FakeLaunchctl()
    with patch("subprocess.run", side_effect=_fake_pip_subprocess_run), \
         patch("github_midwife_plugin.credential_seed.secrets.token_urlsafe", return_value=_SENTINEL_PW), \
         patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_VAULT_SENTINEL):
        first_error = _run_stale_vault_genesis(root, clone, fake_launchctl)
    _check(
        "stale Keychain master fails genesis before autostart",
        "stale macOS Keychain vault state" in str(first_error),
        str(first_error),
    )

    marker_path = clone / "profile" / "data" / "github_midwife" / "attempt.json"
    _check("the attempt marker exists after stale-vault failure", marker_path.is_file(), str(marker_path))
    marker = json.loads(marker_path.read_text())
    step_names = [s.get("step_name") for s in marker.get("steps", [])]
    _check(
        "the stale-vault failure records vault_stale_check and never reaches autostart",
        marker.get("status") == "failed"
        and "vault_stale_check" in step_names
        and "install_autostart" not in step_names,
        str(marker),
    )
    stale_record = next(s for s in marker.get("steps", []) if s.get("step_name") == "vault_stale_check")
    _check(
        "the vault_stale_check marker record is failed and names master-key",
        stale_record.get("status") == "failed" and "master-key" in str(stale_record.get("error", "")),
        str(stale_record),
    )
    _check(
        "launchctl was never invoked after stale-vault detection",
        fake_launchctl.calls == [],
        str(fake_launchctl.calls),
    )

    with patch("subprocess.run", side_effect=_fake_pip_subprocess_run), \
         patch("github_midwife_plugin.credential_seed.secrets.token_urlsafe", return_value=_SENTINEL_PW), \
         patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_VAULT_SENTINEL):
        retry_error = _run_stale_vault_genesis(root, clone, fake_launchctl)
    _check(
        "an unchanged retry still refuses the stale Keychain master",
        "stale macOS Keychain vault state" in str(retry_error),
        str(retry_error),
    )
    _check(
        "launchctl remains uninvoked after the unchanged stale-Keychain retry",
        fake_launchctl.calls == [],
        str(fake_launchctl.calls),
    )


def _check_stale_vault_precheck_is_scriptable() -> None:
    """The operator can detect residue before a new genesis creates state."""

    stale_stdout = io.StringIO()
    stale_stderr = io.StringIO()
    with patch("github_midwife_plugin.genesis.SystemKeychain", return_value=_FakeStaleMasterKeychain()), \
         patch.object(sys, "argv", ["genesis", "--check-vault-stale-state"]), \
         patch.dict("os.environ", {"SOLET_NAME": "testhum4"}), \
         redirect_stdout(stale_stdout), redirect_stderr(stale_stderr):
        stale_exit = genesis_main()
    _check(
        "the stale-vault precheck fails before genesis",
        stale_exit == 1
        and "service testhum4-vault account 'master-key' already exists" in stale_stderr.getvalue(),
        stale_stderr.getvalue(),
    )

    clean_stdout = io.StringIO()
    with patch("github_midwife_plugin.genesis.SystemKeychain", return_value=_FakeCleanMasterKeychain()), \
         patch.object(sys, "argv", ["genesis", "--check-vault-stale-state"]), \
         patch.dict("os.environ", {"SOLET_NAME": "testhum4"}), \
         redirect_stdout(clean_stdout):
        clean_exit = genesis_main()
    _check(
        "the clean precheck is scriptable and names its checked Keychain item",
        clean_exit == 0
        and "service testhum4-vault account master-key is absent" in clean_stdout.getvalue(),
        clean_stdout.getvalue(),
    )


def _run_stale_vault_genesis(
    root: Path,
    clone: Path,
    fake_launchctl: _FakeLaunchctl,
) -> GenesisError:
    """Run the stale-master fixture and require the Genesis refusal."""
    try:
        run_genesis(
            name="testhum4",
            clone_root=clone,
            profile_name=_PROFILE_NAME,
            keychain=_FakeStaleMasterKeychain(),
            alter_role_password=lambda _pw: None,
            role_authenticates=lambda pw: pw == _SENTINEL_PW,
            role_exists=lambda: True,
            plist_dir=root / "LaunchAgents",
            home_dir=root / "home",
            launchctl_run=fake_launchctl,
        )
    except GenesisError as exc:
        return exc
    raise SmokeFailureError("stale-vault-master: run_genesis did not raise")


class _FakeRouterInstaller:
    """Records the argv genesis would run to install the blue-green router;
    returns a clean exit (no real launchctl / port bind)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


def _add_blue_green_capability(clone: Path) -> Path:
    """Make the fixture clone blue-green-capable: add macos_self_deployment_plugin
    to the profile allowlist, ship its pyproject (profile_install's editable-check
    needs it; pip itself is mocked), and ship a stub router installer. Returns the
    stub installer path genesis should invoke."""
    profile_path = (
        clone / "plugins" / "github_midwife_plugin" / "knowledge_base"
        / "profile_templates" / f"{_PROFILE_NAME}.yaml"
    )
    profile = yaml.safe_load(profile_path.read_text())
    profile["plugins"] = ["github_midwife_plugin", "macos_self_deployment_plugin"]
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    sd_dir = clone / "plugins" / "macos_self_deployment_plugin"
    (sd_dir / "src" / "macos_self_deployment_plugin" / "blue_green_router").mkdir(parents=True)
    (sd_dir / "pyproject.toml").write_text("[project]\nname='macos_self_deployment_plugin'\n")
    installer = (
        sd_dir / "src" / "macos_self_deployment_plugin" / "blue_green_router" / "install_router.py"
    )
    installer.write_text("# stub installer\n")
    return installer


def _check_router_installed_for_blue_green_profile(root: Path) -> None:
    """A blue-green-capable newborn (macos_self_deployment_plugin in the profile
    allowlist) gets the router installed: genesis invokes the SHIPPED installer
    via the NEWBORN's own venv python, with argv [venv python, install_router.py,
    name]. End-to-end proof of the SEED-06 wiring's install branch."""
    skip = _bundle_plugin_skip(_SELF_DEPLOYMENT_PLUGIN_ROOT, "macos_self_deployment_plugin")
    if skip is not None:
        print(skip.render())
        return
    clone = _make_fixture_clone(root)
    installer = _add_blue_green_capability(clone)
    router_runner = _FakeRouterInstaller()

    with patch("subprocess.run", side_effect=_fake_pip_subprocess_run), \
         patch("github_midwife_plugin.credential_seed.secrets.token_urlsafe", return_value=_SENTINEL_PW), \
         patch("github_midwife_plugin.vault_passphrase_seed.token_urlsafe", return_value=_VAULT_SENTINEL):
        result = run_genesis(
            name="bizhum",
            clone_root=clone,
            profile_name=_PROFILE_NAME,
            keychain=FakeKeychain(),
            alter_role_password=lambda _pw: None,
            role_authenticates=lambda pw: pw == _SENTINEL_PW,
            role_exists=lambda: True,
            plist_dir=root / "LaunchAgents",
            home_dir=root / "home",
            launchctl_run=_FakeLaunchctl(),
            router_install_run=router_runner,
        )

    _check(
        "router installed for a blue-green-capable newborn",
        result["router"]["status"] == "installed",  # type: ignore[index]
        str(result.get("router")),
    )
    _check(
        "genesis invoked the router installer exactly once",
        len(router_runner.calls) == 1,
        str(router_runner.calls),
    )
    _check(
        "the router installer got [newborn venv python, install_router.py, name]",
        router_runner.calls[0] == [
            str(clone / ".venv" / "bin" / "python3"), str(installer), "bizhum",
        ],
        str(router_runner.calls),
    )


def _check_operation_scoped_launchagent_install(root: Path) -> None:
    """The autostart operation must not replay genesis or touch router state."""
    clone = _make_fixture_clone(root)
    router_state = root / "router-state.json"
    router_state.write_text("router remains active", encoding="utf-8")
    expected = AutostartResult(
        status="success",
        verb="install_autostart",
        label="local.solet.scoped",
        plist_path=str(root / "LaunchAgents/local.solet.scoped.plist"),
        prior_state="loaded",
        message="fixture",
    )
    with patch(
        "github_midwife_plugin.genesis._install_autostart", return_value=expected
    ) as install, patch(
        "github_midwife_plugin.genesis.run_genesis",
        side_effect=AssertionError("full genesis replay must not run"),
    ):
        result = run_autostart_install(name="scoped", clone_root=clone)

    _check("scoped launchagent install invokes only autostart", install.call_count == 1)
    _check(
        "scoped launchagent install preserves router state",
        router_state.read_text(encoding="utf-8") == "router remains active",
    )
    _check(
        "scoped launchagent install returns its main LaunchAgent result",
        result == expected,
    )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_profile_resolution_from_provenance(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_run_genesis_refuses_before_allowlist(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_end_to_end_happy_path(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_autostart_disabled_skips_install(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_genesis_artifact_content_validation(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_router_installed_for_blue_green_profile(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_operation_scoped_launchagent_install(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_phase_failure_writes_failed_marker(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_vault_passphrase_failure_writes_failed_marker(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_stale_vault_master_fails_before_autostart(Path(tmp))
        _check_stale_vault_precheck_is_scriptable()
        with tempfile.TemporaryDirectory() as tmp:
            _check_spine_failure_surfaces_as_genesis_error(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"genesis_integration_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
