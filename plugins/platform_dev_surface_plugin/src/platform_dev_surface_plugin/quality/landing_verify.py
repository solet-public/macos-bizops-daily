"""Read-only landing-verification primitives for Git-Controller.

The helpers in this module never invoke a mutating Git verb.  They inspect a
declared hash manifest, model an append-only register merge with ``git
merge-file -p`` (which writes only its stdout), and consult the quality gate's
own path-scope predicate before a landing relies on whole-tree checks.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, cast

_GIT_TIMEOUT_SECONDS = 30
_POINTER_FILE_MAX_BYTES = 131_072
_ABSOLUTE_PATH_RE = re.compile(r"(?<=[\"'])(/[^\"'\n]+)(?=[\"'])")


class LandingVerificationError(ValueError):
    """A caller supplied an unusable root, path, or revision."""


def verify_hash_manifest(
    *, unit_id: str, root_path: str, manifest: dict[str, str],
) -> dict[str, Any]:
    """Rehash each declared file beneath ``root_path`` without reading Git."""
    root = _require_directory(root_path, "root_path")
    if not unit_id:
        raise LandingVerificationError("unit_id must not be empty")
    if not manifest:
        raise LandingVerificationError("manifest must declare at least one path")
    rows = [_hash_row(root, path, digest) for path, digest in sorted(manifest.items())]
    warnings = _editable_install_warnings(root)
    return {
        "unit_id": unit_id,
        "root_path": str(root),
        "files": rows,
        "all_exact": all(row["status"] == "exact" for row in rows),
        "editable_install_warnings": warnings,
        "editable_install_shadowing_detected": bool(warnings),
    }


def predict_append_only_register_merge(
    *,
    base_ref: str,
    lane_root_path: str,
    current_master: str,
    register_path: str = "quality_gates/gate_smokes.txt",
) -> dict[str, Any]:
    """Report a no-write three-way merge prediction for a tracked register."""
    lane_root = _require_directory(lane_root_path, "lane_root_path")
    register_rel = _require_relative_path(register_path, "register_path")
    _require_git_ref(base_ref, "base_ref")
    _require_git_ref(current_master, "current_master")
    lane_file = lane_root / register_rel
    if not lane_file.is_file():
        raise LandingVerificationError(f"register path is missing in lane root: {register_rel}")
    base_content = _git_show(lane_root, base_ref, register_rel)
    master_content = _git_show(lane_root, current_master, register_rel)
    lane_content = lane_file.read_text(encoding="utf-8")
    candidate_content, merge_exit_code = _merge_file(master_content, base_content, lane_content)
    append_only = lane_content.startswith(base_content)
    lane_hunk = lane_content[len(base_content):] if append_only else ""
    lane_entries = _register_entries(lane_hunk)
    master_entries = _register_entries(master_content)
    duplicates = sorted({entry for entry in lane_entries if entry in master_entries})
    missing = sorted({entry for entry in lane_entries if entry not in master_entries})
    return {
        "base_ref": base_ref,
        "current_master": current_master,
        "lane_root_path": str(lane_root),
        "register_path": register_rel,
        "master_drifted_since_base": master_content != base_content,
        "lane_change_is_append_only": append_only,
        "lane_hunk": lane_hunk,
        "lane_registrations": lane_entries,
        "duplicate_registrations_on_master": duplicates,
        "missing_registrations_on_master": missing,
        "merged_candidate_content": candidate_content,
        "merge_verdict": "clean" if merge_exit_code == 0 else "conflict",
        "git_merge_file_exit_code": merge_exit_code,
    }


def detect_scope_regex_gaps(paths: list[str]) -> dict[str, Any]:
    """Classify paths with ``code_quality_check.py``'s exact scope predicate."""
    if not paths:
        raise LandingVerificationError("paths must contain at least one path")
    predicate = _load_scope_predicate()
    normalized = [_require_relative_path(path, "paths entry") for path in paths]
    in_scope = [path for path in normalized if predicate(path)]
    out_of_scope = [path for path in normalized if not predicate(path)]
    return {
        "paths": normalized,
        "in_scope": in_scope,
        "out_of_scope": out_of_scope,
        "manual_static_analysis_needed": bool(out_of_scope),
        "scope_source": "quality_gates/code_quality_check.py:is_per_file_gate_scoped_path",
    }


