"""Declared-vs-actual release identity comparators (publish_release design §7.4).

Every release ships two declarations of what the customer installed -- the
manager receipt (``share/solet/install-source.json``: ``source_commit``) and,
once the stage-5 draft lands beside it, ``release_manifest.json`` (schema v1,
§7.1: per-file digests of the manager payload, the seed's commit and tree,
per-plugin subtree hashes, the surface digest a running solet reports).
Until this module nothing read them back (``iss_670801d4``): a declaration
nobody compares is a field that will eventually lie.

This module is the read-back.  Each comparator takes ONE side from a real
measurement (a sha256 of installed bytes, ``git rev-parse`` on the checkout,
the running process's own ``attest_runtime_code`` answer) and the other from
the manifest, and grades ``verified`` / ``drifted`` / ``unattestable``.  An
``unattestable`` outcome always carries the reason it could not measure --
``release_manifest_absent``, ``no_git_metadata``, ``solet_not_running`` --
because an unmeasured comparison is not a passing one (the false-green shape
the doctor campaign exists to remove).

What this is NOT: cryptographic.  The manifest carries no signature today
(``factory_signature`` is ``null``, the same reservation ``PROVENANCE.json``
holds for a future factory key), so a hostile producer could write a
manifest that matches a hostile payload.  The threat these comparators
answer is mistakes and drift -- the wrong payload staged, a keg edited in
place, a plugin subtree advanced by hand -- and every emitted document says
so in :data:`NOT_CRYPTOGRAPHIC` so a reader never mistakes a real hash for a
signature.

Pure and seam-injected: the git and process runners are parameters so the
smokes drive every branch from constructed fixture trees, never a real keg.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import SourceError, UpdateBlockedError
from .models import JsonValue
from .target_git import run_target_git

ATTESTATION_FORMAT = "installation-attestation-v1"
RELEASE_MANIFEST_NAME = "release_manifest.json"
INSTALL_SOURCE_NAME = "install-source.json"
RELEASE_MANIFEST_SCHEMA_VERSION = 1
INSTALL_SOURCE_SCHEMA_VERSION = 1

NOT_CRYPTOGRAPHIC = (
    "NOT CRYPTOGRAPHIC: every comparison here is a real hash of installed "
    "bytes against a manifest that carries no signature (factory_signature "
    "is null, the same reservation PROVENANCE.json.signature holds). It "
    "detects mistakes and drift, not a producer that lies consistently."
)

STATUS_VERIFIED = "verified"
STATUS_DRIFTED = "drifted"
STATUS_UNATTESTABLE = "unattestable"

# The exact closed key set of a schema-v1 manifest (§7.1). The stage-5 draft
# and a finalised manifest both carry precisely these keys; sections a later
# stage owns are ``null``, never absent.
MANIFEST_V1_KEYS = frozenset(
    {
        "schema_version",
        "release_label",
        "manager_release_tag",
        "seed",
        "components",
        "bundle_verdict",
        "guest_validation",
        "manager",
        "tap",
        "surface_digests",
        "produced_by",
        "factory_signature",
    }
)
_INSTALL_SOURCE_KEYS = frozenset({"schema_version", "mode", "source_commit"})
_INSTALL_MODES = frozenset({"release", "dev"})
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

# The payload member prefix under which the installed package's files were
# digested at stage 5 (``git archive`` of ``solet_cli``), so the on-disk walk
# and the manifest hash the same member set (Fable review concern 2).
MANAGER_PAYLOAD_PREFIX = "solet_cli/src/solet_manager/"
_IGNORED_DIRECTORIES = frozenset({"__pycache__"})
_IGNORED_SUFFIXES = (".pyc", ".pyo")
_PLUGIN_COMPONENT_PREFIX = "plugin:"

_GIT_TIMEOUT_S = 30
_GIT_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
}


@dataclass(frozen=True)
class CommandOutcome:
    """The bounded result of one read-only subprocess query."""

    returncode: int
    stdout: str
    stderr: str


type CommandRunner = Callable[[Sequence[str], Path | None, int], CommandOutcome]


@dataclass(frozen=True)
class InstallSource:
    """The Formula-written manager receipt: how this keg's payload was cut."""

    path: Path
    mode: str
    source_commit: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"path": str(self.path), "mode": self.mode, "source_commit": self.source_commit}


