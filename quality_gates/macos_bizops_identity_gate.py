#!/usr/bin/env python3
"""Closed census for the canonical macOS business-operations identity.

Parseable Python uses AST ownership instead of token proximity. Targets pair
recursively with values, and starred targets consume deterministic slices.
Parenthesized, tuple, list, and set literal wrappers are transparent. Calls,
lambdas, comprehensions, and generic-key mappings remain opaque to an outer
assignment so that nested instance values do not inherit identity ownership.

Declared assignment targets, keyword arguments, and mapping keys seed one
carrier-aware descent. Nested mapping keys retain their complete path through
transparent sequence wrappers: a generic leaf ``name`` stays an instance
carrier, while ``bundle.name`` and ``provenance.bundle.name`` are declared
product fields. Subscript targets use the same complete-path rule, including
static interpolation-free f-string keys.

YAML, YML, and JSON sources are composed into structural nodes. Their scalar
source ranges bind a token to the complete nested carrier before instance or
prose markers are considered. Sequence wrappers remain transparent only for
exact values; mappings extend the carrier path instead of leaking ownership to
generic descendants. Malformed structured data falls back to the conservative
text detector, preserving the pre-existing fail-closed boundary.

Exact constants include strings, bytes, legal prefixes, and interpolation-free
f-strings, and the statically composed forms the literal census folds --
concatenation, ``str.join`` over constant sequences, percent formatting and
``str.format`` with constant operands, in text or bytes (iss_271f81af);
supersets and interpolated values do not become exact identities.

AST node spans bind each source occurrence separately. Columns are compared as
UTF-8 byte offsets because that is the coordinate system used by Python's AST.
Logical contexts stay statement-bounded, completed contexts survive a later
tokenizer error, and an unfinished suffix uses the conservative text fallback.
Regex remains a fallback only for non-Python, malformed Python, and CLI forms.

The normal repository census reads the stage-zero Git index and rejects
unmerged entries. Candidate and staged snapshots instead provide a canonical,
complete manifest; every listed path must be a readable ordinary file and the
manifest must equal the snapshot path set. This keeps acquisition integrity
separate from classification.

Former product tokens are legal only at exact historical or negative-fixture
line anchors. Bare occurrences are evaluated assignment-first, then against the
closed set of allowed live, instance, URL, dated-artifact, prose, historical,
and negative-fixture classes. Anything else remains unclassified and blocks the
gate; no marker or nearby prose can override a structurally declared carrier.

The census is intentionally noisy: every retained occurrence is printed with
its path, line, classification, and content anchor. Reviewers can therefore
reproduce an allowed result and distinguish a classifier decision from an
anchor decision instead of relying on only the aggregate zero-violation count.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import textwrap
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.candidate_tree import (  # noqa: E402
    CandidateTreeError,
    censusable_manifest_paths,
)
from quality_gates.python_literal_census import (  # noqa: E402
    literal_string_value,
    newly_resolved_pattern_matches,
)
from quality_gates.source_root import assert_running_from_source_root  # noqa: E402

CANONICAL = "macos-bizops"
CANONICAL_REPOSITORY = "https://github.com/solet-public/macos-bizops.git"
_BARE_BIZOPS = "biz" + "ops"
CLASSIFICATIONS = frozenset(
    {
        "live_contract",
        "instance_name",
        "canonical_url",
        "dated_artifact",
        "generic_business_prose",
        "historical_evidence",
        "negative_test_fixture",
        "detector_definition",
    }
)
_RETIRED = (
    "bizops" + "_standard",
    "macos-bizops" + "-solet",
    "macos" + "_bizops",
)
_TOKEN_PATTERN = re.compile(
    "|".join(re.escape(value) for value in _RETIRED)
    + r"|(?<![A-Za-z0-9_])bizops(?![A-Za-z0-9_])"
)
_DATED_REPOSITORY = re.compile(r"\d{4}-\d{2}-\d{2}_[a-z0-9_]+_bizops_[0-9a-f]{8}")
_INSTANCE_MARKERS = re.compile(
    r"solet (?:create|stop) bizops|name[\"']?\s*[:=]\s*[\"']?bizops[\"']?|"
    r"flag_name\s*=\s*[\"']?bizops[\"']?|transaction_path\([\"']bizops|"
    r"/Solets/bizops|Solets[^\n]*bizops|lifecycle-bizops|target[^\n]*bizops|"
    r"launcher[^\n]*bizops|transactions[^\n]*bizops|locks[^\n]*bizops|"
    r"lowercase name such as[^\n]*bizops|bizops session|"
    r"registry[^\n]*bizops|[\"']create[\"'][^\n]*bizops|user-created|instance name",
    re.IGNORECASE,
)
_GENERIC_MARKERS = re.compile(
    r"bizops tooling|(?:Phase 1 )?\(bizops\) scope|bizops connectors?|bizops verbs?|"
    r"business[- ]operations|business ops|business-ops",
    re.IGNORECASE,
)
_IDENTITY_KEY = (
    r"[\"']?(?:profile|profile_name|profile_template|bundle|bundle_name|"
    r"seed_profile|setup_profile|SOLET_PROFILE|(?:provenance\.)?bundle\.name|"
    r"seed(?:_lock)?\.profile)[\"']?"
)
_PYTHON_ANNOTATION = r":[ \t]*[A-Za-z_][A-Za-z0-9_., \t\[\]|]*[ \t]*="
_ASSIGNMENT_OPERATOR = rf"(?:{_PYTHON_ANNOTATION}|:=|:|=)"
_PYTHON_STRING_PREFIX = r"(?:(?:br|rb|fr|rf|r|u|b|f))?"
_EXACT_BARE_BIZOPS = (
    rf"(?:{_PYTHON_STRING_PREFIX}(?P<identity_quote>[\"'])bizops"
    r"(?P=identity_quote)|bizops)"
)
_WRAPPED_BARE_BIZOPS = (
    rf"(?:[\[(][ \t]*)*{_EXACT_BARE_BIZOPS}(?:[ \t]*[\])])*"
)
_VALUE_BOUNDARY = r"(?=$|[ \t,;:#}])"
_KEYED_IDENTITY_ASSIGNMENT = re.compile(
    rf"(?<![A-Za-z0-9_]){_IDENTITY_KEY}[ \t]*{_ASSIGNMENT_OPERATOR}"
    rf"[ \t]*{_WRAPPED_BARE_BIZOPS}{_VALUE_BOUNDARY}",
    re.IGNORECASE,
)
_CLI_IDENTITY_ASSIGNMENT = re.compile(
    rf"--(?:profile|profile-name|profile-template|bundle|bundle-name|"
    rf"seed-profile|setup-profile)(?:[ \t]+|[ \t]*=[ \t]*)"
    rf"{_WRAPPED_BARE_BIZOPS}{_VALUE_BOUNDARY}",
    re.IGNORECASE,
)
_DECLARED_IDENTITY_NAMES = frozenset(
    {"profile", "profile_name", "profile_template", "bundle", "bundle_name"}
    | {"seed_profile", "setup_profile", "solet_profile"}
)
_DECLARED_IDENTITY_PATHS = frozenset(
    {"bundle.name", "provenance.bundle.name", "seed.profile", "seed_lock.profile"}
)
_STRUCTURED_DATA_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
_TEXT_EXACT_BARE_BIZOPS = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:{_PYTHON_STRING_PREFIX}([\"']){_BARE_BIZOPS}\1|"
    rf"{_BARE_BIZOPS})"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_TEXT_IDENTITY_ASSIGNMENT = re.compile(
    rf"(?<![A-Za-z0-9_]){_IDENTITY_KEY}[^;\n]*?(?:=>|::|:=|=|:)"
    r"(?P<assigned>[^;\n]*)",
    re.IGNORECASE,
)
# Exact SHA-256 anchors over ``repo-relative path + NUL + stripped line``.
# These are audited historical occurrences, not path or file exemptions. Any
# byte/context drift removes the match, and every missing anchor is reported as
# stale. The set is populated below from the reviewed baseline census.
_HISTORICAL_LINE_ANCHORS: frozenset[str] = frozenset(
    """\
