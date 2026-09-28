#!/usr/bin/env python3
"""Bundle-license gate — keeps a shipped capability bundle license-clean.

Closes the gap found by the 2026-07-26 dependency + license provenance
audit record (dev-checkout workbench — not part of the shipped tree):
the bundle
boundary in ``capability_bundles.yaml`` is load-bearing for LICENSE COMPLIANCE,
not merely for scope. Every strong-copyleft package in this repo is confined to
plugins the shipped bundle excludes — adding the audio stack or the iterm2
plugin to a bundle would pull GPL into an Apache-2.0 distribution. Nothing
checked that. The seed seal-gate
(``plugins/seed_factory_plugin/src/seed_factory_plugin/license_gate.py``) looks
only at FIRST-PARTY declarations, is Apache-hardcoded, and is absence-tolerant;
it never sees a transitive dependency at all.

This gate is the complement: it reads the bundle membership, resolves each
bundle's TRANSITIVE third-party closure, and fails when a bundle carries strong
copyleft — or when a bundle plugin's own declaration is missing or non-Apache.

Categories:
  B0  undeclared-license   a bundle plugin's ``pyproject.toml`` declares no
                             ``license``. This is exactly the absence the seal
                             gate tolerates, so it is caught here instead.
  B1  non-apache-in-bundle a bundle plugin declares an SPDX id other than
                             ``Apache-2.0``. Allowlist per bundle::plugin for a
                             deliberately ``internal_private``-sealed bundle
                             carrying a separately-licensed connector.
  B2  strong-copyleft-dep  a package in the bundle's transitive closure carries
                             GPL / AGPL / SSPL / OSL / EUPL / CDDL / EPL. WEAK
                             and file-level copyleft (LGPL, MPL) is REPORTED,
                             never blocking: both are compatible with Apache-2.0
                             distribution, and the seed ships no dependency
                             bytes (allowlist-copy of source only — genesis
                             pip-installs from PyPI at hydration), so the
                             redistribution clauses do not engage.
  B3  unresolvable-dep     a declared dependency is not installed, so the
                             closure could not be completed. Blocking BY DESIGN:
                             an incomplete closure is not a proof of absence,
                             and a gate that reported "clean" from a partial
                             walk would be worse than no gate.

Scope: the bundles declared in
``plugins/seed_factory_plugin/knowledge_base/capability_bundles.yaml`` and the
``plugins/<name>/pyproject.toml`` of each member. Reads installed distribution
metadata via ``importlib.metadata`` — the stdlib packaging-metadata API — so it
measures the same resolution a hydrating newborn gets, and adds no dependency
of its own beyond ``packaging`` (the standard requirement/marker parser, already
present) and ``yaml`` (already a platform dependency).

Exit codes (mirror the sibling gates):
  0  — clean / all-allowlisted / ``--warn-only``
  1  — non-allowlisted findings present
  2  — harness error
  64 — usage error (argparse)

Allowlist format (category-keyed, one register for every category); ``#`` starts
a comment and blank lines are ignored::

    B1 macos_samantha::schwab_market_data_plugin
    B2 some_bundle::some-package
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Final

import yaml
from packaging.requirements import InvalidRequirement, Requirement

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
BUNDLES_PATH: Final[Path] = (
    REPO_ROOT
    / "plugins"
    / "seed_factory_plugin"
    / "knowledge_base"
    / "capability_bundles.yaml"
)
PLUGINS_ROOT: Final[Path] = REPO_ROOT / "plugins"

#: The platform core. Its own requirements are declared in ``ananta/setup.py``
#: (``ananta/pyproject.toml`` is tooling config only, with no ``[project]``
#: table), so they are read from its INSTALLED metadata by the closure walk
#: rather than parsed out of executable setup code. Every plugin already
#: depends on it, but it is seeded explicitly so a bundle stays correct even
#: if some future plugin omits the dependency.
CORE_DISTRIBUTION: Final[str] = "ananta"

REQUIRED_SPDX: Final[str] = "Apache-2.0"

#: Strong / network copyleft — blocking inside an Apache-2.0 distribution.
STRONG_COPYLEFT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:A?GPL|GNU\s+(?:Affero\s+)?General\s+Public|SSPL|Server\s+Side\s+Public"
    r"|OSL|Open\s+Software\s+License|EUPL|CDDL|EPL|Eclipse\s+Public"
    r"|CC-BY-SA)\b",
    re.IGNORECASE,
)
#: Weak / file-level copyleft — reported for the inventory, never blocking.
WEAK_COPYLEFT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:LGPL|GNU\s+Lesser|MPL|Mozilla\s+Public)\b", re.IGNORECASE
)

CHECK_UNDECLARED: Final[str] = "B0"
CHECK_NON_APACHE: Final[str] = "B1"
CHECK_STRONG_COPYLEFT: Final[str] = "B2"
CHECK_UNRESOLVABLE: Final[str] = "B3"


class GateError(RuntimeError):
    """Raised when the gate cannot complete a measurement."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One gate finding, addressed as ``<bundle>::<subject>``."""

    check_id: str
    bundle: str
    subject: str
    detail: str

    @property
    def key(self) -> str:
        """The allowlist-matchable identity of this finding."""
        return f"{self.bundle}::{self.subject}"


