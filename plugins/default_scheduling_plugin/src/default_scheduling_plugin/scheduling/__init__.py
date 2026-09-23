"""Scheduling management module.

This module handles APScheduler lifecycle, job management, and event handling.
"""

from .scheduler_manager import SchedulerManager

RELOAD_SAFE = True

__all__ = ["SchedulerManager"]
