"""The recorded seed archive digest is actually compared somewhere.

Detection coverage for ``iss_8b1860e0`` (D-9.4).  ``archive_sha256`` is parsed,
carried, journaled and migrated, and no consumer in ``solet_cli/src`` ever
compares it -- every occurrence is a declaration, a parse, or a serialization.
This smoke pins the comparison that ends that, across the two durable places
the digest is written: the manager seed lock and the target's journal.

The honesty constraint is part of the contract and is asserted here, not just
documented.  Seed acquisition is git-fetch based -- identity is proved by
commit and tree hash and NO archive is ever materialized on the target -- so a
check that claimed to "verify the archive" would be a green tick over an
integrity check that never ran.  Every advisory this module emits, including
the passing ones, must therefore say so in its own summary.  A future edit that
quietly upgrades the wording to imply verification is the mutation
:func:`_assert_every_summary_disclaims_verification` exists to fail.

Offline: constructed seed locks and transactions only.  No network, no target,
no archive.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_seed_integrity_census import (  # noqa: E402
    collect_seed_integrity_advisories,
)
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CHECKS = 0
_MANAGER_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    name = "integrity"


def _seed(archive_sha256: str | None) -> SeedLock:
    return SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "c" * 40,
        "d" * 40,
        archive_sha256,
        "macos-bizops",
    )


def _transaction(archive_sha256: str | None) -> Transaction:
    answers: dict[str, JsonValue] = {"decisions": {}, "consents": {}}
    return Transaction.create(
        name="integrity",
        target=Path("/tmp/integrity"),
        input_fingerprint=canonical_sha256({"name": "integrity"}),
        answers=answers,
        seed=_seed(archive_sha256),
        flow_id="flow",
        flow_source_revision="rev",
        flow_contract_digest="digest",
        stage_ids=("install",),
        completion_probe_ids=(),
    )


def _write_lock(path: Path, archive_sha256: str | None) -> None:
    """Write the manager's on-disk seed lock in its v1 CLOSED schema.

    Note this is a different shape from the transaction-recorded identity dict:
    the on-disk lock uses bare keys (``repository``, ``commit``, ``tree_hash``)
    and requires ``schema_version``, while the journal uses ``seed_``-prefixed
    keys. The schema is closed, so an unknown key is rejected outright --
    ``archive_sha256`` is the one optional key and is omitted entirely when
    unset rather than written as null.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    lock: dict[str, JsonValue] = {
        "schema_version": 1,
        "repository": "https://github.com/solet-public/macos-bizops.git",
        "release_tag": "release-2026-08-20",
        "commit": "c" * 40,
        "tree_hash": "d" * 40,
        "profile": "macos-bizops",
    }
    if archive_sha256 is not None:
        lock["archive_sha256"] = archive_sha256
    path.write_text(json.dumps(lock))


def _advisory(*, lock_digest: str | None, journal_digest: str | None) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "share" / "solet" / "seed.lock.json"
        _write_lock(lock, lock_digest)
        results = collect_seed_integrity_advisories(
            _Record(), _transaction(journal_digest), manager_seed_lock_path=lock
        )
    _check(len(results) == 1, f"expected exactly one advisory, got {len(results)}")
    advisory = results[0]
    assert isinstance(advisory, dict)
    return advisory


def _assert_matching_digests_are_green() -> None:
    advisory = _advisory(lock_digest=_MANAGER_DIGEST, journal_digest=_MANAGER_DIGEST)
    _check(
        advisory["status"] == "verified",
        f"matching recorded digests were not verified: {advisory['status']}",
    )


def _assert_mismatch_is_named() -> None:
    advisory = _advisory(lock_digest=_MANAGER_DIGEST, journal_digest=_OTHER_DIGEST)
    _check(
        advisory["reason_code"] == "seed_archive_digest_mismatch",
        f"a digest mismatch was not NAMED: {advisory['reason_code']}",
    )
    _check(
        advisory["expected"]["manager_seed_archive_sha256"] == _MANAGER_DIGEST
        and advisory["observed"]["journaled_seed_archive_sha256"] == _OTHER_DIGEST,
        "the advisory did not carry both digests that disagree",
    )


def _assert_asymmetry_is_a_different_name() -> None:
    """Recorded on one side only is a distinct cause from two different values."""

    absent_in_lock = _advisory(lock_digest=None, journal_digest=_MANAGER_DIGEST)
    _check(
        absent_in_lock["reason_code"] == "seed_archive_digest_asymmetric",
        f"a one-sided digest was not distinguished: {absent_in_lock['reason_code']}",
    )
    absent_in_journal = _advisory(lock_digest=_MANAGER_DIGEST, journal_digest=None)
    _check(
        absent_in_journal["reason_code"] == "seed_archive_digest_asymmetric",
        "the mirrored asymmetry was not detected",
    )
    _check(
        "target journal" in str(absent_in_lock["summary"])
        and "manager seed lock" in str(absent_in_journal["summary"]),
        "the advisory did not name WHICH side carries the digest",
    )


def _assert_both_absent_is_green_not_a_warning() -> None:
    advisory = _advisory(lock_digest=None, journal_digest=None)
    _check(
        advisory["status"] == "verified",
        "a digest unset on both sides was reported as a divergence",
    )


def _assert_unreadable_lock_is_unknown_not_green() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "share" / "solet" / "seed.lock.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("{ not json")
        results = collect_seed_integrity_advisories(
            _Record(), _transaction(_MANAGER_DIGEST), manager_seed_lock_path=lock
        )
    advisory = results[0]
    assert isinstance(advisory, dict)
    _check(
        advisory["status"] == "unknown",
        f"an unreadable seed lock did not read as unknown: {advisory['status']}",
    )


def _assert_every_summary_disclaims_verification() -> None:
    """No advisory here may imply an archive was verified, because none was.

    Acquisition is git-fetch based and materializes no archive, so every
    emitted summary -- passing ones included -- must carry the disclaimer.
    """

    for lock_digest, journal_digest in (
        (_MANAGER_DIGEST, _MANAGER_DIGEST),
        (_MANAGER_DIGEST, _OTHER_DIGEST),
        (None, _MANAGER_DIGEST),
        (None, None),
    ):
        advisory = _advisory(lock_digest=lock_digest, journal_digest=journal_digest)
        summary = str(advisory["summary"])
        _check(
            "does not verify an archive" in summary,
            f"a summary implied verification that never happened: {summary}",
        )


def main() -> int:
    _assert_matching_digests_are_green()
    _assert_mismatch_is_named()
    _assert_asymmetry_is_a_different_name()
    _assert_both_absent_is_green_not_a_warning()
    _assert_unreadable_lock_is_unknown_not_green()
    _assert_every_summary_disclaims_verification()
    print(f"doctor_seed_integrity_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
