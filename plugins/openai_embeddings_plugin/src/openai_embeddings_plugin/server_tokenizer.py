"""A llama.cpp server's declared input budget, counted by the server itself (iss_3a2a74ea).

A llama.cpp embeddings server refuses an input over its context (2048 tokens
for Nomic v1.5).  Its address-book entry declares ``max_input_tokens``, and the
plugin exposes it through the platform's ``TokenBudget`` with the server's own
``/tokenize`` (special tokens included) as the counter -- the same count the
server compares before it refuses.
"""

from __future__ import annotations

from typing import cast

import httpx
from ananta.interfaces.embedding_service_interface import TokenBudget

__all__ = ["parse_max_input_tokens", "server_token_budget"]


def parse_max_input_tokens(owner: str, value: object) -> int:
    """A positive integer, or a loud refusal naming the owner's entry."""
    text = str(value).strip()
    if not text.isdigit() or int(text) < 1:
        raise RuntimeError(f"{owner}: 'max_input_tokens' must be a positive integer, got {value!r}")
    return int(text)


def server_token_budget(
    owner: str,
    base_url: str,
    max_input_tokens: int,
    *,
    timeout_seconds: int,
    headers: dict[str, str],
) -> TokenBudget:
    """``TokenBudget`` whose counter posts each input to ``<server>/tokenize``."""
    if not base_url.endswith("/v1"):
        raise RuntimeError(f"{owner}: 'max_input_tokens' needs a base_url ending in /v1 to reach /tokenize, got {base_url!r}")
    url = f"{base_url.removesuffix('/v1')}/tokenize"

    def count(text: str) -> int:
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(url, json={"content": text, "add_special": True}, headers=headers)
                response.raise_for_status()
                payload: object = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"{owner}: token count from {url} failed: {exc}") from exc
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        if not isinstance(tokens, list):
            raise RuntimeError(f"{owner}: {url} returned no token list")
        return len(cast(list[object], tokens))

    return TokenBudget(max_input_tokens, count)
