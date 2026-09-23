"""LLM session ledger source for the agent_messaging tables."""

__version__ = "1.0.0"

PLUGIN_NAME = "agent_messaging_session_source_plugin"
PLUGIN_DISPLAY_NAME = "Agent Messaging Session Source"
PLUGIN_DESCRIPTION = (
    "Reads core__agent_thread + core__agent_message as ingest sessions for the LLM "
    "session ledger. Works in both local and cloud profiles."
)
PLUGIN_AUTHOR = "Ananta AI"
PLUGIN_DEPENDENCIES: list[str] = []
