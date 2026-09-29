"""Step-7 fixture support: the real-style ``macos-bizops`` clone, the cold host, the fake keg, the Manager subprocess.

Design: the Step 7 existing-solet-import design (workbench, 2026-09-19; revision 4,
sha256 ``edd5e87a...``), sections 3, 4 and 8.2.  Imports Step 5/6 support unchanged
and adds what a seed-born clone actually looks like -- the fact Step 7 turns on.

**The seed is the seed factory's own output.**  ``build_real_style`` calls
``seed_factory_plugin.assemble.assemble("macos-bizops", ...)`` against the
checkout under test (read-only: a ``git archive`` of ``HEAD``), once per smoke
process, then seals it with Step 5's ``seal()`` as the baseline and again as the
append-only candidate.  Because ``assemble()`` reads the committed ``HEAD``,
uncommitted edits in a fix lane are not in the seed; the smokes assert shape and
attribution, never seed content.

**The clone is what genesis and ``solet create`` leave.**  After the clone, the
seed's own writers are imported and called in ``run_genesis`` order (never a
byte-identical reproduction, never a re-run of genesis itself, which needs
Postgres/Keychain/launchd), then ``hydration::shell.install``'s rendered files
for the ``solet create`` shape, then the operator's edits.  Measured on
``c33c11f32`` (M2, section 3.3), the ``solet create`` shape reads::

     M AGENTS.md
     M CLAUDE.md
     M root_manifest.yaml
    ?? .gitignore
    ?? .solet/genesis.json
    ?? knowledge_bases/<19 relative symlinks, one per KB manifest name>
    ?? profile/config/<11 regular files: identity, service bindings, starting
       actions, prompts/system.json, six plugin configs, the address book>

3 tracked modifications and 32 untracked entries (19 symlinks), plus three
ignored entries (``profile/config/manifest.yaml``, the vault passphrase, the
genesis attempt marker) and, after ``solet create``, seven ignored ``client/**``
and marketplace files.  The count is bundle-dependent; the builder asserts the
**attribution equality** ``set(porcelain) == expected(writers) - ignored`` in
both directions over the writers' own returned path maps, and that
``GENESIS_STEP_RUNNERS`` still names exactly the six spine steps it calls, so a
seventh step added later fails the fixture instead of silently changing the
shape.

Nothing here opens a database connection; ``db_spy`` proves it for every caller.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import (  # noqa: E402
    CANONICAL,
    HOOKS,
    KB,
    ORIGIN_ID,
    FakeHost,
    Fixture,
    Release,
    SeedRuntime,
    _closure_runner,
    bundle_digest,
    bundle_document,
    bundle_files,
    candidate_tree_files,
    default_operations,
    descriptor,
    git,
    installed,
    seal,
)
from _step6_support import byte_map  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, bounded_command_outcome  # noqa: E402
from solet_manager._existing_install_inspection_metadata import InstalledUpdateDescriptor  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ChannelRelation,
    InspectionAnchor,
    InspectionAnchorKind,
    InstalledInspectionMetadata,
)
from solet_manager.maintenance_inventory import read_maintenance_inventory_v2, write_maintenance_inventory_v2  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.update_execution import UpdateRequest, apply_update, preview_update_instance  # noqa: E402
from solet_manager.update_runtime_plan import RuntimeSeams  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "plugins" / "seed_factory_plugin" / "src"))

__all__ = [
    "GENESIS_WRITERS",
    "NAME",
    "PROFILE",
    "ColdHost",
    "FakeKeg",
    "Knobs",
    "OpaqueStores",
    "RealStyleFixture",
    "assembled_seed",
    "build_keg",
    "build_real_style",
    "cli",
    "declare_router",
    "import_metadata",
    "make_offline",
    "manager_subprocess",
    "porcelain",
    "prime_genesis_imports",
    "tree_snapshot",
]

NAME = "bizops"
PROFILE = "macos-bizops"
KB_ROOT = Path(KB)
NEW_PLUGIN = "new_plugin"
NOTES_PATH = "workbench/2026-09-01_operator_notes.md"
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
#: The six spine steps ``run_genesis`` executes, in order (``steps.py:GENESIS_STEP_RUNNERS``); the builder asserts this.
GENESIS_WRITERS = ("validate_name", "resolve_target", "materialize_configs", "seed_root_manifest", "materialize_kb_symlinks", "write_manifest_marker")
_REMOVED_ARTICLE = f"{KB}/07_upstream_feedback_runbook.md"
_SEED_CACHE: dict[str, Path] = {}
_SOURCE_CACHE: dict[tuple[object, ...], tuple[Path, Release, Release, dict[str, bytes], str]] = {}


@dataclass(frozen=True)
class Knobs:
    """The cold-host / topology / tree switches (section 3.5); every default is the ``solet create`` real-style shape."""

    birth: Literal["create", "bootstrap"] = "create"
    birth_failed_after_passphrase: bool = False
    legacy_root_manifest: bool = False
    connector_missing_export_root: bool = False
    operator_edits: bool = True
    tracked_edits: bool = True
    kb_addition: Literal["on_roster", "off_roster"] | None = None
    overlap: bool = False
    untracked_collision: bool = False
    staged: bool = False
    executed_code_edit: Literal["bootstrap", "bootstrap_adapter", "ananta", "macos_vault", "untracked_in_midwife"] | None = None
    git_metadata: Literal["gitattributes_untracked", "gitattributes_tracked_edit"] | None = None
    shape_change: Literal["delete", "mode", "symlink"] | None = None
    instance_python: Literal["present", "dangling", "absent"] = "present"
    host_python: Literal["present", "absent", "unqueryable"] = "present"
    homebrew: Literal["present", "absent"] = "present"
    tmux: Literal["present", "absent"] = "present"
    postgres_client: Literal["present", "absent"] = "present"
    service: Literal["healthy", "offline", "unhealthy"] = "healthy"
    keychain: Literal["present", "absent", "unqueryable"] = "present"
    permissions: Literal["unqueryable"] = "unqueryable"
    manager_state: Literal["cold", "enrolled", "verified"] = "cold"
    knowledge_removal: bool = False
    journal_version: Literal[5, 4] = 5
    router: bool = False
    #: The shipped bundle's own lifecycle strategy (``router_preferred``) with NO declared router: what every stable create has
    #: (iss_6fd900ab).  The default seals ``single_color_required``, which routes around the router branch a real solet takes.
    shipped_strategy: bool = False
    genesis_untracked: bool = True
    #: Extra ``(path, content)`` files the candidate ships (iss_f1d8cfc2: a changed coordination-hook manifest).
    candidate_files: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OpaqueStores:
    """4 KiB of ``os.urandom`` each, written once; every scenario asserts their digests unchanged (A8: a surrogate)."""

    keychain: Path
    postgres: Path

    def digests(self) -> dict[str, str]:
        return {"keychain": hashlib.sha256(self.keychain.read_bytes()).hexdigest(), "postgres": hashlib.sha256(self.postgres.read_bytes()).hexdigest()}


@dataclass(frozen=True)
class ColdHost:
    """The injected host: a ``bin`` directory the knobs populate with stub executables, and the two seams over it."""

    root: Path
    knobs: Knobs

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    def resolve_base_python(self) -> Path | None:
        if self.knobs.host_python == "unqueryable":
            raise OSError("host python probe unqueryable")
        candidate = self.bin / "python3.13"
        return candidate if candidate.is_file() else None

    def which(self, name: str) -> str | None:
        if "/" in name:
            return None
        candidate = self.bin / name
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


@dataclass(frozen=True)
class FakeKeg:
    """The formula's effect from a real wheel (section 8.2): an installed Manager in a prefix with its seed lock."""

    prefix: Path
    python: Path
    bin: Path
    share: Path
    site: Path
    version: str

    def layout(self) -> tuple[str, ...]:
        """The listing section 8.1 binds the unit slice and the VM slice to."""
        rows = [
            "bin/solet",
            "bin/solet-manager",
            "share/solet/seed.lock.json",
            "share/solet/existing_install_inspection_seed_lock_catalog.v1.json",
            "share/solet/install-source.json",
        ]
        rows.extend(sorted(str(item.relative_to(self.prefix)) for item in self.site.glob("solet_cli-*.dist-info/RECORD")))
        return tuple(rows)


