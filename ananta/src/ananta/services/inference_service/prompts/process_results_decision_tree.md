You received action_result from a completed action. Decide what to do next.

STEP 1: CHECK THE ORIGINAL REQUEST

Look at flow_input.original_input to understand USER INTENT:

ACTION INTENT (user wants to DO something):
  - 'generate...', 'create...', 'make...', 'produce...'
  - 'schedule...', 'set...', 'run...'
  - Imperative commands without question words

INFO INTENT (user wants INFORMATION):
  - 'what...?', 'which...?', 'how many...?', 'list...'
  - 'find...', 'search for...', 'show me...'
  - Questions about capabilities or availability

STEP 2: IDENTIFY THE RESULT TYPE

Check action_result - what keys does it contain?

- If action_result contains 'processes' key (array of process objects):
  This is a DISCOVERY result. Check intent to decide CASE A1 or A2.

- If action_result does NOT contain 'processes' key:
  This is a TOOL result. Proceed to CASE B or CASE C based on success/failure.

STEP 3: TAKE ACTION

CASE A1 - Discovery + ACTION INTENT (user wants to DO something):
  → TWO-STEP WORKFLOW REQUIRED:
  → STEP 1: Select the best process from action_result.processes using metadata.description and metadata.parameters
  → STEP 2: Call get_process_schema with the selected process_key to retrieve the invocation schema
  → Discovery results have LIGHTWEIGHT metadata only - you MUST call get_process_schema before executing
  → Return get_process_schema action with process_key argument

CASE A2 - Discovery + INFO INTENT (user wants information about capabilities):
  → Resolve the active IO namespace with query_process_registry, then use plugin::<namespace>::post_message to REPORT the findings
  → List the discovered processes with their names and descriptions
  → DO NOT execute any of the discovered processes
  → arguments MUST include: message (list of what was found); SESSION_AWARE binds the session from context

CASE B - Tool completed successfully (no 'processes' key AND result looks successful):
  → Resolve the active IO namespace with query_process_registry, then use plugin::<namespace>::post_message to tell the user what was accomplished
  → CRITICAL: Copy ACTUAL VALUES from action_result into your message text
  → NEVER use placeholder syntax like ${...}, {{...}}, <<...>>, or variable references
  → Example: If action_result.file_path is 'data/blobs/gen-abc', write 'data/blobs/gen-abc' literally
  → arguments MUST include: message (the actual text to show the user); SESSION_AWARE binds the session from context

CASE C - Tool failed (result contains error or failure indicators):
  → Resolve the active IO namespace with query_process_registry, then use plugin::<namespace>::post_message to inform the user of the error
  → arguments MUST include: message (the error explanation for the user); SESSION_AWARE binds the session from context

OUTPUT FORMAT:
  - reason: Brief internal note about why this step is taken (NOT the user message)
  - arguments.message: The actual text content to display to the user
  - SESSION_AWARE binds the session ID from context; never pass arguments.session_id

IMPORTANT: For discovery results, use get_process_schema to fetch the invocation schema before executing. Build 'process' object using provider_type/provider/function_name from available_processes or action_result.processes. Never invent process identifiers.
If this result represents a novel technique or a correction to existing knowledge, consider updating the relevant knowledge base via edit_file or create_file.
