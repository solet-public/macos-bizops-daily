"""Check the supported macOS 27 seed profiles after real config materialization.

Run from the repository root with ``SOLET_NAME=<name> .venv/bin/python3``.
Runnable proof fails until the separately owned provider sources and portable
Core AI asset-root writer are integrated. Each profile must also hold its
LaunchAgent until the pinned Core AI asset verifies; the staged asset here is
small fixture bytes under a fixture pin, never the real Nomic model.
"""

from __future__ import annotations

# ruff: noqa: E402
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

_REPO = Path(__file__).resolve().parents[3]
_MIDWIFE = _REPO / "plugins" / "github_midwife_plugin"
_KB = _MIDWIFE / "knowledge_base"
_SOURCE = _MIDWIFE / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from github_midwife_plugin import apple_setup_adapter
from github_midwife_plugin.config_materialize import materialize_profile
from github_midwife_plugin.genesis import (
    _PROFILE_TEMPLATE_BY_BUNDLE,
    GenesisError,
    _declared_bundle_name,
    run_autostart_install,
)
from github_midwife_plugin.setup_operations import coreai_autostart_deferral

_PROFILES: dict[str, str | None] = {
    "macos-free-solet": None,
    "macos-bizops": "macos_inference_plugin",
    "macos-samantha-solet": "default_inference_plugin",
}
_NEW = "coreai_embeddings_plugin"
_OLD = "openai_embeddings_plugin"
_ASSET_DECLARATION = Path(
    "plugins/coreai_embeddings_plugin/src/coreai_embeddings_plugin/assets/distribution_manifest.json"
)


class SmokeFailureError(AssertionError):
    """The shipped fresh-profile contract is not satisfied."""


def _mapping(path: Path) -> dict[str, Any]:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SmokeFailureError(f"{path}: expected a mapping")
    return raw


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise SmokeFailureError(detail)


def _check_declared(profile_name: str, inference_plugin: str | None) -> tuple[list[Any], dict[str, Any], list[Any]]:
    profile = _mapping(_KB / "profile_templates" / f"{profile_name}.yaml")
    selected = profile.get("plugins")
    _require(isinstance(selected, list), f"{profile_name}: invalid plugin roster")
    _require(selected.count(_NEW) == 1 and _OLD not in selected,
             f"{profile_name}: expected one Core AI plugin and no old embedding plugin")
    overrides = profile.get("plugin_config_overrides")
    _require(isinstance(overrides, dict),
             f"{profile_name}: invalid plugin_config_overrides")
    coreai_override = overrides.get(_NEW)
    _require(isinstance(coreai_override, dict),
             f"{profile_name}: Core AI Genesis override is missing")
    _require(coreai_override.get("asset_root") == "${SOLET_APP_HOME}/data/model-assets",
             f"{profile_name}: Core AI asset_root must be target-local")
    _require(coreai_override.get("compute_preference") == "gpu",
             f"{profile_name}: Core AI compute_preference must be gpu")
    _require(("default_inference_plugin" in selected) == (inference_plugin == "default_inference_plugin"),
             f"{profile_name}: existing inference roster changed")
    _require(("macos_inference_plugin" in selected) == (inference_plugin == "macos_inference_plugin"),
             f"{profile_name}: Apple inference roster changed")
    bindings = profile.get("service_bindings")
    _require(isinstance(bindings, dict) and bindings.get("embedding_service") == _NEW,
             f"{profile_name}: embedding_service must bind Core AI")
    _require(bindings.get("inference_service") == inference_plugin,
             f"{profile_name}: inference binding changed")
    _require(all(provider != _OLD for provider in bindings.values()),
             f"{profile_name}: an old embedding binding remains")
    actions = profile.get("starting_actions")
    _require(isinstance(actions, list), f"{profile_name}: invalid starting_actions")
    for action in actions:
        _require(isinstance(action, dict), f"{profile_name}: invalid starting action")
        process_key = action.get("process_key")
        _require(isinstance(process_key, str)
                 and process_key.startswith(("plugin::", "service_interface::")),
                 f"{profile_name}: invalid starting action process key")
        # A bound service's boot verb is a service_interface:: key: a bound
        # provider's plugin:: namespace is never registered (iss_49a3820a).
        owner = process_key.split("::")[1]
        if process_key.startswith("service_interface::"):
            owner = bindings.get(owner)
        _require(owner in selected,
                 f"{profile_name}: orphan starting action {process_key}")
    return selected, bindings, actions


