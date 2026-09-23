This is a process_error recovery vertex: the previous tool call failed.
An empty actions array ends the flow silently.

Recovery goal:
- Explain what went wrong.
- If the error includes "JSON parse error" and "Raw LLM response", the previous response had
  malformed JSON. Emit the corrected JSON with the same action and arguments. Count braces
  carefully: `{` opens an object, `}` closes it. The closing sequence for a single-action
  response is `}}]}` (close arguments, close action item, close array, close root).
- For other errors, ask a targeted clarification or propose a safe default.

Do not invent a successful result. Do not attempt a retry unless it is safe to do so without user confirmation.
