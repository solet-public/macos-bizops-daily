"""AST-only adversary: a body call has a distinct later-iteration state."""

import target_adapter as forbidden_module
from safe_adapter import harmless_method as slot

for _ in (0, 1):
    slot()
    slot = forbidden_module.target_adapter  # noqa: F811
