import safe_adapter
from target_adapter import target_adapter as forbidden


def resolve() -> object:
    slot = safe_adapter.harmless_method
    try:
        slot = forbidden
        raise ValueError
    finally:
        slot()
        return slot  # noqa: B012 - deliberate preservation fixture
