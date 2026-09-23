#!/usr/bin/env python3
"""F1 root_manifest.yaml end-to-end smoke.

Builds synthetic-fixture solet roots via ``tempfile`` and exercises
the three consumer surfaces (pre-commit, cutover, diagnostic) directly,
plus the W-INT C6.\\* check.  Pattern follows
``ananta/tests/platform/whole_tree_integration_gate_smoke.py`` — synthetic
trees rather than spinning up a live solet.

Covers the 7 positive-outcome assertions in design memo §7.1-§7.7.
§7.3 sub-asserts (b/c/d) — process registry + KB search + health probe
under a live solet — are integration-test territory beyond the
~250 LOC F1-A8 scope and deferred to a follow-on live-solet smoke.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

# Smokes run directly from a disposable worktree, whose active virtualenv can
# still be editable-installed against another checkout.  Test this worktree's
# sources, including in the subprocesses that exercise the consumer modules.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.root_manifest import (  # noqa: E402
    MANIFEST_FILENAME,
    classify_root_entries,
    load_manifest,
)
from ananta.core.root_manifest.diagnostic import emit_startup_diagnostic  # noqa: E402
from ananta.core.root_manifest.types import ENV_EXTRA_IGNORE_PATTERNS  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_BASE_MANIFEST = """
schema_version: 1
solet_name: smokebot

universal:
  files:
    - .gitignore
    - root_manifest.yaml
  directories:
    - ananta
    - plugins

platform_managed:
  directories:
    - .git
    - .venv
    - __pycache__

sanctioned:
  - path: disabled_plugins
    reason: "test fixture"
    operator_approved: "2026-06-15"

overrides: []

diagnostic:
  report_categories:
    - unknown_root_entries
    - missing_universal_entries
    - sunset_overdue
    - cleanup_overdue
  ignore_patterns:
    - "*.swp"
    - ".DS_Store"
