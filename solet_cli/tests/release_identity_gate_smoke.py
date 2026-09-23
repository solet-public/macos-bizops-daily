"""The §7.3 manager<->seed pairing is enforced where a candidate is selected.

Pins ``release_identity_gate`` and its two consumers.  The verdict table:
``paired`` when the receipt's and the seed provenance's ``source_commit``
agree; ``skew_allowed`` only when a manifest that itself agrees with both
halves records an ``allow_manager_seed_skew`` reason; ``skew`` (refused,
``manager_seed_revision_skew``) when they differ and nothing recorded permits
it; ``inconsistent`` (refused, ``release_identity_inconsistent``) when the
manifest, receipt and lock disagree about what they are, or a receipt that
exists cannot be read; ``unpairable`` (recorded, not refused) when the
manager carries no receipt at all because it was never a Formula keg.

Then the two hard-gate sites: ``acquire_update_candidate`` refuses a skewed
descriptor BEFORE it touches a cache or the network, and ``preview_import``
refuses a skewed installed descriptor before inspection.  Both are driven by
pointing the gate's default receipt path at a fixture file.

Offline: fixture receipts, locks, manifests; no git fetch ever runs because
the refusal precedes it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(Path(__file__).resolve().parent)]

import json  # noqa: E402
import tempfile  # noqa: E402
from typing import Any  # noqa: E402

import solet_manager.import_enrollment as enrollment  # noqa: E402
import solet_manager.release_identity_gate as gate  # noqa: E402
from _release_identity_fixture import ORIGIN_ID, OTHER_COMMIT, SEED_ID, SOURCE_COMMIT, build_seed_checkout, manifest, seed_lock_v3, write_json, write_receipt  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ChannelInspectionIdentity,
    ExistingInstallContractIdentity,
    InstalledInspectionMetadata,
)
from solet_manager.import_enrollment import ImportRequest, preview_import  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_identity_gate import (  # noqa: E402
    REASON_INCONSISTENT,
    REASON_SKEW,
    ReleaseIdentityError,
    pair_manager_and_seed,
    require_manager_seed_pairing,
)
from solet_manager.release_lock import seed_lock_from_fields  # noqa: E402
from solet_manager.seed_lock_parser import parse_seed_lock_bytes  # noqa: E402
from solet_manager.update_candidate import acquire_update_candidate  # noqa: E402

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _descriptor(seed: dict[str, str], source_commit: str) -> bytes:
    return (json.dumps(seed_lock_v3(seed, source_commit=source_commit), indent=2, sort_keys=True) + "\n").encode()


def _assert_verdict_table(root: Path, seed: dict[str, str]) -> None:
    fields = parse_seed_lock_bytes(_descriptor(seed, SOURCE_COMMIT))
    receipt = write_receipt(root / "paired" / "install-source.json")
    paired = pair_manager_and_seed(fields, install_source_path=receipt, manifest_path=root / "none")
    _check(paired["verdict"] == "paired" and paired["reason"] is None, f"equal source commits pair: {paired}")
    _check(paired["manager_source_commit"] == SOURCE_COMMIT and paired["seed_source_commit"] == SOURCE_COMMIT, "both commits are recorded on the verdict")
    skewed = pair_manager_and_seed(fields, install_source_path=write_receipt(root / "skew" / "install-source.json", OTHER_COMMIT), manifest_path=root / "none")
    _check(skewed["verdict"] == "skew" and skewed["reason"] == REASON_SKEW, f"different commits with no manifest are skew: {skewed}")
    allowed_manifest = manifest(seed=seed, file_digests={}, source_commit=OTHER_COMMIT, allow_manager_seed_skew="hotfix r44: manager-only rebuild")
    allowed = pair_manager_and_seed(fields, install_source_path=root / "skew" / "install-source.json", manifest=allowed_manifest)
    _check(allowed["verdict"] == "skew_allowed" and allowed["allow_manager_seed_skew"] == "hotfix r44: manager-only rebuild", f"a manifest-recorded reason allows the skew and is carried: {allowed}")
    silent_manifest = manifest(seed=seed, file_digests={}, source_commit=OTHER_COMMIT)
    silent = pair_manager_and_seed(fields, install_source_path=root / "skew" / "install-source.json", manifest=silent_manifest)
    _check(silent["verdict"] == "skew", "a manifest without a recorded reason does not excuse the skew")
    lying = manifest(seed=seed, file_digests={}, source_commit=SOURCE_COMMIT, allow_manager_seed_skew="forged")
    inconsistent = pair_manager_and_seed(fields, install_source_path=root / "skew" / "install-source.json", manifest=lying)
    _check(inconsistent["verdict"] == "inconsistent" and inconsistent["reason"] == "manifest_manager_source_commit_mismatch", f"a manifest that does not describe this keg's manager cannot excuse anything: {inconsistent}")
    other_seed = manifest(seed={**seed, "commit": "0" * 40}, file_digests={})
    _check(pair_manager_and_seed(fields, install_source_path=receipt, manifest=other_seed)["reason"] == "manifest_seed_identity_mismatch", "a manifest naming another seed commit is inconsistent")
    unpairable = pair_manager_and_seed(fields, install_source_path=root / "absent" / "install-source.json", manifest_path=root / "none")
    _check(unpairable["verdict"] == "unpairable" and unpairable["reason"] == "install_source_absent", "no receipt at all is unpairable, recorded")
    damaged = root / "damaged" / "install-source.json"
    write_json(damaged, {"source": "fixture-keg", "version": "0.1.0"})
    broken = pair_manager_and_seed(fields, install_source_path=damaged, manifest_path=root / "none")
    _check(broken["verdict"] == "inconsistent" and str(broken["reason"]).startswith("install_source_unreadable"), "a receipt that exists but cannot be read is inconsistent, not unpairable")
    unreadable_manifest = root / "bad" / "release_manifest.json"
    unreadable_manifest.parent.mkdir(parents=True)
    unreadable_manifest.write_text("{}", encoding="utf-8")
    _check(pair_manager_and_seed(fields, install_source_path=receipt, manifest_path=unreadable_manifest)["verdict"] == "inconsistent", "a damaged manifest beside a good receipt is inconsistent")
    v1_fields = parse_seed_lock_bytes(json.dumps({"schema_version": 1, "repository": "https://github.com/solet-public/macos-bizops.git", "release_tag": "r1", "commit": seed["commit"], "tree_hash": seed["tree_hash"], "profile": "macos-bizops"}).encode())
    no_provenance = pair_manager_and_seed(v1_fields, install_source_path=receipt, manifest_path=root / "none")
    _check(no_provenance["verdict"] == "unpairable" and no_provenance["reason"] == "seed_provenance_absent", "a lock without provenance is unpairable, named")


def _assert_raising_form(root: Path, seed: dict[str, str]) -> None:
    fields = parse_seed_lock_bytes(_descriptor(seed, SOURCE_COMMIT))
    verdict = require_manager_seed_pairing(fields, install_source_path=root / "paired" / "install-source.json", manifest_path=root / "none")
    _check(verdict["verdict"] == "paired", "the raising form returns a passing verdict")
    _check(require_manager_seed_pairing(fields, install_source_path=root / "absent" / "install-source.json", manifest_path=root / "none")["verdict"] == "unpairable", "the raising form lets an unpairable manager through, recorded")
    for receipt, expected in ((root / "skew" / "install-source.json", REASON_SKEW), (root / "damaged" / "install-source.json", REASON_INCONSISTENT)):
        try:
            require_manager_seed_pairing(fields, install_source_path=receipt, manifest_path=root / "none")
        except ReleaseIdentityError as exc:
            _check(exc.error_kind == expected and exc.repair is not None, f"{expected} is refused with its named reason and a repair: {exc.error_kind}")
        else:
            raise AssertionError(f"red: {expected} was not refused")


def _assert_update_candidate_refuses_before_any_fetch(root: Path, seed: dict[str, str]) -> None:
    paths = ManagerPaths.resolve(explicit_home=root / "home")
    original = gate.default_install_source_path
    gate.default_install_source_path = lambda: root / "skew" / "install-source.json"
    try:
        acquire_update_candidate(paths, _descriptor(seed, SOURCE_COMMIT), transport_url="file:///nonexistent")
    except ReleaseIdentityError as exc:
        _check(exc.error_kind == REASON_SKEW, f"update candidate selection refuses a skewed descriptor: {exc.error_kind}")
    else:
        raise AssertionError("red: a skewed update candidate was acquired")
    finally:
        gate.default_install_source_path = original
    _check(not paths.cache_dir.exists() or not any(paths.cache_dir.rglob("*")), "the refusal happened before any candidate cache was written")


def _metadata(seed: dict[str, str]) -> InstalledInspectionMetadata:
    lock = seed_lock_from_fields(parse_seed_lock_bytes(_descriptor(seed, SOURCE_COMMIT)))
    identity = ChannelInspectionIdentity(
        "stable", lock.repository, "release-2026-09-19-fixture", lock.commit, lock.tree_hash, lock.profile,
        "f" * 64, SEED_ID, ORIGIN_ID, "8" * 64,
        ExistingInstallContractIdentity("existing-install", 1, "sha256:" + "c" * 64),
        "released_metadata/catalog", "1" * 64, "share/solet/seed.lock.json", "2" * 64, "sha256:" + "3" * 64, "released_metadata/anchors", "4" * 64,
    )
    return InstalledInspectionMetadata(identity, lock, ())


def _assert_import_preview_refuses(root: Path, seed: dict[str, str]) -> None:
    target = root / "target"
    target.mkdir()
    calls: list[str] = []

    def _inspect(request: Any, metadata_loader: Any) -> Any:
        calls.append("inspect")
        metadata_loader("stable", None)
        raise AssertionError("red: inspection continued past a skewed installed descriptor")

    original_inspect, original_loader, original_path = enrollment.inspect_existing_install, enrollment.load_installed_inspection_metadata, gate.default_install_source_path
    enrollment.inspect_existing_install = _inspect
    enrollment.load_installed_inspection_metadata = lambda channel, tracker: _metadata(seed)
    gate.default_install_source_path = lambda: root / "skew" / "install-source.json"
    try:
        preview_import(ImportRequest("fixture", target, "stable", ManagerPaths.resolve(explicit_home=root / "home")))
    except ReleaseIdentityError as exc:
        _check(exc.error_kind == REASON_SKEW and calls == ["inspect"], f"import preview refuses the skewed installed descriptor at selection: {exc.error_kind}")
    else:
        raise AssertionError("red: a skewed import preview completed")
    finally:
        enrollment.inspect_existing_install, enrollment.load_installed_inspection_metadata, gate.default_install_source_path = original_inspect, original_loader, original_path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seed = build_seed_checkout(root / "seed")
        _assert_verdict_table(root, seed)
        _assert_raising_form(root, seed)
        _assert_update_candidate_refuses_before_any_fetch(root, seed)
        _assert_import_preview_refuses(root, seed)
    print(f"release_identity_gate_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