def _check_written(profile_name: str, config: Path, selected: list[Any],
                   bindings: dict[str, Any], actions: list[Any]) -> None:
    manifest = _mapping(config / "manifest.yaml")
    _require(manifest.get("plugins") == selected,
             f"{profile_name}: materialized roster changed")
    _require(_json(config / "service_bindings.json") == bindings,
             f"{profile_name}: materialized bindings changed")
    _require(_json(config / "starting_action_definitions.json") == actions,
             f"{profile_name}: materialized starting actions changed")
    book = _json(config / "plugins" / "default_address_book_plugin" / "entries.json")
    _require(isinstance(book, dict) and isinstance(book.get("entries"), list),
             f"{profile_name}: address-book seed is invalid")
    _require(all(isinstance(entry, dict) and entry.get("name") != "openai_embeddings"
                 for entry in book["entries"]),
             f"{profile_name}: old embedding endpoint remains")
    _require("http://localhost:1234/v1" not in json.dumps(book),
             f"{profile_name}: localhost embedding endpoint remains")
    _require(not (config / "plugins" / f"{_OLD}.json").exists(),
             f"{profile_name}: old embedding config remains")


def _check_inference(profile_name: str, config: Path, inference_plugin: str | None) -> list[str]:
    plugins = config / "plugins"
    existing = plugins / "default_inference_plugin.json"
    apple = plugins / "macos_inference_plugin.json"
    _require(existing.is_file() == (inference_plugin == "default_inference_plugin"),
             f"{profile_name}: existing inference config membership changed")
    if inference_plugin == "default_inference_plugin":
        _require(_json(existing) == _json(_KB / "profile_baseline" / existing.name),
                 f"{profile_name}: existing inference config changed")
    if inference_plugin != "macos_inference_plugin":
        _require(not apple.exists(), f"{profile_name}: unexpected Apple inference config")
        return []
    if not apple.is_file():
        return [f"{profile_name}: missing profile/config/plugins/{apple.name}"]
    settings = _json(apple)
    _require(settings == _json(_KB / "profile_baseline" / apple.name),
             f"{profile_name}: Apple inference baseline changed during materialization")
    _require(isinstance(settings, dict) and settings.get("model") == "apple-system"
             and settings.get("max_tokens") == 1024
             and settings.get("context.model_context_tokens") == 8192,
             f"{profile_name}: Apple summary model or limits changed")
    _require(settings.get("context.warming_enabled") is False
             and settings.get("context.auto_compact") is False
             and settings.get("context.supports_clear") is False,
             f"{profile_name}: unsupported Apple context behavior enabled")
    _require("base_url" not in settings and "api_key" not in settings,
             f"{profile_name}: legacy local server or API key setting remains")
    return []


def _check_coreai(profile_name: str, config: Path,
                  expected_asset_root: Path) -> list[str]:
    coreai_config = config / "plugins" / f"{_NEW}.json"
    if not coreai_config.is_file():
        return [f"{profile_name}: missing profile/config/plugins/{coreai_config.name} (absolute asset_root required)"]
    settings = _json(coreai_config)
    if not isinstance(settings, dict) or not isinstance(settings.get("asset_root"), str):
        return [f"{profile_name}: Core AI config lacks asset_root"]
    if not Path(settings["asset_root"]).is_absolute():
        return [f"{profile_name}: Core AI asset_root is not absolute"]
    if settings["asset_root"] != str(expected_asset_root):
        return [f"{profile_name}: Core AI asset_root is not the exact target-local root"]
    if settings.get("compute_preference") != "gpu":
        return [f"{profile_name}: Core AI compute_preference is not gpu"]
    print(
        f"{profile_name}: Genesis Core AI config PASS "
        f"asset_root={settings['asset_root']} compute_preference=gpu"
    )
    return []


