"""Passive blue-green deployment advisories over a target's release state.

Detection coverage for the blue-green rows of the 2026-09-02 doctor-detection
audit.  Every check here is REPORT-ONLY, for the reason the vintage census
gives: blue-green deployment state is not part of the target's pinned
completion contract, so a divergence here must be named loudly but must not
retroactively refuse an installation that its own contract verified.

What this module can and cannot see, stated because the boundary is what makes
the checks honest:

* The release ledger (``state.json``) and the ``current`` / ``previous``
  symlinks are DURABLE on the target and are read here directly.
* ``RouterState`` (drain entries, live bindings) is process-memory only and is
  never persisted, so a target at rest carries no readable residue of it.  No
  check in this module claims to grade it.

Rows detected
-------------
``iss_68f363ae`` (D-1.4) -- ``_compensate_failed_swap`` clears the
``in_progress`` marker even when the symlink restore raised ``OSError``.
``ReleaseSwapper.reconcile`` then keys ONLY on that marker
(``release_manager.py``: ``if ledger.in_progress is None: return
RECONCILE_NOOP``), so it returns NOOP and never compares the ledger against
the symlinks.  Ledger and symlinks disagree permanently, and until now nothing
anywhere compared them.  :func:`_ledger_symlink_agreement_advisory` is that
missing comparator.

``iss_9789ed45`` (D-1.Q6) -- two of the four unenforced blue-green invariants
are exactly this comparison: ``ledger-agrees-with-symlinks`` (both links) and
``rollback-restores-the-manifest`` (the ``previous`` link specifically, which a
compensated swap must leave naming the prior release).  The remaining two,
``active-colour-runs-current`` and ``one-live-instance-per-colour``, require a
live router/launchd observation rather than target state at rest and are NOT
claimed here.

``iss_40e46ac7`` (D-7.6) -- the schema snapshot artifact carries no provenance
of its own.  ``ReleaseManager.current_schema_snapshot`` reads it as an
anonymous dict out of ``<current>/VERSION``, THROUGH the ``current`` symlink,
so the only thing binding a snapshot to a release is where the file happens to
sit.  Couple that with the D-1.4 divergence above and the DDL-free preflight
gate diffs against another release's schema without any signal that it did.
The provenance needed to catch this already exists and is simply never checked:
``VERSION`` records ``release_id`` at top level.
:func:`_release_provenance_advisory` compares that recorded id against the
release the symlink actually names and against the ledger, so a snapshot read
from the wrong release is named rather than silently consumed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_RELEASES_ROOT_DEFAULT = Path("~/.ananta/releases")
_STATE_FILENAME = "state.json"
_CURRENT_LINK_NAME = "current"
_PREVIOUS_LINK_NAME = "previous"

_LEDGER_SYMLINK_CHECK_ID = "doctor::release_ledger_symlink_agreement_v1"
_PROVENANCE_CHECK_ID = "doctor::release_schema_snapshot_provenance_v1"

_VERSION_FILENAME = "VERSION"


def collect_blue_green_advisories(
    record: InstanceRecord,
    *,
    releases_root: Path | None = None,
) -> list[JsonValue]:
    """Return report-only blue-green checks over the target's release state."""

    root = _resolve_releases_root(record, releases_root)
    return [
        _ledger_symlink_agreement_advisory(root),
        _release_provenance_advisory(root),
    ]


def _resolve_releases_root(record: InstanceRecord, override: Path | None) -> Path:
    if override is not None:
        return override
    return _RELEASES_ROOT_DEFAULT.expanduser() / record.name


