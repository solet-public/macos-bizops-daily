#!/usr/bin/env python3
"""Smoke pin for the 2026-05-31 ledger-per-peer-distinction implementation.

Watchdog tag: ``dispatch:in_flight:claude_code_vendor_session_metadata_promotion``.
Architect ruling: ``workbench/2026-05-31_ledger_per_peer_distinction_ruling.md``.

This smoke pins three independent pieces of the per-peer-distinction slice:

1. **claude_code vendor** — :func:`read_session_metadata` extracts
   ``agent-name`` / ``custom-title`` / ``bridge-session`` from a fixture
   rollout file via a separate (Option (a)) pass; ``ai-title`` is
   DROPPED per Architect §3 (operator-set custom-title is authoritative).
   Last-write-wins when the operator ``/rename``-cycled mid-session.

2. **claude_code_filesystem source plugin** — :meth:`discover_sessions`
   threads the metadata onto the new :class:`ExternalSessionRef` fields:
   ``vendor_session_label`` (= agent_name), ``originator_session_label``
   (= agent_name; single-actor mirror), ``originator_agent_instance_id``
   (= bridge_session_id, the durable UUID), ``summary_text_seed``
   (= custom_title). No live state, no live filesystem, tempfile-isolated
   per :memory:[[sandbox-mutating-smokes]].

3. **claude_code vendor parse_line_data invariant** — the 4 metadata
   types remain in ``_SKIP_LINE_TYPES`` (event prose stays clean); the
   promotion happens via the separate pass, not by mutating the event
   stream contract.

Run::

    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_metadata_promotion_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(
        REPO_ROOT
        / "plugins"
        / "claude_code_filesystem_session_source_plugin"
        / "src"
    ),
)

from ananta.llm.session_ledger.vendor import claude_code as claude_code_vendor  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ───── Fixture builders ─────────────────────────────────────────────────────


_SESSION_ID = "471a05a5-d18c-4dc0-87c3-e96a02b852ff"


def _write_fixture(
    path: Path,
    *,
    lines: list[dict[str, object]],
) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for entry in lines:
            fh.write(json.dumps(entry) + "\n")


# ───── (1) Single-pass extraction across all 3 promoted types ───────────────


def test_read_session_metadata_extracts_all_three_types() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{_SESSION_ID}.jsonl"
        _write_fixture(path, lines=[
            {"type": "agent-name", "agentName": "Claude-A",
             "sessionId": _SESSION_ID},
            {"type": "custom-title", "customTitle": "Architect ruling",
             "sessionId": _SESSION_ID},
            {"type": "bridge-session", "sessionId": _SESSION_ID,
             "bridgeSessionId": "cse_0178ZLiM8pUT8jEVyStUqFFm",
             "lastSequenceNum": 0},
            # ai-title MUST be dropped per Architect §3:
            {"type": "ai-title", "aiTitle": "What I think this session is",
             "sessionId": _SESSION_ID},
        ])
        metadata = claude_code_vendor.read_session_metadata(path)
    _check(
        metadata.agent_name == "Claude-A",
        f"agent_name extracted (got {metadata.agent_name!r})",
    )
    _check(
        metadata.custom_title == "Architect ruling",
        f"custom_title extracted (got {metadata.custom_title!r})",
    )
    _check(
        metadata.bridge_session_id == "cse_0178ZLiM8pUT8jEVyStUqFFm",
        f"bridge_session_id extracted (got {metadata.bridge_session_id!r})",
    )
    # ai-title is dropped — confirmed by the absence of a corresponding
    # field on ClaudeCodeSessionMetadata. The dataclass has no ai_title
    # attribute; sanity-check with hasattr to nail the contract.
    _check(
        not hasattr(metadata, "ai_title"),
        "ClaudeCodeSessionMetadata has NO ai_title field (Architect §3 drop)",
    )


# ───── (2) Last-write-wins on /rename-cycled fixtures ───────────────────────


def test_read_session_metadata_last_write_wins() -> None:
    """Architect §3 line: 'A last-write-wins precedence applies when
    multiple lines of the same type appear in the file (operator may
    have /rename-cycled within the session; the most recent value
    reflects the final state).'"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{_SESSION_ID}.jsonl"
        _write_fixture(path, lines=[
            {"type": "agent-name", "agentName": "Claude-A",
             "sessionId": _SESSION_ID},
            {"type": "custom-title", "customTitle": "First title",
             "sessionId": _SESSION_ID},
            # ... mid-session /rename ...
            {"type": "agent-name", "agentName": "Claude-Architect",
             "sessionId": _SESSION_ID},
            {"type": "custom-title", "customTitle": "Final title",
             "sessionId": _SESSION_ID},
        ])
        metadata = claude_code_vendor.read_session_metadata(path)
    _check(
        metadata.agent_name == "Claude-Architect",
        f"final agent_name wins (got {metadata.agent_name!r})",
    )
    _check(
        metadata.custom_title == "Final title",
        f"final custom_title wins (got {metadata.custom_title!r})",
    )


# ───── (3) Missing file → empty metadata struct (no raise) ──────────────────


