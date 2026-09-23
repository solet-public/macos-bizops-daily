"""AST-only adversary: a break preserves the pre-else binding on one exit path."""

from safe_adapter import harmless_method as safe
from target_adapter import target_adapter as slot

for _ in (1,):
    break
else:
    slot = safe  # noqa: F811
slot()