def _canonical(name: str) -> str:
    """Canonicalize a distribution name for cross-source comparison."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def load_bundles() -> dict[str, list[str]]:
    """Read the curation manifest into ``{bundle_name: [plugin, ...]}``."""
    if not BUNDLES_PATH.is_file():
        raise GateError(f"capability bundles manifest not found: {BUNDLES_PATH}")
    document = yaml.safe_load(BUNDLES_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise GateError(f"{BUNDLES_PATH} does not parse to a mapping")
    bundles = document.get("bundles")
    if not isinstance(bundles, dict):
        raise GateError(f"{BUNDLES_PATH} carries no 'bundles' mapping")
    return {str(name): _bundle_plugins(name, spec) for name, spec in bundles.items()}


def _bundle_plugins(name: object, spec: object) -> list[str]:
    """Extract one bundle's plugin list, failing loud on a malformed entry."""
    if not isinstance(spec, dict):
        raise GateError(f"bundle {name!r} does not parse to a mapping")
    plugins = spec.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise GateError(f"bundle {name!r} carries no non-empty 'plugins' list")
    # Transition-retained plugins ship as code in the seed (rul_ffd59e37), so their licenses are checked too.
    retained = spec.get("transition_retained", [])
    if not isinstance(retained, list):
        raise GateError(f"bundle {name!r} carries a non-list 'transition_retained'")
    return [str(plugin) for plugin in (*plugins, *retained)]


def _read_pyproject(path: Path) -> dict[str, object]:
    """Parse a ``pyproject.toml``'s ``[project]`` table."""
    if not path.is_file():
        raise GateError(f"pyproject not found: {path}")
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
    if not isinstance(project, dict):
        raise GateError(f"{path} carries no [project] table")
    return project


def plugin_license(plugin: str) -> str | None:
    """Return a plugin's declared SPDX string, or ``None`` when undeclared."""
    declared = _read_pyproject(PLUGINS_ROOT / plugin / "pyproject.toml").get("license")
    if declared is None:
        return None
    if not isinstance(declared, str):
        raise GateError(
            f"{plugin}: license must be a bare SPDX string, got {type(declared).__name__}"
        )
    return declared


def _declared_requirements(path: Path) -> list[str]:
    """Return the canonical names a pyproject declares as dependencies."""
    dependencies = _read_pyproject(path).get("dependencies", [])
    if not isinstance(dependencies, list):
        raise GateError(f"{path}: [project].dependencies is not a list")
    return [_canonical(_requirement_name(str(entry), path)) for entry in dependencies]


