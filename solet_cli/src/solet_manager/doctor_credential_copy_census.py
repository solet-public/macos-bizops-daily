"""Passive advisory over the credential copy the offline knowledge tools read.

Detection coverage for the credential-rotation row of the 2026-09-02
doctor-detection audit.  REPORT-ONLY, for the reason the vintage census gives:
Keychain contents are host state, not part of the target's pinned completion
contract, so a divergence must be named loudly without retroactively refusing an
installation whose own contract verified.

Row detected
------------
``iss_dd451098`` (D-2.F2) -- ``credential_seed.seed_db_password`` rotates the live
role and exactly TWO Keychain copies: ``_store_and_verify`` runs for
``postgres_state_management_plugin``/``db_password`` (the RFC-2397 live read path)
and ``pgvector_service_plugin``/``password``, then calls ``alter_fn(pw)``.  There
is a THIRD copy it never touches: the legacy RAW account at service
``<solet>.postgres_state_management_plugin``, account ``password``.

That third copy is not vestigial.  It is the documented read path for the offline
knowledge tools -- ``plugins/default_knowledge_plugin/tools/_pg_credentials.py``
reads exactly it, and those tools are what ``CLAUDE.md`` prescribes as the
degraded fallback when live retrieval is down.  ``_pg_credentials`` states the
contract in its own docstring: "Both accounts hold the same password."  Rotation
breaks that written contract, and the break is discoverable only when live
retrieval is ALREADY down -- precisely when the fallback is the thing being
reached for.

Why the predicate is not "the live copy is newer"
-------------------------------------------------
That naive comparison warns on every healthy solet, and it was measured doing so
before this check was written.  The live ``db_password`` account was introduced
additively by G3 (2026-06-28) AFTER the legacy raw account already existed
(2026-06-23), so a newer modification date is the NORMAL state and proves nothing
about the values.

What actually indicates drift is a ROTATION of the live copy after the offline
copy was last written.  A Keychain item that has never been modified since it was
created reports ``mdat == cdat``; one that has been rewritten reports
``mdat > cdat``.  So the warn condition is both: the live copy was modified after
its own creation, AND that modification postdates the offline copy's last write.
A live copy that is merely younger, never rotated, is graded verified and says so.

What this module does NOT claim
-------------------------------
It never reads, compares, or emits a secret VALUE.  Keychain attribute reads
(``security find-generic-password`` without ``-w``) do not touch the secret and do
not raise the SecurityAgent ACL prompt that a value read would -- a doctor check
that hung a headless run on a GUI prompt, or that quoted a credential into a
result document, would be worse than the defect it reports.  The cost of that
choice is stated plainly: identical timestamps are consistent with, but do not
prove, identical values.  This check detects the rotation that strands the offline
copy; it cannot certify that two copies match.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_STATE_SERVICE_SUFFIX = "postgres_state_management_plugin"
_LIVE_ACCOUNT = "db_password"
_OFFLINE_ACCOUNT = "password"

_COPY_CHECK_ID = "doctor::offline_credential_copy_current_v1"

_QUOTED_FIELD = re.compile(r'"([^"]*)"')

_PROBE_TIMEOUT_S = 10

# (service, account) -> attribute mapping, or None when no such item exists.
AttributeReader = Callable[[str, str], dict[str, str] | None]


def collect_credential_copy_advisories(
    record: InstanceRecord,
    *,
    attribute_reader: AttributeReader | None = None,
) -> list[JsonValue]:
    """Return the report-only check for the offline tools' credential copy."""

    reader = _read_keychain_attributes if attribute_reader is None else attribute_reader
    return [_offline_copy_advisory(record, reader)]


def _read_keychain_attributes(service: str, account: str) -> dict[str, str] | None:
    """Return a Keychain item's attributes WITHOUT reading its secret value.

    Deliberately omits ``-w``: attribute reads do not touch the secret and do not
    trigger the SecurityAgent ACL prompt that hangs a headless run.
    """

    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"keychain attribute read failed: {exc}") from exc
    if completed.returncode != 0:
        return None
    return _parse_attributes(completed.stderr + completed.stdout)


