"""The §7.4 comparators grade real bytes, and say so when they cannot.

Pins ``release_identity``: the manifest and receipt loaders refuse any
drift from their closed schemas; the installed-package walk hashes every
payload member and nothing the installer added; each comparator returns
``verified`` only when the measured side equals the declared side, names
the drifted paths / components / digest on ``drifted``, and reports
``unattestable`` WITH its reason when a side could not be measured -- an
absent manifest, a checkout without ``.git``, a solet that is not running.

Offline: a throwaway git repository, a directory of small files, JSON built
from their digests.  No keg, no network, no running process.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(Path(__file__).resolve().parent)]

import tempfile  # noqa: E402
from collections.abc import Callable  # noqa: E402
from typing import Any  # noqa: E402

from _release_identity_fixture import (  # noqa: E402
    MANAGER_PREFIX,
    OTHER_COMMIT,
    SOURCE_COMMIT,
    build_package,
    build_seed_checkout,
    git,
    manifest,
    write_json,
    write_receipt,
)
from solet_manager.errors import SourceError  # noqa: E402
from solet_manager.release_identity import (  # noqa: E402
    NOT_CRYPTOGRAPHIC,
    InstallSource,
    compare_manager,
    compare_runtime,
    compare_seed_checkout,
    installed_file_digests,
    load_install_source,
    load_release_manifest,
)

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _refused(loader: Callable[[Path], object], path: Path, message: str, *, mentioning: str | None = None) -> None:
    """The loader must raise SourceError for ``path``; anything else is a silent acceptance."""

    try:
        loader(path)
    except SourceError as exc:
        _check(mentioning is None or mentioning in str(exc), f"{message}: {exc}")
        return
    raise AssertionError(f"red: {message}: loaded without refusal")


def _assert_loaders_refuse_schema_drift(root: Path) -> None:
    seed = build_seed_checkout(root / "seed")
    good = manifest(seed=seed, file_digests={})
    path = write_json(root / "release_manifest.json", good)
    _check(load_release_manifest(path)["release_label"] == "r44", "a closed v1 manifest loads")
    _refused(load_release_manifest, write_json(path, {**good, "extra": 1}), "an extra manifest key is refused by name", mentioning="exactly")
    missing = dict(good)
    del missing["tap"]
    _refused(load_release_manifest, write_json(path, missing), "a missing manifest key is refused")
    path.write_text("{ not json", encoding="utf-8")
    _refused(load_release_manifest, path, "a corrupt manifest is unreadable, not empty", mentioning="unreadable")
    receipt = write_receipt(root / "install-source.json")
    _check(load_install_source(receipt).source_commit == SOURCE_COMMIT, "the v1 receipt loads its source commit")
    _refused(load_install_source, write_json(receipt, {"source": "fixture-keg", "version": "0.1.0"}), "the pre-v1 fixture receipt shape is refused")
    _refused(load_install_source, write_json(receipt, {"schema_version": 1, "mode": "release", "source_commit": "not-a-commit"}), "a non-hex source_commit is refused")


def _assert_package_walk_hashes_payload_members_only(root: Path) -> dict[str, str]:
    digests = build_package(root / "pkg")
    installed = installed_file_digests(root / "pkg")
    _check(installed == digests, f"the walk digests exactly the payload members under {MANAGER_PREFIX}: {sorted(installed)}")
    _check(not any("__pycache__" in path or path.endswith(".pyc") for path in installed), "bytecode caches are never hashed")
    return digests


def _assert_manager_grades(root: Path, digests: dict[str, str]) -> None:
    seed = build_seed_checkout(root / "seed2")
    declared = manifest(seed=seed, file_digests=digests)
    receipt = InstallSource(root / "install-source.json", "release", SOURCE_COMMIT)
    verified = compare_manager(installed_file_digests(root / "pkg"), receipt, declared)
    _check(verified["status"] == "verified" and verified["reason"] is None, f"identical bytes + commit verify: {verified['reason']}")
    _check(verified["files_hashed"] == len(digests), "the verified section counts every hashed file")
    (root / "pkg" / "models.py").write_text("MANAGER_VERSION = '9.9.9'\n", encoding="utf-8")
    (root / "pkg" / "extra.py").write_text("", encoding="utf-8")
    drifted = compare_manager(installed_file_digests(root / "pkg"), receipt, declared)
    _check(drifted["status"] == "drifted" and drifted["reason"] == "file_digest_drift", "an edited file drifts")
    _check(drifted["drifted_paths"] == [MANAGER_PREFIX + "models.py"], f"the drifted path is named: {drifted['drifted_paths']}")
    _check(drifted["unexpected_paths"] == [MANAGER_PREFIX + "extra.py"], "an undeclared file is named as unexpected")
    (root / "pkg" / "extra.py").unlink()
    (root / "pkg" / "models.py").write_text("MANAGER_VERSION = '0.1.0'\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").unlink()
    missing = compare_manager(installed_file_digests(root / "pkg"), receipt, declared)
    _check(missing["missing_paths"] == [MANAGER_PREFIX + "__init__.py"], "a declared-but-absent file is named as missing")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    skewed = compare_manager(installed_file_digests(root / "pkg"), InstallSource(root / "r", "release", OTHER_COMMIT), declared)
    _check(skewed["status"] == "drifted" and skewed["reason"] == "source_commit_mismatch", "a receipt naming another commit drifts even with identical bytes")
    _check(skewed["source_commit_matches"] is False, "the commit comparison is recorded")
    absent = compare_manager(installed_file_digests(root / "pkg"), receipt, None)
    _check(absent["status"] == "unattestable" and absent["reason"] == "release_manifest_absent", "no manifest is unattestable, never verified")
    _check(absent["files_hashed"] == len(digests) and absent["file_digests"] == digests, "the measured digests are still recorded without a manifest")
    no_receipt = compare_manager(installed_file_digests(root / "pkg"), None, declared)
    _check(no_receipt["status"] == "unattestable" and no_receipt["reason"] == "install_source_absent", "identical bytes with no receipt stay unattestable")
    no_digests = dict(declared)
    no_digests["manager"] = {**declared["manager"], "file_digests": None}
    _check(compare_manager(installed_file_digests(root / "pkg"), receipt, no_digests)["reason"] == "manifest_file_digests_absent", "a manifest without digests names that")


def _assert_seed_checkout_grades(root: Path) -> None:
    seed = build_seed_checkout(root / "checkout")
    declared = manifest(seed=seed, file_digests={})
    verified = compare_seed_checkout(root / "checkout", declared)
    _check(verified["status"] == "verified", f"a clean checkout at the declared commit verifies: {verified['reason']}")
    _check(verified["head_commit"] == seed["commit"] and verified["head_tree"] == seed["tree_hash"], "head and tree are measured with git")
    rows = {row["component"]: row for row in verified["components"]}
    _check(rows["plugin:alpha"]["status"] == "verified" and rows["plugin:beta"]["observed_subtree_hash"] == seed["plugin:beta"], "per-plugin subtree hashes are measured")
    _check(rows["platform_base"]["status"] == "not_compared", "a non-plugin component is reported as not compared, not verified")
    (root / "checkout" / "plugins" / "alpha" / "plugin.py").write_text("NAME = 'edited'\n", encoding="utf-8")
    dirty = compare_seed_checkout(root / "checkout", declared)
    _check(dirty["status"] == "drifted" and dirty["reason"] == "working_tree_dirty", "an uncommitted edit drifts as dirty")
    _check(dirty["dirty_paths"] == ["plugins/alpha/plugin.py"], f"the dirty path is named: {dirty['dirty_paths']}")
    git(root / "checkout", "add", "-A")
    git(root / "checkout", "commit", "-q", "-m", "advance alpha")
    _assert_seed_checkout_drift_is_localised(root, seed, declared)


def _assert_seed_checkout_drift_is_localised(root: Path, seed: dict[str, str], declared: dict[str, Any]) -> None:
    advanced = compare_seed_checkout(root / "checkout", declared)
    _check(advanced["status"] == "drifted" and advanced["reason"] == "seed_identity_mismatch", "a new commit drifts as an identity mismatch")
    _check(advanced["drifted_components"] == ["plugin:alpha"], f"drift is localised to the advanced plugin: {advanced['drifted_components']}")
    head = {"commit": advanced["head_commit"], "tree_hash": advanced["head_tree"]}
    localised = compare_seed_checkout(root / "checkout", manifest(seed={**seed, **head}, file_digests={}))
    _check(localised["reason"] == "component_subtree_drift" and localised["drifted_components"] == ["plugin:alpha"], "matching head with a stale component declaration localises to the component")
    gone = manifest(seed={**seed, **head, "plugin:gamma": "0" * 40}, file_digests={})
    _check("plugin:gamma" in compare_seed_checkout(root / "checkout", gone)["drifted_components"], "a declared plugin absent from the tree is drift")
    no_manifest = compare_seed_checkout(root / "checkout", None)
    _check(no_manifest["status"] == "unattestable" and no_manifest["reason"] == "release_manifest_absent" and no_manifest["head_commit"] is not None, "no manifest is unattestable but git identity is still measured")
    no_components = compare_seed_checkout(root / "checkout", manifest(seed=head, file_digests={}, components=False))
    _check(no_components["status"] == "verified" and no_components["components"] == [], "a draft manifest without components verifies head/tree alone")
    plain = root / "plain"
    plain.mkdir()
    no_git = compare_seed_checkout(plain, declared)
    _check(no_git["status"] == "unattestable" and no_git["reason"] == "no_git_metadata", "a clone without .git is stated unattestable, not guessed")


def _assert_runtime_grades(root: Path) -> None:
    seed = build_seed_checkout(root / "rt")
    surface = "sha256:" + "7" * 64
    declared = manifest(seed=seed, file_digests={}, surface_sha256=surface)
    observed = {"release_surface_sha256": surface, "served_by_self": True}
    ok = compare_runtime(observed, None, declared)
    _check(ok["status"] == "verified" and ok["declared_release_surface_sha256"] == surface, "a matching surface digest verifies the RUNNING code")
    bad = compare_runtime({"release_surface_sha256": "sha256:" + "0" * 64}, None, declared)
    _check(bad["status"] == "drifted" and bad["reason"] == "release_surface_mismatch", "a different surface digest drifts")
    down = compare_runtime(None, "solet_not_running", declared)
    _check(down["status"] == "unattestable" and down["reason"] == "solet_not_running", "no running process is unattestable with its reason")
    no_surface = compare_runtime(observed, None, manifest(seed=seed, file_digests={}, surface_sha256=None))
    _check(no_surface["reason"] == "manifest_surface_digests_absent", "a draft manifest without surface digests names that")
    _check("NOT CRYPTOGRAPHIC" in NOT_CRYPTOGRAPHIC and "signature" in NOT_CRYPTOGRAPHIC, "the header says what the comparison is not")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _assert_loaders_refuse_schema_drift(root / "loaders")
        digests = _assert_package_walk_hashes_payload_members_only(root / "walk")
        _assert_manager_grades(root / "walk", digests)
        _assert_seed_checkout_grades(root / "seed")
        _assert_runtime_grades(root / "runtime")
    print(f"release_identity_comparator_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
