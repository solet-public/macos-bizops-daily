"""Prompt-layer renderers for formatting raw edge results into prompt-ready content.

Each renderer_key maps to a deterministic function that takes raw result data
and returns formatted content. The response processor calls these renderers;
no formatting logic lives in the inference plugin.

Renderer functions are registered in RENDERER_REGISTRY keyed by the
renderer_key declared in process registry message_rendering contracts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

# -- Extraction helpers (recursive walkers) --


def _extract_memories_recursive(
    data: object, out: list[dict[str, str]]
) -> None:
    """Walk a nested dict/list looking for memory objects with id + content/summary."""
    if isinstance(data, dict):
        if "id" in data and ("content" in data or "summary" in data):
            out.append({k: str(v) for k, v in data.items() if isinstance(v, str)})
        for v in data.values():
            _extract_memories_recursive(v, out)
    elif isinstance(data, list):
        for item in data:
            _extract_memories_recursive(item, out)


def _is_search_result(data: dict[str, object]) -> bool:
    """Return True if *data* looks like a knowledge base search result."""
    return "title" in data or ("content" in data and "score" in data)


def _flatten_search_result(data: dict[str, object]) -> dict[str, Any]:
    """Convert a raw search result dict into a normalised flat dict.

    Scalar values are stringified; list values (e.g. article_tags) are
    preserved as-is so downstream classification can iterate them.
    """
    return {
        k: (v if isinstance(v, list) else str(v))
        for k, v in data.items()
        if isinstance(v, str | int | float | list)
    }


def _extract_search_results_recursive(
    data: object, out: list[dict[str, Any]]
) -> None:
    """Walk a nested dict/list looking for search result objects with title/kb."""
    if isinstance(data, dict):
        if _is_search_result(data):
            out.append(_flatten_search_result(data))
        for v in data.values():
            _extract_search_results_recursive(v, out)
    elif isinstance(data, list):
        for item in data:
            _extract_search_results_recursive(item, out)


# -- Knowledge base classification helpers --

_KB_CONSTRAINT_HINTS = (
    "constraint",
    "requirements",
    "hard rule",
    "hard exclusion",
    "non-goal",
    "style definition",
    "taste calibration",
    "must ask",
    "never assume",
    "process contract",
)
_KB_DIRECTION_HINTS = (
    "entrypoint",
    "work breakdown",
    "phase sequence",
    "workflow",
    "playbook",
    "blueprint",
    "prototype",
    "brief",
    "joseki",
)
_KB_IMPLEMENTATION_HINTS = (
    "recipe",
    "parameter",
    "effect",
    "fx chain",
    "track model",
    "template",
    "spec",
    "layer",
    "execution opening",
    "catalog",
    "pattern",
)

_APPROVAL_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "target duration",
            "output format",
            "hard exclusions",
            "creative audio requirements",
        ),
        "Confirm target duration, output format, general intent, and hard exclusions before committing to a build recipe.",
    ),
    (
        ("phase sequence", "entrypoint", "work breakdown", "blueprint"),
        "Approve the high-level approach and structure first; detailed blueprints, layer mappings, and execution steps come afterward.",
    ),
    (
        _KB_IMPLEMENTATION_HINTS,
        "Defer exact layer recipes, parameter ranges, and effect chains until after the direction is approved.",
    ),
)

_CATEGORY_VALUE_FIELD: dict[str, str] = {
    "constraints": "summary",
    "directions": "summary",
    "implementation": "reference",
}

# -- Metadata-driven classification (preferred over keyword heuristics) --

_ROLE_CATEGORY_MAP: dict[str, frozenset[str]] = {
    "style_definition": frozenset({"constraints"}),
    "capability_contract_reference": frozenset({"constraints"}),
    "capability_reference": frozenset({"constraints"}),
    "constraints_reference": frozenset({"constraints"}),
    "capability_audit": frozenset({"constraints"}),
    "planning_entrypoint": frozenset({"constraints", "directions"}),
    "planning_reference": frozenset({"directions"}),
    "joseki_catalog": frozenset({"implementation"}),
    "methodology": frozenset({"directions"}),
    "workflow_entrypoint": frozenset({"directions"}),
    "blueprint_reference": frozenset({"implementation"}),
    "execution_opening_catalog": frozenset({"implementation"}),
    "recipe_catalog": frozenset({"implementation"}),
    "pattern_reference": frozenset({"implementation"}),
    "composition_pattern_reference": frozenset({"implementation"}),
    "plan_pattern": frozenset({"implementation"}),
}

_EVIDENCE_CATEGORY_TO_BUCKET: dict[str, str] = {
    "constraints": "constraints",
    "style-definition": "constraints",
    "required-approvals": "constraints",
    "capability-contracts": "constraints",
    "process-contracts": "constraints",
    "capability-validation": "constraints",
    "methodology": "directions",
    "workflow": "directions",
    "approval-gates": "directions",
    "joseki": "implementation",
    "tool-translation": "implementation",
    "execution-patterns": "implementation",
    "recipes": "implementation",
    "execution-openings": "implementation",
    "execution-detail": "implementation",
    "phrase-patterns": "implementation",
}


_KB_METADATA_PREFIXES = (
    "tags:",
    "article role:",
    "article tags:",
    "source:",
    "use this document",
)


def _strip_kb_preamble(text: str) -> str:
    """Remove leading markdown headers and metadata lines from KB content."""
    lines = text.split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            body_start = i + 1
            continue
        if any(stripped.lower().startswith(p) for p in _KB_METADATA_PREFIXES):
            body_start = i + 1
            continue
        break
    return "\n".join(lines[body_start:])


def _normalize_kb_excerpt(text: str, max_len: int = 180) -> str:
    """Collapse whitespace and keep only the leading sentence-sized excerpt."""
    cleaned = re.sub(r"\s+", " ", _strip_kb_preamble(text)).strip(" -:")
    if not cleaned:
        return ""
    sentence = re.split(r"(?<!\d\.)(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
    if len(sentence) <= max_len:
        return sentence
    truncated = sentence[:max_len].rsplit(" ", 1)[0].strip()
    return f"{truncated}..." if truncated else sentence[:max_len]


def _format_kb_reference(result: dict[str, Any]) -> str:
    """Format a compact source reference for a search result."""
    kb = (result.get("kb") or result.get("knowledge_base") or result.get("name") or "").strip()
    title = (result.get("title") or "").strip()
    if kb and title:
        return f"{kb} — {title}"
    return title or kb


def _summarize_kb_result(result: dict[str, Any]) -> str | None:
    """Build a concise single-line summary from a knowledge base search result."""
    title = (result.get("title") or "").strip()
    excerpt = _normalize_kb_excerpt(result.get("content", ""))
    if title and excerpt and excerpt.lower().startswith(title.lower()):
        return excerpt
    if title and excerpt:
        return f"{title}: {excerpt}"
    return title or excerpt or None


def _categorize_from_metadata(result: dict[str, Any]) -> set[str]:
    """Derive categories from Article Role and Article Tags metadata."""
    categories: set[str] = set()
    article_role = str(result.get("article_role", ""))
    if article_role in _ROLE_CATEGORY_MAP:
        categories.update(_ROLE_CATEGORY_MAP[article_role])

    raw_tags = result.get("article_tags")
    for tag in (raw_tags if isinstance(raw_tags, list) else []):
        if isinstance(tag, str) and tag.startswith("evidence-category:"):
            bucket = _EVIDENCE_CATEGORY_TO_BUCKET.get(tag[len("evidence-category:"):])
            if bucket:
                categories.add(bucket)
    return categories


def _categorize_from_heuristics(result: dict[str, Any]) -> set[str]:
    """Derive categories by scanning title + content for keyword hints."""
    haystack = " ".join(
        part
        for part in (
            str(result.get("title", "")),
            _strip_kb_preamble(str(result.get("content", ""))),
        )
        if part
    ).lower()
    categories: set[str] = set()
    if any(hint in haystack for hint in _KB_CONSTRAINT_HINTS):
        categories.add("constraints")
    if any(hint in haystack for hint in _KB_DIRECTION_HINTS):
        categories.add("directions")
    if any(hint in haystack for hint in _KB_IMPLEMENTATION_HINTS):
        categories.add("implementation")
    return categories


def _categorize_kb_result(result: dict[str, Any]) -> set[str]:
    """Assign search findings to planning-oriented sections.

    Uses Article Role and Article Tags metadata when available;
    falls back to keyword heuristics for un-annotated content.
    """
    categories = _categorize_from_metadata(result)
    if not categories:
        categories = _categorize_from_heuristics(result)
    return categories or {"evidence"}


def _combine_result_text(results: list[dict[str, Any]]) -> str:
    """Concatenate title+content from all results into a single lowercase haystack."""
    return "\n".join(
        " ".join(
            part
            for part in (r.get("title", ""), r.get("content", ""))
            if part
        )
        for r in results
    ).lower()


def _derive_approval_notes(results: list[dict[str, Any]]) -> list[str]:
    """Infer the concrete approvals the model should seek before execution detail."""
    combined = _combine_result_text(results)
    return [
        note for keywords, note in _APPROVAL_RULES if any(kw in combined for kw in keywords)
    ][:3]


def _append_unique(target: list[str], item: str | None) -> None:
    """Append item to target only if truthy and not already present."""
    if item and item not in target:
        target.append(item)


def _deduplicate_kb_results(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate results based on reference or summary identity."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        ref = _format_kb_reference(result) or _summarize_kb_result(result) or ""
        if ref and ref not in seen:
            seen.add(ref)
            unique.append(result)
    return unique


def _scan_labeled_patterns(
    content: str,
    patterns: list[tuple[str, str]],
) -> list[str]:
    """Scan content for labeled regex patterns, return matched pairs."""
    pairs: list[str] = []
    for label, pattern in patterns:
        match = re.search(pattern, content)
        if match:
            pairs.append(f"{label} {match.group(1).strip()}")
    return pairs


def _scan_exclusions(content: str, limit: int = 6) -> list[str]:
    """Extract deduplicated 'no ...' exclusion phrases from content."""
    raw = re.findall(r"-\s*(no [^\n]+)", content, flags=re.IGNORECASE)
    seen: list[str] = []
    for exclusion in raw:
        cleaned = re.sub(r"\s+", " ", exclusion).strip().rstrip(".")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen[:limit]


def _extract_exact_binding_summaries(result: dict[str, Any]) -> dict[str, list[str]]:
    """Extract exact binding snippets from KB content for artifact handoff."""
    title = (result.get("title") or "").strip()
    content = _load_full_kb_content(result)
    if not content:
        return {"constraints": [], "directions": []}

    def _prefixed(label: str, value: str) -> str:
        if title:
            return f"{title} {label}{value}"
        return f"{label}{value}"

    summaries: dict[str, list[str]] = {"constraints": [], "directions": []}

    stage_patterns = [
        (name, rf"{re.escape(name)}\s*:\s*(\d+:\d+)")
        for name in [
            "Orientation / Settling", "Induction / Narrowing", "Deepening",
            "Core Absorptive Work", "Fractionation Pocket", "Integration",
            "Return / Reorientation",
        ]
    ]
    stage_pairs = _scan_labeled_patterns(content, stage_patterns)
    if len(stage_pairs) >= 4:
        summaries["constraints"].append(
            _prefixed("sample formal architecture: ", "; ".join(stage_pairs) + ".")
        )

    filename_pairs = _scan_labeled_patterns(content, [
        ("public M4A", r"public M4A:\s*([^\n]+)"),
        ("WAV master", r"WAV master:\s*([^\n]+)"),
        ("FLAC master", r"FLAC master:\s*([^\n]+)"),
        ("cover PNG", r"cover PNG:\s*([^\n]+)"),
        ("cover JPG", r"cover JPG:\s*([^\n]+)"),
    ])
    if filename_pairs:
        summaries["constraints"].append(
            _prefixed("derived delivery filenames: ", "; ".join(filename_pairs) + ".")
        )

    exclusions = _scan_exclusions(content)
    if exclusions:
        summaries["directions"].append(
            _prefixed("hard exclusions: ", ", ".join(exclusions) + ".")
        )

    return summaries


def _resolve_kb_file(kb_dir: Path, file_path: str) -> Path | None:
    """Resolve a knowledge base file_path to an actual disk path.

    Knowledge base tags store paths with directory separators flattened
    to underscores (e.g. ``01_workflow_17_complete_brief.md`` for
    ``01_workflow/17_complete_brief.md``).  Try the literal path first,
    then probe actual subdirectories to reconstruct the real path.
    """
    literal = kb_dir / file_path
    if literal.is_file():
        return literal
    try:
        subdirs = sorted(d.name for d in kb_dir.iterdir() if d.is_dir())
    except OSError:
        return None
    for subdir in subdirs:
        prefix = subdir + "_"
        if file_path.startswith(prefix):
            candidate = kb_dir / subdir / file_path[len(prefix):]
            if candidate.is_file():
                return candidate
    return None


def _load_full_kb_content(result: dict[str, Any]) -> str:
    """Load full KB article content when provenance is available.

    Search results are often document-representative chunks, not full files.
    Exact stage maps, filename bindings, and exclusion lists may live outside
    the representative chunk. When the KB result includes ``knowledge_base`` and
    ``file_path``, load the source markdown file directly so handoff renderers
    can preserve exact bindings from the authoritative article text.
    """
    kb_name = str(result.get("knowledge_base") or result.get("kb") or "").strip()
    file_path = str(result.get("file_path") or "").strip()
    if kb_name and file_path:
        repo_root = Path(__file__).resolve().parents[5]
        kb_dir = repo_root / "knowledge_bases" / kb_name
        resolved = _resolve_kb_file(kb_dir, file_path)
        if resolved is not None:
            try:
                return resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return str(result.get("content", ""))


def _metadata_impl_refs(results: list[dict[str, Any]]) -> frozenset[str]:
    """Return references for results whose implementation category comes from metadata."""
    refs: set[str] = set()
    for result in results:
        if _categorize_from_metadata(result) & frozenset({"implementation"}):
            ref = _format_kb_reference(result)
            if ref:
                refs.add(ref)
    return frozenset(refs)


def _prioritize_metadata(items: list[str], priority: frozenset[str]) -> list[str]:
    """Reorder items so metadata-classified entries come before heuristic-classified ones."""
    return [i for i in items if i in priority] + [i for i in items if i not in priority]


def _classify_kb_results(
    results: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], list[str]]:
    """Classify up to 12 results into planning-oriented buckets and evidence.

    Evidence is ordered so constraint/direction sources precede
    implementation-only sources, keeping stage-appropriate references
    visible within the 4-item evidence limit.
    """
    buckets: dict[str, list[str]] = {cat: [] for cat in _CATEGORY_VALUE_FIELD}
    priority_evidence: list[str] = []
    deferred_evidence: list[str] = []
    for result in results[:12]:
        values = {
            "summary": _summarize_kb_result(result),
            "reference": _format_kb_reference(result),
        }
        categories = _categorize_kb_result(result) & _CATEGORY_VALUE_FIELD.keys()
        ref = values["reference"]
        if ref:
            is_impl_only = categories == {"implementation"}
            target = deferred_evidence if is_impl_only else priority_evidence
            _append_unique(target, ref)
        for cat in categories:
            _append_unique(buckets[cat], values[_CATEGORY_VALUE_FIELD[cat]])
    # Metadata-classified implementation items take priority over heuristic matches
    buckets["implementation"] = _prioritize_metadata(
        buckets["implementation"], _metadata_impl_refs(results[:12])
    )
    evidence = priority_evidence + [
        e for e in deferred_evidence if e not in priority_evidence
    ]
    return buckets, evidence


def _append_section(
    lines: list[str], heading: str, items: list[str], limit: int
) -> None:
    """Append a headed bullet list to lines if items is non-empty."""
    if not items:
        return
    lines.append(heading)
    for item in items[:limit]:
        lines.append(f"- {item}")
    lines.append("")


# -- Renderer functions --


def render_recalled_memories(raw_result: dict[str, Any]) -> str:
    """Format recalled memories as an assistant-role evidence block.

    Output format (Section 16, recalled_memories):
        Recalled memories:

        - mem-ID1: content text...
        - mem-ID2: content text...

    Returns a "no memories found" message when the result set is empty,
    so the observation block is always created with a clear status.
    """
    memories: list[dict[str, str]] = []
    _extract_memories_recursive(raw_result, memories)
    if not memories:
        return "Recalled memories:\n\n- No episodic memories were found for this session yet."
    lines = ["Recalled memories:", ""]
    for mem in memories:
        mem_id = mem.get("id", "?")
        content = mem.get("content") or mem.get("summary", "")
        lines.append(f"- {mem_id}: {content}")
    return "\n".join(lines)


def render_knowledge_findings(raw_result: dict[str, Any]) -> str | None:
    """Format knowledge base search results as planning-oriented findings.

    Output format (Section 16, knowledge_findings):
        High-level findings relevant to choosing a direction:

        High-level constraints
        - ...

        Recommended path forward
        - ...
    """
    results: list[dict[str, Any]] = []
    _extract_search_results_recursive(raw_result, results)
    unique_results = _deduplicate_kb_results(results)
    if not unique_results:
        return None

    buckets, evidence = _classify_kb_results(unique_results)

    lines: list[str] = ["High-level findings relevant to choosing a direction:", ""]
    _append_section(lines, "High-level constraints", buckets["constraints"], 5)
    _append_section(lines, "Recommended path forward", buckets["directions"], 3)
    _append_section(
        lines,
        "Decisions requiring approval",
        _derive_approval_notes(unique_results),
        3,
    )
    _append_section(
        lines,
        "Implementation references to defer until approval",
        buckets["implementation"],
        3,
    )
    _append_section(lines, "Supporting evidence", evidence, 4)

    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) if len(lines) > 1 else None


def _collect_exact_bindings(
    results: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Collect deduplicated exact binding summaries from all results."""
    bindings: dict[str, list[str]] = {"constraints": [], "directions": []}
    for result in results:
        for bucket_name, items in _extract_exact_binding_summaries(result).items():
            for item in items:
                _append_unique(bindings[bucket_name], item)
    return bindings


