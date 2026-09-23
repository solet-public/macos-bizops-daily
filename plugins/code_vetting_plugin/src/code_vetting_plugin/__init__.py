"""Deterministic AI code-vetting scanners (Stream L1).

Working-tree-driven scanners that wrap external security/quality tools and the
platform's own gates, normalizing every result into the F1 finding schema
(``workbench/2026-07-19_vetting_finding_schema_v1.md``). See ``cli.py`` for the
entry point and ``runner.py`` for orchestration.
"""
