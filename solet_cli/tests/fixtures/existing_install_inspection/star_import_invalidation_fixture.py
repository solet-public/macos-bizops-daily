import safe_adapter

slot = safe_adapter.harmless_method
from forbidden_module import *  # noqa: E402, F403, I001 - deliberate invalidation fixture

slot()
