"""Pre-commit consumer — BLOCKING root-manifest drift gate.

Invoked from ``.githooks/pre-commit`` after the venv-path + self-symlink
guards.  Runs from the repo root (git enforces cwd at hook invocation);
the gate refuses the commit on any BLOCKING violation per §6.1.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .classifier import classify_root_entries
from .report import format_report
from .types import MANIFEST_FILENAME


def main() -> int:
    repo_root = Path.cwd().resolve()
    manifest_path = repo_root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        # No manifest yet → no-op (matches the F8 W-INT always-on contract).
        return 0

    # Pre-commit ignores ANANTA_ROOT_MANIFEST_IGNORE_PATTERNS (strict layer).
    classification = classify_root_entries(manifest_path, repo_root)

    if classification.has_blocking_violations:
        print(format_report(classification, severity="BLOCKING"), file=sys.stderr)
        return 1

    if classification.cleanup_overdue:
        print(format_report(classification, severity="WARN"), file=sys.stderr)
        # WARN is not blocking.
    return 0


if __name__ == "__main__":
    sys.exit(main())