@dataclass
class RealStyleFixture(Fixture):
    """The Step-5 ``Fixture`` plus the real-style facts (section 3.1)."""

    name: str = NAME
    keg: FakeKeg | None = None
    stores: OpaqueStores | None = None
    tracked_edits: dict[str, str] = field(default_factory=lambda: {})
    untracked: tuple[str, ...] = ()
    genesis_marker: Path = Path("/fixture/.solet/genesis.json")
    named_launcher: Path = Path("/fixture/.local/bin/bizops")
    router: bool = False
    knobs: Knobs = field(default_factory=Knobs)
    cold_host: ColdHost | None = None
    seed_commit: str = ""
    attribution: dict[str, str] = field(default_factory=lambda: {})
    import_descriptor: InstalledUpdateDescriptor | None = None
    #: Every ``python -m venv`` the fake closure runner performed (CH-10/11: exactly one rebuild).
    venv_rebuilds: list[list[str]] = field(default_factory=lambda: [])
    #: What the fake service does on its restart (``launchctl bootstrap``): CH-47 rewrites its own config, CH-48
    #: reproduces ``materialize_missing_plugin_kb_symlinks`` in a background thread; ``install_signal`` is the explicit
    #: synchronisation the readiness poll waits on (round-4 note 3: never a sleep).
    on_restart: Callable[[RealStyleFixture], None] | None = None
    install_signal: threading.Event | None = None
    wait_for_install: bool = True
    restart_thread: threading.Thread | None = None

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"local.solet.{self.name}.plist"

    @property
    def manager_home(self) -> Path:
        return self.root / "manager"


# --- the seed ---------------------------------------------------------------------------------------------


def _head_commit() -> str:
    return git(_ROOT, "rev-parse", "HEAD")


def _cache_root() -> Path:
    """The per-user, per-HEAD cross-process cache: the assembled seed and every sealed source are built once per
    checkout commit on this host and reused by every smoke process (the registered battery runs the Step-7 smokes
    twelve-wide, and rebuilding a ~4,000-file seed per process is what pushed them past the gate budget).  Keyed by
    the commit ``assemble()`` reads, so a different tree never reuses it; entries are completed by an atomic rename
    and left for the OS temp cleaner."""
    root = Path(tempfile.gettempdir()) / f"step7-cache-{_head_commit()[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cached(name: str, build: Callable[[Path], None]) -> Path:
    """``<cache>/<name>``, built into a private sibling and renamed into place; a concurrent winner is reused."""
    final = _cache_root() / name
    if (final / ".complete").is_file():
        return final
    staging = Path(tempfile.mkdtemp(prefix=f"{name}.build-", dir=_cache_root()))
    build(staging)
    (staging / ".complete").write_text("complete\n", encoding="utf-8")
    try:
        os.rename(staging, final)
    except OSError:
        shutil.rmtree(staging, True)
        if not (final / ".complete").is_file():
            raise
    return final


def assembled_seed() -> tuple[Path, str]:
    """The seed factory's ``macos-bizops`` bundle from this checkout's ``HEAD``, assembled once per host and commit."""
    if "bundle" not in _SEED_CACHE:
        commit = _head_commit()

        def build(staging: Path) -> None:
            from seed_factory_plugin.assemble import assemble  # noqa: PLC0415 - dev-checkout dependency, not shipped in the wheel

            result = assemble(PROFILE, origin_id=ORIGIN_ID, output_dir=staging / "seed", ref="HEAD", repo_root=_ROOT)
            assert str(result.ref) == commit, (result.ref, commit)

        _SEED_CACHE["bundle"] = _cached("seed", build) / "seed"
        _SEED_CACHE["commit"] = Path(commit)
    return _SEED_CACHE["bundle"], str(_SEED_CACHE["commit"])


def _seed_key(knobs: Knobs) -> tuple[object, ...]:
    return (knobs.git_metadata == "gitattributes_tracked_edit", knobs.router, knobs.shipped_strategy, knobs.knowledge_removal, knobs.overlap, knobs.untracked_collision, knobs.kb_addition is not None, knobs.candidate_files)


def _release_json(release: Release) -> dict[str, str]:
    return {"commit": release.commit, "tree": release.tree, "provenance": release.provenance.decode("utf-8"), "seed_id": release.seed_id, "manifest": release.manifest, "source_commit": release.source_commit, "tag": release.tag}


def _release_from_json(value: dict[str, str]) -> Release:
    return Release(value["commit"], value["tree"], value["provenance"].encode("utf-8"), value["seed_id"], value["manifest"], value["source_commit"], value["tag"])


def sealed_source(knobs: Knobs) -> tuple[Path, Release, Release, dict[str, bytes], str]:
    """The sealed two-release seed repository for the seed-affecting knobs: sealed once per host and commit, copied
    once per process into a private scratch (a smoke may seal a third release onto its copy), never written otherwise."""
    key = _seed_key(knobs)
    if key not in _SOURCE_CACHE:
        name = "source-" + hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:12]

        def build(staging: Path) -> None:
            baseline, candidate, files, contract = _seed_repository(staging / "source", knobs)
            document = {"baseline": _release_json(baseline), "candidate": _release_json(candidate), "files": {name: content.decode("utf-8") for name, content in files.items()}, "contract": contract}
            (staging / "releases.json").write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")

        cached = _cached(name, build)
        scratch = Path(tempfile.mkdtemp(prefix="step7-source-"))
        atexit.register(shutil.rmtree, scratch, True)
        source = scratch / "source"
        shutil.copytree(cached / "source", source, symlinks=True)
        document = json.loads((cached / "releases.json").read_text(encoding="utf-8"))
        files = {name: content.encode("utf-8") for name, content in document["files"].items()}
        _SOURCE_CACHE[key] = (source, _release_from_json(document["baseline"]), _release_from_json(document["candidate"]), files, str(document["contract"]))
    return _SOURCE_CACHE[key]


