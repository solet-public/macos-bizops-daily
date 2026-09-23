"""Pure contract controls for the existing-Solet Step-1 foundation."""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.maintenance_inventory import (  # noqa: E402
    parse_maintenance_inventory_bytes,
    read_maintenance_inventory,
)
from solet_manager.manager_cli import build_parser  # noqa: E402
from solet_manager.models import SCHEMA_VERSION  # noqa: E402
from solet_manager.seed_lock_parser import parse_seed_lock, parse_seed_lock_bytes  # noqa: E402
from solet_setup_contracts.provenance_v1 import (  # noqa: E402
    ProvenanceV1Error,
    parse_provenance_v1,
    verify_seal_trailers,
)


def _stamp() -> tuple[bytes, dict[str, object]]:
    origin = "123e4567-e89b-12d3-a456-426614174001"
    commit, manifest = "a" * 40, "b" * 64
    seed = str(uuid.uuid5(uuid.UUID(origin), f"{commit}:{manifest}::"))
    value: dict[str, object] = {
        "schema_version": 1,
        "seed_id": seed,
        "origin_id": origin,
        "source_commit": commit,
        "manifest_sha256": manifest,
        "bundle": {"name": "macos-bizops", "platform": "local"},
        "source_date": "2026-09-15T00:00:00+00:00",
        "lineage": [],
        "ancestry": [],
        "signature": None,
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(), value


def main() -> int:
    raw, value = _stamp()
    stamp = parse_provenance_v1(raw)
    verify_seal_trailers(
        stamp,
        {
            "Subject": "Seed bundle (factory-sealed)",
            "Seed-Id": stamp.seed_id,
            "Origin-Id": stamp.origin_id,
            "Manifest-SHA256": stamp.manifest_sha256,
            "Assembled-Ref": stamp.source_commit,
            "License-Policy": "public_apache",
            "Minted-At": "2026-09-15T00:00:00+00:00",
        },
    )
    try:
        parse_provenance_v1(raw.replace(b'"signature": null', b'"signature": "no"'))
    except ProvenanceV1Error:
        pass
    else:
        raise AssertionError("non-null signature was accepted")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        lock = root / "seed.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "channel_id": "stable",
                    "repository": "https://github.com/solet-public/macos-bizops.git",
                    "release_tag": "release-2026-09-15",
                    "commit": "a" * 40,
                    "tree_hash": "b" * 40,
                    "archive_sha256": "c" * 64,
                    "profile": "macos-bizops",
                    "provenance": {
                        "schema_version": 1,
                        "provenance_sha256": "d" * 64,
                        "seed_id": value["seed_id"],
                        "origin_id": value["origin_id"],
                        "manifest_sha256": "b" * 64,
                        "bundle_name": "macos-bizops",
                        "platform": "local",
                        "source_commit": "a" * 40,
                        "source_date": "2026-09-15T00:00:00+00:00",
                    },
                    "existing_install_contract": {
                        "flow_id": "existing-install",
                        "flow_schema_version": 1,
                        "bundle_digest": "sha256:" + "e" * 64,
                    },
                    "allowed_repository_migrations": [],
                }
            ),
            encoding="utf-8",
        )
        assert parse_seed_lock(lock).channel_id == "stable"
        assert parse_seed_lock_bytes(lock.read_bytes()).channel_id == "stable"
        registry = root / "instances.json"
        registry.write_text(json.dumps({"schema_version": 1, "instances": {}}), encoding="utf-8")
        projections = read_maintenance_inventory(registry)
        assert projections == () and SCHEMA_VERSION == 1
        assert parse_maintenance_inventory_bytes(registry.read_bytes()) == ()
    parser = build_parser()
    inspect = parser.parse_args(["inspect", "--target", "/tmp/solet", "--channel", "stable"])
    assert inspect.command == "inspect"
    command_names = {action.dest for action in parser._actions}
    assert not command_names & {"create", "import", "update", "doctor"}
    print("existing_solet_contract_foundation_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
