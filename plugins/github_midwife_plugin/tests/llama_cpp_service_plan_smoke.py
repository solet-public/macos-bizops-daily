"""The fresh macOS 26 llama.cpp service plan and plugin binding (iss_3a2a74ea).

Proves, offline and against the shipped contract files:

* the reviewed model registry pins each role's GGUF (revision, size, SHA-256),
  serves it on 127.0.0.1 only, and a summaries model swap is a one-line edit;
* the macos-bizops implementation table binds llama.cpp to
  ``openai_embeddings_plugin`` and ``default_inference_plugin``, agrees with the
  registry and with the flow's plugin ``enabled_when``, and resolves a roster
  with no Apple-native plugin;
* the service operation previews both host-shared login jobs, writes and loads
  them once (idempotent on re-apply), reloads a stale definition, and stops for
  an absent GUI session;
* the configuration operations point both plugins at the loopback servers,
  preserving every other address-book entry and config key;
* the first boot waits for the llama.cpp embeddings entry (iss_aec1ef16):
  genesis and its probe hold autostart until ``configure_llama_cpp_embeddings``
  has written the ``openai_embeddings`` entry, while a Core AI roster keeps its
  own asset gate unchanged and an LM Studio roster is not held.

No host command runs: launchd, ``which`` and HTTP go through a fixture runtime,
and files land in a temporary home and target.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import plistlib
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import yaml

_REPO = Path(__file__).resolve().parents[3]
_MIDWIFE = _REPO / "plugins" / "github_midwife_plugin"
_KB = _MIDWIFE / "knowledge_base"
for _path in (_MIDWIFE / "src", _REPO / "solet_cli" / "src", Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from github_midwife_plugin import genesis, setup_operations
from github_midwife_plugin import llama_cpp_setup as llama
from github_midwife_plugin.profile_implementations import implementation_table, resolve_implementations
from github_midwife_plugin.setup_adapter_contract import JsonObject
from llama_cpp_fixture_support import BOTH, SERVER, FixtureRuntime, request
from solet_manager.condition_evaluator import condition_matches

_CHECKS: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL: {label}: {detail}")
    _CHECKS.append(label)


def _status(value: JsonObject) -> str:
    return cast(str, value["checkpoint_status"])


def _check_pins(registry: llama.LlamaRegistry) -> None:
    for role in registry.roles.values():
        _check(f"{role.name} is pinned by revision, size and SHA-256",
               len(role.model.revision) == 40 and len(role.model.sha256) == 64 and role.model.size_bytes > 0)
        _check(f"{role.name} downloads from its pinned revision", f"/resolve/{role.model.revision}/" in role.model.url)


def _check_registry() -> llama.LlamaRegistry:
    registry = llama.load_registry()
    summaries, embeddings = registry.roles["summaries"], registry.roles["embeddings"]
    _check("services bind loopback only", registry.host == "127.0.0.1")
    _check_pins(registry)
    _check("summaries serve Qwen3 8B Q4_K_M from lmstudio-community", summaries.model.filename == "Qwen3-8B-Q4_K_M.gguf"
           and summaries.model.repository == "lmstudio-community/Qwen3-8B-GGUF")
    _check("embeddings serve the Nomic v1.5 GGUF LM Studio serves",
           embeddings.model.repository == "gaianet/Nomic-embed-text-v1.5-Embedding-GGUF"
           and embeddings.model.filename == "nomic-embed-text-v1.5.f16.gguf")
    _check("embeddings declare the 2048-token budget", embeddings.max_input_tokens == 2048)
    _check("summaries disable reasoning so summaries are plain content", "--reasoning" in summaries.server_args
           and summaries.server_args[summaries.server_args.index("--reasoning") + 1] == "off")
    _check("the embedding batch holds a whole 2048-token input", "--ubatch-size" in embeddings.server_args
           and embeddings.server_args[embeddings.server_args.index("--ubatch-size") + 1] == "2048")
    _check("the summaries context matches the served slot",
           summaries.plugin_config.get("context.model_context_tokens") == 8192)
    _check_one_line_swap()
    return registry


def _check_one_line_swap() -> None:
    text = llama.REGISTRY_PATH.read_text(encoding="utf-8")
    swapped = text.replace("    model: qwen3-8b-q4_k_m\n", "    model: qwen3-4b-q4_k_m\n")
    changed = [pair for pair in zip(text.splitlines(), swapped.splitlines(), strict=True) if pair[0] != pair[1]]
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "llama_cpp_models.yaml"
        path.write_text(swapped, encoding="utf-8")
        alternate = llama.load_registry(path)
    _check("a summaries model swap is a one-line edit", len(changed) == 1
           and alternate.roles["summaries"].model.alias == "qwen3-4b"
           and llama.inference_settings(alternate)["model"] == "qwen3-4b", changed)


def _check_binding(registry: llama.LlamaRegistry) -> None:
    template = cast(dict[str, Any], yaml.safe_load((_KB / "profile_templates" / "macos-bizops.yaml").read_text(encoding="utf-8")))
    flow = cast(dict[str, Any], json.loads((_KB / "macos_setup_flow.json").read_text(encoding="utf-8")))
    table = implementation_table(template)
    for role in registry.roles.values():
        _service, options = table[role.decision]
        _check(f"{role.name}: the template binds llama.cpp to {role.consumed_by}", options.get("llama_cpp") == role.consumed_by, options)
    for decision, (_service, options) in table.items():
        for option, plugin in options.items():
            _check(f"{decision}={option} enables {plugin} in the flow",
                   condition_matches(flow["plugins"][plugin]["enabled_when"], {decision: option}))
    for role in registry.roles.values():
        activates = flow["decisions"][role.decision]["option_source"]["options"]["llama_cpp"]["activates"]
        _check(f"the flow's llama.cpp {role.name} option activates {role.consumed_by} and the services",
               activates["plugin_refs"] == [role.consumed_by]
               and {"install_llama_cpp", "install_llama_cpp_services"} <= set(activates["operation_refs"]), activates)

    resolved = resolve_implementations(template, dict(BOTH))
    plugins = cast(list[str], resolved["plugins"])
    bindings = cast(dict[str, str], resolved["service_bindings"])
    _check("the resolved roster carries both llama.cpp consumers",
           {"openai_embeddings_plugin", "default_inference_plugin"} <= set(plugins), plugins)
    _check("the resolved roster carries no Apple-native plugin",
           not {"coreai_embeddings_plugin", "macos_inference_plugin"} & set(plugins), plugins)
    _check("the services bind to the llama.cpp consumers",
           bindings.get("embedding_service") == "openai_embeddings_plugin"
           and bindings.get("inference_service") == "default_inference_plugin", bindings)
    _check("the Apple config override leaves with its plugin",
           "coreai_embeddings_plugin" not in cast(dict[str, object], resolved.get("plugin_config_overrides") or {}))
    _check("the Apple template roster is unchanged by default",
           resolve_implementations(template, {})["plugins"] == template["plugins"])


def _check_rendering(registry: llama.LlamaRegistry, home: Path) -> None:
    for role in registry.roles.values():
        plist_text, helper = llama.render_service(home, registry, role, SERVER)
        plist = plistlib.loads(plist_text.encode())
        _check(f"{role.name}: host-shared label", plist["Label"] == f"local.solet.llama-server.{role.name}")
        _check(f"{role.name}: launchd retries a failed start", plist["KeepAlive"] == {"SuccessfulExit": False}
               and plist["RunAtLoad"] is True and plist["ThrottleInterval"] == 60)
        _check(f"{role.name}: serves 127.0.0.1 on its port with its alias",
               f"--host 127.0.0.1 --port {role.port} --no-webui" in helper and f"--alias {role.model.alias}" in helper)
        _check(f"{role.name}: verifies size and SHA-256 before serving",
               helper.index(role.model.sha256) < helper.index("exec ") and f"!= {role.model.size_bytes} ]" in helper)
        _check(f"{role.name}: resumes the pinned download", "--continue-at -" in helper and role.model.url in helper)
        _check(f"{role.name}: never touches LM Studio", "1234" not in helper and ".lmstudio" not in helper)


def _check_service_operation(registry: llama.LlamaRegistry, root: Path) -> None:
    home, target = root / "home", root / "target"
    runtime = FixtureRuntime(home)
    reference = "setup::llama_cpp.install_services"
    preview = llama.install_services(request(target, reference, phase="probe"), runtime)
    actions = cast(list[JsonObject], preview["planned_actions"])
    _check("preview plans both host-shared services", _status(preview) == "pending" and len(actions) == 2, preview)
    _check("preview names each pinned model", all(role.model.sha256 in str(actions) for role in registry.roles.values()))
    _check("preview mutates nothing", runtime.mutations() == [] and not (home / "Library").exists())

    runtime.serving.add(registry.roles["embeddings"].port)
    applied = llama.install_services(request(target, reference, phase="apply"), runtime)
    _check("apply installs both services", _status(applied) == "applied", applied)
    _check("apply loads both jobs in the GUI domain", runtime.loaded == {role.label for role in registry.roles.values()})
    for role in registry.roles.values():
        _check(f"{role.name}: plist 0644 and helper 0700",
               llama.plist_path(home, role).stat().st_mode & 0o777 == 0o644
               and llama.helper_path(home, role).stat().st_mode & 0o777 == 0o700)
    current = llama.services_current(request(target, "setup::llama_cpp.services_current", phase="probe"), runtime)
    _check("the postcondition verifies", _status(current) == "verified", current)

    before = len(runtime.mutations())
    again = llama.install_services(request(target, reference, phase="apply"), runtime)
    _check("re-apply is a verified no-op", _status(again) == "verified" and len(runtime.mutations()) == before, again)

    helper = llama.helper_path(home, registry.roles["summaries"])
    helper.write_text(helper.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    stale = llama.services_current(request(target, "setup::llama_cpp.services_current", phase="probe"), runtime)
    _check("a stale definition is not current", _status(stale) == "pending", stale)
    llama.install_services(request(target, reference, phase="apply"), runtime)
    _check("a stale loaded job is reloaded from the reviewed definition",
           ("/bin/launchctl", "bootout", f"gui/{os.getuid()}/local.solet.llama-server.summaries") in runtime.commands
           and "# drift" not in helper.read_text(encoding="utf-8"))

    only = FixtureRuntime(root / "only-home")
    llama.install_services(request(target, reference, phase="apply", inputs={
        "embeddings_implementation": "llama_cpp", "inference_implementation": "none"}), only)
    _check("only the selected role is installed", only.loaded == {"local.solet.llama-server.embeddings"}, only.loaded)

    headless = FixtureRuntime(root / "headless-home")
    headless.gui_session = False
    blocked = llama.install_services(request(target, reference, phase="apply"), headless)
    _check("an absent GUI session stops before any write", blocked["error_kind"] == "llama_cpp_service_gui_session_required"
           and not (root / "headless-home" / "Library").exists(), blocked)

    missing = FixtureRuntime(root / "missing-home")
    missing.server_installed = False
    _check("preview before llama.cpp is installed still plans the services",
           _status(llama.install_services(request(target, reference, phase="probe"), missing)) == "pending")
    _check("apply without llama-server blocks loud",
           llama.install_services(request(target, reference, phase="apply"), missing)["error_kind"] == "llama_cpp_server_missing")


def _check_configuration(registry: llama.LlamaRegistry, root: Path) -> None:
    target = root / "config-target"
    runtime = FixtureRuntime(root / "config-home")
    seed = target / "profile/config/plugins/default_address_book_plugin/entries.json"
    seed.parent.mkdir(parents=True)
    pgvector: JsonObject = {"name": "pgvector_service_db", "address_type": "database", "description": "db", "tags": [], "entries": []}
    seed.write_text(json.dumps({"entries": [pgvector]}), encoding="utf-8")
    inference = target / "profile/config/plugins/default_inference_plugin.json"
    inference.write_text(json.dumps({"base_url": "http://localhost:1234/v1", "model": "qwen3-14b", "temperature": 0.1}), encoding="utf-8")

    valid = llama.embedding_config_valid(request(target, "setup::llama_cpp.embedding_config_valid", phase="probe"), runtime)
    _check("the embeddings config is pending before setup writes it", _status(valid) == "pending", valid)
    applied = llama.configure_embeddings(request(target, "setup::llama_cpp.configure_embeddings", phase="apply"), runtime)
    entries = cast(list[JsonObject], json.loads(seed.read_text(encoding="utf-8"))["entries"])
    fields = {cast(str, item["field_type"]): item["value"] for item in cast(list[JsonObject], entries[-1]["entries"])}
    _check("the openai_embeddings entry points at the loopback embeddings server",
           _status(applied) == "applied" and entries[-1]["name"] == "openai_embeddings"
           and fields == {"base_url": "http://127.0.0.1:18181/v1", "model": "nomic-embed-text-v1.5", "max_input_tokens": "2048"}, fields)
    _check("other address-book entries are preserved", entries[0] == pgvector)
    _check("the embeddings config then verifies", _status(llama.embedding_config_valid(
        request(target, "setup::llama_cpp.embedding_config_valid", phase="probe"), runtime)) == "verified")

    llama.configure_inference(request(target, "setup::llama_cpp.configure_inference", phase="apply"), runtime)
    config = cast(dict[str, object], json.loads(inference.read_text(encoding="utf-8")))
    _check("default_inference_plugin points at the loopback summaries server",
           config["base_url"] == "http://127.0.0.1:18180/v1" and config["model"] == "qwen3-8b"
           and config["context.model_context_tokens"] == 8192, config)
    _check("other inference config keys are preserved", config["temperature"] == 0.1)
    _check("the inference config then verifies", _status(llama.inference_config_valid(
        request(target, "setup::llama_cpp.inference_config_valid", phase="probe"), runtime)) == "verified")
    del registry


def _hold_target(root: Path, roster: list[str]) -> Path:
    target = root / "hold-target"
    manifest = target / "profile/config/manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(yaml.safe_dump({"profile_name": "macos-bizops", "plugins": roster}), encoding="utf-8")
    seed = target / "profile/config/plugins/default_address_book_plugin/entries.json"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text(json.dumps({"entries": [{"name": "pgvector_service_db", "address_type": "database",
                                             "description": "db", "tags": [], "entries": []}]}), encoding="utf-8")
    return target


def _refuse_launchctl(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("the LaunchAgent was installed while autostart must be held")


def _genesis_autostart(target: Path, embeddings: str | None) -> tuple[str, list[dict[str, Any]]]:
    phases: list[dict[str, Any]] = []
    status, _label = genesis._run_autostart_phase(  # pyright: ignore[reportPrivateUsage]
        name="tahoe-fixture", clone_root=target, enabled=True, plist_dir=target / "agents", home_dir=target,
        launchctl_run=_refuse_launchctl, phases=phases, finalize_marker=lambda _status: None,
        embeddings_implementation=embeddings,
    )
    return status, phases


def _check_autostart_hold(root: Path) -> None:
    target = _hold_target(root, ["default_address_book_plugin", "openai_embeddings_plugin", "default_inference_plugin"])
    probe = request(target, "genesis::solet.run", phase="probe", inputs={
        **BOTH, "solet_name": "tahoe-fixture", "clone_directory": str(target),
        "setup_profile": "macos-bizops", "autostart": "enabled"})
    held = setup_operations._launchagent_deferral(probe, autostart=True, artifacts_valid=True)  # pyright: ignore[reportPrivateUsage]
    _check("the genesis probe holds autostart until the llama.cpp embeddings entry exists",
           held is not None and "openai_embeddings" in held, held)
    status, phases = _genesis_autostart(target, "llama_cpp")
    _check("genesis defers the first boot on the llama.cpp path",
           status == "deferred_llama_cpp_config_pending" and phases[-1]["reason"] == "llama_cpp_config_pending", phases)
    _check("an LM Studio roster is not held", setup_operations.autostart_deferral(target, "lm_studio") is None)
    llama.configure_embeddings(request(target, "setup::llama_cpp.configure_embeddings", phase="apply"), FixtureRuntime(root / "hold-home"))
    _check("the written entry releases the hold",
           setup_operations._launchagent_deferral(probe, autostart=True, artifacts_valid=True) is None)  # pyright: ignore[reportPrivateUsage]

    apple = _hold_target(root / "apple", ["default_address_book_plugin", "coreai_embeddings_plugin", "macos_inference_plugin"])
    expected = setup_operations.coreai_autostart_deferral(apple)
    _check("control: a Core AI roster keeps exactly its own asset gate",
           expected is not None and setup_operations.autostart_deferral(apple, "coreai") == expected
           and setup_operations.autostart_deferral(apple, None) == expected, expected)
    status, phases = _genesis_autostart(apple, "coreai")
    _check("control: genesis still reports the Core AI asset hold",
           status == "deferred_coreai_asset_pending" and phases[-1]["reason"] == "coreai_asset_pending", phases)


def main() -> int:
    registry = _check_registry()
    _check_binding(registry)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _check_rendering(registry, root / "render-home")
        _check_service_operation(registry, root)
        _check_configuration(registry, root)
        _check_autostart_hold(root)
    print(f"llama_cpp_service_plan_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
