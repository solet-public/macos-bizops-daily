import forbidden_module
import safe_adapter


def f(group: BaseExceptionGroup[Exception]) -> None:
    slot = safe_adapter.harmless_method
    try:
        raise group
    except* ValueError:
        slot = forbidden_module.target_adapter
    except* TypeError:
        slot()