"""


def _write_universal_set(root: Path) -> None:
    (root / ".gitignore").write_text("# fixture\n")
    (root / "ananta").mkdir()
    (root / "plugins").mkdir()
    (root / "disabled_plugins").mkdir()


@contextmanager
def fixture_root(manifest_yaml: str = _BASE_MANIFEST) -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="root_manifest_smoke_") as tmp:
        root = Path(tmp).resolve()
        (root / MANIFEST_FILENAME).write_text(manifest_yaml.lstrip())
        _write_universal_set(root)
        yield root


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

class SmokeError(AssertionError):
    pass


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def _capture_diagnostic(root: Path) -> str:
    """Run the diagnostic consumer against ``root`` and return log output."""
    logger = logging.getLogger("root_manifest_smoke")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        emit_startup_diagnostic(logger, root)
    finally:
        logger.removeHandler(handler)
    return buf.getvalue()


def _run_pre_commit(root: Path) -> tuple[int, str]:
    """Invoke ``python -m ananta.core.root_manifest.pre_commit`` with cwd=root."""
    env = os.environ.copy()
    env.pop(ENV_EXTRA_IGNORE_PATTERNS, None)
    env["PYTHONPATH"] = str(REPO_ROOT / "ananta" / "src")
    result = subprocess.run(
        [sys.executable, "-m", "ananta.core.root_manifest.pre_commit"],
        cwd=str(root), env=env, capture_output=True, text=True, check=False,
    )
    return result.returncode, result.stderr


# ---------------------------------------------------------------------------
# §7.1 — Pre-commit BLOCKS on unknown root entry
# ---------------------------------------------------------------------------

def case_pre_commit_blocks_on_unknown() -> None:
    with fixture_root() as root:
        (root / "unsanctioned.txt").write_text("rogue\n")
        code, stderr = _run_pre_commit(root)
        _assert(code != 0, "pre-commit should refuse commit on unknown entry")
        _assert("unsanctioned.txt" in stderr,
                f"pre-commit output should name the file; got: {stderr}")


# ---------------------------------------------------------------------------
# §7.2 — Cutover BLOCKS on unknown root entry
# ---------------------------------------------------------------------------

def case_cutover_blocks_on_unknown() -> None:
    with fixture_root() as root:
        (root / "rogue_dir").mkdir()
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(classification.has_blocking_violations,
                "cutover preflight Classification should flag BLOCKING")
        _assert("rogue_dir" in classification.unknown_entries,
                f"unknown_entries should list rogue_dir; got: {classification.unknown_entries}")


# ---------------------------------------------------------------------------
# §7.3 (a) — Startup REPORTS but does not raise / block
# ---------------------------------------------------------------------------

def case_startup_diagnostic_reports_without_blocking() -> None:
    with fixture_root() as root:
        (root / "noisy_drift").mkdir()
        log_text = _capture_diagnostic(root)
        _assert("noisy_drift" in log_text,
                f"diagnostic should name the drifting entry; got: {log_text!r}")
        _assert("DIAGNOSTIC" in log_text,
                f"diagnostic severity tag missing; got: {log_text!r}")
        # NEVER BLOCKING: _capture_diagnostic completed without raising.


# ---------------------------------------------------------------------------
# §7.4 — Sanctioned entries pass cleanly + (d) .DS_Store excluded
# ---------------------------------------------------------------------------

def case_sanctioned_passes_cleanly() -> None:
    with fixture_root() as root:
        (root / ".DS_Store").write_text("hidden\n")  # §7.4(d) ignore_patterns assertion
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(not classification.has_blocking_violations,
                f"baseline fixture should be clean; got: {classification}")
        _assert(".DS_Store" not in classification.unknown_entries,
                f".DS_Store must be filtered via diagnostic.ignore_patterns; "
                f"got: {classification.unknown_entries}")
        log_text = _capture_diagnostic(root)
        _assert(log_text == "" or "DIAGNOSTIC" not in log_text,
                f"diagnostic should be silent on a clean tree; got: {log_text!r}")


# ---------------------------------------------------------------------------
# §7.5 — Override metadata enforcement (missing field → schema fail)
# ---------------------------------------------------------------------------

def case_override_missing_field_fails_schema() -> None:
    broken = _BASE_MANIFEST.replace(
        "overrides: []",
        'overrides:\n  - path: foo/\n    reason: "no operator_approved"\n',
    )
    with fixture_root(broken) as root:
        manifest, error = load_manifest(root / MANIFEST_FILENAME)
        _assert(manifest is None and error is not None,
                f"manifest with missing override field should fail-closed; got: {manifest=}, {error=}")
        _assert("operator_approved" in (error or ""),
                f"error should name the missing field; got: {error!r}")
        code, stderr = _run_pre_commit(root)
        _assert(code != 0, "pre-commit should refuse the malformed manifest")
        _assert("operator_approved" in stderr,
                f"pre-commit output should name the missing field; got: {stderr!r}")


# ---------------------------------------------------------------------------
# §7.6 — Schema sidecar validation fail (bad schema_version)
# ---------------------------------------------------------------------------

def case_schema_validation_fail_blocks_strict_layers() -> None:
    broken = _BASE_MANIFEST.replace("schema_version: 1", "schema_version: 2")
    with fixture_root(broken) as root:
        # Pre-commit + cutover layer: BLOCKING
        code, stderr = _run_pre_commit(root)
        _assert(code != 0, "pre-commit should refuse on schema_version mismatch")
        _assert("schema" in stderr.lower(),
                f"pre-commit output should mention schema failure; got: {stderr!r}")
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(classification.has_blocking_violations,
                "cutover Classification should be BLOCKING on schema failure")
        _assert(classification.schema_validation_error is not None,
                "schema_validation_error should carry the violation")
        # Diagnostic layer: NEVER BLOCKING — falls back to UNGATED startup
        log_text = _capture_diagnostic(root)
        _assert("UNGATED" in log_text or "schema validation failed" in log_text,
                f"diagnostic should log fallback message; got: {log_text!r}")


# ---------------------------------------------------------------------------
# §7.7 — Sunset enforcement (date-string)
# ---------------------------------------------------------------------------

def case_sunset_enforcement_date_string() -> None:
    past = (date.today() - timedelta(days=42)).isoformat()
    with_overdue = _BASE_MANIFEST.replace(
        '  - path: disabled_plugins\n    reason: "test fixture"\n    operator_approved: "2026-06-15"',
        f'  - path: disabled_plugins\n'
        f'    reason: "test fixture"\n'
        f'    operator_approved: "2026-06-15"\n'
        f'    sunset_target: "{past}"',
    )
    with fixture_root(with_overdue) as root:
        # Path STILL present → BLOCKING (sunset_overdue)
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(any(e.path == "disabled_plugins" for e in classification.sunset_overdue),
                f"date-past sanctioned entry with path present should flag sunset_overdue; "
                f"got: {classification.sunset_overdue}")
        _assert(classification.has_blocking_violations,
                "sunset_overdue should mark BLOCKING for pre-commit + cutover")
        code, stderr = _run_pre_commit(root)
        _assert(code != 0, "pre-commit should refuse on date-past sunset_target")
        _assert("sunset" in stderr.lower() or "overdue" in stderr.lower(),
                f"pre-commit output should mention sunset; got: {stderr!r}")
        # Now: path ABSENT → cleanup_overdue WARN (not blocking)
        shutil.rmtree(root / "disabled_plugins")
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(not classification.has_blocking_violations,
                "absent path with past sunset should NOT block (cleanup_overdue is WARN)")
        _assert("disabled_plugins" in classification.cleanup_overdue,
                f"cleanup_overdue should list the entry; got: {classification.cleanup_overdue}")


# ---------------------------------------------------------------------------
# §7.8 — Git-awareness: untracked vs tracked-but-undeclared (iss_ce45713b)
# ---------------------------------------------------------------------------

def _git(root: Path, *arguments: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="smoke", GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="smoke", GIT_COMMITTER_EMAIL="smoke@example.invalid",
    )
    subprocess.run(("git", *arguments), cwd=str(root), env=env,
                   check=True, capture_output=True)


def case_git_awareness_untracked_vs_tracked() -> None:
    # (a) No Git at all: no index is consulted and every unknown entry is
    # tracked_unknown — exactly the pure filesystem scan this grew from.
    with fixture_root() as root:
        (root / "scratch.md").write_text("lane note\n")
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(not classification.git_index_consulted,
                "a bare directory must report git_index_consulted=False")
        _assert(classification.untracked_entries == (),
                f"without git nothing is classed untracked; got {classification.untracked_entries}")
        _assert(classification.tracked_unknown_entries == ("scratch.md",),
                f"without git every unknown is tracked_unknown; got "
                f"{classification.tracked_unknown_entries}")

    with fixture_root() as root:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")

        # (b) An UNTRACKED root file — a lane's scratch note — is unknown but
        # untracked: has_blocking_violations still holds (pre-commit and the
        # preflight probe keep their strictness) while tracked_unknown is empty.
        (root / "scratch.md").write_text("lane note\n")
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(classification.git_index_consulted, "a git root must consult the index")
        _assert(classification.unknown_entries == ("scratch.md",),
                f"unknown_entries unchanged in meaning; got {classification.unknown_entries}")
        _assert(classification.untracked_entries == ("scratch.md",),
                f"the scratch note must be classed untracked; got "
                f"{classification.untracked_entries}")
        _assert(classification.tracked_unknown_entries == (),
                f"an untracked note is not tracked_unknown; got "
                f"{classification.tracked_unknown_entries}")
        _assert(classification.has_blocking_violations,
                "has_blocking_violations must be UNCHANGED by git-awareness")
        code, stderr = _run_pre_commit(root)
        _assert(code != 0 and "untracked by git" in stderr,
                f"pre-commit still blocks and the report names the untracked "
                f"section; got code={code} stderr={stderr!r}")

        # (c) The DISCRIMINATOR: the same undeclared entry, STAGED (the index
        # is the boundary, so a file added for this very commit is tracked)
        # and then committed, is real root-manifest drift and stays
        # tracked_unknown at both points.
        _git(root, "add", "scratch.md")
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(classification.untracked_entries == (),
                f"a staged entry is tracked; got untracked {classification.untracked_entries}")
        _assert(classification.tracked_unknown_entries == ("scratch.md",),
                f"a staged undeclared entry must stay tracked_unknown; got "
                f"{classification.tracked_unknown_entries}")
        _git(root, "commit", "-qm", "undeclared root entry")
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(classification.tracked_unknown_entries == ("scratch.md",),
                f"a committed undeclared entry must stay tracked_unknown; got "
                f"{classification.tracked_unknown_entries}")

        # (d) A directory whose only contents are untracked is untracked; one
        # with a single tracked file inside is tracked.
        (root / "scratch_dir").mkdir()
        (root / "scratch_dir" / "note.txt").write_text("x\n")
        (root / "real_dir").mkdir()
        (root / "real_dir" / "kept.txt").write_text("x\n")
        _git(root, "add", "real_dir/kept.txt")
        classification = classify_root_entries(root / MANIFEST_FILENAME, root)
        _assert(classification.untracked_entries == ("scratch_dir",),
                f"only the all-untracked directory is untracked; got "
                f"{classification.untracked_entries}")
        _assert(classification.tracked_unknown_entries == ("real_dir", "scratch.md"),
                f"got {classification.tracked_unknown_entries}")

        # (e) A NESTED directory inside the repo is not the git top level:
        # no index is consulted for it rather than borrowing the parent's.
        nested = root / "ananta"
        (nested / MANIFEST_FILENAME).write_text(_BASE_MANIFEST.lstrip())
        classification = classify_root_entries(nested / MANIFEST_FILENAME, nested)
        _assert(not classification.git_index_consulted,
                "a nested directory must not borrow its parent repo's index")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CASES = [
    ("§7.1 pre-commit BLOCKS on unknown", case_pre_commit_blocks_on_unknown),
    ("§7.2 cutover BLOCKS on unknown", case_cutover_blocks_on_unknown),
    ("§7.3 startup reports without blocking", case_startup_diagnostic_reports_without_blocking),
    ("§7.4 sanctioned passes cleanly (.DS_Store excluded)", case_sanctioned_passes_cleanly),
    ("§7.5 override missing field fails schema", case_override_missing_field_fails_schema),
    ("§7.6 schema validation fail blocks strict layers", case_schema_validation_fail_blocks_strict_layers),
    ("§7.7 sunset enforcement date-string", case_sunset_enforcement_date_string),
    ("§7.8 git-awareness: untracked vs tracked-but-undeclared",
     case_git_awareness_untracked_vs_tracked),
]


def main() -> int:
    failures: list[tuple[str, str]] = []
    for label, fn in CASES:
        try:
            fn()
            print(f"  PASS — {label}")
        except SmokeError as exc:
            failures.append((label, str(exc)))
            print(f"  FAIL — {label}: {exc}", file=sys.stderr)
    print()
    if failures:
        print(f"{len(failures)}/{len(CASES)} cases failed", file=sys.stderr)
        return 1
    print(f"{len(CASES)}/{len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
