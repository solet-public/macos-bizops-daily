"""Exact, receipt-gated acquisition of a Step-4 update candidate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .contracts import transition_bundle_filenames
from .errors import ContractError, SourceError, TransitionContractMismatchError
from .existing_install_bundle import TransitionBundle, parse_transition_bundle, transition_bundle_digest
from .models import JsonValue
from .paths import ManagerPaths, update_candidate_cache
from .release_identity_gate import (
    VERDICT_INCONSISTENT,
    VERDICT_SKEW,
    ReleaseIdentityError,
    pair_manager_and_seed,
    require_manager_seed_pairing,
)
from .seed_lock_parser import SeedLockFields, parse_seed_lock_bytes
from .state_io import atomic_write_json, ensure_private_directory, instance_lock

CANDIDATE_REF_PREFIX = "refs/solet/candidates/"
TRANSITION_BUNDLE_DIRECTORY = "plugins/github_midwife_plugin/knowledge_base"
REASON_PINNED_CANDIDATE_UNSUPPORTED = "pinned_candidate_unsupported"
ACQUIRE_LOCK_NAME = "acquire.lock"


@dataclass(frozen=True, slots=True)
class UpdateCandidate:
    """The complete seed-lock identity used by all later update stages.

    ``bundle`` is the transition bundle read from the exact candidate commit
    and proven against the descriptor's ``bundle_digest``; ``bundle_files``
    are the exact bytes it was digested from (design section 2.3).
    """

    descriptor_digest: str
    fields: SeedLockFields
    receipt_digest: str
    bundle: TransitionBundle
    bundle_files: dict[str, bytes]
    cache_status: str = "reused"
    #: The §7.3 manager<->seed pairing verdict measured at acquisition, disclosed on the
    #: fresh and the runtime preview.  A refusing verdict reaches here only on a resume, with
    #: the ``resume_admission`` that admitted the pinned seed as a supported predecessor of
    #: the running release (``acquire_pinned_candidate``); otherwise it raises.
    release_identity: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    @property
    def candidate_ref(self) -> str:
        """Return the digest-derived private ref used in both cache and target."""
        return CANDIDATE_REF_PREFIX + self.descriptor_digest[7:]

    @property
    def bundle_digest(self) -> str:
        return str(cast(dict[str, object], self.fields.existing_install_contract)["bundle_digest"])


def acquire_update_candidate(
    paths: ManagerPaths, descriptor_bytes: bytes, *, transport_url: str | None = None
) -> UpdateCandidate:
    """Fetch and prove one exact tag into a Manager-owned bare cache.

    A cache directory has no authority without its matching immutable receipt.
    ``transport_url`` is a test seam for offline fixtures; the CLI never sets
    it, and the receipt always records the descriptor's canonical repository.
    """
    fields = _v3_fields(descriptor_bytes)
    # Design §7.3: the candidate's seed half must pair with the installed
    # manager half BEFORE any cache or network work -- a skewed pair is
    # refused here, a recorded skew reason or an unpairable (non-keg) manager
    # is carried on the candidate and disclosed by the preview.
    pairing = require_manager_seed_pairing(fields)
    return _acquire(paths, descriptor_bytes, fields, pairing, transport_url)


def acquire_pinned_candidate(
    paths: ManagerPaths,
    descriptor_bytes: bytes,
    running_descriptor: Callable[[], bytes],
    *,
    name: str,
    transport_url: str | None = None,
) -> UpdateCandidate:
    """Re-acquire the candidate a non-terminal journal pinned, which an earlier Manager release may have selected.

    The §7.3 gate ran when the journal was written: ``probe_update`` acquires the candidate before any journal
    exists.  After the manager formula is upgraded the running keg pairs with its OWN seed, so the pinned seed of the
    earlier release no longer pairs with it and every resume, doctor and reconcile refused (iss_f81e71d3).
    A pinned seed that still pairs takes the ordinary path.  One that does not is admitted only when the
    running keg's own seed pairs (the unchanged gate, via ``acquire_update_candidate``) and that paired
    release's proven transition bundle lists the pinned commit and tree as a supported predecessor: the
    update then finishes at a release the running Manager accepts as an update baseline.  Anything else is
    refused as ``pinned_candidate_unsupported``.  ``running_descriptor`` is read only on that path.
    """
    fields = _v3_fields(descriptor_bytes)
    verdict = pair_manager_and_seed(fields)
    if verdict["verdict"] in (VERDICT_SKEW, VERDICT_INCONSISTENT):
        running = acquire_update_candidate(paths, running_descriptor(), transport_url=transport_url)
        verdict = _admit_supported_predecessor(fields, verdict, running, name)
    return _acquire(paths, descriptor_bytes, fields, verdict, transport_url)


def _admit_supported_predecessor(fields: SeedLockFields, verdict: dict[str, JsonValue], running: UpdateCandidate, name: str) -> dict[str, JsonValue]:
    listed = running.bundle.predecessor_for(fields.commit, fields.tree_hash)
    if listed is None or listed.repository != fields.repository or fields.channel_id != running.fields.channel_id:
        raise ReleaseIdentityError(
            REASON_PINNED_CANDIDATE_UNSUPPORTED,
            f"the in-progress update is pinned to seed {fields.commit} ({fields.repository}), which does not pair with "
            f"the installed manager ({verdict['reason']}) and which the installed release {running.fields.commit} does not "
            "list as a supported predecessor",
            repair="Leave the target and the update journal as they are. Upgrade the manager "
            "(`brew tab --installed-on-request python@3.13 && brew upgrade solet`) to a "
            f"release that lists {fields.commit} as a supported predecessor, then run `solet-manager update {name} --dry-run`.",
        )
    return {
        **verdict,
        "resume_admission": {
            "kind": "supported_predecessor",
            "running_seed_commit": running.fields.commit,
            "running_release_identity": running.release_identity,
        },
    }


def _v3_fields(descriptor_bytes: bytes) -> SeedLockFields:
    fields = parse_seed_lock_bytes(descriptor_bytes)
    if fields.channel_id is None or fields.release_tag is None or fields.existing_install_contract is None:
        raise SourceError("update candidate requires an exact seed-lock v3 descriptor")
    return fields


def _acquire(
    paths: ManagerPaths, descriptor_bytes: bytes, fields: SeedLockFields, pairing: dict[str, JsonValue], transport_url: str | None
) -> UpdateCandidate:
    digest = "sha256:" + hashlib.sha256(descriptor_bytes).hexdigest()
    cache = update_candidate_cache(paths, digest)
    ensure_private_directory(cache.repository.parent)
    # The entry is shared by every solet on the host and a dry-run holds no instance lock: the
    # receipt check, the discard of a receipt-less repository and the fetch run under one
    # exclusive lock per entry, so no acquisition can delete another's work (iss_285d061f).
    with instance_lock(cache.repository.parent / ACQUIRE_LOCK_NAME, create=True):
        return _acquire_holding_entry_lock(cache.repository, cache.receipt, digest, fields, pairing, transport_url)


def _acquire_holding_entry_lock(
    repository: Path, receipt_path: Path, digest: str, fields: SeedLockFields, pairing: dict[str, JsonValue], transport_url: str | None
) -> UpdateCandidate:
    if receipt_path.exists():
        receipt_digest = _read_receipt(receipt_path, digest, fields)
        _verify_cached_objects(repository, digest, fields)
        bundle, files = read_transition_bundle(repository, fields)
        return UpdateCandidate(digest, fields, receipt_digest, bundle, files, "reused", pairing)
    _discard_unreceipted(repository)
    _git(("git", "init", "--bare", str(repository)), None)
    private_ref = CANDIDATE_REF_PREFIX + digest[7:]
    tag_ref = f"refs/tags/{fields.release_tag}:{private_ref}"
    source = fields.repository if transport_url is None else transport_url
    _git(("git", "fetch", "--no-tags", source, tag_ref), repository)
    commit = _git(("git", "rev-parse", f"{private_ref}^{{commit}}"), repository)
    tree = _git(("git", "rev-parse", f"{commit}^{{tree}}"), repository)
    if commit != fields.commit or tree != fields.tree_hash:
        raise SourceError("candidate tag, commit, or tree differs from the descriptor")
    # The transition bundle is proven against the descriptor BEFORE the receipt
    # exists: a cache entry with a receipt but a mismatched bundle would be a
    # reusable lie (design section 2.3).
    bundle, files = read_transition_bundle(repository, fields)
    receipt = {"descriptor_digest": digest, "commit": commit, "tree": tree, "repository": fields.repository, "tag": fields.release_tag}
    receipt_digest = "sha256:" + hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    atomic_write_json(receipt_path, {**receipt, "receipt_digest": receipt_digest})
    return UpdateCandidate(digest, fields, receipt_digest, bundle, files, "acquired", pairing)


def _discard_unreceipted(repository: Path) -> None:
    """A repository without its receipt is an interrupted acquisition (an offline fetch), never proof.

    Called only under the entry lock with no receipt present: it is discarded and fetched again rather
    than wedge every later verb (iss_285d061f).  ``rmtree`` refuses a symlink or a regular file rather than
    follow it; that refusal, like any other, is a typed ``SourceError``.
    """
    if not os.path.lexists(repository):
        return
    try:
        shutil.rmtree(repository)
    except OSError as exc:
        raise SourceError(
            f"cannot discard the interrupted candidate cache repository {repository}: {exc}",
            repair=f"Remove {repository} (a Manager cache directory, not part of the solet), then run the command again.",
        ) from exc


def read_transition_bundle(repository: Path, fields: SeedLockFields) -> tuple[TransitionBundle, dict[str, bytes]]:
    """Read the closed bundle file set from the exact candidate commit and prove its digest.

    The bytes come from ``git show <commit>:<path>`` inside the Manager cache,
    never from a working tree, so a Manager-packaged copy can only ever be an
    acquisition aid.  A recomputed digest that differs from the seed-lock
    declaration is ``transition_contract_mismatch``.
    """
    contract = cast(dict[str, object], fields.existing_install_contract)
    declared = str(contract["bundle_digest"])
    files = {
        name: _git_blob(repository, fields.commit, f"{TRANSITION_BUNDLE_DIRECTORY}/{name}")
        for name in transition_bundle_filenames()
    }
    if transition_bundle_digest(files) != declared:
        raise TransitionContractMismatchError(
            "the candidate commit's transition bundle does not digest to the seed-lock declaration",
            repair="Refuse this candidate; the release descriptor and the committed bundle disagree.",
        )
    try:
        bundle = parse_transition_bundle(files["existing_install_flow.json"])
    except ContractError as exc:
        raise TransitionContractMismatchError(
            f"the candidate's transition bundle is malformed: {exc}",
            repair="Refuse this candidate; the committed bundle violates the closed schema.",
        ) from exc
    if contract["flow_id"] != bundle.flow_id or contract["flow_schema_version"] != bundle.schema_version:
        raise TransitionContractMismatchError(
            "the seed-lock contract identity does not name the committed bundle's flow",
            repair="Refuse this candidate; the descriptor flow identity and the bundle disagree.",
        )
    return bundle, files


def _read_receipt(path: Path, digest: str, fields: SeedLockFields) -> str:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError("candidate receipt is unreadable") from exc
    if not isinstance(value, dict) or value.get("descriptor_digest") != digest or value.get("repository") != fields.repository or value.get("tag") != fields.release_tag or value.get("commit") != fields.commit or value.get("tree") != fields.tree_hash or not isinstance(value.get("receipt_digest"), str):
        raise SourceError("candidate receipt does not prove the descriptor identity")
    return str(value["receipt_digest"])


def _verify_cached_objects(repository: Path, digest: str, fields: SeedLockFields) -> None:
    """A reused cache entry must still resolve its private ref to the receipt identity."""
    commit = _git(("git", "rev-parse", f"{CANDIDATE_REF_PREFIX + digest[7:]}^{{commit}}"), repository)
    tree = _git(("git", "rev-parse", f"{commit}^{{tree}}"), repository)
    if commit != fields.commit or tree != fields.tree_hash:
        raise SourceError("candidate cache repository no longer proves its receipt")


def _git_blob(repository: Path, commit: str, path: str) -> bytes:
    """Read one blob from the exact candidate commit; a missing file is a contract mismatch."""
    try:
        return _git_show(repository, commit, path)
    except SourceError as exc:
        raise TransitionContractMismatchError(
            f"the candidate commit does not provide transition bundle file {path}: {exc}",
            repair="Refuse this candidate; the committed bundle is incomplete.",
        ) from exc


def _git_show(repository: Path, commit: str, path: str) -> bytes:
    """Byte-exact ``git show``; separate from ``_git`` because the text helper strips output."""
    environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    completed = subprocess.run(("git", "show", f"{commit}:{path}"), cwd=repository, stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120, env=environment)  # noqa: S603
    if completed.returncode:
        raise SourceError(completed.stderr.decode("utf-8", "replace").strip())
    return completed.stdout


def _git(argv: tuple[str, ...], cwd: Path | None) -> str:
    environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    completed = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, check=False, text=True, timeout=120, env=environment)  # noqa: S603
    if completed.returncode:
        raise SourceError(f"candidate Git verification failed: {completed.stderr.strip()}")
    return completed.stdout.strip()
