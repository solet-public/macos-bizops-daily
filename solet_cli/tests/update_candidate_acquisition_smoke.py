"""Offline smoke coverage for receipt-gated update candidate acquisition."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src")]
import solet_manager.update_candidate as candidate  # noqa: E402
from solet_manager.contracts import contract_digest_from_bytes, transition_bundle_filenames  # noqa: E402
from solet_manager.errors import SourceError, TransitionContractMismatchError  # noqa: E402
from solet_manager.paths import ManagerPaths, update_candidate_cache  # noqa: E402

_KB = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_BUNDLE = {name: (_KB / name).read_bytes() for name in transition_bundle_filenames()}
_BUNDLE_DIGEST = contract_digest_from_bytes(_BUNDLE)


def _descriptor(bundle_digest: str = _BUNDLE_DIGEST) -> bytes:
    return json.dumps(
        {
            "schema_version": 3,
            "channel_id": "stable",
            "repository": "https://github.com/example/seed.git",
            "release_tag": "r1",
            "commit": "a" * 40,
            "tree_hash": "b" * 40,
            "archive_sha256": "c" * 64,
            "profile": "profile",
            "provenance": {
                "schema_version": 1,
                "provenance_sha256": "d" * 64,
                "seed_id": "123e4567-e89b-12d3-a456-426614174001",
                "origin_id": "123e4567-e89b-12d3-a456-426614174002",
                "manifest_sha256": "e" * 64,
                "bundle_name": "bundle",
                "platform": "local",
                "source_commit": "a" * 40,
                "source_date": "2026-09-18T00:00:00+00:00",
            },
            "existing_install_contract": {"flow_id": "existing-install", "flow_schema_version": 1, "bundle_digest": bundle_digest},
            "allowed_repository_migrations": [],
        },
        sort_keys=True,
    ).encode()


def _expect(kind: type[Exception], action: Callable[[], object], label: str) -> None:
    try:
        action()
    except kind:
        return
    raise AssertionError(label)


def _check_refusals(paths: ManagerPaths, got: candidate.UpdateCandidate) -> None:
    update_candidate_cache(paths, got.descriptor_digest).repository.mkdir()
    update_candidate_cache(paths, got.descriptor_digest).receipt.unlink()
    _expect(SourceError, lambda: candidate.acquire_update_candidate(paths, _descriptor()), "missing receipt accepted")
    _expect(SourceError, lambda: candidate.acquire_update_candidate(paths, b"{}"), "bad descriptor accepted")
    # A descriptor whose bundle digest does not match the committed
    # bundle is refused BEFORE any receipt is written (design 2.3).
    mismatched = _descriptor("sha256:" + "f" * 64)
    _expect(TransitionContractMismatchError, lambda: candidate.acquire_update_candidate(paths, mismatched), "bundle digest mismatch accepted")
    assert not update_candidate_cache(paths, "sha256:" + hashlib.sha256(mismatched).hexdigest()).receipt.exists()


def main() -> int:
    with TemporaryDirectory() as temp:
        paths = ManagerPaths(*((Path(temp) / name) for name in ("config", "state", "cache")))
        original = candidate._git
        calls: list[tuple[str, ...]] = []
        mismatch = True

        def git(argv: tuple[str, ...], cwd: Path | None) -> str:
            calls.append(argv)
            if "^{commit}" in argv[-1]:
                return "c" * 40 if mismatch else "a" * 40
            return "b" * 40 if "^{tree}" in argv[-1] else ""

        candidate._git = git
        original_show = candidate._git_show
        candidate._git_show = lambda repository, commit, path: _BUNDLE[path.rsplit("/", 1)[1]]
        try:
            _expect(SourceError, lambda: candidate.acquire_update_candidate(paths, _descriptor()), "candidate identity mismatch accepted")
            mismatch = False
            got = candidate.acquire_update_candidate(paths, _descriptor())
            assert got.fields.commit == "a" * 40 and len(calls) >= 7
            assert candidate.acquire_update_candidate(paths, _descriptor()).receipt_digest == got.receipt_digest
            _check_refusals(paths, got)
        finally:
            candidate._git = original
            candidate._git_show = original_show
    print("update_candidate_acquisition_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
