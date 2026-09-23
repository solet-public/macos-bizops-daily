#!/usr/bin/env python3
"""DEP-01 Phase-2a — authored-by-value conversion smoke (no pytest).

Proves the Class-A artifact-authoring verbs are converted OFF the qwen
thinking-model push path onto the authored-by-value contract:

* invoked WITH authored ``content`` → the full downstream pipeline runs
  (normalize → section-size validation → KB write → state record → focus
  upsert) with NO thinking-model invocation — structurally guaranteed:
  ``ArtifactAuthoringService`` no longer accepts a model client, article
  loader, or prompt serializer at all;
* invoked WITHOUT ``content`` (empty / whitespace) → the typed loud
  error ``default_thinking_plugin.authored_content_required`` fires
  BEFORE any storage side effect — no silent fallback to the retired
  qwen path;
* the downstream validation that used to police model output now
  polices the AUTHORED content (oversized ## section still rejects);
* the six WBS push verbs are retired from every layer while the
  authored-by-value / deterministic survivors remain.

Offline: no live solet, no LM Studio, no Postgres. Storage collaborators
are recording stubs; the service and plugin delegate are the real
production code path.

Run:
    .venv/bin/python3 \\
      plugins/default_thinking_plugin/tests/authored_by_value_conversion_smoke.py
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "default_thinking_plugin" / "src"))

importlib.import_module("ananta.core.config")  # pre-warm; avoids a latent cycle

from ananta.error_handling import FrameworkError  # noqa: E402
from default_thinking_plugin.artifact_authoring import (  # noqa: E402
    ArtifactAuthoringService,
)
from default_thinking_plugin.constants import ErrorCode  # noqa: E402
from default_thinking_plugin.plugin import DefaultThinkingPlugin  # noqa: E402

# ---------------------------------------------------------------------------
# Collaborator stubs
# ---------------------------------------------------------------------------


class RecordingKnowledgeWriter:
    """Records KB writes; reads return empty (no pre-existing document)."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def write(self, path: str, content: str) -> None:
        self.writes.append((path, content))

    def read(self, path: str) -> str:
        return ""


class RecordingStateStore:
    """Records state writes; reads return an empty-records envelope."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        self.writes.append((namespace, data))
        return {"action_status": "completed"}

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"records": []}}

    def generate_id(self, *, prefix: str) -> str:
        return f"{prefix}stub-1"


class RecordingFocusManager:
    """Records focus upserts/defocuses; returns a fixed memory id.

    ``focused`` seeds ``get_focused`` so verbs that derive identity from
    focus (create_work_manifest via the focused brief tag) can run.
    """

    def __init__(self, focused: list[dict[str, Any]] | None = None) -> None:
        self.upserts: list[tuple[str, str]] = []
        self.defocused: list[str] = []
        self._focused = focused or []

    def upsert(self, content: str, *, doc_tag: str, label: str) -> str:
        self.upserts.append((doc_tag, label))
        return "mem-stub-1"

    def defocus_by_label(self, label: str) -> None:
        self.defocused.append(label)

    def get_focused(self) -> list[dict[str, Any]]:
        return list(self._focused)


def build_service(
    focused: list[dict[str, Any]] | None = None,
) -> tuple[
    ArtifactAuthoringService,
    RecordingKnowledgeWriter,
    RecordingStateStore,
    RecordingFocusManager,
]:
    """Real service over recording storage collaborators.

    The constructor accepting ONLY storage collaborators is itself the
    no-model-path proof: there is no seam left through which a converted
    verb could reach a thinking model.
    """
    knowledge_writer = RecordingKnowledgeWriter()
    state_store = RecordingStateStore()
    focus_manager = RecordingFocusManager(focused)
    service = ArtifactAuthoringService(
        knowledge_writer=knowledge_writer,
        state_store=state_store,
        focus_manager=focus_manager,
    )
    return service, knowledge_writer, state_store, focus_manager


def build_plugin_delegate() -> tuple[Any, RecordingKnowledgeWriter]:
    """Real plugin delegate over the real service (``__new__`` + injection)."""
    service, knowledge_writer, _, _ = build_service()
    plugin: Any = DefaultThinkingPlugin.__new__(DefaultThinkingPlugin)
    plugin._artifact_authoring_service = (  # noqa: SLF001 — harness binding
        lambda session_id, _service=service: _service
    )
    return plugin, knowledge_writer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

INTAKE_ID = "intk-authored-fixture"

AUTHORED_RIS = """# Resolved Intake State

