#!/usr/bin/env python3
"""Offline smoke for spawn-env survival across a blue-green deploy.

Systemic finding of record (filed 2026-08-16 23:3xZ, closed by this suite's
subject): every spawn surface pinned the solet-bridge CLI into a VERSIONED release
directory — ``~/.ananta/releases/<name>/rel-<ts>-<sha>/venv/bin/solet-bridge`` — as
``AGENT_WAKE_CLI``, as the ``PATH`` prepend, and as ``argv[0]`` of the
long-lived ``solet-bridge watch`` registration sidecar. A deploy REAPS old releases
(``release_manager`` keeps the last K, default 3), so a single cutover
dangled every long-running worker's CLI at once. Every consumer of those
values is deliberately non-fatal, so the whole failure was silent: no
liveness stamp, no wake, no registration, exit 0 everywhere. Measured twice
(lane-ai dark both ways across two deploys, 2026-08-16).

The fix expresses the resolved path through the deployment's ``current``
pointer, which cutover swaps atomically.

Named mutations this suite must catch:

* ``stable_release_path`` reverting to a no-op (the pre-fix pin), on either
  the direct call or through ``resolve_solet_bin``;
* the rewrite RESOLVING the symlink (``Path.resolve()``, ``os.path.realpath``,
  ``Path.readlink()`` chasing) — which re-pins the versioned directory and
  makes the fix decorative while every "does it still resolve today?" check
  stays green;
* the rewrite firing when ``current`` names a DIFFERENT release than the path
  came from (a silent version substitution during cutover skew);
* the rewrite firing onto a REAPED release — a ``current`` pointer whose
  target no longer exists must not produce a confidently-wrong stable path;
* dropping the stat/exec check, so a non-existent rewrite is handed to a
  worker;
* the stabilization being applied at ``expose_worker_cli`` only, leaving
  ``watch_sidecar_argv``/``worker_path`` (and therefore registration itself)
  still version-pinned;
* any regression of the Repair-4 property: a dangling pointer must never
  break a session that would otherwise resolve ``solet-bridge`` from PATH, and must
  never raise.

PURE UNIT, offline: a synthetic release layout in a temp dir with real
symlinks and real executable stubs. No tmux server, bridge, database,
network, deploy, or model turn.

RUNNER-INDEPENDENT BY CONSTRUCTION. ``resolve_solet_bin`` tries
``shutil.which("solet-bridge")`` first, so any leg exercising a LATER rung is
ambient-PATH sensitive: a runner whose own PATH already resolves a real
``solet-bridge`` takes that rung instead and the assertion silently becomes a claim
about the runner's machine. Every such leg pins PATH (``_path_exactly``).
Verified both ways -- green with a real release ``solet-bridge`` on PATH and green
with an empty PATH -- because "passes where I ran it" is not the property.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import solet_cli  # noqa: E402
from agent_messaging_plugin.solet_cli import (  # noqa: E402
    CURRENT_LINK_NAME,
    expose_worker_cli,
    resolve_solet_bin,
    stable_release_path,
    watch_sidecar_argv,
    worker_path,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _make_release(releases: Path, release_id: str) -> Path:
    """A materialized release, laid out exactly as ``release_manager`` does:
    ``<releases>/<release_id>/venv/bin/solet-bridge``, executable."""
    bin_dir = releases / release_id / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "solet-bridge"
    cli.write_text(f"#!/bin/sh\necho {release_id}\n")
    cli.chmod(0o755)
    return cli


def _make_executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return executable


@contextmanager
def _managed_release_root(releases: Path) -> Iterator[None]:
    """Bind resolver ownership checks to this smoke's synthetic instance."""
    original = solet_cli.ANANTA_RELEASES_ROOT
    solet_cli.ANANTA_RELEASES_ROOT = releases.parent
    try:
        yield
    finally:
        solet_cli.ANANTA_RELEASES_ROOT = original


