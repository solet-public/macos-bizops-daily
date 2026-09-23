"""Exact, receipt-gated acquisition of a Step-4 update candidate."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .contracts import transition_bundle_filenames
from .errors import ContractError, SourceError, TransitionContractMismatchError
from .existing_install_bundle import TransitionBundle, parse_transition_bundle, transition_bundle_digest
from .models import JsonValue
from .paths import ManagerPaths, update_candidate_cache
from .release_identity_gate import require_manager_seed_pairing
from .seed_lock_parser import SeedLockFields, parse_seed_lock_bytes
from .state_io import atomic_write_json, ensure_private_directory

CANDIDATE_REF_PREFIX = "refs/solet/candidates/"
TRANSITION_BUNDLE_DIRECTORY = "plugins/github_midwife_plugin/knowledge_base"


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
    #: The §7.3 manager<->seed pairing verdict measured at selection, disclosed
    #: on the preview; a refusing verdict never reaches here (it raises).
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
    fields = parse_seed_lock_bytes(descriptor_bytes)
    if fields.channel_id is None or fields.release_tag is None or fields.existing_install_contract is None:
        raise SourceError("update candidate requires an exact seed-lock v3 descriptor")
    # Design §7.3: the candidate's seed half must pair with the installed
    # manager half BEFORE any cache or network work -- a skewed pair is
    # refused here, a recorded skew reason or an unpairable (non-keg) manager
    # is carried on the candidate and disclosed by the preview.
    pairing = require_manager_seed_pairing(fields)
    digest = "sha256:" + hashlib.sha256(descriptor_bytes).hexdigest()
    cache = update_candidate_cache(paths, digest)
    receipt_path, repository = cache.receipt, cache.repository
    if receipt_path.exists():
        receipt_digest = _read_receipt(receipt_path, digest, fields)
        _verify_cached_objects(repository, digest, fields)
        bundle, files = read_transition_bundle(repository, fields)
        return UpdateCandidate(digest, fields, receipt_digest, bundle, files, "reused", pairing)
    ensure_private_directory(repository.parent)
    if repository.exists():
        raise SourceError("candidate cache repository lacks its immutable receipt")
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
