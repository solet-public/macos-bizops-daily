"""ACT-R Memory Plugin.

Provides biologically-inspired memory with decay, retrieval strengthening,
and automatic consolidation. Wraps the memory_service interface with
scheduled operations and CLI access.
"""

from .plugin import ACTRMemoryPlugin

__all__ = ["ACTRMemoryPlugin"]