def _point(releases: Path, release_id: str, *, absolute: bool = False) -> None:
    """(Re)point ``current`` at ``release_id``, the way cutover does: write a
    fresh link and ``os.replace`` it over the old one, so the swap is atomic
    and the link text stays relative unless a caller asks otherwise."""
    target = str(releases / release_id) if absolute else release_id
    staged = releases / f".{CURRENT_LINK_NAME}.staged"
    staged.symlink_to(target)
    os.replace(staged, releases / CURRENT_LINK_NAME)


def test_versioned_path_is_rewritten_onto_the_stable_pointer() -> None:
    print("\na versioned release path is expressed through `current`")
    with tempfile.TemporaryDirectory() as tmp:
        releases = Path(tmp) / "releases" / "testsolet"
        releases.mkdir(parents=True)
        versioned = _make_release(releases, "rel-20260817T005356Z-b518d3138")
        _point(releases, "rel-20260817T005356Z-b518d3138")

        stable = stable_release_path(str(versioned))
        _check(stable != str(versioned), "the versioned path is not returned as-is")
        _check(
            Path(stable).parts[-4:] == (CURRENT_LINK_NAME, "venv", "bin", "solet-bridge"),
            "rewritten onto <releases>/current/venv/bin/solet-bridge, tail preserved",
        )
        # The decorative-fix guard. A rewrite that resolves the symlink still
        # names a real file TODAY, so every liveness check passes and the pin
        # survives untouched. The property is textual: the release id must be
        # ABSENT from what lands in the environment.
        _check(
            "rel-20260817T005356Z-b518d3138" not in stable,
            "UNRESOLVED: no release id survives in the stable path",
        )
        _check(
            Path(stable).is_file() and os.access(stable, os.X_OK),
            "the stable path stats as an executable file",
        )
        _check(
            os.path.samefile(stable, versioned),
            "and it is the same file the versioned path named",
        )


def test_survives_a_deploy_swap_that_reaps_the_old_release() -> None:
    print("\nDEPLOY-SWAP SIMULATION: old release removed, pointer moved")
    with tempfile.TemporaryDirectory() as tmp:
        releases = Path(tmp) / "releases" / "testsolet"
        releases.mkdir(parents=True)
        old = _make_release(releases, "rel-20260816T152738Z-dd0ebb517")
        python_stub = _make_executable(old.parent, "python3")
        _point(releases, "rel-20260816T152738Z-dd0ebb517")

        # What a worker spawned BEFORE the deploy carries in its environment.
        env: dict[str, str] = {"PATH": "/usr/bin:/bin"}
        with _managed_release_root(releases):
            resolved_old = resolve_solet_bin(
                str(old), python_executable=str(python_stub),
            )
        expose_worker_cli(env, resolved_old)
        pinned_cli = env["AGENT_WAKE_CLI"]
        pinned_path = env["PATH"]
        sidecar = watch_sidecar_argv(resolved_old, agent_id="claude_code", spool=True)
        _check(
            "rel-20260816T152738Z-dd0ebb517" not in pinned_cli
            and "rel-20260816T152738Z-dd0ebb517" not in pinned_path
            and "rel-20260816T152738Z-dd0ebb517" not in sidecar[0],
            "no spawn surface carries a release id: env, PATH prepend, sidecar argv",
        )

        # The deploy: a new release is materialized, `current` swaps, the old
        # release directory is REAPED. This is the exact sequence that went
        # dark twice on 2026-08-16.
        _make_release(releases, "rel-20260817T005356Z-b518d3138")
        _point(releases, "rel-20260817T005356Z-b518d3138")
        shutil.rmtree(releases / "rel-20260816T152738Z-dd0ebb517")

        # Negative control: the simulation really did reap something. Without
        # this, a swap that silently no-op'd would let every assertion below
        # pass for the wrong reason.
        _check(
            not Path(old).exists(),
            "negative control: the pre-deploy versioned path is genuinely gone",
        )

        _check(
            Path(pinned_cli).is_file(),
            "the worker's AGENT_WAKE_CLI still names a file AFTER the deploy",
        )
        ran = subprocess.run(
            [pinned_cli], capture_output=True, text=True, check=False,
        )
        _check(
            ran.returncode == 0
            and ran.stdout.strip() == "rel-20260817T005356Z-b518d3138",
            "and it EXECUTES, now serving the new release",
        )
        _check(
            shutil.which("solet-bridge", path=pinned_path) is not None,
            "the worker's prepended PATH still resolves a bare `solet-bridge`",
        )
        _check(
            Path(sidecar[0]).is_file(),
            "the registration sidecar's argv[0] survives too "
            "(registration, not just wake)",
        )
        _check(
            shutil.which("solet-bridge", path=worker_path(pinned_cli)) is not None,
            "worker_path() — the Codex shell_environment_policy PATH — survives",
        )


