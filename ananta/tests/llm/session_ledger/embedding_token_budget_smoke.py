"""Every embedding caller fits the provider's declared token budget, as a class (iss_9166af93).

Core AI refuses any input over 2048 tokens; the ledger chunker windowed at
8192 characters, and ~10% of live 4-8K-char messages exceed 2048 tokens (max
measured 2629).  A deterministic counter stands in for the provider tokenizer
(``\\w+`` runs and single punctuation marks, plus 2 special tokens) so every
count here is exact and hermetic; the real Nomic tokenizer figures are
recorded as separate evidence on the issue.

Legs:

- ``split_to_fit``: covers the input whole, every window fits, prefers
  boundaries, refuses an unsplittable input and an empty one;
- a 2629-token, sub-8192-char message embeds as several chunks, each within
  2048 tokens, covering the message; the MUTATION -- the old 8192-char
  window -- sends one 2629-token chunk the provider refuses;
- the facade refuses an over-budget batch before the provider runs, loudly,
  and counts it;
- the chunking-policy backfill: an event that failed under the old window is
  embedded by the first drain under the new policy (a full sweep), a halted
  sweep repeats, and later drains are incremental again;
- an over-budget search query is refused naming both counts;
- the knowledge indexer splits an enriched chunk the provider would refuse;
- the Core AI provider declares its largest bucket and counts only when ready;
- the enumeration: every module that sends text to ``generate_embeddings``
  is in the reviewed list below, so a new caller fails here until its budget
  handling is reviewed.
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [
    str(REPO_ROOT / "ananta" / "src"),
    str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"),
    str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"),
    str(REPO_ROOT / "plugins" / "coreai_embeddings_plugin" / "src"),
]

from _stub_state_service import StubStateService  # noqa: E402
from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface, TokenBudget  # noqa: E402
from ananta.llm.session_ledger import event_embeddings  # noqa: E402
from ananta.llm.session_ledger.event_embeddings import EVENT_CHUNK_MAX_CHARS, EVENT_MAX_CHUNKS, EventEmbeddingWriter, PolicyBackfill, chunk_event_content, event_chunk_external_id  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.services.embedding_service import EmbeddingService  # noqa: E402
from ananta.services.embedding_service.input_budget import InputBudgetError, split_to_fit  # noqa: E402

_MAX = 2048
_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _count(text: str) -> int:
    return len(re.findall(r"\w+|[^\w\s]", text)) + 2


_BUDGET = TokenBudget(_MAX, _count)


def _ok(payload: dict[str, Any]) -> ActionResult:
    return ActionResult(action_status="completed", data={"result": payload}, actions=[], error=None, timestamp=datetime.now(UTC).isoformat())


@functools.cache
def _message(tokens: int) -> str:
    """Dense prose of unique short words (so every slice is unique) with exactly ``tokens`` counted tokens.

    Counted incrementally -- joining words with spaces adds no token -- so a
    40000-token message builds in linear time.
    """
    words: list[str] = []
    counted = _count("")
    while counted < tokens:
        index = len(words)
        word = _base36(index) + ("." if index % 5 == 4 else "") + ("\n\n" if index % 60 == 59 else "")
        words.append(word)
        counted += _count(word) - _count("")
    text = " ".join(words)
    while _count(text) > tokens:
        text = text[:-1].rstrip()
    return text


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    word = ""
    while True:
        value, remainder = divmod(value, 36)
        word = digits[remainder] + word
        if value == 0:
            return word  # one \w+ run whatever its first character


def _covers(text: str, windows: list[str]) -> bool:
    """Windows are contiguous slices, in order, that together cover the text."""
    position, previous = 0, -1
    for window in windows:
        start = text.rfind(window, 0, position + len(window))  # the latest placement not past the covered end
        if start == -1 or start <= previous:
            return False
        position, previous = max(position, start + len(window)), start
    return position == len(text)


class _Provider(EmbeddingServiceInterface):
    """A provider that refuses any input over the budget, like Core AI (never truncates)."""

    def __init__(self, budget: TokenBudget | None) -> None:
        self.budget = budget
        self.batches: list[list[str]] = []

    def input_token_budget(self) -> TokenBudget | None:
        return self.budget

    def generate_embeddings(self, inputs: list[str], model: str | None = None, input_type: str = "text") -> ActionResult:
        del model, input_type
        self.batches.append(list(inputs))
        if any(_count(text) > _MAX for text in inputs):
            return ActionResult(action_status="error", actions=[], timestamp="", error={"type": "Refused", "code": "coreai_embeddings.input_too_long", "message": "Input has too many tokens", "details": {}, "severity": "error", "timestamp": ""})
        return _ok({"embeddings": [[float(len(text)), 1.0, 0.0] for text in inputs], "dimension": 3, "model": "fake"})

    def get_embedding_dimension(self, model: str | None = None) -> ActionResult:
        return _ok({"dimension": 3})

    def list_models(self) -> ActionResult:
        return _ok({"models": []})

    def is_ready(self) -> bool:
        return True

    def get_readiness_error(self) -> str | None:
        return None


class _Vectors:
    def __init__(self) -> None:
        self.present: set[str] = set()
        self.stored: list[str] = []
        self.deleted: list[str] = []

    def store_vectors(self, namespace: str, vectors: list[dict[str, object]]) -> ActionResult:
        del namespace
        for record in vectors:
            self.present.add(str(record["external_id"]))
            self.stored.append(str(record["external_id"]))
        return _ok({"inserted_ids": ["x"] * len(vectors)})

    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> ActionResult:
        del namespace
        self.deleted.extend(external_ids)
        gone = self.present & set(external_ids)
        self.present -= gone
        return _ok({"deleted_count": len(gone)})

    def find_missing_external_ids(self, namespace: str, candidate_external_ids: list[str]) -> ActionResult:
        return _ok({"missing": [item for item in candidate_external_ids if item not in self.present]})

    def search_similar(self, namespace: str, query_vector: list[float], top_k: int = 10, filters: dict[str, object] | None = None, distance_metric: object = None) -> ActionResult:
        return _ok({"results": []})


class _Corpus(SessionLedgerRepository):
    """Real repository (real KV cursor, counter and policy) over a fixed candidate corpus."""

    def __init__(self, state_service: Any, corpus: list[dict[str, object]]) -> None:
        super().__init__(state_service)
        self.corpus = corpus

    def list_event_embedding_candidates(self, *, limit: int, after: tuple[object, object] | None = None, order_column: str = "event_at", ascending: bool = False) -> list[dict[str, object]]:
        rows = sorted(self.corpus, key=lambda row: (str(row[order_column]), str(row["id"])), reverse=not ascending)
        if after is not None:
            key = (str(after[0]), str(after[1]))
            rows = [row for row in rows if ((str(row[order_column]), str(row["id"])) > key) == ascending and (str(row[order_column]), str(row["id"])) != key]
        return [dict(row) for row in rows[:limit]]


def _event(row_id: str, content: str, imported_at: str) -> dict[str, object]:
    return {"id": row_id, "session_id": "les_1", "sequence": 1, "event_type": "MESSAGE", "role": "assistant", "content_text": content, "content_json": None, "event_at": imported_at, "imported_at": imported_at, "session_vendor": "codex", "source_kind": "codex_local"}


# --- legs ---------------------------------------------------------------------------------------


def test_split_to_fit() -> None:
    text = _message(5000)
    windows = split_to_fit(text, _BUDGET.fits, overlap_chars=64)
    _check(len(windows) >= 3 and all(_BUDGET.fits(window) for window in windows), "split_to_fit: every window fits the budget")
    _check(_covers(text, windows), "split_to_fit: the windows cover the input whole (no loss)")
    _check(all(window.endswith((" ", "\n", ".")) for window in windows[:-1]), "split_to_fit: cuts land on natural boundaries")
    _check(split_to_fit("short text", _BUDGET.fits) == ["short text"], "split_to_fit: a fitting input is returned unchanged")
    try:
        split_to_fit("abc", lambda window: len(window) < 1)
        _check(False, "split_to_fit: an unsplittable input refuses")
    except InputBudgetError:
        _check(True, "split_to_fit: an unsplittable input refuses loudly")
    try:
        split_to_fit("", _BUDGET.fits)
        _check(False, "split_to_fit: an empty input refuses")
    except InputBudgetError:
        _check(True, "split_to_fit: an empty input refuses loudly")
    progress = split_to_fit("x" * 50, lambda window: len(window) <= 10, overlap_chars=20)
    _check(_covers("x" * 50, progress) and len(progress) <= 50, "split_to_fit: an overlap wider than a window still advances")


def test_2629_token_message() -> None:
    message = _message(2629)
    _check(_count(message) == 2629 and len(message) <= EVENT_CHUNK_MAX_CHARS, f"the message is 2629 tokens inside one 8192-char window ({len(message)} chars)")
    old = chunk_event_content(message)
    _check(len(old) == 1 and _count(old[0]) == 2629, "MUTATION control: the old 8192-char window yields one 2629-token chunk")
    new = chunk_event_content(message, _BUDGET)
    _check(len(new) >= 2 and all(_count(chunk) <= _MAX for chunk in new), f"token-aware chunking: {len(new)} chunks, each within 2048 tokens")
    _check(_covers(message, new), "token-aware chunking covers the whole message")



def test_2629_token_message_embeds() -> None:
    message = _message(2629)
    new = chunk_event_content(message, _BUDGET)
    writer, provider, vectors = _writer(_BUDGET)
    outcome = writer.embed_event(_event("evt_long", message, "2026-09-27T01:00:00"))
    _check(outcome["chunks_stored"] == len(new) and vectors.stored == [f"evt_long:{index}" for index in range(len(new))], "the 2629-token message embeds as multiple chunks, stored :0..:n")
    _check(all(_count(text) <= _MAX for batch in provider.batches for text in batch), "the provider never received an over-budget input")


def test_old_window_mutation() -> None:
    message = _message(2629)
    original = event_embeddings.chunk_event_content
    event_embeddings.chunk_event_content = lambda text, budget=None: original(text)  # type: ignore[assignment]
    try:
        mutant, _provider, mutant_vectors = _writer(_BUDGET)
        try:
            mutant.embed_event(_event("evt_long", message, "2026-09-27T01:00:00"))
            _check(False, "MUTATION: the old 8192-char window must fail against a 2048-token provider")
        except RuntimeError as exc:
            _check("generate_embeddings failed" in str(exc) and not mutant_vectors.stored, "MUTATION: the old 8192-char window is refused by the provider; nothing stored")
    finally:
        event_embeddings.chunk_event_content = original


def _writer(budget: TokenBudget | None, corpus: list[dict[str, object]] | None = None) -> tuple[EventEmbeddingWriter, _Provider, _Vectors]:
    provider = _Provider(budget)
    vectors = _Vectors()
    repository = _Corpus(StubStateService(), corpus or [])
    writer = EventEmbeddingWriter(repository=repository, embedding_service=provider, vector_service=vectors)  # type: ignore[arg-type]
    return writer, provider, vectors


def test_facade_refuses_loudly_and_counts() -> None:
    provider = _Provider(_BUDGET)

    class _Plugins:
        def get_plugin(self, name: str) -> object:
            return provider if name == "fake_embeddings" else None

    service = EmbeddingService(plugin_manager=_Plugins(), embedding_plugin_name="fake_embeddings")  # type: ignore[arg-type]
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    logging.getLogger("ananta.services.embedding_service").addHandler(handler)
    try:
        refused = service.generate_embeddings(["fine", _message(2100), _message(3000)])
    finally:
        logging.getLogger("ananta.services.embedding_service").removeHandler(handler)
    error = refused.get("error") or {}
    _check(refused.get("action_status") == "error" and error.get("code") == "embedding_input_too_long", "facade: an over-budget batch is refused with embedding_input_too_long")
    _check(error.get("details", {}).get("oversize_inputs") == [{"index": 1, "tokens": 2100}, {"index": 2, "tokens": 3000}], "facade: the refusal names every oversize input and its count")
    _check(service.oversize_inputs_refused == 2 and not provider.batches, "facade: counted (2) and the provider never ran; nothing truncated")
    _check(any("oversize_inputs_refused=2" in record.getMessage() for record in records), "facade: a WARNING carries the running count")
    _check(service.generate_embeddings(["fine"]).get("action_status") == "completed", "facade: a fitting batch passes through")
    _check(service.input_token_budget() is _BUDGET, "facade: exposes the bound provider's budget")


def test_policy_backfill() -> None:
    long_message = _message(2629)
    corpus = [_event("evt_a", "short one", "2026-09-27T01:00:00"), _event("evt_long", long_message, "2026-09-27T02:00:00"), _event("evt_b", "short two", "2026-09-27T03:00:00")]
    writer, _provider, vectors = _writer(_BUDGET, corpus)
    original = event_embeddings.chunk_event_content
    event_embeddings.chunk_event_content = lambda text, budget=None: original(text)  # type: ignore[assignment]
    try:
        halted = writer.drain_missing_events(page_size=1)
    finally:
        event_embeddings.chunk_event_content = original
    _check(halted["halted_on_error"] and "evt_long:0" not in vectors.present and "evt_b:0" not in vectors.present, "before the fix: the drain halts at the over-budget event and strands everything after it")
    repository = writer._repository  # noqa: SLF001
    _check(repository.get_event_embed_chunk_policy() is None, "a halted sweep records no policy, so the backfill repeats")
    swept = writer.drain_missing_events(page_size=1)
    _check(swept["policy_backfill"] and swept["reconcile"] and not swept["halted_on_error"], "the first drain under the new policy is a full backfill sweep")
    _check({"evt_long:0", "evt_long:1", "evt_b:0"} <= vectors.present, "the previously failed event and everything it stranded are embedded")
    _check(repository.get_event_embed_chunk_policy() == f"tokens:{_MAX}+chars:{EVENT_CHUNK_MAX_CHARS}", "the completed backfill records the policy")
    later = writer.drain_missing_events(page_size=1)
    _check(not later["policy_backfill"] and not later["reconcile"], "later drains are incremental again")


_LEGACY = {
    "evt_legacy": 2629,  # one old window over budget -> two new chunks
    "evt_same_count": 3000,  # two old windows, the first over budget -> two new chunks: same count, same ids (iss_50df7f10)
    "evt_capped": 40000,  # past EVENT_MAX_CHUNKS under both policies: the capped counts coincide (iss_50df7f10)
}


def _legacy_texts() -> dict[str, str]:
    return {**{event_id: _message(tokens) for event_id, tokens in _LEGACY.items()}, "evt_fit": "short and whole"}


def _old_chunk_ids(texts: dict[str, str]) -> set[str]:
    return {event_chunk_external_id(event_id, index) for event_id, text in texts.items() for index in range(min(len(chunk_event_content(text, None)), EVENT_MAX_CHUNKS))}


def _legacy_sweep(stale: Any, *, previous: str | None = None) -> _Vectors:
    """One policy backfill over an LM Studio-era corpus: every old-policy chunk of every event is already stored."""
    texts = _legacy_texts()
    corpus = [_event(event_id, text, f"2026-09-27T0{index}:00:00") for index, (event_id, text) in enumerate(texts.items())]
    writer, _provider, vectors = _writer(_BUDGET, corpus)
    vectors.present |= _old_chunk_ids(texts)
    if previous is not None:
        writer._repository.set_event_embed_chunk_policy(previous)  # noqa: SLF001
    original = event_embeddings.policy_backfill_chunks
    event_embeddings.policy_backfill_chunks = stale
    try:
        writer.drain_missing_events(page_size=10)
    finally:
        event_embeddings.policy_backfill_chunks = original
    return vectors


def _pre_fix_rule() -> Any:
    """The pre-fix rule (iss_50df7f10): re-embed only when the new policy's last chunk id is not stored."""
    texts = _legacy_texts()
    ids, stored = {text: event_id for event_id, text in texts.items()}, _old_chunk_ids(texts)

    def stale(text: str, backfill: PolicyBackfill) -> int | None:
        last = min(len(chunk_event_content(text, backfill.budget)), EVENT_MAX_CHUNKS) - 1
        return None if event_chunk_external_id(ids[text], last) in stored else 0

    return stale