def _requirement_name(entry: str, origin: Path) -> str:
    """Parse one requirement specifier into its distribution name."""
    try:
        return Requirement(entry).name
    except InvalidRequirement as exc:
        raise GateError(f"{origin}: unparseable requirement {entry!r}: {exc}") from exc


def bundle_roots(plugins: Iterable[str]) -> set[str]:
    """Direct third-party dependency roots of a bundle, plus the platform core."""
    roots: set[str] = {_canonical(CORE_DISTRIBUTION)}
    for plugin in plugins:
        roots.update(_declared_requirements(PLUGINS_ROOT / plugin / "pyproject.toml"))
    return roots


def installed_index() -> dict[str, metadata.Distribution]:
    """Index every installed distribution by canonical name."""
    index: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            index[_canonical(name)] = dist
    return index


def _active_requirements(dist: metadata.Distribution) -> list[str]:
    """Canonical names of a distribution's requirements that apply to this env.

    Extra-gated requirements are excluded: they are not installed by a plain
    ``pip install <dist>``, so they are not in the shipped closure.
    """
    active: list[str] = []
    for entry in dist.requires or []:
        try:
            requirement = Requirement(entry)
        except InvalidRequirement:
            continue
        if requirement.marker is not None and not requirement.marker.evaluate(
            {"extra": ""}
        ):
            continue
        active.append(_canonical(requirement.name))
    return active


def resolve_closure(
    roots: set[str], index: dict[str, metadata.Distribution]
) -> tuple[set[str], set[str]]:
    """Walk the transitive closure, returning ``(resolved, unresolvable)``."""
    resolved: set[str] = set()
    unresolvable: set[str] = set()
    pending: list[str] = sorted(roots)
    while pending:
        name = pending.pop()
        if name in resolved or name in unresolvable:
            continue
        dist = index.get(name)
        if dist is None:
            unresolvable.add(name)
            continue
        resolved.add(name)
        pending.extend(_active_requirements(dist))
    return resolved, unresolvable


def license_blob(dist: metadata.Distribution) -> str:
    """Every license-bearing metadata field of a distribution, as one string."""
    fields = [
        dist.metadata.get("License-Expression") or "",
        dist.metadata.get("License") or "",
    ]
    classifiers = [
        entry
        for entry in dist.metadata.get_all("Classifier") or []
        if str(entry).startswith("License ::")
    ]
    return " | ".join([*fields, *classifiers])


def _first_party_findings(bundle: str, plugins: Iterable[str]) -> list[Finding]:
    """B0 + B1 — every bundle plugin declares exactly one correct SPDX id."""
    findings: list[Finding] = []
    for plugin in plugins:
        declared = plugin_license(plugin)
        if declared is None:
            findings.append(
                Finding(
                    CHECK_UNDECLARED,
                    bundle,
                    plugin,
                    "ships in this bundle but declares no license",
                )
            )
        elif declared != REQUIRED_SPDX:
            findings.append(
                Finding(
                    CHECK_NON_APACHE,
                    bundle,
                    plugin,
                    f"declares {declared!r}, expected {REQUIRED_SPDX!r}",
                )
            )
    return findings


def _third_party_findings(
    bundle: str, plugins: Iterable[str], index: dict[str, metadata.Distribution]
) -> list[Finding]:
    """B2 + B3 — the bundle's transitive closure is copyleft-clean and complete."""
    first_party = {_canonical(path.name) for path in PLUGINS_ROOT.iterdir()}
    first_party.add(_canonical("ananta"))
    resolved, unresolvable = resolve_closure(bundle_roots(plugins), index)

    findings = [
        Finding(
            CHECK_UNRESOLVABLE,
            bundle,
            name,
            "declared dependency is not installed — closure incomplete",
        )
        for name in sorted(unresolvable - first_party)
    ]
    findings.extend(
        Finding(
            CHECK_STRONG_COPYLEFT,
            bundle,
            name,
            f"strong copyleft in the shipped closure: {blob.strip()[:160]}",
        )
        for name, blob in _closure_licenses(sorted(resolved - first_party), index)
        if STRONG_COPYLEFT.search(blob)
    )
    return findings


