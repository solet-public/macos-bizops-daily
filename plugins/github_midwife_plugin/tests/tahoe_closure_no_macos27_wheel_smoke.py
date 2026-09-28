"""A macOS 26 fresh macos-bizops install carries no macOS 27-only package (iss_3a2a74ea).

Genesis pip-installs the roster ``load_plugin_allowlist`` resolves from the
setup's implementation decisions; each plugin installs editable with its
declared dependencies and its own ``vendor/`` wheels.  Before this change the
roster ignored the decisions, so a macOS 26 create pip-installed
``macos_inference_plugin`` and its ``apple-fm-sdk`` wheel, tagged
``macosx_27_0_arm64``.

The macOS 27-only set is detected, not remembered: every vendored wheel in the
tree whose platform tag is ``macosx_27`` or later, plus ``coreai-core``, the
Core AI runtime whose floor is macOS 27 (rul_5cc2910c; it has never run on 26,
iss_c6abab0d).  The llama.cpp closure must reach none of them, and the macOS 27
closure must reach ``apple-fm-sdk`` (the detector's control).

The roster check runs in every tree.  The package check reads each rostered
plugin's ``pyproject.toml``, so it runs where the macos-bizops plugins ship: the
origin checkout and a macos-bizops seed.
"""

from __future__ import annotations

# ruff: noqa: E402
import re
import sys
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_MIDWIFE = _REPO / "plugins" / "github_midwife_plugin"
if str(_MIDWIFE / "src") not in sys.path:
    sys.path.insert(0, str(_MIDWIFE / "src"))

from github_midwife_plugin.genesis import _declared_bundle_name
from github_midwife_plugin.profile_install import load_plugin_allowlist

_TEMPLATE = _MIDWIFE / "knowledge_base" / "profile_templates" / "macos-bizops.yaml"
_TAHOE = {"embeddings_implementation": "llama_cpp", "inference_implementation": "llama_cpp"}
_GOLDEN_GATE = {"embeddings_implementation": "coreai", "inference_implementation": "apple_foundation_models"}
_APPLE_PLUGINS = frozenset({"coreai_embeddings_plugin", "macos_inference_plugin"})
_DECLARED_MACOS27 = {"coreai-core": "Core AI runtime, macOS 27 floor (rul_5cc2910c, iss_c6abab0d)"}
_WHEEL_TAG = re.compile(r"macosx_(\d+)_\d+_")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_CHECKS: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL: {label}: {detail}")
    _CHECKS.append(label)


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _macos27_only() -> dict[str, str]:
    """Every macOS 27-only distribution this tree could install, by normalized name."""
    found = {_normalized(name): reason for name, reason in _DECLARED_MACOS27.items()}
    for wheel in sorted(_REPO.glob("plugins/*/vendor/*.whl")):
        tags = [int(major) for major in _WHEEL_TAG.findall(wheel.name)]
        if tags and min(tags) >= 27:
            found[_normalized(wheel.name.split("-", 1)[0])] = f"vendored {wheel.relative_to(_REPO)}"
    return found


def _closure(plugins: list[str]) -> dict[str, str]:
    """Distribution name -> the rostered plugin that brings it (declared dependencies and vendored wheels)."""
    reached: dict[str, str] = {}
    for plugin in plugins:
        root = _REPO / "plugins" / plugin
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
        for requirement in project.get("dependencies", []):
            match = _REQUIREMENT_NAME.match(requirement)
            if match is not None:
                reached.setdefault(_normalized(match.group(1)), plugin)
        for wheel in sorted((root / "vendor").glob("*.whl")):
            reached.setdefault(_normalized(wheel.name.split("-", 1)[0]), plugin)
    return reached


def main() -> int:
    tahoe = load_plugin_allowlist(_TEMPLATE, _TAHOE)
    golden_gate = load_plugin_allowlist(_TEMPLATE, _GOLDEN_GATE)
    _check("the macOS 26 roster carries no Apple-native plugin", not _APPLE_PLUGINS & set(tahoe), sorted(_APPLE_PLUGINS & set(tahoe)))
    _check("the macOS 26 roster carries the llama.cpp consumers",
           {"openai_embeddings_plugin", "default_inference_plugin"} <= set(tahoe))
    _check("the macOS 27 roster keeps both Apple-native plugins", _APPLE_PLUGINS <= set(golden_gate))
    _check("only the implementation plugins differ", set(tahoe) ^ set(golden_gate)
           == _APPLE_PLUGINS | {"openai_embeddings_plugin", "default_inference_plugin"}, sorted(set(tahoe) ^ set(golden_gate)))

    bundle = _declared_bundle_name(_REPO)
    if bundle not in {None, "macos-bizops"}:
        print(f"package closure not applicable: this seed ships bundle {bundle!r}, not macos-bizops")
    else:
        macos27 = _macos27_only()
        _check("the detector finds the vendored Apple Foundation Models wheel", "apple-fm-sdk" in macos27, macos27)
        tahoe_reach = _closure(tahoe)
        leaked = {name: (tahoe_reach[name], reason) for name, reason in macos27.items() if name in tahoe_reach}
        _check("the macOS 26 closure reaches no macOS 27-only package", not leaked, leaked)
        golden_reach = _closure(golden_gate)
        _check("control: the macOS 27 closure reaches apple-fm-sdk through macos_inference_plugin",
               golden_reach.get("apple-fm-sdk") == "macos_inference_plugin", golden_reach.get("apple-fm-sdk"))
    print(f"tahoe_closure_no_macos27_wheel_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
