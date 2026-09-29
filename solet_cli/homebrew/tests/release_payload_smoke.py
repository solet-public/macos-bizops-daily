"""Focused no-network smoke for release rendering and Formula package closure.

The installability checks are host-dependent by design (see the README):
they require `brew` and Homebrew's `python@3.13` (the Formula's first
interpreter candidate), but touch no
network — no PyPI index, no live fetch of the Formula's own pinned
`setuptools`/`wheel`/`packaging` resources (those are vendored locally
under `fixtures/pinned_wheels/`, hash-verified against the Formula's own
declared sha256 at run time — see `_install_pinned_build_resources`). This
file proves the Formula's *text* (pinned resources declared with sha256,
installed before the build_isolation:false manager step), a local RED
control (setuptools is absent from a clean venv unless something
provisions it), and a real, fully-offline GREEN install using those
vendored resources — mirroring the Formula's own `venv.pip_install
resources` step without depending on the host's ambient site-packages.
Whether the Formula's declared URLs/checksums are themselves still correct
against the real PyPI files (i.e. that the vendored copies are not stale)
is proven separately, over the network, by
`ci/resource_provisioning_acceptance.py`, gated like
`ci/lifecycle_acceptance.py`, and never registered as a gate smoke.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY = _ROOT.parents[1]
_RENDERER = _ROOT / "scripts" / "render_release_payload.py"
_EXAMPLE = _ROOT / "release_metadata.example.json"
_EXAMPLE_MANIFEST = _ROOT / "release_manifest.example.json"
_checks = 0

sys.path.insert(0, str(_REPOSITORY / "solet_cli" / "src"))

from solet_manager.cli import run  # noqa: E402


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _run_renderer(
    metadata_path: Path,
    output: Path,
    *,
    manifest_path: Path = _EXAMPLE_MANIFEST,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_RENDERER),
            "--metadata",
            str(metadata_path),
            "--manifest",
            str(manifest_path),
            "--output-root",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_metadata(path: Path, metadata: dict[str, object]) -> None:
    path.write_text(json.dumps(metadata), encoding="utf-8")


def _render_valid_payload(
    root: Path,
    metadata: dict[str, object],
) -> tuple[str, dict[str, object], Path]:
    metadata_path = root / "release.json"
    output = root / "output"
    _write_metadata(metadata_path, metadata)
    result = _run_renderer(metadata_path, output)
    _check(result.returncode == 0, f"release payload render failed: {result.stderr}")
    formula = (output / "Formula" / "solet.rb").read_text(encoding="utf-8")
    lock_value: object = json.loads(
        (output / "solet_cli" / "homebrew" / "seed.lock.json").read_text(
            encoding="utf-8"
        )
    )
    _check(isinstance(lock_value, dict), "rendered seed lock is an object")
    return formula, cast(dict[str, object], lock_value), output


def _check_named_seed_catalog(
    output: Path,
    metadata: dict[str, object],
    lock: dict[str, object],
) -> None:
    """The tap symlink exposes this exact path to ``_resolve_named_seed_lock``."""
    profile = str(metadata["seed_profile"])
    catalog_lock = output / "solet_cli" / "homebrew" / "seeds" / profile / "seed.lock.json"
    _check(
        catalog_lock.is_file(),
        "renderer emits the named-seed catalog lock at the resolver's symlink target",
    )
    catalog_value: object = json.loads(catalog_lock.read_text(encoding="utf-8"))
    _check(
        catalog_value == lock,
        "named-seed catalog lock is byte-equivalent JSON to the bundled default lock",
    )


def _check_formula_boundary(
    formula: str,
    lock: dict[str, object],
    metadata: dict[str, object],
) -> None:
    _check_formula_install_shape(formula, lock)
    _check_rendered_identity(formula, lock, metadata)
    _check_install_source_receipt(formula, metadata)
    _check_release_manifest_receipt(formula)
    brewfile = (_ROOT / "ci" / "Brewfile.enterprise.example").read_text(encoding="utf-8")
    _check(
        'trusted: { formula: "solet" }' in brewfile and "trusted: true" not in brewfile,
        "Brewfile trusts only the formula",
    )


def _check_formula_caveats(formula: str) -> None:
    caveats = formula.split("def caveats", 1)[1].split("\n  end", 1)[0]
    _check(
        all(
            token in caveats
            for token in (
                "never installs or upgrades it",
                "brew tab --installed-on-request python@3.13 && brew upgrade solet",
                "brew pin python@3.13",
                "-25293",
                "Always Allow",
            )
        )
        and "can upgrade the shared python@3.13" not in caveats,
        "formula caveats say it never moves python@3.13 and name the guarded upgrade, the optional pin, and the recovery",
    )


def _check_formula_install_shape(formula: str, lock: dict[str, object]) -> None:
    _check(
        "{{" not in formula and "post_install" not in formula,
        "formula has no markers or post-install mutation",
    )
    _check(
        "def caveats" in formula and "Next: run solet create" in formula,
        "formula prints the standard separate setup instruction",
    )
    _check_formula_caveats(formula)
    _check(
        "include Language::Python::Virtualenv" in formula,
        "formula imports Homebrew's Python virtualenv helpers",
    )
    _check(
        'depends_on "git"' in formula and re.search(r'^\s*depends_on\s+"python', formula, re.MULTILINE) is None,
        "formula declares git and no Python dependency (a source formula's python dependency forces its upgrade)",
    )
    _check_python_candidate_selection(formula)
    _check(
        "virtualenv_create(libexec" in formula
        and "build_isolation: false" in formula
        and "bin.install_symlink" in formula,
        "formula owns a closed virtualenv and public manager wrapper",
    )
    _check(
        "assert_match" in formula and "list --json" in formula and "create brew-test" in formula,
        "formula test is behavioral",
    )
    _check(
        "brew install" not in _without_odie_message(formula) and "services" not in formula,
        "formula does not create machine or instance state",
    )
    _check_seeds_symlink_shape(formula, lock)


def _check_install_source_receipt(formula: str, metadata: dict[str, object]) -> None:
    marker = '(libexec/"share"/"solet"/"install-source.json").write <<~\'JSON\'\n'
    start = formula.find(marker)
    end = formula.find("    JSON\n", start + len(marker))
    _check(start != -1 and end != -1, "formula writes an installed source receipt")
    if start == -1 or end == -1:
        return
    receipt: object = json.loads(formula[start + len(marker) : end])
    _check(
        receipt
        == {
            "schema_version": 1,
            "mode": "release",
            "source_commit": metadata["manager_source_commit"],
        },
        "published Formula receipt records release mode and its immutable source commit",
    )


def _check_release_manifest_receipt(formula: str) -> None:
    """iss_18c47206: the Formula must install the exact same stage-5 draft
    manifest into the keg (`share/solet/release_manifest.json`) as was passed
    to the renderer -- that installed copy is what the consumption-side
    pairing gate (`solet_manager.release_identity_gate`) reads."""
    marker = '(libexec/"share"/"solet"/"release_manifest.json").write <<~\'JSON\'\n'
    start = formula.find(marker)
    end = formula.find("    JSON\n", start + len(marker))
    _check(start != -1 and end != -1, "formula writes a release manifest into the keg")
    if start == -1 or end == -1:
        return
    embedded: object = json.loads(formula[start + len(marker) : end])
    expected = json.loads(_EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    _check(embedded == expected, "formula's embedded release manifest matches the staged draft byte-for-byte")


def _check_seeds_symlink_shape(formula: str, lock: dict[str, object]) -> None:
    lock_marker = '(libexec/"share"/"solet"/"seed.lock.json").write <<~\'JSON\'\n'
    lock_at = formula.find(lock_marker)
    seeds_at = formula.find(
        'install_symlink Pathname(__dir__).parent/"solet_cli"/"homebrew"/"seeds" => "seeds"'
    )
    _check(lock_at != -1, "formula writes the reviewed default lock inside the sandbox")
    lock_end = formula.find("    JSON\n", lock_at + len(lock_marker))
    embedded: object | None = None
    if lock_at != -1 and lock_end != -1:
        embedded = json.loads(formula[lock_at + len(lock_marker) : lock_end])
    _check(embedded == lock, "formula's embedded default lock matches the rendered lock")
    _check(
        '.install Pathname(__dir__).parent/"solet_cli"/"homebrew"/"seed.lock.json"'
        not in formula,
        "formula does not read the tap checkout inside Homebrew's build sandbox",
    )
    _check(seeds_at != -1, "formula symlinks the discoverable seed set from the tap directory")
    _check(
        lock_at != -1 and seeds_at != -1 and lock_at < seeds_at,
        "seeds symlink is declared after the bundled-default lock install",
    )


def _check_rendered_identity(
    formula: str,
    lock: dict[str, object],
    metadata: dict[str, object],
) -> None:
    _check(
        lock["commit"] == metadata["seed_commit"]
        and lock["repository"] == metadata["seed_repository"]
        and lock["profile"] == metadata["seed_profile"] == "macos-bizops",
        "seed lock carries reviewed identity",
    )
    _check(
        metadata["release_archive_sha256"] == lock["archive_sha256"]
        and str(metadata["release_archive_sha256"]) in formula,
        "one sealed archive checksum drives formula and seed lock",
    )
    _check(
        str(metadata["manager_url"]) in formula,
        "formula carries the independently validated manager release URL",
    )
    _check(
        "solet-public/homebrew-tap" in str(metadata["manager_url"])
        and "https://github.com/solet-public/macos-bizops.git"
        == metadata["seed_repository"],
        "public manager host and seed repository are independent identities",
    )


def _expect_refused(
    root: Path,
    metadata: dict[str, object],
    label: str,
    expected_error: str,
) -> None:
    metadata_path = root / f"{label}.json"
    output = root / f"output-{label}"
    _write_metadata(metadata_path, metadata)
    result = _run_renderer(metadata_path, output)
    _check(
        result.returncode != 0 and expected_error in result.stderr,
        f"{label} is refused: {result.stderr}",
    )
    _check(
        not (output / "Formula" / "solet.rb").exists()
        and not (output / "solet_cli" / "homebrew" / "seed.lock.json").exists(),
        f"{label} writes no renderer outputs",
    )


def _check_identity_refusals(root: Path, metadata: dict[str, object]) -> None:
    dual_checksum = dict(metadata)
    dual_checksum["manager_sha256"] = "d" * 64
    _expect_refused(root, dual_checksum, "dual-checksum", "must contain exactly")

    cases: tuple[tuple[str, str, str, str], ...] = (
        (
            "latest-route",
            "manager_url",
            "https://github.com/solet-public/macos-bizops/releases/latest/download/solet-0.1.0.tar.gz",
            "uploaded release asset",
        ),
        ("moving-tag", "seed_release_tag", "LaTeSt", "invalid immutable identity"),
        ("mixed-case-main", "seed_release_tag", "MaIn", "invalid immutable identity"),
        (
            "moving-manager-tag",
            "manager_url",
            "https://github.com/solet-public/homebrew-tap/releases/download/main/solet-0.1.0.tar.gz",
            "invalid immutable identity",
        ),
        (
            "query-ambiguity",
            "manager_url",
            str(metadata["manager_url"]) + "?download=1",
            "unambiguous",
        ),
        (
            "fragment-ambiguity",
            "manager_url",
            str(metadata["manager_url"]) + "#asset",
            "unambiguous",
        ),
        (
            "unversioned-asset",
            "manager_url",
            "https://github.com/solet-public/macos-bizops/releases/download/release-2026-08-21/solet.tar.gz",
            "versioned archive",
        ),
    )
    for label, key, value, expected_error in cases:
        changed = dict(metadata)
        changed[key] = value
        _expect_refused(root, changed, label, expected_error)

    # Manager distribution identity is independent of seed identity: the
    # public release asset can live in the tap while the macos-bizops seed
    # remains in its own repository with its immutable tag/commit/tree tuple.


def _check_independent_manager_identities(
    root: Path,
    metadata: dict[str, object],
) -> None:
    cases = (
        (
            "independent-manager-tag",
            "https://github.com/solet-public/macos-bizops/releases/download/"
            "release-OTHER/solet-0.1.0.tar.gz",
        ),
        (
            "independent-manager-repository",
            "https://github.com/solet-public/other/releases/download/"
            "release-2026-08-21/solet-0.1.0.tar.gz",
        ),
    )
    for label, manager_url in cases:
        case_root = root / label
        case_root.mkdir()
        changed = dict(metadata)
        changed["manager_url"] = manager_url
        formula, _, _ = _render_valid_payload(case_root, changed)
        _check(manager_url in formula, f"{label} is carried into the Formula")


_PYTHON_CANDIDATES = (
    "/opt/homebrew/opt/python@3.13/bin/python3.13",
    "/usr/local/bin/python3.13",
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13",
)
# One `odie` statement: its first line plus every line a trailing backslash continues.
_ODIE_MESSAGE = re.compile(r"odie (?:[^\n]*\\\n)*[^\n]*")


# review unt_f3bf05be F1: Homebrew's Virtualenv#pip_install runs `<base> -m pip --python=<venv>`
# with std_pip_args, whose --uploaded-prior-to pip < 26.1 rejects. The Formula bootstraps its
# pinned pip into the venv from pip's own wheel and installs everything with the venv's pip.
_PIP_BOOTSTRAP = 'system venv_python, wheel/"pip", "install", *std_pip_args(prefix: false), wheel'
_PIP_REBIND = "venv = virtualenv_create(libexec, venv_python)"


def _check_pip_bootstrap(formula: str, code: str) -> None:
    resources = _parse_pinned_resources(formula)
    pip_url = resources.get("pip", ("", ""))[0]
    pinned = re.search(r"/pip-(?P<major>\d+)\.(?P<minor>\d+)(?:\.\d+)?-py3-none-any\.whl$", pip_url)
    _check(
        pinned is not None and (int(pinned["major"]), int(pinned["minor"])) >= (26, 1),
        f"formula pins a pip wheel resource with sha256 at 26.1 or later (carries --uploaded-prior-to): {pip_url!r}",
    )
    create = code.find("virtualenv_create(libexec, python)")
    stage = code.find('resource("pip").stage do')
    bootstrap = code.find(_PIP_BOOTSTRAP)
    rebind = code.find(_PIP_REBIND)
    first_install = code.find(".pip_install")
    _check(
        -1 < create < stage < bootstrap < rebind < first_install
        and "venv_python = libexec/\"bin/python\"" in code
        and "= virtualenv_create(libexec, python)" not in code,
        "pip is bootstrapped into the venv by the venv's own interpreter before the installer is rebound and before any resource installs",
    )
    _check(
        'venv.pip_install resources.reject { |r| r.name == "pip" }' in code
        and code.count(".pip_install") == code.count("venv.pip_install")
        and '"-m", "pip"' not in code,
        "every install goes through the rebound venv (its own pip), none through the base interpreter's pip",
    )


def _without_odie_message(formula: str) -> str:
    return _ODIE_MESSAGE.sub("", formula)


def _check_python_candidate_selection(formula: str) -> None:
    listed = re.search(r"PYTHON_CANDIDATES = %w\[(?P<body>[^\]]*)\]\.freeze", formula)
    _check(
        listed is not None and tuple(listed.group("body").split()) == _PYTHON_CANDIDATES,
        "formula lists exactly the three Python 3.13 candidates, opt path first",
    )
    install = formula.split("  def install", 1)[1].split("\n  end\n", 1)[0]
    code = "\n".join(line for line in _without_odie_message(install).splitlines() if not line.lstrip().startswith("#"))
    probe = install.find('PYTHON_CANDIDATES.map { |candidate| Pathname(candidate) }.find do |candidate|')
    odie = install.find("odie ")
    venv = install.find("virtualenv_create(libexec, python)")
    _check(
        -1 < probe < odie < venv
        and 'quiet_system(candidate, "-c", "import sys; sys.exit(sys.version_info[:2] != (3, 13))")' in install
        and "popen_read" not in code
        and "unless python" in install[probe:odie],
        "install takes the first candidate reporting 3.13, odies when none does, then builds the venv on it",
    )
    odie_messages = _ODIE_MESSAGE.findall(install)
    _check(
        len(odie_messages) == 1
        and "No Python 3.13 found" in odie_messages[0]
        and "HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 #{full_name}" in odie_messages[0],
        "the odie names the exact guarded install command for this formula's own tap",
    )
    _check_install_shell_outs(code)
    _check_pip_bootstrap(formula, code)


def _check_install_shell_outs(code: str) -> None:
    without_bootstrap = code.replace(_PIP_BOOTSTRAP, "", 1)
    _check(
        re.search(r"\b(?:safe_system|system|exec)\b|%x|\bbrew\b", without_bootstrap) is None
        and code.count("quiet_system(") == 1
        and code.count(_PIP_BOOTSTRAP) == 1,
        "install never shells out to brew, so it cannot install or upgrade Python itself; its one system call is the pip bootstrap",
    )


def _homebrew_python() -> Path:
    brew = shutil.which("brew")
    if brew is None:
        # Exit 77 is the register's dedicated environment-skip code: a host
        # (or a constrained gate PATH) without Homebrew cannot exercise the
        # formula-install leg at all, and that is an environment fact about
        # the runner, not a defect in the payload under test.
        print(
            "SKIP  formula-install checks: no `brew` on PATH to provide "
            "python@3.13, the Formula's first interpreter candidate"
        )
        raise SystemExit(77)
    result = subprocess.run(
        [brew, "--prefix", "python@3.13"],
        check=False,
        capture_output=True,
        text=True,
    )
    _check(
        result.returncode == 0,
        "host prerequisite missing: install Homebrew's python@3.13, the "
        f"Formula's first interpreter candidate: {result.stderr.strip()}",
    )
    python = Path(result.stdout.strip()) / "bin" / "python3.13"
    _check(
        python.is_file(),
        "host prerequisite missing: the Homebrew python@3.13 "
        f"executable is absent: {python}",
    )
    return python


_RESOURCE_BLOCK = re.compile(
    r'resource\s+"(?P<name>[^"]+)"\s+do\s+'
    r'url\s+"(?P<url>[^"]+)"\s+'
    r'sha256\s+"(?P<sha256>[0-9a-f]{64})"\s+'
    r"end",
)


def _parse_pinned_resources(formula: str) -> dict[str, tuple[str, str]]:
    """Every ``resource "name" do url ... sha256 ... end`` block, by name."""
    return {
        match.group("name"): (match.group("url"), match.group("sha256"))
        for match in _RESOURCE_BLOCK.finditer(formula)
    }


def _assert_resources_declared_before_manager_install(formula: str) -> None:
    """Offline ordering check: ``pip_install resources`` must precede the
    ``build_isolation: false`` manager install, or the resources exist in
    the Formula without ever provisioning the venv before it needs them.
    """
    resources_at = formula.find("pip_install resources")
    contracts_at = formula.find('pip_install buildpath/"solet_setup_contracts"')
    manager_at = formula.find('pip_install buildpath/"solet_cli"')
    _check(resources_at != -1, "Formula installs the declared resources into the venv")
    _check(
        contracts_at != -1,
        "Formula installs shared setup contracts with build_isolation: false",
    )
    _check(manager_at != -1, "Formula installs the manager with build_isolation: false")
    _check(
        resources_at != -1 and contracts_at != -1 and manager_at != -1
        and resources_at < contracts_at < manager_at,
        "resources install before shared contracts, which install before the manager",
    )


def _no_user_site_env() -> dict[str, str]:
    """Strip ambient per-user site-packages so a probe measures only what
    Homebrew's python@3.13 + this Formula's own resources actually provide —
    never what this machine happens to have accumulated in
    ``~/Library/Python`` or equivalent. Without this, the probe can pass on
    a real machine only because of an unrelated personal
    `pip install --user setuptools`, not because of anything Homebrew or
    this Formula guarantees.
    """
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run_with_contention_retry(
    args: list[str],
    *,
    attempts: int = 3,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Retry a subprocess that touches the shared system Homebrew python@3.13
    prefix (venv creation, pip install against it) under a bounded backoff.

    Under quality_gates/run_smokes.py's own full-battery concurrency (up to
    ``_DEFAULT_JOBS`` parallel smoke subprocesses), this call's two heaviest
    steps have been observed to fail nondeterministically while passing
    cleanly every time in isolation (iss_469b680c) -- real contention on a
    genuinely shared host resource, not a defect in what is being asserted.
    This retries only the transient subprocess step itself; every assertion
    in this file still evaluates the final (successful or failed) result,
    so a real, reproducible failure still fails loudly here.
    """
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(attempts):
        last = subprocess.run(args, check=False, capture_output=True, text=True, env=env)
        if last.returncode == 0:
            return last
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    assert last is not None  # attempts >= 1 guarantees at least one run
    return last


