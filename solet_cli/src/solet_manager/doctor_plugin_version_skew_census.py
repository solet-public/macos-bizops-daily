"""Passive advisory over disagreement between a plugin's version sources.

Detection coverage for the plugin-convergence row of the 2026-09-02
doctor-detection audit.  REPORT-ONLY, and deliberately FORK-NEUTRAL: it declares
no source authoritative and prescribes no remediation.

Row detected
------------
``iss_1e3a1558`` (D-8-u4) -- ``plugin_install`` converges to the WRONG FIXED
POINT.  Probe success is ``visible AND interpreter_ok``, where visibility checks
only id/installed/enabled, and the module contains ZERO version reads.  So a
stale-but-enabled plugin returns verified, ``apply`` never runs, and the
reinstall that would refresh it never fires: staleness is a STABLE verified
state, and setup and doctor both report success while stale hook code executes.
The sharpest facet is that remediation is present and correctly wired -- the
visibility probes carry ``remediation_operation_refs`` to the install operations
-- so the control hangs off a sensor that cannot sense the condition.  The row
records that no version-aware probe exists anywhere, measured across the flow
probes and both doctor modules.  This is that probe.

Why it grades disagreement and nothing else
-------------------------------------------
The row's fix fork is OPEN and explicitly deferred: the authoritative
current-version source is packaged ``plugin.json`` vs the marketplace manifest,
AND THEY MAY DISAGREE.  A probe that decided which one is right would silently
settle that design question in code, and a later ruling the other way would make
this check wrong rather than merely incomplete.

So this check reports the three version-bearing sources and warns only when they
DISAGREE.  It never says which is correct, never says which way to converge, and
prescribes no repair action -- the "repair" field names the open question instead
of an instruction.  Whatever the fork resolves to, a disagreement between these
three is a real finding, and agreement is not evidence of correctness under any
resolution.

The three sources
-----------------
1. The registry -- ``~/.claude/plugins/installed_plugins.json``, whose entry for
   a selector carries a ``version`` and the ``installPath`` it was installed to.
2. The installed cache copy -- ``<installPath>/.claude-plugin/plugin.json``, the
   code that ACTUALLY executes.  A plugin runs from this cache copy, not from the
   checkout, so this is the version whose behaviour is live.
3. The packaged source -- the checkout's own plugin manifest, located by
   following the ``source`` pointer in ``<target>/.claude-plugin/marketplace.json``
   rather than by a hardcoded path.

Sources 1 and 2 can disagree with EACH OTHER, not only with the checkout: the
registry records what an install believed it placed, and the cache directory
holds what is there now.  That pair is graded too.

What this module does NOT claim
-------------------------------
It does not decide staleness, does not rank the sources, and does not remediate.
It also does not read the codex-side plugin tree; the row's measured instance and
the registry shape above are Claude-side, and inventing a second layout unmeasured
would be guessing.  A plugin the marketplace declares but the registry does not
carry is reported ``unknown``, never ``verified`` -- an uninstalled plugin has no
version agreement to confirm.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .models import InstanceRecord, JsonValue

_MARKETPLACE_RELPATH = Path(".claude-plugin") / "marketplace.json"
_PLUGIN_MANIFEST_RELPATH = Path(".claude-plugin") / "plugin.json"
_REGISTRY_RELPATH = Path(".claude") / "plugins" / "installed_plugins.json"

_SKEW_CHECK_ID = "doctor::plugin_version_source_agreement_v1"

# Named rather than prescribed: the row's fix fork is open, so this check states
# the question instead of an instruction.
_OPEN_QUESTION = (
    "This check does NOT say which source is authoritative -- that question is "
    "open and deferred (iss_1e3a1558): packaged plugin.json vs marketplace "
    "manifest may legitimately disagree. Reported so a human can decide; no "
    "convergence direction is implied and no repair is prescribed."
)


def collect_plugin_version_skew_advisories(
    record: InstanceRecord,
    *,
    home: Path | None = None,
) -> list[JsonValue]:
    """Return report-only checks comparing each plugin's version sources."""

    target = Path(record.target)
    user_home = Path.home() if home is None else home
    return [_version_skew_advisory(target, user_home)]


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Return a parsed JSON object, or ``None`` when the file is absent."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is {type(value).__name__}, not an object")
    return value


def _manifest_version(path: Path) -> str | None:
    """Return the ``version`` a plugin manifest declares, if it declares one."""

    manifest = _read_json_object(path)
    if manifest is None:
        return None
    version = manifest.get("version")
    return version if isinstance(version, str) else None


