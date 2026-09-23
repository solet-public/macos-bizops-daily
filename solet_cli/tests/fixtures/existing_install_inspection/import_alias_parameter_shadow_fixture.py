from safe_adapter import harmless_method as slot  # noqa: F401, F811
from target_adapter import target_adapter as forbidden


def invoke(slot: object) -> None:  # noqa: F811
    slot()  # type: ignore[operator]


invoke(forbidden)