def _seed_repository(source: Path, knobs: Knobs) -> tuple[Release, Release, dict[str, bytes], str]:
    """Copy the assembled bundle into ``source``, seal r1, then seal the append-only r2 (section 3.2).

    The r2 transition bundle declares the lifecycle strategy the knobs ask for.  A fixture that only stands in for a
    router install (``router``) or for the shipped bundle on a solet with no router identity (``shipped_strategy``)
    seals ``router_preferred``, which is what the real flow declares; every other fixture seals
    ``single_color_required``, the strategy that routes around the router branch a real created solet takes.
    """
    bundle, _ = assembled_seed()
    shutil.copytree(bundle, source, symlinks=True)
    git(source, "init", "--quiet", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    # Sealing two releases of a ~4,000-file tree crosses ``gc.auto``'s loose-object threshold; a detached
    # background ``git gc`` then prunes objects while the first clone reads them (measured 2026-09-19: eight
    # smokes in parallel, every clone "unable to read tree").  The fixture never needs a repack.
    git(source, "config", "gc.auto", "0")
    baseline_files: dict[str, str | bytes] = {}
    if knobs.git_metadata == "gitattributes_tracked_edit":
        baseline_files[".gitattributes"] = "* text=auto\n"
    baseline = seal(source, "a" * 40, "b" * 64, "r1", baseline_files)
    strategy = "router_preferred" if knobs.router or knobs.shipped_strategy else "single_color_required"
    document = bundle_document(baseline, strategy=strategy, knowledge_removals=["github_midwife_plugin"] if knobs.knowledge_removal else None, operations=default_operations())
    files = bundle_files(document)
    contract = bundle_digest(files)
    if knobs.knowledge_removal:
        (source / _REMOVED_ARTICLE).unlink()
    extra: dict[str, str | bytes] = {"RELEASE_NOTES.md": "# Release r2 fixture title\n\nA fixture release for the Step-7 cold-host matrix.\n"}
    if knobs.overlap:
        extra["root_manifest.yaml"] = (source / "root_manifest.yaml").read_text(encoding="utf-8") + "\n# candidate touch\n"
    if knobs.untracked_collision:
        extra[".gitignore"] = "profile/data/\n"
    extra.update(dict(knobs.candidate_files))
    if knobs.kb_addition is not None:
        extra[f"plugins/{NEW_PLUGIN}/knowledge_base/manifest.yaml"] = f"name: {NEW_PLUGIN}\nversion: 1\ndescription: fixture knowledge base\n"
        extra[f"plugins/{NEW_PLUGIN}/knowledge_base/01_article.md"] = "# New plugin article\n\nFixture content.\n"
        extra[f"plugins/{NEW_PLUGIN}/src/{NEW_PLUGIN}/__init__.py"] = "\n"
    candidate = seal(source, "c" * 40, "d" * 64, "r2", candidate_tree_files(files, extra=extra))
    # One pack instead of ~4,400 loose objects: a local clone then copies one file (measured: 1.0s -> 0.3s per
    # fixture on APFS, where hardlinking every object is slower than copying it), and the per-process copy of
    # the sealed source is a handful of files.  Done here, once, inside the cache build; never at clone time.
    git(source, "repack", "-a", "-d", "-q")
    return baseline, candidate, files, contract


# --- the clone -------------------------------------------------------------------------------------------


def _clone(source: Path, target: Path, baseline: Release, canonical: str = CANONICAL) -> None:
    completed = subprocess.run(("git", "clone", "--quiet", "--no-hardlinks", str(source), str(target)), check=False, capture_output=True, text=True, env=_ENV)
    if completed.returncode != 0:
        raise AssertionError(f"fixture clone of {source} failed: {completed.stderr.strip()}")
    git(target, "checkout", "--quiet", "-B", "main", baseline.commit)
    git(target, "remote", "set-url", "origin", canonical)


def _run_genesis_writers(target: Path, knobs: Knobs) -> dict[str, str]:
    """Section 3.3 items 1-7: the seed's own writers in ``run_genesis`` order; returns the expected path -> writer map."""
    sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))
    from github_midwife_plugin import genesis  # noqa: PLC0415
    from github_midwife_plugin.config_materialize import materialize_profile  # noqa: PLC0415
    from github_midwife_plugin.constants import MANIFEST_MARKER_PATH  # noqa: PLC0415
    from github_midwife_plugin.git_init import git_init_worktree  # noqa: PLC0415
    from github_midwife_plugin.kb_symlinks import materialize_kb_symlinks  # noqa: PLC0415
    from github_midwife_plugin.manifest_marker import build_marker_payload, write_marker  # noqa: PLC0415
    from github_midwife_plugin.root_manifest_seed import seed_for_newborn  # noqa: PLC0415
    from github_midwife_plugin.steps import GENESIS_STEP_RUNNERS  # noqa: PLC0415
    from github_midwife_plugin.vault_passphrase_seed import (  # noqa: PLC0415
        clear_vault_passphrase_stale_check_pending,
        seed_vault_passphrase,
        vault_passphrase_path,
        vault_passphrase_stale_check_pending_path,
    )

    assert tuple(name for name, _ in GENESIS_STEP_RUNNERS) == GENESIS_WRITERS, tuple(name for name, _ in GENESIS_STEP_RUNNERS)
    expected: dict[str, str] = {}
    written = materialize_profile(target=target, kb_root=target / KB_ROOT, profile_name=PROFILE, name=NAME)
    for group, paths in written.items():
        for path in paths:
            if Path(path).is_file():
                expected[str(Path(path).relative_to(target))] = f"config_materialize.materialize_profile[{group}]"
    seed_for_newborn(target, NAME)
    expected["root_manifest.yaml"] = "root_manifest_seed.seed_for_newborn (tracked modification)"
    report = materialize_kb_symlinks(target)
    for name in report["created"]:
        expected[f"knowledge_bases/{name}"] = "kb_symlinks.materialize_kb_symlinks (relative symlink)"
    write_marker(target, build_marker_payload(name=NAME, profile_name=PROFILE, steps=[], status="spine_complete"))
    expected[str(MANIFEST_MARKER_PATH)] = "manifest_marker.write_marker"
    seed_vault_passphrase(target)
    expected[str(vault_passphrase_path(target).relative_to(target))] = "vault_passphrase_seed.seed_vault_passphrase"
    if knobs.birth_failed_after_passphrase:
        expected[str(vault_passphrase_stale_check_pending_path(target).relative_to(target))] = "vault_passphrase_seed (stale-check sidecar; failed birth only)"
    else:
        clear_vault_passphrase_stale_check_pending(target)
    git_init_worktree(target, NAME)
    expected[".gitignore"] = "git_init.git_init_worktree"
    genesis._write_genesis_marker(name=NAME, clone_root=target, profile_name=PROFILE, result={"steps": []})  # noqa: SLF001 - genesis is its only writer
    expected[".solet/genesis.json"] = "genesis._write_genesis_marker"
    _evict_platform_state_modules()
    return expected