0fe06e18beedb8127ba6d391420efc60b3d6d7d4321515794ad81bcf7aa4c311
2f5dc4ff7e4f13f3bc85881d5bafa0c88b5dc722da26f9c06b44a30b845d3de3
5f7c89d015e3049b65da5af3c1e97c6ad8cc3047470c371b1db62f833df5ea31
6a4ca785209186fb5c7853a28ef36609f1b52868bdba5aecd8a97f6e2bd6cf79
a1063dc1975824cd4cf26f86ddef1a04fa482a24cde65925a432b26270407597
a28048ae66f019d345b4d4372e91d665fe54846ff0c000b5333f19d77548f16e
a9505058352a823eaad3f45ab1c561581efde9730c9f78a6c016b0fe23ab459c
e48a718b59e3943c757fa2cea945d20daeb273e3c941076b7f9607d22fbbf2a0
f8721c9a94ab0e80a1e3eef2895eaf94225638b9ccbc1cabd798b56b957635ac
""".split()
)

# 2026-09-05 (Architect verdict on unt_cf2d70d7): the last two anchors on the
# final line below are SELF-PATH references. One is the seed manifest's
# exclude_paths key, the other gate_smokes.txt's not-shipped marker, and both are
# REQUIRED to name this gate's own smoke -- whose filename carries the retired
# token, as does this file's. Neither line can avoid it: an exclusion keyed by
# path and a marker mandated to name the file both must contain it. Same class
# and same convention as the bare register line for that same smoke, already
# anchored above.
#
# NOTE the self-path exemption tuple this should have used was DEAD CODE --
# referenced only from this gate's smoke, never from the classifier -- so every
# self-path reference falls through to UNCLASSIFIED and is answered by an
# anchor here instead. The tuple was deleted 2026-09-20 (iss_9c648e1e), along
# with the two anchors that pinned its own two path-literal lines; the anchor
# convention remains the reviewed answer for self-path references.
#
# Anchors hash the EXACT stripped line content, so rewording either line stales
# them. Re-derive from this gate's own output if either changes. This comment is
# deliberately written WITHOUT the token: prose naming those paths becomes a new
# unclassified occurrence, which is how the first attempt at this note failed.
# 2026-09-05, second pass: three MORE self-path anchors on the line after those
# two, same class and same reason. They are the shipped-smoke selection
# contract's registration key for that smoke, and the two places its killing
# test names it — a docstring and a list literal. All three are registration or
# documentation references to a path, not identity carriers.
#
# WHY THEY WERE MISSED THE FIRST TIME, worth knowing: the census reads the
# stage-zero Git INDEX, so it never saw those two files while they were still
# untracked in a lane worktree. A green run over the tracked tree is NOT a green
# run over the candidate. Verify with the candidate tree, which overlays the
# working-tree scope onto the base and reproduces exactly what staging will
# show:  quality_gates/candidate_tree.py --scope-file <paths> --rename-file
# <empty> --run-identity-gate
# 2026-09-17: two self-path references from tracked debt lines under
# rul_11fcd2ec-d5ef-4504-80f9-f79c4bfab5a7, not identity carriers.
# 2026-09-20: two anchors for chg_505884b2's reproduce_release/reissue smoke
# fixtures (plugins/seed_factory_plugin/tests/reissue_smoke.py and
# reproduce_release_smoke.py) -- each an identical build_stamp(...) call whose
# bundle-name keyword argument literal is this gate's own canonical bundle
# token, minting a throwaway PROVENANCE stamp for a local git fixture, never
# a real seed. This comment is deliberately written WITHOUT the token, same
# reason as the 2026-09-05 note above. Missed on the first gate run for the
# same reason too: the census reads the stage-zero Git INDEX and these files
# were still untracked in a lane worktree; verified with the candidate tree
# per that same note.
_NEGATIVE_LINE_ANCHORS: frozenset[str] = frozenset(
    """\