def test_refuses_to_rewrite_across_a_version_skew() -> None:
    print("\nmid-cutover skew is REFUSED, never silently substituted")
    with tempfile.TemporaryDirectory() as tmp:
        releases = Path(tmp) / "releases" / "testsolet"
        releases.mkdir(parents=True)
        running = _make_release(releases, "rel-aaa")
        _make_release(releases, "rel-bbb")
        _point(releases, "rel-bbb")
        _check(
            stable_release_path(str(running)) == str(running),
            "a path from rel-aaa is NOT rewritten while current names rel-bbb",
        )


def test_refuses_a_pointer_whose_target_was_reaped() -> None:
    print("\na `current` pointing at a reaped release yields no stable path")
    with tempfile.TemporaryDirectory() as tmp:
        releases = Path(tmp) / "releases" / "testsolet"
        releases.mkdir(parents=True)
        cli = _make_release(releases, "rel-aaa")
        _point(releases, "rel-aaa")
        shutil.rmtree(releases / "rel-aaa" / "venv")
        _check(
            stable_release_path(str(cli)) == str(cli),
            "the stat guard refuses a rewrite that would not exist",
        )


def test_absolute_link_text_is_honored() -> None:
    print("\n`current` written with an ABSOLUTE target still matches")
    with tempfile.TemporaryDirectory() as tmp:
        releases = Path(tmp) / "releases" / "testsolet"
        releases.mkdir(parents=True)
        cli = _make_release(releases, "rel-aaa")
        _point(releases, "rel-aaa", absolute=True)
        _check(
            CURRENT_LINK_NAME in Path(stable_release_path(str(cli))).parts,
            "link-text comparison handles absolute and relative targets alike",
        )


def test_non_release_paths_and_degraded_inputs_are_untouched() -> None:
    print("\nno release layout is assumed anywhere; degraded input never raises")
    _check(stable_release_path("") == "", "empty stays empty (the degraded rung)")
    _check(
        stable_release_path("solet-bridge") == "solet-bridge",
        "the bare command name is not a path and is left alone",
    )
    _check(
        stable_release_path("/usr/local/bin/solet-bridge") == "/usr/local/bin/solet-bridge",
        "an operator-installed CLI outside any release layout is untouched",
    )
    with tempfile.TemporaryDirectory() as tmp:
        releases = Path(tmp) / "releases" / "testsolet"
        releases.mkdir(parents=True)
        cli = _make_release(releases, "rel-aaa")
        _check(
            stable_release_path(str(cli)) == str(cli),
            "a release dir with NO current pointer is untouched",
        )
        (releases / CURRENT_LINK_NAME).symlink_to("rel-never-existed")
        _check(
            stable_release_path(str(cli)) == str(cli),
            "a BROKEN current pointer is untouched and does not raise "
            "(Repair-4: a dangling pointer never breaks a working session)",
        )


@contextmanager
def _path_exactly(value: str) -> Iterator[None]:
    """Pin ``PATH`` for the duration of the block.

    EVERY rung below is ambient-PATH sensitive, because ``resolve_solet_bin``
    tries ``shutil.which("solet")` FIRST. A runner whose own PATH already
    resolves a real ``solet`` -- which is exactly what an operator-launched
    session or a release-hosted worker looks like -- silently takes that rung
    instead of the one under test, and the assertions become claims about the
    RUNNER'S environment rather than about the code. Pinning PATH is what makes
    these legs mean the same thing in every environment.
    """
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = value
    try:
        yield
    finally:
        os.environ["PATH"] = previous