def _reembedded(vectors: _Vectors) -> list[str]:
    return sorted({item.split(":")[0] for item in vectors.stored})


def test_policy_backfill_reembeds_truncated() -> None:
    for tokens in (3000, 40000):
        old, new = chunk_event_content(_message(tokens), None), chunk_event_content(_message(tokens), _BUDGET)
        _check(min(len(old), EVENT_MAX_CHUNKS) == min(len(new), EVENT_MAX_CHUNKS) and max(_count(chunk) for chunk in old) > _MAX, f"fixture: {tokens} tokens has an over-budget old chunk and the same capped chunk count")
    texts = _legacy_texts()
    vectors = _legacy_sweep(event_embeddings.policy_backfill_chunks)
    for event_id in _LEGACY:
        old = min(len(chunk_event_content(texts[event_id], None)), EVENT_MAX_CHUNKS)
        new = min(len(chunk_event_content(texts[event_id], _BUDGET)), EVENT_MAX_CHUNKS)
        stored = [item for item in vectors.stored if item.startswith(f"{event_id}:")]
        _check(stored == [event_chunk_external_id(event_id, index) for index in range(new)], f"the policy sweep re-embeds {event_id} whole under the new policy ({len(stored)} of {new} chunks)")
        _check({event_chunk_external_id(event_id, index) for index in range(max(new, old))} <= set(vectors.deleted), f"{event_id}: every old chunk id is cleared before the store")
    _check("evt_fit" not in _reembedded(vectors), "an event whose stored chunking is unchanged is not re-embedded")
    unknown = _legacy_sweep(event_embeddings.policy_backfill_chunks, previous="tokens:4096+chars:8192")
    _check("evt_fit:0" in unknown.stored and f"evt_fit:{EVENT_MAX_CHUNKS - 1}" in unknown.deleted, "a previous policy this code cannot reproduce re-embeds every event and clears up to the chunk cap")
    mutant = _legacy_sweep(_pre_fix_rule())
    _check(_reembedded(mutant) == ["evt_legacy"], f"MUTATION: the pre-fix last-chunk rule keeps the equal-count and capped truncated vectors (re-embeds only {_reembedded(mutant)})")


