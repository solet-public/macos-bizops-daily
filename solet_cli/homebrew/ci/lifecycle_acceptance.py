#!/usr/bin/env python3
"""Run the release-dependent Homebrew manager lifecycle on a fresh CI runner."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

JsonObject = dict[str, object]


@dataclass(frozen=True)
class Inputs:
    tap: str
    formula: str
    previous_formula: Path
    current_formula: Path
    current_bottle: Path
    previous_version: str
    current_version: str
    previous_lock_sha256: str
    current_lock_sha256: str
    brewfile: Path
    create_config_template: Path
    fixture_root: Path
    instance_name: str
    python_formula: str


@dataclass(frozen=True)
class Fixture:
    root: Path
    home: Path
    manager: Path
    target: Path
    config: Path


@dataclass(frozen=True)
class SnapshotEntry:
    kind: str
    value: str


@dataclass(frozen=True)
class Phase:
    name: str
    commands: tuple[tuple[str, ...], ...]


class Runner:
    def __init__(self, environment: dict[str, str]) -> None:
        self.environment = environment

    def run(
        self,
        arguments: list[str],
        *,
        allowed: tuple[int, ...] = (0,),
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        print(f"$ {shlex.join(arguments)}", flush=True)
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
            cwd=cwd,
        )
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        if completed.returncode not in allowed:
            raise RuntimeError(
                f"command exited {completed.returncode}, expected one of {allowed}: "
                f"{shlex.join(arguments)}"
            )
        return completed

    def json(
        self,
        arguments: list[str],
        *,
        allowed: tuple[int, ...] = (0,),
    ) -> JsonObject:
        completed = self.run(arguments, allowed=allowed)
        try:
            value: object = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"command did not return JSON: {shlex.join(arguments)}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"command returned non-object JSON: {shlex.join(arguments)}")
        return cast(JsonObject, value)


def build_plan(inputs: Inputs) -> tuple[Phase, ...]:
    return (
        Phase(
            "prepare-disposable-local-tap",
            (
                ("brew", "tap-new", inputs.tap),
                (
                    "git",
                    "-C",
                    "<tap-repository>",
                    "rev-parse",
                    "--verify",
                    "HEAD",
                ),
                (
                    "lifecycle-harness",
                    "stage-formula-byte-equal",
                    str(inputs.current_formula),
                    "<tap-repository>/Formula/solet.rb",
                ),
                ("brew", "trust", "--formula", inputs.formula),
                ("brew", "trust", "--json=v1"),
            ),
        ),
        Phase(
            "clean-source-install",
            (
                ("brew", "style", inputs.formula),
                ("brew", "audit", "--strict", "--new", "--online", inputs.formula),
                ("env", "HOMEBREW_NO_INSTALL_UPGRADE=1", "brew", "install", inputs.python_formula),
                ("brew", "install", "--build-from-source", inputs.formula),
                ("brew", "test", inputs.formula),
                ("brew", "uninstall", inputs.formula),
            ),
        ),
        Phase(
            "clean-bottle-install",
            (
                ("brew", "install", "--force-bottle", str(inputs.current_bottle)),
                ("brew", "test", inputs.formula),
                ("brew", "uninstall", inputs.formula),
            ),
        ),
        Phase(
            "manager-and-lock-upgrade",
            (
                (
                    "lifecycle-harness",
                    "stage-formula-byte-equal",
                    str(inputs.previous_formula),
                    "<tap-repository>/Formula/solet.rb",
                ),
                ("brew", "install", "--build-from-source", inputs.formula),
                ("solet", "create", "--dry-run", "--json"),
                ("solet", "create", "--yes", "--approval-fingerprint", "<reviewed>"),
                (
                    "lifecycle-harness",
                    "stage-formula-byte-equal",
                    str(inputs.current_formula),
                    "<tap-repository>/Formula/solet.rb",
                ),
                ("brew", "upgrade", "--build-from-source", inputs.formula),
            ),
        ),
        Phase(
            "simulated-python-dependency-replacement",
            (
                ("brew", "list", "--versions", inputs.python_formula),
                ("brew", "reinstall", inputs.python_formula),
                ("brew", "list", "--versions", inputs.python_formula),
                ("brew", "reinstall", inputs.formula),
                ("solet", "--version"),
                ("solet", "status", inputs.instance_name, "--json"),
                ("solet", "doctor", inputs.instance_name, "--json"),
            ),
        ),
        Phase(
            "uninstall-reinstall-preservation",
            (
                ("brew", "uninstall", inputs.formula),
                ("brew", "install", "--build-from-source", inputs.formula),
                ("solet", "list", "--json"),
                ("solet", "status", inputs.instance_name, "--json"),
                ("solet", "doctor", inputs.instance_name, "--json"),
            ),
        ),
        Phase(
            "formula-scoped-brewfile",
            (
                ("brew", "uninstall", inputs.formula),
                ("brew", "bundle", "install", "--file", str(inputs.brewfile)),
                ("brew", "bundle", "check", "--file", str(inputs.brewfile)),
                ("solet", "--version"),
                ("brew", "--prefix", inputs.formula),
                ("brew", "test", inputs.formula),
                ("solet", "list", "--json"),
                ("solet", "status", inputs.instance_name, "--json"),
                ("solet", "doctor", inputs.instance_name, "--json"),
            ),
        ),
        Phase(
            "brewtestbot-github-actions",
            (
                (
                    "brew",
                    "test-bot",
                    "--only-formulae",
                    "--tap",
                    inputs.tap,
                    "--testing-formulae",
                    inputs.formula,
                ),
            ),
        ),
        *_existing_install_phases(inputs),
    )


def _existing_install_phases(inputs: Inputs) -> tuple[Phase, ...]:
    """Step 7 design section 8.4: the two VM-only phases.

    ``existing-install-import-update`` builds the section-3 real-style clone
    from the SEED archive the formula's lock names (``real_style_fixture.py``,
    never this checkout), imports it under keg N, upgrades the formula to N+1
    between import and update, updates through the runtime stage, reinstalls,
    and proves the pointer was released.  ``cold-host-degraded`` is the ONLY
    place true host absence across a process boundary is measured (section 4.2):
    ``python@3.13`` uninstalled (``host_python_313 missing``, ``update --dry-run``
    ``host_requirement_missing``), then the service booted out (``service_offline``
    unknowns; the section-7.3 single-colour refusal).  ``<reviewed>`` placeholders
    are resolved from the preceding ``--dry-run`` JSON exactly as
    ``_create_until_verified`` does; the canonical Manager name is
    ``solet-manager`` (governing section 10), never a bare ``solet`` verb.
    """
    name = f"{inputs.instance_name}-import"
    root = inputs.fixture_root / "existing-install"
    clone = str(root / "target")
    fixture_builder = str(Path(__file__).resolve().parent / "real_style_fixture.py")
    stage_previous = (
        "lifecycle-harness",
        "stage-formula-byte-equal",
        str(inputs.previous_formula),
        "<tap-repository>/Formula/solet.rb",
    )
    stage_current = (
        "lifecycle-harness",
        "stage-formula-byte-equal",
        str(inputs.current_formula),
        "<tap-repository>/Formula/solet.rb",
    )
    return (
        Phase(
            "existing-install-import-update",
            (
                stage_previous,
                ("brew", "install", "--build-from-source", inputs.formula),
                ("python3", fixture_builder, "--root", str(root), "--name", name),
                ("solet-manager", "inspect", "--target", clone, "--channel", "stable", "--json"),
                ("solet-manager", "import", name, "--target", clone, "--channel", "stable", "--dry-run", "--json"),
                ("solet-manager", "import", name, "--target", clone, "--channel", "stable", "--yes", "--approval-fingerprint", "<reviewed>", "--json"),
                ("solet-manager", "doctor", name, "--json"),
                stage_current,
                ("brew", "upgrade", "--build-from-source", inputs.formula),
                ("solet-manager", "update", name, "--dry-run", "--json"),
                ("solet-manager", "update", name, "--yes", "--approval-fingerprint", "<reviewed>", "--json"),
                ("solet-manager", "update", name, "--dry-run", "--json"),
                ("solet-manager", "update", name, "--yes", "--approval-fingerprint", "<reviewed-runtime>", "--json"),
                ("solet-manager", "doctor", name, "--json"),
                ("brew", "reinstall", inputs.formula),
                ("solet-manager", "doctor", name, "--json"),
                ("solet-manager", "reconcile", name, "--dry-run", "--json"),
            ),
        ),
        Phase(
            "cold-host-degraded",
            (
                ("brew", "uninstall", "--ignore-dependencies", inputs.python_formula),
                ("solet-manager", "doctor", name, "--json"),
                ("solet-manager", "update", name, "--dry-run", "--json"),
                ("brew", "install", inputs.python_formula),
                ("launchctl", "bootout", f"gui/<uid>/local.solet.{name}"),
                ("solet-manager", "doctor", name, "--json"),
                ("solet-manager", "update", name, "--dry-run", "--json"),
            ),
        ),
    )




def execute(inputs: Inputs) -> None:
    _require_fresh_ci()
    _validate_release_inputs(inputs)
    fixture = _prepare_fixture(inputs)
    runner = Runner(_environment(fixture))
    tap_formula = _prepare_local_tap(inputs, runner)
    stage_formula(inputs.current_formula, tap_formula)
    _trust_formula(inputs, runner)
    _run_clean_source(inputs, fixture, runner)
    _run_clean_bottle(inputs, fixture, runner)
    _run_previous_create_and_upgrade(inputs, fixture, tap_formula, runner)
    _run_simulated_python_replacement(inputs, fixture, runner)
    _run_uninstall_reinstall(inputs, fixture, runner)
    _run_unmanaged_discovery(inputs, fixture, runner)
    _run_brewfile_and_testbot(inputs, fixture, tap_formula.parents[1], runner)
    _run_existing_install_import_update(inputs, fixture, tap_formula, runner)
    _run_cold_host_degraded(inputs, fixture, runner)
    print(f"Homebrew lifecycle acceptance PASSED; fixture retained at {fixture.root}")


def _require_fresh_ci() -> None:
    if (
        os.environ.get("CI") != "true"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("SOLET_ACCEPT_LIFECYCLE_MUTATION") != "1"
    ):
        raise RuntimeError(
            "lifecycle execution requires CI=true, GITHUB_ACTIONS=true, and "
            "SOLET_ACCEPT_LIFECYCLE_MUTATION=1 on a disposable GitHub Actions runner"
        )


def _validate_release_inputs(inputs: Inputs) -> None:
    if inputs.previous_lock_sha256 == inputs.current_lock_sha256:
        raise RuntimeError("previous/current installed seed-lock checksums must differ")
    expected_formula = f"{inputs.tap}/solet"
    if inputs.formula != expected_formula:
        raise RuntimeError(
            "formula must be the fully qualified name for the disposable tap: "
            f"expected {expected_formula!r}, got {inputs.formula!r}"
        )
    for label, path in (
        ("previous Formula", inputs.previous_formula),
        ("current Formula", inputs.current_formula),
    ):
        if not path.is_file():
            raise RuntimeError(f"{label} source artifact is absent: {path}")


def _prepare_local_tap(inputs: Inputs, runner: Runner) -> Path:
    runner.run(["brew", "tap-new", inputs.tap])
    repository = Path(runner.run(["brew", "--repository", inputs.tap]).stdout.strip())
    if not repository.is_dir():
        raise RuntimeError(f"disposable tap repository is absent: {repository}")
    runner.run(["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"])
    return repository / "Formula" / "solet.rb"


def stage_formula(source: Path, destination: Path) -> None:
    """Stage one reviewed Formula artifact and prove byte equality."""
    if not source.is_file():
        raise RuntimeError(f"Formula source artifact is absent: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if source.read_bytes() != destination.read_bytes():
        raise RuntimeError(f"staged Formula differs from reviewed source artifact: {source}")


def _trust_formula(inputs: Inputs, runner: Runner) -> None:
    runner.run(["brew", "trust", "--formula", inputs.formula])
    trust = runner.json(["brew", "trust", "--json=v1"])
    formulae_value = trust.get("formulae")
    formulae = cast(list[object], formulae_value) if isinstance(formulae_value, list) else []
    taps_value = trust.get("taps")
    taps = cast(list[object], taps_value) if isinstance(taps_value, list) else []
    if inputs.formula.casefold() not in {str(item).casefold() for item in formulae}:
        raise RuntimeError(f"Formula-scoped Homebrew trust was not recorded: {trust}")
    if inputs.tap.casefold() in {str(item).casefold() for item in taps}:
        raise RuntimeError(f"disposable tap was trusted wholesale instead of by Formula: {trust}")


def _prepare_fixture(inputs: Inputs) -> Fixture:
    root = inputs.fixture_root.expanduser().resolve(strict=False)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"fixture root must be absent or empty: {root}")
    if root in {Path.home().resolve(), Path("/")}:
        raise RuntimeError(f"fixture root is unsafe: {root}")
    home = root / "home"
    manager = root / "manager"
    target = home / "Solets" / inputs.instance_name
    config = root / "create.toml"
    home.mkdir(parents=True, exist_ok=True)
    manager.mkdir(parents=True, exist_ok=True)
    render_create_config(inputs.create_config_template, config, inputs.instance_name, target)
    return Fixture(root, home, manager, target, config)


def render_create_config(template: Path, destination: Path, name: str, target: Path) -> None:
    rendered = template.read_text(encoding="utf-8")
    rendered = rendered.replace("{{INSTANCE_NAME}}", name)
    rendered = rendered.replace("{{INSTANCE_TARGET}}", str(target))
    if "{{" in rendered or "}}" in rendered:
        raise RuntimeError(f"unresolved lifecycle config marker in {template}")
    destination.write_text(rendered, encoding="utf-8")


def _environment(fixture: Fixture) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(fixture.home),
            "SOLET_HOME": str(fixture.manager),
            "XDG_CONFIG_HOME": str(fixture.home / ".config"),
            "XDG_STATE_HOME": str(fixture.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(fixture.home / ".cache"),
            "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_INSTALL_CLEANUP": "1",
        }
    )
    return environment


def _run_clean_source(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    runner.run(["brew", "style", inputs.formula])
    runner.run(["brew", "audit", "--strict", "--new", "--online", inputs.formula])
    # The Formula declares no Python dependency (r61), so the runner supplies the
    # interpreter the documented way: installed if missing, never upgraded.
    runner.run(["env", "HOMEBREW_NO_INSTALL_UPGRADE=1", "brew", "install", inputs.python_formula])
    runner.run(["brew", "install", "--build-from-source", inputs.formula])
    runner.run(["brew", "test", inputs.formula])
    _assert_version(runner, inputs.current_version)
    _assert_lock(runner, inputs.formula, inputs.current_lock_sha256)
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)
    runner.run(["brew", "uninstall", inputs.formula])
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)


def _run_clean_bottle(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    if not inputs.current_bottle.is_file():
        raise RuntimeError(f"current bottle is absent: {inputs.current_bottle}")
    runner.run(["brew", "install", "--force-bottle", str(inputs.current_bottle)])
    runner.run(["brew", "test", inputs.formula])
    _assert_version(runner, inputs.current_version)
    _assert_lock(runner, inputs.formula, inputs.current_lock_sha256)
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)
    runner.run(["brew", "uninstall", inputs.formula])
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)


def _run_previous_create_and_upgrade(
    inputs: Inputs,
    fixture: Fixture,
    tap_formula: Path,
    runner: Runner,
) -> None:
    stage_formula(inputs.previous_formula, tap_formula)
    runner.run(["brew", "install", "--build-from-source", inputs.formula])
    _assert_version(runner, inputs.previous_version)
    _assert_lock(runner, inputs.formula, inputs.previous_lock_sha256)
    _create_until_verified(inputs, fixture, runner)
    _assert_managed_health(inputs, fixture, runner)
    preserved = preserved_roots(fixture)
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)
    before = snapshot_tree(preserved)
    stage_formula(inputs.current_formula, tap_formula)
    runner.run(["brew", "upgrade", "--build-from-source", inputs.formula])
    assert_unchanged(before, preserved, "N to N+1 upgrade")
    _assert_version(runner, inputs.current_version)
    _assert_lock(runner, inputs.formula, inputs.current_lock_sha256)
    _assert_managed_health(inputs, fixture, runner)
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)


def _run_simulated_python_replacement(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    preserved = preserved_roots(fixture)
    before = snapshot_tree(preserved)
    previous_python = _installed_formula_version(runner, inputs.python_formula)
    print(
        f"Simulating Homebrew dependency replacement with brew reinstall "
        f"{inputs.python_formula}; no Python version change is claimed",
        flush=True,
    )
    runner.run(["brew", "reinstall", inputs.python_formula])
    current_python = _installed_formula_version(runner, inputs.python_formula)
    print(
        f"Simulated dependency replacement observed before={previous_python!r}, "
        f"after={current_python!r}",
        flush=True,
    )
    runner.run(["brew", "reinstall", inputs.formula])
    assert_unchanged(before, preserved, "simulated Python dependency replacement and repair")
    _assert_version(runner, inputs.current_version)
    _assert_managed_health(inputs, fixture, runner)
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)


def _run_uninstall_reinstall(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    preserved = preserved_roots(fixture)
    before = snapshot_tree(preserved)
    runner.run(["brew", "uninstall", inputs.formula])
    assert_unchanged(before, preserved, "formula uninstall")
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)
    runner.run(["brew", "install", "--build-from-source", inputs.formula])
    assert_unchanged(before, preserved, "formula reinstall")
    _assert_managed_health(inputs, fixture, runner)
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)


def _run_unmanaged_discovery(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    registry = fixture.manager / "config" / "instances.json"
    saved_registry = fixture.root / "instances.json.saved"
    registry.replace(saved_registry)
    try:
        result = _solet_json(runner, fixture, ["status", inputs.instance_name, "--json"], (3,))
        assert_unmanaged(result, inputs.instance_name)
    finally:
        saved_registry.replace(registry)
    manual_name = f"{inputs.instance_name}-manual"
    (fixture.home / "Solets" / manual_name).mkdir(parents=True)
    result = _solet_json(runner, fixture, ["status", manual_name, "--json"], (3,))
    assert_unmanaged(result, manual_name)


def _run_brewfile_and_testbot(
    inputs: Inputs,
    fixture: Fixture,
    tap_repository: Path,
    runner: Runner,
) -> None:
    if not inputs.brewfile.is_file():
        raise RuntimeError(f"Brewfile is absent: {inputs.brewfile}")
    preserved = preserved_roots(fixture)
    before = snapshot_tree(preserved)
    runner.run(["brew", "uninstall", inputs.formula])
    assert_unchanged(before, preserved, "pre-Brewfile formula uninstall")
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)
    runner.run(["brew", "bundle", "install", "--file", str(inputs.brewfile)])
    runner.run(["brew", "bundle", "check", "--file", str(inputs.brewfile)])
    assert_unchanged(before, preserved, "Brewfile formula install")
    _assert_version(runner, inputs.current_version)
    _assert_lock(runner, inputs.formula, inputs.current_lock_sha256)
    runner.run(["brew", "test", inputs.formula])
    _assert_managed_health(inputs, fixture, runner)
    _assert_no_homebrew_keg_paths(inputs, fixture, runner)
    runner.run(
        [
            "brew",
            "test-bot",
            "--only-formulae",
            "--tap",
            inputs.tap,
            "--testing-formulae",
            inputs.formula,
        ],
        cwd=tap_repository,
    )


def _run_existing_install_import_update(inputs: Inputs, fixture: Fixture, tap_formula: Path, runner: Runner) -> None:
    """Section 8.4, phase ``existing-install-import-update`` on the disposable runner."""
    name = f"{inputs.instance_name}-import"
    root = inputs.fixture_root / "existing-install"
    clone = root / "target"
    stage_formula(inputs.previous_formula, tap_formula)
    runner.run(["brew", "install", "--build-from-source", inputs.formula])
    runner.run(["python3", str(Path(__file__).resolve().parent / "real_style_fixture.py"), "--root", str(root), "--name", name])
    inspected = _manager_json(runner, ["inspect", "--target", str(clone), "--channel", "stable", "--json"], (3,))
    _require(inspected, "existing_install_inspection", "attention_required")
    preview = _manager_json(runner, ["import", name, "--target", str(clone), "--channel", "stable", "--dry-run", "--json"])
    fingerprint = _fingerprint(preview, "approval_fingerprint")
    imported = _manager_json(runner, ["import", name, "--target", str(clone), "--channel", "stable", "--yes", "--approval-fingerprint", fingerprint, "--json"])
    _require(imported, "existing_install_import", "imported")
    _manager_json(runner, ["doctor", name, "--json"], (0, 3))
    stage_formula(inputs.current_formula, tap_formula)
    runner.run(["brew", "upgrade", "--build-from-source", inputs.formula])
    preview = _manager_json(runner, ["update", name, "--dry-run", "--json"])
    _require(preview, "existing_install_update", "preview_ready")
    advanced = _manager_json(runner, ["update", name, "--yes", "--approval-fingerprint", _fingerprint(preview, "approval_fingerprint"), "--json"])
    _require(advanced, "existing_install_update", "source_advanced")
    runtime = _manager_json(runner, ["update", name, "--dry-run", "--json"])
    _require(runtime, "existing_install_update", "runtime_preview_ready")
    promoted = _manager_json(runner, ["update", name, "--yes", "--approval-fingerprint", _fingerprint(runtime, "runtime_approval_fingerprint"), "--json"])
    _require(promoted, "existing_install_update", "promoted")
    verified = _manager_json(runner, ["doctor", name, "--json"])
    _require(verified, "existing_install_doctor", "verified")
    runner.run(["brew", "reinstall", inputs.formula])
    verified = _manager_json(runner, ["doctor", name, "--json"])
    _require(verified, "existing_install_doctor", "verified")
    released = _manager_json(runner, ["reconcile", name, "--dry-run", "--json"], (3,))
    if released.get("kind") != "existing_install_reconcile" or released.get("error_kind") != "no_active_update":
        raise RuntimeError(f"reconcile after promotion did not report the released pointer: {released}")


def _run_cold_host_degraded(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    """Section 8.4, phase ``cold-host-degraded``: the only measurement of true host absence across a process boundary."""
    name = f"{inputs.instance_name}-import"
    runner.run(["brew", "uninstall", "--ignore-dependencies", inputs.python_formula])
    try:
        degraded = _manager_json(runner, ["doctor", name, "--json"], (3,))
        _require_check(degraded, "host_python_313", "missing")
        refused = _manager_json(runner, ["update", name, "--dry-run", "--json"], (3,))
        if refused.get("error_kind") != "host_requirement_missing":
            raise RuntimeError(f"update --dry-run without host Python 3.13 did not refuse host_requirement_missing: {refused}")
    finally:
        runner.run(["brew", "install", inputs.python_formula])
    runner.run(["launchctl", "bootout", f"gui/{os.getuid()}/local.solet.{name}"], allowed=(0, 3, 113))
    offline = _manager_json(runner, ["doctor", name, "--json"], (3,))
    _require_check(offline, "bridge_health", "unknown")
    # After promotion the keg's lock names the installed release, so a zero-delta
    # preview is `already_current` before any runtime plan is rendered; a lock
    # naming a newer release reaches the section-7.3 single-colour observation
    # and is refused `runtime_plan_blocked` with `service_offline_before_transition`.
    refused = _manager_json(runner, ["update", name, "--dry-run", "--json"], (0, 3))
    if refused.get("status") == "already_current":
        return
    data = refused.get("data")
    lifecycle = cast(JsonObject, data).get("lifecycle") if isinstance(data, dict) else None
    unproven = cast(JsonObject, lifecycle).get("unproven_reason") if isinstance(lifecycle, dict) else None
    if refused.get("error_kind") != "runtime_plan_blocked" or unproven != "service_offline_before_transition":
        raise RuntimeError(f"update --dry-run with the service offline neither reported already_current nor refused the single-colour transition: {refused}")


def _manager_json(runner: Runner, arguments: list[str], allowed: tuple[int, ...] = (0,)) -> JsonObject:
    return runner.json(["solet-manager", *arguments], allowed=allowed)


def _require(result: JsonObject, kind: str, status: str) -> None:
    if result.get("kind") != kind or result.get("status") != status:
        raise RuntimeError(f"expected {kind} {status}, got {result.get('kind')} {result.get('status')} ({result.get('error_kind')})")


def _require_check(result: JsonObject, check_id: str, status: str) -> None:
    data = result.get("data")
    sections = cast(list[JsonObject], cast(JsonObject, data).get("sections", [])) if isinstance(data, dict) else []
    for section in sections:
        for check in cast(list[JsonObject], section.get("checks", [])):
            if check.get("check_id") == check_id:
                if check.get("status") != status:
                    raise RuntimeError(f"doctor check {check_id} is {check.get('status')}, expected {status}")
                return
    raise RuntimeError(f"doctor check {check_id} is absent")


def _fingerprint(result: JsonObject, key: str) -> str:
    data = result.get("data")
    value = cast(JsonObject, data).get(key) if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise RuntimeError(f"preview carries no {key}: {result}")
    return value


def _create_until_verified(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    for _attempt in range(16):
        preview = _solet_json(
            runner,
            fixture,
            ["create", "--config", str(fixture.config), "--dry-run", "--json"],
            (0, 3),
        )
        if preview.get("status") == "verified":
            return
        data_value = preview.get("data")
        data = cast(JsonObject, data_value) if isinstance(data_value, dict) else {}
        fingerprint = data.get("approval_fingerprint")
        if preview.get("status") != "preview_ready" or not isinstance(fingerprint, str):
            raise RuntimeError(f"create did not produce an approvable preview: {preview}")
        applied = _solet_json(
            runner,
            fixture,
            [
                "create",
                "--config",
                str(fixture.config),
                "--yes",
                "--approval-fingerprint",
                fingerprint,
                "--json",
            ],
            (0, 3),
        )
        if applied.get("status") == "verified":
            return
    raise RuntimeError("create did not verify within 16 reviewed frontier rounds")


def _assert_managed_health(inputs: Inputs, fixture: Fixture, runner: Runner) -> None:
    listed = _solet_json(runner, fixture, ["list", "--json"])
    data_value = listed.get("data")
    data = cast(JsonObject, data_value) if isinstance(data_value, dict) else {}
    instances_value = data.get("instances")
    instances = cast(list[object], instances_value) if isinstance(instances_value, list) else []
    names: set[object] = set()
    for item_value in instances:
        if isinstance(item_value, dict):
            names.add(cast(JsonObject, item_value).get("name"))
    if inputs.instance_name not in names:
        raise RuntimeError(f"preserved instance absent from registry: {listed}")
    status = _solet_json(runner, fixture, ["status", inputs.instance_name, "--json"])
    doctor = _solet_json(runner, fixture, ["doctor", inputs.instance_name, "--json"])
    if status.get("status") != "verified" or doctor.get("status") != "verified":
        raise RuntimeError(f"preserved instance is not healthy: status={status}, doctor={doctor}")


def assert_unmanaged(result: JsonObject, name: str) -> None:
    data_value = result.get("data")
    data = cast(JsonObject, data_value) if isinstance(data_value, dict) else None
    if (
        result.get("kind") != "instance_status"
        or result.get("error_kind") != "instance_unmanaged"
        or not isinstance(data, dict)
        or data.get("name") != name
        or data.get("managed") is not False
    ):
        raise RuntimeError(f"unmanaged instance did not return the typed boundary: {result}")


def _solet_json(
    runner: Runner,
    fixture: Fixture,
    arguments: list[str],
    allowed: tuple[int, ...] = (0,),
) -> JsonObject:
    return runner.json(["solet", "--home", str(fixture.manager), *arguments], allowed=allowed)


def _assert_version(runner: Runner, expected: str) -> None:
    actual = runner.run(["solet", "--version"]).stdout.strip()
    if actual != f"solet {expected}":
        raise RuntimeError(f"manager version mismatch: expected solet {expected}, got {actual!r}")


def _assert_lock(runner: Runner, formula: str, expected_sha256: str) -> None:
    prefix = Path(runner.run(["brew", "--prefix", formula]).stdout.strip())
    lock = prefix / "libexec" / "share" / "solet" / "seed.lock.json"
    actual = _sha256(lock)
    if actual != expected_sha256:
        raise RuntimeError(f"installed seed lock mismatch: expected {expected_sha256}, got {actual}")


def _formula_cellar(runner: Runner, formula: str) -> Path:
    return _canonicalize_cellar_root(runner.run(["brew", "--cellar", formula]).stdout)


def _canonicalize_cellar_root(reported_root: str) -> Path:
    """Return one safe canonical Homebrew Cellar root.

    Homebrew owns the value, but it is still external input to the preservation
    guard.  Reject a relative, root-wide, or cyclic value before it becomes a
    prefix that could either miss a keg or match unrelated preserved state.
    """
    raw_root = reported_root.strip()
    if not raw_root:
        raise RuntimeError("Homebrew returned an empty Cellar root")
    candidate = Path(raw_root)
    if not candidate.is_absolute():
        raise RuntimeError(f"Homebrew returned a relative Cellar root: {raw_root!r}")
    canonical = _normalize_path(candidate, f"Homebrew Cellar root {raw_root!r}")
    if canonical == Path("/"):
        raise RuntimeError(f"Homebrew returned an unsafe Cellar root: {raw_root!r}")
    return canonical


def forbidden_homebrew_prefixes(runner: Runner, inputs: Inputs) -> tuple[Path, ...]:
    """Resolve the closed set of Formula-owned physical Cellar prefixes."""
    roots = tuple(_formula_cellar(runner, formula) for formula in (inputs.formula, inputs.python_formula))
    if len(set(roots)) != 2:
        raise RuntimeError(f"Solet and Python Cellar roots must be distinct: {roots}")
    return roots


def _assert_no_homebrew_keg_paths(
    inputs: Inputs,
    fixture: Fixture,
    runner: Runner,
) -> None:
    assert_no_keg_paths(preserved_roots(fixture), forbidden_homebrew_prefixes(runner, inputs))


def _installed_formula_version(runner: Runner, formula: str) -> str:
    value = runner.run(["brew", "list", "--versions", formula]).stdout.strip()
    if not value:
        raise RuntimeError(f"{formula} has no installed version")
    return value


def preserved_roots(fixture: Fixture) -> tuple[Path, ...]:
    """Return only instance-owned state whose bytes must survive Homebrew work."""
    return (
        fixture.target,
        fixture.manager,
        fixture.home / ".local" / "bin",
        fixture.home / "Library" / "LaunchAgents",
    )


def snapshot_tree(roots: tuple[Path, ...]) -> dict[str, SnapshotEntry]:
    entries: dict[str, SnapshotEntry] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            key = str(path)
            if path.is_symlink():
                entries[key] = SnapshotEntry("symlink", os.readlink(path))
            elif path.is_file():
                entries[key] = SnapshotEntry("file", _sha256(path))
            elif path.is_dir():
                entries[key] = SnapshotEntry("directory", "")
    return entries


def assert_unchanged(
    before: dict[str, SnapshotEntry],
    roots: tuple[Path, ...],
    operation: str,
) -> None:
    after = snapshot_tree(roots)
    if before != after:
        changed = sorted(set(before) ^ set(after))
        changed.extend(key for key in set(before) & set(after) if before[key] != after[key])
        raise RuntimeError(f"{operation} changed preserved instance state: {changed[:20]}")


def assert_no_keg_paths(
    roots: tuple[Path, ...],
    forbidden_roots: tuple[Path, ...],
) -> None:
    _validate_forbidden_roots(forbidden_roots)
    markers = tuple(str(root).encode() for root in forbidden_roots)
    for root in roots:
        for path in root.rglob("*"):
            if _contains_marker(_scan_values(path), markers):
                raise RuntimeError(
                    f"forbidden Homebrew keg path leaked into preserved instance state: {path}"
                )


def _validate_forbidden_roots(forbidden_roots: tuple[Path, ...]) -> None:
    if (
        len(forbidden_roots) != 2
        or len(set(forbidden_roots)) != 2
        or any(not root.is_absolute() or root == Path("/") for root in forbidden_roots)
    ):
        raise RuntimeError(
            "forbidden Homebrew roots must be the closed, distinct absolute Solet/Python pair"
        )


def _scan_values(path: Path) -> tuple[bytes, ...]:
    name = os.fsencode(path)
    if path.is_symlink():
        raw_target = os.readlink(path)
        return (
            name,
            os.fsencode(raw_target),
            os.fsencode(_normalized_symlink_destination(path, raw_target)),
        )
    if path.is_file():
        return (name, path.read_bytes())
    return (name,)


def _normalized_symlink_destination(path: Path, raw_target: str) -> str:
    target = Path(raw_target)
    destination = target if target.is_absolute() else path.parent / target
    return str(_normalize_path(destination, f"preserved symlink target {path}"))


def _normalize_path(path: Path, description: str) -> Path:
    try:
        canonical = path.resolve(strict=False)
    except RuntimeError as exc:
        raise RuntimeError(f"cannot normalize {description}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot normalize {description}") from exc
    try:
        path.resolve(strict=True)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"cannot normalize cyclic {description}") from exc
        raise RuntimeError(f"cannot normalize {description}") from exc
    return canonical


def _contains_marker(values: tuple[bytes, ...], markers: tuple[bytes, ...]) -> bool:
    return any(_contains_root(value, marker) for marker in markers for value in values)


def _contains_root(value: bytes, root: bytes) -> bool:
    """Match an exact Cellar root or a descendant, never a near-prefix decoy."""
    start = value.find(root)
    while start != -1:
        end = start + len(root)
        if end == len(value) or value[end : end + 1] == b"/":
            return True
        start = value.find(root, start + 1)
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tap", required=True)
    parser.add_argument("--formula", required=True)
    parser.add_argument("--previous-formula", type=Path, required=True)
    parser.add_argument("--current-formula", type=Path, required=True)
    parser.add_argument("--current-bottle", type=Path, required=True)
    parser.add_argument("--previous-version", required=True)
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--previous-lock-sha256", required=True)
    parser.add_argument("--current-lock-sha256", required=True)
    parser.add_argument("--brewfile", type=Path, required=True)
    parser.add_argument("--create-config-template", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--instance-name", default="lifecycle-bizops")
    parser.add_argument("--python-formula", default="python@3.13")
    parser.add_argument("--plan-only", action="store_true")
    return parser


def _inputs(namespace: argparse.Namespace) -> Inputs:
    return Inputs(
        tap=namespace.tap,
        formula=namespace.formula,
        previous_formula=namespace.previous_formula,
        current_formula=namespace.current_formula,
        current_bottle=namespace.current_bottle,
        previous_version=namespace.previous_version,
        current_version=namespace.current_version,
        previous_lock_sha256=namespace.previous_lock_sha256,
        current_lock_sha256=namespace.current_lock_sha256,
        brewfile=namespace.brewfile,
        create_config_template=namespace.create_config_template,
        fixture_root=namespace.fixture_root,
        instance_name=namespace.instance_name,
        python_formula=namespace.python_formula,
    )


def main() -> int:
    namespace = _parser().parse_args()
    inputs = _inputs(namespace)
    if namespace.plan_only:
        print(
            json.dumps(
                [
                    {"phase": phase.name, "commands": [list(command) for command in phase.commands]}
                    for phase in build_plan(inputs)
                ],
                indent=2,
            )
        )
        return 0
    execute(inputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
