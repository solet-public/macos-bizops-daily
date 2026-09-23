__version__ = "0.1.0"

# Import only what's needed to avoid circular dependencies


# Core imports are done lazily when needed

__all__: list[str] = [
    "__version__",
]


def get_app_runner() -> object:
    from ananta.app_runner import run_async_app

    return run_async_app


def get_cli() -> object:
    """Lazy import of CLI module to avoid circular dependencies"""
    from ananta import cli

    return cli
