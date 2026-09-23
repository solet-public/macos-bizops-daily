"""Verb-mode venv/seed install + newborn self-seed smoke (per-solet role
isolation, operator override 2026-07-12; acquisition mode retired 2026-07-18).

Pins the surviving properties explicitly: `birth_solet` requires an
EXISTING clone (an absent/empty target is refused -- acquisition-mode
clone-of-pinned-upstream was RETIRED, the Seed Factory replaces it), the §7
`provision_venv` birth variant builds the source-only seed folder's venv
EXPLICITLY + UNCONDITIONALLY while standard mode leaves it untouched, and --
under per-solet isolation -- the newborn self-seeds its OWN role in its
OWN venv subprocess (no parent-provisions-child credential copy; no credential
crosses a namespace).

Run directly: ``SOLET_NAME=<name> .venv/bin/python3
plugins/github_midwife_plugin/tests/venv_provision_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin import venv_provision  # noqa: E402
from github_midwife_plugin.plugin import GithubMidwifePlugin  # noqa: E402

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _fake_completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _is_build_backend_prep_call(call: list[str]) -> bool:
    return all(
        (
            all(package in call for package in ("pip", "setuptools", "wheel")),
            "install" in call,
            "--upgrade" in call,
        )
    )


def _has_expected_editable_install_order(calls: list[list[str]], target: Path) -> bool:
    package_dirs = (
        target / "solet_setup_contracts",
        target / "ananta",
        target / "plugins" / "macos_vault_plugin",
        target / "plugins" / "github_midwife_plugin",
    )
    return all(
        any(str(package_dir) in argument for argument in call)
        for call, package_dir in zip(calls[2:], package_dirs, strict=True)
    )


# ── probe_target_absent_or_empty ────────────────────────────────────


def _check_probe_target_absent_or_empty(root: Path) -> None:
    # The probe now gates birthability: absent/empty -> True (birth refuses it,
    # acquisition retired); an occupied clone dir -> False (birthable).
    absent = root / "does_not_exist"
    _check("absent target probes True (not a birthable clone)", venv_provision.probe_target_absent_or_empty(absent) is True)

    empty = root / "empty_dir"
    empty.mkdir()
    _check("empty directory probes True (not a birthable clone)", venv_provision.probe_target_absent_or_empty(empty) is True)

    occupied = root / "occupied_dir"
    occupied.mkdir()
    (occupied / "some_file").write_text("content")
    _check("non-empty directory probes False", venv_provision.probe_target_absent_or_empty(occupied) is False)


# ── create_venv_and_install_seed ─────────────────────────────────────


def _check_create_venv_and_install_seed(root: Path) -> None:
    target = root / "clone"
    target.mkdir()
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _fake_completed(0)

    venv_dir = venv_provision.create_venv_and_install_seed(target, run=_fake_run)
    _check("create_venv_and_install_seed returns <target>/.venv", venv_dir == target / ".venv", str(venv_dir))
    # c60455872 deliberately adds the solet_setup_contracts editable install.
    # RED MUTATION: revert this expectation to 5 while that sixth install exists.
    _check(
        "exactly 6 subprocess calls: venv create + build-backend prep + 4 editable installs (setup contracts, ananta, vault, seed plugin)",
        len(calls) == 6,
        str(calls),
    )
    _check("the first call creates the venv", "venv" in calls[0], str(calls[0]))
    # RED-FIRST (finding F8, 2026-07-11): a stock py3.13 venv ships pip but NOT
    # setuptools, so the `pip install --no-build-isolation -e` calls below fail
    # with BackendUnavailable until the build backend is pre-installed. This
    # asserts the backend is upgraded BEFORE any editable install runs — the
    # pre-fix body went venv -> ananta-install directly, so calls[1] was the
    # ananta editable install (no setuptools) and this check fails RED.
    _check(
        "the build backend (pip+setuptools+wheel) is upgraded before any editable install",
        _is_build_backend_prep_call(calls[1]),
        str(calls[1]),
    )
    # RED-FIRST (finding F-5, 2026-07-12 cold run): the seed plugin's
    # pyproject pins macos_vault_plugin (genesis imports its keychain at module
    # level), so the local editable install of the vault plugin MUST land
    # between ananta and the seed plugin — the pre-fix body installed only
    # ananta + seed plugin, and pip failed resolving the pin from PyPI.
    _check(
        "the seed installs target solet_setup_contracts/, ananta/, plugins/macos_vault_plugin/, plugins/github_midwife_plugin/ in that order",
        _has_expected_editable_install_order(calls, target),
        str(calls),
    )


def _check_create_venv_and_install_seed_fails_loud(root: Path) -> None:
    target = root / "clone2"
    target.mkdir()
    with patch("subprocess.run", return_value=_fake_completed(1, stderr="simulated venv failure")):
        try:
            venv_provision.create_venv_and_install_seed(target, run=subprocess.run)
        except venv_provision.VerbModeProvisionError:
            _check("a venv-creation failure raises VerbModeProvisionError", True)
        else:
            raise SmokeFailureError("create-venv-fails-loud: did not raise")


# ── seed_newborn_credential (verb-mode newborn self-seed) ──────────────


def _check_seed_newborn_credential_subprocess_shape() -> None:
    """Per-solet isolation: the newborn self-seeds its OWN role via a
    subprocess in its OWN venv (SOLET_NAME=<newborn>), running
    credential_seed's --seed CLI with the isolation-sibling-db flag set to the
    parent's db. No password ever crosses the boundary -- nothing is piped in.
    """
    with patch("subprocess.run", return_value=_fake_completed(0)) as mock_run:
        venv_provision.seed_newborn_credential("newbornhum", Path("/fake/venv"), "parentdb", run=mock_run)
    cmd = mock_run.call_args.args[0]
    _, kwargs = mock_run.call_args
    _check(
        "the seed subprocess runs the NEWBORN venv's python",
        str(Path("/fake/venv") / "bin" / "python3") in cmd,
        str(cmd),
    )
    _check(
        "the seed subprocess invokes credential_seed's --seed CLI with --isolation-sibling-db <parentdb>",
        "github_midwife_plugin.credential_seed" in cmd and "--seed" in cmd
        and "--isolation-sibling-db" in cmd and "parentdb" in cmd,
        str(cmd),
    )
    _check(
        "the seed subprocess env sets SOLET_NAME to the NEWBORN's name",
        kwargs.get("env", {}).get("SOLET_NAME") == "newbornhum",
        str(kwargs.get("env", {}).get("SOLET_NAME")),
    )
    _check(
        "no credential crosses the boundary: nothing is piped on the subprocess stdin",
        kwargs.get("input") is None,
        f"unexpected stdin input={kwargs.get('input')!r}",
    )


def _check_seed_newborn_credential_subprocess_failure_surfaces_diagnostic() -> None:
    """The self-seed subprocess is secret-free by construction (credential_seed's
    --seed never prints a password), so its FATAL diagnostic — role-absent naming
    wizard step 1, or an isolation breach — IS surfaced on failure. That is the
    actionable error; unlike the retired parent-provisions-child transit, no
    secret can ride the subprocess's stderr, so echoing its tail is safe."""
    with patch(
        "subprocess.run",
        return_value=_fake_completed(1, stderr="FATAL: Postgres role 'newbornhum' does not exist -- wizard step 1 was not performed"),
    ):
        try:
            venv_provision.seed_newborn_credential("newbornhum", Path("/fake/venv"), "parentdb", run=subprocess.run)
        except venv_provision.VerbModeProvisionError as exc:
            _check(
                "a self-seed subprocess failure surfaces the FATAL diagnostic (actionable)",
                "wizard step 1" in str(exc) and "exited 1" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("seed-newborn-subprocess-failure: did not raise")


# ── Plugin-level: existing-clone requirement + the provision_venv variant ──


def _check_require_existing_clone(root: Path) -> None:
    """Acquisition retired 2026-07-18: an absent/empty target is NOT birthable
    (refused -- assemble a seed first); only a fully-formed clone passes; a
    non-empty non-clone dir is refused (never guess or clobber).
    """
    plugin = GithubMidwifePlugin()

    absent = root / "absent"
    try:
        plugin._require_existing_clone(absent)  # noqa: SLF001
    except ValueError as exc:
        _check("an absent target is refused (acquisition retired)", "absent or empty" in str(exc), str(exc))
    else:
        raise SmokeFailureError("require-existing-clone-absent: did not raise")

    empty = root / "empty"
    empty.mkdir()
    try:
        plugin._require_existing_clone(empty)  # noqa: SLF001
    except ValueError as exc:
        _check("an empty target is refused (acquisition retired)", "absent or empty" in str(exc), str(exc))
    else:
        raise SmokeFailureError("require-existing-clone-empty: did not raise")

    existing = root / "existing_clone"
    for marker in ("ananta", "plugins"):
        (existing / marker).mkdir(parents=True)
    _check(
        "a fully-formed clone passes (returns None, no raise)",
        plugin._require_existing_clone(existing) is None,  # noqa: SLF001
    )

    invalid = root / "invalid_occupied"
    invalid.mkdir()
    (invalid / "random_file").write_text("junk")
    try:
        plugin._require_existing_clone(invalid)  # noqa: SLF001
    except ValueError as exc:
        _check("a non-empty non-clone target raises ValueError (refuses to guess)", "not a valid platform clone" in str(exc), str(exc))
    else:
        raise SmokeFailureError("require-existing-clone-invalid: did not raise")


def _check_birth_refuses_absent_target(root: Path) -> None:
    """Verb-level retirement pin: birth_solet against an absent target no
    longer clones a pinned upstream -- it fails loud (ValueError) at the door.
    """
    plugin = GithubMidwifePlugin()
    absent = root / "absent_birth_target"
    try:
        plugin.birth_solet(
            name="testhum",
            profile_template="macos-free-solet",
            environment_config={"target": str(absent)},
        )
    except ValueError as exc:
        _check(
            "birth against an absent target fails loud (acquisition retired -- assemble a seed first)",
            "absent or empty" in str(exc) and "acquisition mode retired" in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("birth-refuses-absent-target: did not raise")


def _check_existing_clone_seeds_own_credential(root: Path) -> None:
    """Standard existing-clone birth builds the per-solet self-seed closure
    over the pre-existing <target>/.venv: a pre-seed scram VERIFY, then the
    newborn's OWN self-seed with the isolation self-proof against the parent's
    db. The CLI path (genesis.main) stays on the in-process seed_db_password --
    unchanged, not tested here.
    """
    plugin = GithubMidwifePlugin()

    target = root / "existing_clone"
    for marker in ("ananta", "plugins"):
        (target / marker).mkdir(parents=True)

    captured: dict[str, object] = {}

    def _fake_run_genesis(
        *, name: str, clone_root: Path, profile_name: str,
        credential_provisioner: object = None, **_kw: object,
    ) -> dict[str, object]:
        del name, clone_root, profile_name
        captured["credential_provisioner"] = credential_provisioner
        return {"steps": []}

    # The pre-seed scram VERIFY must run BEFORE the self-seed (Architect C3
    # ordering). BOTH are patched so the closure NEVER touches real Postgres.
    # The self-seed's sibling_db must be the parent's db (== the parent
    # process's SOLET_NAME). Derive it from the environment rather than
    # hardcoding a literal name: the seed factory births solets under many
    # different names that run these gates, so a hardcode would false-RED
    # there (Reviewer-C, SF-D).
    expected_sibling = os.environ["SOLET_NAME"]
    call_order: list[str] = []

    def _fake_seed(newborn_name: str, newborn_venv: Path, sibling_db: str, *, run: object) -> None:
        del newborn_venv, run
        call_order.append(f"seed:{newborn_name}:sibling={sibling_db}")

    def _fake_verify_db(newborn_name: str, *, run: object) -> None:
        del run
        call_order.append(f"verify_db:{newborn_name}")

    with patch("github_midwife_plugin.plugin.run_genesis", _fake_run_genesis), \
         patch.object(venv_provision, "seed_newborn_credential", _fake_seed), \
         patch.object(venv_provision, "verify_newborn_db_scram_gated", _fake_verify_db):
        plugin._run_genesis_against_clone("newbornhum", target, "macos-free-solet", provision_venv=False)  # noqa: SLF001
        provisioner = captured.get("credential_provisioner")
        _check(
            "existing-clone birth passes a credential_provisioner to run_genesis (not in-process seed_db_password)",
            provisioner is not None and callable(provisioner),
            f"got {provisioner!r}",
        )
        provisioner()  # type: ignore[misc]  # invoke the closure while the patches are active

    _check(
        "the provisioner VERIFIES the db scram-gated, then the newborn self-seeds against the parent sibling db (ordered)",
        call_order == ["verify_db:newbornhum", f"seed:newbornhum:sibling={expected_sibling}"],
        f"got {call_order!r} (expected sibling={expected_sibling!r})",
    )


def _check_provision_venv_variant(root: Path) -> None:
    """The §7 birth VARIANT: provision_venv=True runs create_venv_and_install_seed
    EXPLICITLY + UNCONDITIONALLY before genesis (a source-only seed folder has no
    .venv); provision_venv=False (standard mode, UNCHANGED) NEVER calls it (the
    venv must pre-exist). Purely the flag decides -- no lazy create-if-absent.
    """
    plugin = GithubMidwifePlugin()
    target = root / "seed_clone"
    for marker in ("ananta", "plugins"):
        (target / marker).mkdir(parents=True)

    venv_calls: list[Path] = []

    def _fake_create_venv(t: Path, *, run: object) -> Path:
        del run
        venv_calls.append(t)
        return t / ".venv"

    def _fake_run_genesis(*, name: str, clone_root: Path, profile_name: str, **_kw: object) -> dict[str, object]:
        del name, clone_root, profile_name
        return {"steps": []}

    def _run(provision: bool) -> int:
        venv_calls.clear()
        with patch.object(venv_provision, "create_venv_and_install_seed", _fake_create_venv), \
             patch("github_midwife_plugin.plugin.run_genesis", _fake_run_genesis), \
             patch.object(venv_provision, "seed_newborn_credential", lambda *_a, **_k: None), \
             patch.object(venv_provision, "verify_newborn_db_scram_gated", lambda *_a, **_k: None):
            plugin._run_genesis_against_clone("hum", target, "macos-free-solet", provision_venv=provision)  # noqa: SLF001
        return len(venv_calls)

    _check(
        "provision_venv=True -> create_venv_and_install_seed called once (explicit, unconditional)",
        _run(provision=True) == 1,
        f"venv_calls={venv_calls!r}",
    )
    _check(
        "provision_venv=False (standard mode) -> create_venv_and_install_seed NEVER called (venv must pre-exist)",
        _run(provision=False) == 0,
        f"venv_calls={venv_calls!r}",
    )


def _check_birth_profile_mismatch_precedes_venv(root: Path) -> None:
    plugin = GithubMidwifePlugin()
    target = root / "canonical_seed"
    for marker in ("ananta", "plugins"):
        (target / marker).mkdir(parents=True)
    (target / "PROVENANCE.json").write_text(
        json.dumps({"bundle": {"name": "macos-bizops", "platform": "local"}}),
        encoding="utf-8",
    )
    with patch.object(venv_provision, "create_venv_and_install_seed") as create_venv, \
         patch("github_midwife_plugin.plugin.run_genesis") as run_genesis:
        result = plugin.birth_solet(
            name="bizops",
            profile_template="macos_bizops",
            environment_config={"target": str(target)},
            provision_venv=True,
        )
    _check(
        "birth_solet reports a declared provenance/profile mismatch as failed",
        result.status.value == "failed" and "profile identity mismatch" in result.message,
        result.message,
    )
    _check(
        "birth_solet mismatch makes zero venv/package/genesis calls",
        create_venv.call_count == 0 and run_genesis.call_count == 0,
        f"venv={create_venv.call_count}, genesis={run_genesis.call_count}",
    )
    _check(
        "birth_solet mismatch writes no venv or genesis marker",
        not (target / ".venv").exists() and not (target / ".solet" / "genesis.json").exists(),
        str(target),
    )


# ── verify_newborn_db_scram_gated (assumes-and-verifies, 2026-07-11) ──


def _check_verify_db_scram_gated_passes_when_wrong_pw_rejected() -> None:
    """Scram IS gating: a WRONG password is REJECTED (psql exit != 0) -> verify
    returns without raising. Per-role isolation: the negative probe connects as
    the newborn's OWN role (== the passed name) to the newborn db, password via
    PGPASSWORD, never argv."""
    keg_only_psql = "/opt/homebrew/opt/postgresql@17/bin/psql"
    with patch("subprocess.run", return_value=_fake_completed(1)) as mock_run, \
         patch.object(venv_provision, "_resolve_psql_binary", return_value=keg_only_psql):
        venv_provision.verify_newborn_db_scram_gated("fern-fresh-forge", run=subprocess.run)
    cmd = mock_run.call_args.args[0]
    _, kwargs = mock_run.call_args
    _check(
        "the verify probe targets the newborn db AND role via DISCRETE argv (-d/-U), NOT a libpq conninfo string (F3)",
        cmd[cmd.index("-d") + 1] == "fern-fresh-forge"
        and cmd[cmd.index("-U") + 1] == "fern-fresh-forge"
        and "-h" in cmd
        # RED-FIRST: the pre-fix single conninfo token (`host=... dbname=... user=...`)
        # is exactly the space-keyword-injection sink F3 flagged; assert it is gone.
        and not any("dbname=" in str(a) or "host=" in str(a) for a in cmd),
        f"cmd={cmd!r}",
    )
    _check(
        "the verify probe uses the resolver's keg-only PostgreSQL 17 psql path, not bare PATH argv0",
        cmd[0] == keg_only_psql,
        f"cmd={cmd!r}",
    )
    _check(
        "the verify probe sends its (wrong) password via PGPASSWORD, never argv",
        bool(kwargs.get("env", {}).get("PGPASSWORD"))
        and all("PGPASSWORD" not in str(a) for a in cmd),
        f"cmd={cmd!r} env_has_pgpassword={bool(kwargs.get('env', {}).get('PGPASSWORD'))}",
    )


def _check_verify_db_scram_gated_refuses_when_wrong_pw_accepted() -> None:
    """RED-FIRST: a WRONG password ACCEPTED (psql exit 0 = trust fallthrough, db
    passwordless-accessible) -> refuse LOUD, naming the R3 default-scram lines
    wizard step 1 must add. This is the ONE invisible failure the verb keeps
    checking."""
    with patch("subprocess.run", return_value=_fake_completed(0)), \
         patch.object(
             venv_provision,
             "_resolve_psql_binary",
             return_value="/opt/homebrew/opt/postgresql@17/bin/psql",
         ):
        try:
            venv_provision.verify_newborn_db_scram_gated("fern-fresh-forge", run=subprocess.run)
        except venv_provision.VerbModeProvisionError as exc:
            _check(
                "an ungated (passwordless-accessible) newborn db is refused, naming the R3 default-scram fix",
                "passwordless-accessible" in str(exc)
                and "scram-sha-256" in str(exc)
                and "fern-fresh-forge" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("verify-db-scram-gated: did not raise on an ungated db")


def _check_verify_db_rejects_invalid_name_before_any_probe() -> None:
    """F3 regression (2026-07-19): a name carrying a space -- a libpq conninfo
    keyword-injection vector -- must fail closed at the verb entrypoint BEFORE any
    psql subprocess runs. `run` is a recording fake that must never be called."""
    calls: list[object] = []

    def _recording_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _fake_completed(1)

    try:
        venv_provision.verify_newborn_db_scram_gated("x host=evil password=p", run=_recording_run)
    except venv_provision.VerbModeProvisionError as exc:
        _check(
            "a space-bearing (conninfo-injecting) newborn name is refused before any psql call",
            "not a valid solet name" in str(exc) and not calls,
            f"exc={exc!r} calls={calls!r}",
        )
    else:
        raise SmokeFailureError("verify-db-rejects-invalid-name: did not raise")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _check_probe_target_absent_or_empty(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_create_venv_and_install_seed(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_create_venv_and_install_seed_fails_loud(Path(tmp))
        _check_seed_newborn_credential_subprocess_shape()
        _check_seed_newborn_credential_subprocess_failure_surfaces_diagnostic()
        with tempfile.TemporaryDirectory() as tmp:
            _check_require_existing_clone(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_birth_refuses_absent_target(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_existing_clone_seeds_own_credential(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_provision_venv_variant(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_birth_profile_mismatch_precedes_venv(Path(tmp))
        _check_verify_db_scram_gated_passes_when_wrong_pw_rejected()
        _check_verify_db_scram_gated_refuses_when_wrong_pw_accepted()
        _check_verify_db_rejects_invalid_name_before_any_probe()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"venv_provision_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
