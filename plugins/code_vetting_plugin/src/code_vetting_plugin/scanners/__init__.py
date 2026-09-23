"""Deterministic L1 scanner family — one module per tool/dimension group.

Each module exposes a ``scan(tree, run_id)`` (or tool-specific) entry point
returning a :class:`~code_vetting_plugin.coverage.ScannerResult`.
"""
