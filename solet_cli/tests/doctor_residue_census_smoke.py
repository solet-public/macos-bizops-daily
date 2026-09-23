"""Host-residue doctor census coverage.

Every check here is paired: a fixture that reproduces the residue must make the
advisory name that specific cause, and a clean fixture must go green.  A test
that only asserts the green half cannot tell a working detector from a
detector that never fires, which is the failure mode the rows this module
detects are themselves made of.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor import _doctor_result  # pyright: ignore[reportPrivateUsage]  # noqa: E402
from solet_manager.doctor_residue_census import (  # noqa: E402
    STAGING_SUFFIX,
    collect_residue_advisories,
)

# The literal, restated deliberately. Building the fixtures from the module's
# own STAGING_SUFFIX made detector and fixture move together, so mutating the
# constant left the suite green -- the suffix IS the whole detection predicate
# for the abandoned-staging check, and it had no guard. This is the same
# independence the ingress fixtures already have, where the smoke hardcodes
# "fixture.mcp_ingress.port" against the module's _INGRESS_PORT_SUFFIX.
# Canonical source: release_manager.py STAGING_SUFFIX.
_CANONICAL_STAGING_SUFFIX = ".incoming"
from solet_manager.models import CheckpointStatus, InstanceRecord, JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _record() -> InstanceRecord:
    return InstanceRecord(
        name="fixture",
        target="/fixture/target",
        launcher="/fixture/target/client/bin/fixture",
        seed_repository="https://github.com/solet-public/macos-bizops.git",
        seed_tag="release-2026-08-20",
        seed_commit="a" * 40,
        seed_tree_hash="b" * 40,
        profile="macos-bizops",
        flow_id="fixture-flow",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "c" * 64,
        created_at="2026-09-01T00:00:00+00:00",
        updated_at="2026-09-01T00:00:00+00:00",
        expected_router_name="fixture",
        expected_router_socket="/fixture/fixture.router.sock",
        expected_router_port_range="8800-8999",
    )


def _transaction() -> Transaction:
    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        None,
        "macos-bizops",
    )
    return Transaction.create(
        name="fixture",
        target=Path("/fixture/target"),
        input_fingerprint=canonical_sha256({"name": "fixture"}),
        answers={"setup_profile": "macos-bizops"},
        seed=seed,
        flow_id="fixture-flow",
        flow_source_revision="a" * 40,
        flow_contract_digest="sha256:" + "c" * 64,
        stage_ids=("fixture-stage",),
        completion_probe_ids=("fixture-check",),
    ).with_statuses(
        stages={"fixture-stage": CheckpointStatus.VERIFIED},
        completion={"fixture-check": CheckpointStatus.VERIFIED},
    )


def _by_id(advisories: list[JsonValue]) -> dict[str, dict[str, JsonValue]]:
    found: dict[str, dict[str, JsonValue]] = {}
    for item in advisories:
        assert isinstance(item, dict)
        found[str(item["check_id"])] = item
    return found


def _collect(
    runtime: Path,
    releases: Path,
    *,
    accepts: bool = False,
    router_present: bool | None = False,
) -> dict[str, dict[str, JsonValue]]:
    return _by_id(
        collect_residue_advisories(
            _record(),
            runtime_directory=runtime,
            releases_root=releases,
            connect_probe=lambda _port: accepts,
            router_presence=lambda _name: router_present,
        )
    )


def _assert_stale_ingress_port_file_is_named() -> None:
    """iss_e12ef805 -- the ingress port file outlives the ingress that wrote it."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        runtime = root / "runtime"
        runtime.mkdir()
        releases = root / "releases"
        releases.mkdir()

        clean = _collect(runtime, releases)["doctor::stale_ingress_port_file_v1"]
        _check(clean["status"] == "verified", "absent ingress port file must be green")

        port_file = runtime / "fixture.mcp_ingress.port"
        port_file.write_text("54321", encoding="utf-8")

        served = _collect(runtime, releases, accepts=True)["doctor::stale_ingress_port_file_v1"]
        _check(served["status"] == "verified", "a served ingress port must be green")

        stale = _collect(runtime, releases, accepts=False)["doctor::stale_ingress_port_file_v1"]
        _check(stale["status"] == "warn", "a refusing ingress port must be named, not green")
        _check(
            stale["reason_code"] == "ingress_port_file_stale",
            "the stale ingress port must be named by its specific cause",
        )
        _check(stale["observed"]["port"] == 54321, "the advisory must report the stale port")

        port_file.write_text("not-a-port", encoding="utf-8")
        unreadable = _collect(runtime, releases)["doctor::stale_ingress_port_file_v1"]
        _check(
            unreadable["status"] == "unknown",
            "an unreadable port file is unknown, never a silent pass",
        )


