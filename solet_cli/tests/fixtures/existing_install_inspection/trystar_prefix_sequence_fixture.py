import forbidden_module
import safe_adapter


def f(group: BaseExceptionGroup[Exception]) -> None:
    slot = safe_adapter.harmless_method
    try:
        raise group
    except* ValueError:
        slot = forbidden_module.target_adapter
        1 / 0  # noqa: B018 - deliberate prefix fixture
        slot = safe_adapter.harmless_method
    except* TypeError:
        pass
    except* LookupError:
        pass
    except* OSError:
        slot()
