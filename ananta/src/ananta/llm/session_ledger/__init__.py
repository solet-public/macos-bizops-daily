"""LLM session ledger — durable transcript persistence across LLM tools.

Stores normalized session events from heterogeneous sources (Claude Code,
Codex, ChatGPT export, agent_messaging) behind a stable schema. Events
land with full content; the 2026-06-14 eradication PR1a removed the
pre-rip ingest-time scan surface per the secretgate-full-eradication
design record. See the original session-ledger implementation spec
(both dev-checkout workbench records — not part of the shipped tree).
"""
