"""Manager<->seed same-revision pairing at consumption (design §7.3, §7.2).

The manager and the seed are cut from ONE source revision (stage 5's
``_require_same_source_revision``), but nothing at the consuming end ever
checked that the keg a customer actually has pairs its two halves
(``iss_da99a951``): a landed install-path fix can ship in one half and be
absent from the other, and ``update``/``import`` would act on the mismatched
pair without noticing.

This gate is the consumption-side half.  It reads the two declarations a
keg already carries -- the Formula-written ``install-source.json``
(``source_commit`` of the manager payload) and the candidate seed lock's
``provenance.source_commit`` -- and, when a release manifest is present
beside them, the manifest's own record of both plus its
``allow_manager_seed_skew`` reason.  The verdicts:

- ``paired``          the two source commits agree.
- ``skew_allowed``    they differ AND the manifest records an explicit
                      ``allow_manager_seed_skew`` reason (stage 5's escape
                      hatch, recorded there, honoured here, never silent).
- ``skew``            they differ and nothing recorded permits it -> REFUSED.
- ``inconsistent``    manifest, receipt and lock disagree about what they
                      each are (``release_identity_inconsistent``) -> REFUSED.
- ``unpairable``      the manager carries no receipt at all (a checkout that
                      was never a Formula keg -- an editable install, a test
                      venv).  There is nothing to pair against, so this is
                      recorded on the candidate and disclosed, not refused;
                      a receipt that EXISTS but cannot be read is
                      ``inconsistent``, because a keg wrote it and something
                      since has damaged it.

Report-only callers (``solet attest``, ``doctor::release_identity_v1``) take
the verdict; the ``update``/``import`` candidate paths take the raising form.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, cast

from .errors import SourceError, UpdateBlockedError
from .models import JsonValue
from .release_identity import (
    INSTALL_SOURCE_NAME,
    RELEASE_MANIFEST_NAME,
    InstallSource,
    load_install_source,
    load_release_manifest,
)

VERDICT_PAIRED = "paired"
VERDICT_SKEW_ALLOWED = "skew_allowed"
VERDICT_SKEW = "skew"
VERDICT_INCONSISTENT = "inconsistent"
VERDICT_UNPAIRABLE = "unpairable"

REASON_SKEW = "manager_seed_revision_skew"
REASON_INCONSISTENT = "release_identity_inconsistent"

_REFUSED = frozenset({VERDICT_SKEW, VERDICT_INCONSISTENT})


class SeedIdentity(Protocol):
    """The seed-lock fields the pairing reads (``SeedLockFields`` and ``SeedLock`` both fit)."""

    @property
    def commit(self) -> str: ...

    @property
    def tree_hash(self) -> str: ...

    @property
    def provenance(self) -> dict[str, object] | None: ...


class ReleaseIdentityError(UpdateBlockedError):
    """A candidate's manager and seed halves do not pair, or their records disagree."""


def default_install_source_path() -> Path:
    return Path(sys.prefix) / "share" / "solet" / INSTALL_SOURCE_NAME


def default_release_manifest_path() -> Path:
    return Path(sys.prefix) / "share" / "solet" / RELEASE_MANIFEST_NAME


