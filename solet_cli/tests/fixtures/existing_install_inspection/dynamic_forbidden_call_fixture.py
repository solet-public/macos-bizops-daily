"""AST-only adversarial fixture for the inspection preservation boundary."""

import target_adapter as x

getattr(x, "target_adapter")()  # noqa: B009 - deliberate AST adversary
