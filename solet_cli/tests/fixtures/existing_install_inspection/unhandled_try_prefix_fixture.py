import forbidden_module
import safe_adapter


def resolve() -> object:
    slot = safe_adapter.harmless_method
    try:
        slot = forbidden_module.target_adapter
        raise KeyError
    except ValueError:
        slot = safe_adapter.harmless_method
    except TypeError:
        slot = safe_adapter.harmless_method
    else:
        slot = safe_adapter.harmless_method
    finally:
        slot()
        return slot  # noqa: B012 - deliberate preservation fixture
