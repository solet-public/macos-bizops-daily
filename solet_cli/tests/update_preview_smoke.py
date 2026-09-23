"""Step-4 update preview approval-carrier smoke."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.contracts import transition_bundle_filenames  # noqa: E402
from solet_manager.existing_install_bundle import parse_transition_bundle  # noqa: E402
from solet_manager.seed_lock_parser import SeedLockFields  # noqa: E402
from solet_manager.update_candidate import UpdateCandidate  # noqa: E402
from solet_manager.update_preview import preview_update  # noqa: E402
from solet_manager.update_topology import UpdateTopology  # noqa: E402

_KB = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"


def main() -> int:
    fields = SeedLockFields("https://github.com/example/seed.git", "r1", "a" * 40, "b" * 40, "c" * 64, "profile", "stable", {}, {"bundle_digest": "sha256:" + "d" * 64}, ())
    files = {name: (_KB / name).read_bytes() for name in transition_bundle_filenames()}
    candidate = UpdateCandidate("sha256:" + "e" * 64, fields, "sha256:" + "f" * 64, parse_transition_bundle(files["existing_install_flow.json"]), files)
    ready = preview_update(candidate, UpdateTopology((), True), baseline_commit="0" * 40)
    assert ready.approval_fingerprint and ready.to_command_result().status == "preview_ready"
    blocked = preview_update(candidate, UpdateTopology(("tracked_state_present",), False), baseline_commit="0" * 40)
    assert blocked.approval_fingerprint is None and blocked.to_command_result().status == "awaiting_user"
    print("update_preview_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