def _ledger_symlink_agreement_advisory(root: Path) -> dict[str, JsonValue]:
    """Compare the durable ledger against the links it is supposed to describe.

    This is the comparator whose absence is ``iss_68f363ae``.  A cleared
    ``in_progress`` marker makes ``reconcile`` a NOOP, so a divergence left by a
    failed compensation is invisible to every existing code path.
    """

    source = str(root / _STATE_FILENAME)
    expected: dict[str, JsonValue] = {
        "ledger_current": None,
        "ledger_previous": None,
    }
    observed: dict[str, JsonValue] = {
        "symlink_current": None,
        "symlink_previous": None,
        "in_progress_phase": None,
    }

    try:
        ledger = _read_ledger(root)
    except (OSError, ValueError) as exc:
        return advisory_unknown(
            _LEDGER_SYMLINK_CHECK_ID,
            "The release ledger could not be read, so ledger/symlink agreement is unknown.",
            expected,
            observed,
            source,
            "release_ledger_unreadable",
            str(exc),
        )

    if ledger is None:
        return advisory_verified(
            _LEDGER_SYMLINK_CHECK_ID,
            "The target carries no release ledger, so there is no swap state to disagree.",
            expected,
            observed,
            source,
        )

    expected = {
        "ledger_current": ledger["current"],
        "ledger_previous": ledger["previous"],
    }
    in_progress = ledger["in_progress"]
    phase = None if in_progress is None else in_progress.get("phase")

    try:
        link_current = _read_link(root / _CURRENT_LINK_NAME)
        link_previous = _read_link(root / _PREVIOUS_LINK_NAME)
    except OSError as exc:
        return advisory_unknown(
            _LEDGER_SYMLINK_CHECK_ID,
            "The release symlinks could not be read, so ledger/symlink agreement is unknown.",
            expected,
            observed,
            source,
            "release_symlink_unreadable",
            str(exc),
        )

    observed = {
        "symlink_current": link_current,
        "symlink_previous": link_previous,
        "in_progress_phase": phase,
    }

    divergent = [
        name
        for name, ledger_value, link_value in (
            (_CURRENT_LINK_NAME, ledger["current"], link_current),
            (_PREVIOUS_LINK_NAME, ledger["previous"], link_previous),
        )
        if ledger_value != link_value
    ]

    if not divergent:
        return advisory_verified(
            _LEDGER_SYMLINK_CHECK_ID,
            "The release ledger and the current/previous symlinks name the same releases.",
            expected,
            observed,
            source,
        )

    if in_progress is not None:
        # A swap is genuinely mid-flight; reconcile still owns this and CAN
        # act, because the marker it keys on is present. Divergence here is
        # expected-in-transit, not the silent-stranding defect.
        return advisory_warn(
            _LEDGER_SYMLINK_CHECK_ID,
            "A swap is in progress and the "
            f"{', '.join(divergent)} link(s) do not yet match the ledger.",
            expected,
            observed,
            source,
            "release_swap_in_progress",
            "Run the deployment reconcile; the in-progress marker still names the terminal target.",
        )

    return advisory_warn(
        _LEDGER_SYMLINK_CHECK_ID,
        f"The release ledger and the {', '.join(divergent)} link(s) disagree with no "
        "in-progress marker, so reconcile cannot repair it.",
        expected,
        observed,
        source,
        "release_ledger_symlink_stranded",
        "A failed compensation cleared the in-progress marker without restoring the "
        "links. Re-point the links at the ledger's recorded releases, or re-run the "
        "deployment, before trusting the active release.",
    )


def _read_ledger(root: Path) -> dict[str, JsonValue] | None:
    state_path = root / _STATE_FILENAME
    if not state_path.is_file():
        return None
    raw = json.loads(state_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"state.json is not a JSON object (got {type(raw).__name__})")
    in_progress = raw.get("in_progress")
    return {
        "current": raw.get("current"),
        "previous": raw.get("previous"),
        "in_progress": in_progress if isinstance(in_progress, dict) else None,
    }


def _read_link(link: Path) -> str | None:
    """Return the basename the link names, or ``None`` when it is absent.

    A dangling symlink still NAMES a release, so it is read with ``readlink``
    semantics rather than a resolve: the ledger records a basename, and a link
    pointing at a deleted release must compare as that basename, not vanish.
    """

    if not link.is_symlink():
        return None
    return Path(link.readlink()).name