04d9c55118bf4d05f56fea80e342b4a38ec7577f04fbdb962d1e120c63206fbf 0970d75540f30fb0c3465e6e81a4b523ee7246c78e8e5d881b3ed245f923aea4 0d7ffbf20386408b821d12f3500a410f2b74e47d58f2ac2637edd68e663c0116 12c4cf0ee3a19e4e0f07adeb35718a853cca46d18f0cf9d59dcf6ad1d9f9b353 1643a53c710f1d677beae38d81c6faec9353aaf46d53f03d7670de97608edcd3 17ca293d9799c97355e079e72d93d0716cdefba78fb159e54771e2b56de5ed5f 2a1ec7939dae525abcb41a3d0f742167dabbf3ae98f491f7b17e4a1bacbe0647 52a648d067f50c05fde91107280118bf24339e33c707a56c60a951cef37e4444 79dc587e25081639ab6545f53fa0e3d9e8c7d4555d610ecbf32eebefe440da8d 8269b5cc69592a373210d50aeb40e8225dd9450af36d84ee3ee90b1f232f41d0
86593bf779142bcfa03ec6f2d503301df719f77ed9061eaa083621e3978396e0 86ccfa0a3ac29b5c40d1bc8d0445eb60213c588eb4f8986611253f5527c3831f 90f0bf26be29012f388d1be2a17296b59c48a48bc398cfab2acc97269c3c108f ab4ca3064ead1f576885c5d4d522515c692d9cf06fea38f4f6cdda271cfbb23c 22be7f7479f94f8b84242a19f880d28ac4b2b089de997c8deae2bd4deebcf841 bdae51f427601a8cf43e81c43cff548c1e5b2cfade4bedbdb284f2ab55088b2d 28d82afbe2aeeea73a82b6ccab49358108a67b513c8e63654ea0fc42d75de90e 8025e9bb49b6bc47850ea9ebca5acc563f02c78459da58a8b81f2a9a78bae160
cc20d5c4de436973665467c3b529443a46cb96a794c9961578f7788a5ca41a67 e8e9f62f9cf3767ded863ddd8a1ca6c57f0808bf5cb7356dd4b1c0eb35b7abfa ea7004af20f9db0817b40f714aa1043174d3a042b831a8e7d724a64e03a3bc24 fd0e7aa4cc780b8613f365b41775171e7c2580183249766dd98ea5d5166c63a1 0cd95c9d8ae9a3ec2d770301e493d99ddaeb0773e001582f419cc75b8dc5cb8c 3e5151a4a13e812b90d1be7ed22b865f4840c39fe6b52a1eb72ed0c28b1dc34a 7442792113dd17a3abc59b8fcb5de91de8948f43118b6454ab47ccc2db879107 4e6ad84441964fa3f1b70a7155b3d5f8fb661b72d9d3cd20c7aae370ea44adc0 61e5a17a285062f14cbbc45aa05e96633b146622e1828725bf086687b8519083
903d6aa30acbddd685739aa03ef45f6afef9a1c0f964992833570c36352b19ca 9e20ff0656d72222916456cab08302c2ff020df03938f523a52c7d3ba687ac96 b268d148968998a891bbf120c756451680d57d3fc070d55a65544d80dfbc6ffd b0c3536edef73c8cf12d5a7946268240db31d6ac3d831f99c10791cb145c5ea6 d1f3d3f9eb073a52d4f20e13e66e69b2f75fe4236c37fbb8370373f6f12a6f99 2d1c0fd911701ec9c8de397ed1e76a4651ce263c0e4b0ac03fc479870572563b 9abe2b4ea7719701096c329aa7bc9eceed407765b0771b95fc3a3d76e6cf91c4 e05ebb66e6136aba414bf69122a332ea68253e4d2152cdbaf8360c680800ab41
631e6703d283972fbc82879e9e6f8624781ca7c09d425429f543edf572899909 7c63f7758db563e7b5b1c5a39f350a7381502338ee9617ae57b17033471485e8
0d36d0a0038a227558630e25113288b034adbbc327f41a20c8581169e271165e efc64c2fe6621e54f7d47d744c58ef025ae3d1a86b6f3553a0c74151f61d4e71 d440090e167e6a4ddae7699d54097e4d530c61fd720113122992e312b2fa5358
f82bf6a8d635f75b5991903365cca0f4a79de666d581ccacc44084558709a6b1 5df86ce9def78a7fe06563c59edd21e03f146376661c8598aee776e2d3d10bb4
def12426a178a7ebcdc242c422cf7c743b0ada9c8734ee8895224a02bb4c59ad 0448986b0f75d672cd8f9fb6c5af85206afbdfc94f82fd7c3052916bcc811816
""".split()
) - frozenset(
    {
        "0970d75540f30fb0c3465e6e81a4b523ee7246c78e8e5d881b3ed245f923aea4",
        "0d7ffbf20386408b821d12f3500a410f2b74e47d58f2ac2637edd68e663c0116",
        "12c4cf0ee3a19e4e0f07adeb35718a853cca46d18f0cf9d59dcf6ad1d9f9b353",
        "8025e9bb49b6bc47850ea9ebca5acc563f02c78459da58a8b81f2a9a78bae160",
        "e8e9f62f9cf3767ded863ddd8a1ca6c57f0808bf5cb7356dd4b1c0eb35b7abfa",
    }
)

# Folded-only anchors for the gate's exact pattern definitions and regression
# fixtures. Raw occurrences on these lines still take the ordinary
# classification path. Staleness is fatal so moving a definition requires an
# explicit review instead of silently widening this exclusion to whole files.
_DETECTOR_DEFINITION_LINE_ANCHORS: frozenset[str] = frozenset(
    """\
