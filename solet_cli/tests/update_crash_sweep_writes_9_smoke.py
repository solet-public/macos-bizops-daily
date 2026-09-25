"""Step-6 crash sweep, write boundaries slice 9 of 12 (design section 6.1, F-CR-1).

A crash is injected after EVERY durable Manager-state write (update journal, inventory, doctor journal) of the single-colour reference run (an enrolled row
through Step 4, the runtime approval, every runtime stage, the final doctor and
promotion); after each crash a FRESH ``apply_update`` resumes and the sweep
asserts the reference terminal ``promoted``, the reference inventory row, the
reference target/HOME bytes, bounded mutation counters and an immutable journal
prefix.  This file sweeps slice 9 of 12 of the write boundaries; the twelve slices partition them exactly.  Runs under the fail-on-call database spy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import build_fixture, db_spy  # noqa: E402
from _step6_support import CrashSweep, run_to_promoted, sweep_slice  # noqa: E402

_CHECKS = 0
_PART, _PARTS = 9, 12


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        sweep = CrashSweep(build=lambda path: build_fixture(path), scenario=run_to_promoted, root=root)
        _, writes, applies = sweep.reference()
        _check((writes >= 25, applies >= 5) == (True, True), f"reference run crossed {writes} write and {applies} apply boundaries")
        total = writes
        sweep.sweep_writes(total, _PART, _PARTS)
        indices = sweep_slice(total, _PART, _PARTS)
        _check(len(sweep.outcomes) == len(indices), "one outcome per crash point in this slice")
        _check(all(item.resumed_status == "promoted" for item in sweep.outcomes), "every crash point in this slice resumed to promoted")
        _check([item.index for item in sweep.outcomes] == list(indices), "the slice covered exactly its crash indices")
    print(f"update_crash_sweep_writes_9_smoke OK: {_CHECKS} checks passed; crash points {list(indices)[0]}..{list(indices)[-1]} of {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