def _parse_attributes(text: str) -> dict[str, str]:
    """Extract the quoted attribute values ``security`` prints, e.g. ``mdat``.

    Two line shapes have to be handled, and conflating them silently loses every
    date.  A blob prints its value straight after the type::

        "acct"<blob>="db_password"

    while a timedate prints a hex encoding FIRST and the readable value last::

        "mdat"<timedate>=0x32...5A00  "20260628220221Z\\000"

    So the name is the first quoted field and the value is the LAST one, rather
    than whatever follows ``="``.
    """

    attributes: dict[str, str] = {}
    for line in text.splitlines():
        quoted = _QUOTED_FIELD.findall(line)
        if len(quoted) < 2:
            continue
        name = quoted[0]
        value = quoted[-1]
        if name:
            attributes[name] = value.replace("\\000", "").strip()
    return attributes


def _offline_copy_advisory(record: InstanceRecord, reader: AttributeReader) -> dict[str, JsonValue]:
    service = f"{record.name}.{_STATE_SERVICE_SUFFIX}"
    expected: dict[str, JsonValue] = {
        "offline_account_present": True,
        "live_rotation_after_offline_write": False,
        "service": service,
    }
    observed: dict[str, JsonValue] = {
        "live_modified_at": None,
        "live_created_at": None,
        "offline_modified_at": None,
    }
    source = f"security find-generic-password -s {service} (attributes only, no secret read)"

    try:
        live = reader(service, _LIVE_ACCOUNT)
        offline = reader(service, _OFFLINE_ACCOUNT)
    except OSError as exc:
        return advisory_unknown(
            _COPY_CHECK_ID,
            "The Keychain attributes could not be read, so the offline copy's currency is unknown.",
            expected,
            observed,
            source,
            "keychain_attributes_unreadable",
            str(exc),
        )

    if offline is None:
        return advisory_warn(
            _COPY_CHECK_ID,
            "The raw Keychain account the offline knowledge tools read does not exist, so "
            "the documented degraded fallback cannot authenticate at all.",
            expected,
            observed,
            source,
            "offline_credential_copy_absent",
            "Recreate the raw account at this service with the CURRENT password (verbatim "
            "keyring.set_password, not the RFC-2397 form); the offline tools read it "
            "directly and fail loud without it.",
        )

    if live is None:
        return advisory_unknown(
            _COPY_CHECK_ID,
            "The live credential account is absent, so there is no rotation to compare the "
            "offline copy against.",
            expected,
            observed,
            source,
            "live_credential_copy_absent",
            "The live db_password account does not exist; this check grades the offline "
            "copy relative to it and cannot do so.",
        )

    return _grade_currency(live, offline, expected, observed, source)


def _grade_currency(
    live: dict[str, str],
    offline: dict[str, str],
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    """Grade the offline copy against a ROTATION of the live copy, not its age."""

    live_modified = live.get("mdat", "")
    live_created = live.get("cdat", "")
    offline_modified = offline.get("mdat", "")
    observed["live_modified_at"] = live_modified or None
    observed["live_created_at"] = live_created or None
    observed["offline_modified_at"] = offline_modified or None

    if not (live_modified and live_created and offline_modified):
        return advisory_unknown(
            _COPY_CHECK_ID,
            "A Keychain item did not report the dates this check compares.",
            expected,
            observed,
            source,
            "keychain_dates_missing",
            "Without both dates on both accounts the rotation cannot be ordered; an "
            "ungradable pair is not a passing one.",
        )

    live_was_rotated = live_modified > live_created
    rotated_after_offline = live_modified > offline_modified

    if live_was_rotated and rotated_after_offline:
        return advisory_warn(
            _COPY_CHECK_ID,
            "The live credential was rotated after the offline copy was last written, so "
            "the copy the offline knowledge tools read is stale.",
            expected,
            observed,
            source,
            "offline_credential_copy_stale",
            "Rewrite the raw account with the current password. credential_seed rotates the "
            "live and pgvector copies and the role, and never this one, so the documented "
            "offline fallback breaks silently -- and is discovered only when live retrieval "
            "is already down and the fallback is what you reached for.",
        )

    if rotated_after_offline:
        return advisory_verified(
            _COPY_CHECK_ID,
            "The live account is younger than the offline copy but has never been modified "
            "since it was created, which is the expected additive-introduction state, not a "
            "rotation that stranded the copy.",
            expected,
            observed,
            source,
        )

    return advisory_verified(
        _COPY_CHECK_ID,
        "No rotation of the live credential postdates the offline copy's last write.",
        expected,
        observed,
        source,
    )