def _release_provenance_advisory(root: Path) -> dict[str, JsonValue]:
    """Bind the schema snapshot that would be read to the release it came from.

    ``current_schema_snapshot`` resolves ``<current>/VERSION`` through the
    symlink and lifts ``schema_snapshot`` out of it as an anonymous dict.  The
    snapshot therefore inherits whatever release the link happens to point at.
    ``VERSION`` already records ``release_id``; comparing it to the link's own
    name is the check nobody makes, and it is what turns a wrong-release read
    from silent into named.
    """

    source = str(root / _CURRENT_LINK_NAME / _VERSION_FILENAME)
    expected: dict[str, JsonValue] = {"symlink_current": None}
    observed: dict[str, JsonValue] = {
        "version_release_id": None,
        "schema_snapshot_present": None,
    }

    try:
        link_current = _read_link(root / _CURRENT_LINK_NAME)
    except OSError as exc:
        return advisory_unknown(
            _PROVENANCE_CHECK_ID,
            "The current release link could not be read, so snapshot provenance is unknown.",
            expected,
            observed,
            source,
            "release_symlink_unreadable",
            str(exc),
        )

    if link_current is None:
        return advisory_verified(
            _PROVENANCE_CHECK_ID,
            "The target names no current release, so no schema snapshot would be read.",
            expected,
            observed,
            source,
        )

    expected = {"symlink_current": link_current}
    version_path = root / link_current / _VERSION_FILENAME
    if not version_path.is_file():
        return advisory_warn(
            _PROVENANCE_CHECK_ID,
            f"The current release {link_current!r} has no VERSION file, so any schema "
            "snapshot read from it would be unattributable.",
            expected,
            observed,
            source,
            "release_version_absent",
            "Re-cut the release; a release directory without VERSION cannot prove which "
            "build its schema snapshot describes.",
        )

    try:
        version = json.loads(version_path.read_text())
    except (OSError, ValueError) as exc:
        return advisory_unknown(
            _PROVENANCE_CHECK_ID,
            "The current release VERSION could not be read, so snapshot provenance is unknown.",
            expected,
            observed,
            source,
            "release_version_unreadable",
            str(exc),
        )

    if not isinstance(version, dict):
        return advisory_unknown(
            _PROVENANCE_CHECK_ID,
            "The current release VERSION is not a JSON object, so snapshot provenance is unknown.",
            expected,
            observed,
            source,
            "release_version_unreadable",
            f"VERSION is {type(version).__name__}, not an object",
        )

    recorded_id = version.get("release_id")
    snapshot = version.get("schema_snapshot")
    observed = {
        "version_release_id": recorded_id if isinstance(recorded_id, str) else None,
        "schema_snapshot_present": isinstance(snapshot, dict),
    }

    if not isinstance(recorded_id, str):
        return advisory_warn(
            _PROVENANCE_CHECK_ID,
            f"The current release {link_current!r} records no release_id, so a schema "
            "snapshot read from it cannot be attributed to any build.",
            expected,
            observed,
            source,
            "release_provenance_absent",
            "Re-cut the release so VERSION records release_id; the schema snapshot "
            "carries no provenance of its own and relies on it.",
        )

    if recorded_id != link_current:
        return advisory_warn(
            _PROVENANCE_CHECK_ID,
            f"The current link names {link_current!r} but its VERSION records "
            f"release_id {recorded_id!r}, so the schema snapshot that would be read "
            "belongs to a different release.",
            expected,
            observed,
            source,
            "release_provenance_mismatch",
            "Do not trust a schema diff taken through this link. Re-point current at "
            "the release its VERSION claims, or re-cut the release.",
        )

    return advisory_verified(
        _PROVENANCE_CHECK_ID,
        f"The current release {link_current!r} records a matching release_id, so a "
        "schema snapshot read through the link is attributable to it.",
        expected,
        observed,
        source,
    )