## 1. Intake Strategy

Long-form neuro-ambient intake, authored by the calling frontier agent
and passed by value under the DEP-01 authored-by-value contract.

## 2. Resolved Blocking Fields

- target duration: 20 minutes
- delivery format: single continuous track
- primary use context: evening wind-down
- core feeling: settled warmth
- hard exclusions: no sudden transients

## 3. Deliberate Defaults

Root frequency and loudness follow house style; no operator overrides.

## 4. Manifest Handoff

Identity values flow to the Work Manifest unchanged.
"""


def _authored_content_error(exc: FrameworkError) -> bool:
    return getattr(exc, "error_code", None) == ErrorCode.AUTHORED_CONTENT_REQUIRED


# ---------------------------------------------------------------------------
# Checker (standalone pass/fail accumulator — project policy: no pytest)
# ---------------------------------------------------------------------------


class Checker:
    """Minimal pass/fail accumulator."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: object, label: str) -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label}")

    def summary(self) -> int:
        print(f"\n{self.passed} passed, {len(self.failed)} failed")
        for label in self.failed:
            print(f"  FAILED: {label}")
        return 1 if self.failed else 0


# ---------------------------------------------------------------------------
# Cases — create_resolved_intake_state
# ---------------------------------------------------------------------------


def test_ris_authored_content_creates(c: Checker) -> None:
    service, knowledge_writer, state_store, focus_manager = build_service()
    result = service.create_resolved_intake_state(INTAKE_ID, AUTHORED_RIS)
    c.check(result["status"] == "created", f"status is created ({result['status']!r})")
    c.check(result["intake_id"] == INTAKE_ID, "intake_id echoes back")
    c.check(
        result["content"].startswith("# Resolved Intake State"),
        "returned content is the authored document",
    )
    c.check(
        [path for path, _ in knowledge_writer.writes]
        == [f"intake_states/{INTAKE_ID}.md"],
        f"exactly one KB write at the intake_states/ path ({knowledge_writer.writes!r})",
    )
    records = [
        data["record"]
        for _, data in state_store.writes
        if data.get("table") == "thinking_intake_state"
    ]
    c.check(len(records) == 1, f"exactly one thinking_intake_state record ({records!r})")
    c.check(
        (f"resolved_intake_state:{INTAKE_ID}", "resolved_intake_state")
        in focus_manager.upserts,
        "document lands in focus like every stored intake state",
    )


def test_ris_empty_content_fails_loud_before_storage(c: Checker) -> None:
    service, knowledge_writer, state_store, _ = build_service()
    try:
        service.create_resolved_intake_state(INTAKE_ID, "")
    except FrameworkError as exc:
        c.check(
            _authored_content_error(exc),
            f"empty content raises authored_content_required ({exc})",
        )
        c.check(
            "DEP-01" in str(exc) and "content" in str(exc),
            f"error message names the retired path and the contract ({exc})",
        )
    else:
        c.check(False, "empty content must raise FrameworkError")
    c.check(not knowledge_writer.writes, "no KB write on missing content")
    c.check(not state_store.writes, "no state write on missing content")


def test_ris_whitespace_content_fails_loud(c: Checker) -> None:
    service, knowledge_writer, _, _ = build_service()
    try:
        service.create_resolved_intake_state(INTAKE_ID, "   \n\n  ")
    except FrameworkError as exc:
        c.check(
            _authored_content_error(exc),
            f"whitespace-only content raises authored_content_required ({exc})",
        )
    else:
        c.check(False, "whitespace-only content must raise FrameworkError")
    c.check(not knowledge_writer.writes, "no KB write on whitespace content")


