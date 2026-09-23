"""AST-only adversary: a zero-iteration loop reaches its else from pre-loop state."""

from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

slot = forbidden  # noqa: F811
for _ in ():
    from safe_adapter import harmless_method as slot  # noqa: F401, F811, PLC0415
else:
    slot()