def _merge_bindings_into_buckets(
    buckets: dict[str, list[str]],
    bindings: dict[str, list[str]],
    limits: dict[str, int],
) -> None:
    """Merge exact bindings into classified buckets with budget control."""
    for bucket_name, limit in limits.items():
        binding_items = bindings.get(bucket_name, [])
        if binding_items:
            summary_budget = max(limit - len(binding_items), 1)
            buckets[bucket_name] = [
                item for item in buckets[bucket_name]
                if item not in binding_items
            ][:summary_budget] + binding_items


def render_artifact_handoff(raw_result: dict[str, Any]) -> str | None:
    """Format knowledge base search results as a condensed artifact-handoff summary."""
    results: list[dict[str, Any]] = []
    _extract_search_results_recursive(raw_result, results)
    unique_results = _deduplicate_kb_results(results)
    if not unique_results:
        return None

    limits = {"constraints": 5, "directions": 5}
    buckets, _ = _classify_kb_results(unique_results)
    bindings = _collect_exact_bindings(unique_results)
    _merge_bindings_into_buckets(buckets, bindings, limits)

    lines: list[str] = ["Artifact handoff for the current step:", ""]
    _append_section(lines, "Locked decisions", buckets["constraints"], limits["constraints"])
    _append_section(
        lines,
        "Artifact requirements supported by retrieved references",
        buckets["directions"],
        limits["directions"],
    )

    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) if len(lines) > 1 else None


