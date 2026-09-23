from safe_adapter import harmless_method as slot

try:
    raise RuntimeError
except RuntimeError as slot:  # noqa: F811
    slot()