def test_resolution_rungs_all_stabilize() -> None:
    print("\nevery rung of resolve_solet_bin lands stable, not just one")
    with tempfile.TemporaryDirectory() as tmp:
        releases = Path(tmp) / "releases" / "testsolet"
        releases.mkdir(parents=True)
        cli = _make_release(releases, "rel-aaa")
        _point(releases, "rel-aaa")
        bin_dir = str(Path(cli).parent)
        python_stub = _make_executable(Path(bin_dir), "python3")
        # A directory guaranteed to contain no `solet-bridge`, so the PATH rung
        # cannot fire for the two legs that are not testing it.
        empty_dir = Path(tmp) / "empty-bin"
        empty_dir.mkdir()

        with _managed_release_root(releases):
            _check(
                CURRENT_LINK_NAME in Path(
                    resolve_solet_bin(str(cli), python_executable=str(python_stub)),
                ).parts,
                "explicit override is stabilized",
            )
            # The venv-sibling rung: the release's python3 and solet are siblings,
            # which is how a materialized release with a minimal PATH resolves.
            with _path_exactly(str(empty_dir)):
                from_sibling = resolve_solet_bin(
                    None, python_executable=str(python_stub),
                )
            _check(
                CURRENT_LINK_NAME in Path(from_sibling).parts,
                "the active-venv sibling rung is stabilized",
            )
            # The PATH rung, exercised through the real shutil.which by handing it
            # a PATH containing only the release bin dir.
            with _path_exactly(bin_dir):
                _check(
                    CURRENT_LINK_NAME in Path(
                        resolve_solet_bin(None, python_executable=str(python_stub)),
                    ).parts,
                    "the PATH-discovery rung is stabilized",
                )
            with _path_exactly(str(empty_dir)):
                unresolvable = resolve_solet_bin(
                    None, python_executable="/nope/python3",
                )
            _check(
                unresolvable == "",
                "the unresolvable case still returns '' — degradation is unchanged",
            )


def _adapter_solet_bin_readers() -> list[tuple[str, object]]:
    """The four spawn adapters, each as ``(name, factory)``.

    ``factory`` takes the constructor's ``solet_bin`` override and returns a
    zero-argument callable reading the adapter's wake-CLI spawn surface — the
    same attribute every one of their spawn paths reads. Kept as a table because
    the R11 defect was IDENTICAL in all four and a leg that covers only the one
    that got measured is the miswiring-fix-on-one-consumer shape in test form.
    """
    from agent_messaging_plugin.codex_app_server import (  # noqa: PLC0415
        CodexAppServerHostDriver,
    )
    from agent_messaging_plugin.codex_tmux import CodexTmuxHostDriver  # noqa: PLC0415
    from agent_messaging_plugin.headless_adapter import (  # noqa: PLC0415
        HeadlessHostDriver,
    )
    from agent_messaging_plugin.tmux_adapter import TmuxHostDriver  # noqa: PLC0415

    def _tmux(cli: str | None, cwd: Path, python_executable: str) -> object:
        d = TmuxHostDriver(
            solet_bin=cli, solet_name="testsolet", cwd=cwd,
            python_executable=python_executable,
        )
        return lambda: d._solet_bin  # noqa: SLF001
    def _headless(cli: str | None, cwd: Path, python_executable: str) -> object:
        d = HeadlessHostDriver(
            solet_bin=cli, solet_name="testsolet", cwd=cwd,
            python_executable=python_executable,
        )
        return lambda: d._solet_bin  # noqa: SLF001
    def _codex_tmux(cli: str | None, cwd: Path, python_executable: str) -> object:
        d = CodexTmuxHostDriver(
            solet_bin=cli, solet_name="testsolet", cwd=cwd,
            python_executable=python_executable,
        )
        return lambda: d._solet_bin  # noqa: SLF001
    def _codex_app(cli: str | None, cwd: Path, python_executable: str) -> object:
        d = CodexAppServerHostDriver(
            solet_bin=cli, solet_name="testsolet", cwd=cwd,
            python_executable=python_executable,
        )
        return lambda: d._solet_bin  # noqa: SLF001

    return [
        ("TmuxHostDriver", _tmux),
        ("HeadlessHostDriver", _headless),
        ("CodexTmuxHostDriver", _codex_tmux),
        ("CodexAppServerHostDriver", _codex_app),
    ]