def test_read_session_metadata_missing_file_returns_empty() -> None:
    """A non-existent path returns the default ClaudeCodeSessionMetadata
    so downstream code stays uniform — discover_sessions calls this
    speculatively for every rollout, including ones that may have
    vanished between glob and read."""
    metadata = claude_code_vendor.read_session_metadata(
        Path("/tmp/nonexistent-rollout-2026-12-31.jsonl"),
    )
    _check(
        metadata.agent_name is None,
        f"missing file: agent_name=None (got {metadata.agent_name!r})",
    )
    _check(
        metadata.custom_title is None,
        f"missing file: custom_title=None (got {metadata.custom_title!r})",
    )
    _check(
        metadata.bridge_session_id is None,
        f"missing file: bridge_session_id=None (got {metadata.bridge_session_id!r})",
    )


# ───── (4) discover_sessions threads metadata onto ExternalSessionRef ───────


class _StubConfigProvider:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, name: str, default: object | None = None) -> object:
        if name in self._values:
            return self._values[name]
        return default


def test_discover_sessions_populates_promoted_fields() -> None:
    """End-to-end: a tempdir with one project dir + one rollout file
    yields exactly one ExternalSessionRef whose new fields carry the
    promoted metadata."""
    # Defer import so PYTHONPATH manipulation above takes effect.
    from claude_code_filesystem_session_source_plugin.plugin import (  # noqa: PLC0415
        ClaudeCodeFilesystemSessionSourcePlugin,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project_dir = root / "-Users-alice-Workspace-example"
        project_dir.mkdir()
        rollout = project_dir / f"{_SESSION_ID}.jsonl"
        _write_fixture(rollout, lines=[
            {"type": "agent-name", "agentName": "Claude-A",
             "sessionId": _SESSION_ID},
            {"type": "custom-title", "customTitle": "Per-peer slice impl",
             "sessionId": _SESSION_ID},
            {"type": "bridge-session", "sessionId": _SESSION_ID,
             "bridgeSessionId": "cse_TEST_BRIDGE_UUID",
             "lastSequenceNum": 0},
            # An actual user message so the rollout isn't metadata-only:
            {"type": "user", "uuid": "evt-msg-1",
             "timestamp": "2026-05-31T17:00:00.000Z",
             "sessionId": _SESSION_ID, "cwd": "/Users/alice/Workspace/example",
             "gitBranch": "master",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "hello"}]}},
        ])

        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        # P1.1.E: root is threaded per-call as root_uri; only glob stays in config.
        plugin.set_config_provider(  # type: ignore[arg-type]
            _StubConfigProvider({"glob": "*.jsonl"}),
        )
        plugin.prepare_for_readiness()
        refs = list(plugin.discover_sessions(str(root), None))

    _check(
        len(refs) == 1,
        f"exactly one ExternalSessionRef yielded (got {len(refs)})",
    )
    if not refs:
        return
    ref = refs[0]
    _check(
        ref.external_session_id == _SESSION_ID,
        f"external_session_id = filename stem (got {ref.external_session_id!r})",
    )
    _check(
        ref.vendor_session_label == "Claude-A",
        f"vendor_session_label = agent_name (got {ref.vendor_session_label!r})",
    )
    _check(
        ref.originator_session_label == "Claude-A",
        f"originator_session_label = agent_name (single-actor mirror); "
        f"got {ref.originator_session_label!r}",
    )
    _check(
        ref.originator_agent_instance_id == "cse_TEST_BRIDGE_UUID",
        f"originator_agent_instance_id = bridge_session_id "
        f"(got {ref.originator_agent_instance_id!r})",
    )
    _check(
        ref.summary_text_seed == "Per-peer slice impl",
        f"summary_text_seed = custom_title "
        f"(got {ref.summary_text_seed!r})",
    )
    # Non-applicable for single-actor sessions per Architect §3:
    _check(
        ref.recipient_session_label is None,
        f"recipient_session_label is None for single-actor "
        f"(got {ref.recipient_session_label!r})",
    )
    _check(
        ref.recipient_agent_instance_id is None,
        f"recipient_agent_instance_id is None for single-actor "
        f"(got {ref.recipient_agent_instance_id!r})",
    )


# ───── (5) Skip-list invariant for parse_line_data still holds ──────────────


def test_skip_list_invariant_holds_after_promotion() -> None:
    """The promotion happens via the separate read_session_metadata pass.
    parse_line_data MUST still skip all 4 types from event prose so
    the per-event stream stays clean (no double-counting against
    vendor_event_id idempotency)."""
    for line_type in ("agent-name", "custom-title", "bridge-session", "ai-title"):
        raised: ValueError | None = None
        events: list[object] = []
        try:
            events = list(claude_code_vendor.parse_line_data({
                "type": line_type,
                "sessionId": _SESSION_ID,
            }))
        except ValueError as exc:
            raised = exc
        _check(
            raised is None and events == [],
            f"parse_line_data skip-list still drops {line_type!r} "
            f"(raised={raised!r}, events={len(events)})",
        )


# ───── Driver ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=== claude_code_metadata_promotion_smoke ===")
    test_read_session_metadata_extracts_all_three_types()
    test_read_session_metadata_last_write_wins()
    test_read_session_metadata_missing_file_returns_empty()
    test_discover_sessions_populates_promoted_fields()
    test_skip_list_invariant_holds_after_promotion()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