051ac5b94c894b578fd7e9c0ed780fc15ae77fa150730ecf8bc53a76dd28164b
0f965818baf201a613ca1b72b1f37000e3168d27f4497d2af5042cf0819cfdb8
129a23303aa32ee904947594fa256c3aeeed8f45af8cf7b62ba9616b53867d0c
28d82afbe2aeeea73a82b6ccab49358108a67b513c8e63654ea0fc42d75de90e
2946d6cb94cf584b11df434c5c88c07194ef69f3f3e8eaf380d24f0aafd7fb8c
2d2475ddf16d4d34a6557fffd6878915fb54b9d8a88c41e9674f3a6d2fdc9e14
3e5151a4a13e812b90d1be7ed22b865f4840c39fe6b52a1eb72ed0c28b1dc34a
62b769ab51b3dbee1d1c8bf4f5d93a7b80e26d552b73d5ebff627642dac81cdd
674d0444c23387f351415bc838bd287acb213f47811495d26b2d3b3af8b3ca9d
7e2dc25ce36c54c7bed099150fe3b977025c7369623664d5c359195c272dfa8f
96a74cec46772b40447515336d5bf90662eb9fccf3cbdaf3a3770a00e5f51923
a6b4518375c235d774aee7a90c0a14cdc1fbf80aa442a018f5e5ad31e9a21ecf
a9b7b26d503d2a121e866d37126781fda8627b401380432af0e88cacd4129737
abd46332d6e32a843c0ccd169b6b16f4391816400f3d15ba2cb9dd27f88aba55
bdf537ae86e4bcf6594de37ef1116c320a5b4d0e989f6fc757df3e200b33a889
ca1e9abb1b2f5d555a93ab58998538a3439242d9179522842521388b0954cc25
d61d6f435e9ccdff607d059cfeaed40039edba7dffeb47cf192234469a00ebad
fac38d845394d0789117836d36a5cdbb86a915ae917b3f9863f0b9cea591067d
""".split()
)

@dataclass(frozen=True)
class Occurrence:
    path: str
    line_number: int
    token: str
    line: str
    classification: str | None

    def render(self) -> str:
        label = self.classification or "UNCLASSIFIED"
        anchor = _line_anchor(self.path, self.line)
        return (
            f"{self.path}:{self.line_number}:{self.token}:{label}:"
            f"anchor={anchor}: {self.line.strip()}"
        )


@dataclass(frozen=True)
class _LogicalContext:
    source: str
    start_line: int


def _tracked_regular_path(repo_root: Path, record: str) -> str | None:
    """Decode one stage-zero regular file that still has worktree bytes."""
    try:
        metadata, relpath = record.split("\t", maxsplit=1)
        mode, _object_id, stage = metadata.split()
    except ValueError as exc:
        raise RuntimeError(f"malformed git ls-files record: {record!r}") from exc
    if stage != "0":
        raise RuntimeError(f"unmerged index entry is not a complete tree: {relpath}")
    if mode in {"100644", "100755"}:
        return relpath if (repo_root / relpath).is_file() else None
    if mode not in {"120000", "160000"}:
        raise RuntimeError(f"unsupported tracked mode {mode} for {relpath}")
    return None


def _tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Return regular index paths that are present in this candidate tree.

    The index retains a deleted path until staging; it cannot be census input
    because there are no candidate bytes to inspect. Symlinks remain excluded
    by this gate's regular-text-file contract.
    """
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {result.stderr.strip()}")
    paths: set[str] = set()
    for record in (item for item in result.stdout.split("\0") if item):
        relpath = _tracked_regular_path(repo_root, record)
        if relpath is None:
            continue
        if relpath in paths:
            raise RuntimeError(f"duplicate tracked regular path: {relpath}")
        paths.add(relpath)
    return tuple(sorted(paths))


def _in_census_scope(path: str) -> bool:
    return not path.startswith("workbench/")


def _bind_logical_context(
    contexts: list[_LogicalContext], lines: list[str], start: int, end: int
) -> None:
    source = "\n".join(lines[start - 1 : end])
    context = _LogicalContext(source=source, start_line=start)
    for line_number in range(start, end + 1):
        contexts[line_number - 1] = context


def _python_logical_contexts(lines: list[str]) -> tuple[_LogicalContext, ...]:
    contexts = [
        _LogicalContext(source=line, start_line=index)
        for index, line in enumerate(lines, start=1)
    ]
    source = "\n".join((*lines, ""))
    statement_start: int | None = None
    ignored_starts = {
        tokenize.COMMENT, tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING,
        tokenize.INDENT, tokenize.NEWLINE, tokenize.NL,
    }
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token_info in tokens:
            if statement_start is None and token_info.type not in ignored_starts:
                statement_start = token_info.start[0]
            if token_info.type != tokenize.NEWLINE or statement_start is None:
                continue
            statement_end = token_info.end[0]
            _bind_logical_context(contexts, lines, statement_start, statement_end)
            statement_start = None
    except (IndentationError, tokenize.TokenError):
        if statement_start is not None:
            _bind_logical_context(contexts, lines, statement_start, len(lines))
    return tuple(contexts)