def _assert_staging_suffix_matches_canonical() -> None:
    """The detection predicate itself must be able to fail.

    ``doctor_residue_census`` restates ``.incoming`` rather than importing it
    from ``release_manager`` (the manager does not import the deployment
    plugin). A restated constant needs a guard, or it silently drifts from the
    canonical one and the detector stops detecting while every test stays
    green.
    """

    _check(
        STAGING_SUFFIX == _CANONICAL_STAGING_SUFFIX,
        "doctor_residue_census.STAGING_SUFFIX drifted from the canonical "
        "release_manager.STAGING_SUFFIX value",
    )


def _assert_abandoned_release_staging_is_named() -> None:
    """iss_89275d5c -- staging dirs no garbage collection pass will ever walk."""

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        runtime = root / "runtime"
        runtime.mkdir()
        releases = root / "releases"
        releases.mkdir()
        (releases / "release-2026-09-01").mkdir()

        clean = _collect(runtime, releases)["doctor::abandoned_release_staging_v1"]
        _check(clean["status"] == "verified", "finalized releases alone must be green")

        (releases / f"release-2026-09-02{_CANONICAL_STAGING_SUFFIX}").mkdir()
        dirty = _collect(runtime, releases)["doctor::abandoned_release_staging_v1"]
        _check(dirty["status"] == "warn", "an abandoned staging dir must be named")
        _check(
            dirty["reason_code"] == "abandoned_release_staging",
            "the abandoned staging dir must be named by its specific cause",
        )
        _check(
            dirty["observed"]["abandoned"] == [f"release-2026-09-02{_CANONICAL_STAGING_SUFFIX}"],
            "the advisory must name which staging directory was abandoned",
        )
        _check(
            dirty["observed"]["abandoned_count"] == 1,
            "a finalized release must not be counted as abandoned",
        )


def _assert_orphaned_router_port_files_are_named() -> None:
    """iss_bf9aaea8 -- residue detector for router-owned port files.

    The uninstall defect itself is fixed; this guards the regression and finds
    files stranded before the fix.
    """

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        runtime = root / "runtime"
        runtime.mkdir()
        releases = root / "releases"
        releases.mkdir()
        (runtime / "fixture.router.port").write_text("8801", encoding="utf-8")
        (runtime / "fixture.bridge.port").write_text("8802", encoding="utf-8")

        installed = _collect(runtime, releases, router_present=True)
        live = installed["doctor::orphaned_router_port_files_v1"]
        _check(live["status"] == "verified", "port files are expected while the router is live")

        orphaned = _collect(runtime, releases, router_present=False)
        gone = orphaned["doctor::orphaned_router_port_files_v1"]
        _check(gone["status"] == "warn", "port files outliving the router must be named")
        _check(
            gone["reason_code"] == "orphaned_router_port_files",
            "orphaned router port files must be named by their specific cause",
        )
        _check(gone["observed"]["present_count"] == 2, "both orphaned port files must be listed")

        unknown_presence = _collect(runtime, releases, router_present=None)
        undetermined = unknown_presence["doctor::orphaned_router_port_files_v1"]
        _check(
            undetermined["status"] == "unknown",
            "an unqueryable router must be unknown, never read as absent",
        )


def _assert_residue_census_never_refuses_doctor() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        runtime = root / "runtime"
        runtime.mkdir()
        releases = root / "releases"
        releases.mkdir()
        (runtime / "fixture.mcp_ingress.port").write_text("54321", encoding="utf-8")
        (releases / f"release-2026-09-02{_CANONICAL_STAGING_SUFFIX}").mkdir()
        advisories = collect_residue_advisories(
            _record(),
            runtime_directory=runtime,
            releases_root=releases,
            connect_probe=lambda _port: False,
            router_presence=lambda _name: False,
        )
        warned = [
            item for item in advisories if isinstance(item, dict) and item.get("status") == "warn"
        ]
        _check(len(warned) >= 1, "the fixture must actually trip at least one advisory")
        _check(
            all(isinstance(item, dict) and item.get("blocking") is False for item in advisories),
            "a residue advisory may never refuse doctor",
        )
        result = _doctor_result(
            "fixture",
            _transaction(),
            [{"id": "fixture-check", "status": "verified"}],
            advisories=advisories,
        )
        _check(
            result.exit_code == 0,
            "a tripped residue advisory must not change doctor acceptance",
        )
        _check(
            result.data["advisories"] == advisories,
            "doctor result did not surface the residue census",
        )


def main() -> int:
    _assert_stale_ingress_port_file_is_named()
    _assert_staging_suffix_matches_canonical()
    _assert_abandoned_release_staging_is_named()
    _assert_orphaned_router_port_files_are_named()
    _assert_residue_census_never_refuses_doctor()
    print(f"doctor_residue_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