def run_command(argv: Sequence[str], cwd: Path | None, timeout: int) -> CommandOutcome:
    """Run a fixed read-only vector with no inherited Git redirection."""

    if argv and argv[0] == "git":
        return _run_target_git(argv, cwd, timeout)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(_GIT_ENVIRONMENT)
    try:
        completed = subprocess.run(  # noqa: S603 - vectors are closed by the callers
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandOutcome(1, "", str(exc))
    return CommandOutcome(completed.returncode, completed.stdout, completed.stderr)


def _run_target_git(argv: Sequence[str], cwd: Path | None, timeout: int) -> CommandOutcome:
    """A target Git vector through the shared hardened surface; a refusal is a failed, explained outcome."""
    try:
        completed = run_target_git(argv[1:], cwd=cwd, timeout=timeout, inherit_environment=True)
    except (OSError, subprocess.TimeoutExpired, UpdateBlockedError) as exc:
        return CommandOutcome(1, "", str(exc))
    return CommandOutcome(completed.returncode, completed.stdout.decode("utf-8", "replace"), completed.stderr.decode("utf-8", "replace"))


def load_release_manifest(path: Path) -> dict[str, JsonValue]:
    """Read one schema-v1 release manifest; any drift from §7.1 is refused."""

    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"release manifest is unreadable at {path}: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise SourceError(f"release manifest must be one JSON object: {path}")
    manifest = cast(dict[str, JsonValue], raw)
    if frozenset(manifest) != MANIFEST_V1_KEYS:
        raise SourceError(
            f"release manifest must contain exactly {', '.join(sorted(MANIFEST_V1_KEYS))}: {path}"
        )
    if manifest["schema_version"] != RELEASE_MANIFEST_SCHEMA_VERSION:
        raise SourceError(
            f"release manifest schema_version must be {RELEASE_MANIFEST_SCHEMA_VERSION}: {path}"
        )
    return manifest


def manifest_sha256(manifest: dict[str, JsonValue]) -> str:
    """Canonical (sorted-key, compact) digest -- the identity stage 13 records."""

    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def load_install_source(path: Path) -> InstallSource:
    """Read the Formula-written receipt; a malformed receipt is refused, not guessed."""

    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"install-source receipt is unreadable at {path}: {exc}") from exc
    if not isinstance(raw, dict) or frozenset(cast(dict[object, object], raw)) != _INSTALL_SOURCE_KEYS:
        raise SourceError(
            f"install-source receipt must contain exactly {', '.join(sorted(_INSTALL_SOURCE_KEYS))}: {path}"
        )
    receipt = cast(dict[str, object], raw)
    mode = receipt["mode"]
    commit = receipt["source_commit"]
    if receipt["schema_version"] != INSTALL_SOURCE_SCHEMA_VERSION:
        raise SourceError(f"install-source schema_version must be {INSTALL_SOURCE_SCHEMA_VERSION}: {path}")
    if not isinstance(mode, str) or mode not in _INSTALL_MODES:
        raise SourceError(f"install-source mode must be one of {sorted(_INSTALL_MODES)}: {path}")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise SourceError(f"install-source source_commit must be a 40-hex commit: {path}")
    return InstallSource(path, mode, commit)


