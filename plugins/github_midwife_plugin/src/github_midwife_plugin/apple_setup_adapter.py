"""Closed Apple-native setup operations and truthful target-local probes."""

from __future__ import annotations

import hashlib
import json
import operator
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .setup_adapter_contract import AdapterRequest, JsonObject, evidence, planned_action, result
from .setup_adapter_runtime import Runtime

if TYPE_CHECKING:
    from coreai_embeddings_plugin.assets.acquisition import UrlOpener

_ASSET_ROOT = Path("profile/data/model-assets")
_ASSET_DIRECTORY = "nomic-embed-text-v1.5"
_ASSET_MANIFEST = Path(
    "plugins/coreai_embeddings_plugin/src/coreai_embeddings_plugin/assets/distribution_manifest.json"
)
_ASSET_MANIFEST_SHA256 = "8b732f8d83177b2ac18efffef0897789cdfd0fb48bcee08cfe8c98fe7bc7fd74"
_COMPUTE_PREFERENCES = ("cpu", "gpu")
_COREAI_CONFIG = Path("profile/config/plugins/coreai_embeddings_plugin.json")
_INFERENCE_CONFIG = Path("profile/config/plugins/macos_inference_plugin.json")
_INFERENCE_SOURCE = Path(
    "plugins/macos_inference_plugin/src/macos_inference_plugin/resources/default_config.json"
)
_INFERENCE_SOURCE_SHA256 = "507745a254cb526a0a6bd19b98b47bc38049086772cee6c0dde7fbd619828d68"
_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
#: Public names for the plugin transition handler, which provisions the same asset.
COREAI_ASSET_ROOT = _ASSET_ROOT
COREAI_ASSET_MANIFEST = _ASSET_MANIFEST
_VERSION = re.compile(r"^(\d+)(?:\.\d+){0,2}$")


