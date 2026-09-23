from safe_adapter import harmless_method as slot
from target_adapter import target_adapter as forbidden

slot: object = forbidden  # noqa: F811
slot()  # type: ignore[operator]
