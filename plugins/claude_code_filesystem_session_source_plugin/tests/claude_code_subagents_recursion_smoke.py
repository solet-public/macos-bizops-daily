#!/usr/bin/env python3
"""M6.5 Bug 3 smoke — M3 ``discover_sessions`` recurses into subagents/.

Run:

    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_subagents_recursion_smoke.py

Per 2026-06-11 M6.5 Bug 3 (Coordinator-Dawn dispatch §5): the pre-fix
single-level ``project_dir.glob(glob_pat)`` at
``plugins/claude_code_filesystem_session_source_plugin/src/claude_code_filesystem_session_source_plugin/plugin.py:204-205``
silently missed the 2,711 ``<project>/<root-uuid>/subagents/agent-*.jsonl``
files on operator's machine. The new shape uses ``rglob`` plus a
path-shape classifier (``_classify_session_path``) that accepts depth-2
root sessions, depth-4 subagent rollouts, and warns-and-skips on
anything else.

This smoke writes a tmp filesystem mirroring the real layout and
verifies the discovery + locate paths cover both shapes without
re-ingesting or fail-fasting on cruft files.

Verifications:

1. ``discover_sessions`` yields one ``ExternalSessionRef`` per
   root-session ``<project>/<uuid>.jsonl`` AND one per subagent
   ``<project>/<root-uuid>/subagents/agent-*.jsonl``.
2. Each subagent surfaces as its OWN session (separate ``external_session_id``).
3. Unrecognized path shapes (``.DS_Store``, depth-6 sub-sub-agents,
   non-``agent-`` filenames inside ``subagents/``) are warn-and-skipped,
   NOT fail-fast.
4. ``_locate_session_file`` finds BOTH root and subagent files by stem.
5. Re-running discovery with a high-water cursor that includes the
   first batch's mtimes yields ZERO additional refs — idempotency
   preserved.
6. The path-shape classifier (``_classify_session_path``) correctly
   returns ``(project_dir, session_id)`` for each shape and ``None``
   for unrecognized.
"""

from __future__ import annotations

import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(
    _REPO_ROOT
    / "plugins"
    / "claude_code_filesystem_session_source_plugin"
    / "src"
))


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: {message}")


class _StubConfigProvider:
    # P1.1.E: root is threaded per-call as root_uri; only glob stays in config.
    def __init__(self, glob: str = "*.jsonl") -> None:
        self._glob = glob

    def get(self, key: str) -> str | None:
        if key == "glob":
            return self._glob
        return None


