"""Prove the release workflow invokes the canonical staging script.

This smoke is deliberately private to the development checkout.  The public
payload's staging smoke exercises ``stage_release.py`` directly because the
payload correctly excludes this checkout's CI workflow.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[2]
_SCRIPT = _REPOSITORY / "solet_cli" / "homebrew" / "scripts" / "stage_release.py"
_WORKFLOW = _REPOSITORY / ".github" / "workflows" / "release-payload-stage.yml"
_RELEASE_TAG = "release-2026-08-23"
_WORKFLOW_REPOSITORY = "other-owner/other-seed"
_MANAGER_REPOSITORY = "https://github.com/solet-public/homebrew-tap.git"
_MANAGER_RELEASE_TAG = "manager-v0.1.0-r0"
_MANAGER_SOURCE_REPOSITORY = "https://github.com/solet-public/solet.git"
_checks = 0


def _stage_contract_paths() -> tuple[str, ...]:
    spec = spec_from_file_location("release_workflow_parity_stage_release", _SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load stage_release.py from {_SCRIPT}")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    paths = getattr(module, "_CONTRACT_PATHS", None)
    if not isinstance(paths, tuple) or not all(isinstance(path, str) for path in paths):
        raise AssertionError("stage_release.py does not expose a string _CONTRACT_PATHS tuple")
    return paths


_CONTRACT_PATHS = _stage_contract_paths()


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "release-workflow-parity-smoke",
            "GIT_AUTHOR_EMAIL": "release-workflow-parity-smoke@example.invalid",
            "GIT_COMMITTER_NAME": "release-workflow-parity-smoke",
            "GIT_COMMITTER_EMAIL": "release-workflow-parity-smoke@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )


def _build_seed_fixture(root: Path, checkout_name: str = "seed-checkout") -> Path:
    """Two commits, deliberately (mirrors `stage_release_smoke.py`'s own
    fixture): the first ("manager content") carries everything the manager
    side needs; PROVENANCE.json, added in the second ("seed release", tagged
    here), declares that first commit's hash as its own `source_commit` --
    the only way to satisfy `_require_same_source_revision` without being
    self-referential (a commit's hash cannot appear as plaintext inside its
    own tree). The checkout is left detached back at the first commit so
    `${{ github.sha }}` (this checkout's HEAD, substituted below) resolves
    to it, matching what PROVENANCE.json declares.
    """
    checkout = root / checkout_name
    checkout.mkdir()
    for name in ("LICENSE", "NOTICE"):
        (checkout / name).write_bytes((_REPOSITORY / name).read_bytes())
    (checkout / "solet_cli" / "homebrew").mkdir(parents=True)
    (checkout / "solet_cli" / "pyproject.toml").write_text('[project]\nname = "solet-cli"\nversion = "0.1.0"\n', encoding="utf-8")
    (checkout / "solet_cli" / "src" / "solet_manager").mkdir(parents=True)
    (checkout / "solet_cli" / "src" / "solet_manager" / "models.py").write_text('MANAGER_VERSION = "0.1.0"\n', encoding="utf-8")
    (checkout / "solet_cli" / "src.marker").write_text("manager source\n", encoding="utf-8")
    (checkout / "solet_cli" / "homebrew" / "seed.lock.json.template").write_text("{}\n", encoding="utf-8")
    (checkout / "solet_setup_contracts").mkdir()
    (checkout / "solet_setup_contracts" / "pyproject.toml").write_text(
        '[project]\nname = "solet-setup-contracts"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    contracts = checkout / "plugins" / "github_midwife_plugin" / "knowledge_base"
    contracts.mkdir(parents=True)
    for relative_path in _CONTRACT_PATHS:
        (checkout / relative_path).write_text("{}\n", encoding="utf-8")
    (contracts / "existing_install_flow.schema.json").write_text("{}\n", encoding="utf-8")
    (contracts / "existing_install_flow.json").write_text(
        '{"flow_id": "existing-install", "schema_version": 1}\n', encoding="utf-8"
    )
    _run_git(checkout, "init", "-q")
    _run_git(checkout, "add", "-A")
    _run_git(checkout, "commit", "-q", "-m", "manager content")
    manager_commit = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()

    (checkout / "PROVENANCE.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "seed_id": "123e4567-e89b-12d3-a456-426614174000",
                "origin_id": "123e4567-e89b-12d3-a456-426614174001",
                "source_commit": manager_commit,
                "manifest_sha256": "b" * 64,
                "bundle": {"name": "macos-bizops", "platform": "local"},
                "source_date": "2026-09-15T00:00:00+00:00",
                "lineage": [],
                "ancestry": [],
                "signature": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _run_git(checkout, "add", "PROVENANCE.json")
    _run_git(checkout, "commit", "-q", "-m", "seed release")
    _run_git(checkout, "tag", _RELEASE_TAG)
    _run_git(checkout, "checkout", "-q", manager_commit)
    return checkout


def _workflow_stage_argv(workspace: Path, output_root: Path) -> list[str]:
    """Materialize the real workflow staging command for the local fixture."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    _check(
        "Immutable tag on this repository" in workflow and "repository: ${{ github.repository }}" in workflow and "https://github.com/${{ github.repository }}.git" in workflow,
        "workflow uses static dispatch-description text and runtime context where evaluated",
    )
    _check(
        "seed_profile:" in workflow and 'default: "macos-bizops"' in workflow and '--seed-profile "${{ inputs.seed_profile }}"' in workflow,
        "workflow parameterizes the required seed profile",
    )
    lines = workflow.splitlines()
    step_index = _required_line_index(
        lines,
        "- name: Stage the release payload",
        start=0,
        label="workflow has the release-payload staging step",
    )
    run_index = _required_line_index(
        lines,
        "run: |",
        start=step_index + 1,
        label="workflow staging step has a shell run block",
    )
    command_lines = _indented_block(lines, run_index)
    _check(bool(command_lines), "workflow staging run block is non-empty")
    expression_substitutions = {
        "${{ inputs.release_tag }}": _RELEASE_TAG,
        "${{ github.repository }}": _WORKFLOW_REPOSITORY,
        "${{ inputs.seed_profile }}": "macos-bizops",
        "${{ inputs.manager_repository }}": _MANAGER_REPOSITORY,
        "${{ inputs.manager_release_tag }}": _MANAGER_RELEASE_TAG,
        "${{ inputs.manager_source_repository }}": _MANAGER_SOURCE_REPOSITORY,
        "${{ inputs.formula_revision }}": "0",
        "${{ github.sha }}": _run_git(workspace / "manager-checkout", "rev-parse", "HEAD").stdout.strip(),
        "$RUNNER_TEMP/staged-release": str(output_root),
    }
    command = "\n".join(command_lines).replace("\\\n", " ")
    for source, replacement in expression_substitutions.items():
        command = command.replace(source, replacement)
    argv = shlex.split(command)
    expected_prefix = ["python3", "manager-checkout/solet_cli/homebrew/scripts/stage_release.py"]
    _check(argv[:2] == expected_prefix, "workflow invokes the canonical staging script")
    if argv[:2] != expected_prefix:
        raise AssertionError(f"unexpected workflow staging command: {argv}")
    return [
        sys.executable,
        str(_SCRIPT),
        *({"manager-checkout": str(workspace / "manager-checkout"), "seed-checkout": str(workspace / "seed-checkout")}.get(value, value) for value in argv[2:]),
    ]


