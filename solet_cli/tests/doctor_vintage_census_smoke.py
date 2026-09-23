"""Report-only manager-versus-target doctor census regression coverage."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor import _doctor_result  # pyright: ignore[reportPrivateUsage]  # noqa: E402
from solet_manager.doctor_vintage_census import (  # noqa: E402
    _launchctl_observation,
    collect_doctor_advisories,
)
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


def _write_seed_lock(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "https://github.com/solet-public/macos-bizops.git",
                "release_tag": "release-2026-09-04",
                "commit": "d" * 40,
                "tree_hash": "e" * 40,
                "profile": "macos-bizops",
            }
        ),
        encoding="utf-8",
    )


def _write_contracts(directory: Path) -> None:
    from solet_manager.contracts import _CONTRACT_FILENAMES  # pyright: ignore[reportPrivateUsage]

    directory.mkdir()
    for name in _CONTRACT_FILENAMES:
        (directory / name).write_text("{}", encoding="utf-8")


def _launchctl(command: tuple[str, ...]) -> tuple[int, str, str]:
    label = command[-1]
    if label.endswith(".router"):
        return 0, "state = running\nlast exit code = 0\nruns = 1\n", ""
    return 0, "state = running\nlast exit code = 0\nruns = 2\n", ""


def _launchctl_output(output: str) -> tuple[int, str, str]:
    return 0, output, ""


def _by_id(advisories: list[JsonValue]) -> dict[str, dict[str, JsonValue]]:
    values: dict[str, dict[str, JsonValue]] = {}
    for item in advisories:
        if not isinstance(item, dict):
            raise AssertionError("red: advisory must be an object")
        check_id = item.get("check_id")
        if not isinstance(check_id, str):
            raise AssertionError("red: advisory id missing")
        values[check_id] = item
    return values


def _assert_report_only_census_names_all_skew() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        lock = root / "seed.lock.json"
        contracts = root / "contracts"
        _write_seed_lock(lock)
        _write_contracts(contracts)
        advisories = collect_doctor_advisories(
            _record(),
            _transaction(),
            manager_seed_lock_path=lock,
            release_contract_directory=contracts,
            launchctl_runner=_launchctl,
        )
    values = _by_id(advisories)
    _check(len(values) == 4, "doctor census omitted a ranked advisory")
    _check(
        values["doctor::manager_seed_vintage_v1"]["status"] == "warn",
        "seed-vintage skew remained undetected",
    )
    _check(
        values["doctor::persisted_contract_digest_v1"]["status"] == "warn",
        "persisted-contract skew remained undetected",
    )
    _check(
        values["doctor::background_item_cardinality_v1"]["status"] == "verified",
        "declared background items were not counted",
    )
    _check(
        values["doctor::launchagent_boot_history_v1"]["status"] == "warn",
        "retried LaunchAgent boot remained undetected",
    )
    _check(
        all(item.get("blocking") is False for item in values.values()),
        "a census advisory may never refuse doctor",
    )
    result = _doctor_result(
        "fixture",
        _transaction(),
        [{"id": "fixture-check", "status": "verified"}],
        advisories=advisories,
    )
    _check(result.exit_code == 0, "advisory census changed doctor acceptance exit")
    _check(
        result.data["advisories"] == advisories,
        "doctor result did not surface the report-only census",
    )


def _assert_launchctl_first_launch_sentinel() -> None:
    first_launch = _launchctl_observation(
        "local.solet.fixture",
        lambda _command: _launchctl_output(
            "state = running\nlast exit code = (never exited)\nruns = 1\n"
        ),
    )
    _check(first_launch["query_status"] == "readable", "first launch was unreadable")
    _check(
        first_launch["last_exit_code"] == "(never exited)",
        "first-launch sentinel was not preserved",
    )

    malformed = _launchctl_observation(
        "local.solet.fixture",
        lambda _command: _launchctl_output(
            "state = running\nlast exit code = unavailable\nruns = 1\n"
        ),
    )
    _check(malformed["query_status"] == "unknown", "malformed exit status became readable")

    stopped = _launchctl_observation(
        "local.solet.fixture",
        lambda _command: _launchctl_output(
            "state = stopped\nlast exit code = (never exited)\nruns = 1\n"
        ),
    )
    _check(stopped["query_status"] == "unknown", "stopped sentinel became readable")

    retried = _launchctl_observation(
        "local.solet.fixture",
        lambda _command: _launchctl_output(
            "state = running\nlast exit code = 1\nruns = 2\n"
        ),
    )
    _check(retried["query_status"] == "readable", "numeric crash-loop status became unreadable")
    _check(retried["runs"] == 2, "crash-loop run count changed")


def main() -> int:
    _assert_report_only_census_names_all_skew()
    _assert_launchctl_first_launch_sentinel()
    print(f"doctor_vintage_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
