"""AST-only adversary: comprehension walrus bindings escape to the enclosure."""

from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

[(slot := forbidden) for _ in (0,)]  # noqa: F811
slot()