def test_foreign_path_cannot_supply_an_in_instance_cli() -> None:
    """The global manager and a formula-keg CLI must not cross this boundary."""
    print("\nforeign PATH candidates are refused by resolution and every adapter")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        releases = root / "releases" / "A"
        releases.mkdir(parents=True)
        instance_cli = _make_release(releases, "rel-instance")
        _point(releases, "rel-instance")
        instance_python = _make_executable(instance_cli.parent, "python3")
        expected_cli = stable_release_path(str(instance_cli))
        sibling_releases = releases.parent / "B"
        sibling_release_cli = _make_release(sibling_releases, "rel-sibling")
        _point(sibling_releases, "rel-sibling")
        sibling_current_cli = (
            sibling_releases / CURRENT_LINK_NAME / "venv" / "bin" / "solet-bridge"
        )
        similar_root_cli = _make_release(
            root / "releases-similar" / "A", "rel-similar",
        )
        foreign_bin = root / "opt" / "homebrew" / "bin"
        foreign_cli = _make_executable(foreign_bin, "solet-bridge")
        keg_cli = _make_executable(
            root / "opt" / "homebrew" / "Cellar" / "solet" / "1.0" / "libexec"
            / "venv" / "bin",
            "solet-bridge",
        )
        arbitrary_venv_cli = _make_executable(
            root / "vendor" / "venv" / "bin", "solet-bridge",
        )
        with _managed_release_root(releases):
            for label, foreign in (
                ("global manager", foreign_cli),
                ("formula keg", keg_cli),
                ("arbitrary foreign venv", arbitrary_venv_cli),
            ):
                with _path_exactly(str(Path(foreign).parent)):
                    _check(
                        resolve_solet_bin(
                            None, python_executable=str(instance_python),
                        ) == expected_cli,
                        f"{label}: direct resolution rejects foreign PATH before the instance CLI",
                    )
                    for adapter_name, factory in _adapter_solet_bin_readers():
                        reader = factory(None, root, str(instance_python))
                        _check(
                            reader() == expected_cli,
                            f"{label}: {adapter_name} exports the instance CLI, never the foreign one",
                        )

            for label, foreign in (
                ("sibling instance rel-*", sibling_release_cli),
                ("sibling instance current", sibling_current_cli),
            ):
                with _path_exactly(str(Path(foreign).parent)):
                    _check(
                        resolve_solet_bin(
                            str(foreign), python_executable=str(instance_python),
                        ) == "",
                        f"{label}: explicit foreign instance CLI is rejected",
                    )
                    _check(
                        resolve_solet_bin(
                            None, python_executable=str(instance_python),
                        ) == expected_cli,
                        f"{label}: foreign PATH falls through only to A's owned CLI",
                    )

            _check(
                resolve_solet_bin(
                    str(similar_root_cli), python_executable=str(instance_python),
                ) == "",
                "a rel-* CLI under releases-similar/A is rejected despite the matching shape",
            )


