"""Focused no-network smoke for the release-payload staging script.

Builds a throwaway local git fixture standing in for a checked-out seed
release, runs ``stage_release.py`` against it twice, and checks: the payload
archive excludes the rendered lock file, the checksum in the metadata
matches the archive's real bytes, the render step's own invariants still
hold, and staging the same commit twice is byte-for-byte reproducible. No
network access, no GitHub, no git mutation of the real checkout.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "stage_release.py"
_RENDERER = _ROOT / "scripts" / "render_release_payload.py"
_PLUGIN_SRC = _REPOSITORY_ROOT / "plugins" / "github_midwife_plugin" / "src"
_SETUP_CONTRACTS_SRC = _REPOSITORY_ROOT / "solet_setup_contracts" / "src"
sys.path.insert(0, str(_ROOT.parent / "src"))
sys.path.insert(0, str(_PLUGIN_SRC))
sys.path.insert(0, str(_SETUP_CONTRACTS_SRC))

from github_midwife_plugin.permissions_manifest import load_flow, render_manifest  # noqa: E402
from solet_manager.contracts import (  # noqa: E402
    PERMISSIONS_MANIFEST_FILENAME,
    ContractBundle,
    contract_digest,
    contract_digested_filenames,
    contract_filenames,
)
from solet_manager.errors import ContractError  # noqa: E402
from solet_manager.permission_preflight import render_permission_preflight  # noqa: E402
from solet_manager.plan_builder import SetupPlan  # noqa: E402
from solet_manager.release_identity_gate import pair_manager_and_seed  # noqa: E402
from solet_manager.seed_lock_parser import parse_seed_lock_bytes  # noqa: E402
from solet_setup_contracts.selected_source_record import (  # noqa: E402
    SelectedSourceTransaction,
    validate_target_contract_identity,
)

_PORTABLE_RUBY = Path("/opt/homebrew/Library/Homebrew/vendor/portable-ruby/current/bin/ruby")
_HEREDOC_WRITE_CALL = re.compile(
    r'\(libexec/"share"/"solet"/"(?P<name>[^"]+\.json)"\)\.write (?P<opener><<~\'?JSON\'?)\n'
    r"(?P<body>.*?\n)    JSON\n",
    re.DOTALL,
)
_CONTRACT_ARCHIVE_ROOT = "plugins/github_midwife_plugin/knowledge_base"
_FORMULA_ONLY_CONTRACT_EXTENSIONS = frozenset({"existing_install_flow.json", "existing_install_flow.schema.json"})
_CONTRACT_SOURCE = _REPOSITORY_ROOT / _CONTRACT_ARCHIVE_ROOT
_CANONICAL_SEED_REPOSITORY = "https://github.com/solet-public/macos-bizops.git"
_CANONICAL_SEED_PROFILE = "macos-bizops"
_RELEASE_TAG = "release-2026-08-23"
_MANAGER_REPOSITORY = "https://github.com/solet-public/homebrew-tap.git"
_MANAGER_RELEASE_TAG = "manager-v0.1.0-r0"
_MANAGER_SOURCE_REPOSITORY = "https://github.com/solet-public/solet.git"
_OTHER_SEED_REPOSITORY = "https://github.com/solet-public/macos-samantha.git"
_OTHER_SEED_PROFILE = "macos-samantha"
_FIXTURE_LICENSE = b"fixture Apache-2.0 license\n"
_FIXTURE_NOTICE = b"fixture notice\n"
_FORMULA_BUILD_PATH_INSTALL = re.compile(r'venv\.pip_install buildpath/"(?P<path>[^"]+)"')
_FORMULA_CONTRACT_INSTALL = re.compile(
    rf'"{re.escape(_CONTRACT_ARCHIVE_ROOT)}/(?P<filename>[^"]+)"'
)
_checks = 0


@dataclass(frozen=True)
class _ContractFilenameConsumer:
    path: Path
    extractor: Callable[[Path], set[str]]
    authority: Callable[[], tuple[str, ...]]
    authority_name: str


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
            "GIT_AUTHOR_NAME": "stage-release-smoke",
            "GIT_AUTHOR_EMAIL": "stage-release-smoke@example.invalid",
            "GIT_COMMITTER_NAME": "stage-release-smoke",
            "GIT_COMMITTER_EMAIL": "stage-release-smoke@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )


def _build_seed_fixture(
    root: Path,
    *,
    provenance_source_commit: str | None = None,
    manager_version: str = "0.1.0",
) -> tuple[Path, Path, str]:
    """A minimal committed tree carrying only what stage_release.py needs.

    Two commits, deliberately: the first ("manager content") carries
    everything the manager side needs and nothing else; PROVENANCE.json,
    added in the second ("seed release", tagged here), declares that first
    commit's hash as its own ``source_commit`` by default -- the only way to
    make ``PROVENANCE.source_commit == the manager's resolved commit``
    without being self-referential (a commit's hash cannot appear as
    plaintext inside its own tree). This mirrors the real, cross-repository
    relationship: in production the seed repo's PROVENANCE.json is committed
    separately from, and after, the manager-source commit it names.

    ``provenance_source_commit``, when given, overrides that default with an
    explicit (deliberately mismatched, for the skew-refusal checks) value.
    """
    checkout = root / "seed-checkout"
    checkout.mkdir()
    (checkout / "LICENSE").write_bytes(_FIXTURE_LICENSE)
    (checkout / "NOTICE").write_bytes(_FIXTURE_NOTICE)
    (checkout / "solet_cli" / "homebrew").mkdir(parents=True)
    (checkout / "solet_cli" / "pyproject.toml").write_text(
        '[project]\nname = "solet-cli"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (checkout / "solet_cli" / "src" / "solet_manager").mkdir(parents=True)
    (checkout / "solet_cli" / "src" / "solet_manager" / "models.py").write_text(
        f'MANAGER_VERSION = "{manager_version}"\n', encoding="utf-8"
    )
    (checkout / "solet_cli" / "src.marker").write_text("manager source\n", encoding="utf-8")
    (checkout / "solet_cli" / "homebrew" / "seed.lock.json.template").write_text(
        "{}\n", encoding="utf-8"
    )
    (checkout / "solet_setup_contracts").mkdir()
    (checkout / "solet_setup_contracts" / "pyproject.toml").write_text(
        '[project]\nname = "solet-setup-contracts"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    contracts = checkout / _CONTRACT_ARCHIVE_ROOT
    contracts.mkdir(parents=True)
    for name in contract_filenames():
        (contracts / name).write_bytes((_CONTRACT_SOURCE / name).read_bytes())
    for name in _FORMULA_ONLY_CONTRACT_EXTENSIONS:
        (contracts / name).write_bytes((_CONTRACT_SOURCE / name).read_bytes())

    _run_git(checkout, "init", "-q")
    _run_git(checkout, "add", "-A")
    _run_git(checkout, "commit", "-q", "-m", "manager content")
    manager_ref = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
    manager_checkout = root / "manager-checkout"
    _run_git(
        checkout,
        "worktree",
        "add",
        "--detach",
        "-q",
        str(manager_checkout),
        manager_ref,
    )

    (checkout / "PROVENANCE.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "seed_id": "123e4567-e89b-12d3-a456-426614174000",
                "origin_id": "123e4567-e89b-12d3-a456-426614174001",
                "source_commit": provenance_source_commit or manager_ref,
                "manifest_sha256": "b" * 64,
                "bundle": {"name": _CANONICAL_SEED_PROFILE, "platform": "local"},
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

    (checkout / "shared-head.marker").write_text("shared checkout moved\n", encoding="utf-8")
    _run_git(checkout, "add", "shared-head.marker")
    _run_git(checkout, "commit", "-q", "-m", "shared checkout moves after pin")
    _run_git(checkout, "branch", "release-branch")
    return checkout, manager_checkout, manager_ref


def _stage(
    seed_checkout: Path,
    manager_checkout: Path,
    manager_ref: str,
    output_root: Path,
    *,
    dev_mode: bool = False,
    release_label: str | None = None,
    allow_manager_seed_skew: str | None = None,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        sys.executable,
        str(_SCRIPT),
        "--seed-checkout",
        str(seed_checkout),
        "--release-tag",
        _RELEASE_TAG,
        "--seed-repository",
        _CANONICAL_SEED_REPOSITORY,
        "--seed-profile",
        _CANONICAL_SEED_PROFILE,
        "--manager-checkout",
        str(manager_checkout),
        "--manager-ref",
        manager_ref,
        "--manager-source-repository",
        _MANAGER_SOURCE_REPOSITORY,
        "--formula-revision",
        "0",
    ]
    if dev_mode:
        arguments.append("--dev-mode")
    else:
        arguments.extend(
            (
                "--manager-repository",
                _MANAGER_REPOSITORY,
                "--manager-release-tag",
                _MANAGER_RELEASE_TAG,
            )
        )
    if release_label is not None:
        arguments.extend(("--release-label", release_label))
    if allow_manager_seed_skew is not None:
        arguments.extend(("--allow-manager-seed-skew", allow_manager_seed_skew))
    arguments.extend(("--output-root", str(output_root)))
    result = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
    )
    if expect_success:
        _check(result.returncode == 0, f"stage_release.py failed: {result.stderr}")
    return result


def _stage_lock_only(checkout: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(checkout),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            _OTHER_SEED_REPOSITORY,
            "--seed-profile",
            _OTHER_SEED_PROFILE,
            "--lock-only",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _payload_archive_path(output_root: Path) -> Path:
    payload_dir = output_root / "payload"
    archives = sorted(payload_dir.glob("solet-*.tar.gz"))
    _check(len(archives) == 1, f"exactly one payload archive staged, found {archives}")
    return archives[0]


def _check_metadata(
    metadata: dict[str, object],
    expected_commit: str,
    expected_tree: str,
    expected_manager_ref: str,
    expected_manager_commit: str,
    expected_manager_tree: str,
) -> None:
    _check(metadata["seed_repository"] == _CANONICAL_SEED_REPOSITORY, "canonical repository")
    _check(metadata["seed_release_tag"] == _RELEASE_TAG, "tag carried through")
    _check(metadata["seed_commit"] == expected_commit, "commit resolved from the tag")
    _check(metadata["seed_tree_hash"] == expected_tree, "tree hash resolved from the commit")
    _check(
        metadata["manager_source_repository"] == _MANAGER_SOURCE_REPOSITORY,
        "manager source repository is carried through",
    )
    _check(
        "manager_source_ref" in metadata,
        "release metadata records the supplied manager source ref",
    )
    _check(
        metadata["manager_source_ref"] == expected_manager_ref,
        "manager source ref records the supplied immutable pin",
    )
    _check(
        metadata["manager_source_commit"] == expected_manager_commit,
        "manager source commit resolved from the manager checkout",
    )
    _check(
        metadata["manager_source_tree_hash"] == expected_manager_tree,
        "manager source tree hash resolved from the manager checkout",
    )
    _check(
        metadata["manager_url"] == "https://github.com/solet-public/homebrew-tap/releases/download/"
        f"{_MANAGER_RELEASE_TAG}/solet-0.1.0.tar.gz",
        "manager_url carries the independent public manager repository and tag",
    )


def _formula_buildpath_installs(formula: str) -> tuple[str, ...]:
    return tuple(match.group("path") for match in _FORMULA_BUILD_PATH_INSTALL.finditer(formula))


def _missing_archive_install_paths(
    archive_members: set[str], install_paths: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(
        path
        for path in install_paths
        if not any(member == path or member.startswith(f"{path}/") for member in archive_members)
    )


def _check_archive(archive_path: Path, metadata: dict[str, object], formula: str) -> str:
    real_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    _check(
        metadata["release_archive_sha256"] == real_sha256,
        "declared checksum matches the payload archive's real bytes",
    )
    with tarfile.open(archive_path, mode="r:gz") as archive:
        names = set(archive.getnames())
        license_member = archive.extractfile("LICENSE") if "LICENSE" in names else None
        notice_member = archive.extractfile("NOTICE") if "NOTICE" in names else None
        distributed_license = license_member.read() if license_member is not None else None
        distributed_notice = notice_member.read() if notice_member is not None else None
    _check(
        "solet_cli/src.marker" in names
        and "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json" in names,
        "payload carries the manager source and the setup contracts",
    )
    _check_contract_shipment_parity(names)
    install_paths = _formula_buildpath_installs(formula)
    _check(bool(install_paths), "rendered Formula installs at least one payload-local package")
    missing_install_paths = _missing_archive_install_paths(names, install_paths)
    _check(
        not missing_install_paths,
        "payload carries every Formula buildpath install: " + ", ".join(missing_install_paths),
    )
    _check(
        "solet_cli/homebrew/seed.lock.json" not in names,
        "payload excludes the rendered lock — it ships in the tap, not the download, "
        "or the checksum would describe an archive that contains its own checksum",
    )
    _check(
        distributed_license is not None and distributed_license == _FIXTURE_LICENSE,
        "payload carries the fixture LICENSE byte-for-byte",
    )
    _check(
        distributed_notice is not None and distributed_notice == _FIXTURE_NOTICE,
        "payload carries the fixture NOTICE byte-for-byte",
    )
    return real_sha256


def _check_contract_shipment_parity(archive_members: set[str]) -> None:
    expected = set(contract_filenames()) | _FORMULA_ONLY_CONTRACT_EXTENSIONS
    archive_prefix = f"{_CONTRACT_ARCHIVE_ROOT}/"
    archived = {
        member.removeprefix(archive_prefix)
        for member in archive_members
        if member.startswith(archive_prefix)
    }
    _check(
        archived == expected,
        "payload contract files exactly match ContractBundle's required-file authority",
    )
    _check_contract_consumer_parity()
    _check(
        _bundle_directory_read_filenames() <= expected,
        "every CLI filename read below bundle.directory belongs to the shipped contract set",
    )


def _check_contract_consumer_parity() -> None:
    for consumer in _CONTRACT_FILENAME_CONSUMERS:
        expected = set(consumer.authority())
        if consumer.path.name == "solet.rb.template":
            expected |= _FORMULA_ONLY_CONTRACT_EXTENSIONS
        _check(
            consumer.extractor(consumer.path) == expected,
            f"{consumer.path.relative_to(_REPOSITORY_ROOT)} exactly matches "
            f"the {consumer.authority_name} contract authority",
        )


def _extract_stage_contract_filenames(path: Path) -> set[str]:
    return _contract_filenames_from_paths(_extract_module_tuple_strings(path, "_CONTRACT_PATHS"))


def _extract_stage_digested_contract_filenames(path: Path) -> set[str]:
    return _contract_filenames_from_paths(
        _extract_module_tuple_strings(path, "_DIGESTED_CONTRACT_PATHS")
    )


def _extract_selected_source_contract_filenames(path: Path) -> set[str]:
    return set(_extract_module_tuple_strings(path, "_CONTRACT_FILENAMES"))


def _extract_module_tuple_strings(path: Path, constant_name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tuples = _module_tuple_assignments(tree)
    value = tuples.get(constant_name)
    if value is None:
        raise AssertionError(f"red: no {constant_name} tuple in {path}")
    return _tuple_string_literals(value, tuples, resolving={constant_name})


def _module_tuple_assignments(tree: ast.Module) -> dict[str, ast.Tuple]:
    tuples: dict[str, ast.Tuple] = {}
    for assignment in tree.body:
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
            continue
        target = assignment.targets[0]
        if isinstance(target, ast.Name) and isinstance(assignment.value, ast.Tuple):
            tuples[target.id] = assignment.value
    return tuples


def _tuple_string_literals(
    value: ast.Tuple,
    tuples: dict[str, ast.Tuple],
    *,
    resolving: set[str],
) -> list[str]:
    values: list[str] = []
    for item in value.elts:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            values.append(item.value)
            continue
        if isinstance(item, ast.Starred) and isinstance(item.value, ast.Name):
            name = item.value.id
            nested = tuples.get(name)
            if nested is None or name in resolving:
                raise AssertionError(f"red: contract tuple star cannot resolve local {name}")
            values.extend(_tuple_string_literals(nested, tuples, resolving=resolving | {name}))
            continue
        raise AssertionError("red: contract tuple contains a non-literal or unresolved path")
    return values


def _extract_formula_contract_filenames(path: Path) -> set[str]:
    formula = path.read_text(encoding="utf-8")
    return {match.group("filename") for match in _FORMULA_CONTRACT_INSTALL.finditer(formula)}


def _contract_filenames_from_paths(paths: list[str]) -> set[str]:
    prefix = f"{_CONTRACT_ARCHIVE_ROOT}/"
    if not all(path.startswith(prefix) for path in paths):
        raise AssertionError(
            "red: contract consumer path is outside the shipped contracts directory"
        )
    return {path.removeprefix(prefix) for path in paths}


_CONTRACT_FILENAME_CONSUMERS = (
    _ContractFilenameConsumer(
        path=_ROOT / "scripts" / "stage_release.py",
        extractor=_extract_stage_digested_contract_filenames,
        authority=contract_digested_filenames,
        authority_name="digested",
    ),
    _ContractFilenameConsumer(
        path=_ROOT / "scripts" / "stage_release.py",
        extractor=_extract_stage_contract_filenames,
        authority=contract_filenames,
        authority_name="shipped",
    ),
    _ContractFilenameConsumer(
        path=_ROOT / "Formula" / "solet.rb.template",
        extractor=_extract_formula_contract_filenames,
        authority=contract_filenames,
        authority_name="shipped",
    ),
    _ContractFilenameConsumer(
        path=_REPOSITORY_ROOT
        / "solet_setup_contracts"
        / "src"
        / "solet_setup_contracts"
        / "selected_source_record.py",
        extractor=_extract_selected_source_contract_filenames,
        authority=contract_digested_filenames,
        authority_name="digested",
    ),
)


def _bundle_directory_read_filenames() -> set[str]:
    filenames: set[str] = set()
    source_root = _ROOT.parent / "src" / "solet_manager"
    for source in source_root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            filename = _bundle_directory_read_filename(node, constants)
            if filename is not None:
                filenames.add(filename)
    return filenames


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for assignment in tree.body:
        if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
            continue
        target = assignment.targets[0]
        value = assignment.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value.value
    return constants


def _bundle_directory_read_filename(node: ast.AST, constants: dict[str, str]) -> str | None:
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return None
    if not isinstance(node.left, ast.Attribute) or node.left.attr != "directory":
        return None
    if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
        return node.right.value
    if isinstance(node.right, ast.Name):
        return constants.get(node.right.id)
    return None


def _check_extracted_contract_bundle(
    archive_path: Path,
    expected_commit: str,
    staged_contract_digest: str,
    root: Path,
) -> None:
    extracted = root / "extracted-payload"
    with tarfile.open(archive_path, mode="r:gz") as archive:
        archive.extractall(extracted, filter="data")
    contracts = extracted / _CONTRACT_ARCHIVE_ROOT
    bundle = ContractBundle.load(source_revision=expected_commit, directory=contracts)
    preflight = render_permission_preflight(
        bundle,
        SetupPlan(
            answers={
                "decisions": {
                    "setup_profile": "free",
                    "autostart": "disabled",
                    "coding_agents": ["codex"],
                    "session_sources": ["codex_local"],
                },
                "consents": {"system_change_consent": True},
            },
            operations=(),
            unresolved_decisions=(),
            unresolved_consents=(),
        ),
        {},
    )
    _check(
        preflight["manifest"] == PERMISSIONS_MANIFEST_FILENAME,
        "preflight reads the manifest loaded from the extracted ContractBundle",
    )
    _check(
        bundle.contract_digest == staged_contract_digest,
        "manifest is shipped and load-required but excluded from flow reconciliation identity",
    )
    _check_manifest_matches_flow(contracts)
    _check_manifest_drift_mutation_is_red(contracts)
    (contracts / PERMISSIONS_MANIFEST_FILENAME).unlink()
    try:
        ContractBundle.load(source_revision=expected_commit, directory=contracts)
    except ContractError as exc:
        _check(
            PERMISSIONS_MANIFEST_FILENAME in str(exc),
            "missing manifest is refused at ContractBundle load",
        )
    else:
        raise AssertionError(
            "red: r21-style manifest omission reached preflight instead of load refusal"
        )


def _check_manifest_matches_flow(contracts: Path) -> None:
    expected = render_manifest(load_flow(contracts / "macos_setup_flow.json")).json_bytes
    actual = (contracts / PERMISSIONS_MANIFEST_FILENAME).read_bytes()
    _check(actual == expected, "extracted manifest exactly matches its setup-flow rendering")


def _check_manifest_drift_mutation_is_red(contracts: Path) -> None:
    manifest = json.loads((contracts / PERMISSIONS_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise AssertionError("red: fixture manifest has no mutable entry")
    entries[0]["why"] = "MUTATED"
    mutated = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    try:
        _assert_manifest_matches_flow(mutated, contracts)
    except AssertionError:
        _check(True, "mutated extracted manifest is RED against its setup-flow rendering")
        return
    raise AssertionError("red: mutated extracted manifest passed the flow-rendering drift check")


def _assert_manifest_matches_flow(actual: bytes, contracts: Path) -> None:
    expected = render_manifest(load_flow(contracts / "macos_setup_flow.json")).json_bytes
    if actual != expected:
        raise AssertionError("manifest does not match its setup-flow rendering")


def _staged_contract_digest(stage_result: subprocess.CompletedProcess[str]) -> str:
    match = re.search(r"setup contracts:\s+(sha256:[0-9a-f]{64})", stage_result.stdout)
    _check(match is not None, "stager reports its validated flow contract digest")
    if match is None:
        raise AssertionError("red: stager did not report a flow contract digest")
    return match.group(1)


def _check_contract_digest_implementation_parity(
    checkout: Path,
    staged_contract_digest: str,
) -> None:
    manager_digest = contract_digest(checkout / _CONTRACT_ARCHIVE_ROOT)
    _check(
        manager_digest == staged_contract_digest,
        "manager and release stager compute the same setup-contract identity",
    )
    transaction = SelectedSourceTransaction(
        name="fixture",
        target=str(checkout),
        answers={},
        answers_fingerprint="fixture",
        flow_source_revision="fixture",
        flow_contract_digest=manager_digest,
    )
    validate_target_contract_identity(transaction, target=checkout)
    _check(
        True,
        "selected-source validation computes the same setup-contract identity as the manager",
    )


def _check_archive_guard_rejects_pre_fix_omission() -> None:
    missing = _missing_archive_install_paths(
        {
            "solet_cli/src.marker",
            "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json",
        },
        ("solet_setup_contracts", "solet_cli"),
    )
    _check(
        missing == ("solet_setup_contracts",),
        "archive guard rejects the pre-fix omission that the former spot-check missed",
    )


def _check_rendered_outputs(output_root: Path, real_sha256: str) -> None:
    formula = (output_root / "Formula" / "solet.rb").read_text(encoding="utf-8")
    lock = json.loads(
        (output_root / "solet_cli" / "homebrew" / "seed.lock.json").read_text(encoding="utf-8")
    )
    _check(str(real_sha256) in formula, "rendered formula carries the real checksum")
    _check(lock["archive_sha256"] == real_sha256, "rendered lock carries the real checksum")
    _check(
        "Pathname(__dir__)" in formula and "buildpath" not in formula.split("seed.lock")[0][-80:],
        "formula installs the lock from the tap directory, not the downloaded payload",
    )


def _check_staged_metadata_is_rerenderable(output_root: Path) -> None:
    rerender_root = output_root.parent / "rerendered"
    result = subprocess.run(
        [
            sys.executable,
            str(_RENDERER),
            "--metadata",
            str(output_root / "release_metadata.json"),
            "--manifest",
            str(output_root / "release_manifest.json"),
            "--output-root",
            str(rerender_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(result.returncode == 0, f"staged metadata re-renders: {result.stderr}")
    for relative_path in ("Formula/solet.rb", "solet_cli/homebrew/seed.lock.json"):
        _check(
            (output_root / relative_path).read_bytes()
            == (rerender_root / relative_path).read_bytes(),
            f"re-rendered {relative_path} is byte-identical",
        )


def _install_source_receipt(formula: str) -> dict[str, object]:
    marker = '(libexec/"share"/"solet"/"install-source.json").write <<~\'JSON\'\n'
    start = formula.find(marker)
    end = formula.find("    JSON\n", start + len(marker))
    _check(start != -1 and end != -1, "Formula embeds an install-source receipt")
    if start == -1 or end == -1:
        raise AssertionError("install-source receipt is absent")
    value: object = json.loads(formula[start + len(marker) : end])
    _check(isinstance(value, dict), "install-source receipt is a JSON object")
    if not isinstance(value, dict):
        raise AssertionError("install-source receipt is not an object")
    return value


def _release_manifest_receipt(formula: str) -> dict[str, object]:
    marker = '(libexec/"share"/"solet"/"release_manifest.json").write <<~\'JSON\'\n'
    start = formula.find(marker)
    end = formula.find("    JSON\n", start + len(marker))
    _check(start != -1 and end != -1, "Formula embeds the stage-5 release manifest")
    if start == -1 or end == -1:
        raise AssertionError("release manifest is absent from the Formula")
    value: object = json.loads(formula[start + len(marker) : end])
    _check(isinstance(value, dict), "embedded release manifest is a JSON object")
    if not isinstance(value, dict):
        raise AssertionError("embedded release manifest is not an object")
    return value


def _check_dev_mode_stage(
    checkout: Path,
    manager_checkout: Path,
    manager_ref: str,
    root: Path,
) -> None:
    output = root / "stage-dev"
    _stage(checkout, manager_checkout, manager_ref, output, dev_mode=True)
    metadata = json.loads((output / "release_metadata.json").read_text(encoding="utf-8"))
    archive = _payload_archive_path(output)
    _check(
        metadata["install_mode"] == "dev",
        "dev stage records dev mode in release metadata",
    )
    _check(
        metadata["manager_url"] == archive.resolve().as_uri(),
        "dev Formula URL resolves to the worktree-built payload archive",
    )
    formula = (output / "Formula" / "solet.rb").read_text(encoding="utf-8")
    receipt = _install_source_receipt(formula)
    _check(
        receipt
        == {
            "schema_version": 1,
            "mode": "dev",
            "source_commit": manager_ref,
        },
        "dev Formula installs mode and immutable source commit receipt",
    )

    mistaken_release = dict(metadata)
    mistaken_release["install_mode"] = "release"
    metadata_path = root / "dev-claimed-as-release.json"
    metadata_path.write_text(json.dumps(mistaken_release), encoding="utf-8")
    refusal = subprocess.run(
        [
            sys.executable,
            str(_RENDERER),
            "--metadata",
            str(metadata_path),
            "--manifest",
            str(output / "release_manifest.json"),
            "--output-root",
            str(root / "dev-claimed-as-release"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        refusal.returncode != 0 and "manager_url" in refusal.stderr,
        "a dev file payload cannot be rendered as release provenance",
    )


def _check_lock_only(
    checkout: Path,
    output_root: Path,
    expected_commit: str,
    expected_tree: str,
) -> None:
    result = _stage_lock_only(checkout, output_root)
    _check(result.returncode == 0, f"stage_release.py --lock-only failed: {result.stderr}")
    _check(not (output_root / "payload").exists(), "--lock-only builds no payload archive")
    _check(not (output_root / "Formula").exists(), "--lock-only renders no Formula")
    lock = json.loads((output_root / "seed.lock.json").read_text(encoding="utf-8"))
    _check(
        lock["repository"] == _OTHER_SEED_REPOSITORY
        and lock["profile"] == _OTHER_SEED_PROFILE
        and lock["release_tag"] == _RELEASE_TAG,
        "--lock-only lock carries the given (repository, profile, tag), not the canonical seed's",
    )
    _check(
        lock["commit"] == expected_commit and lock["tree_hash"] == expected_tree,
        "--lock-only resolves commit/tree_hash from real git, same core as the full path",
    )
    _check(
        "archive_sha256" not in lock,
        "--lock-only omits archive_sha256 entirely — absence, not a placeholder value",
    )


def _check_refused_invocation(checkout: Path) -> None:
    manager_ref = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(checkout),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            _CANONICAL_SEED_REPOSITORY,
            "--seed-profile",
            _CANONICAL_SEED_PROFILE,
            "--manager-repository",
            _MANAGER_REPOSITORY,
            "--manager-release-tag",
            _MANAGER_RELEASE_TAG,
            "--manager-checkout",
            str(checkout),
            "--manager-ref",
            manager_ref,
            "--manager-source-repository",
            _MANAGER_SOURCE_REPOSITORY,
            "--output-root",
            str(checkout / "refused-output"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        result.returncode != 0
        and "--output-root must be outside every source tree" in result.stderr,
        f"stage_release.py refuses an output root inside its source tree: {result.stderr}",
    )


def _check_refused_manager_refs(checkout: Path) -> None:
    for manager_ref in ("HEAD", "main", "master", "latest", "release-branch"):
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--seed-checkout",
                str(checkout),
                "--release-tag",
                _RELEASE_TAG,
                "--seed-repository",
                _CANONICAL_SEED_REPOSITORY,
                "--seed-profile",
                _CANONICAL_SEED_PROFILE,
                "--manager-repository",
                _MANAGER_REPOSITORY,
                "--manager-release-tag",
                _MANAGER_RELEASE_TAG,
                "--manager-checkout",
                str(checkout),
                "--manager-ref",
                manager_ref,
                "--manager-source-repository",
                _MANAGER_SOURCE_REPOSITORY,
                "--output-root",
                str(checkout.parent / f"refused-{manager_ref}"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        _check(
            result.returncode != 0
            and "--manager-ref has an invalid immutable identity shape" in result.stderr,
            f"stage_release.py refuses moving --manager-ref {manager_ref!r}: {result.stderr}",
        )


def _check_manager_checkout_must_be_pinned(
    checkout: Path, manager_ref: str, shared_head: str, output_root: Path
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--seed-checkout",
            str(checkout),
            "--release-tag",
            _RELEASE_TAG,
            "--seed-repository",
            _CANONICAL_SEED_REPOSITORY,
            "--seed-profile",
            _CANONICAL_SEED_PROFILE,
            "--manager-repository",
            _MANAGER_REPOSITORY,
            "--manager-release-tag",
            _MANAGER_RELEASE_TAG,
            "--manager-checkout",
            str(checkout),
            "--manager-ref",
            manager_ref,
            "--manager-source-repository",
            _MANAGER_SOURCE_REPOSITORY,
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        result.returncode != 0
        and "--manager-checkout must be a Git-Controller-provided worktree pinned" in result.stderr
        and manager_ref in result.stderr
        and shared_head in result.stderr,
        f"stage_release.py refuses a manager checkout not pinned at --manager-ref: {result.stderr}",
    )


def _check_manager_checkout_must_be_clean(
    checkout: Path, manager_ref: str, output_root: Path
) -> None:
    dirty_path = checkout / "dirty-manager.marker"
    dirty_path.write_text("uncommitted manager change\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--seed-checkout",
                str(checkout),
                "--release-tag",
                _RELEASE_TAG,
                "--seed-repository",
                _CANONICAL_SEED_REPOSITORY,
                "--seed-profile",
                _CANONICAL_SEED_PROFILE,
                "--manager-repository",
                _MANAGER_REPOSITORY,
                "--manager-release-tag",
                _MANAGER_RELEASE_TAG,
                "--manager-checkout",
                str(checkout),
                "--manager-ref",
                manager_ref,
                "--manager-source-repository",
                _MANAGER_SOURCE_REPOSITORY,
                "--output-root",
                str(output_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        dirty_path.unlink()
    _check(
        result.returncode != 0
        and "--manager-checkout must be clean before staging" in result.stderr,
        f"stage_release.py refuses a dirty manager checkout: {result.stderr}",
    )


def _read_release_manifest(output_root: Path) -> dict[str, object]:
    return json.loads((output_root / "release_manifest.json").read_text(encoding="utf-8"))


def _check_release_label_and_manifest(
    checkout: Path,
    manager_checkout: Path,
    manager_ref: str,
    root: Path,
    expected_manager_commit: str,
    expected_manager_tree: str,
) -> None:
    """The release-label discriminator folds into the asset filename, and the
    draft release_manifest.json (design §7.1) carries the seed+manager
    sections with real per-file digests -- everything stage 5 owns."""
    output = root / "stage-labeled"
    _stage(checkout, manager_checkout, manager_ref, output, release_label="r44")
    archive = _payload_archive_path(output)
    real_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    _check(
        archive.name == "solet-0.1.0-r44.tar.gz",
        f"--release-label folds into the asset filename: got {archive.name}",
    )
    metadata = json.loads((output / "release_metadata.json").read_text(encoding="utf-8"))
    _check(
        metadata["manager_url"].endswith("/solet-0.1.0-r44.tar.gz"),
        "manager_url carries the discriminated asset name",
    )

    manifest = _read_release_manifest(output)
    formula = (output / "Formula" / "solet.rb").read_text(encoding="utf-8")
    _check(
        _release_manifest_receipt(formula) == manifest,
        "iss_18c47206: the Formula installs into the keg the exact same stage-5 "
        "draft manifest that was staged beside it",
    )
    _check(manifest["schema_version"] == 1, "release manifest declares schema_version 1")
    _check(manifest["release_label"] == "r44", "release manifest carries the release label")
    _check(
        manifest["manager_release_tag"] == _MANAGER_RELEASE_TAG,
        "release manifest carries the manager release tag",
    )
    for null_section in (
        "components",
        "bundle_verdict",
        "guest_validation",
        "tap",
        "surface_digests",
        "produced_by",
        "factory_signature",
    ):
        _check(
            manifest[null_section] is None,
            f"stage-5 draft leaves {null_section} null (a later publish_release stage fills it)",
        )

    seed_section = manifest["seed"]
    _check(
        seed_section["source_commit"] == expected_manager_commit,
        "seed.source_commit (from PROVENANCE.json) matches the paired manager commit "
        "in this hermetically-paired fixture",
    )
    manager_section = manifest["manager"]
    _check(
        manager_section["source_commit"] == expected_manager_commit
        and manager_section["source_tree_hash"] == expected_manager_tree,
        "manager section carries the resolved manager identity",
    )
    _check(
        manager_section["payload_asset"]
        == {
            "name": "solet-0.1.0-r44.tar.gz",
            "url": metadata["manager_url"],
            "sha256": f"sha256:{real_sha256}",
        },
        "manager.payload_asset uses the staged archive's prefixed digest identity",
    )
    _check(manager_section["allow_manager_seed_skew"] is None, "no skew override was requested")

    file_digests = manager_section["file_digests"]
    _check(
        "solet_cli/src.marker" in file_digests
        and file_digests["solet_cli/src.marker"].startswith("sha256:"),
        "manager.file_digests carries a real per-file digest for an archived file",
    )
    with tarfile.open(archive, mode="r:gz") as tar:
        marker_member = tar.extractfile("solet_cli/src.marker")
        marker_bytes = marker_member.read() if marker_member is not None else None
        regular_file_members = {member.name for member in tar.getmembers() if member.isfile()}
    _check(
        marker_bytes is not None
        and file_digests["solet_cli/src.marker"] == f"sha256:{hashlib.sha256(marker_bytes).hexdigest()}",
        "the recorded per-file digest is the archived member's real sha256",
    )
    _check(
        set(file_digests) == regular_file_members,
        "file_digests has exactly one entry per archived regular-file member, no more, no fewer",
    )


def _check_release_label_shape_refused(checkout: Path, manager_checkout: Path, manager_ref: str, root: Path) -> None:
    result = _stage(
        checkout,
        manager_checkout,
        manager_ref,
        root / "stage-bad-label",
        release_label="not-a-label",
        expect_success=False,
    )
    _check(
        result.returncode != 0
        and "--release-label has an invalid shape" in result.stderr,
        f"stage_release.py refuses a malformed --release-label: {result.stderr}",
    )


def _check_manager_version_disagreement_refused(root: Path) -> None:
    fixture_root = root / "version-skew-fixture"
    fixture_root.mkdir(parents=True)
    checkout, manager_checkout, manager_ref = _build_seed_fixture(
        fixture_root, manager_version="9.9.9"
    )
    result = _stage(
        checkout,
        manager_checkout,
        manager_ref,
        root / "stage-version-skew",
        expect_success=False,
    )
    _check(
        result.returncode != 0
        and "manager version disagreement" in result.stderr
        and "9.9.9" in result.stderr
        and "0.1.0" in result.stderr,
        f"stage_release.py refuses a MANAGER_VERSION/pyproject-version disagreement: {result.stderr}",
    )


def _ruby_binary() -> tuple[Path, str]:
    """Homebrew's own portable Ruby (what actually runs a Formula's `install`
    at `brew install` time) if present, else whatever `ruby` is on PATH.
    Reviewer-verified interpreter for Finding 1 of the Opus review
    (`reissue_manifest_in_keg_review.md`): an unquoted `<<~JSON` heredoc is
    double-quote semantics, so Ruby re-escapes `\\`/`"`/newline and executes
    `#{...}` in whatever free-text `allow_manager_seed_skew` reason a caller
    of `--allow-manager-seed-skew` records -- json.loads-ing the raw
    rendered-Formula text (as the other checks in this file legitimately do,
    for shape) can never see that, because it skips the Ruby step entirely."""
    if _PORTABLE_RUBY.is_file():
        return _PORTABLE_RUBY, "Homebrew portable Ruby"
    found = shutil.which("ruby")
    if found is not None:
        return Path(found), "system ruby on PATH"
    print("SKIP  Ruby-heredoc delivery check: no Homebrew portable Ruby and no system ruby on PATH")
    raise SystemExit(77)


def _heredoc_bytes_via_ruby(formula: str, install_name: str, ruby: Path, work_dir: Path) -> bytes:
    """The exact bytes Ruby writes for one `(...).write <<~...JSON` heredoc in
    a rendered Formula, run through the real interpreter -- not a Python-side
    slice-and-json.loads, which is blind to Ruby's own string processing."""
    match = None
    for candidate in _HEREDOC_WRITE_CALL.finditer(formula):
        if candidate.group("name") == install_name:
            match = candidate
            break
    _check(match is not None, f"formula writes {install_name!r} through a `.write <<~JSON` heredoc")
    if match is None:
        raise AssertionError(f"{install_name} heredoc call is absent")
    script = work_dir / f"heredoc-{install_name}.rb"
    script.write_text(f"print {match['opener']}\n{match['body']}JSON\n", encoding="utf-8")
    result = subprocess.run([str(ruby), str(script)], check=False, capture_output=True)
    _check(result.returncode == 0, f"Ruby evaluates the {install_name} heredoc: {result.stderr.decode(errors='replace')}")
    return result.stdout