def prime_genesis_imports() -> None:
    """Import the seed's genesis writers once, outside any ``db_spy``, then evict the platform state modules they pull in.

    A sweep builds fixtures INSIDE the spy; the writers must already be cached so no poisoned module is touched.
    """
    sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))
    import github_midwife_plugin.genesis  # noqa: F401, PLC0415
    import github_midwife_plugin.setup_shell_operations  # noqa: F401, PLC0415
    from github_midwife_plugin import autostart  # noqa: F401, PLC0415

    assembled_seed()
    _evict_platform_state_modules()


def _evict_platform_state_modules() -> None:
    """``github_midwife_plugin.genesis`` (imported only for its marker writer) transitively imports the platform's
    state-management interface; ``db_spy`` proves the MANAGER never imports it, so the fixture's own dev-checkout
    import is evicted from ``sys.modules`` once the writer has run, exactly as if the seed had been born elsewhere."""
    for name in [name for name in sys.modules if name.startswith("ananta.") and "state_management" in name]:
        del sys.modules[name]


def _run_create_shape(target: Path) -> dict[str, str]:
    """Section 3.3 item 8: what ``hydration::shell.install`` writes for the ``solet create`` shape."""
    from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: PLC0415
    from github_midwife_plugin.setup_shell_operations import _rendered_files  # noqa: PLC0415

    request = AdapterRequest(request_id="r", operation_id="o", operation_ref="hydration::shell.install", phase="apply", probe_purpose=None, attempt=1, name=NAME, target=target, flow_source_revision="x", answers_fingerprint="x", approval_fingerprint=None, dry_run=False, timeout_seconds=10, public_inputs={})
    expected: dict[str, str] = {}
    for destination, (content, mode) in _rendered_files(request).items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        destination.chmod(mode)
        expected[str(destination.relative_to(target))] = "setup_shell_operations._rendered_files (solet create)"
    return expected


def porcelain(target: Path, *, ignored: bool = False) -> dict[str, str]:
    """``git status --porcelain=v1 --untracked-files=all`` as ``{path: XY}`` (the leading status space is significant)."""
    args = ["status", "--porcelain=v1", "--untracked-files=all", *(["--ignored"] if ignored else [])]
    completed = subprocess.run(("git", "-C", str(target), *args), check=True, capture_output=True, text=True, env=_ENV)
    return {line[3:]: line[:2] for line in completed.stdout.splitlines() if len(line) > 3}


def _assert_attribution(target: Path, expected: dict[str, str]) -> None:
    """Section 3.3's completeness proof, both directions, on every build (criterion 16)."""
    observed = porcelain(target)
    ignored = {path for path, code in porcelain(target, ignored=True).items() if code == "!!"}
    unattributed = sorted(set(observed) - set(expected))
    unseen = sorted(path for path in expected if path not in observed and path not in ignored)
    assert not unattributed, f"porcelain entries no writer accounts for: {unattributed}"
    assert not unseen, f"writer outputs neither observed nor ignored: {unseen}"


def _apply_knobs(target: Path, knobs: Knobs) -> dict[str, str]:
    """Section 3.3 items 9-10 and the tree knobs; returns the tracked edits' digests."""
    _apply_operator_knobs(target, knobs)
    _apply_local_state_knobs(target, knobs)
    if knobs.kb_addition == "on_roster":
        manifest = target / "profile" / "config" / "manifest.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + f"- {NEW_PLUGIN}\n", encoding="utf-8")
    status = porcelain(target)
    edits: dict[str, str] = {}
    for path, code in status.items():
        full = target / path
        if code != "??" and not full.is_symlink() and full.is_file():
            edits[path] = hashlib.sha256(full.read_bytes()).hexdigest()
    return edits


def _apply_operator_knobs(target: Path, knobs: Knobs) -> None:
    """Items 9-10: the operator's own edits, the legacy root manifest, the one connector that already answers the export root."""
    if knobs.operator_edits:
        notice = target / "NOTICE"
        notice.write_text(notice.read_text(encoding="utf-8") + "\nOperator note: this clone is managed by hand.\n", encoding="utf-8")
        (target / "workbench").mkdir(exist_ok=True)
        (target / NOTES_PATH).write_text("# Operator notes\n\nLocal notes never shipped by a release.\n", encoding="utf-8")
    if knobs.legacy_root_manifest:
        # CH-44: a clone born before the 2026-08-13 rename carries ``homunculus_name: <name>`` (the old midwife's
        # rewrite); ``migration_solet_rename`` renames the key in place -- a declared write to a preserved tracked path.
        manifest = target / "root_manifest.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(f"solet_name: {NAME}", f"homunculus_name: {NAME}", 1), encoding="utf-8")
    if knobs.connector_missing_export_root:
        # CH-45: one installed connector already answers the export root (the runbook's step 4a done once); the
        # other six connectors lack it, so ``migration_export_root_containment`` propagates it to them.
        exports = target.parent / "exports"
        exports.mkdir(exist_ok=True)
        config = target / "profile" / "config" / "plugins" / "jira_plugin.json"
        config.write_text(json.dumps({"export_allowed_roots": [str(exports)]}, indent=2) + "\n", encoding="utf-8")


_GIT_METADATA_BYTES = {"gitattributes_untracked": "* text=auto\n", "gitattributes_tracked_edit": "* text=auto\n*.md text\n"}


def _apply_local_state_knobs(target: Path, knobs: Knobs) -> None:
    """The refusal knobs: staged, executed-code, git-metadata and shape-change edits (sections 6.2 and 5)."""
    if knobs.staged:
        git(target, "add", "NOTICE")
    if knobs.executed_code_edit is not None:
        _executed_code_edit(target, knobs.executed_code_edit)
    if knobs.git_metadata is not None:
        (target / ".gitattributes").write_text(_GIT_METADATA_BYTES[knobs.git_metadata], encoding="utf-8")
    if knobs.shape_change is not None:
        _shape_change(target / "NOTICE", knobs.shape_change)


def _shape_change(notice: Path, shape: str) -> None:
    if shape == "delete":
        notice.unlink()
    elif shape == "mode":
        notice.chmod(0o755)
    else:
        notice.unlink()
        notice.symlink_to("README.md")