def render_opening_catalog(raw_result: dict[str, Any]) -> str | None:
    """Format the opening plan catalog as a prompt asset.

    The raw_result is expected to contain the pre-authored catalog document.
    """
    content = raw_result.get("content", "")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def render_raw_observation(raw_result: dict[str, Any]) -> str:
    """Default renderer for action results presented as working evidence.

    When the result contains a ``content`` field (at top level or inside
    ``data``), returns just the content text.  Otherwise renders the raw
    result as compact JSON.
    """
    content = raw_result.get("content")
    if not isinstance(content, str) or not content.strip():
        data = raw_result.get("data")
        if isinstance(data, dict):
            content = data.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return json.dumps(raw_result, separators=(",", ":"), default=str)


def render_error_summary(raw_result: dict[str, Any]) -> str:
    """Format an error summary as working evidence."""
    error = raw_result.get("error", "")
    context = raw_result.get("action_context", "")
    process_key = raw_result.get("process_key", "")

    lines: list[str] = []
    if process_key:
        lines.append(f"process: {process_key}")
    if error:
        lines.append(f"error: {error}")
    if context:
        lines.append(f"context: {context}")
    return "\n".join(lines) if lines else "Unknown error"


# -- Renderer type and registry --

type RendererFunction = Callable[..., str | None]

