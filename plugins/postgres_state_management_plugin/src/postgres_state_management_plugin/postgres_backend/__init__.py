"""Postgres backend machinery used by postgres_state_management_plugin.

Owned per-plugin (no platform-layer sharing). When evolved, evolve only
this copy — `rds_postgres_state_management_plugin` has its own copy.
"""
