#!/usr/bin/env python3
"""D-3-structural: the shared allowlist schema enforces owner/reason/expires.

A loader construction with a missing mandatory field must fail, not
silently default (iss_23fa51b5, iss_9b0ad3d5). Offline, no fixture files
outside a temporary directory.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quality_gates.allowlist_schema import (  # noqa: E402
    AllowlistEntry,
    AllowlistEntryError,
    active_keys,
    expired_entries,
    load_allowlist_entries,
    parse_allowlist_line,
)


def _assert_raises(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except AllowlistEntryError:
        return
    raise AssertionError(f"expected AllowlistEntryError from {fn.__name__}{args!r}")


def _check_missing_fields() -> None:
    source = Path("fixture.txt")
    _assert_raises(
        parse_allowlist_line,
        "some/path.py  # reason: tracked debt expires: 2027-01-01",
        source=source,
        lineno=1,
    )
    _assert_raises(
        parse_allowlist_line,
        "some/path.py  # owner: team-x expires: 2027-01-01",
        source=source,
        lineno=2,
    )
    _assert_raises(
        parse_allowlist_line,
        "some/path.py  # owner: team-x reason: tracked debt",
        source=source,
        lineno=3,
    )
    _assert_raises(
        parse_allowlist_line,
        "some/path.py  # owner:  reason: tracked debt expires: 2027-01-01",
        source=source,
        lineno=4,
    )


def _check_no_tag_at_all() -> None:
    _assert_raises(parse_allowlist_line, "some/path.py", source=Path("fixture.txt"), lineno=1)
    _assert_raises(parse_allowlist_line, "some/path.py  # just a note", source=Path("fixture.txt"), lineno=1)


def _check_malformed_date() -> None:
    _assert_raises(
        parse_allowlist_line,
        "some/path.py  # owner: team-x reason: tracked debt expires: not-a-date",
        source=Path("fixture.txt"),
        lineno=1,
    )
    _assert_raises(
        parse_allowlist_line,
        "some/path.py  # owner: team-x reason: tracked debt expires: 2027-13-40",
        source=Path("fixture.txt"),
        lineno=1,
    )


def _check_blank_and_comment_lines_are_none() -> None:
    assert parse_allowlist_line("", source=Path("fixture.txt"), lineno=1) is None
    assert parse_allowlist_line("   ", source=Path("fixture.txt"), lineno=1) is None
    assert parse_allowlist_line("# a free-text provenance note", source=Path("fixture.txt"), lineno=1) is None


def _check_valid_entry_round_trips() -> None:
    entry = parse_allowlist_line(
        "plugins/foo::Bar  # owner: team-x reason: tracked debt, see iss_1 expires: 2027-06-30",
        source=Path("fixture.txt"),
        lineno=7,
    )
    assert entry == AllowlistEntry(
        key="plugins/foo::Bar",
        owner="team-x",
        reason="tracked debt, see iss_1",
        expires=date(2027, 6, 30),
        source=Path("fixture.txt"),
        lineno=7,
    )
    assert not entry.is_expired


def _check_field_order_is_free() -> None:
    entry = parse_allowlist_line(
        "k  # expires: 2027-01-01 owner: team-x reason: reordered fields still parse",
        source=Path("fixture.txt"),
        lineno=1,
    )
    assert entry is not None
    assert entry.owner == "team-x"
    assert entry.expires == date(2027, 1, 1)
    assert entry.reason == "reordered fields still parse"


def _check_active_keys_and_expired_entries() -> None:
    past = AllowlistEntry("stale-key", "team-x", "lapsed debt", date(2020, 1, 1), Path("f.txt"), 1)
    future = AllowlistEntry("fresh-key", "team-x", "live debt", date(2099, 1, 1), Path("f.txt"), 2)
    entries = (past, future)
    assert active_keys(entries, today=date(2026, 9, 20)) == frozenset({"fresh-key"})
    assert expired_entries(entries, today=date(2026, 9, 20)) == (past,)
    assert past.is_expired
    assert not future.is_expired


def _check_loader_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        fixture = Path(temp_dir) / "sample_allowlist.txt"
        fixture.write_text(
            "# a comment line, ignored\n"
            "\n"
            "path/one.py  # owner: team-x reason: tracked debt A expires: 2099-01-01\n"
            "path/two.py::ClassName  # owner: team-y reason: tracked debt B expires: 2099-01-01\n",
            encoding="utf-8",
        )
        entries = load_allowlist_entries(fixture)
        assert len(entries) == 2
        assert active_keys(entries, today=date(2026, 9, 20)) == {"path/one.py", "path/two.py::ClassName"}

        missing = Path(temp_dir) / "does_not_exist.txt"
        try:
            load_allowlist_entries(missing)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("expected FileNotFoundError for a missing allowlist file")

        malformed = Path(temp_dir) / "malformed_allowlist.txt"
        malformed.write_text("path/three.py  # no tag at all\n", encoding="utf-8")
        _assert_raises(load_allowlist_entries, malformed)


def main() -> int:
    _check_missing_fields()
    _check_no_tag_at_all()
    _check_malformed_date()
    _check_blank_and_comment_lines_are_none()
    _check_valid_entry_round_trips()
    _check_field_order_is_free()
    _check_active_keys_and_expired_entries()
    _check_loader_end_to_end()
    print("allowlist_schema_smoke: 8 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
