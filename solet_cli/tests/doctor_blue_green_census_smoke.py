"""The release ledger and the symlinks it describes are actually compared.

Detection coverage for ``iss_68f363ae`` (D-1.4) and the two durable invariants
of ``iss_9789ed45`` (D-1.Q6).

``_compensate_failed_swap`` clears the ``in_progress`` marker on its
best-effort path even when the symlink restore raised ``OSError``.
``ReleaseSwapper.reconcile`` keys ONLY on that marker -- ``if
ledger.in_progress is None: return RECONCILE_NOOP`` -- so it returns NOOP
without ever comparing the ledger to the links.  The ledger then says the
active release is one thing, ``current`` points at another, and NOTHING in the
system compares them.  That silence is the defect; this smoke pins the
comparator that ends it.

The discriminator is deliberately not "the check went warn".  A check that
warned on every target would also pass a warn-only assertion while being
useless, so each red asserts the NAMED ``reason_code``, and the two divergence
shapes are required to produce DIFFERENT names:

* stranded (no marker, reconcile cannot act)   -> release_ledger_symlink_stranded
* in-transit (marker present, reconcile can)   -> release_swap_in_progress

Collapsing those two into one name is the mutation this smoke is built to
fail, because it is the one that would let the unrepairable case hide behind
the benign one.

Offline: constructed release roots under a temporary directory only.  No
target, no router, no deployment is contacted.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_blue_green_census import (  # noqa: E402
    collect_blue_green_advisories,
)

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _write_root(
    root: Path,
    *,
    ledger_current: str | None,
    ledger_previous: str | None,
    link_current: str | None,
    link_previous: str | None,
    in_progress: dict[str, object] | None = None,
) -> None:
    """Materialize a release root whose ledger and links are set independently."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps(
            {
                "current": ledger_current,
                "previous": ledger_previous,
                "in_progress": in_progress,
            },
            indent=2,
            sort_keys=True,
        )
    )
    for name, target in (("current", link_current), ("previous", link_previous)):
        link = root / name
        if link.is_symlink():
            link.unlink()
        if target is not None:
            (root / target).mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)


def _advisories(root: Path) -> dict[str, dict[str, object]]:
    """Index every emitted advisory by its check_id.

    Selecting by id rather than by list position means adding a check can never
    silently re-point an existing assertion at a different check.
    """

    results = collect_blue_green_advisories(_record(), releases_root=root)
    by_id: dict[str, dict[str, object]] = {}
    for entry in results:
        assert isinstance(entry, dict)
        check_id = str(entry["check_id"])
        _check(check_id not in by_id, f"duplicate advisory id emitted: {check_id}")
        by_id[check_id] = entry
    return by_id


def _advisory(root: Path) -> dict[str, object]:
    """The ledger/symlink agreement advisory specifically."""

    return _advisories(root)["doctor::release_ledger_symlink_agreement_v1"]


def _provenance(root: Path) -> dict[str, object]:
    """The schema-snapshot provenance advisory specifically (D-7.6)."""

    return _advisories(root)["doctor::release_schema_snapshot_provenance_v1"]


def _write_version(root: Path, release: str, payload: dict[str, object]) -> None:
    (root / release).mkdir(parents=True, exist_ok=True)
    (root / release / "VERSION").write_text(json.dumps(payload))


class _Record:
    """The only field the blue-green census reads off a registry record."""

    name = "census"


def _record() -> object:
    return _Record()


def _assert_agreement_is_green() -> None:
    """A target whose ledger and links agree must not be warned about."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous="rel-001",
            link_current="rel-002",
            link_previous="rel-001",
        )
        advisory = _advisory(root)
        _check(
            advisory["status"] == "verified",
            f"an agreeing ledger/symlink pair was not verified: {advisory['status']}",
        )
        _check(
            advisory["blocking"] is False,
            "a blue-green advisory declared itself blocking",
        )


def _assert_stranded_divergence_is_named() -> None:
    """The D-1.4 residue: marker cleared, links never restored, reconcile blind."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous="rel-001",
            link_current="rel-001",
            link_previous="rel-001",
            in_progress=None,
        )
        advisory = _advisory(root)
        _check(
            advisory["status"] == "warn",
            f"a stranded ledger/symlink divergence was not warned: {advisory['status']}",
        )
        _check(
            advisory["reason_code"] == "release_ledger_symlink_stranded",
            f"the stranded divergence was not NAMED: {advisory['reason_code']}",
        )
        _check(
            advisory["expected"]["ledger_current"] == "rel-002"
            and advisory["observed"]["symlink_current"] == "rel-001",
            "the advisory did not carry the specific releases that disagree",
        )


