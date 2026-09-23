#!/usr/bin/env python3
"""Tier β acceptance smoke for the session-ledger ABC split.

Per `workbench/2026-06-11_session_ledger_api_god_class_split_v1.md` Tier β
acceptance criterion: post-split, ``quality_gates/god_class_check.py``
against ``ananta/src/ananta/services/session_ledger_service/interfaces/public.py``
reports 0 violations AND the per-class @service_interface_process verb
count matches the v1 §3 verb-routing table 1-to-1.

Run::

    .venv/bin/python3 ananta/tests/session_ledger_service/interfaces_split_no_god_class_smoke.py

Exit codes:
  0 — both gates pass
  1 — one or more gates fail (verb count mismatch OR god-class violation)
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PUBLIC_PY = (
    REPO_ROOT
    / "ananta/src/ananta/services/session_ledger_service/interfaces/public.py"
)
GATE_SCRIPT = REPO_ROOT / "quality_gates/god_class_check.py"

# v1 §3 verb-routing table + the post-v1 additions (the three backfill ABCs —
# SessionLedgerEventExternalIdBackfillAPI / SessionLedgerEventSourceDenormBackfillAPI
# / SessionLedgerSessionSourceKindBackfillAPI — plus the census / poll_source /
# backfill_export_blob_identity verbs that landed on existing ABCs since the
# split). Every verb routes to its ABC; this is the load-bearing expectation —
# a mismatch means public.py diverged from the intended routing (a verb on the
# wrong ABC, or a new verb/ABC added without refreshing this table).
EXPECTED_VERBS_BY_ABC: dict[str, set[str]] = {
    "SessionLedgerReadAPI": {
        "list_sources",
        "list_sessions",
        "list_active_sessions",
        "get_session_timeline",
        "list_tool_calls",
        "list_canonical_contributors",
        "census",
    },
    "SessionLedgerQualificationAPI": {
        "qualify_selected_sources",
    },
    "SessionLedgerIngestAPI": {
        "register_source",
        "ingest_raw_chunk",
        "get_import_status",
        "backfill_export_blob_identity",
        "set_source_enabled",
    },
    "SessionLedgerPollingDriverAPI": {
        "trigger_poll",
        "ensure_periodic_poll_schedule",
        "reset_ingest_state",
        "reset_source_cursor",
        "poll_source",
    },
    "SessionLedgerCanonicalPointerRepairAPI": {
        "lift_canonical_pointer_for_duplicate_sessions",
    },
    "SessionLedgerInvertedBoundsRepairAPI": {
        "backfill_first_last_event_at_repair",
        "backfill_orphan_running_batches_for_source",
        "backfill_summary_embedding_vector_ids",
        "retire_duplicate_source",
    },
    "SessionLedgerEventExternalIdBackfillAPI": {
        "backfill_event_external_ids",
    },
    "SessionLedgerEventSourceDenormBackfillAPI": {
        "backfill_event_source_denormalization",
    },
    "SessionLedgerSessionSourceKindBackfillAPI": {
        "backfill_session_source_kinds",
    },
    "SessionLedgerSummarizeAPI": {
        "lift_codex_stage1_summaries",
        "summarize_quiescent_sessions",
        "ensure_periodic_summarize_schedule",
    },
    "SessionLedgerDeploymentAPI": {
        "generate_ingest_setup",
        "approve_pairing",
        "shipper_self_revoke",
    },
    "SessionLedgerSearchAPI": {
        "push_session_summary_chunk",
        "search_sessions",
        "list_events_by_source_window",
        "search_event_content",
        "event_embedding_coverage",
        "embed_missing_event_content",
    },
    "SessionLedgerEmbeddingDrainAPI": {
        "drain_event_embeddings",
        "ensure_periodic_embed_schedule",
    },
}


def _is_sip(dec: ast.expr) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name):
        return target.id == "service_interface_process"
    if isinstance(target, ast.Attribute):
        return target.attr == "service_interface_process"
    return False


def _sip_name(dec: ast.expr) -> str | None:
    if not isinstance(dec, ast.Call):
        return None
    for kw in dec.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def check_verb_counts() -> int:
    """Walk public.py AST and verify per-ABC @service_interface_process counts."""
    tree = ast.parse(PUBLIC_PY.read_text(), filename=str(PUBLIC_PY))
    actual: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        verbs: set[str] = set()
        for item in node.body:
            if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in item.decorator_list:
                if _is_sip(dec):
                    name = _sip_name(dec) or item.name
                    verbs.add(name)
                    break
        if verbs:
            actual[node.name] = verbs

    print(f"Verb-count check: found {len(actual)} ABCs in public.py")
    failures = 0
    if set(actual) != set(EXPECTED_VERBS_BY_ABC):
        print(
            f"  CLASS-SET MISMATCH: expected={sorted(EXPECTED_VERBS_BY_ABC)}"
            f" actual={sorted(actual)}",
        )
        failures += 1
    for abc_name, expected_verbs in sorted(EXPECTED_VERBS_BY_ABC.items()):
        actual_verbs = actual.get(abc_name, set())
        if actual_verbs == expected_verbs:
            print(f"  OK {abc_name}: {len(actual_verbs)} verbs")
        else:
            missing = expected_verbs - actual_verbs
            extra = actual_verbs - expected_verbs
            print(
                f"  FAIL {abc_name}: missing={sorted(missing)} extra={sorted(extra)}",
            )
            failures += 1
    return failures


def check_god_class_gate() -> int:
    """Run god_class_check.py against public.py; expect exit 0."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(GATE_SCRIPT),
            str(PUBLIC_PY),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"\ngod-class gate exit code: {result.returncode}")
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip())
    if result.returncode == 0:
        print("  OK god-class gate clean")
        return 0
    print("  FAIL god-class gate reported violations")
    return 1


def main() -> int:
    failures = check_verb_counts() + check_god_class_gate()
    if failures:
        print(f"\n{failures} failure(s).")
        return 1
    print("\nOK: split landed; verb counts match v1 §3; gate clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