def _logical_contexts(path: str, lines: list[str]) -> tuple[_LogicalContext, ...]:
    if Path(path).suffix == ".py":
        return _python_logical_contexts(lines)
    if Path(path).suffix.casefold() in _STRUCTURED_DATA_SUFFIXES:
        context = _LogicalContext(source="\n".join(lines), start_line=1)
        return tuple(context for _line in lines)
    return tuple(
        _LogicalContext(source=line, start_line=index)
        for index, line in enumerate(lines, start=1)
    )


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id.casefold()
    if not isinstance(node, ast.Attribute):
        return None
    owner = _dotted_name(node.value)
    return f"{owner}.{node.attr.casefold()}" if owner is not None else None


def _literal_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.casefold()
    if isinstance(node, ast.Name):
        return node.id.casefold()
    if isinstance(node, ast.JoinedStr):
        components: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            components.append(value.value)
        return "".join(components).casefold()
    return None


def _target_name(node: ast.expr) -> str | None:
    dotted = _dotted_name(node)
    if dotted is not None:
        return dotted
    if isinstance(node, ast.Subscript):
        key = _literal_key(node.slice)
        if key is None:
            return None
        owner = _target_name(node.value)
        return f"{owner}.{key}" if owner is not None else key
    return None


def _is_declared_identity_carrier(name: str) -> bool:
    return (
        name in _DECLARED_IDENTITY_NAMES
        or name in _DECLARED_IDENTITY_PATHS
        or name.rsplit(".", maxsplit=1)[-1] in _DECLARED_IDENTITY_NAMES
        or any(name.endswith(f".{path}") for path in _DECLARED_IDENTITY_PATHS)
    )


def _declared_target_path(node: ast.expr) -> tuple[str, ...] | None:
    name = _target_name(node)
    if name is None or not _is_declared_identity_carrier(name):
        return None
    return tuple(name.split("."))


def _is_exact_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id.casefold() == _BARE_BIZOPS
    if isinstance(node, ast.Constant):
        return node.value in {_BARE_BIZOPS, _BARE_BIZOPS.encode()}
    return literal_string_value(node) == _BARE_BIZOPS


def _literal_mapping_items(node: ast.Dict) -> tuple[tuple[str, ast.expr], ...]:
    items: list[tuple[str, ast.expr]] = []
    for key, value in zip(node.keys, node.values, strict=True):
        if key is not None and (key_name := _literal_key(key)) is not None:
            items.append((key_name, value))
    return tuple(items)


def _carrier_literals(
    node: ast.expr, path: tuple[str, ...]
) -> tuple[ast.expr, ...]:
    if _is_exact_literal(node):
        return (node,) if _is_declared_identity_carrier(".".join(path)) else ()
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return tuple(
            literal
            for element in node.elts
            for literal in _carrier_literals(element, path)
        )
    if not isinstance(node, ast.Dict):
        return ()
    return tuple(
        literal
        for key_name, value in _literal_mapping_items(node)
        for literal in _carrier_literals(value, (*path, key_name))
    )


def _sequence_elements(node: ast.expr) -> tuple[ast.expr, ...] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        return tuple(node.elts)
    return None


def _starred_pairs(
    targets: tuple[ast.expr, ...], values: tuple[ast.expr, ...], star_index: int
) -> tuple[tuple[ast.expr, ast.expr], ...]:
    required = len(targets) - 1
    if len(values) < required:
        return ()
    suffix_count = len(targets) - star_index - 1
    prefix = tuple(zip(targets[:star_index], values[:star_index], strict=True))
    suffix = tuple(
        zip(targets[star_index + 1 :], values[len(values) - suffix_count :], strict=True)
    )
    middle_end = len(values) - suffix_count if suffix_count else len(values)
    middle = ast.List(elts=list(values[star_index:middle_end]), ctx=ast.Load())
    starred = targets[star_index]
    assert isinstance(starred, ast.Starred)
    return (*prefix, (starred.value, middle), *suffix)


def _target_value_pairs(
    target: ast.expr, value: ast.expr
) -> tuple[tuple[ast.expr, ast.expr], ...]:
    targets = _sequence_elements(target)
    values = _sequence_elements(value)
    if targets is None:
        return ((target, value),)
    if values is None:
        return ()
    stars = [index for index, item in enumerate(targets) if isinstance(item, ast.Starred)]
    if not stars:
        return tuple(zip(targets, values, strict=True)) if len(targets) == len(values) else ()
    return _starred_pairs(targets, values, stars[0]) if len(stars) == 1 else ()


def _assigned_literals(target: ast.expr, value: ast.expr) -> tuple[ast.expr, ...]:
    pairs = _target_value_pairs(target, value)
    literals: list[ast.expr] = []
    for paired_target, paired_value in pairs:
        declared_path = _declared_target_path(paired_target)
        if declared_path is not None:
            literals.extend(_carrier_literals(paired_value, declared_path))
        elif _sequence_elements(paired_target) is not None:
            literals.extend(_assigned_literals(paired_target, paired_value))
    return tuple(literals)