def _declared_plugins(marketplace: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(plugin_name, source)`` for each entry the marketplace declares."""

    entries = marketplace.get("plugins")
    if not isinstance(entries, list):
        return []
    declared: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        source = entry.get("source")
        if isinstance(name, str) and isinstance(source, str):
            declared.append((name, source))
    return declared


def _registry_entry(registry: dict[str, Any], selector: str) -> dict[str, Any] | None:
    """Return the first registry row for a selector, if the registry has one."""

    plugins = registry.get("plugins")
    if not isinstance(plugins, dict):
        return None
    rows = plugins.get(selector)
    if not isinstance(rows, list) or not rows:
        return None
    first = rows[0]
    return first if isinstance(first, dict) else None


def _version_skew_advisory(target: Path, home: Path) -> dict[str, JsonValue]:
    marketplace_path = target / _MARKETPLACE_RELPATH
    registry_path = home / _REGISTRY_RELPATH
    source = f"{marketplace_path} + {registry_path} + installed cache"

    expected: dict[str, JsonValue] = {"version_sources_agree": True}
    observed: dict[str, JsonValue] = {"plugins": [], "disagreements": []}

    try:
        marketplace = _read_json_object(marketplace_path)
        registry = _read_json_object(registry_path)
    except (OSError, ValueError) as exc:
        return advisory_unknown(
            _SKEW_CHECK_ID,
            "A plugin manifest could not be read, so version agreement is unknown.",
            expected,
            observed,
            source,
            "plugin_manifest_unreadable",
            str(exc),
        )

    if marketplace is None:
        return advisory_verified(
            _SKEW_CHECK_ID,
            "This target declares no plugin marketplace, so there are no plugin version "
            "sources to disagree.",
            expected,
            observed,
            source,
        )

    marketplace_name = marketplace.get("name")
    if not isinstance(marketplace_name, str):
        return advisory_unknown(
            _SKEW_CHECK_ID,
            "The marketplace manifest declares no name, so no selector can be resolved.",
            expected,
            observed,
            source,
            "marketplace_name_missing",
            "Without a marketplace name the registry selector cannot be built.",
        )

    return _grade_declared_plugins(
        marketplace, marketplace_name, registry, target, expected, observed, source
    )


def _grade_declared_plugins(
    marketplace: dict[str, Any],
    marketplace_name: str,
    registry: dict[str, Any] | None,
    target: Path,
    expected: dict[str, JsonValue],
    observed: dict[str, JsonValue],
    source: str,
) -> dict[str, JsonValue]:
    """Compare the three version sources for every declared plugin."""

    rows: list[JsonValue] = []
    disagreements: list[JsonValue] = []
    ungraded: list[JsonValue] = []

    for name, relative_source in _declared_plugins(marketplace):
        selector = f"{name}@{marketplace_name}"
        try:
            row = _plugin_versions(selector, name, relative_source, registry, target)
        except (OSError, ValueError) as exc:
            ungraded.append(f"{selector}: {exc}")
            continue
        if row is None:
            ungraded.append(f"{selector}: not present in the installed-plugin registry")
            continue
        rows.append(dict(row))
        if _versions_disagree(row):
            disagreements.append(selector)

    observed["plugins"] = rows
    observed["disagreements"] = disagreements
    observed["ungraded"] = ungraded

    if disagreements:
        return advisory_warn(
            _SKEW_CHECK_ID,
            "A plugin's version sources disagree: the registry, the installed cache copy "
            "that actually executes, and the packaged manifest do not all report the same "
            "version.",
            expected,
            observed,
            source,
            "plugin_version_sources_disagree",
            _OPEN_QUESTION,
        )

    if ungraded and not rows:
        return advisory_unknown(
            _SKEW_CHECK_ID,
            "No declared plugin could be graded, so version agreement is unknown.",
            expected,
            observed,
            source,
            "plugin_versions_ungradable",
            "; ".join(str(item) for item in ungraded),
        )

    return advisory_verified(
        _SKEW_CHECK_ID,
        "Every gradable plugin reports the same version from the registry, the installed "
        "cache copy and the packaged manifest.",
        expected,
        observed,
        source,
    )


def _versions_disagree(row: dict[str, JsonValue]) -> bool:
    """True when the three sources do not all report the same version.

    Narrowed to ``str | None`` before comparison: a ``JsonValue`` may be a list or
    dict, which a set cannot hold, so an unhashable version would crash the check
    rather than report skew.  A missing source reads as ``None`` and counts as a
    distinct value -- absent is not equal to present.
    """

    versions = tuple(
        value if isinstance(value, str) else None
        for value in (
            row["registry_version"],
            row["cache_version"],
            row["packaged_version"],
        )
    )
    return len(set(versions)) > 1


def _plugin_versions(
    selector: str,
    name: str,
    relative_source: str,
    registry: dict[str, Any] | None,
    target: Path,
) -> dict[str, JsonValue] | None:
    """Return the three versions for one plugin, or ``None`` when uninstalled."""

    entry = None if registry is None else _registry_entry(registry, selector)
    if entry is None:
        return None
    registry_version = entry.get("version")
    install_path = entry.get("installPath")
    cache_version = (
        _manifest_version(Path(install_path) / _PLUGIN_MANIFEST_RELPATH)
        if isinstance(install_path, str)
        else None
    )
    packaged_version = _manifest_version(
        target / relative_source.lstrip("./") / _PLUGIN_MANIFEST_RELPATH
    )
    return {
        "plugin": name,
        "selector": selector,
        "registry_version": registry_version if isinstance(registry_version, str) else None,
        "cache_version": cache_version,
        "packaged_version": packaged_version,
        "install_path": install_path if isinstance(install_path, str) else None,
    }
