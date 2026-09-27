#!/usr/bin/env python3
"""Explicit eligible-host prose and guided summary qualification; never a fake green."""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ananta/src'))
sys.path.insert(0, str(ROOT / 'plugins/macos_inference_plugin/src'))

from ananta.core.config.config_provider import ConfigProvider  # noqa: E402
from ananta.interfaces import InferenceRequest  # noqa: E402
from macos_inference_plugin.configuration import default_config  # noqa: E402
from macos_inference_plugin.plugin import Plugin  # noqa: E402


def main() -> int:
    plugin = Plugin()
    plugin.set_config_provider(ConfigProvider(plugin.name, default_config()))
    plugin.prepare_for_readiness()
    availability = plugin.validate_availability()
    print(json.dumps({'platform': platform.platform(), 'availability': availability}))
    if availability.get('data', {}).get('available') is not True:
        return 1
    source = ('The platform runs Apple Foundation Models for local summaries. '
              'Frontier sessions handle autonomic planning. The record nonce is macos-plugin-20260926.')
    prose = plugin.generate_completion(InferenceRequest(
        'Summarize these facts in one sentence: ' + source,
        max_tokens=256, temperature=0.0, use_structured_output=False,
    ))
    print(json.dumps({'prose': prose}))
    schema = {'type': 'object', 'required': ['leading_nonce', 'summary'],
              'properties': {'leading_nonce': {'type': 'string'}, 'summary': {'type': 'string'}},
              'additionalProperties': False}
    guided = plugin.generate_completion(InferenceRequest(
        'Summarize the source and copy its record nonce exactly to leading_nonce: ' + source,
        max_tokens=256, temperature=0.0, response_schema=schema,
    ))
    print(json.dumps({'guided': guided}))
    payload = guided.get('data', {}).get('result')
    assert isinstance(payload, dict)
    parsed = json.loads(str(payload['completion']))
    assert set(parsed) == {'leading_nonce', 'summary'}
    assert parsed['leading_nonce'] == 'macos-plugin-20260926'
    assert parsed['summary'].strip()
    print('physical_host_probe: real prose and guided summary PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
