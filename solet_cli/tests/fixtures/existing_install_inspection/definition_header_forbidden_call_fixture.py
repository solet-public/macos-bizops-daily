"""AST-only decorator, default, annotation, and class-header adversaries."""

import target_adapter as x


@x.target_adapter()
def decorated(
    value: x.target_adapter() = x.target_adapter(),  # noqa: B008 - deliberate AST adversary
    *,
    keyed: x.target_adapter() = x.target_adapter(),  # noqa: B008 - deliberate AST adversary
) -> x.target_adapter():
    pass


class Header(x.target_adapter(), metaclass=x.target_adapter()):
    pass