def _check_heredocs_are_quoted(formula: str) -> None:
    """Cheap, ruby-free floor for every JSON heredoc in the Formula (Finding 1,
    'Class, not site'): the opener must be the non-interpolating `<<~'JSON'`,
    never the bare `<<~JSON` an accidental revert would reintroduce."""
    names = {match["name"] for match in _HEREDOC_WRITE_CALL.finditer(formula)}
    _check(
        names
        == {
            "seed.lock.json",
            "existing_install_inspection_seed_lock_catalog.v1.json",
            "install-source.json",
            "release_manifest.json",
        },
        f"formula writes exactly the four expected JSON heredocs: {sorted(names)}",
    )
    for match in _HEREDOC_WRITE_CALL.finditer(formula):
        _check(
            match["opener"] == "<<~'JSON'",
            f"{match['name']} heredoc opener is the non-interpolating quoted form, not {match['opener']!r}",
        )


def _check_release_manifest_heredoc_survives_ruby(root: Path) -> None:
    """iss_18c47206, Finding 1: an adversarial `allow_manager_seed_skew` reason
    (a quote, a backslash, a newline, and a Ruby interpolation attempt) must
    round-trip byte-identically through Homebrew's own Ruby, and the gate
    must still grade the keg `skew_allowed` from those Ruby-written bytes --
    not from the pre-Ruby Python rendering."""
    ruby, ruby_label = _ruby_binary()
    print(f"  (using {ruby_label}: {ruby})")
    adversarial_reason = 'reissue "r46" \\ line1\nline2 #{1+1}'
    fixture_root = root / "heredoc-fixture"
    fixture_root.mkdir(parents=True)
    checkout, manager_checkout, manager_ref = _build_seed_fixture(
        fixture_root, provenance_source_commit="f" * 40
    )
    adversarial_output = root / "stage-heredoc-adversarial"
    _stage(
        checkout,
        manager_checkout,
        manager_ref,
        adversarial_output,
        allow_manager_seed_skew=adversarial_reason,
    )
    formula = (adversarial_output / "Formula" / "solet.rb").read_text(encoding="utf-8")
    _check_heredocs_are_quoted(formula)

    work_dir = root / "heredoc-ruby"
    work_dir.mkdir(parents=True)
    manifest_bytes = _heredoc_bytes_via_ruby(formula, "release_manifest.json", ruby, work_dir)
    parsed: object = json.loads(manifest_bytes)
    _check(isinstance(parsed, dict), "Ruby writes valid, parseable JSON for the adversarial reason")
    manager_section = parsed["manager"] if isinstance(parsed, dict) else {}
    _check(
        isinstance(manager_section, dict) and manager_section.get("allow_manager_seed_skew") == adversarial_reason,
        "Ruby writes the adversarial reason byte-identically -- no re-escaping, no interpolation: "
        f"got {manager_section.get('allow_manager_seed_skew')!r}",
    )

    receipt_bytes = _heredoc_bytes_via_ruby(formula, "install-source.json", ruby, work_dir)
    share_dir = root / "keg-heredoc-adversarial" / "share" / "solet"
    share_dir.mkdir(parents=True)
    receipt_path = share_dir / "install-source.json"
    receipt_path.write_bytes(receipt_bytes)
    manifest_path = share_dir / "release_manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    seed = parse_seed_lock_bytes((adversarial_output / "solet_cli" / "homebrew" / "seed.lock.json").read_bytes())
    verdict = pair_manager_and_seed(seed, install_source_path=receipt_path, manifest_path=manifest_path)
    _check(
        verdict["verdict"] == "skew_allowed" and verdict["allow_manager_seed_skew"] == adversarial_reason,
        f"the gate grades skew_allowed from the Ruby-written keg bytes, with the reason intact: {verdict}",
    )


