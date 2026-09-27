#!/usr/bin/env python3
"""Portable cancellation, isolation and typed SDK error controls."""
from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'ananta/src'))
sys.path.insert(0, str(ROOT / 'plugins/macos_inference_plugin/src'))

from ananta.interfaces import (  # noqa: E402
    InferenceServiceUnavailableError,
    InferenceTimeoutError,
    InferenceValidationError,
)
from apple_fm_provider_smoke import _FakeModel, _FakeOptions, _request  # noqa: E402
from macos_inference_plugin.providers.apple_fm_provider import AppleFMProvider  # noqa: E402


class _SDKError(Exception):
    pass


class _Session:
    made: list[_Session] = []
    active = 0
    peak = 0
    cancelled = False
    delay = 0.01
    error: type[Exception] | None = None

    def __init__(self, *, instructions: str | None, model: _FakeModel) -> None:
        self.instructions = instructions
        self.calls: list[str] = []
        self.made.append(self)

    async def respond(self, prompt: str, **kwargs: Any) -> str:
        _Session.active += 1
        _Session.peak = max(_Session.peak, _Session.active)
        self.calls.append(prompt)
        try:
            await asyncio.sleep(self.delay)
            if self.error:
                raise self.error('injected SDK failure')
            return 'ok ' + prompt
        except asyncio.CancelledError:
            _Session.cancelled = True
            raise
        finally:
            _Session.active -= 1


def _sdk() -> SimpleNamespace:
    errors = {name: type(name, (_SDKError,), {}) for name in (
        'ExceededContextWindowSizeError', 'InvalidGenerationSchemaError',
        'UnsupportedGuideError', 'AssetsUnavailableError', 'RateLimitedError', 'RefusalError',
    )}
    return SimpleNamespace(
        **errors, FoundationModelsError=_SDKError, SystemLanguageModel=_FakeModel,
        LanguageModelSession=_Session, GenerationOptions=_FakeOptions,
    )


def check_isolation(provider: AppleFMProvider) -> None:
    def generate(text: str) -> object:
        return provider.generate_completion(_request(text)).get('data', {}).get('result')
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(generate, ['a', 'b', 'c', 'd']))
    assert len(results) == 4
    for text, result in zip(['a', 'b', 'c', 'd'], results, strict=True):
        assert isinstance(result, dict) and result['completion'] == 'ok ' + text
    assert _Session.peak == 1 and len(_Session.made) == 4
    assert all(len(session.calls) == 1 for session in _Session.made)


def check_timeout(provider: AppleFMProvider) -> None:
    _Session.delay = 5
    try:
        provider.generate_completion(_request('timeout'))
    except InferenceTimeoutError:
        pass
    else:
        raise AssertionError('no timeout')
    assert _Session.cancelled
    _Session.delay = 0
    assert provider.generate_completion(_request('recovered')).get('action_status') == 'completed'


def check_errors(provider: AppleFMProvider, sdk: SimpleNamespace) -> None:
    cases = (
        (sdk.RefusalError, InferenceValidationError),
        (sdk.ExceededContextWindowSizeError, InferenceValidationError),
        (sdk.InvalidGenerationSchemaError, InferenceValidationError),
        (sdk.UnsupportedGuideError, InferenceValidationError),
        (sdk.AssetsUnavailableError, InferenceServiceUnavailableError),
        (sdk.RateLimitedError, InferenceServiceUnavailableError),
    )
    for error, expected in cases:
        _Session.error = error
        try:
            provider.generate_completion(_request('error'))
        except expected:
            pass
        else:
            raise AssertionError(f'error not surfaced: {error}')
    _Session.error = None


def main() -> int:
    sdk = _sdk()
    provider = AppleFMProvider(1)
    provider._load_sdk = lambda: sdk  # type: ignore[method-assign]
    check_isolation(provider)
    check_timeout(provider)
    check_errors(provider, sdk)
    print('provider_isolation_smoke: isolated serial requests, cancellation/recovery, typed errors PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
