"""``doctor::release_identity_v1`` reads the declaration back (iss_670801d4).

Pins the advisory that finally consumes ``manager_source_commit`` and the
per-file / per-plugin digests a release ships: ``verified`` only when the
installed manager bytes, the receipt's commit, the target checkout's git
identity and the plugin subtrees all equal the manifest AND the pairing does
not refuse; ``warn`` (``release_identity_drift``) naming the section that
differs when any of them does; ``unknown`` -- never ``verified`` -- when no
manifest is reachable, with the measurements still in ``observed``; and the
archive-digest consistency census folded in as ``observed.archive_digest_
consistency`` rather than re-implemented.  Every summary, passing ones
included, carries the NOT CRYPTOGRAPHIC header; the mutation that quietly
drops it is what :func:`_assert_every_summary_disclaims_cryptography` fails.
It never blocks (``blocking`` is False on every outcome).

Offline: a throwaway git checkout, a package directory, JSON built from
their digests; no keg, no network, no running process.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(Path(__file__).resolve().parent)]

import tempfile  # noqa: E402
from typing import Any  # noqa: E402

from _release_identity_fixture import OTHER_COMMIT, SOURCE_COMMIT, build_package, build_seed_checkout, git, manifest, seed_lock_v3, write_json, write_receipt  # noqa: E402
from solet_manager.doctor_release_identity_census import collect_release_identity_advisories  # noqa: E402
from solet_manager.models import InstanceRecord, JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CHECKS = 0
_CHECK_ID = "doctor::release_identity_v1"


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.target = root / "target"
        self.seed = build_seed_checkout(self.target)
        self.package = root / "pkg"
        self.digests = build_package(self.package)
        share = root / "keg" / "share" / "solet"
        self.seed_lock = write_json(share / "seed.lock.json", seed_lock_v3(self.seed))
        self.receipt = write_receipt(share / "install-source.json")
        self.manifest_path = write_json(share / "release_manifest.json", manifest(seed=self.seed, file_digests=self.digests))
        self.record = InstanceRecord(
            name="fixture", target=str(self.target), launcher=str(self.target / "client" / "bin" / "fixture"),
            seed_repository="https://github.com/solet-public/macos-bizops.git", seed_tag="release-2026-09-19-fixture",
            seed_commit=self.seed["commit"], seed_tree_hash=self.seed["tree_hash"], profile="macos-bizops",
            flow_id="fixture-flow", flow_source_revision="a" * 40, flow_contract_digest="sha256:" + "c" * 64,
            created_at="2026-09-19T00:00:00+00:00", updated_at="2026-09-19T00:00:00+00:00",
        )
        seed = SeedLock("https://github.com/solet-public/macos-bizops.git", "release-2026-09-19-fixture", self.seed["commit"], self.seed["tree_hash"], "e" * 64, "macos-bizops")
        answers: dict[str, JsonValue] = {"decisions": {}, "consents": {}}
        self.transaction = Transaction.create(name="fixture", target=self.target, input_fingerprint=canonical_sha256({"name": "fixture"}), answers=answers, seed=seed, flow_id="fixture-flow", flow_source_revision="a" * 40, flow_contract_digest="sha256:" + "c" * 64, stage_ids=("install",), completion_probe_ids=())

    def advisory(self, *, manifest_path: Path | None = None, receipt: Path | None = None) -> dict[str, Any]:
        results = collect_release_identity_advisories(
            self.record, self.transaction,
            manager_seed_lock_path=self.seed_lock,
            manifest_path=self.manifest_path if manifest_path is None else manifest_path,
            install_source_path=self.receipt if receipt is None else receipt,
            package_root=self.package,
        )
        _check(len(results) == 1, f"exactly one advisory is emitted, got {len(results)}")
        advisory = results[0]
        assert isinstance(advisory, dict)
        _check(advisory["check_id"] == _CHECK_ID and advisory["blocking"] is False, "the advisory is named and never blocks")
        return advisory


def _assert_matching_installation_is_verified(fixture: _Fixture) -> None:
    advisory = fixture.advisory()
    _check(advisory["status"] == "verified", f"a matching installation verifies: {advisory['status']} {advisory['reason_code']} {advisory['summary']}")
    _check(advisory["expected"]["manager_source_commit"] == SOURCE_COMMIT and advisory["expected"]["seed_commit"] == fixture.seed["commit"], "the declaration is carried as expected")
    observed = advisory["observed"]
    _check(observed["manager"]["files_hashed"] == len(fixture.digests) and observed["seed_checkout"]["head_commit"] == fixture.seed["commit"], "the measured manager digests and git identity are carried as observed")
    _check(observed["pairing"]["verdict"] == "paired", "the pairing verdict is folded in")
    _check(observed["archive_digest_consistency"]["status"] == "verified", "the existing archive-digest census is CALLED and its outcome folded in, not re-implemented")


def _assert_drift_warns_and_names_the_section(fixture: _Fixture) -> None:
    (fixture.package / "models.py").write_text("MANAGER_VERSION = 'edited'\n", encoding="utf-8")
    manager_drift = fixture.advisory()
    _check(manager_drift["status"] == "warn" and manager_drift["reason_code"] == "release_identity_drift", f"an edited manager file warns: {manager_drift['status']}")
    _check(manager_drift["observed"]["manager"]["drifted_paths"] == ["solet_cli/src/solet_manager/models.py"] and "manager: drifted" in manager_drift["summary"], "the drifted manager path is named and the summary says which section")
    (fixture.package / "models.py").write_text("MANAGER_VERSION = '0.1.0'\n", encoding="utf-8")
    (fixture.target / "plugins" / "beta" / "plugin.py").write_text("NAME = 'edited'\n", encoding="utf-8")
    git(fixture.target, "add", "-A")
    git(fixture.target, "commit", "-q", "-m", "advance beta")
    checkout_drift = fixture.advisory()
    _check(checkout_drift["status"] == "warn" and checkout_drift["observed"]["seed_checkout"]["drifted_components"] == ["plugin:beta"], f"an advanced plugin subtree warns with the component named: {checkout_drift['observed']['seed_checkout']}")
    git(fixture.target, "reset", "-q", "--hard", "HEAD~1")
    skew = fixture.advisory(receipt=write_receipt(fixture.root / "skew" / "install-source.json", OTHER_COMMIT))
    # The manifest still names the ORIGINAL manager commit, so beside a receipt naming another one the
    # records disagree about what the keg is: that is `inconsistent` (a refusing verdict), not mere skew.
    _check(skew["status"] == "warn" and skew["observed"]["pairing"]["verdict"] == "inconsistent" and "pairing: inconsistent" in skew["summary"], f"a receipt the manifest does not describe warns through the doctor with the pairing verdict named: {skew['observed']['pairing']}")


def _assert_no_manifest_is_unknown_not_green(fixture: _Fixture) -> None:
    advisory = fixture.advisory(manifest_path=fixture.root / "absent" / "release_manifest.json")
    _check(advisory["status"] == "unknown" and advisory["reason_code"] == "release_manifest_absent", f"no manifest is unknown, never verified: {advisory['status']}")
    _check(advisory["observed"]["manager"]["files_hashed"] == len(fixture.digests) and advisory["observed"]["seed_checkout"]["head_commit"] == fixture.seed["commit"], "the measurements are still recorded so the unknown is not empty")
    corrupt = fixture.root / "corrupt" / "release_manifest.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{ not json", encoding="utf-8")
    _check(fixture.advisory(manifest_path=corrupt)["reason_code"] == "release_manifest_unreadable", "a corrupt manifest is a distinct unknown from an absent one")


def _assert_every_summary_disclaims_cryptography(fixture: _Fixture) -> None:
    for advisory in (fixture.advisory(), fixture.advisory(manifest_path=fixture.root / "absent" / "release_manifest.json"), fixture.advisory(receipt=fixture.root / "skew" / "install-source.json")):
        _check("NOT CRYPTOGRAPHIC" in str(advisory["summary"]), f"a summary implied a signature check that never ran: {advisory['summary']}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = _Fixture(Path(tmp))
        _assert_matching_installation_is_verified(fixture)
        _assert_drift_warns_and_names_the_section(fixture)
        _assert_no_manifest_is_unknown_not_green(fixture)
        _assert_every_summary_disclaims_cryptography(fixture)
    print(f"doctor_release_identity_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
