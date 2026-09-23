"""Exception-handler state collection for the inspection call collector."""

from __future__ import annotations

import ast
from collections.abc import Callable

State = dict[str, str | ast.expr | None]
CollectExpression = Callable[[ast.AST, State, set[str]], None]
CollectSuite = Callable[[list[ast.stmt], State, set[str]], tuple[State, list[State]]]


def collect_handler_outcomes(
    statement: ast.Try | ast.TryStar,
    handler_entry: State,
    called: set[str],
    collect_expression: CollectExpression,
    collect_suite: CollectSuite,
    merge_states: Callable[..., State],
) -> list[State]:
    """Collect alternative except paths and sequential except-star paths."""
    outcomes: list[State] = []
    handler_state = dict(handler_entry)
    for handler in statement.handlers:
        entering = (
            merge_states(handler_entry, handler_state)
            if isinstance(statement, ast.TryStar)
            else handler_entry
        )
        outcome = dict(entering)
        if handler.type is not None:
            collect_expression(handler.type, outcome, called)
        if handler.name:
            outcome[handler.name] = None
        outcome, prefixes = collect_suite(handler.body, outcome, called)
        outcomes.extend((outcome, *prefixes))
        if isinstance(statement, ast.TryStar):
            handler_state = merge_states(handler_state, outcome, *prefixes)
            outcomes.append(dict(handler_state))
    return outcomes
