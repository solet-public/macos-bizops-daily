"""Pure probe parsers and the candidate/local collision matrix for Step-4 updates."""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.update_topology import (  # noqa: E402
    CollisionRow,
    collision_rows,
    config_flag,
    origin_reason,
    parse_git_config_entries,
    parse_local_entries,
    parse_transition_paths,
    unsafe_config_keys,
)


def _assert_config_surface() -> None:
    raw = (
        b"global\0credential.helper\nosxkeychain\0"
        b"local\0core.fsmonitor\n/tmp/hook.sh\0"
        b"local\0remote.origin.url\nhttps://example.invalid/seed.git\0"
        b"local\0core.ignorecase\ntrue\0"
        b"worktree\0filter.lfs.smudge\ngit-lfs smudge\0"
        b"local\0remote.origin.fetch\n+refs/heads/*:refs/remotes/origin/*\0"
        b"local\0Diff.Custom.command\n/bin/sh\0"
    )
    entries = parse_git_config_entries(raw)
    assert len(entries) == 7 and entries[1] == ("local", "core.fsmonitor", "/tmp/hook.sh")
    assert unsafe_config_keys(entries) == ("Diff.Custom.command", "core.fsmonitor", "filter.lfs.smudge")
    assert config_flag(entries, "core.ignorecase") and not config_flag(entries, "core.bare")
    assert unsafe_config_keys(parse_git_config_entries(b"local\0remote.origin.url\nx\0local\0branch.main.merge\nrefs/heads/main\0")) == ()
    for malformed in (b"local\0core.bare", b"local\0corebare\0", b"local\0"):
        try:
            parse_git_config_entries(malformed)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed config listing accepted")


def _assert_parsers() -> None:
    assert parse_transition_paths(b"A\0d/x\0M\0f\0D\0g\0T\0s\0") == (("A", "d/x"), ("M", "f"), ("D", "g"), ("T", "s"))
    assert parse_transition_paths(b"") == ()
    for malformed in (b"R100\0a\0b\0", b"A\0d/x", b"A\0\0"):
        try:
            parse_transition_paths(malformed)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed transition listing accepted")
    rows = parse_local_entries(b"?? u\0!! ig/\0 M t\0R  new\0old\0!! g\0")
    assert rows == (("untracked", "??", "u"), ("ignored", "!!", "ig/"), ("tracked", " M", "t"), ("tracked", "R ", "new"), ("ignored", "!!", "g"))
    try:
        parse_local_entries(b"??u\0")
    except ValueError:
        pass
    else:
        raise AssertionError("malformed status record accepted")


def _assert_collisions() -> None:
    transition = (
        ("A", "cfg/local.json"),
        ("A", "docs/x"),
        ("T", "link"),
        ("M", "README.md"),
        ("A", "a/b/c"),
        ("A", "z"),
        ("A", "Foo.txt"),
        ("A", "café.txt"),
        ("A", "other.txt"),
        ("D", "gone"),
    )
    local = (
        ("ignored", "!!", "cfg/local.json"),
        ("untracked", "??", "docs/"),
        ("untracked", "??", "link"),
        ("untracked", "??", "README.md"),
        ("untracked", "??", "a/b"),
        ("ignored", "!!", "z/q"),
        ("untracked", "??", "foo.txt"),
        ("untracked", "??", unicodedata.normalize("NFD", "café.txt")),
        ("untracked", "??", "gone"),
        ("tracked", " M", "other.txt"),
    )
    sensitive = collision_rows(transition, local, case_insensitive=False)
    assert sensitive == (
        CollisionRow("file_directory_prefix_collision", "a/b/c"),
        CollisionRow("file_directory_prefix_collision", "z"),
        CollisionRow("untracked_destination_collision", "cfg/local.json"),
        CollisionRow("untracked_destination_collision", "docs/x"),
        CollisionRow("untracked_destination_collision", "link"),
    ), sensitive
    insensitive = collision_rows(transition, local, case_insensitive=True)
    assert set(insensitive) - set(sensitive) == {
        CollisionRow("casefold_collision", "Foo.txt"),
        CollisionRow("casefold_collision", "café.txt"),
    }, insensitive
    assert collision_rows(transition, (), case_insensitive=True) == ()
    assert collision_rows((("A", "x"),), (("untracked", "??", "y"),), case_insensitive=True) == ()


def _assert_origin() -> None:
    canonical = "https://github.com/example/seed.git"
    assert origin_reason((canonical,), canonical) is None
    for origins in ((), ("https://example.invalid/other.git",), (canonical, "https://example.invalid/other.git")):
        assert origin_reason(origins, canonical) == "origin_unapproved"


def main() -> int:
    _assert_config_surface()
    _assert_parsers()
    _assert_collisions()
    _assert_origin()
    print("update_topology_collision_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