def _assert_in_transit_is_a_different_name() -> None:
    """A live swap is repairable by reconcile and must not read as stranded."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-001",
            ledger_previous=None,
            link_current="rel-002",
            link_previous=None,
            in_progress={"old_rel": "rel-001", "new_rel": "rel-002", "phase": "cutover"},
        )
        advisory = _advisory(root)
        _check(
            advisory["reason_code"] == "release_swap_in_progress",
            "an in-flight swap was not distinguished from a stranded one: "
            f"{advisory['reason_code']}",
        )
        _check(
            advisory["observed"]["in_progress_phase"] == "cutover",
            "the advisory dropped the in-progress phase it keys on",
        )


def _assert_previous_link_alone_is_detected() -> None:
    """rollback-restores-the-manifest: a wrong ``previous`` is its own divergence."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous="rel-001",
            link_current="rel-002",
            link_previous="rel-000",
        )
        advisory = _advisory(root)
        _check(
            advisory["reason_code"] == "release_ledger_symlink_stranded",
            "a divergent previous link alone was not detected",
        )
        _check(
            "previous" in str(advisory["summary"]),
            f"the summary did not name which link diverged: {advisory['summary']}",
        )


def _assert_dangling_link_still_compares_by_name() -> None:
    """A link naming a deleted release must compare as that name, not vanish."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous=None,
            link_current="rel-002",
            link_previous=None,
        )
        # Delete the release directory out from under the link.
        for child in (root / "rel-002").iterdir():
            child.unlink()
        (root / "rel-002").rmdir()
        advisory = _advisory(root)
        _check(
            advisory["status"] == "verified",
            "a dangling but correctly-named current link was reported as divergent",
        )


def _assert_absent_ledger_is_not_a_divergence() -> None:
    """A target that never deployed has no swap state to disagree."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        root.mkdir(parents=True)
        advisory = _advisory(root)
        _check(
            advisory["status"] == "verified",
            "a target with no release ledger was warned about",
        )


def _assert_unreadable_ledger_is_unknown_not_green() -> None:
    """An unreadable source is not a passing source."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        root.mkdir(parents=True)
        (root / "state.json").write_text("[]")
        advisory = _advisory(root)
        _check(
            advisory["status"] == "unknown",
            f"a structurally-wrong ledger did not read as unknown: {advisory['status']}",
        )
        _check(
            advisory["reason_code"] == "release_ledger_unreadable",
            f"the unreadable ledger was not NAMED: {advisory['reason_code']}",
        )


def _assert_provenance_matches_is_green() -> None:
    """A VERSION whose release_id matches the link it sits behind is attributable."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous=None,
            link_current="rel-002",
            link_previous=None,
        )
        _write_version(root, "rel-002", {"release_id": "rel-002", "schema_snapshot": {}})
        advisory = _provenance(root)
        _check(
            advisory["status"] == "verified",
            f"a matching release_id was not verified: {advisory['status']}",
        )


def _assert_provenance_mismatch_is_named() -> None:
    """The D-7.6 residue: the snapshot read through the link is another build's.

    This is the shape that couples to D-1.4 -- a link pointing at a release
    whose VERSION claims a different id means the DDL-free preflight diffs
    against a schema that does not belong to the release it thinks is active.
    """

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous=None,
            link_current="rel-002",
            link_previous=None,
        )
        _write_version(root, "rel-002", {"release_id": "rel-001", "schema_snapshot": {}})
        advisory = _provenance(root)
        _check(
            advisory["reason_code"] == "release_provenance_mismatch",
            f"a wrong-release snapshot was not NAMED: {advisory['reason_code']}",
        )
        _check(
            advisory["observed"]["version_release_id"] == "rel-001"
            and advisory["expected"]["symlink_current"] == "rel-002",
            "the advisory did not carry both the link name and the recorded id",
        )


def _assert_absent_release_id_is_its_own_name() -> None:
    """No release_id at all is a distinct cause from a mismatching one."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous=None,
            link_current="rel-002",
            link_previous=None,
        )
        _write_version(root, "rel-002", {"schema_snapshot": {}})
        advisory = _provenance(root)
        _check(
            advisory["reason_code"] == "release_provenance_absent",
            f"an unattributable snapshot was not distinguished: {advisory['reason_code']}",
        )


def _assert_missing_version_is_named() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        _write_root(
            root,
            ledger_current="rel-002",
            ledger_previous=None,
            link_current="rel-002",
            link_previous=None,
        )
        advisory = _provenance(root)
        _check(
            advisory["reason_code"] == "release_version_absent",
            f"a release without VERSION was not named: {advisory['reason_code']}",
        )


def _assert_both_checks_are_always_emitted() -> None:
    """Every call emits both ids, so neither can quietly stop running."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "releases"
        root.mkdir(parents=True)
        ids = set(_advisories(root))
        _check(
            ids
            == {
                "doctor::release_ledger_symlink_agreement_v1",
                "doctor::release_schema_snapshot_provenance_v1",
            },
            f"the census stopped emitting one of its checks: {sorted(ids)}",
        )


def main() -> int:
    _assert_agreement_is_green()
    _assert_stranded_divergence_is_named()
    _assert_in_transit_is_a_different_name()
    _assert_previous_link_alone_is_detected()
    _assert_dangling_link_still_compares_by_name()
    _assert_absent_ledger_is_not_a_divergence()
    _assert_unreadable_ledger_is_unknown_not_green()
    _assert_provenance_matches_is_green()
    _assert_provenance_mismatch_is_named()
    _assert_absent_release_id_is_its_own_name()
    _assert_missing_version_is_named()
    _assert_both_checks_are_always_emitted()
    print(f"doctor_blue_green_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