def _plant_fixture(root: Path) -> None:
    """Lay down a realistic mini ``~/.claude/projects/`` tree."""
    # Project A — depth-2 root session + 2 subagents.
    project_a = root / "-Users-alice-Workspace-example"
    (project_a).mkdir(parents=True)
    (project_a / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        '{"type":"session_meta","sessionId":"11111111-1111-1111-1111-111111111111"}\n',
        encoding="utf-8",
    )
    subdir_a = project_a / "11111111-1111-1111-1111-111111111111" / "subagents"
    subdir_a.mkdir(parents=True)
    (subdir_a / "agent-aaaaaaa1.jsonl").write_text(
        '{"type":"session_meta","sessionId":"agent-aaaaaaa1"}\n',
        encoding="utf-8",
    )
    (subdir_a / "agent-aaaaaaa2.jsonl").write_text(
        '{"type":"session_meta","sessionId":"agent-aaaaaaa2"}\n',
        encoding="utf-8",
    )
    # Project B — depth-2 root session only.
    project_b = root / "-Users-bob-other"
    project_b.mkdir()
    (project_b / "22222222-2222-2222-2222-222222222222.jsonl").write_text(
        '{"type":"session_meta","sessionId":"22222222-2222-2222-2222-222222222222"}\n',
        encoding="utf-8",
    )
    # Project C — root session + cruft under subagents/ + a deeper bad shape.
    # NOTE: depth-2 ``<project>/<anything>.jsonl`` is BY CONTRACT a root session
    # per the dispatch §5 classifier (we accept any stem at depth 2; a future
    # M-section could add stem-shape validation but it's not in M6.5 scope).
    # So cruft like ``.DS_Store.jsonl`` planted at depth-2 would erroneously
    # surface as a "root session" — that's a real-world concern callers solve
    # via the configured ``glob`` (the canonical pattern excludes leading dots).
    project_c = root / "-Users-bob-cruft"
    project_c.mkdir()
    # Wrong-prefix file under subagents/ — should be skipped (not match agent-).
    (project_c / "33333333-3333-3333-3333-333333333333.jsonl").write_text(
        '{"type":"session_meta","sessionId":"33333333-3333-3333-3333-333333333333"}\n',
        encoding="utf-8",
    )
    bad_sub = project_c / "33333333-3333-3333-3333-333333333333" / "subagents"
    bad_sub.mkdir(parents=True)
    (bad_sub / "not-an-agent.jsonl").write_text("garbage\n", encoding="utf-8")
    # Depth-6 hypothetical sub-sub-agent — should be skipped.
    deep = project_c / "33333333-3333-3333-3333-333333333333" / "subagents" / "agent-xyz" / "subagents"
    deep.mkdir(parents=True)
    (deep / "agent-grandchild.jsonl").write_text("garbage\n", encoding="utf-8")