def test_active_environment_and_release_family_prove_ownership() -> None:
    """Positive counterparts: active venv and its own release family remain valid."""
    print("\nownership accepts the active environment and its release family")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        developer_bin = root / "checkout" / ".venv" / "bin"
        developer_cli = _make_executable(developer_bin, "solet-bridge")
        framework_python = _make_executable(
            root / "opt" / "homebrew" / "Cellar" / "python@3.13" / "bin", "python3",
        )
        developer_python = developer_bin / "python3"
        developer_python.symlink_to(framework_python)
        nested_foreign_cli = _make_executable(
            root / "checkout" / "vendor" / "venv" / "bin", "solet-bridge",
        )
        with _path_exactly(str(Path(nested_foreign_cli).parent)):
            _check(
                resolve_solet_bin(
                    str(nested_foreign_cli),
                    python_executable=str(developer_python),
                ) == "",
                "an explicit nested foreign venv is rejected despite living under the checkout",
            )
            _check(
                resolve_solet_bin(
                    str(developer_cli), python_executable=str(developer_python),
                ) == str(developer_cli),
                "the active Python environment's venv CLI is accepted",
            )
            _check(
                resolve_solet_bin(
                    None, python_executable=str(developer_python),
                ) == str(developer_cli),
                "a nested foreign venv on PATH falls through to the active venv sibling",
            )

        releases = root / ".ananta" / "releases" / "testsolet"
        releases.mkdir(parents=True)
        active_cli = _make_release(releases, "rel-active")
        active_python = _make_executable(active_cli.parent, "python3")
        peer_cli = _make_release(releases, "rel-peer")
        nested_release_foreign_cli = _make_executable(
            releases / "rel-peer" / "vendor" / "venv" / "bin", "solet-bridge",
        )
        _point(releases, "rel-active")
        with _managed_release_root(releases):
            _check(
                resolve_solet_bin(
                    str(active_cli), python_executable=str(active_python),
                ) == str(releases / CURRENT_LINK_NAME / "venv" / "bin" / "solet-bridge"),
                "the active release is stabilized through current",
            )
            _check(
                resolve_solet_bin(
                    str(peer_cli), python_executable=str(active_python),
                ) == str(peer_cli),
                "a rel-* CLI in the active instance's release root is accepted without skew rewrite",
            )
            _check(
                resolve_solet_bin(
                    str(nested_release_foreign_cli),
                    python_executable=str(active_python),
                ) == "",
                "a nested venv beneath rel-* is rejected as a foreign environment",
            )


def test_adapter_reresolves_after_a_flip_that_followed_construction() -> None:
    """R11 (2026-08-17): the leg the pre-R11 suite could not make.

    Every leg above calls ``stable_release_path``/``resolve_solet_bin`` directly
    at assert time, so all of them prove the FUNCTION is correct — and all of
    them stayed GREEN while the defect was live in production, because none of
    them could see a CALLER that asks once in ``__init__`` and remembers the
    answer forever. An instrument blind to the question confirms nothing.

    The sequence below is the measured production one: the platform process
    started at 13:38:51Z, after the new release was materialized (13:38Z) and
    BEFORE cutover flipped ``current`` onto it (13:43Z). Constructing inside that
    skew window makes :func:`stable_release_path` correctly refuse to rewrite;
    the bug was caching the refusal, so every spawn for the next 2h47m — verified
    on a real spawn at 16:30Z — carried the versioned path.
    """
    print("\nR11: an adapter built during cutover skew re-resolves after the flip")
    for name, factory in _adapter_solet_bin_readers():
        with tempfile.TemporaryDirectory() as tmp:
            releases = Path(tmp) / "releases" / "testsolet"
            releases.mkdir(parents=True)
            incoming = _make_release(releases, "rel-20260817T133803Z-606323923")
            incoming_python = _make_executable(incoming.parent, "python3")
            _make_release(releases, "rel-20260817T005356Z-b518d3138")
            # SKEW: the new release is on disk, `current` still names the old one.
            _point(releases, "rel-20260817T005356Z-b518d3138")
            _check(
                stable_release_path(str(incoming)) == str(incoming),
                f"{name}: negative control — the rewrite really does refuse "
                f"during skew, so construction cannot have stabilized early",
            )

            with _managed_release_root(releases):
                read_spawn_surface = factory(  # type: ignore[operator]
                    str(incoming), Path(tmp), str(incoming_python),
                )

                # CUTOVER COMPLETES, ~4 minutes after construction.
                _point(releases, "rel-20260817T133803Z-606323923")
                after_flip = read_spawn_surface()
                _check(
                    "rel-20260817T133803Z-606323923" not in after_flip,
                    f"{name}: no release id survives in the spawn surface after the "
                    f"flip (got {after_flip})",
                )
                _check(
                    CURRENT_LINK_NAME in Path(after_flip).parts,
                    f"{name}: the spawn surface goes through `current`, so it was "
                    f"re-resolved and not cached at construction",
                )