def render_action_label_summary(raw_result: dict[str, Any]) -> str:
    """Render a compact human-readable summary from the action label.

    Used for actions where the raw result is structural metadata (IDs,
    status) rather than content the model needs to see.  Produces a
    one-line summary from the ``action_label`` customization and key
    result fields.
    """
    label = raw_result.get("action_label", "")
    process_key = raw_result.get("process_key", "")
    data = raw_result.get("data", raw_result)
    if isinstance(data, dict):
        status = data.get("status", "")
        wbs_id = data.get("wbs_id", "")
        parts = [label] if label else [f"process: {process_key}"]
        if wbs_id:
            parts.append(f"wbs: {wbs_id}")
        if status:
            parts.append(f"status: {status}")
        return "\n".join(parts)
    return label or json.dumps(raw_result, separators=(",", ":"), default=str)


def render_artifact_result_compact(raw_result: dict[str, Any]) -> str:
    """Render a compact artifact creation result for the main inference prompt.

    Shows artifact type, ID, parent, status, knowledge base path, and source
    memory ID — but NOT the full content.  The full document is already stored
    in the knowledge base and focused memory; duplicating it in the observation
    wastes context budget.
    """
    data = raw_result.get("data", raw_result)
    if not isinstance(data, dict):
        return json.dumps(raw_result, separators=(",", ":"), default=str)

    lines: list[str] = []

    artifact_type = data.get("artifact_type", "")
    if artifact_type:
        lines.append(f"artifact_type: {artifact_type}")

    artifact_id = data.get("artifact_id", data.get("wbs_id", data.get("outline_id", "")))
    if artifact_id:
        lines.append(f"artifact_id: {artifact_id}")

    parent_id = data.get("parent_id", data.get("manifest_id", ""))
    if parent_id:
        lines.append(f"parent_id: {parent_id}")

    status = data.get("status", "")
    if status:
        lines.append(f"status: {status}")

    kb_path = data.get("knowledge_base_path", "")
    if kb_path:
        lines.append(f"knowledge_base_path: {kb_path}")

    source_mem = data.get("source_memory_id", "")
    if source_mem:
        lines.append(f"source_memory_id: {source_mem}")

    # Include section_index if available (compact summary of sections)
    section_index = data.get("section_index", "")
    if section_index:
        lines.append(f"section_index:\n{section_index}")

    return "\n".join(lines) if lines else json.dumps(data, separators=(",", ":"), default=str)


RENDERER_REGISTRY: dict[str, RendererFunction] = {
    "recalled_memories": render_recalled_memories,
    "knowledge_findings": render_knowledge_findings,
    "opening_catalog": render_opening_catalog,
    "raw_observation": render_raw_observation,
    "action_label_summary": render_action_label_summary,
    "artifact_result_compact": render_artifact_result_compact,
    "artifact_handoff": render_artifact_handoff,
    "error_summary": render_error_summary,
}


def get_renderer(renderer_key: str) -> RendererFunction:
    """Look up a renderer function by key. Fails fast if not found."""
    renderer = RENDERER_REGISTRY.get(renderer_key)
    if renderer is None:
        msg = (
            f"No renderer registered for renderer_key='{renderer_key}'. "
            f"Available: {sorted(RENDERER_REGISTRY.keys())}"
        )
        raise ValueError(msg)
    return renderer