def _require_directory(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise LandingVerificationError(f"{name} must be an existing absolute directory: {value!r}")
    return path.resolve()


def _require_relative_path(value: str, name: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise LandingVerificationError(f"{name} must be a non-empty repo-relative path: {value!r}")
    return path.as_posix()


def _require_git_ref(value: str, name: str) -> None:
    if not value or value.startswith("-"):
        raise LandingVerificationError(f"{name} must be a non-option Git revision: {value!r}")


def _hash_row(root: Path, path: str, expected: str) -> dict[str, str | None]:
    rel = _require_relative_path(path, "manifest path")
    if not _is_sha256(expected):
        raise LandingVerificationError(f"manifest digest for {rel!r} is not a SHA-256 hex digest")
    candidate = (root / rel).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise LandingVerificationError(f"manifest path escapes root through a symlink: {rel!r}")
    if not candidate.is_file():
        return {"path": rel, "expected_sha256": expected, "actual_sha256": None, "status": "missing"}
    with candidate.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    return {
        "path": rel,
        "expected_sha256": expected,
        "actual_sha256": actual,
        "status": "exact" if actual == expected else "mismatch",
    }


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _editable_install_warnings(root: Path) -> list[dict[str, str]]:
    """Find editable-install pointers that resolve outside a scratch worktree."""
    venv = root / ".venv"
    if not venv.exists():
        return []
    warnings: list[dict[str, str]] = []
    resolved_venv = venv.resolve()
    if not resolved_venv.is_relative_to(root):
        warnings.append({
            "kind": "venv_outside_root",
            "pointer_file": ".venv",
            "target": str(resolved_venv),
        })
    for site_packages in _site_packages_dirs(resolved_venv):
        for pointer in sorted((*site_packages.glob("*.pth"), *site_packages.glob("__editable__*.py"))):
            warnings.extend(_outside_pointer_targets(root, pointer))
    return warnings


def _site_packages_dirs(venv: Path) -> list[Path]:
    return [
        path for path in (*venv.glob("lib/python*/site-packages"), venv / "Lib" / "site-packages")
        if path.is_dir()
    ]


def _outside_pointer_targets(root: Path, pointer: Path) -> list[dict[str, str]]:
    if pointer.stat().st_size > _POINTER_FILE_MAX_BYTES:
        return [{
            "kind": "editable_pointer_unreadable",
            "pointer_file": str(pointer),
            "target": "pointer file exceeds inspection cap",
        }]
    content = pointer.read_text(encoding="utf-8", errors="replace")
    warnings: list[dict[str, str]] = []
    for raw_target in sorted(set(_ABSOLUTE_PATH_RE.findall(content))):
        target = Path(raw_target).resolve(strict=False)
        if target.exists() and not target.is_relative_to(root):
            warnings.append({
                "kind": "editable_install_points_outside_root",
                "pointer_file": str(pointer),
                "target": str(target),
            })
    return warnings


def _git_show(root: Path, ref: str, register_path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{ref}:{register_path}"], cwd=root, check=False,
        capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise LandingVerificationError(
            f"git show {ref}:{register_path} failed: {result.stderr.strip()[:400]}"
        )
    return result.stdout


def _merge_file(current: str, base: str, lane: str) -> tuple[str, int]:
    with tempfile.TemporaryDirectory(prefix="gc_landing_verify_") as directory:
        temporary = Path(directory)
        current_file, base_file, lane_file = (
            temporary / "current", temporary / "base", temporary / "lane",
        )
        current_file.write_text(current, encoding="utf-8")
        base_file.write_text(base, encoding="utf-8")
        lane_file.write_text(lane, encoding="utf-8")
        result = subprocess.run(
            ["git", "merge-file", "-p", str(current_file), str(base_file), str(lane_file)],
            check=False, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
        )
    if result.returncode not in (0, 1):
        raise LandingVerificationError(
            f"git merge-file prediction failed (exit {result.returncode}): {result.stderr.strip()[:400]}"
        )
    return result.stdout, result.returncode


def _register_entries(content: str) -> list[str]:
    return [line for raw in content.splitlines()
            if (line := raw.split("#", 1)[0].strip())]


def _load_scope_predicate() -> Callable[[str], bool]:
    repo_root = Path(__file__).resolve().parents[5]
    gate_directory = str(repo_root / "quality_gates")
    if gate_directory not in sys.path:
        sys.path.insert(0, gate_directory)
    module = importlib.import_module("code_quality_check")
    predicate = getattr(module, "is_per_file_gate_scoped_path", None)
    if not callable(predicate):
        raise RuntimeError("code_quality_check lacks is_per_file_gate_scoped_path")
    return cast(Callable[[str], bool], predicate)
