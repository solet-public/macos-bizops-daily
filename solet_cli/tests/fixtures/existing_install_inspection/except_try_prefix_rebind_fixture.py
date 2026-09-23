"""AST-only adversary: handlers can observe a rebind from a try-body prefix."""

from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

try:
    slot = forbidden  # noqa: F811
    raise ValueError
except ValueError:
    slot()
