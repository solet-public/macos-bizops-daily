"""State Management - State persistence and async job lifecycle.

This package provides state management services including:
- StateManager: Central state persistence and retrieval
- AsyncJobManager: Asynchronous job lifecycle management

The state package handles all state-related concerns including persistence,
async job tracking, and state lifecycle management.

NOTE: StateServiceProtocol has been consolidated to ananta.interfaces.state_service_protocol
"""

from ananta.core.state.async_job_manager import AsyncJobManager
from ananta.core.state.state_manager import StateManager

__all__ = [
    # State Management
    "StateManager",
    # Async Job Management
    "AsyncJobManager",
]
