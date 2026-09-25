"""Step-6 F-RT-2: the router candidate failure under the write crash-sweep mode, slice 1 of 10 (design sections 6.1-6.2, M5).

This file sweeps the first tenth of the write boundaries; the shared body and its assertions live in
``_router_failure_sweep_support.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _router_failure_sweep_support import run_slice  # noqa: E402

_PART, _PARTS = 1, 10


def main() -> int:
    return run_slice("existing_install_router_failure_sweep_1_smoke", "write", _PART, _PARTS)


if __name__ == "__main__":
    raise SystemExit(main())
