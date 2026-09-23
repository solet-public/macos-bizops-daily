# claude_code_filesystem_session_source_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:session_ledger

Embedding Description: Operator-facing pitch and setup steps for ingesting Claude Code session transcripts into a solet's own session ledger, surfaced during hydration if this plugin is present but not yet bound and registered.

## Pitch

With this plugin present but not wired up, the solet has zero visibility into its own conversation history — no `search_sessions`, no `search_event_content`, no cross-session recall, and the solet-first "ask the solet what happened" pattern this platform runs on has nothing to search. Every session with it starts from a blank slate.

Enabling this means the solet reads your local Claude Code session transcript files (`~/.claude/projects/<encoded_cwd>/<session_id>.jsonl`, one per session, on THIS machine). Nothing leaves the machine, but it is real filesystem access to your own conversation history, and that is exactly why it needs an explicit yes before wiring. Consent-gated is not the same as optional: this is a CORE capability of the platform — the deployment report card tracks it until configured. Ask before wiring it up: "Right now this solet can't recall anything about past conversations, even the one you're having right now once it ends. I can set up ingestion of your Claude Code session transcripts so it can search its own history — that means reading your local `~/.claude/projects/` files. Want this set up?"

## Setup

On an explicit yes:

Use the no-MCP command path first. Substitute the actual solet name for
`<name>` below; do not translate these steps into `mcp__<name>__process_call`
unless MCP tools are already connected and the operator's machine policy permits
MCP.

1. **Do not edit `service_bindings.json` for `session_ledger_service`.** The session ledger is constructed by platform startup, not bound through `profile/config/service_bindings.json`; adding `session_ledger_service` there is invalid config because it is not a `ServiceName`. Verify the ledger with `<name> call service_interface::session_ledger_service::list_sources '{}'` and check whether `claude_code_local`, `claude_code_history`, or `claude_code_tasks` already have registered rows.
2. **Set `ledger_allowed_roots`** in `<clone>/profile/config/plugins/session_ledger_service.json` to include only the operator-approved local paths. For Claude Code these are usually `~/.claude/projects`, `~/.claude/history.jsonl`, and `~/.claude/tasks`; ask before adding any path that differs. Some seed profiles pre-populate these roots, but this file remains the consent boundary for filesystem ingestion. If you change this file, restart the solet before registering sources; the session-ledger service reads `ledger_allowed_roots` at construction time, and `reload_plugin_config` is not enough.
3. **Register any missing source rows** via `<name> call service_interface::session_ledger_service::register_source '{"source_kind": "claude_code_local", "root_uri": "~/.claude/projects"}'` for project transcripts. If desired, also register `source_kind="claude_code_history", root_uri="~/.claude/history.jsonl"` and `source_kind="claude_code_tasks", root_uri="~/.claude/tasks"` with the same command shape. Registration is idempotent; an existing row should be treated as already wired, not an error.
4. **Run an initial backfill** with `<name> call service_interface::session_ledger_service::trigger_poll '{}'` so existing history is ingested immediately, then verify with `<name> call service_interface::session_ledger_service::list_sources '{}'`, `<name> call service_interface::session_ledger_service::list_sessions '{"vendor": "claude_code", "limit": 5}'`, or `<name> call service_interface::session_ledger_service::search_sessions '{"query": "<known recent topic>", "limit": 5}'` that rows actually landed.

If the operator also uses Codex, see `plugins/codex_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` for the equivalent source rows. The shared setup is `ledger_allowed_roots`, not a session-ledger service binding.

On decline: stop, no partial wiring — the consent boundary is real and a no is respected. But state what it means, and keep it visible: without ingestion this solet has no session memory at all, and the platform treats ledger functionality as core correctness, not an optional extra (operator ruling 2026-08-02, quoted in the hydration runbook's ingestion disclosure). The deployment report card (`plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md`) carries the decline as an unconfigured core row on every future card; re-offer at natural moments rather than silently accepting the gap as permanent.