def test_query_refusal() -> None:
    writer, provider, _vectors = _writer(_BUDGET)
    try:
        writer.search(query=_message(2100), limit=5)
        _check(False, "an over-budget query is refused")
    except ValueError as exc:
        _check("2100 tokens" in str(exc) and "2048" in str(exc) and not provider.batches, "an over-budget query is refused naming both counts, before embedding")
    _check(writer.search(query="short query", limit=5) == [], "a fitting query runs")


def test_knowledge_indexer_splits() -> None:
    from default_knowledge_plugin import kb_indexing  # noqa: PLC0415

    class _Memory:
        def __init__(self) -> None:
            self.remembered: list[str] = []

        def remember(self, content: str, tags: list[str]) -> dict[str, str]:
            self.remembered.append(content)
            return {"memory_id": f"mem_{len(self.remembered)}"}

    dense = ", ".join(["a"] * 1300)  # ~2600 counted tokens in ~3900 chars: the char bound alone passes a refusable chunk
    with tempfile.TemporaryDirectory() as temporary:
        kb_dir = Path(temporary) / "kb"
        kb_dir.mkdir()
        (kb_dir / "manifest.yaml").write_text("name: kb\ncontent:\n  chunking:\n    max_chars: 2900\n", encoding="utf-8")
        (kb_dir / "dense.md").write_text(f"# Dense\n\n{dense[:2300]}\n", encoding="utf-8")
        manifest = kb_indexing.resolve_manifest(kb_dir, "kb", None)
        unbounded = _Memory()
        kb_indexing.index_files(kb_dir, "kb", manifest, unbounded, budget=None)
        bounded = _Memory()
        kb_indexing.index_files(kb_dir, "kb", manifest, bounded, budget=TokenBudget(1024, _count))
    _check(any(_count(text) > 1024 for text in unbounded.remembered), "control: without a budget the chunk exceeds a 1024-token provider")
    _check(len(bounded.remembered) > len(unbounded.remembered) and all(_count(text) <= 1024 for text in bounded.remembered), "with the budget every remembered chunk (preamble included) fits")
    body = "".join(text.split("\n", 1)[1] for text in bounded.remembered)
    _check(body.replace(" ", "").replace("\n", "") == "".join(text.split("\n", 1)[1] for text in unbounded.remembered).replace(" ", "").replace("\n", ""), "the split chunks carry every character of the original (no loss)")


