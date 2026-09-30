"""Shipped text tells the truth about the embedding default (iss_f510b5c9, iss_ba94a96d, iss_aecf8d6a).

The shipped Apple-native profiles default embeddings to ``coreai_embeddings_plugin``
(macOS 27), a macOS 26 create binds ``openai_embeddings_plugin`` to its own loopback
llama.cpp server, and LM Studio stays supported but is not recommended
(rul_a328cc24).  Two shipped surfaces used to say otherwise:

* shipped prose named ``openai_embeddings_plugin`` as the current or default embedding;
* the ``local.yaml`` profile template header claimed to be what the solet runs as and that
  every plugin in the tree is loaded.

Each detector is proven on a known-bad string before it is trusted on the tree, so a
detector that stopped matching cannot read as a clean tree.  ``--root`` points the
scan at another checkout (used to run the red leg against the pre-fix bytes).

The setup flow's ``lm_studio`` and ``default_inference_plugin`` wording is deliberately not
checked here: it moves to chg_04232fcc because macos_setup_flow.json is a digested contract
file, so a wording edit changes the setup contract's release identity.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_TEMPLATES = (
    Path("initialization/profiles/local.yaml"),
    Path("plugins/macos_midwife_plugin/knowledge_base/profile_templates/local.yaml"),
)
_PROSE_ROOTS = (Path("ananta/knowledge_bases"), Path("plugins"), Path("README.md"))
_PROSE_SUFFIXES = frozenset({".md"})

_TEMPLATE_FALSE_CLAIMS = (
    re.compile(r"This is what \w+ runs as"),
    re.compile(r"Every plugin in the tree is loaded"),
    re.compile(r"(?<!not )the one \w+ itself runs under|(?<!not )This is the profile \w+ itself runs under"),
)
_EMBEDDING_DEFAULT_CLAIMS = (
    re.compile(r"[Cc]urrent implementation\*{0,2}:\s*`openai_embeddings_plugin`"),
    re.compile(r"[Cc]urrent implementation\*{0,2}:\s*`default_inference_plugin`"),
    re.compile(r"(?:shipped|current) default[^.\n]{0,80}`openai_embeddings_plugin`"),
    re.compile(r"`openai_embeddings_plugin`[^.\n]{0,60}\b(?:is|as) the (?:shipped |current )?default"),
)
_CHECKS: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL: {label}: {detail}")
    _CHECKS.append(label)


def template_false_claims(text: str) -> list[str]:
    return [pattern.pattern for pattern in _TEMPLATE_FALSE_CLAIMS if pattern.search(text)]


def embedding_default_claims(text: str) -> list[str]:
    return [match.group(0) for pattern in _EMBEDDING_DEFAULT_CLAIMS for match in pattern.finditer(text)]


def _prose_files(root: Path) -> Iterator[Path]:
    for entry in _PROSE_ROOTS:
        base = root / entry
        if base.is_file():
            yield base
        elif base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.suffix in _PROSE_SUFFIXES and ".archive" not in path.parts and path.is_file():
                    yield path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=_REPO)
    root = parser.parse_args().root

    _check("detector: the old local.yaml header is found",
           len(template_false_claims("# This is what Foo runs as. Every plugin in the tree is loaded.")) == 2)
    _check("detector: a default claim for openai_embeddings_plugin is found",
           embedding_default_claims("**Current implementation**: `openai_embeddings_plugin` using OpenAI") != [])
    _check("control: an option description for openai_embeddings_plugin is not a default claim",
           embedding_default_claims("`openai_embeddings_plugin` remains an optional/legacy provider.") == [])

    _check("detector: a current default_inference_plugin claim is found",
           embedding_default_claims("**Current implementation**: `default_inference_plugin` using LM Studio") != [])

    for template in _TEMPLATES:
        if not (root / template).is_file():
            print(f"  SKIP    {template}: origin-only artifact, not shipped in a seed")
            continue
        text = (root / template).read_text()
        _check(f"{template} makes no false claim about what the solet runs or loads", not template_false_claims(text),
               template_false_claims(text))

    claims = {str(path.relative_to(root)): embedding_default_claims(path.read_text()) for path in _prose_files(root)}
    claims = {path: hits for path, hits in claims.items() if hits}
    _check("no shipped prose names openai_embeddings_plugin or default_inference_plugin as the current or default implementation", not claims, claims)

    print(f"embedding_default_doc_truth_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
