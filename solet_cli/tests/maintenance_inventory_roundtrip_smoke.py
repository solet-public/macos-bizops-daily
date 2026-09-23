"""Round-trip the complete closed Step 3 v2 inventory record."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager.maintenance_inventory import (  # noqa: E402
    parse_maintenance_inventory_v2_bytes,
    serialize_maintenance_inventory_v2,
)  # noqa: E402
from solet_manager.models import SCHEMA_VERSION  # noqa: E402

null = None


def main() -> int:
    digest = "sha256:" + "a" * 64
    record = {"instance_id":"ins_"+"1"*32,"name":"bizops","target":{"canonical_path":"/tmp/bizops","filesystem_identity":{"device":1,"inode":2},"parent_filesystem_identity":{"device":1,"inode":1}},"management_origin":"import","management_state":"diagnostic","update_eligibility":{"state":"current","reason_codes":[]},"service_identity":{"service_cli_path":"/tmp/bizops/.venv/bin/solet","bridge_cli_path":"/tmp/bizops/.venv/bin/solet-bridge","named_launcher_path":"/tmp/bin/bizops","named_launcher_target":null,"profile_id":null,"app_home":"/tmp/bizops/profile","launchagent_label":"local.solet.bizops","router_label":null,"router_socket":null},"channel":{"channel_id":"stable","descriptor_digest":digest,"canonical_repository":"https://example.test/seed.git"},"observed_provenance":{"condition":"strict","provenance_sha256":digest,"seed_id":"seed","origin_id":"origin","manifest_sha256":digest,"anchor_id":null},"source_release":{"repository":"https://example.test/seed.git","commit":"a"*40,"tree":"b"*40,"tag":null},"runtime_release":null,"verified_release":null,"contract_identities":{"diagnostic_contract_digest":digest,"current_contract_digest":null,"source_contract_digest":null,"runtime_contract_digest":null,"verified_contract_digest":null},"inspection_bundle_digest":digest,"active_operation":null,"last_verified_operation_id":null,"created_at":"2026-09-18T00:00:00Z","updated_at":"2026-09-18T00:00:00Z","last_inspected_at":"2026-09-18T00:00:00Z","last_verified_at":null}
    document = {"schema_version": 2, "records": [record]}
    parsed = parse_maintenance_inventory_v2_bytes(json.dumps(document).encode())
    assert serialize_maintenance_inventory_v2(parsed)["records"] == [record]
    assert SCHEMA_VERSION == 1

    def rejects(value: dict[str, object], label: str) -> None:
        try:
            parse_maintenance_inventory_v2_bytes(json.dumps(value).encode())
        except Exception:
            return
        raise AssertionError(f"accepted malformed v2 inventory field {label}")

    for key in document:
        malformed = deepcopy(document)
        malformed.pop(key)
        rejects(malformed, key)
    for key in record:
        malformed = deepcopy(document)
        malformed["records"][0].pop(key)
        rejects(malformed, key)
    for section in (
        "target",
        "update_eligibility",
        "service_identity",
        "channel",
        "observed_provenance",
        "source_release",
        "contract_identities",
    ):
        for key in record[section]:
            if section == "update_eligibility" and key == "reason_codes":
                continue  # the empty list is the deliberately valid value.
            malformed = deepcopy(document)
            malformed["records"][0][section][key] = []
            rejects(malformed, f"{section}.{key}")
    for nullable_release in ("runtime_release", "verified_release"):
        malformed = deepcopy(document)
        malformed["records"][0][nullable_release] = {"commit": "not-a-release"}
        rejects(malformed, nullable_release)
    print("maintenance_inventory_roundtrip_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