def pair_manager_and_seed(
    seed: SeedIdentity,
    *,
    install_source_path: Path | None = None,
    manifest_path: Path | None = None,
    manifest: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Return the pairing verdict as a closed, serialisable record.

    ``manifest`` short-circuits the on-disk read when the caller already
    loaded one (``solet attest --against``); otherwise the default keg
    location is consulted and its absence is a recorded fact, not an error.
    """

    receipt_path = default_install_source_path() if install_source_path is None else install_source_path
    verdict: dict[str, JsonValue] = {
        "verdict": VERDICT_UNPAIRABLE,
        "reason": None,
        "install_source_path": str(receipt_path),
        "manager_source_commit": None,
        "seed_source_commit": _seed_source_commit(seed),
        "seed_commit": seed.commit,
        "allow_manager_seed_skew": None,
        "manifest_present": False,
    }
    receipt = _read_receipt(receipt_path, verdict)
    if receipt is None:
        return verdict
    verdict["manager_source_commit"] = receipt.source_commit
    loaded = _read_manifest(manifest, manifest_path, verdict)
    if verdict["verdict"] == VERDICT_INCONSISTENT:
        return verdict
    if loaded is not None and not _manifest_agrees(loaded, receipt, seed, verdict):
        return verdict
    _grade_pairing(receipt, verdict)
    return verdict


def _grade_pairing(receipt: InstallSource, verdict: dict[str, JsonValue]) -> None:
    seed_commit = verdict["seed_source_commit"]
    if seed_commit is None:
        verdict["reason"] = "seed_provenance_absent"
        return
    if receipt.source_commit == seed_commit:
        verdict["verdict"], verdict["reason"] = VERDICT_PAIRED, None
        return
    allowed = verdict["allow_manager_seed_skew"]
    if isinstance(allowed, str) and allowed:
        verdict["verdict"], verdict["reason"] = VERDICT_SKEW_ALLOWED, None
        return
    verdict["verdict"], verdict["reason"] = VERDICT_SKEW, REASON_SKEW


def require_manager_seed_pairing(
    seed: SeedIdentity,
    *,
    install_source_path: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, JsonValue]:
    """The hard gate: return the verdict, or refuse the candidate with a named reason."""

    verdict = pair_manager_and_seed(seed, install_source_path=install_source_path, manifest_path=manifest_path)
    if verdict["verdict"] not in _REFUSED:
        return verdict
    if verdict["verdict"] == VERDICT_SKEW:
        raise ReleaseIdentityError(
            REASON_SKEW,
            "the installed manager and the candidate seed were cut from different source "
            f"revisions ({verdict['manager_source_commit']} vs {verdict['seed_source_commit']}) "
            "and no release manifest records an allow_manager_seed_skew reason",
            repair="Upgrade the manager and seed together (brew tab --installed-on-request python@3.13 && brew upgrade solet), or stage a release "
            "that records --allow-manager-seed-skew <reason> in its manifest.",
        )
    raise ReleaseIdentityError(
        REASON_INCONSISTENT,
        f"the keg's release identity records disagree: {verdict['reason']}",
        repair="Reinstall the manager formula; the receipt, seed lock and manifest it wrote no longer describe one release.",
    )


def _seed_source_commit(seed: SeedIdentity) -> str | None:
    provenance = seed.provenance
    if provenance is None:
        return None
    value = provenance.get("source_commit")
    return value if isinstance(value, str) else None


def _read_receipt(path: Path, verdict: dict[str, JsonValue]) -> InstallSource | None:
    if not path.exists():
        verdict["reason"] = "install_source_absent"
        return None
    try:
        return load_install_source(path)
    except SourceError as exc:
        verdict["verdict"], verdict["reason"] = VERDICT_INCONSISTENT, f"install_source_unreadable: {exc}"
        return None


def _read_manifest(
    manifest: dict[str, JsonValue] | None, manifest_path: Path | None, verdict: dict[str, JsonValue]
) -> dict[str, JsonValue] | None:
    if manifest is not None:
        verdict["manifest_present"] = True
        return manifest
    path = default_release_manifest_path() if manifest_path is None else manifest_path
    if not path.exists():
        return None
    try:
        loaded = load_release_manifest(path)
    except SourceError as exc:
        verdict["verdict"], verdict["reason"] = VERDICT_INCONSISTENT, f"release_manifest_unreadable: {exc}"
        return None
    verdict["manifest_present"] = True
    return loaded


def _manifest_agrees(
    manifest: dict[str, JsonValue], receipt: InstallSource, seed: SeedIdentity, verdict: dict[str, JsonValue]
) -> bool:
    """The manifest must describe the SAME manager and seed the keg carries, or it proves nothing."""

    manager = manifest.get("manager")
    seed_section = manifest.get("seed")
    if not isinstance(manager, dict) or not isinstance(seed_section, dict):
        verdict["verdict"], verdict["reason"] = VERDICT_INCONSISTENT, "manifest_sections_absent"
        return False
    manager_section = cast(dict[str, JsonValue], manager)
    if manager_section.get("source_commit") != receipt.source_commit:
        verdict["verdict"], verdict["reason"] = VERDICT_INCONSISTENT, "manifest_manager_source_commit_mismatch"
        return False
    seed_record = cast(dict[str, JsonValue], seed_section)
    if seed_record.get("commit") != seed.commit or seed_record.get("tree_hash") != seed.tree_hash:
        verdict["verdict"], verdict["reason"] = VERDICT_INCONSISTENT, "manifest_seed_identity_mismatch"
        return False
    if seed_record.get("source_commit") != verdict["seed_source_commit"]:
        verdict["verdict"], verdict["reason"] = VERDICT_INCONSISTENT, "manifest_seed_source_commit_mismatch"
        return False
    allowed = manager_section.get("allow_manager_seed_skew")
    verdict["allow_manager_seed_skew"] = allowed if isinstance(allowed, str) else None
    return True


__all__ = [
    "REASON_INCONSISTENT",
    "REASON_SKEW",
    "VERDICT_INCONSISTENT",
    "VERDICT_PAIRED",
    "VERDICT_SKEW",
    "VERDICT_SKEW_ALLOWED",
    "VERDICT_UNPAIRABLE",
    "ReleaseIdentityError",
    "SeedIdentity",
    "default_install_source_path",
    "default_release_manifest_path",
    "pair_manager_and_seed",
    "require_manager_seed_pairing",
]
