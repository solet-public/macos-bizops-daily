"""Process registry constants.

Contains constants used across the process registry module,
including the list of core processes that must be included in every system prompt.
"""

# Core processes that MUST be included in every system prompt.
# The active IO plugin's post_message is injected dynamically per-request.
# All other processes (blob_storage, address_book, knowledge CRUD, discovery, etc.)
# are discoverable via the model's own actions and do NOT need to be in the system prompt.
SYSTEM_PROMPT_PROCESS_KEYS: frozenset[str] = frozenset({
    "service_interface::thinking_service::upsert_plan",
    "service_interface::memory_service::recall",
    "service_interface::knowledge_service::search",
    "service_interface::thinking_service::create_authored_artifact",
    "service_interface::thinking_service::create_work_manifest",
    "service_interface::thinking_service::patch_work_manifest",
    "service_interface::thinking_service::register_authored_work_breakdown_structure",
    "service_interface::thinking_service::patch_work_breakdown_structure",
})

# Semantic ordering for system prompt display.
# Planning core first, then memory, then knowledge, then artifact/WBS lifecycle.
# IO plugin post_message is appended dynamically after these.
SYSTEM_PROMPT_PROCESS_ORDER: tuple[str, ...] = (
    "service_interface::thinking_service::upsert_plan",
    "service_interface::memory_service::recall",
    "service_interface::knowledge_service::search",
    "service_interface::thinking_service::create_authored_artifact",
    "service_interface::thinking_service::create_work_manifest",
    "service_interface::thinking_service::patch_work_manifest",
    "service_interface::thinking_service::register_authored_work_breakdown_structure",
    "service_interface::thinking_service::patch_work_breakdown_structure",
)