def test_ris_missing_intake_id_still_parameter_error(c: Checker) -> None:
    service, _, _, _ = build_service()
    try:
        service.create_resolved_intake_state("", AUTHORED_RIS)
    except FrameworkError as exc:
        c.check(
            getattr(exc, "error_code", None) == ErrorCode.PARAMETER_ERROR,
            f"missing intake_id keeps the parameter_error contract ({exc})",
        )
    else:
        c.check(False, "missing intake_id must raise FrameworkError")


def test_ris_oversized_section_still_rejects(c: Checker) -> None:
    """Downstream validation now polices the AUTHORED content."""
    service, knowledge_writer, _, _ = build_service()
    oversized = AUTHORED_RIS + "\n## 5. Oversized\n\n" + ("x" * 4000) + "\n"
    try:
        service.create_resolved_intake_state(INTAKE_ID, oversized)
    except (FrameworkError, ValueError) as exc:
        c.check(True, f"oversized section rejects ({type(exc).__name__})")
    else:
        c.check(False, "oversized ## section must be rejected")
    c.check(not knowledge_writer.writes, "no KB write on oversized section")


def test_ris_plugin_delegate_wires_content_through(c: Checker) -> None:
    """4-layer wiring: the plugin delegate passes authored content down."""
    plugin, knowledge_writer = build_plugin_delegate()
    result = plugin.create_resolved_intake_state(
        INTAKE_ID, AUTHORED_RIS, session_id="sess-smoke",
    )
    c.check(
        result["status"] == "created",
        f"plugin delegate creates from authored content ({result['status']!r})",
    )
    c.check(
        bool(knowledge_writer.writes),
        "plugin delegate reaches the KB through the real service",
    )


# ---------------------------------------------------------------------------
# Cases — create_work_manifest
# ---------------------------------------------------------------------------

FOCUSED_BRIEF = [
    {
        "content": "# Neuro-Ambient Complete Brief Form\n\n- brief_id: brf-fixture-001\n",
        "tags": ["complete_brief", "complete_brief:brf-fixture-001"],
    },
]

AUTHORED_MANIFEST = """# Work Manifest

MANIFEST ID: wmf-fixture-001
Title: Authored Fixture Manifest

## Phases

1. Compose the material
2. Deliver the master
"""


def test_manifest_authored_content_creates(c: Checker) -> None:
    service, knowledge_writer, state_store, focus_manager = build_service(
        focused=FOCUSED_BRIEF,
    )
    result = service.create_work_manifest(AUTHORED_MANIFEST)
    c.check(result["status"] == "created", f"status is created ({result['status']!r})")
    c.check(
        result["manifest_id"] == "wmf-fixture-001",
        f"manifest_id derived from the focused brief ({result['manifest_id']!r})",
    )
    c.check(
        [path for path, _ in knowledge_writer.writes]
        == ["manifests/wmf-fixture-001.md"],
        f"exactly one KB write at the manifests/ path ({knowledge_writer.writes!r})",
    )
    c.check(
        "resolved_intake_state" in focus_manager.defocused,
        "manifest creation still defocuses the consumed Resolved Intake State",
    )
    records = [
        data["record"]
        for _, data in state_store.writes
        if data.get("table") == "thinking_manifest"
    ]
    c.check(len(records) == 1, f"exactly one thinking_manifest record ({records!r})")


def test_manifest_empty_content_fails_loud(c: Checker) -> None:
    service, knowledge_writer, state_store, _ = build_service(focused=FOCUSED_BRIEF)
    try:
        service.create_work_manifest("")
    except FrameworkError as exc:
        c.check(
            _authored_content_error(exc),
            f"empty content raises authored_content_required ({exc})",
        )
    else:
        c.check(False, "empty content must raise FrameworkError")
    c.check(not knowledge_writer.writes, "no KB write on missing content")
    c.check(not state_store.writes, "no state write on missing content")


