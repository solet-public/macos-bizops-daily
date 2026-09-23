"""ReleaseBuilder dirty-tree gate — the six ruled legs plus the unattestable case.

Architect's ruling (dev-checkout workbench record — not part of the
shipped tree): the deploy artifact is built from the WORKING TREE but
IDENTIFIED by HEAD, so a live
edit ships unreviewed under a clean SHA. The gate refuses by default, but ONLY on dirt
the artifact actually ships, and ``VERSION`` attests the tree state POSITIVELY in both
directions so absence of the field can never be read as "clean".

Every leg drives the REAL ``git status --porcelain`` path against a synthetic git repo
under ``~/.ananta`` scratch (NEVER ``/tmp`` — operator hard rule). No dirty-path
resolver is injected: injecting one would test the fake, not the gate.

Legs, and the mutation each one reds on:

===============================  ===================================================
Leg                              Red mutation it catches
===============================  ===================================================
ships-dirty refuses              drop the gate
non-shipped dirt builds          unscope the porcelain call (the FALSE-POSITIVE
                                 direction — the expensive one on a 13-writer tree)
positive attestation             omit the field when clean
override is disclosed            stamp ``clean`` under ``allow_dirty``
gate follows the clone           split the shared constant in two
untracked ships too              gate on modified-only
unattestable is not clean        report a non-repo tree as ``clean``
===============================  ===================================================

NOT covered, and named so ``tree_state: clean`` is never read as "artifact fully
attested": ``build()`` also ships ``.venv``, which is gitignored and therefore invisible
to porcelain. A hand-edited or stale venv ships mislabeled under this gate (ruling §5).
``VERSION.tree_state_scope`` records exactly which subtrees the attestation covers so a
downstream reader can see the venv is not among them.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _release_manager_smoke_support as support  # noqa: E402
from macos_self_deployment_plugin import release_manager as rm_module  # noqa: E402
from macos_self_deployment_plugin.release_code_collection import (  # noqa: E402
    ReleaseCodeCollectionError,
    ReleaseCodeCollector,
)
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    SELECTED_FILE_MANIFEST_FILENAME,
    STAGING_SUFFIX,
    TREE_STATE_CLEAN,
    TREE_STATE_DIRTY,
    TREE_STATE_UNKNOWN,
    VERSION_FILENAME,
    ReleaseManagerError,
)
from macos_self_deployment_plugin.schema_snapshot_producer import (  # noqa: E402
    _pythonpath_for_tree,
    build_schema_snapshot_fn,
)

_GIT_IDENTITY = (
    "-c", "user.name=smoke",
    "-c", "user.email=smoke@example.invalid",
    "-c", "commit.gpgsign=false",
)


def _git(source: Path, *args: str) -> None:
    """Run a git command inside the SYNTHETIC scratch repo (never the checkout)."""
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args!r} failed: {result.stderr.strip()}")


def _init_repo(source: Path) -> None:
    """``git init`` + commit everything, so the tree starts genuinely clean."""
    _git(source, "init", "--quiet")
    _git(source, "add", "-A")
    _git(source, *_GIT_IDENTITY, "commit", "--quiet", "-m", "synthetic baseline")


def _make_repo_source(root: Path, **kwargs: object) -> Path:
    """A synthetic source tree that is its own committed git repo."""
    source = support.build_fake_source(root, **kwargs)  # type: ignore[arg-type]
    # .venv is cloned but gitignored in the real repo; mirror that so porcelain
    # does not report the synthetic venv as untracked dirt (ruling §5 scope).
    (source / ".gitignore").write_text(".venv/\n")
    _init_repo(source)
    return source


def _read_version(releases: Path, release_id: str) -> dict[str, object]:
    payload = json.loads((releases / release_id / VERSION_FILENAME).read_text())
    assert isinstance(payload, dict)
    return payload


def _capture(fn: object) -> BaseException | None:
    try:
        fn()  # type: ignore[operator]
    except BaseException as exc:  # noqa: BLE001 — the smoke inspects the type
        return exc
    return None


def _build_or_fail(
    rec: support.SmokeRecorder, tag: str, mgr: object, **kwargs: object
) -> object | None:
    """Build, converting an UNEXPECTED refusal into a labelled FAIL row.

    Legs that assert a build SUCCEEDS must not let a refusal escape as an
    uncaught exception: that aborts the run, skips every later leg, and
    prints a traceback with no FAIL line — so a mutation this fixture does
    catch reads as "not load-bearing" to anything parsing the report. The
    verdict has to come from the recorder, never from the exit path.
    """
    try:
        return mgr.build_candidate(**kwargs)  # type: ignore[attr-defined]
    except ReleaseManagerError as exc:
        rec.check(False, f"[{tag}] build was REFUSED but should have succeeded: {exc}")
        return None


_PRE_CLONE_REFUSAL = "refusing to build a release from a dirty working tree"
_MID_CLONE_REFUSAL = "working tree changed while the release was being cloned"


def _is_pre_clone_refusal(exc: BaseException | None) -> bool:
    """Did the PRE-clone gate fire, as opposed to the post-clone re-check?

    The two gates are defence in depth, so several mutations to one are
    masked by the other — the build still refuses, just for a different
    reason. Asserting WHICH one fired is what keeps each leg able to
    isolate its own mutation instead of buying a green from its neighbour.
    """
    return isinstance(exc, ReleaseManagerError) and _PRE_CLONE_REFUSAL in str(exc)


def _staging_dirs(releases: Path) -> list[Path]:
    if not releases.is_dir():
        return []
    return [p for p in releases.iterdir() if p.name.endswith(STAGING_SUFFIX)]


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Leg 1 — dirt the artifact SHIPS refuses, and leaves no staging dir behind
# ---------------------------------------------------------------------------

def _case_ships_dirty_refuses(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "ships-dirty"
    source = _make_repo_source(root)
    releases = root / "releases"
    mgr = support.make_manager(source, releases)

    edited = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "__init__.py"
    edited.write_text("# LIVE EDIT that would ship under a clean SHA\n")

    exc = _capture(mgr.build_candidate)
    rec.check(
        isinstance(exc, ReleaseManagerError),
        f"[{tag}] modified file under plugins/ ⇒ build_candidate raises "
        f"ReleaseManagerError (got {type(exc).__name__ if exc else 'no raise'})",
    )
    rec.check(
        _is_pre_clone_refusal(exc),
        f"[{tag}] and it is the PRE-CLONE gate that refuses, not the post-clone "
        f"re-check catching it late — nothing should be copied at all "
        f"(got {str(exc)[:80]!r})",
    )
    rec.check(
        exc is not None and "foo_plugin" in str(exc),
        f"[{tag}] the refusal names the offending path, not just 'dirty'",
    )
    rec.check(
        _staging_dirs(releases) == [],
        f"[{tag}] no staging dir left behind: {[p.name for p in _staging_dirs(releases)]}",
    )
    rec.check(
        not any(p.name.startswith("rel-") and not p.name.endswith(STAGING_SUFFIX)
                for p in (releases.iterdir() if releases.is_dir() else [])),
        f"[{tag}] no finalized release materialized either",
    )


# ---------------------------------------------------------------------------
# Leg 2 — dirt OUTSIDE the shipped subtrees must NOT refuse (false-positive
# direction: the expensive one; an unscoped gate bricks every deploy)
# ---------------------------------------------------------------------------

def _case_non_shipped_dirt_builds(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "non-shipped-dirt"
    source = _make_repo_source(root)
    releases = root / "releases"
    mgr = support.make_manager(source, releases)

    workbench = source / "workbench"
    workbench.mkdir()
    (workbench / "2026-08-01_lane_journal.md").write_text("permanently dirty by design\n")
    (source / "README.md").write_text("a dirty root doc\n")

    unscoped = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip().splitlines()
    rec.check(
        len(unscoped) >= 2,
        f"[{tag}] PRECONDITION: the tree really is dirty unscoped ({len(unscoped)} paths) "
        "— otherwise this leg would pass vacuously",
    )

    candidate = _build_or_fail(rec, tag, mgr)
    if candidate is None:
        return
    payload = _read_version(releases, candidate.release_id)  # type: ignore[attr-defined]
    rec.check(
        payload.get("tree_state") == TREE_STATE_CLEAN,
        f"[{tag}] workbench/ + root-doc dirt ⇒ tree_state == clean "
        f"(got {payload.get('tree_state')!r})",
    )
    rec.check(
        payload.get("dirty_paths") == [],
        f"[{tag}] dirty_paths empty (got {payload.get('dirty_paths')!r})",
    )


# ---------------------------------------------------------------------------
# Leg 3 — positive attestation: the field is PRESENT on a clean build
# ---------------------------------------------------------------------------

def _case_positive_attestation(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "positive-attestation"
    source = _make_repo_source(root)
    releases = root / "releases"
    mgr = support.make_manager(source, releases)

    candidate = _build_or_fail(rec, tag, mgr)
    if candidate is None:
        return
    payload = _read_version(releases, candidate.release_id)  # type: ignore[attr-defined]
    rec.check(
        "tree_state" in payload,
        f"[{tag}] a CLEAN build still writes tree_state — absence must never be "
        "readable as clean (it means only 'predates the gate')",
    )
    rec.check(
        payload.get("tree_state") == TREE_STATE_CLEAN
        and payload.get("dirty_paths") == [],
        f"[{tag}] clean ⇒ tree_state=clean + empty dirty_paths "
        f"({payload.get('tree_state')!r}, {payload.get('dirty_paths')!r})",
    )
    rec.check(
        payload.get("tree_state_scope") == list(rm_module.CODE_SUBTREES),
        f"[{tag}] VERSION discloses WHICH subtrees the attestation covers "
        f"(got {payload.get('tree_state_scope')!r}) — so 'clean' is not read as "
        "'artifact fully attested'; .venv is gitignored and out of scope",
    )


# ---------------------------------------------------------------------------
# Leg 4 — the override builds, but is DISCLOSED in VERSION and logged loudly
# ---------------------------------------------------------------------------

def _case_override_is_disclosed(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "override-disclosed"
    source = _make_repo_source(root)
    releases = root / "releases"
    handler = _CapturingHandler()
    logger = logging.getLogger(f"dirty-gate-smoke.{tag}")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    mgr = support.make_manager(source, releases, logger=logger)

    edited = source / "ananta" / "src" / "ananta" / "__init__.py"
    edited.write_text("# live edit, shipped deliberately\n")

    candidate = _build_or_fail(rec, tag, mgr, allow_dirty=True)
    if candidate is None:
        logger.removeHandler(handler)
        return
    payload = _read_version(releases, candidate.release_id)  # type: ignore[attr-defined]
    rec.check(
        payload.get("tree_state") == TREE_STATE_DIRTY,
        f"[{tag}] allow_dirty stamps tree_state=dirty, NOT clean "
        f"(got {payload.get('tree_state')!r})",
    )
    listed = payload.get("dirty_paths")
    rec.check(
        isinstance(listed, list) and any("ananta" in str(p) for p in listed),
        f"[{tag}] VERSION lists the full porcelain population (got {listed!r})",
    )
    warnings = [r for r in handler.records if r.levelno >= logging.WARNING]
    rec.check(
        any("ananta" in r.getMessage() for r in warnings),
        f"[{tag}] logs at WARNING with the dirty list INLINE "
        f"({[r.getMessage() for r in warnings]})",
    )
    logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Leg 5 — the gate's scope is the CLONE's scope: one constant, not two lists
# ---------------------------------------------------------------------------

def _case_gate_follows_the_clone(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "gate-follows-clone"
    source = support.build_fake_source(root)
    extra = source / "extra_subtree"
    extra.mkdir()
    (extra / "shipped.py").write_text("# a third cloned subtree\n")
    (source / ".gitignore").write_text(".venv/\n")
    _init_repo(source)
    releases = root / "releases"

    original = rm_module.CODE_SUBTREES
    rm_module.CODE_SUBTREES = (*original, "extra_subtree")  # type: ignore[misc]
    try:
        # (a) the widened constant reaches the CLONE.
        candidate = _build_or_fail(rec, tag, support.make_manager(source, releases))
        rec.check(
            candidate is not None
            and (
                releases / candidate.release_id  # type: ignore[attr-defined]
                / "code" / "extra_subtree" / "shipped.py"
            ).is_file(),
            f"[{tag}] the clone loop ships the added subtree",
        )
        # (b) the SAME constant reaches the GATE — dirt in the added subtree refuses.
        (extra / "shipped.py").write_text("# edited after the baseline commit\n")
        exc = _capture(support.make_manager(source, releases / "second").build_candidate)
        rec.check(
            _is_pre_clone_refusal(exc),
            f"[{tag}] dirt in the added subtree REFUSES AT THE PRE-CLONE GATE — "
            f"that gate reads the same constant the clone does, not a second "
            f"literal list. (A post-clone refusal here would mean the pre-clone "
            f"scope had drifted and the re-check was covering for it.) "
            f"(got {str(exc)[:90]!r})",
        )
    finally:
        rm_module.CODE_SUBTREES = original  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Leg 5a — the release carries setup contracts and their editable target
# ---------------------------------------------------------------------------

def _case_setup_contracts_ship(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "setup-contracts-carry"
    source = support.build_fake_source(root)
    candidate = _build_or_fail(rec, tag, support.make_manager(source, root / "releases"))
    rec.check(
        candidate is not None
        and (
            candidate.code_root  # type: ignore[attr-defined]
            / "solet_setup_contracts" / "src" / "solet_setup_contracts" / "__init__.py"
        ).is_file(),
        f"[{tag}] release code/ carries solet_setup_contracts for ananta imports",
    )
    rec.check(
        candidate is not None and candidate.missing_pth_targets == (),  # type: ignore[attr-defined]
        f"[{tag}] setup-contracts editable target resolves in release code/",
    )


# ---------------------------------------------------------------------------
# Leg 6 — untracked files ship too, so untracked dirt must refuse
# ---------------------------------------------------------------------------

def _case_untracked_refuses(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "untracked"
    source = _make_repo_source(root)
    releases = root / "releases"
    mgr = support.make_manager(source, releases)

    new_file = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "brand_new.py"
    new_file.write_text("# never committed, but cp -cR ships it\n")

    exc = _capture(mgr.build_candidate)
    rec.check(
        _is_pre_clone_refusal(exc),
        f"[{tag}] a NEW untracked .py under plugins/ refuses at the PRE-CLONE "
        f"gate — the clone is cp -cR, which ships untracked files (got "
        f"{str(exc)[:80]!r})",
    )
    rec.check(
        exc is not None and "brand_new.py" in str(exc),
        f"[{tag}] the refusal names the untracked file",
    )


# ---------------------------------------------------------------------------
# Leg 7 (not in the ruling table; the ruling assumed a git repo) — a tree git
# cannot attest is UNKNOWN, never clean, and does not refuse.
# ---------------------------------------------------------------------------

def _case_unattestable_is_not_clean(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "unattestable"
    source = support.build_fake_source(root)  # deliberately NOT a git repo
    releases = root / "releases"
    handler = _CapturingHandler()
    logger = logging.getLogger(f"dirty-gate-smoke.{tag}")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    mgr = support.make_manager(source, releases, logger=logger)

    candidate = _build_or_fail(rec, tag, mgr)
    if candidate is None:
        logger.removeHandler(handler)
        return
    payload = _read_version(releases, candidate.release_id)  # type: ignore[attr-defined]
    rec.check(
        payload.get("tree_state") == TREE_STATE_UNKNOWN,
        f"[{tag}] a non-repo source tree attests UNKNOWN, never clean "
        f"(got {payload.get('tree_state')!r}) — the one inference the field must "
        "never support is 'could not measure' ⇒ 'clean'",
    )
    warned = [r.getMessage() for r in handler.records if r.levelno >= logging.WARNING]
    rec.check(
        any("UNATTESTABLE for" in m for m in warned),
        f"[{tag}] the PRE-clone attestation is what reports it unmeasurable — "
        f"not the post-clone re-check downgrading a bogus 'clean' after the "
        f"fact, which would mean the gate itself had stopped detecting it "
        f"(got {warned})",
    )
    logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Leg 8 — the TORN-SNAPSHOT specimen from the live 20:41Z incident: files in
# the artifact matching NEITHER HEAD NOR the worktree. Coordinator-Dawn asked
# whether the scoped porcelain catches this, since a torn file has no
# reviewable provenance at all — you cannot reconstruct what shipped from a
# SHA plus a diff. It is not a distinct tree state at GATE time: a file must
# already be dirty at build time in order to be torn later, so the refusal
# fires on the same measurement leg 1 makes. This leg pins that reasoning to
# an executable reproduction of the real sequence.
# ---------------------------------------------------------------------------

def _case_torn_snapshot_refuses(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "torn-snapshot"
    source = _make_repo_source(root)
    releases = root / "releases"
    mgr = support.make_manager(source, releases)

    torn = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "__init__.py"
    committed = torn.read_text()
    torn.write_text("# edit v1 — what a build at this instant would capture\n")

    exc = _capture(mgr.build_candidate)
    rec.check(
        _is_pre_clone_refusal(exc),
        f"[{tag}] the build that WOULD have torn the file is refused at PRE-CLONE "
        f"gate time — before anything is copied (got {str(exc)[:80]!r})",
    )

    # The lane keeps editing after the build attempt — the second half of the
    # real sequence, which is what made the shipped copy match neither side.
    torn.write_text("# edit v2 — the lane kept going after the build\n")
    rec.check(
        torn.read_text() != committed,
        f"[{tag}] PRECONDITION: the file now matches neither HEAD nor the "
        "build-time content — the torn state the incident produced",
    )
    exc_again = _capture(mgr.build_candidate)
    rec.check(
        isinstance(exc_again, ReleaseManagerError),
        f"[{tag}] still refused after the follow-up edit — 'torn' is not a state "
        f"that escapes a gate keyed on dirty-vs-HEAD "
        f"(got {type(exc_again).__name__ if exc_again else 'no raise'})",
    )
    rec.check(
        _staging_dirs(releases) == [],
        f"[{tag}] no torn artifact materialized: {[p.name for p in _staging_dirs(releases)]}",
    )


# ---------------------------------------------------------------------------
# Leg 9 — the window leg 8 CANNOT close: the tree is clean when the gate runs
# and is edited while ``cp -cR`` is still copying. This is the only leg that
# injects a dirty-paths resolver, deliberately: what is under test is whether
# the builder re-measures after the clone and compares, not what git reports.
# A real mid-clone edit is a race no fixture can schedule reliably.
# ---------------------------------------------------------------------------

def _case_mid_clone_edit_refuses(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "mid-clone"
    source = _make_repo_source(root)
    releases = root / "releases"

    calls: list[int] = []

    def racing_resolver(_root: Path, _subtrees: tuple[str, ...]) -> tuple[str, ...]:
        """Clean at gate time; dirty by the time the clone finishes."""
        calls.append(1)
        return () if len(calls) == 1 else (" M plugins/foo_plugin/src/foo_plugin/__init__.py",)

    mgr = rm_module.ReleaseManager(
        solet_name="smoke",
        source_root=source,
        releases_root=releases,
        clock=lambda: datetime(2026, 8, 1, 20, 41, tzinfo=UTC),
        git_sha_resolver=lambda _p: "7b3f011ce",
        dirty_paths_resolver=racing_resolver,
    )

    exc = _capture(mgr.build_candidate)
    rec.check(
        len(calls) >= 2,
        f"[{tag}] the builder measures the tree AGAIN after the clone "
        f"(resolver called {len(calls)}x — a single call cannot detect the race)",
    )
    rec.check(
        isinstance(exc, ReleaseManagerError) and _MID_CLONE_REFUSAL in str(exc),
        f"[{tag}] a tree that moves mid-clone REFUSES via the POST-clone re-check "
        f"rather than shipping a torn artifact (got {str(exc)[:90]!r})",
    )
    rec.check(
        _staging_dirs(releases) == [],
        f"[{tag}] the half-built staging dir is cleaned up: "
        f"{[p.name for p in _staging_dirs(releases)]}",
    )


# ---------------------------------------------------------------------------
# Leg 10 — the override must not launder a mid-clone change into a clean
# attestation. allow_dirty is the ONE path where a torn artifact is allowed to
# exist, which makes it the one path where VERSION has to describe it
# honestly: ruling §3 (absence/clean is never the fallback) and §4 (the
# override stamps the FULL porcelain list) both apply here, not just to the
# pre-clone measurement.
# ---------------------------------------------------------------------------

def _case_override_attests_the_post_clone_truth(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "override-post-clone"
    source = _make_repo_source(root)
    releases = root / "releases"
    appeared_line = " M plugins/foo_plugin/src/foo_plugin/late.py"
    calls: list[int] = []

    def racing_resolver(_root: Path, _subtrees: tuple[str, ...]) -> tuple[str, ...]:
        calls.append(1)
        return () if len(calls) == 1 else (appeared_line,)

    mgr = rm_module.ReleaseManager(
        solet_name="smoke",
        source_root=source,
        releases_root=releases,
        clock=lambda: datetime(2026, 8, 1, 20, 42, tzinfo=UTC),
        git_sha_resolver=lambda _p: "7b3f011ce",
        dirty_paths_resolver=racing_resolver,
    )

    candidate = _build_or_fail(rec, tag, mgr, allow_dirty=True)
    if candidate is None:
        return
    payload = _read_version(releases, candidate.release_id)  # type: ignore[attr-defined]
    rec.check(
        payload.get("tree_state") == TREE_STATE_DIRTY,
        f"[{tag}] clean-at-gate-time + changed-mid-clone + allow_dirty ⇒ VERSION "
        f"says DIRTY, not clean (got {payload.get('tree_state')!r}) — the "
        "override must not launder a torn artifact into a clean attestation",
    )
    rec.check(
        payload.get("dirty_paths") == [appeared_line],
        f"[{tag}] and it records the POST-clone population, not the empty "
        f"pre-clone one (got {payload.get('dirty_paths')!r})",
    )


# ---------------------------------------------------------------------------
# Leg 11 — measurable before the clone, unmeasurable after. We cannot say the
# clone captured a clean tree, so we must not claim it did.
# ---------------------------------------------------------------------------

def _case_post_clone_unmeasurable_downgrades(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "post-clone-unmeasurable"
    source = _make_repo_source(root)
    releases = root / "releases"
    calls: list[int] = []

    def failing_after(_root: Path, _subtrees: tuple[str, ...]) -> tuple[str, ...] | None:
        calls.append(1)
        return () if len(calls) == 1 else None

    mgr = rm_module.ReleaseManager(
        solet_name="smoke",
        source_root=source,
        releases_root=releases,
        clock=lambda: datetime(2026, 8, 1, 20, 43, tzinfo=UTC),
        git_sha_resolver=lambda _p: "7b3f011ce",
        dirty_paths_resolver=failing_after,
    )

    candidate = _build_or_fail(rec, tag, mgr)
    if candidate is None:
        return
    payload = _read_version(releases, candidate.release_id)  # type: ignore[attr-defined]
    rec.check(
        payload.get("tree_state") == TREE_STATE_UNKNOWN,
        f"[{tag}] clean at gate time but unmeasurable after the clone ⇒ "
        f"tree_state downgrades to unknown, it does NOT keep the stale clean "
        f"(got {payload.get('tree_state')!r})",
    )


# ---------------------------------------------------------------------------
# Leg 12 — the mirror of leg 11, and the ONLY case that isolates the pre-clone
# attestation's unknown-handling. Git fails at gate time and succeeds after
# (a transient), so the post-clone re-check cannot rescue a wrong pre-clone
# verdict: it sees an unchanged population and defers. If _attest_tree_state
# called an unmeasurable tree "clean", that claim would reach VERSION intact —
# for a tree nobody ever attested while it was being copied.
# ---------------------------------------------------------------------------

def _case_pre_clone_unmeasurable_survives_a_later_success(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "pre-clone-unmeasurable"
    source = _make_repo_source(root)
    releases = root / "releases"
    calls: list[int] = []

    def failing_first(_root: Path, _subtrees: tuple[str, ...]) -> tuple[str, ...] | None:
        calls.append(1)
        return None if len(calls) == 1 else ()

    mgr = rm_module.ReleaseManager(
        solet_name="smoke",
        source_root=source,
        releases_root=releases,
        clock=lambda: datetime(2026, 8, 1, 20, 44, tzinfo=UTC),
        git_sha_resolver=lambda _p: "7b3f011ce",
        dirty_paths_resolver=failing_first,
    )

    candidate = _build_or_fail(rec, tag, mgr)
    if candidate is None:
        return
    payload = _read_version(releases, candidate.release_id)  # type: ignore[attr-defined]
    rec.check(
        payload.get("tree_state") == TREE_STATE_UNKNOWN,
        f"[{tag}] unmeasurable at gate time stays UNKNOWN even though the tree "
        f"measured clean afterwards — a clean reading AFTER the copy is not "
        f"evidence the tree was clean DURING it "
        f"(got {payload.get('tree_state')!r})",
    )


# ---------------------------------------------------------------------------
# Selected-file collector — ignored nested environments are exclusion evidence,
# not a second packaging specification. These legs use the real builder and
# the synthetic Git source; Git-Controller owns their execution.
# ---------------------------------------------------------------------------


def _ignore(source: Path, relative: str) -> None:
    """Ignore one synthetic nested environment without changing the index."""
    exclude = source / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + f"{relative}/\n")


def _make_nested_environment(root: Path, relative: str, *, python_link: bool = False) -> Path:
    environment = root / relative
    (environment / "bin").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /opt/python/bin\n")
    python = environment / "bin" / "python3"
    if python_link:
        os.symlink("python-real", python)
        (environment / "bin" / "python-real").write_text("#!/bin/sh\n")
    else:
        python.write_text("#!/bin/sh\n")
    (environment / "cache.pyc").write_bytes(b"nested cached bytecode")
    return environment


def _case_ignored_environment_is_excluded_and_manifested(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "selected-ignored-environment"
    source = _make_repo_source(root)
    relative = "plugins/foo_plugin/src/foo_plugin/.venv_cosyvoice"
    _ignore(source, relative)
    _make_nested_environment(source, relative, python_link=True)

    candidate = _build_or_fail(rec, tag, support.make_manager(source, root / "releases"))
    if candidate is None:
        return
    release = root / "releases" / candidate.release_id  # type: ignore[attr-defined]
    payload = _read_version(root / "releases", candidate.release_id)  # type: ignore[attr-defined]
    manifest = json.loads((release / SELECTED_FILE_MANIFEST_FILENAME).read_text())
    selected = manifest.get("selected_files", [])
    exclusions = manifest.get("exclusions", [])
    rec.check(
        not (release / "code" / relative).exists(),
        f"[{tag}] ignored nested environment is absent from materialized code/",
    )
    rec.check(
        all(relative not in str(row.get("path", "")) for row in selected),
        f"[{tag}] its cached file and interpreter link are absent from selected files",
    )
    rec.check(
        any(row.get("path") == relative and row.get("reason") == "nested_python_environment"
            for row in exclusions),
        f"[{tag}] manifest records positive pyvenv.cfg + interpreter exclusion evidence",
    )
    bound = payload.get("selected_file_manifest")
    rec.check(
        isinstance(bound, dict)
        and bound.get("filename") == SELECTED_FILE_MANIFEST_FILENAME
        and isinstance(bound.get("sha256"), str)
        and len(str(bound.get("sha256"))) == 64,
        f"[{tag}] VERSION binds the selected-file manifest SHA-256",
    )


def _case_reserved_name_without_environment_evidence_ships(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "selected-reserved-ordinary-source"
    source = _make_repo_source(root)
    ordinary = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / ".venv_notes"
    ordinary.mkdir()
    (ordinary / "readme.py").write_text("# ordinary source, not an environment\n")
    _git(source, "add", "-A")
    _git(source, *_GIT_IDENTITY, "commit", "--quiet", "-m", "ordinary reserved name")

    candidate = _build_or_fail(rec, tag, support.make_manager(source, root / "releases"))
    rec.check(
        candidate is not None
        and (root / "releases" / candidate.release_id / "code" / "plugins" / "foo_plugin"
             / "src" / "foo_plugin" / ".venv_notes" / "readme.py").is_file(),  # type: ignore[attr-defined]
        f"[{tag}] a similarly named ordinary directory ships without pyvenv evidence",
    )


def _case_links_refuse_before_finalize(rec: support.SmokeRecorder, root: Path) -> None:
    tag = "selected-links-refuse"
    source = _make_repo_source(root)
    link = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "escape.py"
    os.symlink("/etc/hosts", link)
    exc = _capture(
        lambda: support.make_manager(source, root / "releases").build_candidate(
            allow_dirty=True
        )
    )
    rec.check(
        isinstance(exc, ReleaseManagerError) and "symbolic link" in str(exc),
        f"[{tag}] retained file link refuses rather than following outside source root",
    )
    rec.check(
        _staging_dirs(root / "releases") == [],
        f"[{tag}] link refusal leaves no staging or finalized artifact",
    )


def _case_ignored_asset_link_is_excluded_before_traversal(
    rec: support.SmokeRecorder, root: Path
) -> None:
    """Local asset-reorg links are ignored content, not release code."""
    tag = "selected-ignored-asset-link"
    source = _make_repo_source(root)
    asset_dir = source / "plugins" / "foo_plugin" / "assets"
    library = source / "binary_libraries" / "foo_plugin" / "sample"
    library.mkdir(parents=True)
    (library / "payload.bin").write_bytes(b"local asset bytes")
    asset_dir.mkdir(parents=True)
    (asset_dir / "sample").symlink_to(
        "../../../binary_libraries/foo_plugin/sample", target_is_directory=True
    )
    _ignore(source, "plugins/foo_plugin/assets")
    _ignore(source, "binary_libraries")

    candidate = _build_or_fail(rec, tag, support.make_manager(source, root / "releases"))
    if candidate is None:
        return
    release = root / "releases" / candidate.release_id  # type: ignore[attr-defined]
    manifest = json.loads((release / SELECTED_FILE_MANIFEST_FILENAME).read_text())
    selected = manifest.get("selected_files", [])
    exclusions = manifest.get("exclusions", [])
    rec.check(
        not (release / "code" / "plugins" / "foo_plugin" / "assets").exists(),
        f"[{tag}] ignored asset link is absent from materialized code/",
    )
    rec.check(
        all("plugins/foo_plugin/assets" not in str(row.get("path", "")) for row in selected),
        f"[{tag}] ignored asset link is absent from selected files",
    )
    rec.check(
        any(
            row.get("path") == "plugins/foo_plugin/assets"
            and row.get("reason") == "gitignored_path"
            and row.get("evidence") == {"git_check_ignore": "matched"}
            for row in exclusions
        ),
        f"[{tag}] manifest records the Git-ignore exclusion before link traversal",
    )


def _case_tracked_link_refuses_before_finalize(
    rec: support.SmokeRecorder, root: Path
) -> None:
    """Git tracking never turns a symbolic link into an ignorable artifact."""
    tag = "selected-tracked-link-refuse"
    source = _make_repo_source(root)
    link = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "tracked_link.py"
    link.symlink_to("__init__.py")
    _git(source, "add", "-A")
    _git(source, *_GIT_IDENTITY, "commit", "--quiet", "-m", "tracked symbolic link")
    exc = _capture(
        lambda: support.make_manager(source, root / "releases").build_candidate()
    )
    rec.check(
        isinstance(exc, ReleaseManagerError) and "symbolic link" in str(exc),
        f"[{tag}] tracked symbolic link remains fail-closed",
    )
    rec.check(
        _staging_dirs(root / "releases") == [],
        f"[{tag}] tracked-link refusal leaves no staging or finalized artifact",
    )


def _case_artifact_head_membership_is_complete(
    rec: support.SmokeRecorder, root: Path
) -> None:
    """A tracked path deleted from the retained population is never equal."""
    tag = "artifact-head-membership"
    source = _make_repo_source(root)
    deleted = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "__init__.py"
    deleted.unlink()
    collector = ReleaseCodeCollector(
        source_root=source,
        code_subtrees=("plugins",),
        cp_binary="/bin/cp",
        clone_timeout_seconds=10,
    )
    equality = collector.manifest_payload(collector.plan())["artifact_to_head_equality"]
    assert isinstance(equality, dict)
    mismatches = equality.get("mismatches")
    rec.check(
        equality.get("state") == "unequal",
        f"[{tag}] deleted tracked scoped path ⇒ artifact-to-HEAD is unequal",
    )
    rec.check(
        isinstance(mismatches, list)
        and {"kind": "missing_from_artifact", "path": "plugins/foo_plugin/src/foo_plugin/__init__.py"}
        in mismatches,
        f"[{tag}] mismatch identifies the tracked path missing from artifact",
    )


def _case_artifact_head_executable_mode_is_complete(
    rec: support.SmokeRecorder, root: Path
) -> None:
    """Content identity does not hide a retained executable-bit change."""
    tag = "artifact-head-executable-mode"
    source = _make_repo_source(root)
    retained = source / "solet_cli" / "src" / "solet_manager" / "__init__.py"
    retained.chmod(0o755)
    collector = ReleaseCodeCollector(
        source_root=source,
        code_subtrees=("solet_cli",),
        cp_binary="/bin/cp",
        clone_timeout_seconds=10,
    )
    equality = collector.manifest_payload(collector.plan())["artifact_to_head_equality"]
    assert isinstance(equality, dict)
    mismatches = equality.get("mismatches")
    rec.check(
        equality.get("state") == "unequal",
        f"[{tag}] content-identical executable-mode change ⇒ artifact-to-HEAD is unequal",
    )
    rec.check(
        isinstance(mismatches, list)
        and {
            "kind": "executable_mode_differs",
            "path": "solet_cli/src/solet_manager/__init__.py",
            "head_mode": "100644",
            "artifact_mode": "0755",
        }
        in mismatches,
        f"[{tag}] mismatch identifies Git executable mode versus retained mode",
    )


def _case_tracked_environment_conflict_refuses(
    rec: support.SmokeRecorder, root: Path
) -> None:
    """A clean Git checkout cannot silently exclude tracked environment bytes."""
    tag = "tracked-environment-conflict"
    source = _make_repo_source(root)
    relative = "plugins/foo_plugin/src/foo_plugin/.venv_cosyvoice"
    _make_nested_environment(source, relative, python_link=False)
    _git(source, "add", "-A")
    _git(source, *_GIT_IDENTITY, "commit", "--quiet", "-m", "tracked environment")
    exc = _capture(_plain_collector(source).plan)
    rec.check(
        isinstance(exc, ReleaseCodeCollectionError) and "tracked or staged environment" in str(exc),
        f"[{tag}] tracked nested environment is an explicit collector conflict",
    )


def _case_staged_environment_conflict_refuses(
    rec: support.SmokeRecorder, root: Path
) -> None:
    """An index-only environment is also too important to omit silently."""
    tag = "staged-environment-conflict"
    source = _make_repo_source(root)
    relative = "plugins/foo_plugin/src/foo_plugin/.venv_cosyvoice"
    _make_nested_environment(source, relative, python_link=False)
    _git(source, "add", "-A")
    exc = _capture(_plain_collector(source).plan)
    rec.check(
        isinstance(exc, ReleaseCodeCollectionError) and "tracked or staged environment" in str(exc),
        f"[{tag}] staged nested environment is an explicit collector conflict",
    )


def _plain_collector(source: Path) -> ReleaseCodeCollector:
    """Construct a Gitless collector for checks that must not mutate Git."""
    return ReleaseCodeCollector(
        source_root=source,
        code_subtrees=("plugins",),
        cp_binary="/bin/cp",
        clone_timeout_seconds=10,
    )


def _make_snapshot_bytecode_fixture(root: Path) -> tuple[Path, Path]:
    """Build a minimal Git fixture for the real schema-snapshot subprocess.

    The source collector runs by file path while its import resolves from the
    materialized destination through ``PYTHONPATH`` — the same arrangement as
    the production schema snapshot. Importing ``foo_plugin`` therefore writes
    a cache under the destination unless bytecode output is explicitly off.
    """
    source = _make_repo_source(root)
    (source / ".gitignore").write_text(".venv/\n__pycache__/\n")
    source_python = source / ".venv" / "bin" / "python3"
    source_python.unlink()
    source_python.symlink_to(sys.executable)
    collector_path = (
        source / "plugins" / "macos_self_deployment_plugin" / "src"
        / "macos_self_deployment_plugin" / "schema_snapshot_collector.py"
    )
    collector_path.parent.mkdir(parents=True)
    collector_path.write_text("import foo_plugin\nprint('{}')\n")
    _git(source, "add", "-A")
    _git(source, *_GIT_IDENTITY, "commit", "--quiet", "-m", "snapshot fixture")
    return source, collector_path


def _run_pre_fix_snapshot_writer(source: Path, collector_path: Path, destination: Path) -> None:
    """Run the pre-fix producer environment, which permits destination caches."""
    env = dict(os.environ)
    env["PYTHONPATH"] = _pythonpath_for_tree(destination)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [str(source / ".venv" / "bin" / "python3"), str(collector_path)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(source),
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or result.stdout.strip() != "{}":
        raise RuntimeError(
            f"pre-fix snapshot fixture failed: exit={result.returncode}, stderr={result.stderr!r}"
        )


def _case_snapshot_import_does_not_pollute_materialized_tree(
    rec: support.SmokeRecorder, root: Path
) -> None:
    """The real snapshot producer must not invalidate collector verification."""
    tag = "snapshot-bytecode"
    source, collector_path = _make_snapshot_bytecode_fixture(root)
    collector = _plain_collector(source)
    plan = collector.plan()

    pre_fix_destination = root / "pre_fix_code"
    collector.materialize(plan, pre_fix_destination)
    _run_pre_fix_snapshot_writer(source, collector_path, pre_fix_destination)
    pre_fix_error = _capture(lambda: collector.verify_materialized(plan, pre_fix_destination))
    rec.check(
        isinstance(pre_fix_error, ReleaseCodeCollectionError)
        and "selected-file set differs" in str(pre_fix_error),
        f"[{tag}] pre-fix snapshot import writes destination bytecode and reproduces set refusal",
    )

    fixed_destination = root / "fixed_code"
    collector.materialize(plan, fixed_destination)
    snapshot_fn = build_schema_snapshot_fn(
        solet_name="smoke", app_home=root / "profile", source_root=source,
    )
    rec.check(
        snapshot_fn(fixed_destination) == {},
        f"[{tag}] real snapshot producer completes against materialized code",
    )
    rec.check(
        not list(fixed_destination.rglob("__pycache__")),
        f"[{tag}] real snapshot producer leaves no bytecode cache in destination",
    )
    rec.check(
        _capture(lambda: collector.verify_materialized(plan, fixed_destination)) is None,
        f"[{tag}] materialized selected-file verification still passes after snapshot",
    )


def _case_environment_root_symlink_refuses_without_git(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "environment-root-symlink-nongit"
    source = support.build_fake_source(root)
    target = root / "external_environment"
    (target / "bin").mkdir(parents=True)
    (target / "pyvenv.cfg").write_text("home = /opt/python/bin\n")
    (target / "bin" / "python3").write_text("#!/bin/sh\n")
    link = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / ".venv_external"
    link.symlink_to(target, target_is_directory=True)
    exc = _capture(_plain_collector(source).plan)
    rec.check(
        isinstance(exc, ReleaseCodeCollectionError) and "symbolic link" in str(exc),
        f"[{tag}] environment-root symbolic link refuses without following its target",
    )


def _case_dangling_link_refuses_without_git(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "dangling-link-nongit"
    source = support.build_fake_source(root)
    link = source / "plugins" / "foo_plugin" / "src" / "foo_plugin" / "dangling.py"
    link.symlink_to(root / "does-not-exist")
    exc = _capture(_plain_collector(source).plan)
    rec.check(
        isinstance(exc, ReleaseCodeCollectionError) and "symbolic link" in str(exc),
        f"[{tag}] dangling symbolic link refuses without target traversal",
    )


def _case_excluded_environment_pth_refuses_without_git(
    rec: support.SmokeRecorder, root: Path
) -> None:
    tag = "excluded-environment-pth-nongit"
    source = root / "source"
    source.mkdir(parents=True)
    relative = "plugins/foo_plugin/src/foo_plugin/.venv_cosyvoice"
    pth = root / "venv" / "lib" / "python3.13" / "site-packages" / "local.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(f"{source / relative}\n")
    exc = _capture(
        lambda: rm_module._repoint_and_validate_pth(
            root / "venv",
            source_root=source,
            final_code_root=root / "artifact" / "code",
            strict_pth_validation=True,
            logger=logging.getLogger(tag),
            excluded_roots=(relative,),
        )
    )
    rec.check(
        isinstance(exc, ReleaseManagerError) and "excluded nested Python environment" in str(exc),
        f"[{tag}] editable target into excluded environment refuses before rewrite",
    )


def _run_non_git_matrix(rec: support.SmokeRecorder, scratch: Path) -> None:
    """Run only matrix legs whose setup and assertion require no Git mutation."""
    _case_environment_root_symlink_refuses_without_git(rec, scratch / "environment_root_link")
    _case_dangling_link_refuses_without_git(rec, scratch / "dangling_link")
    _case_excluded_environment_pth_refuses_without_git(rec, scratch / "excluded_pth")


def main() -> int:
    rec = support.SmokeRecorder()
    scratch = support.scratch_root("dirty-gate")
    non_git_matrix_only = sys.argv[1:] == ["--non-git-matrix"]
    print("=== release_builder_dirty_tree_gate_smoke ===")
    print(f"scratch: {scratch}")
    try:
        if non_git_matrix_only:
            _run_non_git_matrix(rec, scratch / "non_git_matrix")
        else:
            _case_ships_dirty_refuses(rec, scratch / "ships_dirty")
            _case_non_shipped_dirt_builds(rec, scratch / "non_shipped")
            _case_positive_attestation(rec, scratch / "positive")
            _case_override_is_disclosed(rec, scratch / "override")
            _case_gate_follows_the_clone(rec, scratch / "follows_clone")
            _case_setup_contracts_ship(rec, scratch / "setup_contracts")
            _case_untracked_refuses(rec, scratch / "untracked")
            _case_unattestable_is_not_clean(rec, scratch / "unattestable")
            _case_torn_snapshot_refuses(rec, scratch / "torn")
            _case_mid_clone_edit_refuses(rec, scratch / "mid_clone")
            _case_override_attests_the_post_clone_truth(rec, scratch / "override_post")
            _case_post_clone_unmeasurable_downgrades(rec, scratch / "post_unmeasurable")
            _case_pre_clone_unmeasurable_survives_a_later_success(rec, scratch / "pre_unmeas")
            _case_ignored_environment_is_excluded_and_manifested(rec, scratch / "selected_env")
            _case_reserved_name_without_environment_evidence_ships(rec, scratch / "selected_ordinary")
            _case_links_refuse_before_finalize(rec, scratch / "selected_links")
            _case_ignored_asset_link_is_excluded_before_traversal(rec, scratch / "selected_ignored_link")
            _case_tracked_link_refuses_before_finalize(rec, scratch / "selected_tracked_link")
            _case_artifact_head_membership_is_complete(rec, scratch / "artifact_head_membership")
            _case_artifact_head_executable_mode_is_complete(rec, scratch / "artifact_head_mode")
            _case_tracked_environment_conflict_refuses(rec, scratch / "tracked_environment")
            _case_staged_environment_conflict_refuses(rec, scratch / "staged_environment")
            _case_snapshot_import_does_not_pollute_materialized_tree(rec, scratch / "snapshot_bytecode")
            _run_non_git_matrix(rec, scratch / "non_git_matrix")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return rec.report("dirty-tree gate")


if __name__ == "__main__":
    sys.exit(main())
