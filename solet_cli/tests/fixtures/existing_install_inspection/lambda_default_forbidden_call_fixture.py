"""AST-only adversary: lambda defaults execute in the enclosing scope."""

from target_adapter import target_adapter as forbidden

callback = lambda value=forbidden(): value  # noqa: B008, E731
