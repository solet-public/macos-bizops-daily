"""M10 §1.3 — vendor parser smoke for claude_code_tasks.

Verifies per-file JSON parsing:
- happy-path: all 7 required fields parse, lists coerce, event_at = mtime
- missing-field raises ValueError (KB "Critical Development Guidelines v2")
- non-dict top-level raises ValueError
- malformed JSON raises ValueError
- non-string blockedBy items coerce via str() (Codex CLI sometimes mixes types)
- to_payload emits stable dict shape consumed by the source plugin's normalize
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor import claude_code_tasks as v  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _write(path: Path, obj: object, mtime: float | None = None) -> None:
    path.write_text(json.dumps(obj))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_happy_path_parse() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "1.json"
        mtime = 1_700_000_000.0
        _write(
            p,
            {
                "id": "t1",
                "subject": "Run smoke tests",
                "description": "Run the M10 parser smoke and verify pass",
                "activeForm": "Running smoke tests",
                "status": "in_progress",
                "blocks": ["t2", "t3"],
                "blockedBy": [],
            },
            mtime=mtime,
        )
        parsed = v.parse_task_file(p)
        _assert(parsed.task_id == "t1", parsed.task_id)
        _assert(parsed.subject == "Run smoke tests", parsed.subject)
        _assert(parsed.description.startswith("Run the M10"), parsed.description)
        _assert(parsed.active_form == "Running smoke tests", parsed.active_form)
        _assert(parsed.status == "in_progress", parsed.status)
        _assert(parsed.blocks == ("t2", "t3"), str(parsed.blocks))
        _assert(parsed.blocked_by == (), str(parsed.blocked_by))
        _assert(
            parsed.event_at == datetime.fromtimestamp(mtime, tz=UTC),
            f"event_at must equal file mtime: {parsed.event_at}",
        )


def test_missing_id_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.json"
        _write(
            p,
            {
                "subject": "no id field",
                "description": "x",
                "activeForm": "x",
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
            },
        )
        try:
            v.parse_task_file(p)
        except ValueError as e:
            _assert("'id'" in str(e), f"unexpected ValueError: {e}")
            return
        raise AssertionError("missing id should ValueError")


def test_non_dict_top_level_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "y.json"
        p.write_text(json.dumps(["not", "a", "dict"]))
        try:
            v.parse_task_file(p)
        except ValueError as e:
            _assert("not a dict" in str(e), f"unexpected ValueError: {e}")
            return
        raise AssertionError("non-dict should ValueError")


def test_malformed_json_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "z.json"
        p.write_text('{"id": "t1", subject: "missing quotes"}')
        try:
            v.parse_task_file(p)
        except ValueError as e:
            _assert("malformed JSON" in str(e), f"unexpected ValueError: {e}")
            return
        raise AssertionError("malformed JSON should ValueError")


def test_blocked_by_with_non_string_items_coerces() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "5.json"
        _write(
            p,
            {
                "id": "t5",
                "subject": "mixed types",
                "description": "mixed",
                "activeForm": "mixing",
                "status": "pending",
                "blocks": [],
                "blockedBy": [1, "t4", 2.5],
            },
        )
        parsed = v.parse_task_file(p)
        _assert(
            parsed.blocked_by == ("1", "t4", "2.5"),
            f"blocked_by coerce failed: {parsed.blocked_by}",
        )


def test_to_payload_shape_stable() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "1.json"
        _write(
            p,
            {
                "id": "t1",
                "subject": "subj",
                "description": "desc",
                "activeForm": "ing",
                "status": "completed",
                "blocks": ["t2"],
                "blockedBy": ["t0"],
            },
        )
        parsed = v.parse_task_file(p)
        payload = v.to_payload(parsed)
        _assert(payload["kind"] == v.PAYLOAD_KIND_TASK, payload["kind"])
        _assert(payload["task_id"] == "t1", payload["task_id"])
        _assert(payload["subject"] == "subj", payload["subject"])
        _assert(payload["status"] == "completed", payload["status"])
        # Lists in payload, not tuples — JSONB round-trip needs JSON-native types.
        _assert(payload["blocks"] == ["t2"], str(payload["blocks"]))
        _assert(payload["blockedBy"] == ["t0"], str(payload["blockedBy"]))


def main() -> int:
    tests = [
        test_happy_path_parse,
        test_missing_id_raises,
        test_non_dict_top_level_raises,
        test_malformed_json_raises,
        test_blocked_by_with_non_string_items_coerces,
        test_to_payload_shape_stable,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