def _closure_licenses(
    names: list[str], index: dict[str, metadata.Distribution]
) -> list[tuple[str, str]]:
    """Pair each closure member with its license metadata blob."""
    return [(name, license_blob(index[name])) for name in names if name in index]


def collect_findings() -> list[Finding]:
    """Run every category over every declared bundle."""
    index = installed_index()
    findings: list[Finding] = []
    for bundle, plugins in load_bundles().items():
        findings.extend(_first_party_findings(bundle, plugins))
        findings.extend(_third_party_findings(bundle, plugins, index))
    findings.sort(key=lambda finding: (finding.check_id, finding.bundle, finding.subject))
    return findings


def load_allowlist(path: Path) -> set[tuple[str, str]]:
    """Parse a category-keyed allowlist into ``{(check_id, key), ...}``.

    Mandatory owner/reason/expires schema (D-3-structural, iss_23fa51b5).
    """
    try:
        keys = _load_tagged_allowlist(path)
    except FileNotFoundError as exc:
        raise GateError(str(exc)) from exc
    entries: set[tuple[str, str]] = set()
    for key in keys:
        parts = key.split(None, 1)
        if len(parts) != 2:
            raise GateError(f"{path}: expected '<check_id> <bundle>::<subject>', got {key!r}")
        entries.add((parts[0], parts[1].strip()))
    return entries


def _partition(
    findings: list[Finding], allowlist: set[tuple[str, str]]
) -> tuple[list[Finding], list[Finding]]:
    """Split findings into ``(blocking, allowlisted)``."""
    blocking = [f for f in findings if (f.check_id, f.key) not in allowlist]
    allowlisted = [f for f in findings if (f.check_id, f.key) in allowlist]
    return blocking, allowlisted


def _emit_human(blocking: list[Finding], allowlisted: list[Finding]) -> None:
    """Print a human-readable report; allowlisted findings stay visible."""
    for finding in allowlisted:
        print(f"  [allowlisted] {finding.check_id} {finding.key}: {finding.detail}")
    for finding in blocking:
        print(f"  {finding.check_id} {finding.key}: {finding.detail}", file=sys.stderr)


def _emit_json(blocking: list[Finding], allowlisted: list[Finding]) -> None:
    """Print the same report as JSON for machine consumers."""
    payload = {
        "blocking": [
            {"check_id": f.check_id, "key": f.key, "detail": f.detail} for f in blocking
        ],
        "allowlisted": [
            {"check_id": f.check_id, "key": f.key, "detail": f.detail}
            for f in allowlisted
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allowlist", type=Path, default=None, help="tracked-debt register to apply"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of prose")
    parser.add_argument(
        "--warn-only", action="store_true", help="report findings but exit 0"
    )
    return parser


def main(argv: list[str]) -> int:
    """Entry point. See the module docstring for exit-code semantics."""
    args = _build_parser().parse_args(argv)
    try:
        allowlist = load_allowlist(args.allowlist) if args.allowlist else set()
        findings = collect_findings()
    except (GateError, OSError, ValueError) as exc:
        print(f"ERROR: harness failure: {exc}", file=sys.stderr)
        return 2

    blocking, allowlisted = _partition(findings, allowlist)
    if args.json:
        _emit_json(blocking, allowlisted)
    else:
        _emit_human(blocking, allowlisted)
        if blocking:
            print(
                f"\n{len(findings)} bundle-license finding(s) "
                f"({len(allowlisted)} allowlisted; {len(blocking)} non-allowlisted).",
                file=sys.stderr,
            )
        else:
            print(
                f"OK: {len(findings)} finding(s) — "
                f"{len(allowlisted)} allowlisted; bundle-license gate clean."
            )
    if args.warn_only:
        return 0
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
