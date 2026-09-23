"""Step-6 durable contract copies (design sections 3.2, 4.5, 4.6): written at Step-4 apply, self-sufficient,
read first on resume, backfilled for a copy-less journal, refused on tamper, and the HEAD-blob binding."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import advance_to_source_advanced, build_fixture, db_spy, expect, runtime_fingerprint  # noqa: E402
from solet_manager._existing_install_inspection_metadata import anchors_document, descriptor_from_copy  # noqa: E402
from solet_manager.contract_copies import (  # noqa: E402
    ANCHORS_COPY_NAME,
    RECEIPT_NAME,
    SEED_LOCK_COPY_NAME,
    descriptor_copy_exists,
    read_descriptor_copy,
    read_transition_contract,
    transition_contract_exists,
    write_descriptor_copy,
    write_transition_contract,
)
from solet_manager.errors import StateError, TransitionContractMismatchError  # noqa: E402
from solet_manager.paths import descriptor_copy_dir, transition_contract_dir  # noqa: E402
from solet_manager.update_execution import apply_update  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _assert_written_at_apply(root: Path) -> None:
    fixture = build_fixture(root)
    _check(not transition_contract_exists(fixture.paths, fixture.contract) and not descriptor_copy_exists(fixture.paths, fixture.descriptor_digest), "no copies before the first apply")
    advance_to_source_advanced(fixture)
    _check(transition_contract_exists(fixture.paths, fixture.contract) and descriptor_copy_exists(fixture.paths, fixture.descriptor_digest), "both copies exist after the Step-4 apply")
    files = read_transition_contract(fixture.paths, fixture.contract)
    _check(set(files) == {"existing_install_flow.json", "existing_install_flow.schema.json", "setup_adapter_envelope.schema.json"}, "the closed three-file set is copied")
    seed_raw, anchors, receipt = read_descriptor_copy(fixture.paths, fixture.descriptor_digest)
    _check("sha256:" + hashlib.sha256(seed_raw).hexdigest() == fixture.descriptor_digest, "the copied seed lock digests to the journaled descriptor digest")
    _check(set(receipt) == {"descriptor_digest", "anchors_sha256", "catalog_path", "catalog_sha256", "channel_id", "manager_version"} and receipt["channel_id"] == "stable", "the receipt carries the closed key set")
    _check(anchors == {"schema_version": 1, "anchors": []}, "the fixture's empty anchor table round-trips")
    rebuilt = descriptor_from_copy(fixture.paths, fixture.descriptor_digest)
    identity = rebuilt.metadata.channel_identity
    _check(identity.commit == fixture.candidate.commit and identity.descriptor_digest == fixture.descriptor_digest and identity.seed_lock_resource.startswith("copy:") and identity.anchor_table_resource.startswith("copy:"), "descriptor_from_copy rebuilds the exact identity with copy-sourced resources")
    _check(rebuilt.descriptor_bytes == seed_raw, "the rebuilt descriptor carries the exact copied bytes")
    _check(transition_contract_dir(fixture.paths, fixture.contract).is_relative_to(fixture.paths.state_dir) and descriptor_copy_dir(fixture.paths, fixture.descriptor_digest).is_relative_to(fixture.paths.state_dir), "copies live under state_dir, never cache_dir")


def _assert_backfill_and_idempotence(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    shutil.rmtree(transition_contract_dir(fixture.paths, fixture.contract))
    shutil.rmtree(descriptor_copy_dir(fixture.paths, fixture.descriptor_digest))
    _check(not transition_contract_exists(fixture.paths, fixture.contract), "copies removed to simulate a journal written by the landed Step-4/5 code")
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "promoted", f"a copy-less journal resumes and backfills: {result.status} {result.error_kind}")
    _check(transition_contract_exists(fixture.paths, fixture.contract) and descriptor_copy_exists(fixture.paths, fixture.descriptor_digest), "copies backfilled on resume")
    before = {path: path.read_bytes() for path in transition_contract_dir(fixture.paths, fixture.contract).iterdir()}
    write_transition_contract(fixture.paths, fixture.contract, read_transition_contract(fixture.paths, fixture.contract))
    _check({path: path.read_bytes() for path in transition_contract_dir(fixture.paths, fixture.contract).iterdir()} == before, "rewriting identical bytes is a no-op")
    expect(TransitionContractMismatchError, lambda: write_transition_contract(fixture.paths, fixture.contract, {**read_transition_contract(fixture.paths, fixture.contract), "existing_install_flow.json": b"{}"}), "mismatching bytes accepted by the writer")


def _assert_tamper(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    directory = descriptor_copy_dir(fixture.paths, fixture.descriptor_digest)
    seed = directory / SEED_LOCK_COPY_NAME
    original = seed.read_bytes()
    seed.write_bytes(original + b"\n")
    expect(StateError, lambda: read_descriptor_copy(fixture.paths, fixture.descriptor_digest), "tampered seed lock accepted")
    seed.write_bytes(original)
    anchors = directory / ANCHORS_COPY_NAME
    anchors_original = anchors.read_bytes()
    anchors.write_bytes(anchors_original + b" ")
    expect(StateError, lambda: read_descriptor_copy(fixture.paths, fixture.descriptor_digest), "tampered anchors accepted")
    anchors.write_bytes(anchors_original)
    receipt = directory / RECEIPT_NAME
    receipt_original = receipt.read_bytes()
    receipt.write_text(json.dumps({**json.loads(receipt_original), "channel_id": "other"}))
    _check(read_descriptor_copy(fixture.paths, fixture.descriptor_digest)[2]["channel_id"] == "other", "a receipt field the digests do not cover is read as recorded")
    receipt.write_bytes(receipt_original)
    read_descriptor_copy(fixture.paths, fixture.descriptor_digest)
    fingerprint = runtime_fingerprint(fixture)
    flow = transition_contract_dir(fixture.paths, fixture.contract) / "existing_install_flow.json"
    flow.write_bytes(flow.read_bytes() + b" ")
    exc = expect(TransitionContractMismatchError, lambda: apply_update(fixture.request, fingerprint), "tampered transition copy accepted at re-entry")
    _check(cast(TransitionContractMismatchError, exc).exit_code == 3, "transition_contract_mismatch is exit 3")


def _assert_head_blob_binding(root: Path) -> None:
    """Section 4.6 step 6: the target's committed bundle must equal the copy's bytes."""
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    files = read_transition_contract(fixture.paths, fixture.contract)
    committed = (fixture.target / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json").read_bytes()
    _check(committed == files["existing_install_flow.json"], "the target's committed bundle equals the copy")
    document = anchors_document(fixture.request.descriptor_loader("stable", cast(object, None)).metadata)  # type: ignore[arg-type]
    _check(document["schema_version"] == 1, "anchors_document renders the v1 shape")
    digest = write_descriptor_copy(fixture.paths, descriptor_bytes=b"{}", anchors_document=document, catalog_path="catalog", catalog_sha256="0" * 64, channel_id="stable", manager_version="0.1.0")
    _check(digest == "sha256:" + hashlib.sha256(b"{}").hexdigest() and descriptor_copy_exists(fixture.paths, digest), "an arbitrary descriptor copy is content-addressed")
    result = apply_update(fixture.request, fingerprint)
    _check(result.status == "promoted", "an unrelated copy does not disturb the bound one")


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_written_at_apply(root / "apply")
        _assert_backfill_and_idempotence(root / "backfill")
        _assert_tamper(root / "tamper")
        _assert_head_blob_binding(root / "head")
    print(f"existing_install_contract_copy_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
