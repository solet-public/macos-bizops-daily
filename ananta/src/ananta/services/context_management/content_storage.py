"""File Context Content Storage - Plugin helper for file-based content storage.

Stores event content and snapshot summaries in plugin data directories.
Returns paths relative to APP_HOME for database storage.
"""

import secrets
from datetime import UTC, datetime
from pathlib import Path

from ananta.constants import DATA_DIRECTORY_NAME, PLUGIN_DATA_DIRECTORY_NAME

from .types import (
    CONTENT_EVENTS_SUBDIR,
    CONTENT_FILE_EXTENSION,
    CONTENT_FILENAME_PREFIX_EVENT,
    CONTENT_FILENAME_PREFIX_SNAPSHOT,
    CONTENT_SNAPSHOTS_SUBDIR,
    CONTENT_STORAGE_SUBDIR,
)


class FileContextContentStorage:
    """Plugin helper for storing context content in files.

    Content files are stored under:
    APP_HOME/data/plugin_data/<plugin_name>/context/<context_id>/events/
    APP_HOME/data/plugin_data/<plugin_name>/context/<context_id>/snapshots/

    Paths returned are relative to APP_HOME for database storage.
    """

    def __init__(self, app_home: str, plugin_name: str) -> None:
        """Initialize with app home and plugin name.

        Args:
            app_home: Application home directory
            plugin_name: Name of the plugin owning this storage
        """
        self._app_home = Path(app_home).resolve()
        self._plugin_name = plugin_name
        self._base_relative = (
            Path(DATA_DIRECTORY_NAME)
            / PLUGIN_DATA_DIRECTORY_NAME
            / plugin_name
            / CONTENT_STORAGE_SUBDIR
        )
        # Store resolved plugin data root for path traversal validation
        self._plugin_data_root = self._app_home / self._base_relative

    def _ensure_directory(self, path: Path) -> None:
        """Ensure directory exists."""
        path.mkdir(parents=True, exist_ok=True)

    def _validate_path(self, relative_path: str) -> Path:
        """Validate relative path and return resolved absolute path.

        Prevents path traversal attacks by ensuring resolved path stays
        within the plugin's data root.

        Args:
            relative_path: Path relative to APP_HOME

        Returns:
            Resolved absolute path

        Raises:
            ValueError: If path escapes plugin data root
        """
        absolute_path = (self._app_home / relative_path).resolve()
        try:
            absolute_path.relative_to(self._plugin_data_root)
        except ValueError as e:
            msg = f"Path traversal attempt: {relative_path!r} escapes plugin data root"
            raise ValueError(msg) from e
        return absolute_path

    def _generate_filename(self, prefix: str) -> str:
        """Generate unique filename with timestamp and random suffix.

        Args:
            prefix: Filename prefix (event or snapshot)

        Returns:
            Filename like event_20260102_120000_ab12.txt
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        random_suffix = secrets.token_hex(2)
        return f"{prefix}_{timestamp}_{random_suffix}{CONTENT_FILE_EXTENSION}"

    def _context_events_dir(self, context_id: str) -> Path:
        """Get events directory for context (absolute)."""
        return self._app_home / self._base_relative / context_id / CONTENT_EVENTS_SUBDIR

    def _context_snapshots_dir(self, context_id: str) -> Path:
        """Get snapshots directory for context (absolute)."""
        return self._app_home / self._base_relative / context_id / CONTENT_SNAPSHOTS_SUBDIR

    def store_event(self, context_id: str, content: str) -> tuple[str, int]:
        """Store event content in file.

        Args:
            context_id: Context stream ID
            content: Event content to store

        Returns:
            Tuple of (relative_path, char_count)
        """
        events_dir = self._context_events_dir(context_id)
        self._ensure_directory(events_dir)

        filename = self._generate_filename(CONTENT_FILENAME_PREFIX_EVENT)
        file_path = events_dir / filename

        file_path.write_text(content, encoding="utf-8")

        relative_path = self._base_relative / context_id / CONTENT_EVENTS_SUBDIR / filename
        return str(relative_path), len(content)

    def store_snapshot(self, context_id: str, content: str) -> tuple[str, int]:
        """Store snapshot summary in file.

        Args:
            context_id: Context stream ID
            content: Snapshot summary content to store

        Returns:
            Tuple of (relative_path, char_count)
        """
        snapshots_dir = self._context_snapshots_dir(context_id)
        self._ensure_directory(snapshots_dir)

        filename = self._generate_filename(CONTENT_FILENAME_PREFIX_SNAPSHOT)
        file_path = snapshots_dir / filename

        file_path.write_text(content, encoding="utf-8")

        relative_path = self._base_relative / context_id / CONTENT_SNAPSHOTS_SUBDIR / filename
        return str(relative_path), len(content)

    def read_text(self, relative_path: str) -> str:
        """Read content from file by relative path.

        Args:
            relative_path: Path relative to APP_HOME

        Returns:
            File content as string

        Raises:
            FileNotFoundError: If file does not exist
            ValueError: If path escapes plugin data root
        """
        absolute_path = self._validate_path(relative_path)
        return absolute_path.read_text(encoding="utf-8")

    def delete(self, path: str) -> None:
        """Delete content file.

        Args:
            path: Path relative to APP_HOME

        Raises:
            ValueError: If path escapes plugin data root
        """
        absolute_path = self._validate_path(path)
        if absolute_path.exists():
            absolute_path.unlink()

    def delete_context_files(self, context_id: str) -> int:
        """Delete all content files for a context.

        Args:
            context_id: Context stream ID

        Returns:
            Number of files deleted
        """
        deleted_count = 0

        events_dir = self._context_events_dir(context_id)
        if events_dir.exists():
            for file_path in events_dir.iterdir():
                if file_path.is_file():
                    file_path.unlink()
                    deleted_count += 1

        snapshots_dir = self._context_snapshots_dir(context_id)
        if snapshots_dir.exists():
            for file_path in snapshots_dir.iterdir():
                if file_path.is_file():
                    file_path.unlink()
                    deleted_count += 1

        return deleted_count

    def list_event_files(self, context_id: str) -> list[str]:
        """List all event files for a context.

        Args:
            context_id: Context stream ID

        Returns:
            List of relative paths
        """
        events_dir = self._context_events_dir(context_id)
        if not events_dir.exists():
            return []

        relative_base = self._base_relative / context_id / CONTENT_EVENTS_SUBDIR
        return [
            str(relative_base / f.name)
            for f in events_dir.iterdir()
            if f.is_file() and f.suffix == CONTENT_FILE_EXTENSION
        ]

    def list_snapshot_files(self, context_id: str) -> list[str]:
        """List all snapshot files for a context.

        Args:
            context_id: Context stream ID

        Returns:
            List of relative paths
        """
        snapshots_dir = self._context_snapshots_dir(context_id)
        if not snapshots_dir.exists():
            return []

        relative_base = self._base_relative / context_id / CONTENT_SNAPSHOTS_SUBDIR
        return [
            str(relative_base / f.name)
            for f in snapshots_dir.iterdir()
            if f.is_file() and f.suffix == CONTENT_FILE_EXTENSION
        ]

    def get_storage_stats(self, context_id: str) -> dict[str, int]:
        """Get storage statistics for a context.

        Args:
            context_id: Context stream ID

        Returns:
            Dict with event_files, snapshot_files, total_bytes
        """
        total_bytes = 0
        event_count = 0
        snapshot_count = 0

        events_dir = self._context_events_dir(context_id)
        if events_dir.exists():
            for f in events_dir.iterdir():
                if f.is_file():
                    event_count += 1
                    total_bytes += f.stat().st_size

        snapshots_dir = self._context_snapshots_dir(context_id)
        if snapshots_dir.exists():
            for f in snapshots_dir.iterdir():
                if f.is_file():
                    snapshot_count += 1
                    total_bytes += f.stat().st_size

        return {
            "event_files": event_count,
            "snapshot_files": snapshot_count,
            "total_bytes": total_bytes,
        }
