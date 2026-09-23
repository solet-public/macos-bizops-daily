from safe_adapter import harmless_method as slot  # noqa: F811
from target_adapter import target_adapter as forbidden

slot = forbidden  # noqa: F811
slot()