def _write_keg_receipts(share_dir: Path, formula: str) -> tuple[Path, Path]:
    """Materialize a Formula's embedded ``install-source.json`` and
    ``release_manifest.json`` exactly as its real ``install`` method writes
    them into a keg's ``share/solet/`` -- so the gate below reads the same
    bytes a real ``brew install`` would produce, not a hand-typed fixture."""
    share_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = share_dir / "install-source.json"
    receipt_path.write_text(json.dumps(_install_source_receipt(formula)), encoding="utf-8")
    manifest_path = share_dir / "release_manifest.json"
    manifest_path.write_text(json.dumps(_release_manifest_receipt(formula)), encoding="utf-8")
    return receipt_path, manifest_path


def _check_manifest_reaches_consumption_gate(root: Path, output_a: Path, overridden_output: Path) -> None:
    """iss_18c47206, proved end to end against REAL rendered kegs (not the
    gate's own pure-function fixtures -- ``release_identity_gate_smoke.py``
    already covers those): a regression that breaks the render/Formula
    wiring, and not just the gate's own verdict logic, is caught here too.

    (c) same-revision, unaffected; (a) a manager-only reissue's recorded
    ``allow_manager_seed_skew`` reason now actually reaches the gate; (b) the
    identical skew with no recorded reason still refuses, fail-closed."""
    paired_formula = (output_a / "Formula" / "solet.rb").read_text(encoding="utf-8")
    paired_seed = parse_seed_lock_bytes((output_a / "solet_cli" / "homebrew" / "seed.lock.json").read_bytes())
    paired_receipt, paired_manifest = _write_keg_receipts(root / "keg-paired" / "share" / "solet", paired_formula)
    paired = pair_manager_and_seed(paired_seed, install_source_path=paired_receipt, manifest_path=paired_manifest)
    _check(
        paired["verdict"] == "paired" and paired["reason"] is None,
        f"(c) a same-revision release stays paired now that its keg carries a manifest too: {paired}",
    )

    skewed_formula = (overridden_output / "Formula" / "solet.rb").read_text(encoding="utf-8")
    skewed_seed = parse_seed_lock_bytes((overridden_output / "solet_cli" / "homebrew" / "seed.lock.json").read_bytes())
    skewed_receipt, skewed_manifest = _write_keg_receipts(root / "keg-skew-allowed" / "share" / "solet", skewed_formula)
    allowed = pair_manager_and_seed(skewed_seed, install_source_path=skewed_receipt, manifest_path=skewed_manifest)
    _check(
        allowed["verdict"] == "skew_allowed"
        and allowed["allow_manager_seed_skew"] == "smoke: intentionally mismatched fixture",
        f"(a) a manager-only reissue's --allow-manager-seed-skew reason, now installed in the keg, reaches the gate: {allowed}",
    )

    silenced = json.loads(skewed_manifest.read_text(encoding="utf-8"))
    silenced["manager"]["allow_manager_seed_skew"] = None
    silenced_path = root / "keg-skew-silent" / "release_manifest.json"
    silenced_path.parent.mkdir(parents=True)
    silenced_path.write_text(json.dumps(silenced), encoding="utf-8")
    refused = pair_manager_and_seed(skewed_seed, install_source_path=skewed_receipt, manifest_path=silenced_path)
    _check(
        refused["verdict"] == "skew" and refused["reason"] == "manager_seed_revision_skew",
        f"(b) the identical skew with no recorded reason still refuses -- the gate stays fail-closed: {refused}",
    )