def host_eligible(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    """Gate the Apple route on the macOS 27 Apple Silicon host contract."""
    version = runtime.run(("/usr/bin/sw_vers", "-productVersion"), timeout_seconds=5)
    machine = runtime.run(("/usr/bin/uname", "-m"), timeout_seconds=5)
    version_text = version.stdout.strip()
    if (
        not version.ok
        or version.stdout_truncated
        or _VERSION.fullmatch(version_text) is None
        or int(version_text.split(".", 1)[0]) < 27
        or not machine.ok
        or machine.stdout_truncated
        or machine.stdout.strip() != "arm64"
    ):
        return _blocked(
            request,
            "apple_host_ineligible",
            "Use macOS 27 or later on Apple Silicon, then re-preview the Apple-native choice.",
        )
    return _verified(request, "apple_host", "macOS 27 or later Apple Silicon host observed", version_text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pinned_asset_error(target: Path) -> str | None:
    """Return why the target's pinned Core AI asset is unverified, or None."""
    return _pinned_asset_error(target)


def _pinned_asset_error(target: Path) -> str | None:
    """Verify the installed bytes against the source-pinned distribution declaration.

    Acquisition is the separate ``acquire_coreai_asset`` operation. This check
    is read only, refuses absent or corrupt material, and never treats a model
    directory as proof.
    """
    files, manifest_error = _asset_file_pins(target / _ASSET_MANIFEST)
    if manifest_error is not None:
        return manifest_error
    assert files is not None
    asset = target / _ASSET_ROOT / _ASSET_DIRECTORY
    layout_error = _asset_layout_error(asset)
    if layout_error is not None:
        return layout_error
    for relative, pin in files.items():
        file_error = _asset_file_error(asset, relative, pin)
        if file_error is not None:
            return file_error
    return None


def _asset_file_pins(source: Path) -> tuple[dict[str, object] | None, str | None]:
    if source.is_symlink() or not source.is_file():
        return None, "source-pinned Core AI distribution declaration is absent"
    try:
        if _sha256(source) != _ASSET_MANIFEST_SHA256:
            return None, "Core AI distribution declaration differs from the reviewed pin"
        raw: object = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "Core AI distribution declaration is unreadable"
    if not isinstance(raw, dict) or raw.get("model_id") != _MODEL_ID:
        return None, "Core AI distribution declaration has the wrong model identity"
    files = raw.get("files")
    if not isinstance(files, dict) or not all(isinstance(key, str) for key in files):
        return None, "Core AI distribution declaration has no file pins"
    return files, None


def _asset_layout_error(asset: Path) -> str | None:
    root = asset.parent
    model = asset / "model.aimodel"
    if any(path.is_symlink() for path in (root, asset, model)):
        return "Core AI asset path is a symbolic link"
    if not asset.is_dir() or not model.is_dir():
        return "source-pinned Core AI model asset is absent"
    try:
        if {path.name for path in asset.iterdir()} != {
            "model.aimodel", "tokenizer.json", "LICENSE-2.0.txt", "NOTICE.txt"
        } or {path.name for path in model.iterdir()} != {
            "main.mlirb", "main.hash", "metadata.json"
        }:
            return "Core AI asset has unexpected or missing members"
    except OSError:
        return "Core AI asset members are unreadable"
    return None


def _asset_file_error(asset: Path, relative: str, pin: object) -> str | None:
    if not isinstance(pin, dict):
        return "Core AI distribution file pin is malformed"
    candidate = _relative_asset_path(relative)
    if candidate is None:
        return "Core AI distribution file path is invalid"
    path = asset / candidate
    expected_size = pin.get("size_bytes")
    expected_digest = pin.get("sha256")
    if (
        path.is_symlink()
        or not path.is_file()
        or type(expected_size) is not int
        or not isinstance(expected_digest, str)
    ):
        return f"Core AI asset file is absent or invalid: {relative}"
    try:
        if path.stat().st_size != expected_size or _sha256(path) != expected_digest:
            return f"Core AI asset file differs from its pin: {relative}"
    except OSError:
        return f"Core AI asset file is unreadable: {relative}"
    return None


def _relative_asset_path(relative: str) -> Path | None:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        return None
    return candidate


def asset_verified(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    del runtime
    error = _pinned_asset_error(request.target)
    if error is not None:
        return _blocked(
            request,
            "coreai_asset_unavailable",
            f"{error}. Resume setup so acquire_coreai_asset downloads and verifies the pinned asset, then retry.",
        )
    return _verified(request, "coreai_asset", "installed Core AI asset matches every pin", _MODEL_ID)


def acquire_coreai_asset(
    request: AdapterRequest, runtime: Runtime, *, opener: UrlOpener | None = None
) -> JsonObject:
    """Download, verify, and activate the pinned asset; never report an unverified success.

    A valid installed asset is reused without network. An absent asset is a
    planned download in the probe phase. Every failure, including a corrupt
    existing asset that acquisition refuses to replace, is blocked.
    """
    del runtime
    if _pinned_asset_error(request.target) is None:
        return _verified(request, "coreai_asset", "installed Core AI asset matches every pin", _MODEL_ID)
    _files, manifest_error = _asset_file_pins(request.target / _ASSET_MANIFEST)
    if manifest_error is not None:
        return _blocked(request, "coreai_asset_manifest_invalid", f"{manifest_error}. Reinstall the reviewed coreai_embeddings_plugin source, then retry.")
    asset = request.target / _ASSET_ROOT / _ASSET_DIRECTORY
    if asset.exists() or asset.is_symlink():
        return _blocked(request, "coreai_asset_corrupt", f"Installed Core AI asset at {asset} fails its pins; review and remove it, then retry.")
    if request.phase == "probe":
        return result(request, status="pending", actions=[planned_action(
            action_id="apple.acquire_coreai_asset",
            title="Download and verify the pinned Core AI Nomic model asset",
            mutation_kind="host_provisioning",
            target=str(request.target / _ASSET_ROOT),
            evidence_ref="coreai_asset_missing",
        )], repair="Approve the pinned Core AI model download.")
    return _acquire(request, opener)


def _acquire(request: AdapterRequest, opener: UrlOpener | None) -> JsonObject:
    try:
        from coreai_embeddings_plugin.assets import AssetError, DistributionManifest, acquire_asset
    except ImportError:
        return _blocked(request, "coreai_plugin_missing", "Install the selected coreai_embeddings_plugin into the target environment, then retry.")
    asset_root = request.target / _ASSET_ROOT
    try:
        manifest = DistributionManifest.from_file(request.target / _ASSET_MANIFEST)
        if opener is None:
            acquire_asset(asset_root, manifest)
        else:
            acquire_asset(asset_root, manifest, opener=opener)
    except (AssetError, OSError) as exc:
        return _blocked(request, "coreai_asset_download_failed", f"Core AI asset acquisition failed ({type(exc).__name__}: {exc}); check the network and retry.")
    # acquire_asset returns only after verifying every pin, and the manager's
    # coreai_asset_verified postcondition re-verifies against the declaration.
    return result(request, status="applied", retry_safe=True)


def coreai_config_text(target: Path) -> str:
    value = {
        "asset_root": str(target / _ASSET_ROOT),
        "compute_preference": "cpu",
    }
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def apple_inference_config_text(target: Path) -> str | None:
    source = target / _INFERENCE_SOURCE
    if source.is_symlink() or not source.is_file():
        return None
    try:
        if _sha256(source) != _INFERENCE_SOURCE_SHA256:
            return None
        content = source.read_text(encoding="utf-8")
        value: object = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("model") != "apple-system":
        return None
    return content


def coreai_config_matches(current: object, desired: object) -> bool:
    """Core AI config equality, except that an installed ``gpu`` preference stays valid (iss_f3e65e52): only the seed's default moved to ``cpu``."""
    if not isinstance(current, dict) or not isinstance(desired, dict):
        return False
    return {**current, "compute_preference": None} == {**desired, "compute_preference": None} and current.get("compute_preference") in _COMPUTE_PREFERENCES


def _config_result(request: AdapterRequest, runtime: Runtime, path: Path, desired: str, accepts: Callable[[object, object], bool] = operator.eq) -> JsonObject:
    if path.is_symlink():
        return _blocked(request, "apple_config_conflict", "Refuse a symbolic-link plugin config; repair the target and re-preview.")
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return _blocked(request, "apple_config_conflict", "Existing plugin config is unreadable; repair and re-preview.")
        try:
            matches = accepts(json.loads(current), json.loads(desired))
        except json.JSONDecodeError:
            matches = False
        if not matches:
            return _blocked(request, "apple_config_conflict", "Existing plugin config differs from the pinned Apple config; review it before retrying.")
        return _verified(request, "apple_plugin_config", "pinned plugin config is present", str(path))
    if request.phase == "probe":
        return result(
            request,
            status="pending",
            actions=[planned_action(
                action_id="apple.write_plugin_config",
                title="Write the reviewed Apple plugin configuration",
                mutation_kind="config_write",
                target=str(path),
                evidence_ref="apple_plugin_config_missing",
            )],
            repair="Approve materialization of the reviewed plugin configuration.",
        )
    runtime.atomic_write(path, desired, mode=0o600)
    if path.read_text(encoding="utf-8") != desired:
        return _blocked(request, "apple_config_write_failed", "Plugin config readback differs; inspect the target before retrying.")
    return result(request, status="applied", retry_safe=True)


def configure_coreai_embeddings(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    if _pinned_asset_error(request.target) is not None:
        return asset_verified(request, runtime)
    return _config_result(request, runtime, request.target / _COREAI_CONFIG, coreai_config_text(request.target), coreai_config_matches)


def embedding_config_valid(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    if _pinned_asset_error(request.target) is not None:
        return asset_verified(request, runtime)
    path = request.target / _COREAI_CONFIG
    if not path.is_file() or path.is_symlink():
        return _blocked(request, "coreai_config_missing", "Materialize the reviewed absolute Core AI asset_root config, then retry.")
    return _config_result(request, runtime, path, coreai_config_text(request.target), coreai_config_matches)


def configure_apple_inference(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    desired = apple_inference_config_text(request.target)
    if desired is None:
        return _blocked(request, "apple_inference_source_missing", "Install the reviewed macos_inference_plugin source and config resource, then retry.")
    return _config_result(request, runtime, request.target / _INFERENCE_CONFIG, desired)


def inference_config_valid(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    desired = apple_inference_config_text(request.target)
    path = request.target / _INFERENCE_CONFIG
    if desired is None or not path.is_file() or path.is_symlink():
        return _blocked(request, "apple_inference_config_missing", "Materialize the complete pinned Apple inference config, then retry.")
    return _config_result(request, runtime, path, desired)


def model_availability(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    try:
        available, reason_name, context = _model_state()
    except Exception as exc:
        return _blocked(request, "apple_model_probe_failed", f"Install the verified Apple FM wheel and retry availability: {type(exc).__name__}.")
    blocker = _model_availability_blocker(request, runtime, available, reason_name, context)
    if blocker is not None:
        return blocker
    return _model_availability_result(request, available, reason_name)


def _model_availability_blocker(
    request: AdapterRequest, runtime: Runtime, available: bool, reason_name: str, context: int
) -> JsonObject | None:
    """Only unsupported hardware and a wrong model context block; model absence is a warning."""
    if not available and reason_name == "DEVICE_NOT_ELIGIBLE" and not _virtual_mac(runtime):
        return _blocked(request, "apple_physical_model_unavailable", "An eligible physical macOS 27 host must produce a real Apple summary; repair Apple Intelligence and retry.")
    if available and context != 8192:
        return _blocked(request, "apple_model_context_invalid", "Expected the reviewed 8192-token Apple system model context.")
    return None


# Summaries are non-essential (rul_18bd93a3, rul_73886083): every unavailable
# reason except DEVICE_NOT_ELIGIBLE on physical hardware (rul_cc1afc13) lets
# installation proceed, with a warning that names the reason and the user action.
_UNAVAILABLE_WARNINGS: dict[str, tuple[str, str]] = {
    "DEVICE_NOT_ELIGIBLE": (
        "Apple system summarization unavailable on this VM; installation may proceed with a warning",
        "Use an eligible physical macOS 27 Apple Silicon host for summaries.",
    ),
    "APPLE_INTELLIGENCE_NOT_ENABLED": (
        "Apple Intelligence is not enabled (APPLE_INTELLIGENCE_NOT_ENABLED); installation proceeds "
        "and summaries stay degraded until you enable Apple Intelligence in System Settings",
        "Enable Apple Intelligence in System Settings and accept its terms; summaries recover on their own.",
    ),
    "MODEL_NOT_READY": (
        "Apple system model still downloading (MODEL_NOT_READY); installation proceeds and "
        "summaries stay degraded until the download finishes",
        "Let the Apple Intelligence model finish downloading (System Settings shows its progress); "
        "summaries recover on their own.",
    ),
}


def _unavailable_warning(reason_name: str) -> tuple[str, str]:
    return _UNAVAILABLE_WARNINGS.get(reason_name, (
        f"Apple system model unavailable ({reason_name}); installation proceeds and summaries stay "
        "degraded until it is available",
        "Check Apple Intelligence in System Settings; summaries recover on their own once the model is available.",
    ))


def _model_availability_result(request: AdapterRequest, available: bool, reason_name: str) -> JsonObject:
    summary, repair = (
        ("Apple system model is available", None) if available else _unavailable_warning(reason_name)
    )
    return result(
        request,
        status="verified",
        evidence_items=[evidence(
            evidence_id="apple_model_availability",
            kind="readiness",
            status="warning" if not available else "passed",
            summary=summary,
            observed=reason_name,
            expected="AVAILABLE; any other reason is a warning except DEVICE_NOT_ELIGIBLE on physical hardware",
            source="apple_fm_sdk.SystemLanguageModel.is_available",
        )],
        repair=repair,
    )


def _model_state() -> tuple[bool, str, int]:
    import apple_fm_sdk  # type: ignore[import-not-found]

    model = apple_fm_sdk.SystemLanguageModel()
    available, reason = model.is_available()
    reason_name = getattr(reason, "name", str(reason)) if not available else "AVAILABLE"
    return bool(available), str(reason_name), int(model.context_size)


def _virtual_mac(runtime: Runtime) -> bool:
    host_model = runtime.run(("/usr/sbin/sysctl", "-n", "hw.model"), timeout_seconds=5)
    return (
        host_model.ok
        and not host_model.stdout_truncated
        and host_model.stdout.strip().startswith("VirtualMac")
    )


def _verified(request: AdapterRequest, key: str, summary: str, observed: str) -> JsonObject:
    return result(
        request,
        status="verified",
        evidence_items=[evidence(
            evidence_id=key,
            kind="readiness",
            status="passed",
            summary=summary,
            observed=observed,
            expected=observed,
            source=request.operation_ref,
        )],
    )


def _blocked(request: AdapterRequest, code: str, repair: str) -> JsonObject:
    return result(request, status="blocked", error_kind=code, retry_safe=False, exit_code=None, repair=repair)
