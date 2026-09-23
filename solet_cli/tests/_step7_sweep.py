"""CH-34: Step 6's ``CrashSweep`` over the real-style fixture (Step 7 design section 5.6).

The reference run is CH-44's (the legacy root manifest, so the sweep covers the
per-operation re-baseline write of section 6.6 as well as every Step-6
boundary).  Three Step-6 helpers are re-bound for a fixture enrolled by a REAL
import rather than ``enroll()``'s hand-written row: the journal finder must
skip the import journal ``last_verified_operation_id`` names, the row shape
must normalise the import's inspection-bundle digest (it hashes the target
path), and the byte map must include every untracked symlink by target and
hash ``.solet/genesis.json`` without its wall-clock stamp.  Every crash is a
fresh fixture; every resume a fresh ``apply_update``; ``db_spy`` wraps the run.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _step6_support as step6  # noqa: E402
from _step5_support import Fixture, db_spy  # noqa: E402
from _step6_support import CrashSweep, run_to_promoted  # noqa: E402
from _step7_support import Knobs, RealStyleFixture, build_real_style, fixture_bytes, prime_genesis_imports  # noqa: E402
from solet_manager.errors import StateError  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.update_journal import read_update_journal  # noqa: E402

__all__ = ["SWEEP_KNOBS", "run_slice"]

SWEEP_KNOBS = Knobs(manager_state="enrolled", legacy_root_manifest=True)
prime_genesis_imports()
_original_last_update_journal = step6.last_update_journal
_original_record_shape = step6.record_shape


def _last_update_journal(fixture: Fixture) -> dict[str, JsonValue]:
    try:
        return _original_last_update_journal(fixture)
    except StateError:
        record = fixture.record()
        journals = sorted((fixture.paths.operations_dir / record.instance_id).glob("opr_*.json"), key=lambda path: path.stat().st_mtime)
        for path in reversed(journals):
            if json.loads(path.read_text(encoding="utf-8")).get("kind") == "update":
                return read_update_journal(path)
        raise AssertionError("no update journal exists yet") from None


def _record_shape(fixture: Fixture) -> dict[str, JsonValue]:
    shape = _original_record_shape(fixture)
    shape["inspection_bundle_digest"] = "<inspection_bundle>"
    return shape


def _byte_map(fixture: Fixture) -> dict[str, str]:
    assert isinstance(fixture, RealStyleFixture)
    return fixture_bytes(fixture)


@contextmanager
def _rebound() -> Iterator[None]:
    with patch.object(step6, "last_update_journal", _last_update_journal), patch.object(step6, "record_shape", _record_shape), patch.object(step6, "byte_map", _byte_map):
        yield


def _build(path: Path) -> Fixture:
    return build_real_style(path, knobs=SWEEP_KNOBS)


def run_slice(mode: str, part: int, parts: int) -> str:
    """Run the reference, then the ``part``-th of ``parts`` slices of the write or apply boundaries."""
    with tempfile.TemporaryDirectory() as temporary, db_spy(), _rebound():
        sweep = CrashSweep(_build, run_to_promoted, Path(temporary).resolve())
        _fixture, writes, applies = sweep.reference()
        assert sweep.reference_bytes is not None
        assert any(key.startswith("target/knowledge_bases/") and value.startswith("link:") for key, value in sweep.reference_bytes.items()), "the byte map carries the untracked symlinks by target"
        assert "target/NOTICE" in sweep.reference_bytes and "target/root_manifest.yaml" in sweep.reference_bytes, "the byte map carries the preserved tracked files"
        if mode == "write":
            sweep.sweep_writes(writes, part, parts)
        else:
            sweep.sweep_applies(applies, part, parts)
        outcomes: list[dict[str, Any]] = [{"index": item.index, "boundary": item.boundary, "crashed": item.crashed_status, "resumed": item.resumed_status} for item in sweep.outcomes]
        assert all(item["resumed"] == "promoted" for item in outcomes), outcomes
        return f"{mode} boundaries {writes if mode == 'write' else applies}, slice {part}/{parts}: {len(outcomes)} crashes, every one resumed to promoted"