_PINNED_WHEELS_DIR = Path(__file__).resolve().parent / "fixtures" / "pinned_wheels"

# Vendored copies of the exact wheels the Formula pins (matched to the
# `resource "name" do ... end` blocks `_parse_pinned_resources` parses), so
# `_check_no_index_formula_install` can provision them without the network —
# each one is re-hashed against the Formula's OWN declared sha256 at run
# time, so a stale vendored file (Formula pin bumped, fixture not updated)
# fails loudly here rather than silently installing the wrong bytes.
_PINNED_WHEEL_FILENAMES: dict[str, str] = {
    "pip": "pip-26.2.1-py3-none-any.whl",
    "setuptools": "setuptools-84.0.0-py3-none-any.whl",
    "wheel": "wheel-0.48.0-py3-none-any.whl",
    "packaging": "packaging-26.2-py3-none-any.whl",
}


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_pinned_build_resources(
    venv_python: Path, resources: dict[str, tuple[str, str]],
) -> None:
    """Provision the venv with the Formula's pinned setuptools/wheel/packaging,
    from the vendored, hash-verified local wheels — mirrors what the real
    Formula's own `install` method does (`venv.pip_install resources`,
    solet_cli/homebrew/Formula/solet.rb.template) before the
    `build_isolation: false` manager install below, so this offline smoke's
    venv is provisioned the same way a real `brew install` provisions its
    own (isolated, non-system-site) venv — not left to depend on whatever
    the host machine's ambient site-packages happens to contain.
    """
    for name, wheel_filename in _PINNED_WHEEL_FILENAMES.items():
        pinned_sha256 = resources[name][1]
        wheel_path = _PINNED_WHEELS_DIR / wheel_filename
        _check(wheel_path.is_file(), f"vendored wheel present for pinned resource {name!r}: {wheel_path}")
        actual_sha256 = _sha256_of(wheel_path)
        _check(
            actual_sha256 == pinned_sha256,
            f"vendored {wheel_filename} sha256 matches the Formula's pinned "
            f"resource {name!r} (expected {pinned_sha256}, got {actual_sha256} — "
            "the Formula's pin moved without the vendored fixture being updated)",
        )
    # The Formula's bootstrap: the venv's interpreter runs pip from the pinned wheel itself.
    pip_wheel = _PINNED_WHEELS_DIR / _PINNED_WHEEL_FILENAMES["pip"]
    booted = _run_with_contention_retry(
        [str(venv_python), str(pip_wheel / "pip"), "install", "--no-index", "--no-deps", "--ignore-installed", str(pip_wheel)],
    )
    _check(booted.returncode == 0, f"pinned pip bootstraps into the venv from its own wheel: {booted.stderr}")
    own_pip = subprocess.run(
        [str(venv_python), "-c", "import pip, sys; print(pip.__file__.startswith(sys.prefix))"],
        check=False, capture_output=True, text=True, env=_no_user_site_env(),
    )
    _check(own_pip.stdout.strip() == "True", f"the venv's -m pip is its own bootstrapped pip, not the base's: {own_pip.stdout}{own_pip.stderr}")
    installed_resources = _run_with_contention_retry(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(_PINNED_WHEELS_DIR),
            "--no-deps",
            *(name for name in _PINNED_WHEEL_FILENAMES if name != "pip"),
        ],
    )
    _check(
        installed_resources.returncode == 0,
        f"pinned build resources (setuptools/wheel/packaging) install failed: {installed_resources.stderr}",
    )


