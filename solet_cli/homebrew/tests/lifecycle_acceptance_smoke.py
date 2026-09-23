"""Offline control-flow and preservation smoke for Homebrew lifecycle acceptance."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from ci.lifecycle_acceptance import (  # noqa: E402
    Fixture,
    Inputs,
    Phase,
    assert_no_keg_paths,
    assert_unchanged,
    assert_unmanaged,
    build_plan,
    preserved_roots,
    render_create_config,
    snapshot_tree,
    stage_formula,
)

_checks = 0


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _raises(callback: Callable[[], object], label: str) -> None:
    try:
        callback()
    except RuntimeError:
        _check(True, label)
    else:
        _check(False, label)


def _inputs(root: Path) -> Inputs:
    return Inputs(
        tap="solet-public/tap",
        formula="solet-public/tap/solet",
        previous_formula=root / "previous" / "Formula" / "solet.rb",
        current_formula=root / "current" / "Formula" / "solet.rb",
        current_bottle=root / "solet--0.2.0.arm64.bottle.tar.gz",
        previous_version="0.1.0",
        current_version="0.2.0",
        previous_lock_sha256="a" * 64,
        current_lock_sha256="b" * 64,
        brewfile=_ROOT / "ci" / "Brewfile.enterprise.example",
        create_config_template=_ROOT / "ci" / "lifecycle_config.toml.template",
        fixture_root=root / "fixture",
        instance_name="lifecycle-bizops",
        python_formula="python@3.13",
    )


def _check_plan(root: Path) -> None:
    inputs = _inputs(root)
    phases = build_plan(inputs)
    names = tuple(phase.name for phase in phases)
    _check(
        names
        == (
            "prepare-disposable-local-tap",
            "clean-source-install",
            "clean-bottle-install",
            "manager-and-lock-upgrade",
            "simulated-python-dependency-replacement",
            "uninstall-reinstall-preservation",
            "formula-scoped-brewfile",
            "brewtestbot-github-actions",
            "existing-install-import-update",
            "cold-host-degraded",
        ),
        "release lifecycle phase order is closed and complete",
    )
    commands = [command for phase in phases for command in phase.commands]
    _check_required_commands(commands)
    _check_name_based_commands(inputs, commands)
    _check_formula_trust_order(phases)
    _check_upgrade_sequence(inputs, phases)
    _check_python_replacement(commands, phases)
    _check_brewfile_sequence(phases)
    _check_import_update_sequence(inputs, phases)
    _check_degraded_sequence(inputs, phases)


def _check_required_commands(commands: list[tuple[str, ...]]) -> None:
    command_text = "\n".join(" ".join(command) for command in commands)
    required = (
        "brew tap-new solet-public/tap",
        "git -C <tap-repository> rev-parse --verify HEAD",
        "lifecycle-harness stage-formula-byte-equal",
        "brew trust --formula solet-public/tap/solet",
        "brew trust --json=v1",
        "brew style",
        "brew audit --strict --new --online",
        "brew install --build-from-source",
        "brew install --force-bottle",
        "brew test solet-public/tap/solet",
        "brew upgrade --build-from-source",
        "brew reinstall python@3.13",
        "brew list --versions python@3.13",
        "brew reinstall solet-public/tap/solet",
        "brew uninstall",
        "solet list --json",
        "solet status lifecycle-bizops --json",
        "solet doctor lifecycle-bizops --json",
        "brew bundle install --file",
        "brew bundle check --file",
        "solet --version",
        "brew --prefix solet-public/tap/solet",
        "brew test-bot --only-formulae --tap solet-public/tap "
        "--testing-formulae solet-public/tap/solet",
    )
    _check(all(item in command_text for item in required), "plan names every required real command")


def _check_name_based_commands(inputs: Inputs, commands: list[tuple[str, ...]]) -> None:
    source_paths = {str(inputs.previous_formula), str(inputs.current_formula)}
    brew_commands = [command for command in commands if command[0] == "brew"]
    _check(
        all(not source_paths.intersection(command) for command in brew_commands),
        "no planned brew command receives a source Formula path",
    )


def _check_formula_trust_order(phases: tuple[Phase, ...]) -> None:
    prepare = next(phase for phase in phases if phase.name == "prepare-disposable-local-tap")
    _check(
        prepare.commands[-2]
        == ("brew", "trust", "--formula", "solet-public/tap/solet")
        and prepare.commands[-1] == ("brew", "trust", "--json=v1"),
        "Formula-scoped trust is recorded and inspected before style/audit",
    )


def _check_upgrade_sequence(inputs: Inputs, phases: tuple[Phase, ...]) -> None:
    upgrade = next(phase for phase in phases if phase.name == "manager-and-lock-upgrade")
    _check(
        upgrade.commands[0][1:3]
        == ("stage-formula-byte-equal", str(inputs.previous_formula))
        and upgrade.commands[1]
        == ("brew", "install", "--build-from-source", "solet-public/tap/solet")
        and upgrade.commands[4][1:3]
        == ("stage-formula-byte-equal", str(inputs.current_formula))
        and upgrade.commands[5]
        == ("brew", "upgrade", "--build-from-source", "solet-public/tap/solet"),
        "N to N+1 stages previous then current bytes around name-based install and upgrade",
    )


def _check_python_replacement(
    commands: list[tuple[str, ...]],
    phases: tuple[Phase, ...],
) -> None:
    python_replacement = next(
        phase for phase in phases if phase.name == "simulated-python-dependency-replacement"
    )
    _check(
        ("brew", "reinstall", "python@3.13") in python_replacement.commands
        and all(command[:3] != ("brew", "upgrade", "python@3.13") for command in commands),
        "Python dependency replacement is deterministic and not called a version upgrade",
    )


def _check_brewfile_sequence(phases: tuple[Phase, ...]) -> None:
    brewfile = next(phase for phase in phases if phase.name == "formula-scoped-brewfile")
    _check(
        brewfile.commands[0] == ("brew", "uninstall", "solet-public/tap/solet")
        and brewfile.commands[1][:3] == ("brew", "bundle", "install"),
        "Brewfile phase uninstalls first so bundle performs a real install",
    )


def _manager_verbs(commands: tuple[tuple[str, ...], ...]) -> list[str]:
    return [command[1] for command in commands if command[0] == "solet-manager"]


def _indices(commands: tuple[tuple[str, ...], ...], verb: str, flag: str) -> list[int]:
    return [index for index, command in enumerate(commands) if command[:2] == ("solet-manager", verb) and flag in command]


def _check_import_update_sequence(inputs: Inputs, phases: tuple[Phase, ...]) -> None:
    """Step 7 design section 8.4: import before the keg upgrade, a doctor after every mutation, reinstall before the last doctor, no bare ``solet`` verb."""
    phase = next(phase for phase in phases if phase.name == "existing-install-import-update")
    commands = phase.commands
    name = f"{inputs.instance_name}-import"
    _check(all(command[0] != "solet" for command in commands), "existing-install phase names the Manager as solet-manager, never a bare solet verb")
    _check(all(command[-1] == "--json" for command in commands if command[0] == "solet-manager"), "every Manager command is machine-readable")
    _check(_manager_verbs(commands) == ["inspect", "import", "import", "doctor", "update", "update", "update", "update", "doctor", "doctor", "reconcile"], f"Manager verb order: {_manager_verbs(commands)}")
    _check_import_prelude(inputs, commands, name)
    _check_import_before_upgrade(inputs, commands, name)
    _check_update_pairs(commands, name)
    _check_reinstall_tail(inputs, commands, name)


def _check_import_prelude(inputs: Inputs, commands: tuple[tuple[str, ...], ...], name: str) -> None:
    staged = commands[0][:3] == ("lifecycle-harness", "stage-formula-byte-equal", str(inputs.previous_formula))
    _check(staged and commands[1] == ("brew", "install", "--build-from-source", inputs.formula), "the previous formula is staged and installed before the clone exists")
    builder = commands[2]
    _check(builder[0] == "python3" and builder[1].endswith("/real_style_fixture.py"), "the clone is built by real_style_fixture.py")
    _check(builder[2:] == ("--root", str(inputs.fixture_root / "existing-install"), "--name", name), "the builder receives the fixture root and the instance name (the installed lock, not this checkout, is its source)")


def _check_import_before_upgrade(inputs: Inputs, commands: tuple[tuple[str, ...], ...], name: str) -> None:
    text = [" ".join(command) for command in commands]
    upgrade = text.index(f"brew upgrade --build-from-source {inputs.formula}")
    import_yes = _indices(commands, "import", "--yes")[0]
    _check(import_yes < upgrade and text.index(f"solet-manager doctor {name} --json") == import_yes + 1, "import (with its doctor) completes under keg N before the upgrade to N+1")
    _check(commands[upgrade - 1][:3] == ("lifecycle-harness", "stage-formula-byte-equal", str(inputs.current_formula)), "the current formula is staged immediately before the upgrade")


def _check_update_pairs(commands: tuple[tuple[str, ...], ...], name: str) -> None:
    dry_runs = _indices(commands, "update", "--dry-run")
    applies = _indices(commands, "update", "--yes")
    _check(len(dry_runs) == 2 and len(applies) == 2, f"two update previews and two applies: {dry_runs} {applies}")
    _check(all(dry < apply == dry + 1 for dry, apply in zip(dry_runs, applies, strict=True)), "each update --yes immediately follows the --dry-run whose fingerprint it cites")
    _check(commands[applies[0]][-2] == "<reviewed>" and commands[applies[1]][-2] == "<reviewed-runtime>", "the source and runtime approvals cite their own placeholders")
    _check(commands[applies[1] + 1] == ("solet-manager", "doctor", name, "--json"), "a doctor follows the promoting update")


def _check_reinstall_tail(inputs: Inputs, commands: tuple[tuple[str, ...], ...], name: str) -> None:
    text = [" ".join(command) for command in commands]
    reinstall = text.index(f"brew reinstall {inputs.formula}")
    _check(commands[reinstall + 1] == ("solet-manager", "doctor", name, "--json"), "reinstall precedes the last doctor")
    _check(commands[-1] == ("solet-manager", "reconcile", name, "--dry-run", "--json"), "reconcile --dry-run closes the phase")


def _check_degraded_sequence(inputs: Inputs, phases: tuple[Phase, ...]) -> None:
    """Step 7 design section 8.4: host Python removed then restored, service booted out; a doctor and an update --dry-run after each."""
    phase = next(phase for phase in phases if phase.name == "cold-host-degraded")
    commands = phase.commands
    name = f"{inputs.instance_name}-import"
    _check(all(command[0] != "solet" for command in commands), "cold-host phase names the Manager as solet-manager, never a bare solet verb")
    _check(commands[0] == ("brew", "uninstall", "--ignore-dependencies", inputs.python_formula) and commands[3] == ("brew", "install", inputs.python_formula), "host Python is removed, measured, and restored before the service is touched")
    _check(commands[4] == ("launchctl", "bootout", f"gui/<uid>/local.solet.{name}"), "the service is booted out by its enrolled LaunchAgent label")
    probe = (("solet-manager", "doctor", name, "--json"), ("solet-manager", "update", name, "--dry-run", "--json"))
    _check(commands[1:3] == probe and commands[5:7] == probe and len(commands) == 7, "each absence is measured by a doctor then an update --dry-run, and nothing mutates the instance")
    _check(all("--yes" not in command for command in commands), "the degraded phase never applies an update")


def _check_formula_staging(root: Path) -> None:
    previous = root / "previous" / "Formula" / "solet.rb"
    current = root / "current" / "Formula" / "solet.rb"
    staged = root / "tap" / "Formula" / "solet.rb"
    previous.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    previous.write_bytes(b"class Solet < Formula\n  version \"0.1.0\"\nend\n")
    current.write_bytes(b"class Solet < Formula\n  version \"0.2.0\"\nend\n")
    stage_formula(previous, staged)
    _check(staged.read_bytes() == previous.read_bytes(), "previous Formula stages byte-for-byte")
    stage_formula(current, staged)
    _check(staged.read_bytes() == current.read_bytes(), "current Formula stages byte-for-byte")


def _check_config_render(root: Path) -> None:
    destination = root / "create.toml"
    target = root / "home" / "Solets" / "lifecycle-bizops"
    render_create_config(
        _ROOT / "ci" / "lifecycle_config.toml.template",
        destination,
        "lifecycle-bizops",
        target,
    )
    rendered = destination.read_text(encoding="utf-8")
    _check("{{" not in rendered and str(target) in rendered, "fixture config is fully bound")
    _check(
        'inference_implementation = "none"' in rendered and "autostart = false" in rendered,
        "fixture selects the closed no-model, no-autostart path",
    )


def _check_preservation_and_keg_scan(root: Path) -> None:
    home = root / "home"
    manager = root / "manager"
    target = home / "Solets" / "lifecycle-bizops"
    launcher = home / ".local" / "bin" / "lifecycle-bizops"
    target.mkdir(parents=True)
    manager.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    (target / "data.json").write_text('{"preserved": true}\n', encoding="utf-8")
    launcher.symlink_to(target / "client" / "bin" / "solet")
    registry = manager / "instances.json"
    registry.write_text('{"schema_version": 1}\n', encoding="utf-8")
    cache = home / ".cache" / "Homebrew" / "downloads"
    cache.mkdir(parents=True)
    cache_file = cache / "formula.json"
    cache_file.write_text("before\n", encoding="utf-8")
    fixture = Fixture(root, home, manager, target, root / "create.toml")
    preserved = preserved_roots(fixture)
    before = snapshot_tree(preserved)
    cache_file.write_text("after\n", encoding="utf-8")
    assert_unchanged(before, preserved, "Homebrew cache update")
    _check(True, "unrelated Homebrew cache bytes are outside preservation evidence")
    forbidden = (
        Path("/opt/homebrew/Cellar/solet"),
        Path("/opt/homebrew/Cellar/python@3.13"),
    )
    assert_no_keg_paths(preserved, forbidden)
    _check(True, "no-keg-path scan accepts instance-private launcher")

    registry.write_text("changed\n", encoding="utf-8")
    _raises(
        lambda: assert_unchanged(before, preserved, "manager update"),
        "preservation assertion detects changed registry bytes",
    )
    registry.write_text('{"schema_version": 1}\n', encoding="utf-8")
    (target / "data.json").write_text("changed\n", encoding="utf-8")
    _raises(
        lambda: assert_unchanged(before, preserved, "upgrade"),
        "preservation assertion detects changed instance bytes",
    )
    leak_file = target / "keg-content"
    leak_link = launcher.parent / "keg-symlink"
    cases = (
        ("Solet", "/opt/homebrew/Cellar/solet/0.2.0/bin/solet"),
        ("Python", "/opt/homebrew/Cellar/python@3.13/3.13.13_1/bin/python3.13"),
    )
    for label, leaked_path in cases:
        leak_file.write_text(f"#!{leaked_path}\n", encoding="utf-8")
        _raises(
            lambda: assert_no_keg_paths(preserved, forbidden),
            f"no-keg-path assertion detects {label} Cellar file content",
        )
        leak_file.unlink()
        leak_link.symlink_to(leaked_path)
        _raises(
            lambda: assert_no_keg_paths(preserved, forbidden),
            f"no-keg-path assertion detects {label} Cellar symlink target",
        )
        leak_link.unlink()

    for label, cellar_root in (
        ("Solet", forbidden[0]),
        ("Python", forbidden[1]),
    ):
        leak_link.symlink_to(cellar_root)
        _raises(
            lambda: assert_no_keg_paths(preserved, forbidden),
            f"no-keg-path assertion detects exact {label} Cellar root",
        )
        leak_link.unlink()
        leak_link.symlink_to(
            Path(os.path.relpath(cellar_root, start=leak_link.parent)),
        )
        _raises(
            lambda: assert_no_keg_paths(preserved, forbidden),
            f"no-keg-path assertion detects relative {label} Cellar symlink target",
        )
        leak_link.unlink()

    leak_file.write_text("/opt/homebrew/Cellar/python@3.13-tools/bin/python\n", encoding="utf-8")
    assert_no_keg_paths(preserved, forbidden)
    _check(True, "near-prefix Cellar decoy remains accepted")
    leak_file.unlink()
    leak_link.symlink_to(leak_link.name)
    _raises(
        lambda: assert_no_keg_paths(preserved, forbidden),
        "no-keg-path assertion fails loudly when a symlink target is cyclic",
    )


def _check_unmanaged_boundary() -> None:
    valid: dict[str, object] = {
        "kind": "instance_status",
        "status": "awaiting_user",
        "error_kind": "instance_unmanaged",
        "data": {"name": "manual", "managed": False},
    }
    assert_unmanaged(valid, "manual")
    _check(True, "typed instance_unmanaged result is accepted")
    invalid = dict(valid)
    invalid["error_kind"] = "instance_missing"
    _raises(lambda: assert_unmanaged(invalid, "manual"), "untyped discovery result is refused")


def _check_shell_wrapper() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_ROOT / "ci" / "acceptance.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(result.returncode == 0, f"acceptance shell wrapper parses: {result.stderr}")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _check_plan(root)
        _check_formula_staging(root)
        _check_config_render(root)
        _check_preservation_and_keg_scan(root)
        _check_unmanaged_boundary()
        _check_shell_wrapper()
    print(f"lifecycle_acceptance_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
