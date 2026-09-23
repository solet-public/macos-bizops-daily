"""A plugin's three version sources are actually compared against each other.

Detection coverage for ``iss_1e3a1558`` (D-8-u4).

``plugin_install`` converges to the wrong fixed point: probe success is visible
AND interpreter_ok, visibility checks only id/installed/enabled, and the module
contains ZERO version reads.  A stale-but-enabled plugin is therefore a STABLE
verified state -- setup and doctor both report success while stale hook code
executes -- and the correctly-wired remediation hangs off a sensor that cannot
sense the condition.

This check is deliberately FORK-NEUTRAL, and the tests below pin that as hard as
they pin the detection.  The row's fix fork is open: which of packaged
``plugin.json`` and the marketplace manifest is authoritative is a deferred design
question, and they may legitimately disagree.  So the check warns ONLY on
disagreement, names no authoritative source, and prescribes no repair --
:func:`_assert_warning_prescribes_no_convergence` pins that the reported "repair"
states the open question rather than an instruction, because a check that quietly
picked a winner would settle a deferred design decision in code.

The shape is not hypothetical.  Measured on this machine while writing the check:
the registry and installed cache report coordination-hooks 0.6.0 while the
packaged manifest reports 0.8.1 -- the live reg-58 incident, sitting undetected
because nothing anywhere compares these numbers.

Each red asserts its NAMED ``reason_code``:

* the three sources disagreeing -> plugin_version_sources_disagree
* nothing gradable at all       -> plugin_versions_ungradable
* a manifest that will not parse -> plugin_manifest_unreadable
* a marketplace with no name     -> marketplace_name_missing

Offline: constructed manifest trees under a temporary directory only.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.doctor_plugin_version_skew_census import (  # noqa: E402
    collect_plugin_version_skew_advisories,
)

_ID = "doctor::plugin_version_source_agreement_v1"

_PLUGIN = "coordination-hooks"
# fixture marketplace name: a real origin identity here is refused by the
# shipped-doc gate's reserved-identity scan, and the census builds its selector
# as f"{name}@{marketplace_name}", so no assertion depends on this value.
_MARKET = "census"
_SOURCE = "./plugins/github_midwife_plugin/claude_plugin/coordination-hooks"

_CHECKS = 0


def _check(condition: bool, message: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(f"red: {message}")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _build(
    root: Path,
    *,
    registry_version: str | None,
    cache_version: str | None,
    packaged_version: str | None,
    marketplace_name: str | None = _MARKET,
    installed: bool = True,
) -> tuple[Path, Path]:
    """Materialise a target checkout and a home with a plugin registry."""

    target = root / "target"
    home = root / "home"
    market: dict[str, object] = {"plugins": [{"name": _PLUGIN, "source": _SOURCE}]}
    if marketplace_name is not None:
        market["name"] = marketplace_name
    _write_json(target / ".claude-plugin" / "marketplace.json", market)

    if packaged_version is not None:
        _write_json(
            target / _SOURCE.lstrip("./") / ".claude-plugin" / "plugin.json",
            {"name": _PLUGIN, "version": packaged_version},
        )

    install_path = home / "cache" / _PLUGIN / (cache_version or "none")
    if cache_version is not None:
        _write_json(
            install_path / ".claude-plugin" / "plugin.json",
            {"name": _PLUGIN, "version": cache_version},
        )

    plugins: dict[str, object] = {}
    if installed:
        plugins[f"{_PLUGIN}@{_MARKET}"] = [
            {"scope": "user", "installPath": str(install_path), "version": registry_version}
        ]
    _write_json(home / ".claude" / "plugins" / "installed_plugins.json", {"plugins": plugins})
    return target, home


class _Record:
    name = "census"

    def __init__(self, target: Path) -> None:
        self.target = str(target)


def _advisory(target: Path, home: Path) -> dict[str, object]:
    results = collect_plugin_version_skew_advisories(_Record(target), home=home)
    _check(len(results) == 1, f"the census stopped emitting exactly one check: {len(results)}")
    entry = results[0]
    assert isinstance(entry, dict)
    _check(str(entry["check_id"]) == _ID, f"unexpected check id: {entry['check_id']}")
    return entry


def _assert_agreeing_versions_are_green() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp), registry_version="0.8.1", cache_version="0.8.1", packaged_version="0.8.1"
        )
        advisory = _advisory(target, home)
        _check(
            advisory["status"] == "verified",
            f"three agreeing versions were not verified: {advisory['status']}",
        )
        _check(
            advisory["blocking"] is False,
            "a plugin version advisory must never block a doctor result",
        )


def _assert_stale_cache_is_named() -> None:
    """The measured reg-58 shape: cache and registry behind the packaged manifest."""

    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp), registry_version="0.6.0", cache_version="0.6.0", packaged_version="0.8.1"
        )
        advisory = _advisory(target, home)
        _check(
            advisory["reason_code"] == "plugin_version_sources_disagree",
            f"a stale installed plugin was not named: {advisory['reason_code']}",
        )
        observed = advisory["observed"]
        assert isinstance(observed, dict)
        _check(
            observed["disagreements"] == [f"{_PLUGIN}@{_MARKET}"],
            f"the disagreeing plugin was not identified: {observed['disagreements']}",
        )
        rows = observed["plugins"]
        assert isinstance(rows, list) and rows
        row = rows[0]
        assert isinstance(row, dict)
        _check(
            (row["registry_version"], row["cache_version"], row["packaged_version"])
            == ("0.6.0", "0.6.0", "0.8.1"),
            f"the three versions were not all reported: {row}",
        )


def _assert_registry_vs_cache_disagreement_is_caught() -> None:
    """Sources 1 and 2 can disagree with each other, not only with the checkout.

    The registry records what an install believed it placed; the cache directory
    holds what is actually there. A check that only compared installed-vs-packaged
    would miss this pair entirely.
    """

    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp), registry_version="0.8.1", cache_version="0.6.0", packaged_version="0.8.1"
        )
        advisory = _advisory(target, home)
        _check(
            advisory["reason_code"] == "plugin_version_sources_disagree",
            f"a registry/cache disagreement was not named: {advisory['reason_code']}",
        )


def _assert_registry_alone_disagreeing_is_caught() -> None:
    """The third pairwise direction: registry vs packaged, with the cache agreeing.

    Completes the pairwise matrix required of this check. The two tests above
    cover registry-vs-cache and cache-vs-packaged; this is the case where the
    installed code and the checkout agree and only the registry ROW is wrong,
    which is what a cache replaced without a registry update looks like. A check
    that compared only "what executes vs what is packaged" would call this green.
    """

    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp), registry_version="0.6.0", cache_version="0.8.1", packaged_version="0.8.1"
        )
        advisory = _advisory(target, home)
        _check(
            advisory["reason_code"] == "plugin_version_sources_disagree",
            f"a lone stale registry row was not named: {advisory['reason_code']}",
        )


def _assert_three_way_disagreement_is_caught() -> None:
    """All three sources different is still one disagreement, not a crash."""

    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp), registry_version="0.5.0", cache_version="0.6.0", packaged_version="0.8.1"
        )
        advisory = _advisory(target, home)
        _check(
            advisory["reason_code"] == "plugin_version_sources_disagree",
            f"a three-way disagreement was not named: {advisory['reason_code']}",
        )
        observed = advisory["observed"]
        assert isinstance(observed, dict)
        rows = observed["plugins"]
        assert isinstance(rows, list) and rows
        row = rows[0]
        assert isinstance(row, dict)
        _check(
            (row["registry_version"], row["cache_version"], row["packaged_version"])
            == ("0.5.0", "0.6.0", "0.8.1"),
            f"every reading must be reported, not just the disagreeing pair: {row}",
        )


def _assert_warning_prescribes_no_convergence() -> None:
    """Fork-neutrality is a property of the OUTPUT, so it is asserted directly.

    The row's authority question is deferred. A check whose repair text told an
    operator which way to converge would settle that question in code, so the
    repair must name the open question and must not instruct.
    """

    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp), registry_version="0.6.0", cache_version="0.6.0", packaged_version="0.8.1"
        )
        advisory = _advisory(target, home)
        repair = str(advisory["repair"])
        _check(
            "iss_1e3a1558" in repair,
            "the repair text must cite the deferred authority question by id",
        )
        _check(
            "does NOT say which source is authoritative" in repair,
            f"the repair text must disclaim authority, got: {repair[:120]}",
        )
        for instruction in ("reinstall", "upgrade", "run ", "converge to"):
            _check(
                instruction not in repair.lower(),
                f"the repair text must not prescribe an action, found {instruction!r}",
            )


def _assert_uninstalled_plugin_is_unknown_not_green() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp),
            registry_version=None,
            cache_version=None,
            packaged_version="0.8.1",
            installed=False,
        )
        advisory = _advisory(target, home)
        _check(
            advisory["status"] == "unknown",
            f"an uninstalled plugin did not read as unknown: {advisory['status']}",
        )
        _check(
            advisory["reason_code"] == "plugin_versions_ungradable",
            f"an uninstalled plugin was not named: {advisory['reason_code']}",
        )


def _assert_absent_marketplace_is_not_a_divergence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "target").mkdir(parents=True, exist_ok=True)
        _write_json(root / "home" / ".claude" / "plugins" / "installed_plugins.json", {})
        advisory = _advisory(root / "target", root / "home")
        _check(
            advisory["status"] == "verified",
            f"a target with no marketplace was warned about: {advisory['reason_code']}",
        )


def _assert_unnamed_marketplace_is_its_own_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target, home = _build(
            Path(tmp),
            registry_version="0.8.1",
            cache_version="0.8.1",
            packaged_version="0.8.1",
            marketplace_name=None,
        )
        advisory = _advisory(target, home)
        _check(
            advisory["reason_code"] == "marketplace_name_missing",
            f"an unnamed marketplace was not named: {advisory['reason_code']}",
        )


def _assert_unparseable_manifest_is_unknown_not_green() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "target"
        (target / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (target / ".claude-plugin" / "marketplace.json").write_text("{not json", encoding="utf-8")
        _write_json(root / "home" / ".claude" / "plugins" / "installed_plugins.json", {})
        advisory = _advisory(target, root / "home")
        _check(
            advisory["status"] == "unknown",
            f"an unparseable marketplace did not read as unknown: {advisory['status']}",
        )
        _check(
            advisory["reason_code"] == "plugin_manifest_unreadable",
            f"an unparseable marketplace was not named: {advisory['reason_code']}",
        )


def main() -> int:
    _assert_agreeing_versions_are_green()
    _assert_stale_cache_is_named()
    _assert_registry_vs_cache_disagreement_is_caught()
    _assert_registry_alone_disagreeing_is_caught()
    _assert_three_way_disagreement_is_caught()
    _assert_warning_prescribes_no_convergence()
    _assert_uninstalled_plugin_is_unknown_not_green()
    _assert_absent_marketplace_is_not_a_divergence()
    _assert_unnamed_marketplace_is_its_own_name()
    _assert_unparseable_manifest_is_unknown_not_green()
    print(f"doctor_plugin_version_skew_census_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
