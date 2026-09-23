"""Postgres backend machinery used by pgvector_service_plugin.

Owned per-plugin (no platform-layer sharing). Vector-specific; this
plugin has no need for the broader state-management surface.
"""