def _assignment_literals(node: ast.AST) -> tuple[ast.expr, ...]:
    if isinstance(node, ast.Assign):
        return tuple(
            literal
            for target in node.targets
            for literal in _assigned_literals(target, node.value)
        )
    if isinstance(node, ast.AnnAssign):
        value = node.annotation if node.value is None else node.value
        return _assigned_literals(node.target, value)
    if isinstance(node, ast.NamedExpr):
        return _assigned_literals(node.target, node.value)
    return ()


def _keyword_literals(node: ast.Call) -> tuple[ast.expr, ...]:
    return tuple(
        literal
        for keyword in node.keywords
        if keyword.arg is not None
        and keyword.arg.casefold() in _DECLARED_IDENTITY_NAMES
        for literal in _carrier_literals(keyword.value, (keyword.arg.casefold(),))
    )


def _identity_literal_nodes(tree: ast.AST) -> tuple[ast.expr, ...]:
    return tuple(
        literal
        for node in ast.walk(tree)
        for literal in (
            _assignment_literals(node)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
            else _keyword_literals(node)
            if isinstance(node, ast.Call)
            else _carrier_literals(node, ())
            if isinstance(node, ast.Dict)
            else ()
        )
    )


def _dedent_source(source: str) -> tuple[str, tuple[int, ...]]:
    original_lines = source.splitlines()
    dedented = textwrap.dedent(source)
    dedented_lines = dedented.splitlines()
    removed = tuple(
        len(original) - len(rendered)
        for original, rendered in zip(original_lines, dedented_lines, strict=True)
    )
    return dedented, removed


def _node_contains(node: ast.expr, line_number: int, byte_column: int) -> bool:
    end_line = node.end_lineno or node.lineno
    end_column = node.end_col_offset or node.col_offset
    if not node.lineno <= line_number <= end_line:
        return False
    if line_number == node.lineno and byte_column < node.col_offset:
        return False
    return line_number != end_line or byte_column < end_column


def _structured_transparent_literals(node: Node) -> tuple[ScalarNode, ...]:
    """Return exact scalar values through sequence-only wrappers."""

    if isinstance(node, ScalarNode):
        return (node,) if node.value.casefold() == _BARE_BIZOPS else ()
    if not isinstance(node, SequenceNode):
        return ()
    return tuple(
        literal
        for element in node.value
        for literal in _structured_transparent_literals(element)
    )


def _structured_identity_literals(
    node: Node, path: tuple[str, ...] = ()
) -> tuple[ScalarNode, ...]:
    """Collect exact values owned by complete structured carrier paths.

    Mapping descent extends ownership one key at a time. Sequence descent keeps
    the current path, allowing ordinary list wrappers without granting a
    generic child key the identity semantics of an outer mapping.
    """

    literals: list[ScalarNode] = []
    if isinstance(node, MappingNode):
        for key, value in node.value:
            if not isinstance(key, ScalarNode):
                continue
            child_path = (*path, key.value.casefold())
            if _is_declared_identity_carrier(".".join(child_path)):
                literals.extend(_structured_transparent_literals(value))
            literals.extend(_structured_identity_literals(value, child_path))
    elif isinstance(node, SequenceNode):
        for value in node.value:
            literals.extend(_structured_identity_literals(value, path))
    return tuple(literals)


def _structured_node_contains(
    node: ScalarNode, line_number: int, column: int | None
) -> bool:
    """Match a source occurrence against PyYAML's character-based span."""

    start_line = node.start_mark.line + 1
    end_line = node.end_mark.line + 1
    if not start_line <= line_number <= end_line:
        return False
    if column is None:
        return True
    if line_number == start_line and column < node.start_mark.column:
        return False
    return line_number != end_line or column < node.end_mark.column


def _structured_identity_assignment(
    source: str, line_number: int, column: int | None
) -> bool | None:
    """Return structural ownership, or ``None`` when parsing is unavailable."""

    try:
        tree = yaml.compose(source)
    except yaml.YAMLError:
        return None
    if tree is None:
        return False
    return any(
        _structured_node_contains(node, line_number, column)
        for node in _structured_identity_literals(tree)
    )


def _python_identity_assignment(
    statement: str, line_number: int, column: int | None
) -> bool | None:
    source, removed = _dedent_source(statement)
    try:
        tree = ast.parse(source)
    except (IndentationError, SyntaxError, ValueError):
        return None
    literals = _identity_literal_nodes(tree)
    if column is None:
        return any(
            node.lineno <= line_number <= (node.end_lineno or node.lineno)
            for node in literals
        )
    source_line = statement.splitlines()[line_number - 1]
    byte_column = len(source_line[removed[line_number - 1] : column].encode())
    return any(_node_contains(node, line_number, byte_column) for node in literals)


def _text_identity_assignment(statement: str) -> bool:
    collapsed = " ".join(statement.splitlines())
    return bool(_CLI_IDENTITY_ASSIGNMENT.search(collapsed)) or any(
        _TEXT_EXACT_BARE_BIZOPS.search(match.group("assigned"))
        for match in _TEXT_IDENTITY_ASSIGNMENT.finditer(collapsed)
    )


def _context_line_number(line: str, context: str) -> int:
    try:
        return context.splitlines().index(line) + 1
    except ValueError:
        return 1


def _is_identity_assignment(
    path: str,
    line: str,
    logical_context: str,
    *,
    column: int | None,
    context_line: int | None,
) -> bool:
    suffix = Path(path).suffix.casefold()
    line_number = context_line or _context_line_number(line, logical_context)
    if suffix == ".py":
        structural = _python_identity_assignment(logical_context, line_number, column)
        if structural is not None:
            return structural
    elif suffix in _STRUCTURED_DATA_SUFFIXES:
        structural = _structured_identity_assignment(
            logical_context, line_number, column
        )
        if structural is not None:
            return structural
    return bool(
        _KEYED_IDENTITY_ASSIGNMENT.search(line)
        or _CLI_IDENTITY_ASSIGNMENT.search(line)
        or _text_identity_assignment(logical_context)
    )