class _Launchctl:
    """Record launchctl argv; answer ``list`` as not-found until ``load``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded = False

    def __call__(self, cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[1] == "list" and not self.loaded:
            return subprocess.CompletedProcess(cmd, 113, "", f'Could not find service "{cmd[2]}"')
        self.loaded = self.loaded or cmd[1] == "load"
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _stage_fixture_asset(target: Path) -> str:
    asset = target / "profile" / "data" / "model-assets" / "nomic-embed-text-v1.5"
    contents = {
        "model.aimodel/main.mlirb": b"fixture model",
        "model.aimodel/main.hash": b"fixture hash",
        "model.aimodel/metadata.json": b"{}",
        "tokenizer.json": b"{}",
        "LICENSE-2.0.txt": b"fixture license",
        "NOTICE.txt": b"fixture notice",
    }
    pins: dict[str, dict[str, str | int]] = {}
    for relative, content in contents.items():
        path = asset / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        pins[relative] = {"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    declaration = json.dumps(
        {"model_id": "nomic-ai/nomic-embed-text-v1.5", "files": pins}, sort_keys=True
    ).encode()
    source = target / _ASSET_DECLARATION
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(declaration)
    return hashlib.sha256(declaration).hexdigest()


def _install(name: str, target: Path, launchctl: _Launchctl) -> str:
    return run_autostart_install(
        name=name,
        clone_root=target,
        plist_dir=target / "LaunchAgents",
        home_dir=target / "home",
        launchctl_run=launchctl,
    ).label


def _check_autostart_gate(profile_name: str, target: Path, name: str) -> None:
    deferral = coreai_autostart_deferral(target)
    _require(deferral is not None and "absent" in deferral,
             f"{profile_name}: a Core AI roster without its asset must defer autostart")
    launchctl = _Launchctl()
    try:
        _install(name, target, launchctl)
    except GenesisError as exc:
        _require("LaunchAgent install refused" in str(exc), f"{profile_name}: wrong refusal: {exc}")
    else:
        raise SmokeFailureError(f"{profile_name}: LaunchAgent installed without the pinned asset")
    plist = target / "LaunchAgents" / f"local.solet.{name}.plist"
    _require(launchctl.calls == [] and not plist.exists(),
             f"{profile_name}: launchctl reached before the pinned asset verified")
    with patch.object(apple_setup_adapter, "_ASSET_MANIFEST_SHA256", _stage_fixture_asset(target)):
        _require(coreai_autostart_deferral(target) is None,
                 f"{profile_name}: a verified staged asset must release the LaunchAgent")
        label = _install(name, target, launchctl)
    _require(label == f"local.solet.{name}" and plist.is_file()
             and any(call[1] == "load" for call in launchctl.calls),
             f"{profile_name}: LaunchAgent did not load after the staged asset verified")
    print(f"{profile_name}: autostart held without the pinned asset; LaunchAgent loads after a verified staged asset")


def _check_profile(profile_name: str, inference_plugin: str | None) -> list[str]:
    selected, bindings, actions = _check_declared(profile_name, inference_plugin)

    with tempfile.TemporaryDirectory(prefix=f"fresh-{profile_name}-") as temporary:
        target = Path(temporary)
        name = f"fresh_{profile_name.replace('-', '_')}"
        materialize_profile(target=target, kb_root=_KB, profile_name=profile_name, name=name)
        config = target / "profile" / "config"
        # Genesis sequence 30 must write this config before models sequence 40
        # invokes the Apple setup adapter or plugin autostart reads it.
        coreai_blockers = _check_coreai(
            profile_name,
            config,
            target / "profile" / "data" / "model-assets",
        )
        _check_written(profile_name, config, selected, bindings, actions)
        _check_autostart_gate(profile_name, target, name)
        return (_check_inference(profile_name, config, inference_plugin)
                + coreai_blockers)


def _shipped_inference_providers() -> set[str]:
    """Inference providers this tree must carry.

    An origin checkout composes every profile's provider. A born seed declares
    its bundle in PROVENANCE.json and carries only that profile's provider.
    """
    bundle = _declared_bundle_name(_REPO)
    if bundle is None:
        return {provider for provider in _PROFILES.values() if provider is not None}
    provider = _PROFILES[_PROFILE_TEMPLATE_BY_BUNDLE[bundle]]
    return set() if provider is None else {provider}


def main() -> None:
    blockers: list[str] = []
    for profile_name, inference_plugin in _PROFILES.items():
        blockers.extend(_check_profile(profile_name, inference_plugin))
        print(f"{profile_name}: roster, bindings and address book checked; runnable inputs measured")

    if not (_REPO / "plugins" / _NEW / "plugin.yaml").is_file():
        blockers.append("Core AI provider source is absent from this isolated candidate; compose its frozen source")
    if "macos_inference_plugin" in _shipped_inference_providers() and not (
        _REPO / "plugins" / "macos_inference_plugin" / "plugin.yaml"
    ).is_file():
        blockers.append("Apple inference provider source is absent from this isolated candidate; compose its frozen source")
    if blockers:
        raise SmokeFailureError("fresh-profile runnable proof BLOCK: " + "; ".join(blockers))
    print("fresh-profile materialization PASS (3 supported profiles; legacy local excluded)")


if __name__ == "__main__":
    main()
