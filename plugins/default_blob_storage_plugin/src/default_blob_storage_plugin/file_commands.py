"""File command helpers for blob storage (ls, file, find)."""
from __future__ import annotations

import shlex
from datetime import datetime
from typing import Any, cast

from ananta.core.plugins.plugin_contracts import ActionResult

from .errors import create_error_response, create_success_response


def execute_file_command(command: str, provider: Any) -> ActionResult:
    """Parse and route a file command string to the appropriate handler."""
    try:
        args = shlex.split(command)
    except ValueError as e:
        return create_error_response("file_command.parse_error", f"Invalid command: {e}")

    if not args:
        args = ["ls"]

    cmd = args[0].lower()

    if cmd == "ls":
        return execute_ls_command(args[1:], provider)
    if cmd == "file":
        if len(args) < 2:
            return create_error_response(
                "file_command.missing_argument", "file command requires ID"
            )
        return execute_file_info_command(args[1], provider)
    if cmd == "find":
        if len(args) < 2:
            return create_error_response(
                "file_command.missing_argument", "find command requires a filename pattern"
            )
        return execute_find_command(" ".join(args[1:]), provider)

    return create_error_response("file_command.unknown_command", f"Unknown command: {cmd}")


def parse_ls_options(args: list[str]) -> tuple[str | None, str, bool]:
    """Parse ls command options. Returns (type_filter, sort_field, count_only)."""
    type_filter: str | None = None
    sort_field = "time"
    count_only = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-l", "--details"):
            pass  # Accepted but ignored - always return full details
        elif arg == "--type" and i + 1 < len(args):
            i += 1
            type_filter = args[i]
        elif arg == "--sort" and i + 1 < len(args):
            i += 1
            sort_field = args[i]
        elif arg == "--count":
            count_only = True
        i += 1

    return type_filter, sort_field, count_only


def build_type_filter(type_filter: str | None) -> dict[str, Any]:
    """Build search filters dict from a type alias."""
    if not type_filter:
        return {}
    type_map = {
        "image": "image/*",
        "audio": "audio/*",
        "video": "video/*",
        "document": "application/*",
    }
    return {"mime_type": type_map.get(type_filter, type_filter)}


def execute_ls_command(args: list[str], provider: Any) -> ActionResult:
    """Execute ls command with options. Returns structured JSON for LLM formatting."""
    type_filter, sort_field, count_only = parse_ls_options(args)
    filters = build_type_filter(type_filter)

    if not provider:
        return create_error_response("file_command.not_initialized", "Storage not initialized")

    result = provider.search_blobs("", filters)
    if result.get("action_status") == "error":
        return result

    data = result.get("data", {})
    files: list[dict[str, Any]] = cast(list[dict[str, Any]], data.get("files", []))
    total_count: int = cast(int, data.get("total_count", len(files)))
    files = sort_files(files, sort_field)

    if count_only:
        return create_success_response({
            "total_count": total_count,
            "message": f"{total_count} file(s)",
        })

    if not files:
        return create_success_response({
            "files": [],
            "total_count": 0,
            "shown_count": 0,
            "message": "No files found.",
        })

    return create_success_response({
        "files": build_structured_file_list(files),
        "total_count": total_count,
        "shown_count": len(files),
    })


def execute_file_info_command(blob_id: str, provider: Any) -> ActionResult:
    """Execute file info command for a specific blob."""
    if not provider:
        return create_error_response("file_command.not_initialized", "Storage not initialized")

    result = provider.search_blobs("", {"blob_id": blob_id})
    if result.get("action_status") == "error":
        return result

    data = result.get("data", {})
    files = cast(list[dict[str, Any]], data.get("files", []))
    if not files:
        return create_error_response("file_command.not_found", f"File not found: {blob_id}")

    meta = files[0].get("metadata", {})
    lines = [
        f"File: {blob_id}",
        f"  Type: {meta.get('mime_type', 'unknown')}",
        f"  Size: {human_readable_size(meta.get('size', 0))}",
        f"  Created: {format_date(meta.get('created_at', ''))}",
    ]

    if meta.get("filename"):
        lines.append(f"  Filename: {meta['filename']}")
    if meta.get("name"):
        lines.append(f"  Name: {meta['name']}")
    if meta.get("extension"):
        lines.append(f"  Extension: {meta['extension']}")
    if meta.get("original_name"):
        lines.append(f"  Original: {meta['original_name']}")
    if meta.get("plugin_namespace"):
        lines.append(f"  Namespace: {meta['plugin_namespace']}")

    return create_success_response({"output": "\n".join(lines)})


def execute_find_command(pattern: str, provider: Any) -> ActionResult:
    """Search files by external_id substring match across all namespaces."""
    if not provider:
        return create_error_response("file_command.not_initialized", "Storage not initialized")

    result = provider.search_blobs("", {"external_id": pattern})
    if result.get("action_status") == "error":
        return result

    data = result.get("data", {})
    files = cast(list[dict[str, Any]], data.get("files", []))

    if not files:
        return create_success_response({"output": f"No files found matching '{pattern}'"})

    lines = [f"Found {len(files)} file(s) matching '{pattern}':", ""]
    for f in files:
        meta = f.get("metadata", {})
        blob_id = f.get("blob_id", "unknown")
        name = meta.get("name", meta.get("external_id", blob_id))
        namespace = meta.get("plugin_namespace", "unknown")
        size = human_readable_size(meta.get("size", 0))
        lines.append(f"  {name}")
        lines.append(f"    blob_id: {blob_id}")
        lines.append(f"    namespace: {namespace}")
        lines.append(f"    size: {size}")
        lines.append("")

    return create_success_response({"output": "\n".join(lines)})


def sort_files(files: list[dict[str, Any]], sort_field: str) -> list[dict[str, Any]]:
    """Sort files by time (default), size, or name."""
    if sort_field == "time":
        return sorted(
            files,
            key=lambda f: f.get("metadata", {}).get("created_at", ""),
            reverse=True,
        )
    if sort_field == "size":
        return sorted(
            files,
            key=lambda f: f.get("metadata", {}).get("size", 0),
            reverse=True,
        )
    if sort_field == "name":
        return sorted(
            files,
            key=lambda f: f.get("metadata", {}).get("filename")
            or f.get("metadata", {}).get("original_name")
            or f.get("blob_id", ""),
        )
    return files


def build_structured_file_list(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build structured file list with human-readable fields for LLM formatting."""
    result = []
    for f in files:
        meta = f.get("metadata", {})
        filename = (
            meta.get("filename")
            or meta.get("original_name")
            or f.get("blob_id", "unknown")
        )
        result.append({
            "filename": filename,
            "type": meta.get("mime_type", "unknown"),
            "size": human_readable_size(meta.get("size", 0)),
            "date": format_date(meta.get("created_at", "")),
        })
    return result


def format_date(timestamp: str) -> str:
    """Format ISO timestamp as short date (Jan 08 14:32)."""
    if not timestamp:
        return "unknown"
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%M")
    except (ValueError, AttributeError):
        return "unknown"


def human_readable_size(size_bytes: int) -> str:
    """Convert byte count to human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