def _line_anchor(path: str, line: str) -> str:
    payload = f"{path}\0{line.strip()}".encode()
    return hashlib.sha256(payload).hexdigest()


def _anchor_registers() -> tuple[tuple[str, frozenset[str], str], ...]:
    return (
        (
            "detector_definition",
            _DETECTOR_DEFINITION_LINE_ANCHORS,
            "detector-definition occurrence",
        ),
        ("historical_evidence", _HISTORICAL_LINE_ANCHORS, "historical occurrence"),
        (
            "negative_test_fixture",
            _NEGATIVE_LINE_ANCHORS,
            "negative-fixture occurrence",
        ),
    )


def _anchored_classification(anchor: str, *, folded: bool) -> str | None:
    registers = _anchor_registers() if folded else _anchor_registers()[1:]
    return next(
        (
            classification
            for classification, anchors, _stale_label in registers
            if anchor in anchors
        ),
        None,
    )


def _bare_bizops_classification(
    path: str,
    line: str,
    logical_context: str,
    *,
    column: int | None,
    context_line: int | None,
    resolved_value: str | None = None,
) -> str | None:
    if _is_identity_assignment(
        path,
        line,
        logical_context,
        column=column,
        context_line=context_line,
    ):
        return None
    semantic_source = line if resolved_value is None else f"{line}\n{resolved_value}"
    if (
        CANONICAL_REPOSITORY in semantic_source
        or "github.com/solet-public/macos-bizops" in semantic_source
    ):
        return "canonical_url"
    if "branch_bizops" in semantic_source or _DATED_REPOSITORY.search(semantic_source):
        return "dated_artifact"
    if _INSTANCE_MARKERS.search(semantic_source):
        return "instance_name"
    if CANONICAL in semantic_source:
        return "live_contract"
    if _GENERIC_MARKERS.search(semantic_source):
        return "generic_business_prose"
    return None


def classify_occurrence(
    path: str,
    token: str,
    line: str,
    context: str,
    *,
    column: int | None = None,
    context_line: int | None = None,
    resolved_value: str | None = None,
) -> str | None:
    """Return one closed classification, or ``None`` for a blocking hit."""

    anchor = _line_anchor(path, line)
    classification = _anchored_classification(
        anchor, folded=resolved_value is not None
    )
    if classification is not None:
        return classification
    if token in _RETIRED:
        return None
    return _bare_bizops_classification(
        path,
        line,
        context,
        column=column,
        context_line=context_line,
        resolved_value=resolved_value,
    )


def _folded_occurrences(
    relpath: str,
    source: str,
    lines: list[str],
    logical_contexts: tuple[_LogicalContext, ...],
) -> tuple[Occurrence, ...]:
    if Path(relpath).suffix != ".py":
        return ()
    occurrences: list[Occurrence] = []
    for resolved_match in newly_resolved_pattern_matches(source, _TOKEN_PATTERN):
        literal = resolved_match.literal
        line = lines[literal.line_number - 1]
        logical_context = logical_contexts[literal.line_number - 1]
        occurrences.append(
            Occurrence(
                path=relpath,
                line_number=literal.line_number,
                token=resolved_match.token,
                line=line,
                classification=classify_occurrence(
                    relpath,
                    resolved_match.token,
                    line,
                    logical_context.source,
                    column=literal.column,
                    context_line=(literal.line_number - logical_context.start_line + 1),
                    resolved_value=literal.value,
                ),
            )
        )
    return tuple(occurrences)


def scan_paths(repo_root: Path, paths: tuple[str, ...]) -> tuple[Occurrence, ...]:
    occurrences: list[Occurrence] = []
    for relpath in paths:
        path = repo_root / relpath
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(
                f"census manifest path is absent or non-regular: {relpath}"
            )
        try:
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
        except UnicodeDecodeError:  # Binary fixtures have no textual census tokens.
            continue
        except OSError as exc:
            raise RuntimeError(f"census manifest path is unreadable: {relpath}: {exc}") from exc
        logical_contexts = _logical_contexts(relpath, lines)
        for index, line in enumerate(lines):
            for match in _TOKEN_PATTERN.finditer(line):
                token = match.group(0)
                logical_context = logical_contexts[index]
                occurrences.append(
                    Occurrence(
                        path=relpath,
                        line_number=index + 1,
                        token=token,
                        line=line,
                        classification=classify_occurrence(
                            relpath,
                            token,
                            line,
                            logical_context.source,
                            column=match.start(),
                            context_line=index + 1 - logical_context.start_line + 1,
                        ),
                    )
                )
        occurrences.extend(
            _folded_occurrences(relpath, source, lines, logical_contexts)
        )
    return tuple(occurrences)


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping")
    return raw


def _valid_bundle_definition(definition: object) -> bool:
    if not isinstance(definition, dict):
        return False
    plugins = definition.get("plugins")
    return (
        definition.get("profile_template") == CANONICAL
        and isinstance(plugins, list)
        and len(plugins) == 34
        and len({str(item) for item in plugins}) == 34
        and "github_midwife_plugin" in plugins
        and "seed_factory_plugin" not in plugins
    )


