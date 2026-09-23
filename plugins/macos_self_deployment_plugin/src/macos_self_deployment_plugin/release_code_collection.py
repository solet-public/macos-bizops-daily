"""Fail-closed selected-file collection for immutable release code.

The release builder deliberately does not use a recursive copy for first-party
code.  Git porcelain cannot see ignored nested virtual environments, so a
recursive copy could make an artifact contain bytes that its source attestation
never examined.  This module first selects a no-follow population and then
materializes exactly that population with APFS CoW file copies.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

COLLECTOR_POLICY_VERSION: Final[str] = "release-code-collector/v1"
GIT_TIMEOUT_SECONDS: Final[float] = 10.0


class ReleaseCodeCollectionError(RuntimeError):
    """The source population cannot be safely selected or materialized."""


@dataclass(frozen=True, slots=True)
class SelectedFile:
    """One regular source file selected for an immutable release."""

    relative_path: str
    mode: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExcludedPath:
    """An intentionally omitted path with the evidence supporting exclusion."""

    relative_path: str
    reason: str
    evidence: dict[str, object]


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    """The selected population and the evidence supporting every omission."""

    code_subtrees: tuple[str, ...]
    files: tuple[SelectedFile, ...]
    exclusions: tuple[ExcludedPath, ...]
    git_head: str | None
    git_state: str


def _is_git_ignored(
    git_runner: Callable[..., subprocess.CompletedProcess[str]],
    relative_path: str,
    git_state: str,
) -> bool:
    """Return whether Git excludes this untracked path from source identity.

    The collector only applies ignore rules when it has a Git checkout to ask.
    ``git check-ignore`` deliberately does not match tracked paths, so a
    tracked symbolic link remains subject to the collector's fail-closed link
    refusal.
    """
    if git_state == "nogit":
        return False
    result = git_runner("check-ignore", "-q", "--", relative_path)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise ReleaseCodeCollectionError(
        f"git ignore check failed for {relative_path}: {result.stderr.strip()}"
    )


def _git_ignored_exclusion(relative_path: str) -> ExcludedPath:
    """Describe an ignored local artifact omitted before any link traversal."""
    return ExcludedPath(
        relative_path=relative_path,
        reason="gitignored_path",
        evidence={"git_check_ignore": "matched"},
    )


class ReleaseCodeCollector:
    """Select and CoW-copy release code without ever traversing links."""

    def __init__(
        self,
        *,
        source_root: Path,
        code_subtrees: tuple[str, ...],
        cp_binary: str,
        clone_timeout_seconds: float,
    ) -> None:
        self._source_root = source_root
        self._code_subtrees = code_subtrees
        self._cp_binary = cp_binary
        self._clone_timeout_seconds = clone_timeout_seconds

    def plan(self) -> CollectionPlan:
        """Return a complete selected-file plan before any code is copied."""
        git_head, git_state = self._git_identity()
        files: list[SelectedFile] = []
        exclusions: list[ExcludedPath] = []
        for subtree in self._code_subtrees:
            root = self._source_root / subtree
            root_stat = self._lstat(root, "code subtree")
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise ReleaseCodeCollectionError(
                    f"code subtree must be a real directory, not a link or other entry: {subtree}"
                )
            self._walk(root, files, exclusions, git_head, git_state)
        return CollectionPlan(
            code_subtrees=self._code_subtrees,
            files=tuple(sorted(files, key=lambda item: item.relative_path)),
            exclusions=tuple(sorted(exclusions, key=lambda item: item.relative_path)),
            git_head=git_head,
            git_state=git_state,
        )

    def materialize(self, plan: CollectionPlan, destination: Path) -> None:
        """CoW-copy exactly the selected regular files, then re-attest them."""
        destination.mkdir(parents=True, exist_ok=False)
        for item in plan.files:
            source = self._source_root / item.relative_path
            target = destination / item.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            source_before = self._regular_stat(source, item.relative_path)
            if stat.S_IMODE(source_before.st_mode) != item.mode or self._sha256(source) != item.sha256:
                raise ReleaseCodeCollectionError(
                    f"source changed after selection and before copy: {item.relative_path}"
                )
            self._cow_copy(source, target)
            os.chmod(target, item.mode, follow_symlinks=False)
            source_after = self._regular_stat(source, item.relative_path)
            target_stat = self._regular_stat(target, f"materialized {item.relative_path}")
            if (
                stat.S_IMODE(source_after.st_mode) != item.mode
                or self._sha256(source) != item.sha256
                or stat.S_IMODE(target_stat.st_mode) != item.mode
                or self._sha256(target) != item.sha256
            ):
                raise ReleaseCodeCollectionError(
                    f"source changed during copy or materialized bytes differ: {item.relative_path}"
                )

    def verify_materialized(self, plan: CollectionPlan, destination: Path) -> None:
        """Prove the staged code tree has precisely the selected population."""
        actual: list[str] = []
        for subtree in plan.code_subtrees:
            root = destination / subtree
            self._verify_walk(destination, root, actual)
        expected = [item.relative_path for item in plan.files]
        if sorted(actual) != expected:
            raise ReleaseCodeCollectionError(
                "materialized selected-file set differs from manifest population"
            )
        for item in plan.files:
            value = self._regular_stat(destination / item.relative_path, item.relative_path)
            if stat.S_IMODE(value.st_mode) != item.mode or self._sha256(destination / item.relative_path) != item.sha256:
                raise ReleaseCodeCollectionError(
                    f"materialized file does not match selected manifest: {item.relative_path}"
                )

    def verify_source_identity(self, plan: CollectionPlan) -> None:
        """Reject a HEAD transition during collection instead of relabelling it."""
        head, state = self._git_identity()
        if (head, state) != (plan.git_head, plan.git_state):
            raise ReleaseCodeCollectionError(
                "source Git identity changed while release code was being collected: "
                f"before=({plan.git_state}, {plan.git_head}), after=({state}, {head})"
            )

    def verify_materialized_manifest(
        self, plan: CollectionPlan, destination: Path, manifest_path: Path
    ) -> None:
        """Verify staged bytes/modes/set against the bytes written to manifest."""
        self.verify_materialized(plan, destination)
        try:
            payload = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseCodeCollectionError(f"cannot read selected-file manifest: {exc}") from exc
        expected = [
            {"path": item.relative_path, "mode": f"{item.mode:04o}", "sha256": item.sha256}
            for item in plan.files
        ]
        if not isinstance(payload, dict) or payload.get("selected_files") != expected:
            raise ReleaseCodeCollectionError(
                "selected-file manifest does not match materialized file population"
            )

    def manifest_payload(self, plan: CollectionPlan) -> dict[str, object]:
        """Return the additive, self-contained selected-file manifest payload."""
        artifact_head_equality = self._artifact_head_equality(plan)
        return {
            "policy_version": COLLECTOR_POLICY_VERSION,
            "code_subtrees": list(plan.code_subtrees),
            "git": {"head": plan.git_head, "state": plan.git_state},
            "artifact_to_head_equality": artifact_head_equality,
            "selected_files": [
                {"path": item.relative_path, "mode": f"{item.mode:04o}", "sha256": item.sha256}
                for item in plan.files
            ],
            "exclusions": [
                {
                    "path": item.relative_path,
                    "reason": item.reason,
                    "evidence": item.evidence,
                }
                for item in plan.exclusions
            ],
        }

    def _walk(
        self,
        directory: Path,
        files: list[SelectedFile],
        exclusions: list[ExcludedPath],
        git_head: str | None,
        git_state: str,
    ) -> None:
        relative_dir = directory.relative_to(self._source_root).as_posix()
        environment_evidence = self._environment_evidence(directory)
        if environment_evidence is not None:
            self._assert_exclusion_untracked(relative_dir, git_head, git_state)
            exclusions.append(
                ExcludedPath(
                    relative_path=relative_dir,
                    reason="nested_python_environment",
                    evidence=environment_evidence,
                )
            )
            return
        if _is_git_ignored(self._git, relative_dir, git_state):
            exclusions.append(_git_ignored_exclusion(relative_dir))
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ReleaseCodeCollectionError(f"cannot enumerate {relative_dir}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(self._source_root).as_posix()
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseCodeCollectionError(f"cannot inspect {relative}: {exc}") from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                # Let _walk preserve the stronger nested-environment evidence
                # before applying a directory-level ignore exclusion.
                self._walk(path, files, exclusions, git_head, git_state)
                continue
            if _is_git_ignored(self._git, relative, git_state):
                exclusions.append(_git_ignored_exclusion(relative))
                continue
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ReleaseCodeCollectionError(f"refusing symbolic link in release code: {relative}")
            if stat.S_ISREG(entry_stat.st_mode):
                files.append(
                    SelectedFile(
                        relative_path=relative,
                        mode=stat.S_IMODE(entry_stat.st_mode),
                        sha256=self._sha256(path),
                    )
                )
            else:
                raise ReleaseCodeCollectionError(
                    f"refusing non-regular release-code entry: {relative}"
                )

    def _verify_walk(self, root: Path, directory: Path, actual: list[str]) -> None:
        value = self._lstat(directory, "materialized code directory")
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise ReleaseCodeCollectionError(f"materialized code directory is unsafe: {directory}")
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ReleaseCodeCollectionError(f"cannot enumerate materialized {directory}: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            value = self._lstat(path, "materialized code entry")
            if stat.S_ISLNK(value.st_mode):
                raise ReleaseCodeCollectionError(f"materialized code contains a link: {path}")
            if stat.S_ISDIR(value.st_mode):
                self._verify_walk(root, path, actual)
            elif stat.S_ISREG(value.st_mode):
                actual.append(path.relative_to(root).as_posix())
            else:
                raise ReleaseCodeCollectionError(f"materialized code contains nonregular entry: {path}")

    def _environment_evidence(self, directory: Path) -> dict[str, object] | None:
        """Recognize an environment only from a config plus interpreter layout."""
        config = directory / "pyvenv.cfg"
        bin_dir = directory / "bin"
        config_stat = self._optional_lstat(config)
        bin_stat = self._optional_lstat(bin_dir)
        if config_stat is None and bin_stat is None:
            return None
        if config_stat is None or bin_stat is None:
            raise ReleaseCodeCollectionError(
                f"incomplete Python environment evidence in {directory.relative_to(self._source_root)}"
            )
        self._assert_real_regular(config, config_stat, "pyvenv.cfg evidence")
        self._assert_real_directory(bin_dir, bin_stat, "interpreter layout")
        interpreters = self._interpreter_evidence(bin_dir)
        if not interpreters:
            raise ReleaseCodeCollectionError(
                f"pyvenv.cfg without interpreter layout is ambiguous: {directory.relative_to(self._source_root)}"
            )
        return {
            "pyvenv_cfg": "regular_file",
            "bin_directory": "real_directory",
            "interpreters": interpreters,
        }

    def _interpreter_evidence(self, bin_dir: Path) -> list[str]:
        interpreters: list[str] = []
        for candidate in (bin_dir / "python", bin_dir / "python3"):
            try:
                candidate_stat = os.lstat(candidate)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ReleaseCodeCollectionError(f"cannot inspect {candidate}: {exc}") from exc
            if not (stat.S_ISREG(candidate_stat.st_mode) or stat.S_ISLNK(candidate_stat.st_mode)):
                raise ReleaseCodeCollectionError(
                    f"ambiguous interpreter evidence in {candidate.relative_to(self._source_root)}"
                )
            interpreters.append(candidate.name)
        return interpreters

    def _git_identity(self) -> tuple[str | None, str]:
        dot_git = self._source_root / ".git"
        known_checkout = dot_git.exists() or dot_git.is_symlink()
        result = self._git("rev-parse", "--verify", "HEAD")
        if result.returncode == 0:
            head = result.stdout.strip()
            if len(head) != 40:
                raise ReleaseCodeCollectionError(f"git returned malformed HEAD: {head!r}")
            return head, "git"
        if known_checkout:
            raise ReleaseCodeCollectionError(
                f"git failed for known checkout {self._source_root}: {result.stderr.strip()}"
            )
        return None, "nogit"

    def _assert_exclusion_untracked(
        self, relative_dir: str, git_head: str | None, git_state: str
    ) -> None:
        if git_state == "nogit":
            return
        if git_head is None:
            raise ReleaseCodeCollectionError("git state is inconsistent")
        index_members, head_members = self._excluded_git_members(relative_dir)
        if index_members or head_members:
            raise ReleaseCodeCollectionError(
                f"refusing to exclude tracked or staged environment {relative_dir}: "
                f"index_members={len(index_members)}, head_members={len(head_members)}"
            )

    def _excluded_git_members(self, relative_dir: str) -> tuple[list[str], list[str]]:
        index = self._git("ls-files", "--stage", "--", relative_dir)
        head = self._git("ls-tree", "-r", "--name-only", "HEAD", "--", relative_dir)
        if index.returncode != 0 or head.returncode != 0:
            raise ReleaseCodeCollectionError(
                f"git membership check failed for excluded environment {relative_dir}: "
                f"index={index.stderr.strip()!r} head={head.stderr.strip()!r}"
            )
        return (
            [line for line in index.stdout.splitlines() if line],
            [line for line in head.stdout.splitlines() if line],
        )

    def _artifact_head_equality(self, plan: CollectionPlan) -> dict[str, object]:
        if plan.git_state == "nogit" or plan.git_head is None:
            return {"state": "unknown", "reason": "gitless_source"}
        self.verify_source_identity(plan)
        head_population = self._head_population(plan.code_subtrees)
        if head_population is None:
            return {
                "state": "unknown",
                "head": plan.git_head,
                "reason": "head_population_unavailable",
                "mismatches": [],
            }

        artifact_population = {item.relative_path: item for item in plan.files}
        mismatches = self._membership_mismatches(head_population, artifact_population)
        content_mismatches, unavailable_path = self._content_and_mode_mismatches(
            head_population, artifact_population
        )
        if unavailable_path is not None:
            return {
                "state": "unknown",
                "head": plan.git_head,
                "reason": "head_blob_unavailable",
                "path": unavailable_path,
                "mismatches": mismatches,
            }
        mismatches.extend(content_mismatches)
        self.verify_source_identity(plan)
        return {
            "state": "equal" if not mismatches else "unequal",
            "head": plan.git_head,
            "mismatches": mismatches,
        }

    @staticmethod
    def _membership_mismatches(
        head_population: dict[str, str], artifact_population: dict[str, SelectedFile]
    ) -> list[dict[str, object]]:
        """Name each membership disagreement in stable path order."""
        head_paths = set(head_population)
        artifact_paths = set(artifact_population)
        missing: list[dict[str, object]] = [
            {"kind": "missing_from_artifact", "path": relative_path}
            for relative_path in sorted(head_paths - artifact_paths)
        ]
        extra: list[dict[str, object]] = [
            {"kind": "extra_in_artifact", "path": relative_path}
            for relative_path in sorted(artifact_paths - head_paths)
        ]
        return missing + extra

    def _content_and_mode_mismatches(
        self, head_population: dict[str, str], artifact_population: dict[str, SelectedFile]
    ) -> tuple[list[dict[str, object]], str | None]:
        """Compare common paths and preserve an unavailable HEAD blob as unknown."""
        mismatches: list[dict[str, object]] = []
        for relative_path in sorted(set(head_population) & set(artifact_population)):
            result = self._git_bytes("show", f"HEAD:{relative_path}")
            if result.returncode != 0:
                return mismatches, relative_path
            item = artifact_population[relative_path]
            if hashlib.sha256(result.stdout).hexdigest() != item.sha256:
                mismatches.append({"kind": "bytes_differ", "path": relative_path})
            mode_mismatch = self._executable_mode_mismatch(
                relative_path, head_population[relative_path], item
            )
            if mode_mismatch is not None:
                mismatches.append(mode_mismatch)
        return mismatches, None

    @staticmethod
    def _executable_mode_mismatch(
        relative_path: str, head_mode: str, item: SelectedFile
    ) -> dict[str, object] | None:
        """Return evidence only when Git's executable bit disagrees."""
        if (head_mode == "100755") == bool(item.mode & 0o111):
            return None
        return {
            "kind": "executable_mode_differs",
            "path": relative_path,
            "head_mode": head_mode,
            "artifact_mode": f"{item.mode:04o}",
        }

    def _head_population(self, code_subtrees: tuple[str, ...]) -> dict[str, str] | None:
        """Read the complete tracked HEAD population under the release roots.

        Git's executable model has only the regular-file modes 100644 and
        100755.  The release manifest preserves the materialized POSIX mode,
        but this comparison intentionally checks only that Git executable bit;
        it is not a general permissions policy.
        """
        result = self._git_bytes("ls-tree", "-r", "-z", "HEAD", "--", *code_subtrees)
        if result.returncode != 0:
            return None
        population: dict[str, str] = {}
        try:
            for raw_entry in result.stdout.split(b"\0"):
                if not raw_entry:
                    continue
                metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
                mode, object_type, _object_id = metadata.split(maxsplit=2)
                if object_type not in (b"blob", b"commit"):
                    return None
                relative_path = os.fsdecode(raw_path)
                if relative_path in population:
                    return None
                population[relative_path] = mode.decode("ascii")
        except (UnicodeDecodeError, ValueError):
            return None
        return population

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(self._source_root), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseCodeCollectionError(f"git {' '.join(args)} failed: {exc}") from exc

    def _git_bytes(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", "-C", str(self._source_root), *args],
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseCodeCollectionError(f"git {' '.join(args)} failed: {exc}") from exc

    def _cow_copy(self, source: Path, target: Path) -> None:
        try:
            result = subprocess.run(
                [self._cp_binary, "-c", str(source), str(target)],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._clone_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseCodeCollectionError(f"cp -c {source} -> {target} failed: {exc}") from exc
        if result.returncode != 0:
            raise ReleaseCodeCollectionError(
                f"cp -c {source} -> {target} exited {result.returncode}: {result.stderr.strip()}"
            )

    @staticmethod
    def _lstat(path: Path, label: str) -> os.stat_result:
        try:
            return os.lstat(path)
        except OSError as exc:
            raise ReleaseCodeCollectionError(f"cannot inspect {label} {path}: {exc}") from exc

    @staticmethod
    def _optional_lstat(path: Path) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ReleaseCodeCollectionError(f"cannot inspect possible environment {path}: {exc}") from exc

    @staticmethod
    def _assert_real_regular(path: Path, value: os.stat_result, label: str) -> None:
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise ReleaseCodeCollectionError(f"ambiguous {label} in {path}")

    @staticmethod
    def _assert_real_directory(path: Path, value: os.stat_result, label: str) -> None:
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            raise ReleaseCodeCollectionError(f"ambiguous {label} in {path}")

    def _regular_stat(self, path: Path, label: str) -> os.stat_result:
        value = self._lstat(path, label)
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise ReleaseCodeCollectionError(f"refusing non-regular file: {label}")
        return value

    def _sha256(self, path: Path) -> str:
        self._regular_stat(path, str(path))
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise ReleaseCodeCollectionError(f"cannot hash {path}: {exc}") from exc
        return digest.hexdigest()


def write_selected_file_manifest(path: Path, payload: dict[str, object]) -> str:
    """Write the canonical selected-file manifest and return its SHA-256."""
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()