def _executed_code_edit(target: Path, variant: str) -> None:
    paths = {
        "bootstrap": "bootstrap.py",
        "bootstrap_adapter": "bootstrap_adapter/postgres.py",
        "ananta": "ananta/src/ananta/__init__.py",
        "macos_vault": "plugins/macos_vault_plugin/src/macos_vault_plugin/__init__.py",
    }
    if variant == "untracked_in_midwife":
        (target / "plugins/github_midwife_plugin/src/github_midwife_plugin/extra.py").write_text("# dropped-in module\n", encoding="utf-8")
        return
    path = target / paths[variant]
    if not path.is_file():
        path = next(item for item in (target / paths[variant]).parent.glob("*.py"))
    path.write_text(path.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")


def _lay_down_ignored_runtime(target: Path, knobs: Knobs, host_python: Path) -> None:
    """Section 3.3 item 10: the venv and the log directory, ignored, plus the instance-python knob."""
    venv_bin = target / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (target / ".venv" / "pyvenv.cfg").write_text(f"home = {host_python.parent}\nversion = 3.13.0\n", encoding="utf-8")
    python = venv_bin / "python3"
    if knobs.instance_python == "present":
        python.write_text("#!/bin/sh\nexec python3 \"$@\"\n", encoding="utf-8")
        python.chmod(0o755)
    elif knobs.instance_python == "dangling":
        python.symlink_to(target / ".venv" / "gone" / "python3.13")
    bridge = venv_bin / "solet-bridge"
    bridge.write_text("#!/bin/sh\necho solet-bridge 0.1.0\n", encoding="utf-8")
    bridge.chmod(0o755)
    (venv_bin / "solet").write_text("#!/bin/sh\necho solet 0.1.0\n", encoding="utf-8")
    (venv_bin / "solet").chmod(0o755)
    (target / "profile" / "data" / "logs").mkdir(parents=True, exist_ok=True)
    (target / "profile" / "data" / "logs" / "ananta.log").write_text("fixture log\n", encoding="utf-8")


def _opaque_stores(root: Path, target: Path) -> OpaqueStores:
    stores = root / "stores"
    stores.mkdir(parents=True, exist_ok=True)
    keychain, postgres = stores / "keychain.store", stores / "postgres.store"
    # Opaque, but deterministic across fixtures so the crash sweep's byte map compares two fixtures of one scenario.
    keychain.write_bytes(random.Random("step7-keychain-store").randbytes(4096))
    postgres.write_bytes(random.Random("step7-postgres-store").randbytes(4096))
    passphrase = target / "profile" / "config" / "plugins" / "macos_vault_plugin" / "passphrase"
    passphrase.parent.mkdir(parents=True, exist_ok=True)
    passphrase.write_bytes(keychain.read_bytes()[:64])
    return OpaqueStores(keychain, postgres)


# --- HOME ------------------------------------------------------------------------------------------------


def _home(root: Path, target: Path, knobs: Knobs) -> Path:
    """Section 3.4: the operator's HOME as the Manager expects it after a create."""
    from github_midwife_plugin.autostart import render_launchagent_plist  # noqa: PLC0415

    home = root / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"local.solet.{NAME}.plist").write_bytes(render_launchagent_plist(NAME, target, home, template_text=None, stamp=None, stamped=False))
    if knobs.router:
        (agents / f"local.solet.{NAME}.router.plist").write_bytes(b"<plist/>\n")
    launcher = home / ".local" / "bin"
    launcher.mkdir(parents=True, exist_ok=True)
    (launcher / NAME).symlink_to(target / ".venv" / "bin" / "solet-bridge")
    # The managed ``~/.zshrc`` block and ``~/.claude/CLAUDE.md`` section are left for the reference run's hydration
    # stage to render at the CURRENT template digest (a hand-written block at a fake digest reads as a conflict to
    # the seed's three-way engine); the operator's own files beside them are never touched.
    (home / ".zshrc").write_text("# operator zshrc\nexport EDITOR=vim\n", encoding="utf-8")
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "CLAUDE.md").write_text("# Operator notes\n\nMine.\n", encoding="utf-8")
    (claude / "settings.json").write_text('{"operator": true}\n', encoding="utf-8")
    cache = claude / "plugins" / "cache" / NAME / "coordination-hooks" / "1.0.0" / "hooks"
    cache.mkdir(parents=True, exist_ok=True)
    hooks = target / "plugins" / "github_midwife_plugin" / "claude_plugin" / "coordination-hooks" / "hooks"
    for entry in hooks.iterdir() if hooks.is_dir() else ():
        if entry.is_file():
            (cache / entry.name).write_bytes(entry.read_bytes())
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {f"coordination-hooks@{NAME}": [{"installPath": str(cache.parent)}]}}), encoding="utf-8")
    (claude / "plugins" / "known_marketplaces.json").write_text("{}\n", encoding="utf-8")
    return home


# --- host --------------------------------------------------------------------------------------------------


def _cold_host(root: Path, knobs: Knobs) -> ColdHost:
    host = ColdHost(root / "host", knobs)
    host.bin.mkdir(parents=True, exist_ok=True)
    stubs = {"python3.13": knobs.host_python == "present", "brew": knobs.homebrew == "present", "tmux": knobs.tmux == "present", "psql": knobs.postgres_client == "present"}
    for name, present in stubs.items():
        if present:
            stub = host.bin / name
            stub.write_text(f"#!/bin/sh\necho {name} fixture 0.0\n", encoding="utf-8")
            stub.chmod(0o755)
    return host


def make_offline(fake: FakeHost) -> None:
    """``service="offline"``: the bridge does not answer (every bridge read raises) and the process table is unreadable,
    while launchd still lists the label -- the "unreachable" shape whose doctor reading is ``unknown service_offline``
    on sections 9/11/14.  A label launchd no longer lists is the ``launchagent_not_loaded`` finding, not an unknown."""
    from solet_manager.errors import AdapterError  # noqa: PLC0415

    def unreachable(*_args: object, **_kwargs: object) -> dict[str, JsonValue]:
        raise AdapterError("bridge call did not succeed: connection refused")

    fake.read_health = unreachable  # type: ignore[method-assign]
    fake.invoke_bridge = unreachable  # type: ignore[method-assign]
    fake.ps_fails = True


def _fake_host(knobs: Knobs) -> FakeHost:
    fake = FakeHost()
    if knobs.service == "offline":
        make_offline(fake)
    elif knobs.service == "unhealthy":
        fake.health = [{"status": "unhealthy"}]
    fake.keychain_present = knobs.keychain == "present"
    if knobs.keychain == "unqueryable":
        fake.ps_fails = False

        def raising(service: str, account: str) -> subprocess.CompletedProcess[str]:
            raise OSError("security unqueryable")

        fake.run_security = raising  # type: ignore[method-assign]
    return fake


# --- Manager state ---------------------------------------------------------------------------------------


def import_metadata(fixture_baseline: Release, candidate: Release, raw: bytes, contract: str) -> InstalledUpdateDescriptor:
    """The installed descriptor a keg carries: the candidate r2 as the channel, the baseline r1 as a ``pre_manager_seed`` anchor."""
    base = installed(candidate, raw, contract)
    anchor = InspectionAnchor(
        "stable-pre-manager-seed-fixture",
        InspectionAnchorKind.PRE_MANAGER_SEED,
        "stable",
        CANONICAL,
        fixture_baseline.commit,
        fixture_baseline.tree,
        hashlib.sha256(fixture_baseline.provenance).hexdigest(),
        fixture_baseline.seed_id,
        ORIGIN_ID,
        fixture_baseline.manifest,
        ChannelRelation.FAST_FORWARD,
        (),
    )
    return InstalledUpdateDescriptor(InstalledInspectionMetadata(base.metadata.channel_identity, base.metadata.seed_lock, (anchor,)), raw)


