"""The credential copy the offline knowledge tools read is checked for staleness.

Detection coverage for ``iss_dd451098`` (D-2.F2).

``credential_seed.seed_db_password`` rotates the live role and exactly two
Keychain copies -- ``db_password`` and the pgvector account -- and never the third:
the legacy RAW ``password`` account that
``default_knowledge_plugin/tools/_pg_credentials.py`` reads, which is the offline
fallback ``CLAUDE.md`` prescribes when live retrieval is down.  That file's own
docstring states the contract rotation breaks: "Both accounts hold the same
password."

The sharpest assertion here is :func:`_assert_younger_but_unrotated_is_green`.
The obvious predicate -- "the live copy is newer than the offline copy" -- warns on
every healthy solet, because the live ``db_password`` account was introduced
ADDITIVELY by G3 after the raw account already existed.  Measured on this machine
while writing the check: live ``cdat``/``mdat`` both 20260628220221Z, offline both
20260623203638Z -- live is five days younger and nothing is wrong.  The real
signal is a ROTATION (``mdat > cdat``) that postdates the offline write, and that
test is what stops the naive version from being shipped.

Each red asserts its NAMED ``reason_code``; the shapes produce DIFFERENT names:

* rotation postdating the offline write -> offline_credential_copy_stale
* the offline account missing entirely  -> offline_credential_copy_absent
* the live account missing              -> live_credential_copy_absent
* attributes unreadable                 -> keychain_attributes_unreadable
* dates absent from the attributes      -> keychain_dates_missing

Offline: constructed attribute mappings only.  Nothing here runs ``security``,
touches the Keychain, or reads a secret value.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_credential_copy_census import (  # noqa: E402
    _parse_attributes,
    collect_credential_copy_advisories,
)

_ID = "doctor::offline_credential_copy_current_v1"

_LIVE = "db_password"
_OFFLINE = "password"

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


class _Record:
    name = "census"
    target = "/nonexistent/census-target"


def _advisory(
    items: dict[str, dict[str, str] | None] | Exception,
) -> dict[str, object]:
    def _reader(_service: str, account: str) -> dict[str, str] | None:
        if isinstance(items, Exception):
            raise items
        return items.get(account)

    results = collect_credential_copy_advisories(_Record(), attribute_reader=_reader)
    _check(len(results) == 1, f"the census stopped emitting exactly one check: {len(results)}")
    entry = results[0]
    assert isinstance(entry, dict)
    _check(str(entry["check_id"]) == _ID, f"unexpected check id: {entry['check_id']}")
    return entry


def _item(created: str, modified: str) -> dict[str, str]:
    return {"cdat": created, "mdat": modified}


def _assert_rotation_after_offline_write_is_named() -> None:
    """The residue: the live copy was rotated and the offline copy was left behind."""

    advisory = _advisory(
        {
            _LIVE: _item("20260628220221Z", "20260901120000Z"),
            _OFFLINE: _item("20260623203638Z", "20260623203638Z"),
        }
    )
    _check(
        advisory["reason_code"] == "offline_credential_copy_stale",
        f"a stranded offline copy was not named: {advisory['reason_code']}",
    )
    _check(
        advisory["blocking"] is False,
        "a credential copy advisory must never block a doctor result",
    )


def _assert_younger_but_unrotated_is_green() -> None:
    """The false positive the naive predicate would ship, using REAL observed dates.

    The live account is five days younger than the offline one and that is the
    normal additive-introduction state: neither has been modified since creation.
    A check that warned here would warn on every healthy solet.
    """

    advisory = _advisory(
        {
            _LIVE: _item("20260628220221Z", "20260628220221Z"),
            _OFFLINE: _item("20260623203638Z", "20260623203638Z"),
        }
    )
    _check(
        advisory["status"] == "verified",
        f"an unrotated younger live account was warned about: {advisory['reason_code']}",
    )


def _assert_offline_newer_is_green() -> None:
    advisory = _advisory(
        {
            _LIVE: _item("20260628220221Z", "20260628220221Z"),
            _OFFLINE: _item("20260623203638Z", "20260901120000Z"),
        }
    )
    _check(
        advisory["status"] == "verified",
        f"an offline copy rewritten last was warned about: {advisory['reason_code']}",
    )


def _assert_missing_offline_account_is_named() -> None:
    advisory = _advisory({_LIVE: _item("20260628220221Z", "20260628220221Z"), _OFFLINE: None})
    _check(
        advisory["reason_code"] == "offline_credential_copy_absent",
        f"an absent offline account was not named: {advisory['reason_code']}",
    )


def _assert_missing_live_account_is_unknown() -> None:
    advisory = _advisory({_LIVE: None, _OFFLINE: _item("20260623203638Z", "20260623203638Z")})
    _check(
        advisory["status"] == "unknown",
        f"an absent live account did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "live_credential_copy_absent",
        f"an absent live account was not named: {advisory['reason_code']}",
    )


def _assert_unreadable_keychain_is_unknown_not_green() -> None:
    advisory = _advisory(OSError("keychain attribute read failed"))
    _check(
        advisory["status"] == "unknown",
        f"an unreadable keychain did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "keychain_attributes_unreadable",
        f"an unreadable keychain was not named: {advisory['reason_code']}",
    )


def _assert_missing_dates_are_unknown_not_green() -> None:
    advisory = _advisory({_LIVE: {"svce": "x"}, _OFFLINE: {"svce": "x"}})
    _check(
        advisory["status"] == "unknown",
        f"missing dates did not read as unknown: {advisory['status']}",
    )
    _check(
        advisory["reason_code"] == "keychain_dates_missing",
        f"missing dates were not named: {advisory['reason_code']}",
    )


def _assert_parser_reads_real_security_output() -> None:
    """Pin the parser against the exact shape ``security`` prints.

    The FORMAT is captured verbatim from a real attribute read -- the quoting,
    the ``<blob>`` and ``<timedate>`` type tags, the hex-then-quoted value order
    that only timedate lines use, and the trailing ``\\000``. A format change
    must red here rather than degrade every date comparison into
    keychain_dates_missing.

    Only the solet identity in the service name is substituted, for a fixture
    token: the shipped-doc gate's reserved-identity scan refuses a real origin
    identity in shipped bytes, and the parser does not read the service value,
    so nothing this test pins depends on what that name is.
    """

    sample = (
        '    "acct"<blob>="db_password"\n'
        '    "cdat"<timedate>=0x32303236303632383232303232315A00  "20260628220221Z\\000"\n'
        '    "mdat"<timedate>=0x32303236303632383232303232315A00  "20260628220221Z\\000"\n'
        '    "svce"<blob>="census.postgres_state_management_plugin"\n'
    )
    attributes = _parse_attributes(sample)
    _check(
        attributes.get("acct") == "db_password",
        f"the account name was not parsed: {attributes.get('acct')}",
    )
    _check(
        attributes.get("svce") == "census.postgres_state_management_plugin",
        f"the service was not parsed: {attributes.get('svce')}",
    )
    _check(
        attributes.get("mdat") == "20260628220221Z",
        f"the modification date was not parsed: {attributes.get('mdat')}",
    )
    _check(
        attributes.get("cdat") == "20260628220221Z",
        f"the creation date was not parsed: {attributes.get('cdat')}",
    )


def main() -> int:
    _assert_rotation_after_offline_write_is_named()
    _assert_younger_but_unrotated_is_green()
    _assert_offline_newer_is_green()
    _assert_missing_offline_account_is_named()
    _assert_missing_live_account_is_unknown()
    _assert_unreadable_keychain_is_unknown_not_green()
    _assert_missing_dates_are_unknown_not_green()
    _assert_parser_reads_real_security_output()
    print(f"doctor_credential_copy_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