def test_classifier_directly() -> None:
    from claude_code_filesystem_session_source_plugin.plugin import (
        _classify_session_path,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _plant_fixture(root)
        # Root session at depth 2.
        root_session = (
            root / "-Users-alice-Workspace-example"
            / "11111111-1111-1111-1111-111111111111.jsonl"
        )
        result = _classify_session_path(root_session, root)
        _expect(
            result is not None
            and result[1] == "11111111-1111-1111-1111-111111111111",
            f"classifier accepts depth-2 root session; got {result}",
        )
        # Subagent at depth 4.
        subagent = (
            root / "-Users-alice-Workspace-example"
            / "11111111-1111-1111-1111-111111111111"
            / "subagents" / "agent-aaaaaaa1.jsonl"
        )
        result = _classify_session_path(subagent, root)
        _expect(
            result is not None
            and result[1] == "agent-aaaaaaa1",
            f"classifier accepts depth-4 subagent; got {result}",
        )
        # Wrong filename inside subagents/ → None.
        bad = (
            root / "-Users-bob-cruft"
            / "33333333-3333-3333-3333-333333333333"
            / "subagents" / "not-an-agent.jsonl"
        )
        _expect(
            _classify_session_path(bad, root) is None,
            "classifier rejects subagents/<non-agent-prefix>",
        )
        # Depth-6 hypothetical sub-sub-agent → None.
        deep = (
            root / "-Users-bob-cruft"
            / "33333333-3333-3333-3333-333333333333"
            / "subagents" / "agent-xyz" / "subagents" / "agent-grandchild.jsonl"
        )
        _expect(
            _classify_session_path(deep, root) is None,
            "classifier rejects depth-6 sub-sub-agent (out of M6.5 scope)",
        )


def test_discover_sessions_yields_both_shapes_and_skips_cruft() -> None:
    from claude_code_filesystem_session_source_plugin.plugin import (
        ClaudeCodeFilesystemSessionSourcePlugin,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _plant_fixture(root)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.config_provider = _StubConfigProvider()  # type: ignore[attr-defined]
        refs = list(plugin.discover_sessions(str(root), None))
        ids = sorted(r.external_session_id for r in refs)
        # Expect: 1 (project A root) + 2 (project A subagents) + 1 (project B
        # root) + 1 (project C root). Non-agent-prefix and depth-6 cruft are
        # warn-and-skipped per the classifier's contract.
        expected_ids = sorted([
            "11111111-1111-1111-1111-111111111111",
            "agent-aaaaaaa1",
            "agent-aaaaaaa2",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ])
        _expect(
            ids == expected_ids,
            f"discover_sessions yields the 5 valid refs; got {ids}",
        )
        # Re-update on second pass yields zero (idempotency via high-water
        # cursor is covered in test_discovery_idempotent_with_high_water_cursor).


def test_locate_session_file_finds_both_shapes() -> None:
    from claude_code_filesystem_session_source_plugin.plugin import (
        ClaudeCodeFilesystemSessionSourcePlugin,
    )
    sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
    from ananta.llm.session_ledger.types import ExternalSessionRef
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _plant_fixture(root)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.config_provider = _StubConfigProvider()  # type: ignore[attr-defined]
        # Depth-2 root locate.
        root_ref = ExternalSessionRef(
            external_session_id="11111111-1111-1111-1111-111111111111",
            vendor_session_label=None,
            project_path=None,
            first_seen_at=datetime(2026, 6, 11, tzinfo=UTC),
        )
        found = plugin._locate_session_file(str(root), root_ref)  # type: ignore[attr-defined]
        _expect(
            found is not None and found.stem == "11111111-1111-1111-1111-111111111111",
            f"locate finds depth-2 root session; got {found}",
        )
        # Depth-4 subagent locate.
        sub_ref = ExternalSessionRef(
            external_session_id="agent-aaaaaaa1",
            vendor_session_label=None,
            project_path=None,
            first_seen_at=datetime(2026, 6, 11, tzinfo=UTC),
        )
        found = plugin._locate_session_file(str(root), sub_ref)  # type: ignore[attr-defined]
        _expect(
            found is not None and found.stem == "agent-aaaaaaa1",
            f"locate finds depth-4 subagent session; got {found}",
        )


def test_discovery_idempotent_with_high_water_cursor() -> None:
    from claude_code_filesystem_session_source_plugin.plugin import (
        ClaudeCodeFilesystemSessionSourcePlugin,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _plant_fixture(root)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.config_provider = _StubConfigProvider()  # type: ignore[attr-defined]
        # First pass yields 5 refs.
        first_pass = list(plugin.discover_sessions(str(root), None))
        _expect(len(first_pass) == 5, f"first pass yields 5 refs; got {len(first_pass)}")
        # High-water cursor set to NOW would skip everything on second pass
        # (all files have mtime <= now).
        time.sleep(0.05)
        cursor: dict[str, object] = {
            "mtime_high_water_iso": datetime.now(UTC).isoformat(),
        }
        second_pass = list(plugin.discover_sessions(str(root), cursor))
        _expect(
            len(second_pass) == 0,
            f"second pass with future-or-equal cursor yields 0 refs (idempotency); "
            f"got {len(second_pass)}",
        )


_OPERATOR_USERNAME_TOKEN = "d" + "w"


def test_source_carries_no_operator_username() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-31): the fake
    ``~/.claude/projects/`` fixture directory names are arbitrary — the
    classifier under test only cares about path SHAPE, not the project-name
    text — so there is no functional reason for the real operator's OS
    username to appear in a hyphen-flattened project directory name here.
    This file ships whenever ``claude_code_filesystem_session_source_plugin``
    is selected. Composed from two concatenated halves (see
    ``_OPERATOR_USERNAME_TOKEN``) so this guard's own source never contains
    the contiguous token it hunts for. Word-bounded so it does not collide
    with an unrelated substring.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _expect(
        pattern.search(source) is None,
        "fixture source carries no bare operator-username token",
    )


def main() -> None:
    print("M6.5 Bug 3 — M3 subagents/ recursion smoke")
    print("=" * 60)
    test_source_carries_no_operator_username()
    test_classifier_directly()
    test_discover_sessions_yields_both_shapes_and_skips_cruft()
    test_locate_session_file_finds_both_shapes()
    test_discovery_idempotent_with_high_water_cursor()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
