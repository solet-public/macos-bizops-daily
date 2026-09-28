#!/usr/bin/env python3
"""The plugin declares a llama.cpp server's input budget through TokenBudget (iss_3a2a74ea).

A llama.cpp embeddings server refuses any input over its context (2048 tokens
for Nomic v1.5), so its address-book entry declares ``max_input_tokens`` and the
plugin returns a ``TokenBudget`` whose counter is the server's own
``/tokenize`` (special tokens included): the same interface Core AI declares,
never a parallel mechanism.  An LM Studio entry, which declares no budget,
keeps ``None``.  Offline: the server is an in-process httpx transport.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "openai_embeddings_plugin" / "src"))

import httpx  # noqa: E402
from ananta.services.embedding_service.input_budget import split_to_fit  # noqa: E402
from openai_embeddings_plugin import plugin as plugin_module  # noqa: E402
from openai_embeddings_plugin.plugin import OpenAIEmbeddingsPlugin  # noqa: E402

_CHECKS: list[str] = []
_BUDGET = 2048
_REAL_CLIENT = httpx.Client


def _check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL: {label}: {detail}")
    _CHECKS.append(label)


def _tokenize(request: httpx.Request) -> httpx.Response:
    """The server's count: one token per word plus BOS and EOS, as llama.cpp's add_special does."""
    body: dict[str, Any] = json.loads(request.content)
    if request.url.path != "/tokenize" or body.get("add_special") is not True:
        return httpx.Response(404)
    return httpx.Response(200, json={"tokens": list(range(len(str(body["content"]).split()) + 2))})


def _client(**kwargs: Any) -> httpx.Client:
    return _REAL_CLIENT(transport=httpx.MockTransport(_tokenize), **kwargs)


def _plugin(entries: list[dict[str, str]]) -> OpenAIEmbeddingsPlugin:
    plugin = OpenAIEmbeddingsPlugin()
    plugin._extract_entries_config(entries)  # pyright: ignore[reportPrivateUsage]
    plugin._validate_required_config()  # pyright: ignore[reportPrivateUsage]
    return plugin


def _entries(**extra: str) -> list[dict[str, str]]:
    fields = {"base_url": "http://127.0.0.1:18181/v1", "model": "nomic-embed-text-v1.5", **extra}
    return [{"field_type": key, "value": value} for key, value in fields.items()]


def _expect_refusal(label: str, build: Any, fragment: str) -> None:
    try:
        build()
    except RuntimeError as exc:
        _check(label, fragment in str(exc), str(exc))
    else:
        _check(label, False)


def main() -> int:
    _check("an LM Studio entry declares no budget", _plugin(_entries()).input_token_budget() is None)

    budget = _plugin(_entries(max_input_tokens=str(_BUDGET))).input_token_budget()
    _check("a llama.cpp entry declares its budget", budget is not None and budget.max_input_tokens == _BUDGET)
    assert budget is not None
    with patch.object(plugin_module.httpx, "Client", _client):
        fits = " ".join(["word"] * (_BUDGET - 2))
        _check("the count is the server's /tokenize with special tokens", budget.count(fits) == _BUDGET, budget.count(fits))
        _check("an input at the ceiling fits", budget.fits(fits))
        _check("one more token does not fit", not budget.fits(fits + " word"))
        text = " ".join(f"w{index}" for index in range(5000))
        windows = split_to_fit(text, budget.fits)
        _check("callers split a long input to the server's budget, dropping nothing",
               len(windows) > 1 and all(budget.fits(window) for window in windows)
               and " ".join(windows).split() == text.split(), len(windows))

    for value in ("0", "-5", "many"):
        _expect_refusal(f"max_input_tokens={value!r} fails loud",
                        lambda value=value: _plugin(_entries(max_input_tokens=value)), "positive integer")
    _expect_refusal("a budget needs a /v1 base URL to reach /tokenize", lambda: _plugin([
        {"field_type": "base_url", "value": "http://127.0.0.1:18181"},
        {"field_type": "model", "value": "m"}, {"field_type": "max_input_tokens", "value": "2048"}]), "/v1")

    def _down(**kwargs: Any) -> httpx.Client:
        return _REAL_CLIENT(transport=httpx.MockTransport(lambda _request: httpx.Response(503)), **kwargs)

    with patch.object(plugin_module.httpx, "Client", _down):
        _expect_refusal("a failed count raises instead of guessing", lambda: budget.count("text"), "token count")
    print(f"llama_cpp_token_budget_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
