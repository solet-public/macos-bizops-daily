#!/usr/bin/env python3
"""Focused U2 immutable archive and no-effect CLI tests."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "solet_cli/src"),
    str(ROOT / "solet_setup_contracts/src"),
    str(Path(__file__).parent),
]

import release_observer_contract_smoke as fixture  # noqa: E402
from solet_manager.cli import run  # noqa: E402
from solet_manager.release_observer import installed_manager_binding  # noqa: E402
from solet_manager.release_observer_store import ObservationArchive  # noqa: E402
from solet_setup_contracts.release_observer_codec import ObserverContractError  # noqa: E402

checks = 0


def check(value: bool, label: str) -> None:
    global checks
    checks += 1
    if not value:
        raise AssertionError(label)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        archive = ObservationArchive(Path(raw))
        observation = fixture._observation()
        check(archive.publish(observation) == observation, "T27 first immutable publish")
        check(archive.publish(observation) == observation, "T27 exact replay")
        changed = replace(observation, source_artifacts=(fixture._artifact("different"),))
        try:
            archive.publish(changed)
        except ObserverContractError as error:
            check(error.code == "request_conflict", "T27 different bytes conflict")
        else:
            check(False, "T27 conflict rejected")
        check(
            archive.read(observation.observation_id) == observation,
            "T31 historical original unchanged",
        )
    capabilities = run(["release-proof", "capabilities"])
    check(capabilities.status == "succeeded", "capabilities package metadata")
    check(capabilities.data["contract"]["owner"] == "manager", "manager binding")
    check("binding" in capabilities.data["contract"]["modes"], "binding receipt is advertised")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        lock = root / "seed.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": "https://github.com/solet-public/macos-bizops.git",
                    "commit": "a" * 40,
                    "tree_hash": "b" * 40,
                    "profile": "macos-bizops",
                }
            ),
            encoding="utf-8",
        )
        capability = root / "capability.json"
        capability.write_text('{"owner":"manager"}', encoding="utf-8")
        binding = installed_manager_binding(
            state_dir=root / "state",
            capability_path=capability,
            seed_lock_path=lock,
        )
        check(binding.release_pin.value == "a" * 40, "installed seed lock supplies release pin")
        check(binding.installed_receipt.producer == "manager", "receipt is Manager-owned")
        receipt = Path(binding.installed_receipt.uri.removeprefix("file://"))
        check(receipt.is_file() and receipt.read_bytes(), "immutable installed receipt exists")
        check(
            installed_manager_binding(
                state_dir=root / "state",
                capability_path=capability,
                seed_lock_path=lock,
            )
            == binding,
            "installed binding receipt exact replay",
        )
        cli_binding = run(
            [
                "--home",
                str(root / "manager-home"),
                "--seed-lock",
                str(lock),
                "release-proof",
                "binding",
            ]
        )
        check(cli_binding.status == "succeeded", "CLI produces installed Manager binding")
        check(
            cli_binding.data["binding"]["release_pin"]["value"] == "a" * 40,
            "CLI binding retains installed release pin",
        )
    print(f"release_observer_smoke: {checks} assertions passed")


if __name__ == "__main__":
    main()