def _check_no_index_formula_install(root: Path, formula: str) -> None:
    python = _homebrew_python()
    venv = root / "formula-venv"
    created = _run_with_contention_retry(
        [
            str(python),
            "-m",
            "venv",
            "--system-site-packages",
            "--without-pip",
            str(venv),
        ]
    )
    _check(created.returncode == 0, f"Homebrew Python virtualenv failed: {created.stderr}")
    venv_python = venv / "bin" / "python3.13"
    no_user_site = _no_user_site_env()

    # RED, unconditionally, every run: with ambient per-user state excluded,
    # a bare --system-site-packages venv against Homebrew's own python@3.13
    # must NOT already have setuptools — if this ever passes, either Homebrew
    # started shipping it (drop the resources below) or this probe stopped
    # measuring what it claims to.
    before = subprocess.run(
        [str(venv_python), "-c", "import setuptools"],
        check=False,
        capture_output=True,
        text=True,
        env=no_user_site,
    )
    _check(
        before.returncode != 0,
        "RED control: setuptools is absent from a clean venv before the "
        "pinned resources are installed (if this is green, the probe is "
        "not measuring what this Formula actually provides)",
    )

    resources = _parse_pinned_resources(formula)
    _check(
        {"pip", "setuptools", "wheel", "packaging"} <= resources.keys(),
        "Formula declares pinned pip, setuptools, wheel, and packaging resources",
    )
    _assert_resources_declared_before_manager_install(formula)

    # GREEN: provision the pinned setuptools/wheel/packaging from the
    # vendored, hash-verified local wheels (see _install_pinned_build_resources)
    # — mirrors the real Formula's own `venv.pip_install resources` step,
    # offline, so build_isolation:false below has what it needs without
    # depending on whatever the host's ambient site-packages happens to
    # contain. This file still never fetches anything over the network —
    # only ci/resource_provisioning_acceptance.py does that, to prove the
    # Formula's declared URLs/checksums are themselves still correct
    # against the real PyPI files (gated the same way
    # solet_cli/homebrew/ci/lifecycle_acceptance.py gates its own
    # network-dependent run). If that acceptance test ever re-pins these
    # resources, the vendored wheels under fixtures/pinned_wheels/ and
    # _PINNED_WHEEL_FILENAMES above need updating too — the sha256 check in
    # _install_pinned_build_resources fails loudly if they drift apart.
    _install_pinned_build_resources(venv_python, resources)

    environment = os.environ.copy()
    environment.update({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    installed = _run_with_contention_retry(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-binary=:all:",
            "--ignore-installed",
            "--no-compile",
            "--no-build-isolation",
            str(_REPOSITORY / "solet_setup_contracts"),
            str(_REPOSITORY / "solet_cli"),
        ],
        env=environment,
    )
    _check(installed.returncode == 0, f"no-index manager install failed: {installed.stderr}")
    version = subprocess.run(
        [str(venv / "bin" / "solet"), "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    _check(
        version.returncode == 0 and version.stdout.strip() == "solet 0.1.0",
        "installed no-index manager entrypoint runs",
    )


def _check_packaged_flow_dry_run(root: Path) -> None:
    seed_lock = root / "seed.lock.json"
    seed_lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "https://github.com/solet-public/macos-bizops.git",
                "release_tag": "release-2026-08-21",
                "commit": "a" * 40,
                "tree_hash": "b" * 40,
                "archive_sha256": "c" * 64,
                "profile": "free",
            }
        ),
        encoding="utf-8",
    )
    target = root / "Solets" / "brew-test"
    result = run(
        [
            "--home",
            str(root / "manager"),
            "--contract-dir",
            str(_REPOSITORY / "plugins" / "github_midwife_plugin" / "knowledge_base"),
            "--seed-lock",
            str(seed_lock),
            "create",
            "brew-test",
            "--target",
            str(target),
            "--decision",
            "session_sources=",
            "--dry-run",
            "--json",
        ]
    )
    _check(
        result.kind == "create_preview"
        and result.status == "preview_ready"
        and int(result.exit_code) == 0,
        "packaged macos-bizops flow reaches a successful dry-run frontier",
    )
    _check(
        result.data.get("dry_run_writes") == 0 and not target.exists(),
        "dry-run writes no target state",
    )


def main() -> int:
    metadata_value: object = json.loads(_EXAMPLE.read_text(encoding="utf-8"))
    _check(isinstance(metadata_value, dict), "example release metadata is an object")
    metadata = cast(dict[str, object], metadata_value)
    metadata["manager_url"] = (
        "https://github.com/solet-public/homebrew-tap/releases/download/"
        "manager-v0.1.0-r0/solet-0.1.0.tar.gz"
    )
    metadata["manager_source_ref"] = "a" * 40
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        formula, lock, output = _render_valid_payload(root, metadata)
        _check_formula_boundary(formula, lock, metadata)
        _check_named_seed_catalog(output, metadata, lock)
        _check_identity_refusals(root, metadata)
        _check_independent_manager_identities(root, metadata)
        _check_no_index_formula_install(root, formula)
        _check_packaged_flow_dry_run(root)
    print(f"release_payload_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
