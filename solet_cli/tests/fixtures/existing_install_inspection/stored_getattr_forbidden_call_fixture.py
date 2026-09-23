"""AST-only stored literal-getattr-reference adversary."""

import target_adapter as x

target = getattr(x, "target_adapter")  # noqa: B009 - deliberate AST adversary
target()