# ---------------------------------------------------------------------------
# Cases — create_authored_artifact
# ---------------------------------------------------------------------------

AUTHORED_BRIEF = """# Neuro-Ambient Complete Brief Form

## 1. Identity

- composition_number: 20
- genre: neuro-ambient

## 2. Creative Intent

Settled warmth for evening wind-down; no sudden transients.
"""


def test_authored_artifact_brief_creates_from_content(c: Checker) -> None:
    """Brief identity now derives from the AUTHORED document's fields."""
    service, knowledge_writer, state_store, _ = build_service()
    result = service.create_authored_artifact("brief", AUTHORED_BRIEF)
    c.check(result["status"] == "created", f"status is created ({result['status']!r})")
    c.check(
        result["artifact_id"] == "brf-neuro-ambient-composition-020-001",
        f"brief id derives from authored content fields ({result['artifact_id']!r})",
    )
    c.check(result["parent_id"] == "", "brief has no parent")
    c.check(
        [path for path, _ in knowledge_writer.writes]
        == ["briefs/brf-neuro-ambient-composition-020-001.md"],
        f"exactly one KB write at the briefs/ path ({knowledge_writer.writes!r})",
    )
    records = [
        data["record"]
        for _, data in state_store.writes
        if data.get("table") == "thinking_brief"
    ]
    c.check(len(records) == 1, f"exactly one thinking_brief record ({records!r})")


def test_authored_artifact_empty_content_fails_loud(c: Checker) -> None:
    service, knowledge_writer, _, _ = build_service()
    try:
        service.create_authored_artifact("brief", "")
    except FrameworkError as exc:
        c.check(
            _authored_content_error(exc),
            f"empty content raises authored_content_required ({exc})",
        )
    else:
        c.check(False, "empty content must raise FrameworkError")
    c.check(not knowledge_writer.writes, "no KB write on missing content")


# ---------------------------------------------------------------------------
# Cases — create_movement_design
# ---------------------------------------------------------------------------

AUTHORED_PACKET = """# Movement Design Packet

## Formal Argument

Authored packet content for the fixture movement.
"""

AUTHORED_LEDGER = """# Phrase Design Ledger

## Phrase Obligations

Authored ledger content for the fixture movement.
"""


def test_movement_design_authored_contents_create_both(c: Checker) -> None:
    service, knowledge_writer, state_store, _ = build_service()
    result = service.create_movement_design(
        "wmf-fixture-001", "toccata", AUTHORED_PACKET, AUTHORED_LEDGER,
    )
    c.check(result["status"] == "created", f"status is created ({result['status']!r})")
    c.check(
        result["packet_id"] == "mdp-stub-1" and result["ledger_id"] == "pdl-stub-1",
        f"platform-generated packet/ledger ids ({result!r})",
    )
    written_paths = [path for path, _ in knowledge_writer.writes]
    c.check(
        written_paths
        == [
            "movement_design_packets/mdp-stub-1.md",
            "phrase_design_ledgers/pdl-stub-1.md",
        ],
        f"packet and ledger both land in the KB ({written_paths!r})",
    )
    tables = [data.get("table") for _, data in state_store.writes]
    c.check(
        tables
        == ["thinking_movement_design_packet", "thinking_phrase_design_ledger"],
        f"packet and ledger rows both recorded ({tables!r})",
    )


def test_movement_design_missing_ledger_fails_loud(c: Checker) -> None:
    service, knowledge_writer, _, _ = build_service()
    try:
        service.create_movement_design(
            "wmf-fixture-001", "toccata", AUTHORED_PACKET, "",
        )
    except FrameworkError as exc:
        c.check(
            _authored_content_error(exc),
            f"missing ledger_content raises authored_content_required ({exc})",
        )
        c.check(
            "ledger_content" in str(exc),
            f"error message names the missing param ({exc})",
        )
    else:
        c.check(False, "missing ledger_content must raise FrameworkError")
    c.check(not knowledge_writer.writes, "no KB write when either document is missing")