def installed_file_digests(package_root: Path) -> dict[str, str]:
    """Real sha256 of every regular file under the installed package.

    Keys are payload member paths (``solet_cli/src/solet_manager/<relative>``)
    so they compare directly against ``manifest.manager.file_digests``.
    Bytecode caches and ``__pycache__`` are the installer's, not the
    payload's, and are excluded on both sides by construction.
    """

    digests: dict[str, str] = {}
    for directory, subdirectories, files in os.walk(package_root):
        subdirectories[:] = sorted(name for name in subdirectories if name not in _IGNORED_DIRECTORIES)
        for name in sorted(files):
            if name.endswith(_IGNORED_SUFFIXES):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(package_root).as_posix()
            digests[MANAGER_PAYLOAD_PREFIX + relative] = _file_sha256(path)
    return digests


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def compare_manager(
    installed: dict[str, str],
    install_source: InstallSource | None,
    manifest: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """Grade the installed manager package against the manifest's declaration."""

    section: dict[str, JsonValue] = {
        "status": STATUS_UNATTESTABLE,
        "reason": None,
        "install_source": None if install_source is None else install_source.to_dict(),
        "declared_source_commit": None,
        "files_hashed": len(installed),
        "file_digests": cast(dict[str, JsonValue], dict(installed)),
        "drifted_paths": [],
        "missing_paths": [],
        "unexpected_paths": [],
        "source_commit_matches": None,
    }
    declared = _manager_declaration(manifest)
    if declared is None:
        section["reason"] = "release_manifest_absent" if manifest is None else "manifest_manager_section_absent"
        return section
    declared_commit = declared.get("source_commit")
    section["declared_source_commit"] = declared_commit if isinstance(declared_commit, str) else None
    expected = _declared_manager_digests(declared)
    if expected is None:
        section["reason"] = "manifest_file_digests_absent"
        return section
    section.update(_path_differences(installed, expected))
    _grade_manager(section, install_source, declared_commit)
    return section


def _path_differences(installed: dict[str, str], expected: dict[str, str]) -> dict[str, JsonValue]:
    """Localise drift: same path different bytes, declared but absent, present but undeclared."""

    return {
        "drifted_paths": cast(list[JsonValue], sorted(path for path, digest in expected.items() if path in installed and installed[path] != digest)),
        "missing_paths": cast(list[JsonValue], sorted(path for path in expected if path not in installed)),
        "unexpected_paths": cast(list[JsonValue], sorted(path for path in installed if path not in expected)),
    }


def _declared_manager_digests(declared: dict[str, JsonValue]) -> dict[str, str] | None:
    digests = declared.get("file_digests")
    if not isinstance(digests, dict):
        return None
    return {path: digest for path, digest in digests.items() if path.startswith(MANAGER_PAYLOAD_PREFIX) and isinstance(digest, str)}


def _grade_manager(section: dict[str, JsonValue], install_source: InstallSource | None, declared_commit: JsonValue) -> None:
    clean = not section["drifted_paths"] and not section["missing_paths"] and not section["unexpected_paths"]
    if install_source is None:
        section["reason"] = "install_source_absent"
        section["status"] = STATUS_UNATTESTABLE if clean else STATUS_DRIFTED
        return
    commit_matches = install_source.source_commit == declared_commit
    section["source_commit_matches"] = commit_matches
    if clean and commit_matches:
        section["status"], section["reason"] = STATUS_VERIFIED, None
        return
    section["status"] = STATUS_DRIFTED
    section["reason"] = "file_digest_drift" if not clean else "source_commit_mismatch"


def _manager_declaration(manifest: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
    if manifest is None:
        return None
    declared = manifest.get("manager")
    return declared if isinstance(declared, dict) else None


def compare_seed_checkout(
    target: Path,
    manifest: dict[str, JsonValue] | None,
    runner: CommandRunner = run_command,
) -> dict[str, JsonValue]:
    """Measure the checkout with git and localise drift per declared component.

    A checkout without ``.git`` cannot be measured -- the manager acquires
    seeds by ``git fetch`` and verifies tree hashes, so a missing ``.git`` is
    a real finding (``no_git_metadata``), reported rather than guessed around.
    """

    section: dict[str, JsonValue] = {
        "status": STATUS_UNATTESTABLE,
        "reason": None,
        "target": str(target),
        "head_commit": None,
        "head_tree": None,
        "dirty_paths": [],
        "declared": None,
        "components": [],
        "drifted_components": [],
    }
    if not (target / ".git").exists():
        section["reason"] = "no_git_metadata"
        return section
    measured = _measure_checkout(runner, target)
    if measured is None:
        section["reason"] = "git_query_failed"
        return section
    head, tree, dirty = measured
    section["head_commit"], section["head_tree"] = head, tree
    section["dirty_paths"] = cast(list[JsonValue], dirty)
    seed = manifest.get("seed") if manifest is not None else None
    if not isinstance(seed, dict):
        section["reason"] = "release_manifest_absent" if manifest is None else "manifest_seed_section_absent"
        return section
    section["declared"] = {"commit": seed.get("commit"), "tree_hash": seed.get("tree_hash")}
    components, drifted = _compare_components(runner, target, manifest)
    section["components"] = cast(list[JsonValue], components)
    section["drifted_components"] = cast(list[JsonValue], drifted)
    _grade_checkout(section, head == seed.get("commit") and tree == seed.get("tree_hash"), bool(dirty), bool(drifted))
    return section


def _measure_checkout(runner: CommandRunner, target: Path) -> tuple[str, str, list[str]] | None:
    head = _git(runner, target, "rev-parse", "HEAD")
    tree = _git(runner, target, "rev-parse", "HEAD^{tree}")
    # Raw stdout: the porcelain's leading status column may be a space.
    status = runner(("git", "-C", str(target), "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=no"), None, _GIT_TIMEOUT_S)
    if head is None or tree is None or status.returncode != 0:
        return None
    return head, tree, _dirty_paths(status.stdout)


def _dirty_paths(porcelain: str) -> list[str]:
    """Tracked paths with index or worktree changes, from ``status --porcelain -z``.

    Each entry is ``XY <path>``; a rename or copy carries the original path
    as one extra NUL-terminated field, which is consumed, not listed.
    """

    fields = iter(field for field in porcelain.split("\0") if field)
    dirty: list[str] = []
    for entry in fields:
        dirty.append(entry[3:])
        if entry[0] in "RC":
            next(fields, None)
    return dirty


def _grade_checkout(section: dict[str, JsonValue], identity_matches: bool, dirty: bool, drifted: bool) -> None:
    if identity_matches and not dirty and not drifted:
        section["status"], section["reason"] = STATUS_VERIFIED, None
        return
    section["status"] = STATUS_DRIFTED
    if not identity_matches:
        section["reason"] = "seed_identity_mismatch"
    else:
        section["reason"] = "working_tree_dirty" if dirty else "component_subtree_drift"


def _compare_components(
    runner: CommandRunner, target: Path, manifest: dict[str, JsonValue] | None
) -> tuple[list[dict[str, JsonValue]], list[str]]:
    declared = manifest.get("components") if manifest is not None else None
    if not isinstance(declared, list):
        return [], []
    rows = [row for row in (_component_row(runner, target, item) for item in declared) if row is not None]
    drifted = [cast(str, row["component"]) for row in rows if row["status"] in {STATUS_DRIFTED, "absent"}]
    return rows, drifted


def _component_row(runner: CommandRunner, target: Path, item: JsonValue) -> dict[str, JsonValue] | None:
    if not isinstance(item, dict) or not isinstance(item.get("component"), str):
        return None
    component = cast(str, item["component"])
    expected = item.get("subtree_hash")
    row: dict[str, JsonValue] = {
        "component": component,
        "declared_subtree_hash": expected if isinstance(expected, str) else None,
        "observed_subtree_hash": None,
        "status": "not_compared",
    }
    if not component.startswith(_PLUGIN_COMPONENT_PREFIX) or not isinstance(expected, str):
        return row
    observed = _git(runner, target, "rev-parse", f"HEAD:plugins/{component.removeprefix(_PLUGIN_COMPONENT_PREFIX)}")
    row["observed_subtree_hash"] = observed
    row["status"] = "absent" if observed is None else (STATUS_VERIFIED if observed == expected else STATUS_DRIFTED)
    return row


def _git(runner: CommandRunner, target: Path, *arguments: str) -> str | None:
    outcome = runner(("git", "-C", str(target), "--no-optional-locks", *arguments), None, _GIT_TIMEOUT_S)
    return outcome.stdout.strip() if outcome.returncode == 0 else None


def compare_runtime(
    observed: dict[str, JsonValue] | None,
    reason: str | None,
    manifest: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """Grade what the RUNNING process reports against the manifest's surface digest."""

    section: dict[str, JsonValue] = {
        "status": STATUS_UNATTESTABLE,
        "reason": reason,
        "observed": observed,
        "declared_release_surface_sha256": None,
    }
    if observed is None:
        return section
    surfaces = manifest.get("surface_digests") if manifest is not None else None
    declared = surfaces.get("release_surface_sha256") if isinstance(surfaces, dict) else None
    if not isinstance(declared, str):
        section["reason"] = "release_manifest_absent" if manifest is None else "manifest_surface_digests_absent"
        return section
    section["declared_release_surface_sha256"] = declared
    matches = observed.get("release_surface_sha256") == declared
    section["status"] = STATUS_VERIFIED if matches else STATUS_DRIFTED
    section["reason"] = None if matches else "release_surface_mismatch"
    return section


__all__ = [
    "ATTESTATION_FORMAT",
    "INSTALL_SOURCE_NAME",
    "MANAGER_PAYLOAD_PREFIX",
    "MANIFEST_V1_KEYS",
    "NOT_CRYPTOGRAPHIC",
    "RELEASE_MANIFEST_NAME",
    "STATUS_DRIFTED",
    "STATUS_UNATTESTABLE",
    "STATUS_VERIFIED",
    "CommandOutcome",
    "CommandRunner",
    "InstallSource",
    "compare_manager",
    "compare_runtime",
    "compare_seed_checkout",
    "installed_file_digests",
    "load_install_source",
    "load_release_manifest",
    "manifest_sha256",
    "run_command",
]
