"""``solet attest`` -- the installation attestation (design §7.4).

Produces ``installation_attestation.json``: what this keg and its managed
instances ACTUALLY are, measured, beside what the release declared they
would be.  No running solet is required (the runtime section says
``solet_not_running`` when there is none); everything else is read from
disk, git, and the host.

Sections, each graded ``verified`` / ``drifted`` / ``unattestable[reason]``
by :mod:`release_identity`:

- ``manager``   the receipt's ``source_commit`` plus a real sha256 of every
                installed ``solet_manager`` file against the manifest's
                ``manager.file_digests``.
- ``pairing``   the §7.3 manager<->seed same-revision verdict.
- ``instances`` per managed instance: the seed checkout (git HEAD, tree,
                dirty list, per-plugin subtree hashes), the running process
                (``attest_runtime_code`` over the target's bridge), and the
                transaction-journal identity with the applied-update history.
- ``environment`` OS build, Homebrew closure versions, models served.

The manifest comes from ``--against`` (a file, or a release tag resolved
through ambient ``gh`` into the manager cache) or from the keg's default
location; when none is reachable every manifest-dependent grade is
``unattestable[release_manifest_absent]`` and the measured side is still
recorded, because the measurements are what a customer report needs.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .attest_environment import environment_facts, probe_runtime
from .errors import SourceError
from .maintenance_inventory import read_maintenance_inventory_v2
from .maintenance_journal import utc_now
from .models import CommandResult, ExitCode, InstanceRecord, JsonValue
from .paths import ManagerPaths
from .registry import InstanceRegistry
from .release_identity import (
    ATTESTATION_FORMAT,
    NOT_CRYPTOGRAPHIC,
    RELEASE_MANIFEST_NAME,
    STATUS_DRIFTED,
    STATUS_VERIFIED,
    CommandRunner,
    InstallSource,
    compare_manager,
    compare_runtime,
    compare_seed_checkout,
    installed_file_digests,
    load_install_source,
    load_release_manifest,
    manifest_sha256,
    run_command,
)
from .release_identity_gate import (
    VERDICT_INCONSISTENT,
    VERDICT_SKEW,
    default_install_source_path,
    default_release_manifest_path,
    pair_manager_and_seed,
)
from .release_lock import load_seed_lock
from .state_io import atomic_write_json
from .transaction import load_transaction
from .update_journal import read_update_journal

ATTESTATION_FILE_NAME = "installation_attestation.json"
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OWNER_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}$")
_GH_TIMEOUT_S = 120
_NOT_MEASURED_STATUSES = frozenset({STATUS_VERIFIED, STATUS_DRIFTED})


@dataclass(frozen=True)
class AttestSeams:
    """Every host touchpoint, injectable so a fixture tree drives the whole command."""

    runner: CommandRunner = run_command
    package_root: Path | None = None
    install_source_path: Path | None = None
    manifest_path: Path | None = None
    seed_lock_path: Path | None = None
    models_endpoint: str | None = None


@dataclass(frozen=True)
class AttestRequest:
    paths: ManagerPaths
    name: str | None = None
    against: str | None = None
    release_repository: str | None = None
    output: Path | None = None


def run_attest(request: AttestRequest, seams: AttestSeams | None = None) -> CommandResult:
    """Build, persist and return the attestation for the keg and its instances."""

    seams = AttestSeams() if seams is None else seams
    manifest_section, manifest = _resolve_manifest(request, seams)
    package_root = Path(__file__).resolve().parent if seams.package_root is None else seams.package_root
    receipt_path = default_install_source_path() if seams.install_source_path is None else seams.install_source_path
    install_source, receipt_error = _read_install_source(receipt_path)
    manager = compare_manager(installed_file_digests(package_root), install_source, manifest)
    if receipt_error is not None:
        manager["reason"] = receipt_error
    manager["package_root"] = str(package_root)
    pairing = _pairing(seams, receipt_path, manifest)
    instances = [_attest_instance(request.paths, record, manifest, seams) for record in _records(request)]
    document: dict[str, JsonValue] = {
        "format": ATTESTATION_FORMAT,
        "not_cryptographic": NOT_CRYPTOGRAPHIC,
        "produced_at": utc_now(),
        "manager_python_prefix": sys.prefix,
        "manifest": manifest_section,
        "manager": manager,
        "pairing": pairing,
        "instances": cast(list[JsonValue], instances),
        "environment": environment_facts(seams.runner) if seams.models_endpoint is None else environment_facts(seams.runner, seams.models_endpoint),
    }
    output = request.paths.state_dir / "attestations" / ATTESTATION_FILE_NAME if request.output is None else request.output
    atomic_write_json(output, document)
    return _result(document, output)


def _resolve_manifest(request: AttestRequest, seams: AttestSeams) -> tuple[dict[str, JsonValue], dict[str, JsonValue] | None]:
    section: dict[str, JsonValue] = {"source": None, "status": "absent", "release_label": None, "manager_release_tag": None, "sha256": None, "error": None}
    path = _manifest_path(request, seams, section)
    if path is None:
        return section, None
    section["source"] = str(path)
    try:
        manifest = load_release_manifest(path)
    except SourceError as exc:
        section["status"], section["error"] = "unreadable", str(exc)
        return section, None
    section["status"] = "loaded"
    section["release_label"] = manifest.get("release_label")
    section["manager_release_tag"] = manifest.get("manager_release_tag")
    section["sha256"] = manifest_sha256(manifest)
    return section, manifest


def _manifest_path(request: AttestRequest, seams: AttestSeams, section: dict[str, JsonValue]) -> Path | None:
    if request.against is None:
        default = default_release_manifest_path() if seams.manifest_path is None else seams.manifest_path
        return default if default.exists() else None
    candidate = Path(request.against).expanduser()
    if candidate.is_file():
        return candidate
    if _TAG.fullmatch(request.against) is None:
        raise SourceError(f"--against must name a readable release_manifest.json or a release tag: {request.against!r}")
    return _download_manifest(request, seams, section)


def _download_manifest(request: AttestRequest, seams: AttestSeams, section: dict[str, JsonValue]) -> Path | None:
    """Resolve a tag to its published manifest asset through ambient ``gh`` (no token is ever read here)."""

    repository = request.release_repository
    if repository is None or _OWNER_REPO.fullmatch(repository) is None:
        raise SourceError("--against <tag> needs --release-repository OWNER/REPO naming the manager release repository")
    tag = cast(str, request.against)
    destination = request.paths.cache_dir / "attest" / "manifests" / tag
    destination.mkdir(parents=True, exist_ok=True)
    outcome = seams.runner(
        ("gh", "release", "download", tag, "--repo", repository, "--pattern", RELEASE_MANIFEST_NAME, "--dir", str(destination), "--clobber"),
        None,
        _GH_TIMEOUT_S,
    )
    if outcome.returncode != 0:
        section["status"], section["error"] = "unreachable", f"gh release download {tag}: {outcome.stderr.strip()[-300:]}"
        return None
    return destination / RELEASE_MANIFEST_NAME


def _read_install_source(path: Path) -> tuple[InstallSource | None, str | None]:
    if not path.exists():
        return None, "install_source_absent"
    try:
        return load_install_source(path), None
    except SourceError as exc:
        return None, f"install_source_unreadable: {exc}"


def _pairing(seams: AttestSeams, receipt_path: Path, manifest: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    lock_path = Path(sys.prefix) / "share" / "solet" / "seed.lock.json" if seams.seed_lock_path is None else seams.seed_lock_path
    try:
        seed = load_seed_lock(lock_path)
    except SourceError as exc:
        return {"verdict": "unpairable", "reason": f"manager_seed_lock_unreadable: {exc}", "seed_lock_path": str(lock_path)}
    verdict = pair_manager_and_seed(seed, install_source_path=receipt_path, manifest_path=seams.manifest_path, manifest=manifest)
    verdict["seed_lock_path"] = str(lock_path)
    return verdict


def _records(request: AttestRequest) -> tuple[InstanceRecord, ...]:
    registry = InstanceRegistry(request.paths.registry_path)
    if request.name is None:
        return registry.list()
    return (registry.require(request.name),)


def _attest_instance(paths: ManagerPaths, record: InstanceRecord, manifest: dict[str, JsonValue] | None, seams: AttestSeams) -> dict[str, JsonValue]:
    target = Path(record.target)
    observed, reason = probe_runtime(target, seams.runner)
    return {
        "name": record.name,
        "target": record.target,
        "lifecycle_state": record.lifecycle_state,
        "seed_checkout": compare_seed_checkout(target, manifest, seams.runner),
        "runtime": compare_runtime(observed, reason, manifest),
        "journal": _journal_identity(paths, record),
    }


def _journal_identity(paths: ManagerPaths, record: InstanceRecord) -> dict[str, JsonValue]:
    """The identities the manager itself journaled, plus every applied-update record."""

    section: dict[str, JsonValue] = {
        "registry": {"seed_commit": record.seed_commit, "seed_tree_hash": record.seed_tree_hash, "seed_tag": record.seed_tag},
        "transaction": None,
        "applied_updates": [],
    }
    transaction = load_transaction(paths.transaction_path(record.name))
    if transaction is not None:
        identity = transaction.seed.identity_dict()
        identity.update({"status": transaction.status.value, "result_kind": transaction.result_kind, "updated_at": transaction.updated_at})
        section["transaction"] = identity
    section["applied_updates"] = cast(list[JsonValue], _applied_updates(paths, record.name))
    return section


def _applied_updates(paths: ManagerPaths, name: str) -> list[dict[str, JsonValue]]:
    """Update journals live under the v2 inventory's instance_id; a create-only instance has none."""

    record = next((item for item in read_maintenance_inventory_v2(paths.maintenance_inventory_path) if item.name == name), None)
    if record is None:
        return []
    instance_dir = paths.operations_dir / record.instance_id
    if not instance_dir.is_dir():
        return []
    rows = (_update_row(journal_path) for journal_path in sorted(instance_dir.glob("*.json")))
    return [row for row in rows if row is not None]