# ---------------------------------------------------------------------------
# Cases — invoke_pipeline_spec_authoring (pipeline_spec authored payload)
# ---------------------------------------------------------------------------


def test_pipeline_spec_authored_payload_passes_through_raw(c: Checker) -> None:
    service, _, _, _ = build_service()
    payload = '{"piece": {"style_family": "neuro_ambient"}}'
    returned = service.invoke_pipeline_spec_authoring(
        "psp-fixture-001", "wmf-fixture-001", payload,
    )
    c.check(
        returned == payload,
        "authored pipeline-spec payload passes through raw (no normalization)",
    )


def test_pipeline_spec_empty_payload_fails_loud(c: Checker) -> None:
    service, _, _, _ = build_service()
    try:
        service.invoke_pipeline_spec_authoring("psp-fixture-001", "wmf-fixture-001", "")
    except FrameworkError as exc:
        c.check(
            _authored_content_error(exc),
            f"empty pipeline-spec payload raises authored_content_required ({exc})",
        )
    else:
        c.check(False, "empty pipeline-spec payload must raise FrameworkError")


# ---------------------------------------------------------------------------
# Cases — WBS push-verb retirement (×6, Coordinator-Day arbiter ruling)
# ---------------------------------------------------------------------------

RETIRED_WBS_VERBS = (
    "create_work_breakdown_structure",
    "create_work_breakdown_structure_outline",
    "create_wbs_work_item_detail",
    "create_joseki_work_breakdown_structure",
    "graft_work_breakdown_structure_detail_steps",
    "assemble_work_breakdown_structure",
)

SURVIVING_WBS_VERBS = (
    "register_authored_work_breakdown_structure",
    "validate_authored_work_breakdown_structure",
    "generate_section_stem_wbs",
    "graft_work_breakdown_structure_segment",
    "record_work_breakdown_structure_step_state",
    "record_work_manifest_phase_state",
    "patch_work_breakdown_structure",
)


def test_wbs_push_verbs_retired_from_every_layer(c: Checker) -> None:
    from ananta.services.thinking_service.interfaces.public import (
        ThinkingServiceAPI,
    )

    for verb in RETIRED_WBS_VERBS:
        c.check(
            not hasattr(ThinkingServiceAPI, verb),
            f"{verb} gone from the decorated interface",
        )
        c.check(
            not hasattr(DefaultThinkingPlugin, verb),
            f"{verb} gone from the plugin",
        )


def test_wbs_survivors_still_present(c: Checker) -> None:
    from ananta.services.thinking_service.interfaces.public import (
        ThinkingServiceAPI,
    )

    for verb in SURVIVING_WBS_VERBS:
        c.check(
            hasattr(ThinkingServiceAPI, verb) and hasattr(DefaultThinkingPlugin, verb),
            f"{verb} survives on interface and plugin",
        )


def main() -> int:
    c = Checker("DEP-01 authored-by-value conversion")
    cases: list[Callable[[Checker], None]] = [
        test_ris_authored_content_creates,
        test_ris_empty_content_fails_loud_before_storage,
        test_ris_whitespace_content_fails_loud,
        test_ris_missing_intake_id_still_parameter_error,
        test_ris_oversized_section_still_rejects,
        test_ris_plugin_delegate_wires_content_through,
        test_manifest_authored_content_creates,
        test_manifest_empty_content_fails_loud,
        test_authored_artifact_brief_creates_from_content,
        test_authored_artifact_empty_content_fails_loud,
        test_movement_design_authored_contents_create_both,
        test_movement_design_missing_ledger_fails_loud,
        test_pipeline_spec_authored_payload_passes_through_raw,
        test_pipeline_spec_empty_payload_fails_loud,
        test_wbs_push_verbs_retired_from_every_layer,
        test_wbs_survivors_still_present,
    ]
    for case in cases:
        print(f"\n{case.__name__}")
        case(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
