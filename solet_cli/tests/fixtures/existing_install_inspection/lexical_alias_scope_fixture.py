"""AST-only proof that nested imports do not rewrite outer aliases."""

from safe_paths import ManagerPaths

ManagerPaths.resolve()


def inner() -> None:
    import target_adapter as ManagerPaths  # noqa: N812 - deliberate AST adversary

    ManagerPaths.resolve()


ManagerPaths.resolve()
