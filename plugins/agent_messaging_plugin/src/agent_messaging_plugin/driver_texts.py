"""Knowledge-base backed operator text with strict template contracts."""
from __future__ import annotations

import json
from pathlib import Path
from string import Formatter

_ROOT = Path(__file__).resolve().parents[2] / "knowledge_base" / "driver_texts"


def _load_contract(text_key: str) -> tuple[str, set[str]]:
    path = _ROOT / f"{text_key}.json"
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"driver text {text_key!r} cannot be loaded: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"driver text {text_key!r} must be a JSON object")
    template = raw.get("template")
    declared = raw.get("placeholders")
    if not (
        isinstance(template, str)
        and isinstance(declared, list)
        and all(isinstance(x, str) for x in declared)
    ):
        raise RuntimeError(f"driver text {text_key!r} has an invalid template contract")
    referenced = {name for _, name, _, _ in Formatter().parse(template) if name is not None}
    declared_names = set(declared)
    if referenced != declared_names:
        raise RuntimeError(
            f"driver text {text_key!r} placeholders mismatch: "
            f"template={sorted(referenced)} declared={sorted(declared_names)}"
        )
    return template, declared_names


def render_driver_text(text_key: str, **values: str) -> str:
    template, declared_names = _load_contract(text_key)
    supplied = set(values)
    if supplied != declared_names:
        raise RuntimeError(
            f"driver text {text_key!r} placeholders mismatch: "
            f"supplied={sorted(supplied)} declared={sorted(declared_names)}"
        )
    return template.format(**values)