def test_coreai_declares_its_ceiling() -> None:
    from coreai_embeddings_plugin.contracts import BUCKETS  # noqa: PLC0415
    from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: PLC0415

    plugin = CoreAIEmbeddingsPlugin({})
    budget = plugin.input_token_budget()
    _check(budget.max_input_tokens == BUCKETS[-1] == 2048, "Core AI declares its largest bucket (2048) as the ceiling")
    try:
        budget.count("text")
        _check(False, "Core AI counts only when prepared")
    except RuntimeError:
        _check(True, "an unprepared Core AI refuses to count (loud, never a guess)")


def test_coreai_host_floor() -> None:
    """rul_c11cf191 / iss_f7937801: the runtime serves from the flow's apple_embeddings floor upward, never only one release."""
    import json as _json  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from coreai_embeddings_plugin import runtime as coreai_runtime  # noqa: PLC0415
    from coreai_embeddings_plugin.contracts import EmbeddingError, ErrorCode  # noqa: PLC0415

    flow = _json.loads((REPO_ROOT / "plugins/github_midwife_plugin/knowledge_base/existing_install_flow.json").read_text(encoding="utf-8"))
    floor = flow["host_profiles"]["apple_embeddings"]["macos_major_min"]
    _check(coreai_runtime.MIN_MACOS_MAJOR == floor, f"the runtime floor ({coreai_runtime.MIN_MACOS_MAJOR}) is the flow's apple_embeddings floor ({floor})")
    with tempfile.TemporaryDirectory() as temporary:
        for version, admitted in ((f"{floor - 1}.6", False), (f"{floor}.0", True), ("27.0", True), ("28.1", True)):
            engine = coreai_runtime.EmbeddingRuntime(Path(temporary), "cpu_only")
            try:
                with patch.object(coreai_runtime.platform, "system", lambda: "Darwin"), patch.object(coreai_runtime.platform, "mac_ver", lambda version=version: (version, ("", "", ""), "arm64")):
                    engine._verified_asset()  # noqa: SLF001
                code = None
            except EmbeddingError as exc:
                code = exc.code
            finally:
                engine.close()
            _check((code != ErrorCode.UNAVAILABLE) is admitted, f"macOS {version}: {'passes' if admitted else 'refused at'} the host gate ({code})")