@contextmanager
def _import_loader(descriptor_value: InstalledUpdateDescriptor) -> Iterator[None]:
    from solet_manager import import_enrollment  # noqa: PLC0415

    def loader(channel: str, tracker: object) -> InstalledInspectionMetadata:
        del channel, tracker
        return descriptor_value.metadata

    with patch.object(import_enrollment, "load_installed_inspection_metadata", loader):
        yield


def _enroll_by_import(fixture: RealStyleFixture) -> None:
    """``manager_state="enrolled"``: the real ``import --dry-run`` then ``import --yes``, never a hand-written row."""
    from solet_manager.import_enrollment import ImportRequest, enroll_import, preview_import  # noqa: PLC0415

    request = ImportRequest(NAME, fixture.target, "stable", fixture.paths)
    with _import_loader(cast(InstalledUpdateDescriptor, fixture.import_descriptor)), patch.object(Path, "home", classmethod(lambda cls: fixture.home)):
        preview = preview_import(request)
        result = enroll_import(request, preview.fingerprint)
    assert result.status == "imported", result.status
    if fixture.knobs.router:
        declare_router(fixture)


def declare_router(fixture: RealStyleFixture) -> None:
    """A router install the import never observes (it records ``router_label=None``): declared on the row as Step 5's ``enroll()`` does.

    A verified state under a router needs Step 6's whole controller fake, so ``Knobs(router=True)`` is for the
    enrolled rows (CH-23); a verified-then-declared row (CH-22) calls this after a single-colour reference run.
    """
    records = read_maintenance_inventory_v2(fixture.paths.maintenance_inventory_path)
    updated = tuple(replace(record, service_identity=replace(record.service_identity, router_label=f"local.solet.{NAME}.router", router_socket=str(fixture.root / "router.sock"))) for record in records)
    write_maintenance_inventory_v2(fixture.paths.maintenance_inventory_path, updated)


class _BizopsRuntime(SeedRuntime):
    """Step 5's in-process seed runtime with the instance named ``bizops`` (its fake ``claude`` answers for that marketplace)."""

    def run(self, argv: tuple[str, ...], *, timeout_seconds: int, cwd: Path | None = None, extra_env: dict[str, str] | None = None, input_text: str | None = None, output_limit: int = 4096) -> CommandOutcome:
        if argv[0] == "/fixture/bin/claude":
            self.commands.append(argv)
            if argv[1:3] == ("plugin", "install"):
                self._refresh_plugin_cache()
            if argv[1:3] == ("plugin", "list"):
                return bounded_command_outcome(returncode=0, timed_out=False, duration_ms=1, stdout=json.dumps([{"id": f"coordination-hooks@{NAME}", "enabled": True}]), stderr="", output_limit=output_limit)
            return CommandOutcome(0, False, 1, "", "")
        return super().run(argv, timeout_seconds=timeout_seconds, cwd=cwd, extra_env=extra_env, input_text=input_text, output_limit=output_limit)

    def _refresh_plugin_cache(self) -> None:
        cache = self.home / ".claude" / "plugins" / "cache" / NAME / "coordination-hooks" / "1.0.0" / "hooks"
        shipped = self.target / HOOKS
        if not shipped.is_dir():
            return
        cache.mkdir(parents=True, exist_ok=True)
        for entry in shipped.iterdir():
            if entry.is_file():
                (cache / entry.name).write_bytes(entry.read_bytes())


