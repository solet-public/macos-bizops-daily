#!/usr/bin/env python3
"""Cross-consumer fixture for the one reconciliation surface-digest algorithm."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from macos_self_deployment_plugin.surface_digest import surface_digest  # noqa: E402


def _write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _write(root / "code/a.py", b"alpha\n", 0o644)
        _write(root / "code/nested/b.py", b"beta\n", 0o755)
        first = surface_digest(root, (Path("code"),))
        _write(root / "code/nested/b.py", b"beta\n", 0o644)
        mode_changed = surface_digest(root, (Path("code"),))
        _write(root / "code/nested/b.py", b"beta changed\n", 0o644)
        content_changed = surface_digest(root, (Path("code"),))
    checks = [
        (first.startswith("sha256:"), "digest is versioned sha256"),
        (first != mode_changed, "mode participates in the digest"),
        (mode_changed != content_changed, "content participates in the digest"),
    ]
    failed = [label for passed, label in checks if not passed]
    for passed, label in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
