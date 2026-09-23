"""Passive advisories over the seed identity fields a target persists.

Detection coverage for ``iss_8b1860e0`` (D-9.4): ``archive_sha256`` is parsed
(``seed_lock_parser.py``, ``release_lock.py``), carried into the transaction,
journaled as ``seed_archive_sha256`` (``journal_validation.py``) and migrated
(``journal_migrations.py``) -- and then read by nobody.  A grep for a
comparison site across ``solet_cli/src`` returns none: every occurrence is a
declaration, a parse, or a serialization.  It is the
field-nobody-reads-will-eventually-lie shape, recorded as though it were a
control.

What this module deliberately does NOT do, because doing it would fake the
control rather than supply it: it does not "verify the archive".  There is no
archive to verify.  ``source_acquisition._fetch_and_verify_identity`` acquires
the seed by ``git fetch`` and proves identity by comparing the fetched commit
to ``seed.commit`` and the commit's tree to ``seed.tree_hash``.  No tarball is
ever downloaded, hashed, or retained on the target, so a doctor running against
a target at rest has no artifact whose digest could be recomputed.  Claiming
otherwise would put a green tick next to an integrity check that never ran --
precisely the false-green class this campaign exists to remove.

What is genuinely available is a CONSISTENCY comparison the system never makes.
The digest is persisted in two independent durable places: the manager's own
seed lock, and the target's transaction journal.  The existing vintage census
compares ``seed_commit`` and ``seed_tree_hash`` across those two sources and
stops there -- ``archive_sha256`` is absent from it.  This module adds that
missing comparison, and names the field's real status in its own summary so a
reader is never left believing an integrity check occurred.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .errors import SourceError
from .models import InstanceRecord, JsonValue
from .release_lock import load_seed_lock
from .transaction import Transaction

_SEED_LOCK_NAME = "seed.lock.json"
_ARCHIVE_DIGEST_CHECK_ID = "doctor::seed_archive_digest_consistency_v1"

_NOT_AN_INTEGRITY_CONTROL = (
    "This compares two recorded values; it does not verify an archive. Seed "
    "acquisition is git-fetch based and verifies commit and tree hash, so no "
    "archive is materialized on the target to hash."
)


def collect_seed_integrity_advisories(
    record: InstanceRecord,
    transaction: Transaction,
    *,
    manager_seed_lock_path: Path | None = None,
) -> list[JsonValue]:
    """Return report-only checks over the target's persisted seed identity."""

    seed_lock_path = (
        Path(sys.prefix) / "share" / "solet" / _SEED_LOCK_NAME
        if manager_seed_lock_path is None
        else manager_seed_lock_path
    )
    return [_archive_digest_consistency_advisory(record, transaction, seed_lock_path)]


def _archive_digest_consistency_advisory(
    record: InstanceRecord,
    transaction: Transaction,
    seed_lock_path: Path,
) -> dict[str, JsonValue]:
    """Compare the two places ``archive_sha256`` is persisted.

    Nothing else in the manager compares them, which is why a divergence here
    has never surfaced: the field is written on both sides and read on neither.
    """

    journaled = transaction.seed.archive_sha256
    expected: dict[str, JsonValue] = {"manager_seed_archive_sha256": None}
    observed: dict[str, JsonValue] = {
        "journaled_seed_archive_sha256": journaled,
        "instance_name": record.name,
    }
    source = str(seed_lock_path)

    try:
        manager_seed = load_seed_lock(seed_lock_path)
    except SourceError as exc:
        return advisory_unknown(
            _ARCHIVE_DIGEST_CHECK_ID,
            "The manager seed lock could not be read, so the recorded archive "
            "digest could not be compared.",
            expected,
            observed,
            source,
            "manager_seed_lock_unreadable",
            str(exc),
        )

    manager_digest = manager_seed.archive_sha256
    expected = {"manager_seed_archive_sha256": manager_digest}

    if manager_digest == journaled:
        if manager_digest is None:
            summary = (
                "Neither the manager seed lock nor the target journal records an "
                f"archive digest. {_NOT_AN_INTEGRITY_CONTROL}"
            )
        else:
            summary = (
                "The manager seed lock and the target journal record the same "
                f"archive digest. {_NOT_AN_INTEGRITY_CONTROL}"
            )
        return advisory_verified(_ARCHIVE_DIGEST_CHECK_ID, summary, expected, observed, source)

    if manager_digest is None or journaled is None:
        recorded_side = "target journal" if manager_digest is None else "manager seed lock"
        absent_side = "manager seed lock" if manager_digest is None else "target journal"
        return advisory_warn(
            _ARCHIVE_DIGEST_CHECK_ID,
            f"The {recorded_side} records an archive digest and the {absent_side} "
            f"does not. {_NOT_AN_INTEGRITY_CONTROL}",
            expected,
            observed,
            source,
            "seed_archive_digest_asymmetric",
            "One side was written by a producer that records the digest and the "
            "other by one that does not. Re-cut the seed lock, or accept the "
            "asymmetry knowingly; nothing in the manager reads this field today.",
        )

    return advisory_warn(
        _ARCHIVE_DIGEST_CHECK_ID,
        "The manager seed lock and the target journal record DIFFERENT archive "
        f"digests. {_NOT_AN_INTEGRITY_CONTROL}",
        expected,
        observed,
        source,
        "seed_archive_digest_mismatch",
        "The target was installed from a different seed archive than the manager "
        "now holds. Compare seed commit and tree hash before trusting either; "
        "this field alone proves nothing because no consumer verifies it.",
    )
