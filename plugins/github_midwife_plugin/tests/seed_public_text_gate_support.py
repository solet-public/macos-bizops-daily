"""Reference data and structural checks for ``seed_public_text_gate_smoke`` (iss_67472e3f).

- the r65 helpers (``setup_adapter_runtime._neutralize`` and ``existing_install_operations._neutralized`` at f3df38abc), encoded here by behaviour, and a
  corpus of label x separator x shape inputs, so the shared helper is held to "nothing the r65 helper redacted comes out raw";
- a structural comparison of the plugin's helper and its ``bootstrap_adapter`` twin: patterns, constants and function bodies, not only behaviour;
- the census of the seed sources for producers that build an envelope field without ``public_text``.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

_R65_PAIR = re.compile(r"(?i)(password|secret|token|authorization|oauth[_ -]?code|private[_ -]?key)\s*[:=]\s*\S+")
_R65_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+")
_R65_ALTERNATION = re.compile(r"(?i)(?:password|secret|token|authorization|oauth[_ -]?code|private[_ -]?key)\s*[:=]\s*\S+|bearer\s+[A-Za-z0-9._~+/-]+")
_KEG = "/Cellar/solet/"
_PASSES = 8
_LABELS = ("password", "secret", "token", "authorization", "oauth code", "oauth_code", "oauth-code", "oauthcode", "private key", "private_key", "PASSWORD", "Token")
_SEPARATORS = ("=", ":", " = ", ": ", "\t=", ": ", "=  ")
_SHAPES = (
    "{l}{s}{v}",
    "x {l}{s}{v} y",
    "bearer {l}{s}{v}",
    "Bearer {l}{s}Bearer {v}",
    "{l}{s}bearer {v}",
    "x Bearer {l}{s}{v}",
    '{l}{s}"{v}"',
    "--{l}={v} and /opt/homebrew/Cellar/solet/1/bin",
    "Authorization: Bearer {v}",
    "BEARER {l}{s}{v}",
    "{l}{s}Bearer Bearer {v}",
    "{l}{s}bearer bearer bearer {v}",
    "Bearer Bearer {v}",
    "x Bearer Bearer Bearer {v} y",
)


def _stable(text: str, step: Callable[[str], str]) -> str:
    for _ in range(_PASSES):
        cleaned = step(text)
        if cleaned == text:
            return text
        text = cleaned
    return "[withheld]"


def r65_runtime_neutralize(text: str) -> str:
    """``setup_adapter_runtime._neutralize`` at f3df38abc: the pair pattern, then the bearer pattern, then the keg path."""
    return _stable(text, lambda value: _R65_BEARER.sub("[redacted]", _R65_PAIR.sub("[redacted]", value)).replace(_KEG, "[keg path]"))


def r65_operations_neutralize(text: str) -> str:
    """``existing_install_operations._neutralized`` at f3df38abc: one alternation, then the keg path."""
    return _stable(text, lambda value: _R65_ALTERNATION.sub("[REDACTED]", value).replace(_KEG, "[keg path]"))


def bearer_first_neutralize(text: str) -> str:
    """The round-1 order (bearer pattern first), kept as the control that proves the differential can fail."""
    return _stable(text, lambda value: _R65_PAIR.sub("[REDACTED]", _R65_BEARER.sub("[REDACTED]", value)).replace(_KEG, "[keg path]"))


def differential_corpus() -> Iterator[tuple[str, str]]:
    """Every ``(text, secret)`` of label x separator x shape; the secret is unique to its text."""
    index = 0
    for label in _LABELS:
        for separator in _SEPARATORS:
            for shape in _SHAPES:
                index += 1
                secret = f"SEKRIT{index}"
                yield shape.format(l=label, s=separator, v=secret), secret


def loosened(neutralize: Callable[[str], str], corpus: list[tuple[str, str]]) -> list[str]:
    """Texts where an r65 helper hid the secret and ``neutralize`` shows it."""
    return [
        text
        for text, secret in corpus
        if secret not in r65_runtime_neutralize(text) and secret in neutralize(text)
        or secret not in r65_operations_neutralize(text) and secret in neutralize(text)
    ]


def _normalized(function: Callable[..., Any]) -> str:
    """A function's body with its name and annotations removed, so a twin with another name and looser types still compares."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    node = tree.body[0]
    if not isinstance(node, ast.FunctionDef):
        raise TypeError("not a function")
    node.name = "f"
    node.returns = None
    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        argument.annotation = None
    return ast.dump(node)


_CONSTANTS = ("_FORMULA_MARKER", "_REDACTED", "_KEG_PATH", "_EMPTY", "_WITHHELD", "_STABLE_PASSES", "_WIDE_TEXT_LIMIT", "_TAIL_SHARE")
_FUNCTIONS = (("neutralized", "neutralized"), ("_fitted", "_fitted"), ("public_text", "public_text"), ("public_value", "_public_value"))


def twin_drift(plugin: ModuleType, twin: ModuleType) -> list[str]:
    """Every way the twin's patterns, constants or function bodies differ from the plugin's."""
    drift: list[str] = []
    plugin_patterns = [(item.pattern, item.flags) for item in plugin._SECRET_SHAPED]  # noqa: SLF001
    twin_patterns = [(item.pattern, item.flags) for item in twin._SECRET_SHAPED]  # noqa: SLF001
    if plugin_patterns != twin_patterns:
        drift.append("_SECRET_SHAPED differs")
    drift += [f"{name} differs" for name in _CONSTANTS if getattr(plugin, name) != getattr(twin, name)]
    drift += [f"{names[0]} body differs" for names in _FUNCTIONS if _normalized(getattr(plugin, names[0])) != _normalized(getattr(twin, names[1]))]
    return drift


_TEXT_WRAPPERS = {"public_text", "public_value", "_public_value"}
#: Sibling keys that identify an envelope shape, and the text fields the Manager runs through ``public_string`` for it.
_SHAPES_FIELDS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (frozenset({"decision_id", "value", "label"}), ("value", "label")),
    (frozenset({"summary", "digest"}), ("status", "summary", "source", "observed", "expected")),
    (frozenset({"mutation_kind", "requires_confirmation"}), ("title", "target", "condition_or_evidence_ref")),
    (frozenset({"repair", "checkpoint_status"}), ("repair",)),
)
#: Subscript assignments are flagged only for keys no other dict in the seed uses (``status``, ``source``, ``title`` and ``target`` are everywhere).
_UNIQUE_ASSIGN_KEYS = {"summary", "repair", "condition_or_evidence_ref"}


def _wraps(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in _TEXT_WRAPPERS for child in ast.walk(node))


def _dict_offenders(node: ast.Dict, name: str) -> list[str]:
    pairs = {key.value: value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    found: list[str] = []
    for keys, fields in _SHAPES_FIELDS:
        if keys <= set(pairs):
            found += [f"{name}:{node.lineno} builds {field} without public_text" for field in fields if field in pairs and not _wraps(pairs[field])]
    return found


def _assign_offenders(node: ast.Assign, name: str) -> list[str]:
    assigns = any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value in _UNIQUE_ASSIGN_KEYS for t in node.targets)
    return [f"{name}:{node.lineno} assigns an envelope field without public_text"] if assigns and not _wraps(node.value) else []


def offenders(source: str, name: str) -> list[str]:
    """Producers in ``source`` that build an envelope text field themselves without ``public_text``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            found += _dict_offenders(node, name)
        elif isinstance(node, ast.Assign):
            found += _assign_offenders(node, name)
    return found