# Every module that sends text to ``generate_embeddings`` and how it meets the provider budget.
_REVIEWED_CALLERS = {
    "ananta/src/ananta/llm/session_ledger/event_embeddings.py": "splits every event to the budget; refuses an over-budget query",
    "ananta/src/ananta/llm/session_ledger/summarization.py": "one vector per summary; the facade refuses over-budget text loudly",
    "ananta/src/ananta/services/discovery_service/service.py": "one vector per process description or query; the facade refuses loudly",
    "ananta/src/ananta/services/embedding_service/__init__.py": "the facade itself: refuses and counts over-budget inputs",
    "plugins/actr_memory_plugin/src/actr_memory_plugin/backend.py": "one vector per memory; KB chunks pre-split by index_files; the facade refuses loudly",
    "plugins/github_midwife_plugin/src/github_midwife_plugin/installation_doctor.py": "a fixed short readiness probe over the bridge",
    "plugins/openai_embeddings_plugin/src/openai_embeddings_plugin/plugin.py": "its own fixed dimension probe",
    "plugins/titanv2_embeddings_plugin/src/titanv2_embeddings_plugin/plugin.py": "its own fixed dimension probe",
}


def test_enumeration() -> None:
    found: set[str] = set()
    sources = [*(REPO_ROOT / "ananta" / "src").rglob("*.py"), *(REPO_ROOT / "plugins").glob("*/src/**/*.py")]
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        calls = [line for line in text.splitlines() if (".generate_embeddings(" in line or "embedding_service::generate_embeddings" in line) and not line.lstrip().startswith((">>>", "#"))]
        if calls:
            found.add(str(path.relative_to(REPO_ROOT)))
    # A seed prunes plugins outside its profile, so a reviewed module absent from this tree is not stale.
    present = {caller for caller in _REVIEWED_CALLERS if (REPO_ROOT / caller).is_file()}
    _check(found == present, f"every generate_embeddings caller is reviewed for the budget (unreviewed: {sorted(found - present)}, stale: {sorted(present - found)})")


def main() -> int:
    print("embedding token budget (iss_9166af93) smoke")
    for test in (test_split_to_fit, test_2629_token_message, test_2629_token_message_embeds, test_old_window_mutation, test_facade_refuses_loudly_and_counts, test_policy_backfill, test_policy_backfill_reembeds_truncated, test_query_refusal, test_knowledge_indexer_splits, test_coreai_declares_its_ceiling, test_coreai_host_floor, test_enumeration):
        print(f"\n[{test.__name__}]")
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
