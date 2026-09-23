"""Report-only ``doctor::release_identity_v1`` -- declaration versus reality.

Detection coverage for ``iss_670801d4`` (D-9.7): ``manager_source_commit``
and ``manager_source_tree_hash`` ship in every release and no consumer reads
them back.  This advisory is the consumer.  It runs the same comparison
``solet attest`` runs (:mod:`release_identity`) in the doctor's passive,
report-only form: the installed manager package's real per-file digests and
the receipt's ``source_commit`` against the manifest's declaration, the
target checkout's git identity and per-plugin subtree hashes against the
manifest's, and the §7.3 manager<->seed pairing verdict.

Two things it deliberately does NOT do.  It does not probe the running
process -- the doctor is passive over a target at rest, and the runtime
half is ``solet attest``'s.  And it does not re-implement the archive-digest
consistency check: ``doctor::seed_archive_digest_consistency_v1``
(``doctor_seed_integrity_census``) already compares the two persisted
digests and is honest that it verifies no archive; this advisory CALLS it
and folds its outcome in as the consistency half beside the integrity half
(a real hash of installed bytes) that the census could not supply.

Like every advisory in this family it never blocks: a keg whose manifest
is absent is ``unknown``, never ``verified``, and a divergence is ``warn``
with the drifted paths and components named.  ``NOT_CRYPTOGRAPHIC`` is in
every summary, passing ones included, so a green here is never mistaken
for a signature check.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .doctor_seed_integrity_census import collect_seed_integrity_advisories
from .errors import SourceError
from .models import InstanceRecord, JsonValue
from .release_identity import (
    NOT_CRYPTOGRAPHIC,
    STATUS_DRIFTED,
    STATUS_VERIFIED,
    CommandRunner,
    InstallSource,
    compare_manager,
    compare_seed_checkout,
    installed_file_digests,
    load_install_source,
    load_release_manifest,
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
from .transaction import Transaction

_CHECK_ID = "doctor::release_identity_v1"
_SEED_LOCK_NAME = "seed.lock.json"


def collect_release_identity_advisories(
    record: InstanceRecord,
    transaction: Transaction,
    *,
    manager_seed_lock_path: Path | None = None,
    manifest_path: Path | None = None,
    install_source_path: Path | None = None,
    package_root: Path | None = None,
    runner: CommandRunner = run_command,
) -> list[JsonValue]:
    """Return the one report-only release-identity check for ``record``."""

    seed_lock_path = Path(sys.prefix) / "share" / "solet" / _SEED_LOCK_NAME if manager_seed_lock_path is None else manager_seed_lock_path
    manifest_file = default_release_manifest_path() if manifest_path is None else manifest_path
    receipt_path = default_install_source_path() if install_source_path is None else install_source_path
    root = Path(__file__).resolve().parent if package_root is None else package_root
    consistency = collect_seed_integrity_advisories(record, transaction, manager_seed_lock_path=seed_lock_path)[0]
    return [_release_identity_advisory(record, seed_lock_path, manifest_file, receipt_path, root, runner, consistency)]


def _release_identity_advisory(
    record: InstanceRecord,
    seed_lock_path: Path,
    manifest_path: Path,
    receipt_path: Path,
    package_root: Path,
    runner: CommandRunner,
    consistency: JsonValue,
) -> dict[str, JsonValue]:
    source = str(manifest_path)
    consistency_row = cast(dict[str, JsonValue], consistency)
    observed: dict[str, JsonValue] = {
        "instance_name": record.name,
        "archive_digest_consistency": {"status": consistency_row["status"], "reason_code": consistency_row["reason_code"]},
    }
    expected: dict[str, JsonValue] = {"release_manifest": source, "manager_source_commit": None, "seed_commit": None, "seed_tree_hash": None}
    manifest, unreadable = _manifest(manifest_path)
    install_source = _install_source(receipt_path)
    manager = compare_manager(installed_file_digests(package_root), install_source, manifest)
    checkout = compare_seed_checkout(Path(record.target), manifest, runner)
    observed["manager"] = _manager_summary(manager)
    observed["seed_checkout"] = _checkout_summary(checkout)
    pairing = _pairing(seed_lock_path, receipt_path, manifest)
    observed["pairing"] = pairing
    if manifest is None:
        return advisory_unknown(
            _CHECK_ID,
            f"No release manifest is reachable at {source}, so the installed manager and seed could not be compared to a declaration. {NOT_CRYPTOGRAPHIC}",
            expected,
            observed,
            source,
            "release_manifest_unreadable" if unreadable else "release_manifest_absent",
            unreadable or "Ship release_manifest.json beside seed.lock.json, or run `solet attest --against <release_manifest.json>`.",
        )
    declared_manager = cast(dict[str, JsonValue], manifest.get("manager") or {})
    declared_seed = cast(dict[str, JsonValue], manifest.get("seed") or {})
    expected.update({"manager_source_commit": declared_manager.get("source_commit"), "seed_commit": declared_seed.get("commit"), "seed_tree_hash": declared_seed.get("tree_hash")})
    return _grade(manager, checkout, pairing["verdict"], expected, observed, source)


def _grade(
    manager: dict[str, JsonValue],
    checkout: dict[str, JsonValue],
    pairing_verdict: JsonValue,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    pairing_refused = pairing_verdict in {VERDICT_SKEW, VERDICT_INCONSISTENT}
    statuses = (manager["status"], checkout["status"])
    if all(status == STATUS_VERIFIED for status in statuses) and not pairing_refused:
        return advisory_verified(
            _CHECK_ID,
            f"The installed manager files and the target checkout match the release manifest's declaration ({manager['files_hashed']} files hashed). {NOT_CRYPTOGRAPHIC}",
            expected,
            observed,
            source,
        )
    if STATUS_DRIFTED in statuses or pairing_refused:
        return advisory_warn(
            _CHECK_ID,
            f"The installation DIFFERS from what the release manifest declares (manager: {manager['status']}, seed checkout: {checkout['status']}, pairing: {pairing_verdict}). {NOT_CRYPTOGRAPHIC}",
            expected,
            observed,
            source,
            "release_identity_drift",
            "Run `solet attest` for the full per-file and per-component listing; reinstall the manager or reset the checkout to the declared release before trusting it.",
        )
    return advisory_unknown(
        _CHECK_ID,
        f"Part of the comparison could not be measured (manager: {manager['status']}/{manager['reason']}, seed checkout: {checkout['status']}/{checkout['reason']}). {NOT_CRYPTOGRAPHIC}",
        expected,
        observed,
        source,
        "release_identity_unmeasured",
        "Run `solet attest` for the reason each section could not be measured.",
    )


def _manifest(path: Path) -> tuple[dict[str, JsonValue] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        return load_release_manifest(path), None
    except SourceError as exc:
        return None, str(exc)


def _install_source(path: Path) -> InstallSource | None:
    try:
        return load_install_source(path)
    except SourceError:
        return None


def _pairing(seed_lock_path: Path, receipt_path: Path, manifest: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    try:
        seed = load_seed_lock(seed_lock_path)
    except SourceError as exc:
        return {"verdict": "unpairable", "reason": f"manager_seed_lock_unreadable: {exc}"}
    verdict = pair_manager_and_seed(seed, install_source_path=receipt_path, manifest=manifest)
    return {"verdict": verdict["verdict"], "reason": verdict["reason"], "allow_manager_seed_skew": verdict["allow_manager_seed_skew"]}


def _manager_summary(manager: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "status": manager["status"],
        "reason": manager["reason"],
        "files_hashed": manager["files_hashed"],
        "drifted_paths": manager["drifted_paths"],
        "missing_paths": manager["missing_paths"],
        "unexpected_paths": manager["unexpected_paths"],
        "source_commit_matches": manager["source_commit_matches"],
    }


def _checkout_summary(checkout: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "status": checkout["status"],
        "reason": checkout["reason"],
        "head_commit": checkout["head_commit"],
        "head_tree": checkout["head_tree"],
        "dirty_paths": checkout["dirty_paths"],
        "drifted_components": checkout["drifted_components"],
    }


__all__ = ["collect_release_identity_advisories"]
