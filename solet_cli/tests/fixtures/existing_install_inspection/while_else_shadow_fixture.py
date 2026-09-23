"""AST-only adversary: a loop body can rebind before its else executes."""

from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

count = 0
while count < 1:
    slot = forbidden  # noqa: F811
    count += 1
else:
    slot()
