import forbidden_module
import safe_adapter


class Suppress:
    async def __aenter__(self) -> "Suppress":
        return self

    async def __aexit__(self, typ: object, val: object, tb: object) -> bool:
        return False


async def run() -> None:
    slot = safe_adapter.harmless_method
    async with Suppress():
        slot = forbidden_module.target_adapter
        1 / 0  # noqa: B018 - deliberate intermediate fixture
        slot = safe_adapter.harmless_method
    slot()