def test_adapter_survives_the_next_cutover_that_reaps_its_own_release() -> None:
    """The consequence leg: what the cached value actually costs.

    A cached versioned path is not merely inelegant — the next deploy REAPS the
    release it names (``release_manager`` keeps the last K), and every consumer
    downstream is deliberately non-fatal, so the whole fleet goes dark with exit 0
    everywhere. This asserts the property that matters at the adapter seam:
    after a cutover that removes the very release the adapter was constructed
    with, the wake CLI it hands a worker still EXECUTES.
    """
    print("\nR11: an adapter outlives the deploy that reaps its own release")
    for name, factory in _adapter_solet_bin_readers():
        with tempfile.TemporaryDirectory() as tmp:
            releases = Path(tmp) / "releases" / "testsolet"
            releases.mkdir(parents=True)
            built_with = _make_release(releases, "rel-20260816T152738Z-dd0ebb517")
            built_with_python = _make_executable(built_with.parent, "python3")
            _point(releases, "rel-20260816T152738Z-dd0ebb517")
            with _managed_release_root(releases):
                read_spawn_surface = factory(  # type: ignore[operator]
                    str(built_with), Path(tmp), str(built_with_python),
                )

                # A later deploy: new release materialized, `current` swaps onto it,
                # the release this adapter was constructed with is REAPED.
                _make_release(releases, "rel-20260817T133803Z-606323923")
                _point(releases, "rel-20260817T133803Z-606323923")
                shutil.rmtree(releases / "rel-20260816T152738Z-dd0ebb517")
                _check(
                    not Path(built_with).exists(),
                    f"{name}: negative control — the construction-time release is "
                    f"genuinely gone",
                )

                surface = read_spawn_surface()
                env: dict[str, str] = {"PATH": "/usr/bin:/bin"}
                expose_worker_cli(env, surface)
                sidecar = watch_sidecar_argv(
                    surface, agent_id="claude_code", spool=True,
                )
                _check(
                    Path(env["AGENT_WAKE_CLI"]).is_file(),
                    f"{name}: AGENT_WAKE_CLI still names a file after the reap",
                )
                ran = subprocess.run(
                    [env["AGENT_WAKE_CLI"]], capture_output=True, text=True, check=False,
                )
                _check(
                    ran.returncode == 0
                    and ran.stdout.strip() == "rel-20260817T133803Z-606323923",
                    f"{name}: and it EXECUTES, serving the new release "
                    f"(rc={ran.returncode}, out={ran.stdout.strip()!r})",
                )
                _check(
                    shutil.which("solet-bridge", path=env["PATH"]) is not None,
                    f"{name}: the worker's prepended PATH still resolves bare `solet-bridge`",
                )
                _check(
                    Path(sidecar[0]).is_file(),
                    f"{name}: the registration sidecar's argv[0] survives too",
                )


def main() -> int:
    tests = [
        test_versioned_path_is_rewritten_onto_the_stable_pointer,
        test_survives_a_deploy_swap_that_reaps_the_old_release,
        test_refuses_to_rewrite_across_a_version_skew,
        test_refuses_a_pointer_whose_target_was_reaped,
        test_absolute_link_text_is_honored,
        test_non_release_paths_and_degraded_inputs_are_untouched,
        test_resolution_rungs_all_stabilize,
        test_foreign_path_cannot_supply_an_in_instance_cli,
        test_active_environment_and_release_family_prove_ownership,
        test_adapter_reresolves_after_a_flip_that_followed_construction,
        test_adapter_survives_the_next_cutover_that_reaps_its_own_release,
    ]
    for test in tests:
        test()
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
