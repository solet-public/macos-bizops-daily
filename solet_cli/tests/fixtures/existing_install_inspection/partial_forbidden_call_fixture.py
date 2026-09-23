"""AST-only adversaries for call-forwarding preservation boundaries."""

import functools

import target_adapter as x

functools.partial(getattr(x, "target_adapter"))()  # noqa: B009 - deliberate AST adversary
functools.partialmethod(getattr(x, "target_adapter"))()  # noqa: B009 - deliberate AST adversary
functools.partial(functools.partial(getattr(x, "target_adapter")))()  # noqa: B009 - deliberate AST adversary
