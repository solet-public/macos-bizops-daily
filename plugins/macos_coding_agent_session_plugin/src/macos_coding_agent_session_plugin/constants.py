"""Constants for the macOS coding-agent session plugin."""

from __future__ import annotations

PLUGIN_NAME = "macos_coding_agent_session_plugin"

ENV_SOLET_NAME = "SOLET_NAME"
ENV_AGENT_IDENTITY = "AGENT_IDENTITY"
ENV_AGENT_INSTANCE_ID = "AGENT_INSTANCE_ID"

DEFAULT_AGENT_IDENTITY = "claude_code"

# Grace window between SIGTERM and SIGKILL on terminate_bridge.
DEFAULT_TERMINATE_GRACE_SECONDS = 5.0
DEFAULT_TERMINATE_POLL_INTERVAL_SECONDS = 0.1

# FSEvents watcher coalescing latency (seconds). Sub-second latency
# keeps the operator-observed reconnect window short; the FSEventStream
# API coalesces rapid changes regardless.
DEFAULT_FSEVENTS_LATENCY_SECONDS = 0.25

# Result-type strings for EdgeProcessDefinition wiring.
RESULT_TYPE_SPAWN = "coding_agent_session_bridge_spawn_result"
RESULT_TYPE_TERMINATE = "coding_agent_session_bridge_terminate_result"
RESULT_TYPE_RESTART = "coding_agent_session_bridge_restart_result"
RESULT_TYPE_LIST = "coding_agent_session_bridge_list_result"

STATUS_FAILED = "failed"