def _required_line_index(lines: list[str], expected: str, *, start: int, label: str) -> int:
    index = next(
        (position for position in range(start, len(lines)) if lines[position].strip() == expected),
        None,
    )
    _check(index is not None, label)
    if index is None:
        raise AssertionError(label)
    return index


def _indented_block(lines: list[str], header_index: int) -> list[str]:
    header_indent = len(lines[header_index]) - len(lines[header_index].lstrip())
    block: list[str] = []
    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= header_indent:
            break
        block.append(line.strip())
    return block


def _check_manager_root_layout_is_red(root: Path, source_checkout: Path) -> None:
    """The pre-r3 manager-root layout is rejected for its nested seed checkout."""
    workspace = root / "pre-r3-workspace"
    _run_git(root, "clone", "--no-local", str(source_checkout), str(workspace))
    _run_git(workspace, "clone", "--no-local", str(source_checkout), "seed-checkout")
    manager_ref = _run_git(workspace, "rev-parse", "HEAD").stdout.strip()
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(workspace / "seed-checkout"),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            f"https://github.com/{_WORKFLOW_REPOSITORY}.git",
            "--seed-profile",
            "macos-bizops",
            "--manager-repository",
            _MANAGER_REPOSITORY,
            "--manager-release-tag",
            _MANAGER_RELEASE_TAG,
            "--manager-checkout",
            str(workspace),
            "--manager-ref",
            manager_ref,
            "--manager-source-repository",
            _MANAGER_SOURCE_REPOSITORY,
            "--formula-revision",
            "0",
            "--output-root",
            str(root / "pre-r3-staged-release"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        result.returncode != 0 and "--manager-checkout must be clean before staging" in result.stderr and "?? seed-checkout/" in result.stderr,
        f"pre-r3 manager-root workspace is red for the nested seed checkout: {result.stderr}",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        manager_checkout = _build_seed_fixture(workspace, "manager-checkout")
        _run_git(workspace, "clone", "--no-local", str(manager_checkout), "seed-checkout")
        _check_manager_root_layout_is_red(root, manager_checkout)
        output_root = root / "staged-release"
        result = subprocess.run(
            _workflow_stage_argv(workspace, output_root),
            check=False,
            capture_output=True,
            text=True,
        )
        _check(result.returncode == 0, f"workflow staging command failed: {result.stderr}")
        _check(
            (output_root / "release_metadata.json").is_file(),
            "workflow staging command writes release metadata",
        )
    print(f"release_workflow_parity_smoke: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
