import forbidden_module
import safe_adapter


def f() -> object:
    slot = safe_adapter.harmless_method
    try:
        raise ValueError
    except ValueError:
        slot = forbidden_module.target_adapter
        1 / 0  # noqa: B018 - deliberate potential-exception fixture
        slot = safe_adapter.harmless_method
    finally:
        slot()
        return slot  # noqa: B012 - deliberate finally-transfer fixture
