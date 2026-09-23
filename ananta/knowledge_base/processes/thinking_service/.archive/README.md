# Archived thinking_service process definitions

These six process JSONs were retired 2026-07-03 under **DEP-01 Phase-2a**
(qwen thinking-path retirement; design:
`workbench/2026-07-02_dep01_qwen_thinking_path_retirement_design.md` §8a,
retire set extended from ×4 to ×6 by Coordinator-Day arbiter ruling).

The qwen push-generation WBS pipeline they fronted is gone from the code
(interface, wrapper, provider protocol, plugin delegate, and
`WbsAuthoringService` methods all removed):

- `create_work_breakdown_structure.json`
- `create_work_breakdown_structure_outline.json`
- `create_wbs_work_item_detail.json`
- `create_joseki_work_breakdown_structure.json`
- `graft_work_breakdown_structure_detail_steps.json` (orphaned pipeline
  stage — grafted plan steps invoking the retired detail verb)
- `assemble_work_breakdown_structure.json` (orphaned pipeline stage —
  its outline/detail producers are retired)

**Replacement:** WBS documents are authored by the calling agent and
registered by value via
`service_interface::thinking_service::register_authored_work_breakdown_structure`
(validate first with `validate_authored_work_breakdown_structure`).
Deterministic per-section pipeline WBS generation remains available via
`generate_section_stem_wbs`; execution-tail grafting over a registered
WBS remains available via `graft_work_breakdown_structure_segment`.

Files stay on disk as the forensic record; the `.archive/**` pattern is
excluded from KB ingestion, and the registry-driven overlay loader never
resolves paths for processes that no longer exist on the decorated
surface.