def _update_row(path: Path) -> dict[str, JsonValue] | None:
    try:
        journal = read_update_journal(path)
    except Exception:  # noqa: BLE001 - a foreign or damaged journal is skipped by name, not fatal to the report
        return None
    if journal.get("kind") != "update":
        return None
    baseline = cast(dict[str, JsonValue], journal["baseline"])
    candidate = cast(dict[str, JsonValue], journal["candidate"])
    return {
        "operation_id": journal["operation_id"],
        "instance_id": journal["instance_id"],
        "status": journal["status"],
        "source_mode": journal["source_mode"],
        "baseline_commit": baseline["commit"],
        "candidate_commit": candidate["commit"],
        "candidate_tag": candidate["tag"],
        "updated_at": journal["updated_at"],
    }


def _result(document: dict[str, JsonValue], output: Path) -> CommandResult:
    manager = cast(dict[str, JsonValue], document["manager"])
    pairing = cast(dict[str, JsonValue], document["pairing"])
    instances = cast(list[dict[str, JsonValue]], document["instances"])
    statuses = _section_statuses(manager, instances)
    drifted = STATUS_DRIFTED in statuses or pairing["verdict"] in {VERDICT_SKEW, VERDICT_INCONSISTENT}
    return CommandResult(
        kind="installation_attestation",
        status=_overall_status(statuses, drifted),
        message=f"{NOT_CRYPTOGRAPHIC} Attestation written to {output}.",
        exit_code=ExitCode.HUMAN_ACTION if drifted else ExitCode.OK,
        error_kind="release_identity_drift" if drifted else None,
        repair="Compare the drifted paths and components against the manifest before trusting this installation." if drifted else None,
        data={"output": str(output), "manager_status": manager["status"], "pairing_verdict": pairing["verdict"], "instances": len(instances), "section_statuses": statuses, "attestation": document},
    )


def _section_statuses(manager: dict[str, JsonValue], instances: list[dict[str, JsonValue]]) -> list[JsonValue]:
    statuses: list[JsonValue] = [manager["status"]]
    for item in instances:
        statuses.append(cast(dict[str, JsonValue], item["seed_checkout"])["status"])
        statuses.append(cast(dict[str, JsonValue], item["runtime"])["status"])
    return statuses


def _overall_status(statuses: list[JsonValue], drifted: bool) -> str:
    if drifted:
        return "drifted"
    measured = [status for status in statuses if status in _NOT_MEASURED_STATUSES]
    return "verified" if measured and len(measured) == len(statuses) else "partial"


__all__ = ["ATTESTATION_FILE_NAME", "AttestRequest", "AttestSeams", "run_attest"]
