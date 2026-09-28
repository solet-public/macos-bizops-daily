#!/usr/bin/env python3
"""Standalone provider lifecycle, unsupported warming, and routing boundaries.

The legacy ``default_inference_plugin`` coexistence check is the checkout-only
companion ``legacy_plugin_coexistence_smoke.py``; no capability bundle ships both.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ananta/src'))
sys.path.insert(0, str(ROOT / 'plugins/macos_inference_plugin/src'))

from ananta.core.config.config_provider import ConfigProvider  # noqa: E402
from ananta.core.plugins.capabilities import validate_service_provider  # noqa: E402
from ananta.core.plugins.plugin_base import PluginReadiness  # noqa: E402
from ananta.interfaces import InferenceRequest, InferenceServiceUnavailableError, InferenceValidationError  # noqa: E402
from ananta.services.context_management.compaction_types import WarmingRequest  # noqa: E402
from ananta.services.inference_service.interfaces.provider import InferenceProvider  # noqa: E402
from apple_fm_provider_smoke import _FakeModel, _FakeSession, _provider, _request  # noqa: E402
from jsonschema import ValidationError  # noqa: E402
from macos_inference_plugin.configuration import default_config, validate_config  # noqa: E402
from macos_inference_plugin.plugin import DEGRADED_REASONS, Plugin  # noqa: E402
from macos_inference_plugin.providers.apple_fm_provider import AppleFMProvider  # noqa: E402


def _initialized() -> Plugin:
    plugin = Plugin()
    plugin.set_config_provider(ConfigProvider(plugin.name, default_config()))
    plugin.prepare_for_readiness()
    return plugin


def check_config() -> None:
    config = default_config()
    validate_config(config)
    for key in config:
        incomplete = dict(config)
        del incomplete[key]
        try:
            validate_config(incomplete)
        except ValidationError:
            pass
        else:
            raise AssertionError(f'missing required field accepted: {key}')
    for key, value in [('context.warming_enabled', True), ('context.auto_compact', True),
                       ('context.model_context_tokens', 32768), ('max_tokens', 4096),
                       ('timeout_seconds', 0), ('model', 'qwen')]:
        altered = dict(config)
        altered[key] = value
        try:
            validate_config(altered)
        except ValidationError:
            pass
        else:
            raise AssertionError(f'unsupported configuration accepted: {key}')
    assert _initialized().get_context_management_config().warming_enabled is False


def check_plugin() -> None:
    plugin = _initialized()
    assert isinstance(plugin, InferenceProvider)
    validate_service_provider(plugin)
    model = _FakeModel()
    plugin.provider = _provider(model)
    model.available = False
    plugin.start_post_registration_work()
    assert plugin.is_ready(), 'an ineligible device is degraded, never a roster error (iss_7f4ce644)'
    assert plugin.readiness_state is PluginReadiness.READY and plugin.get_readiness_error() is None
    assert 'DEVICE_NOT_ELIGIBLE' in (plugin.readiness_warning or '')
    warning = plugin.validate_availability()
    assert warning['error']['severity'] == 'WARNING'
    assert warning['data']['reason'] == 'DEVICE_NOT_ELIGIBLE'
    assert warning['data']['repair_instruction']
    _assert_generation_unavailable(plugin)
    model.available = True
    _check_recovery(plugin)


def _check_recovery(plugin: Plugin) -> None:
    assert plugin.is_ready() and plugin.get_readiness_error() is None
    assert plugin.readiness_warning is None
    assert plugin.generate_completion(_request('Summarize facts.'))['data']['result']['completion']


def _assert_generation_unavailable(plugin: Plugin) -> None:
    try:
        plugin.generate_completion(_request('Summarize facts.'))
    except InferenceServiceUnavailableError:
        return
    raise AssertionError('degraded plugin generated without the system model')


def _availability(reason: str) -> dict[str, object]:
    return {'action_status': 'error', 'data': {'available': False, 'reason': reason},
            'error': {'message': f'Apple Foundation Models unavailable: {reason}'}}


def check_unavailable_reasons() -> None:
    """Every reason the SDK and provider report is warn-only; a probe crash stays an error."""
    sdk_reasons = {'APPLE_INTELLIGENCE_NOT_ENABLED', 'DEVICE_NOT_ELIGIBLE', 'MODEL_NOT_READY', 'UNKNOWN'}
    assert sdk_reasons | {'SDK_UNAVAILABLE'} == DEGRADED_REASONS
    for reason in sorted(DEGRADED_REASONS):
        plugin = _initialized()
        with patch.object(AppleFMProvider, 'validate_availability', return_value=_availability(reason)):
            plugin.validate_availability()
        assert plugin.readiness_state is PluginReadiness.READY, reason
        assert reason in (plugin.readiness_warning or ''), reason
    plugin = _initialized()
    with patch.object(AppleFMProvider, 'validate_availability', return_value=_availability('PROBE_FAILED')):
        plugin.validate_availability()
        assert not plugin.is_ready()
    assert plugin.readiness_state is PluginReadiness.ERROR
    assert 'PROBE_FAILED' in (plugin.get_readiness_error() or '') and plugin.readiness_warning is None
    with patch('macos_inference_plugin.providers.apple_fm_provider.import_module',
               side_effect=RuntimeError('provider crash fixture')):
        plugin = _initialized()
        plugin.validate_availability()
        assert plugin.readiness_state is PluginReadiness.ERROR
        assert 'provider crash fixture' in (plugin.get_readiness_error() or '')


def check_unsupported_operations() -> None:
    plugin = _initialized()
    plugin.provider = _provider(_FakeModel())
    before = len(_FakeSession.calls)
    assert plugin.warm_cache(WarmingRequest('ctx', 'snap', [], 512, 0.1)) is False
    assert len(_FakeSession.calls) == before
    assert plugin.get_model_info()['data']['capabilities']['supports_cache_warming'] is False
    request = InferenceRequest('Plan actions', temperature=0.1, max_tokens=128)
    try:
        plugin.generate_completion(request)
    except InferenceValidationError:
        pass
    else:
        raise AssertionError('default action schema accepted')
    try:
        plugin.propose_name({}, {})
    except InferenceValidationError:
        pass
    else:
        raise AssertionError('unsupported non-summary operation accepted')
    assert not hasattr(plugin, 'process_results')
    assert not hasattr(plugin, 'process_error')


def check_missing_sdk() -> None:
    provider = AppleFMProvider(1)
    with patch('macos_inference_plugin.providers.apple_fm_provider.import_module',
               side_effect=ImportError('missing SDK fixture')):
        result = provider.validate_availability()
        assert result.get('data', {}).get('reason') == 'SDK_UNAVAILABLE'
        assert (result.get('error') or {}).get('severity') == 'WARNING'
        try:
            provider.generate_completion(_request('facts'))
        except InferenceServiceUnavailableError:
            pass
        else:
            raise AssertionError('missing SDK succeeded')


def main() -> int:
    check_config()
    check_missing_sdk()
    check_plugin()
    check_unavailable_reasons()
    check_unsupported_operations()
    print('plugin_contract_smoke: config, protocol, degraded-not-error, recovery, warming, routing PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
