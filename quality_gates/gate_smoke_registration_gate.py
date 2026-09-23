#!/usr/bin/env python3
"""Fail closed when a repository smoke is neither registered nor excluded.

``gate_smokes.txt`` is intentionally an allowlist: the battery may only run
smokes that have been reviewed for offline suitability.  That property alone
cannot distinguish an intentional omission from a newly-written smoke that a
lane forgot to register.  This gate closes that gap.

Every in-repository ``*_smoke.py`` file must appear exactly once in one of two
places:

* ``gate_smokes.txt`` when it belongs in the normal battery; or
* ``gate_smoke_exclusions.txt`` when it deliberately cannot.  Exclusions use
  the shared mandatory owner/reason/expiry schema, are exact paths (not glob
  patterns), and expire back into a blocking finding.

The population is the same tracked-or-unignored rule used by ``gate_scope``.
That makes a newly-created, not-yet-staged smoke visible immediately instead
of allowing a clean battery to hide it until a Git action happens.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_REGISTER = Path("quality_gates/gate_smokes.txt")
_DEFAULT_EXCLUSIONS = Path("quality_gates/gate_smoke_exclusions.txt")
_SMOKE_SUFFIX = "_smoke.py"
_BUNDLED_VENV_PREFIX = ".venv"


@dataclass(frozen=True, slots=True)
class RegistrationReport:
    smoke_paths: frozenset[str]
    registered: frozenset[str]
    excluded: frozenset[str]
    unregistered: tuple[str, ...]
    stale_exclusions: tuple[str, ...]
    overlaps: tuple[str, ...]


def _git_smoke_paths(root: Path) -> frozenset[str]:
    """Return all tracked or unignored ``*_smoke.py`` paths beneath ``root``."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "git ls-files for gate-smoke census failed "
            f"(exit {result.returncode}): {result.stderr.strip() or '(no stderr)'}",
        )
    return frozenset(
        path
        for path in result.stdout.split("\0")
        if path.endswith(_SMOKE_SUFFIX)
        and not any(part.startswith(_BUNDLED_VENV_PREFIX) for part in Path(path).parts)
    )


def _read_registered(path: Path) -> frozenset[str]:
    if not path.is_file():
        raise FileNotFoundError(f"gate-smoke register not found: {path}")
    entries = {
        raw.strip()
        for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.lstrip().startswith("#")
    }
    # The historical register also contains a small set of ``smoke_*.py``
    # files.  The completeness contract deliberately owns the ``*_smoke.py``
    # population named by iss_fb75bf89; those valid older entries remain
    # runnable but are outside this census rather than being misreported.
    return frozenset(entries)


def _read_exclusions(path: Path, *, today: date | None = None) -> frozenset[str]:
    # Import lazily so the module remains directly runnable from a temporary
    # fixture repository in its smoke.
    from quality_gates.allowlist_schema import load_allowlist

    entries = load_allowlist(path, today=today)
    malformed = sorted(entry for entry in entries if not entry.endswith(_SMOKE_SUFFIX))
    if malformed:
        raise ValueError(
            f"{path}: exclusion entries must name exact '*_smoke.py' paths: {malformed}",
        )
    return entries


def inspect_registration(
    root: Path,
    *,
    register_path: Path = _DEFAULT_REGISTER,
    exclusions_path: Path = _DEFAULT_EXCLUSIONS,
    today: date | None = None,
) -> RegistrationReport:
    """Measure one repository's explicit smoke-registration partition."""
    smoke_paths = _git_smoke_paths(root)
    registered = _read_registered(root / register_path)
    excluded = _read_exclusions(root / exclusions_path, today=today)
    overlaps = tuple(sorted(registered & excluded))
    stale_exclusions = tuple(sorted(excluded - smoke_paths))
    unregistered = tuple(sorted(smoke_paths - registered - excluded))
    return RegistrationReport(
        smoke_paths=smoke_paths,
        registered=registered,
        excluded=excluded,
        unregistered=unregistered,
        stale_exclusions=stale_exclusions,
        overlaps=overlaps,
    )


def _print_report(report: RegistrationReport) -> int:
    print(
        "gate-smoke registration census: "
        f"{len(report.smoke_paths)} smoke(s), {len(report.registered)} registered, "
        f"{len(report.excluded)} deliberately excluded",
    )
    problems: list[tuple[str, tuple[str, ...]]] = [
        ("registered AND excluded", report.overlaps),
        ("stale exclusions", report.stale_exclusions),
        ("unregistered smoke(s)", report.unregistered),
    ]
    for label, paths in problems:
        if not paths:
            continue
        print(f"\nFAIL: {label} ({len(paths)}):")
        for path in paths:
            print(f"  {path}")
    return 1 if any(paths for _, paths in problems) else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--register", type=Path, default=_DEFAULT_REGISTER)
    parser.add_argument("--exclusions", type=Path, default=_DEFAULT_EXCLUSIONS)
    args = parser.parse_args(argv)
    try:
        report = inspect_registration(
            args.root.resolve(),
            register_path=args.register,
            exclusions_path=args.exclusions,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 64
    return _print_report(report)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