def _check_same_source_revision(root: Path, output_a: Path) -> None:
    """iss_da99a951: a seed whose PROVENANCE.source_commit names a DIFFERENT
    manager commit than the one actually being staged is refused, unless the
    caller explicitly records an override."""
    fixture_root = root / "revision-skew-fixture"
    fixture_root.mkdir(parents=True)
    checkout, manager_checkout, manager_ref = _build_seed_fixture(
        fixture_root, provenance_source_commit="f" * 40
    )
    refused = _stage(
        checkout,
        manager_checkout,
        manager_ref,
        root / "stage-revision-skew",
        expect_success=False,
    )
    _check(
        refused.returncode != 0
        and "manager/seed revision skew" in refused.stderr
        and manager_ref in refused.stderr
        and "f" * 40 in refused.stderr,
        f"stage_release.py refuses a manager/seed source-commit mismatch: {refused.stderr}",
    )

    overridden_output = root / "stage-revision-skew-overridden"
    _stage(
        checkout,
        manager_checkout,
        manager_ref,
        overridden_output,
        allow_manager_seed_skew="smoke: intentionally mismatched fixture",
    )
    manifest = _read_release_manifest(overridden_output)
    _check(
        manifest["manager"]["allow_manager_seed_skew"] == "smoke: intentionally mismatched fixture",
        "--allow-manager-seed-skew's reason is recorded on the release manifest",
    )
    _check_manifest_reaches_consumption_gate(root, output_a, overridden_output)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checkout, manager_checkout, manager_ref = _build_seed_fixture(root)
        shared_head_commit = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
        expected_commit = _run_git(checkout, "rev-parse", _RELEASE_TAG).stdout.strip()
        expected_tree = _run_git(checkout, "rev-parse", f"{_RELEASE_TAG}^{{tree}}").stdout.strip()
        expected_manager_commit = manager_ref
        expected_manager_tree = _run_git(
            manager_checkout, "rev-parse", "HEAD^{tree}"
        ).stdout.strip()
        _check(
            expected_manager_commit != shared_head_commit,
            "shared checkout HEAD differs from the manager source commit",
        )

        output_a = root / "stage-a"
        stage_a = _stage(checkout, manager_checkout, manager_ref, output_a)

        metadata = json.loads((output_a / "release_metadata.json").read_text(encoding="utf-8"))
        _check_metadata(
            metadata,
            expected_commit,
            expected_tree,
            manager_ref,
            expected_manager_commit,
            expected_manager_tree,
        )
        archive_path = _payload_archive_path(output_a)
        formula = (output_a / "Formula" / "solet.rb").read_text(encoding="utf-8")
        _check_heredocs_are_quoted(formula)
        real_sha256 = _check_archive(archive_path, metadata, formula)
        _check_contract_digest_implementation_parity(
            checkout,
            _staged_contract_digest(stage_a),
        )
        _check_extracted_contract_bundle(
            archive_path,
            expected_commit,
            _staged_contract_digest(stage_a),
            root,
        )
        _check_rendered_outputs(output_a, real_sha256)
        _check_staged_metadata_is_rerenderable(output_a)
        _check_dev_mode_stage(checkout, manager_checkout, manager_ref, root)
        _check_archive_guard_rejects_pre_fix_omission()
        _check_release_label_and_manifest(
            checkout, manager_checkout, manager_ref, root, expected_manager_commit, expected_manager_tree
        )
        _check_release_label_shape_refused(checkout, manager_checkout, manager_ref, root)
        _check_manager_version_disagreement_refused(root)
        _check_same_source_revision(root, output_a)
        _check_release_manifest_heredoc_survives_ruby(root)

        output_b = root / "stage-b"
        _stage(checkout, manager_checkout, manager_ref, output_b)
        archive_path_b = _payload_archive_path(output_b)
        _check(
            archive_path.read_bytes() == archive_path_b.read_bytes(),
            "staging the same commit twice is byte-for-byte reproducible",
        )

        _check_lock_only(checkout, root / "stage-lock-only", expected_commit, expected_tree)
        _check_refused_invocation(manager_checkout)
        _check_refused_manager_refs(manager_checkout)
        _check_manager_checkout_must_be_pinned(
            checkout, manager_ref, shared_head_commit, root / "unpinned-manager-output"
        )
        _check_manager_checkout_must_be_clean(
            manager_checkout, manager_ref, root / "dirty-manager-output"
        )

    print(f"stage_release_smoke: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