def _seams(fixture: RealStyleFixture) -> RuntimeSeams:
    """Step 5's ``make_seams`` with the fixture instance's runtime and the two Step-7 host seams."""
    from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: PLC0415
    from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: PLC0415
    from solet_manager.adapter_protocol import OperationRequest, OperationResult  # noqa: PLC0415
    from solet_manager.existing_install_adapters import ExistingInstallAdapterRegistry  # noqa: PLC0415

    from bootstrap_adapter.routes import execute_adapter_request  # noqa: PLC0415

    host = fixture.host
    runtime = _BizopsRuntime(fixture.home, host, fixture.target)
    fake_host = host
    cold = cast(ColdHost, fixture.cold_host)

    def invoke_adapter(registry: ExistingInstallAdapterRegistry, request: OperationRequest) -> OperationResult:
        fake_host.adapter_calls.append((request.operation_ref, request.phase, request.probe_purpose))
        member = registry.operation(request.operation_ref)
        if registry.command_for(member.runner) is None:
            # The real invoker's ``adapter_missing`` block (an absent host or instance Python leaves no vector), kept in-process.
            return OperationResult.blocked(request, error_kind="adapter_missing", repair=f"The target does not provide the {member.runner!r} vector for {request.operation_ref}.")
        payload = json.loads(json.dumps(request.to_dict(), sort_keys=True))
        if request.operation_ref == "existing::dependencies.reconcile":
            raw = execute_adapter_request(payload, runner=_venv_aware_runner(fixture, registry.target, cold), which=lambda _name: None, base_python=str(cold.bin / "python3.13"))
        else:
            raw = dispatch_request(AdapterRequest.from_json(json.dumps(payload)), runtime)
        return OperationResult.from_dict(cast(dict[str, JsonValue], raw), request)

    host.target = fixture.target

    def launchctl(registry: ExistingInstallAdapterRegistry, verb: str, arguments: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        completed = host.launchctl(registry, verb, arguments, timeout)
        if verb == "bootstrap" and completed.returncode == 0 and fixture.on_restart is not None:
            fixture.on_restart(fixture)
        return completed

    def read_health(registry: ExistingInstallAdapterRegistry, timeout: int) -> dict[str, JsonValue]:
        if fixture.install_signal is not None and fixture.wait_for_install and fixture.restart_thread is not None:
            # Readiness after the restart: wait on the install thread's signal (explicit synchronisation, never a sleep).
            assert fixture.install_signal.wait(timeout=30), "the fake service's install thread did not signal within the readiness budget"
        return host.read_health(registry, timeout)

    # Late-bound through the host object so a scenario can flip the service (``make_offline``) AFTER the build.
    return RuntimeSeams(
        home=fixture.home,
        invoke_adapter=invoke_adapter,
        invoke_reconciliation=lambda registry, envelope, timeout: host.invoke_reconciliation(registry, envelope, timeout),
        invoke_bridge=lambda registry, key, arguments, kind, timeout: host.invoke_bridge(registry, key, arguments, kind, timeout),
        read_health=read_health,
        launchctl=launchctl,
        uid=501,
        run_ps=lambda timeout: host.run_ps(timeout),
        run_security=lambda service, account: host.run_security(service, account),
        resolve_base_python=cold.resolve_base_python,
        which=cold.which,
    )


def _venv_aware_runner(fixture: RealStyleFixture, target: Path, cold: ColdHost) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Step 5's fake closure runner plus the one effect it lacked: ``python -m venv`` lays a fresh instance interpreter
    down (a symlink to the host Python 3.13, CH-10/11), so a dangling or absent venv is repaired the way the real
    command would repair it instead of being reported repaired while still dangling."""
    inner = _closure_runner(fixture.host, target)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if len(command) == 2 and command[1] == "--version" and Path(command[0]).name == "python3.13":
            # The seed-side resolver's ``<candidate> --version`` probe: the host stub answers as Python 3.13.
            return subprocess.CompletedProcess(command, 0, "Python 3.13.7\n", "")
        if len(command) >= 3 and command[1:3] == ["-m", "venv"]:
            venv = Path(command[-1])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            python = venv / "bin" / "python3"
            if python.is_symlink() or python.exists():
                python.unlink()
            python.symlink_to(cold.bin / "python3.13")
            (venv / "pyvenv.cfg").write_text(f"home = {cold.bin}\nversion = 3.13.0\n", encoding="utf-8")
            fixture.venv_rebuilds.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")
        return inner(command, **kwargs)

    return run


def _request(fixture: RealStyleFixture) -> UpdateRequest:
    seams = _seams(fixture)
    return UpdateRequest(NAME, fixture.paths, descriptor_loader=lambda channel, tracker: cast(InstalledUpdateDescriptor, fixture.import_descriptor), transport_url=str(fixture.source), runtime_seams=seams)


# --- the builder ---------------------------------------------------------------------------------------


DEFAULT_KNOBS = Knobs()


def build_real_style(root: Path, *, host: FakeHost | None = None, knobs: Knobs = DEFAULT_KNOBS) -> RealStyleFixture:
    """Section 3: the real-style clone, HOME, host, stores and (per ``manager_state``) Manager state."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / "target"
    source, baseline, candidate, files, contract = sealed_source(knobs)
    _clone(source, target, baseline)
    expected = _birth_shape(target, knobs)
    cold = _cold_host(root, knobs)
    _lay_down_ignored_runtime(target, knobs, cold.bin / "python3.13")
    stores = _opaque_stores(root, target)
    edits = _apply_knobs(target, knobs)
    home = _home(root, target, knobs)
    paths = ManagerPaths(root / "manager" / "config", root / "manager" / "state", root / "manager" / "cache")
    raw = descriptor(candidate, contract)
    loaded = import_metadata(baseline, candidate, raw, contract)
    fake_host = _fake_host(knobs) if host is None else host
    fixture = RealStyleFixture(
        root=root,
        source=source,
        target=target,
        home=home,
        paths=paths,
        baseline=baseline,
        candidate=candidate,
        contract=contract,
        descriptor_digest=loaded.metadata.channel_identity.descriptor_digest,
        request=UpdateRequest(NAME, paths),
        host=fake_host,
        record_name=NAME,
        name=NAME,
        stores=stores,
        tracked_edits=edits,
        untracked=tuple(sorted(path for path, code in porcelain(target).items() if code == "??")),
        genesis_marker=target / ".solet" / "genesis.json",
        named_launcher=home / ".local" / "bin" / NAME,
        router=knobs.router,
        knobs=knobs,
        cold_host=cold,
        seed_commit=assembled_seed()[1],
        attribution=expected,
        import_descriptor=loaded,
    )
    fixture.request = _request(fixture)
    _enter_manager_state(fixture, knobs)
    return fixture


def _birth_shape(target: Path, knobs: Knobs) -> dict[str, str]:
    """Section 3.3: the genesis writers and (for the create birth) the shell shape, attribution-proved; or a bare clone."""
    expected: dict[str, str] = {}
    if not knobs.genesis_untracked:
        _bare_clone_shape(target)
        return expected
    expected.update(_run_genesis_writers(target, knobs))
    if knobs.birth == "create":
        expected.update(_run_create_shape(target))
    _assert_attribution(target, expected)
    if not knobs.tracked_edits:
        for path, code in porcelain(target).items():
            if code != "??":
                git(target, "checkout", "--", path)
    return expected


def _enter_manager_state(fixture: RealStyleFixture, knobs: Knobs) -> None:
    """``manager_state``: cold (nothing), enrolled (a real import), verified (the reference update on top)."""
    if knobs.manager_state == "cold":
        return
    for directory in (fixture.paths.config_dir, fixture.paths.state_dir, fixture.paths.cache_dir):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    _enroll_by_import(fixture)
    if knobs.manager_state == "verified":
        run_reference_update(fixture)


def _bare_clone_shape(target: Path) -> None:
    """``genesis_untracked=False``: the only tree a v4 journal can have been written on (CH-43) -- a clean clone with the ignored runtime only."""
    (target / "profile" / "config").mkdir(parents=True, exist_ok=True)
    (target / "profile" / "config" / "manifest.yaml").write_text(f"profile_name: {PROFILE}\nplugins:\n- github_midwife_plugin\n", encoding="utf-8")
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text(".venv/\nprofile/\n", encoding="utf-8")


def run_reference_update(fixture: RealStyleFixture) -> str:
    """The reference path: source preview/apply, runtime preview/apply through ``promoted``."""
    preview = preview_update_instance(fixture.request)
    assert preview.status == "preview_ready", (preview.status, preview.error_kind, preview.message, preview.data.get("blocked"))
    applied = apply_update(fixture.request, cast(str, preview.data["approval_fingerprint"]))
    assert applied.status == "source_advanced", (applied.status, applied.error_kind, applied.message)
    runtime = preview_update_instance(fixture.request)
    assert runtime.status == "runtime_preview_ready", (runtime.status, runtime.error_kind, runtime.data.get("blocked"), runtime.data.get("lifecycle"))
    result = apply_update(fixture.request, cast(str, runtime.data["runtime_approval_fingerprint"]))
    assert result.status == "promoted", (result.status, result.error_kind, result.message)
    return result.status


# --- keg and subprocess ---------------------------------------------------------------------------------


def build_keg(root: Path, *, seed_lock: bytes, anchors: dict[str, JsonValue], version: str = "0.1.0") -> FakeKeg:
    """Section 8.2: ``python -m venv --without-pip``, a real wheel of ``solet_cli`` whose catalog binds the fixture's seed lock, two shims."""
    prefix = root / "keg"
    if prefix.exists():
        shutil.rmtree(prefix)
    subprocess.run((sys.executable, "-m", "venv", "--without-pip", str(prefix)), check=True, capture_output=True)
    python = prefix / "bin" / "python"
    site = prefix / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    work = root / "keg-build"
    if work.exists():
        shutil.rmtree(work)
    package = work / "solet_cli"
    shutil.copytree(_ROOT / "solet_cli", package, ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.egg-info", "build", "dist", "tests", "homebrew"))
    metadata = package / "src" / "solet_manager" / "released_metadata"
    anchors_raw = (json.dumps(anchors, indent=2, sort_keys=True) + "\n").encode()
    (metadata / "existing_install_inspection_anchors.v1.json").write_bytes(anchors_raw)
    catalog = {"schema_version": 1, "channels": [{"channel_id": "stable", "anchor_table_sha256": hashlib.sha256(anchors_raw).hexdigest()}]}
    (metadata / "existing_install_inspection_catalog.v1.json").write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if version != "0.1.0":
        pyproject = package / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('version = "0.1.0"', f'version = "{version}"'), encoding="utf-8")
        models = package / "src" / "solet_manager" / "models.py"
        models.write_text(models.read_text(encoding="utf-8").replace('MANAGER_VERSION = "0.1.0"', f'MANAGER_VERSION = "{version}"'), encoding="utf-8")
    wheels = work / "wheels"
    for project in (package, _ROOT / "solet_setup_contracts"):
        subprocess.run((sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "--wheel-dir", str(wheels), str(project)), check=True, capture_output=True, text=True)
    for wheel in sorted(wheels.glob("*.whl")):
        subprocess.run((sys.executable, "-m", "pip", "install", "--no-deps", "--quiet", "--target", str(site), str(wheel)), check=True, capture_output=True, text=True)
    import packaging  # noqa: PLC0415 - the one third-party dependency, copied from the building interpreter

    shutil.copytree(Path(packaging.__file__).parent, site / "packaging", ignore=shutil.ignore_patterns("__pycache__"))
    share = prefix / "share" / "solet"
    share.mkdir(parents=True, exist_ok=True)
    (share / "seed.lock.json").write_bytes(seed_lock)
    # The release-rendered seed-lock catalog (solet.rb.template): unlike
    # anchor_table_sha256 above, seed_lock_sha256 must equal sha256 of THIS
    # lock, which embeds this release's own payload digest -- unknowable at
    # manager-source-commit time, so it cannot live in the wheel (iss_42749563).
    seed_lock_catalog = {
        "schema_version": 1,
        "channels": [{"channel_id": "stable", "seed_lock_sha256": hashlib.sha256(seed_lock).hexdigest()}],
    }
    (share / "existing_install_inspection_seed_lock_catalog.v1.json").write_text(
        json.dumps(seed_lock_catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # The receipt the real Formula writes (solet.rb.template), paired with the seed lock's own
    # provenance.source_commit so the design section 7.3 consumption gate sees one cut, not two.
    receipt = {"schema_version": 1, "mode": "release", "source_commit": cast(dict[str, Any], json.loads(seed_lock))["provenance"]["source_commit"]}
    (share / "install-source.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    for name, module in (("solet", "solet_manager.cli"), ("solet-manager", "solet_manager.manager_cli")):
        shim = prefix / "bin" / name
        shim.write_text(f"#!{python}\nimport sys\nfrom {module} import main\nsys.exit(main())\n", encoding="utf-8")
        shim.chmod(0o755)
    shutil.rmtree(work)
    return FakeKeg(prefix, python, prefix / "bin", share, site, version)


def manager_subprocess(fixture: RealStyleFixture, argv: list[str], *, keg: FakeKeg | None = None, env_extra: dict[str, str] | None = None, path_prefix: tuple[Path, ...] = ()) -> tuple[int, dict[str, Any]]:
    """Section 4.3: the Manager as a fresh process, with a from-scratch environment (nothing inherited)."""
    keg_value = keg or fixture.keg
    assert keg_value is not None, "every subprocess row needs a FakeKeg: the installed-metadata loader fails loud on an editable tree"
    cold = cast(ColdHost, fixture.cold_host)
    path = ":".join([*(str(item) for item in path_prefix), str(cold.bin), str(keg_value.bin), "/usr/bin", "/bin"])
    env = {"HOME": str(fixture.home), "SOLET_HOME": str(fixture.manager_home), "PATH": path, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C", **(env_extra or {})}
    completed = subprocess.run((str(keg_value.bin / "solet-manager"), *argv, "--json"), capture_output=True, text=True, env=env, check=False, timeout=300)
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw_stdout": completed.stdout, "raw_stderr": completed.stderr}
    if "raw_stderr" not in payload:
        payload["_stderr"] = completed.stderr
    return completed.returncode, payload


def cli(fixture: RealStyleFixture, argv: list[str]) -> tuple[int, dict[str, Any]]:
    """In-process ``manager_cli.main`` with the fixture's loader, transport and seams (the Step-6 ``cli_resume`` pattern)."""
    from solet_manager import (
        import_enrollment,  # noqa: PLC0415
        manager_cli,  # noqa: PLC0415
    )

    captured: list[str] = []
    template = fixture.request

    def request_factory(name: str, manager_paths: ManagerPaths, **kwargs: Any) -> UpdateRequest:
        return replace(template, name=name, manager_paths=manager_paths, operator_selections=kwargs.get("operator_selections", {}))

    def loader(channel: str, tracker: object) -> InstalledInspectionMetadata:
        del channel, tracker
        return cast(InstalledUpdateDescriptor, fixture.import_descriptor).metadata

    with (
        patch.object(manager_cli.ManagerPaths, "resolve", classmethod(lambda cls, **_kwargs: fixture.paths)),
        patch.object(manager_cli, "UpdateRequest", request_factory),
        patch.object(manager_cli, "load_installed_inspection_metadata", loader),
        patch.object(import_enrollment, "load_installed_inspection_metadata", loader),
        patch.object(Path, "home", classmethod(lambda cls: fixture.home)),
        patch("builtins.print", lambda *args, **_kwargs: captured.append(" ".join(str(item) for item in args))),
    ):
        code = manager_cli.main([*argv, "--json"])
    return code, json.loads("\n".join(captured))


def tree_snapshot(root: Path, *, exclude: tuple[str, ...] = ("profile/data",)) -> dict[str, str]:
    """sha256 (files) / target (symlinks) of everything under ``root`` outside ``.git`` and ``exclude`` prefixes."""
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if ".git" in path.parts or any(relative.startswith(prefix) for prefix in exclude):
            continue
        if path.is_symlink():
            result[relative] = "link:" + os.readlink(path)
        elif path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def fixture_bytes(fixture: RealStyleFixture) -> dict[str, str]:
    """Step 6's ``byte_map`` (target + HOME) plus every symlink by its target, for the sweep and the shared verdict rows.

    ``.solet/genesis.json`` is hashed without its ``completed_at`` stamp (genesis writes the wall clock) so two
    fixtures of one scenario compare equal.
    """
    combined = byte_map(fixture)
    for path in sorted(fixture.target.rglob("*")):
        if path.is_symlink() and ".git" not in path.parts:
            combined[f"target/{path.relative_to(fixture.target)}"] = "link:" + os.readlink(path)
    marker = fixture.target / ".solet" / "genesis.json"
    if marker.is_file():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload.pop("completed_at", None)
        combined["target/.solet/genesis.json"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return combined


def checks_by_id(doctor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The doctor's sections flattened to ``{check_id: check}``."""
    found: dict[str, dict[str, Any]] = {}
    for section in cast(list[dict[str, Any]], doctor.get("sections", [])):
        for check in cast(list[dict[str, Any]], section["checks"]):
            found[cast(str, check["check_id"])] = check
    return found


Builder = Callable[[Path], RealStyleFixture]
