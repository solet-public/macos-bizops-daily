"""Static call-symbol collection for the inspection preservation smoke."""

from __future__ import annotations

import ast
from collections.abc import Sequence

from _existing_install_inspection_flow import (
    assigned_names,
    contains_finally_transfer,
    has_reachable_loop_break,
    nested_blocks,
    terminates_unconditionally,
)
from _existing_install_inspection_generics import type_parameter_expressions
from _existing_install_inspection_handlers import collect_handler_outcomes


def called_symbols(
    tree: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> set[str]:
    """Collect statically resolvable call forms without interpreting Python."""
    called: set[str] = set()
    _collect_scope_symbols(tree, {}, called)
    return called


def _collect_scope_symbols(
    scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    enclosing_current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    """Walk one lexical scope forward, preserving statement-order bindings."""
    current = dict(enclosing_current)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        current.update(dict.fromkeys(_argument_names(scope.args)))
    _collect_block(scope.body, current, called)


def _collect_statement(
    statement: ast.stmt,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    if _collect_simple_statement(statement, current, called):
        return
    if _collect_compound_statement(statement, current, called):
        return
    _collect_statement_expressions(statement, current, called)
    _mark_statement_binders_dynamic(statement, current)


def _collect_simple_statement(
    statement: ast.stmt,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> bool:
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        _update_imports(statement, current)
        return True
    if isinstance(statement, ast.Assign):
        _collect_expression(statement.value, current, called)
        resolved = _called_symbol(statement.value, current)
        for target in statement.targets:
            _collect_and_apply_assignment_target(target, resolved, current, called)
        return True
    if isinstance(statement, ast.AnnAssign):
        if statement.value is not None:
            _collect_expression(statement.value, current, called)
        _collect_expression(statement.target, current, called)
        _collect_expression(statement.annotation, current, called)
        _mark_dynamic(statement.target, current)
        return True
    if isinstance(statement, ast.AugAssign):
        _collect_expression(statement.target, current, called)
        _collect_expression(statement.value, current, called)
        _mark_dynamic(statement.target, current)
        return True
    return False


def _collect_compound_statement(
    statement: ast.stmt,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> bool:
    if _collect_definition_statement(statement, current, called):
        return True
    return _collect_control_statement(statement, current, called)


def _collect_definition_statement(
    statement: ast.stmt,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> bool:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _collect_header_symbols(_function_header_expressions(statement), current, called)
        _collect_scope_symbols(statement, current, called)
        current.update(dict.fromkeys(_outer_rebind_names(statement)))
        current[statement.name] = None
        return True
    if isinstance(statement, ast.ClassDef):
        _collect_header_symbols(_class_header_expressions(statement), current, called)
        _collect_scope_symbols(statement, current, called)
        current.update(dict.fromkeys(_outer_rebind_names(statement)))
        current[statement.name] = None
        return True
    if isinstance(statement, ast.TypeAlias):
        _collect_header_symbols(type_parameter_expressions(statement.type_params), current, called)
        _collect_expression(statement.value, current, called)
        _mark_dynamic(statement.name, current)
        return True
    return False


def _collect_control_statement(
    statement: ast.stmt,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> bool:
    if isinstance(statement, ast.If):
        _collect_expression(statement.test, current, called)
        _collect_conditional_blocks((statement.body, statement.orelse), current, called)
        return True
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        _collect_expression(statement.iter, current, called)
        _collect_for(statement, current, called)
        return True
    if isinstance(statement, ast.While):
        _collect_expression(statement.test, current, called)
        _collect_while(statement, current, called)
        return True
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        entering = dict(current)
        normal = dict(current)
        _apply_with_header_bindings(statement, normal, called)
        _collect_block(statement.body, normal, called)
        _merge_conditional_states(current, [entering, normal])
        current.update(dict.fromkeys(assigned_names(statement.body)))
        return True
    if isinstance(statement, (ast.Try, ast.TryStar)):
        _collect_try(statement, current, called)
        return True
    if isinstance(statement, ast.Match):
        _collect_match(statement, current, called)
        return True
    return False


def _collect_header_symbols(
    expressions: tuple[ast.expr, ...],
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    for expression in expressions:
        _collect_expression(expression, current, called)


def _collect_expression(
    expression: ast.AST,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    if isinstance(expression, ast.Lambda):
        _collect_lambda(expression, current, called)
        return
    if isinstance(expression, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        _collect_comprehension(expression, current, called)
        return
    if isinstance(expression, ast.NamedExpr):
        _collect_expression(expression.value, current, called)
        _mark_dynamic(expression.target, current)
        return
    if isinstance(expression, ast.Call):
        _collect_expression(expression.func, current, called)
        called.add(_called_symbol(expression.func, current))
        for argument in (*expression.args, *(keyword.value for keyword in expression.keywords)):
            _collect_expression(argument, current, called)
        return
    for child in ast.iter_child_nodes(expression):
        _collect_expression(child, current, called)


def _collect_lambda(
    node: ast.Lambda,
    enclosing_current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    for default in (
        *node.args.defaults,
        *(value for value in node.args.kw_defaults if value is not None),
    ):
        _collect_expression(default, enclosing_current, called)
    current = enclosing_current | dict.fromkeys(_argument_names(node.args))
    _collect_expression(node.body, current, called)


def _collect_comprehension(
    node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    enclosing_current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    _collect_comprehension_pass(node, enclosing_current, called)
    enclosing_current.update(dict.fromkeys(_walrus_targets(node)))
    # A walrus escaping the comprehension can affect a later logical pass.
    _collect_comprehension_pass(node, enclosing_current, called)
    enclosing_current.update(dict.fromkeys(_walrus_targets(node)))


def _collect_comprehension_pass(
    node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    enclosing_current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    current = dict(enclosing_current)
    for generator in node.generators:
        _collect_expression(generator.iter, current, called)
        _collect_and_apply_assignment_target(generator.target, "<dynamic>", current, called)
        for condition in generator.ifs:
            _collect_expression(condition, current, called)
    if isinstance(node, ast.DictComp):
        _collect_expression(node.key, current, called)
        _collect_expression(node.value, current, called)
    else:
        _collect_expression(node.elt, current, called)


def _collect_block(
    statements: list[ast.stmt],
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    _collect_block_prefixes(statements, current, called)


def _collect_block_prefixes(
    statements: list[ast.stmt],
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> list[dict[str, str | ast.expr | None]]:
    """Collect live statements and retain the state after every live prefix."""
    outcome, prefixes = _collect_suite_prefixes(statements, current, called, capture_nested=False)
    current.clear()
    current.update(outcome)
    return prefixes


def _collect_suite_prefixes(
    statements: list[ast.stmt],
    entering: dict[str, str | ast.expr | None],
    called: set[str],
    *,
    capture_nested: bool,
) -> tuple[dict[str, str | ast.expr | None], list[dict[str, str | ast.expr | None]]]:
    """Collect every reachable suite prefix, including nested escape points."""
    outcome = dict(entering)
    prefixes: list[dict[str, str | ast.expr | None]] = []
    for statement in statements:
        statement_entering = dict(outcome)
        _collect_statement(statement, outcome, called)
        prefixes.append(dict(outcome))
        if capture_nested:
            prefixes.extend(_collect_nested_prefixes(statement, statement_entering, called))
        if _terminates_unconditionally(statement):
            break
    return outcome, prefixes


def _collect_try_suite_prefixes(
    statements: list[ast.stmt],
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> tuple[dict[str, str | ast.expr | None], list[dict[str, str | ast.expr | None]]]:
    return _collect_suite_prefixes(statements, current, called, capture_nested=True)


def _collect_conditional_blocks(
    blocks: tuple[list[ast.stmt], ...],
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    outcomes: list[dict[str, str | ast.expr | None]] = []
    for block in blocks:
        outcome = dict(current)
        _collect_block(block, outcome, called)
        outcomes.append(outcome)
    _merge_conditional_states(current, outcomes)


def _collect_try(
    statement: ast.Try | ast.TryStar,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    initial = dict(current)
    body_outcome, body_prefixes = _collect_suite_prefixes(
        statement.body, initial, called, capture_nested=True
    )
    normal_outcome, orelse_prefixes = _collect_suite_prefixes(
        statement.orelse, body_outcome, called, capture_nested=True
    )
    handler_entry = _merged_state(initial, *body_prefixes)
    outcomes = [
        initial,
        handler_entry,
        normal_outcome,
        *body_prefixes,
        *orelse_prefixes,
    ]
    outcomes.extend(
        collect_handler_outcomes(
            statement,
            handler_entry,
            called,
            _collect_expression,
            _collect_try_suite_prefixes,
            _merged_state,
        )
    )
    _merge_conditional_states(current, outcomes)
    if contains_finally_transfer(statement.finalbody):
        final_outcome = dict(current)
        _collect_block(statement.finalbody, final_outcome, called)
        current.clear()
        current.update(final_outcome)
        return
    _collect_block(statement.finalbody, current, called)


def _collect_nested_prefixes(
    statement: ast.stmt,
    entering: dict[str, str | ast.expr | None],
    called: set[str],
) -> list[dict[str, str | ast.expr | None]]:
    """Recursively capture every nested compound prefix an exception can escape."""
    # _collect_try already merges every body, handler, and orelse prefix into
    # its outcome. Replaying those suites from the pre-try state here would
    # invent paths such as a handler's finally call before the try body ran.
    if isinstance(statement, (ast.Try, ast.TryStar)):
        return []
    prefixes: list[dict[str, str | ast.expr | None]] = []
    for block in nested_blocks(statement):
        outcome = dict(entering)
        _apply_nested_header_bindings(statement, outcome, called)
        for nested_statement in block:
            nested_entering = dict(outcome)
            _collect_statement(nested_statement, outcome, called)
            prefixes.append(dict(outcome))
            prefixes.extend(_collect_nested_prefixes(nested_statement, nested_entering, called))
            if _terminates_unconditionally(nested_statement):
                break
    return prefixes


def _apply_nested_header_bindings(
    statement: ast.stmt,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        _apply_loop_target(statement.target, current, called)
    elif isinstance(statement, (ast.With, ast.AsyncWith)):
        _apply_with_header_bindings(statement, current, called)


def _collect_for(
    statement: ast.For | ast.AsyncFor,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    initial = dict(current)
    outcome = _collect_loop_fixed_point(statement.target, statement.body, initial, called)
    _finish_loop(statement.body, statement.orelse, initial, outcome, current, called)


def _collect_while(
    statement: ast.While,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    initial = dict(current)
    outcome = _collect_loop_fixed_point(None, statement.body, initial, called)
    _collect_expression(statement.test, outcome, called)
    _finish_loop(statement.body, statement.orelse, initial, outcome, current, called)


def _collect_match(
    statement: ast.Match,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    _collect_expression(statement.subject, current, called)
    initial = dict(current)
    fallthrough = dict(current)
    outcomes: list[dict[str, str | ast.expr | None]] = [initial]
    for case in statement.cases:
        outcome = dict(fallthrough)
        outcome.update(dict.fromkeys(_pattern_names(case.pattern)))
        if case.guard is not None:
            _collect_expression(case.guard, outcome, called)
        selected = dict(outcome)
        _collect_block(case.body, selected, called)
        outcomes.append(selected)
        fallthrough = outcome
    _merge_conditional_states(current, [*outcomes, fallthrough])


def _collect_loop_body(
    target: ast.expr | None,
    body: list[ast.stmt],
    entering: dict[str, str | ast.expr | None],
    called: set[str],
) -> dict[str, str | ast.expr | None]:
    outcome = dict(entering)
    if target is not None:
        _apply_loop_target(target, outcome, called)
    _collect_block(body, outcome, called)
    return outcome


def _apply_loop_target(
    target: ast.expr,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    _collect_and_apply_assignment_target(target, "<dynamic>", current, called)


def _apply_with_header_bindings(
    statement: ast.With | ast.AsyncWith,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    for item in statement.items:
        _collect_expression(item.context_expr, current, called)
        if item.optional_vars is not None:
            _collect_and_apply_assignment_target(item.optional_vars, "<dynamic>", current, called)


def _collect_loop_fixed_point(
    target: ast.expr | None,
    body: list[ast.stmt],
    initial: dict[str, str | ast.expr | None],
    called: set[str],
) -> dict[str, str | ast.expr | None]:
    """Conservatively join loop iterations until their binding state stabilizes."""
    state = dict(initial)
    assigned = assigned_names(body)
    for _ in range(max(8, 2 * len(assigned) + 4)):
        outcome = _collect_loop_body(target, body, state, called)
        next_state = _merged_state(state, outcome)
        if next_state == state:
            return state
        state = next_state
    # Valid Python can contain arbitrarily long finite alias chains.  Falling
    # back to unresolved state is sound; raising here would make inspection crash.
    state.update(dict.fromkeys(assigned))
    return state


def _terminates_unconditionally(statement: ast.stmt) -> bool:
    return terminates_unconditionally(statement, _block_terminates)


def _block_terminates(statements: list[ast.stmt]) -> bool:
    return bool(statements) and _terminates_unconditionally(statements[-1])


def _finish_loop(
    body: list[ast.stmt],
    orelse: list[ast.stmt],
    initial: dict[str, str | ast.expr | None],
    outcome: dict[str, str | ast.expr | None],
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    pre_orelse = _merged_state(initial, outcome)
    post_orelse = dict(pre_orelse)
    _collect_block(orelse, post_orelse, called)
    final = (
        _merged_state(pre_orelse, post_orelse)
        if has_reachable_loop_break(body, _terminates_unconditionally)
        else post_orelse
    )
    current.clear()
    current.update(final)


def _merge_conditional_states(
    current: dict[str, str | ast.expr | None],
    outcomes: list[dict[str, str | ast.expr | None]],
) -> None:
    names: set[str] = set(current)
    for outcome in outcomes:
        names.update(outcome)
    for name in names:
        values = {outcome.get(name) for outcome in outcomes}
        current[name] = values.pop() if len(values) == 1 else None


def _merged_state(
    *outcomes: dict[str, str | ast.expr | None],
) -> dict[str, str | ast.expr | None]:
    merged: dict[str, str | ast.expr | None] = {}
    _merge_conditional_states(merged, list(outcomes))
    return merged


def _collect_statement_expressions(
    statement: ast.stmt,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    for child in ast.iter_child_nodes(statement):
        if isinstance(child, ast.expr):
            _collect_expression(child, current, called)


def _collect_and_apply_assignment_target(
    target: ast.expr,
    resolved: str,
    current: dict[str, str | ast.expr | None],
    called: set[str],
) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _collect_and_apply_assignment_target(element, resolved, current, called)
        return
    _collect_expression(target, current, called)
    if isinstance(target, ast.Name):
        current[target.id] = resolved if resolved != "<dynamic>" else None
        return
    _mark_dynamic(target, current)


def _mark_dynamic(target: ast.AST, current: dict[str, str | ast.expr | None]) -> None:
    current.update(dict.fromkeys(_binding_names(target)))


def _mark_statement_binders_dynamic(
    statement: ast.stmt, current: dict[str, str | ast.expr | None]
) -> None:
    current.update(dict.fromkeys(_binding_names(statement)))


def _binding_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(
        node,
        (
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.Lambda,
            ast.ListComp,
            ast.SetComp,
            ast.DictComp,
            ast.GeneratorExp,
        ),
    ):
        return ()
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return (node.id,)
    direct = _direct_binding_names(node)
    if direct is not None:
        return direct
    return tuple(name for child in ast.iter_child_nodes(node) for name in _binding_names(child))


def _direct_binding_names(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.ExceptHandler):
        return (node.name,) if node.name else ()
    if isinstance(node, ast.MatchAs):
        nested = _binding_names(node.pattern) if node.pattern else ()
        return (*nested, *((node.name,) if node.name else ()))
    if isinstance(node, ast.MatchStar):
        return (node.name,) if node.name else ()
    if isinstance(node, ast.MatchMapping):
        return _mapping_binding_names(node)
    return None


def _mapping_binding_names(node: ast.MatchMapping) -> tuple[str, ...]:
    nested = tuple(name for pattern in node.patterns for name in _binding_names(pattern))
    return (*nested, *((node.rest,) if node.rest else ()))


def _update_imports(
    statement: ast.Import | ast.ImportFrom,
    current: dict[str, str | ast.expr | None],
) -> None:
    if isinstance(statement, ast.Import):
        _update_regular_imports(statement, current)
        return
    _update_from_import(statement, current)


def _update_regular_imports(
    statement: ast.Import,
    current: dict[str, str | ast.expr | None],
) -> None:
    for item in statement.names:
        current[item.asname or item.name.split(".", 1)[0]] = item.name


def _update_from_import(
    statement: ast.ImportFrom,
    current: dict[str, str | ast.expr | None],
) -> None:
    if len(statement.names) == 1 and statement.names[0].name == "*":
        current.update(dict.fromkeys(current))
        return
    if statement.module is None:
        current.update(dict.fromkeys(item.asname or item.name for item in statement.names))
        return
    for item in statement.names:
        current[item.asname or item.name] = f"{statement.module}.{item.name}"


def _walrus_targets(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.NamedExpr):
        return _binding_names(node.target)
    if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ()
    return tuple(name for child in ast.iter_child_nodes(node) for name in _walrus_targets(child))


def _outer_rebind_names(scope: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(scope):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            names.extend(node.names)
    return tuple(names)


def _function_header_expressions(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.expr, ...]:
    return (
        *node.decorator_list,
        *_function_argument_expressions(node.args),
        *((node.returns,) if node.returns is not None else ()),
        *type_parameter_expressions(node.type_params),
    )


def _function_argument_expressions(arguments: ast.arguments) -> tuple[ast.expr, ...]:
    positional = (*arguments.args, *arguments.posonlyargs)
    defaults = (
        *arguments.defaults,
        *(value for value in arguments.kw_defaults if value is not None),
    )
    return (
        *defaults,
        *_argument_annotations(positional),
        *_argument_annotation(arguments.vararg),
        *_argument_annotations(arguments.kwonlyargs),
        *_argument_annotation(arguments.kwarg),
    )


def _argument_annotations(arguments: Sequence[ast.arg]) -> tuple[ast.expr, ...]:
    return tuple(argument.annotation for argument in arguments if argument.annotation is not None)


def _argument_annotation(argument: ast.arg | None) -> tuple[ast.expr, ...]:
    return (argument.annotation,) if argument and argument.annotation is not None else ()


def _class_header_expressions(node: ast.ClassDef) -> tuple[ast.expr, ...]:
    return (
        *node.decorator_list,
        *node.bases,
        *(keyword.value for keyword in node.keywords),
        *type_parameter_expressions(node.type_params),
    )


def _argument_names(arguments: ast.arguments) -> tuple[str, ...]:
    positional = (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    variable = tuple(argument for argument in (arguments.vararg, arguments.kwarg) if argument)
    return tuple(argument.arg for argument in (*positional, *variable))


def _pattern_names(pattern: ast.pattern) -> tuple[str, ...]:
    return _binding_names(pattern)


def _called_symbol(
    node: ast.expr,
    resolutions: dict[str, str | ast.expr | None],
    resolving: frozenset[str] = frozenset(),
) -> str:
    """Resolve direct calls through one scope-local table, never general data flow.

    A bare name may follow one unambiguous direct ``Assign`` in its lexical
    scope. Conditional, repeated, shadowing, cross-scope, and computed
    bindings remain dynamic.
    """
    if isinstance(node, ast.Name):
        return _name_symbol(node, resolutions, resolving)
    if isinstance(node, ast.Attribute):
        return _attribute_symbol(node, resolutions, resolving)
    if forwarded := _forwarded_callable(node, resolutions, resolving):
        return forwarded
    if literal_getattr := _literal_getattr(node, resolutions, resolving):
        return literal_getattr
    if methodcaller := _literal_methodcaller(node, resolutions, resolving):
        return methodcaller
    return "<dynamic>"


def _name_symbol(
    node: ast.Name,
    resolutions: dict[str, str | ast.expr | None],
    resolving: frozenset[str],
) -> str:
    resolution = resolutions.get(node.id)
    if resolution is None or node.id in resolving:
        return "<dynamic>"
    if isinstance(resolution, str):
        return resolution
    resolved = _called_symbol(resolution, resolutions, resolving | {node.id})
    return resolved if resolved != "<dynamic>" else "<dynamic>"


def _attribute_symbol(
    node: ast.Attribute,
    resolutions: dict[str, str | ast.expr | None],
    resolving: frozenset[str],
) -> str:
    value = _called_symbol(node.value, resolutions, resolving)
    return f"{value}.{node.attr}" if value != "<dynamic>" else value


def _forwarded_callable(
    node: ast.expr,
    resolutions: dict[str, str | ast.expr | None],
    resolving: frozenset[str],
) -> str | None:
    if not isinstance(node, ast.Call) or not node.args:
        return None
    wrapper = _called_symbol(node.func, resolutions, resolving)
    if wrapper not in {"functools.partial", "functools.partialmethod"}:
        return None
    return _called_symbol(node.args[0], resolutions, resolving)


def _literal_getattr(
    node: ast.expr,
    resolutions: dict[str, str | ast.expr | None],
    resolving: frozenset[str],
) -> str | None:
    if not isinstance(node, ast.Call) or not _is_builtin_getattr(node.func, resolutions):
        return None
    if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
        return None
    if not isinstance(node.args[1].value, str):
        return None
    receiver = _called_symbol(node.args[0], resolutions, resolving)
    return f"{receiver}.{node.args[1].value}" if receiver != "<dynamic>" else receiver


def _is_builtin_getattr(node: ast.expr, resolutions: dict[str, str | ast.expr | None]) -> bool:
    return isinstance(node, ast.Name) and node.id == "getattr" and node.id not in resolutions


def _literal_methodcaller(
    node: ast.expr,
    resolutions: dict[str, str | ast.expr | None],
    resolving: frozenset[str],
) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if _called_symbol(node.func, resolutions, resolving) != "operator.methodcaller":
        return None
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    if not isinstance(node.args[0].value, str):
        return None
    return f"methodcaller.{node.args[0].value}"
