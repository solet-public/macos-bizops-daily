"""PEP 695 type-parameter expression collection for the call collector."""

from __future__ import annotations

import ast
from typing import cast


def type_parameter_expressions(type_params: list[ast.type_param]) -> tuple[ast.expr, ...]:
    """Return bounds and defaults that can execute while defining a generic."""
    expressions: list[ast.expr] = []
    for parameter in type_params:
        if isinstance(parameter, ast.TypeVar) and parameter.bound is not None:
            expressions.append(parameter.bound)
        default = cast(ast.expr | None, getattr(parameter, "default_value", None))
        if default is not None:
            expressions.append(default)
    return tuple(expressions)
