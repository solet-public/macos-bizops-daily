"""Platform-health diagnostic plugin.

Single @platform_process verb (``execute_registry_sweep``) that iterates the
live process registry and invokes every registered process with sentinel
arguments to surface registration, schema, and SQL-type errors that unit
smokes miss.
"""

from platform_health_plugin.plugin import PlatformHealthPlugin

__all__ = ["PlatformHealthPlugin"]