def _bundle_contract_violations(factory_kb: Path) -> list[str]:
    violations: list[str] = []
    bundles = _load_yaml_mapping(factory_kb / "capability_bundles.yaml").get("bundles")
    if not isinstance(bundles, dict):
        return ["capability_bundles.yaml has no bundles mapping"]
    if not _valid_bundle_definition(bundles.get(CANONICAL)):
        violations.append("canonical bundle/template/34-plugin lockstep is invalid")
    violations.extend(
        f"retired bundle alias remains live: {retired}"
        for retired in _RETIRED
        if retired in bundles
    )
    return violations


def _profile_contract_violations(midwife_kb: Path) -> list[str]:
    violations: list[str] = []
    profile_path = midwife_kb / "profile_templates" / f"{CANONICAL}.yaml"
    retired_profile_path = midwife_kb / "profile_templates" / f"{_RETIRED[1]}.yaml"
    if not profile_path.is_file():
        violations.append("canonical profile template is absent")
    elif _load_yaml_mapping(profile_path).get("profile_name") != CANONICAL:
        violations.append("canonical profile filename stem and profile_name differ")
    if retired_profile_path.exists():
        violations.append("retired profile template path remains")
    return violations


def _flow_contract_violations(midwife_kb: Path) -> list[str]:
    violations: list[str] = []
    flow = json.loads((midwife_kb / "macos_setup_flow.json").read_text(encoding="utf-8"))
    setup_options = flow["decisions"]["setup_profile"]["option_source"]["options"]
    run_genesis = flow["operations"]["run_genesis"]
    if CANONICAL not in setup_options or any(retired in setup_options for retired in _RETIRED):
        violations.append("setup_profile options do not carry only the canonical product identity")
    if run_genesis.get("parameters", {}).get("setup_profile") != {
        "decision_ref": "setup_profile"
    }:
        violations.append("run_genesis does not project setup_profile")
    return violations


def _metadata_contract_violations(repo_root: Path) -> list[str]:
    metadata = json.loads(
        (repo_root / "solet_cli/homebrew/release_metadata.example.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        metadata.get("seed_repository") != CANONICAL_REPOSITORY
        or metadata.get("seed_profile") != CANONICAL
    ):
        return ["Homebrew metadata does not carry the canonical repository/profile pair"]
    return []


def _is_seed_clone(repo_root: Path) -> bool:
    """A seed bundle or born clone carries the factory's provenance stamp at
    its root; the origin development checkout never does. In that context the
    origin-calibrated surfaces are out of scope BY CONSTRUCTION, not by
    tolerance: the seed-factory plugin never ships (its bundle declarations
    cannot be read), and the audited line-anchor sets cover origin files a
    bundle deliberately prunes, so absence there is pruning, not staleness.
    Keying on the stamp rather than on each missing input keeps every check
    armed in the origin checkout, where the inputs are required to exist."""
    return (repo_root / "PROVENANCE.json").is_file()


def _live_contract_violations(repo_root: Path, *, seed_clone: bool = False) -> list[str]:
    factory_kb = repo_root / "plugins/seed_factory_plugin/knowledge_base"
    midwife_kb = repo_root / "plugins/github_midwife_plugin/knowledge_base"
    violations = [] if seed_clone else _bundle_contract_violations(factory_kb)
    violations.extend(_profile_contract_violations(midwife_kb))
    violations.extend(_flow_contract_violations(midwife_kb))
    violations.extend(_metadata_contract_violations(repo_root))
    return violations


def evaluate_repository(
    repo_root: Path,
    *,
    candidate_manifest: Path | None = None,
) -> tuple[tuple[Occurrence, ...], tuple[str, ...]]:
    paths = tuple(filter(
        _in_census_scope,
        (
            _tracked_paths(repo_root)
        if candidate_manifest is None
        else censusable_manifest_paths(repo_root, candidate_manifest)
        ),
    ))
    seed_clone = _is_seed_clone(repo_root)
    occurrences = scan_paths(repo_root, paths)
    violations = _live_contract_violations(repo_root, seed_clone=seed_clone)
    if not seed_clone:
        observed_anchors = {_line_anchor(item.path, item.line) for item in occurrences}
        for _classification, anchors, stale_label in _anchor_registers():
            violations.extend(
                f"stale {stale_label} anchor: {stale}"
                for stale in sorted(anchors - observed_anchors)
            )
    violations.extend(item.render() for item in occurrences if item.classification is None)
    return occurrences, tuple(violations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--candidate-manifest",
        type=Path,
        help="canonical complete manifest for an explicit candidate snapshot",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        # Self-defense half of the wrong-tree fix (iss_ec0db9c7/iss_77fe09ad):
        # even a caller that builds its subprocess command correctly cannot
        # protect this script from some OTHER caller invoking it directly
        # from a stale checkout, so it refuses unless its own on-disk copy
        # lives inside the very tree it was told to scan.
        assert_running_from_source_root(Path(__file__), repo_root)
        occurrences, violations = evaluate_repository(
            repo_root,
            candidate_manifest=(
                args.candidate_manifest.resolve()
                if args.candidate_manifest is not None
                else None
            ),
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        CandidateTreeError,
    ) as exc:
        print(f"macos_bizops_identity_gate CRASH: {exc}", file=sys.stderr)
        return 70

    retained = [item for item in occurrences if item.token in _RETIRED]
    for item in retained:
        print(item.render())
    counts = Counter(item.classification or "unclassified" for item in occurrences)
    print(
        "macos_bizops_identity_gate census: "
        f"occurrences={len(occurrences)} retained_former={len(retained)} "
        f"unclassified={len(violations)} classifications={dict(sorted(counts.items()))}"
    )
    if violations:
        for violation in violations:
            print(f"BLOCKING: {violation}", file=sys.stderr)
        return 2
    print("macos_bizops_identity_gate OK: canonical carriers valid; zero unclassified hits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
