"""Shared compilation context builder for I/O interface plugins.

This service provides consistent compilation context building across all I/O interfaces
(console, JSON-RPC, Telegram, REST, etc.).

The compilation context contains runtime_args needed for template variable resolution:
- TIMESTAMP: UTC timestamp in ISO format
- DATE: Local date in YYYY-MM-DD format
- TIME: Local time with timezone name
- TIMEZONE: Timezone name (e.g., "PST", "UTC")
- TIMEZONE_OFFSET: UTC offset (e.g., "+00:00", "-08:00")
- SESSION_ID / session_id: Session identifier
- FLOW_ID / flow_id: Flow identifier (if provided)

Single Responsibility: Build compilation context for action submission.
Complexity: A (simple data assembly, minimal branching).
"""

import os
import time
from datetime import UTC, datetime
from typing import Any

from ananta.constants import (
    CONTEXT_KEY_DATE,
    CONTEXT_KEY_FLOW_ID,
    CONTEXT_KEY_SESSION_ID,
    CONTEXT_KEY_TIME,
    CONTEXT_KEY_TIMESTAMP,
    CONTEXT_KEY_TIMEZONE,
    CONTEXT_KEY_TIMEZONE_OFFSET,
)


class CompilationContextBuilder:
    """Build compilation context for action submission to ActionFactory.

    This service is injected into all I/O interface plugins to provide
    consistent compilation context building for template variable resolution.

    The context structure matches what VariableResolutionService expects.
    """

    def build_context(
        self,
        session_id: str,
        flow_id: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build compilation context with runtime args.

        Args:
            session_id: Session identifier (required)
            flow_id: Optional flow identifier
            state: Optional additional state to include

        Returns:
            Compilation context dict with runtime_args containing:
            - TIMESTAMP, DATE, TIME, TIMEZONE, TIMEZONE_OFFSET
            - SESSION_ID, session_id
            - FLOW_ID, flow_id (if provided)
        """
        runtime_args = self._build_runtime_args(session_id, flow_id)

        # Structure context to match what VariableResolutionService expects
        compilation_context: dict[str, Any] = {
            CONTEXT_KEY_SESSION_ID: session_id,
            "runtime_args": runtime_args,
            "state": state or {},
            "action": {
                CONTEXT_KEY_SESSION_ID: session_id,
            },
        }

        if flow_id:
            compilation_context[CONTEXT_KEY_FLOW_ID] = flow_id
            compilation_context["action"][CONTEXT_KEY_FLOW_ID] = flow_id

        return compilation_context

    def _build_runtime_args(
        self,
        session_id: str,
        flow_id: str | None = None,
    ) -> dict[str, str]:
        """Build runtime args dictionary for template variable resolution.

        Args:
            session_id: Session identifier
            flow_id: Optional flow identifier

        Returns:
            Dictionary with runtime variables for template resolution
        """
        now_utc = datetime.now(UTC)
        now_local = datetime.now()

        # Get local timezone info
        tz_name, tz_str = self._get_timezone_info()

        # Build runtime args - ALL KEYS ARE LOWERCASE (canonical form)
        # Template placeholders like SESSION_ID are normalized to lowercase at parse time
        runtime_args: dict[str, str] = {
            CONTEXT_KEY_TIMESTAMP: now_utc.isoformat(),
            CONTEXT_KEY_DATE: now_local.strftime("%Y-%m-%d"),
            CONTEXT_KEY_TIME: now_local.strftime(f"%H:%M:%S {tz_name}"),
            CONTEXT_KEY_TIMEZONE: tz_name,
            CONTEXT_KEY_TIMEZONE_OFFSET: tz_str,
            CONTEXT_KEY_SESSION_ID: session_id,
        }

        if flow_id:
            runtime_args[CONTEXT_KEY_FLOW_ID] = flow_id

        return runtime_args

    def _get_timezone_info(self) -> tuple[str, str]:
        """Get local timezone name and UTC offset string.

        Returns:
            Tuple of (timezone_name, utc_offset_string)
            e.g., ("PST", "-08:00") or ("UTC", "+00:00")
        """
        # Try to get timezone from environment first
        tz_name = os.environ.get("TZ", "")

        try:
            # Use system timezone info
            if time.daylight and time.localtime().tm_isdst:
                tz_offset_seconds = -time.altzone
                if not tz_name:
                    tz_name = time.tzname[1]
            else:
                tz_offset_seconds = -time.timezone
                if not tz_name:
                    tz_name = time.tzname[0]

            # Convert offset to hours:minutes format
            tz_hours = tz_offset_seconds // 3600
            tz_minutes = abs(tz_offset_seconds % 3600) // 60
            tz_str = f"{'+' if tz_hours >= 0 else '-'}{abs(tz_hours):02d}:{tz_minutes:02d}"

        except Exception:
            # Fallback to UTC on any error
            tz_str = "+00:00"
            if not tz_name:
                tz_name = "UTC"

        return tz_name, tz_str
