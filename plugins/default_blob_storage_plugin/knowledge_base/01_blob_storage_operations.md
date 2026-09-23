# Blob Storage Operations in Plans

Article Layer: 1
Article Role: operations_reference
Article Tags: planning-stage:execution, evidence-category:capability-reference, domain:blob-storage, consumer_profile:both

Embedding Description: How and when to use blob storage in a plan step — retrieving an uploaded file by name, listing or searching available files, inspecting metadata without downloading content, and ingesting a large finished binary artifact from an absolute path, plus the rule that a blob's original filename lives in its metadata, never in its on-disk path.

**When you need this**: a plan step must inspect a file the user uploaded, retrieve a stored file by name, store a generated or finished binary artifact for later retrieval, or list/search available files — and you need to know which verb fits and how the filename survives the round trip.

## When to Use Blob Storage in Plans

Use blob storage when the plan needs to:
- Inspect files the user has uploaded
- Retrieve stored files by name
- Store generated content for later retrieval
- List or search available files

## Retrieve File by Name

When a plan step needs an existing file (e.g., user uploaded "my_song.mp3"):

```
[ ] N. Retrieve uploaded file
    PROCESS: service_interface::blob_storage_service::retrieve_blob_by_name
    ARGS: {"filename": "<exact filename>"}
```

## List Available Files

When the plan needs to discover what files exist:

```
[ ] N. List available files
    PROCESS: service_interface::blob_storage_service::file_command
    ARGS: {"command": "ls -l"}
```

Filter by type:

```
[ ] N. List audio files
    PROCESS: service_interface::blob_storage_service::file_command
    ARGS: {"command": "ls --type audio"}
```

## Inspect a Specific File

Get metadata about a file without downloading its content:

```
[ ] N. Inspect file details
    PROCESS: service_interface::blob_storage_service::get_blob_metadata
    ARGS: {"namespace": "<namespace>", "blob_id": "<blob_id>"}
```

## Search Files by Metadata

When the plan needs to find blobs by tag, MIME type, or a plugin-specific
metadata field rather than an exact filename:

```
[ ] N. Find blobs matching a metadata filter
    PROCESS: service_interface::blob_storage_service::search_blobs
    ARGS: {"namespace": "<namespace>", "metadata_filters": {"tags": "render,final"}}
```

Filters on schema fields (`tags`, `mime_type`, `original_name`,
`external_id`, `plugin_namespace`) or descend into plugin-specific metadata
with dotted paths (e.g. `"plugin_metadata.role"`). Tag values OR-match on a
comma-separated string; multiple `plugin_metadata.*` filters AND together.
Returns matching `blob_id`s and their metadata — follow up with `get_blob`
or `get_blob_metadata` to act on a specific match. Use `file_command` for a
quick shell-style listing instead when you don't need metadata filtering.

## Blob Ingestion Sources

For *agent-driven ingestion* of large finished binary artifacts (audio renders, cover art, videos), use `store_blob_from_file`. The agent supplies only an absolute path; the platform reads the bytes and stores the blob. This is the canonical agent-driven path for big binaries — the file content never round-trips through the agent's conversation context.

This mirrors the vault `store_from_file` pattern: ingestion methods that name a *source* rather than carrying the value itself.

```
[ ] N. Ingest finished quarterly report into blob storage
    PROCESS: service_interface::blob_storage_service::store_blob_from_file
    ARGS: {
      "namespace": "<namespace>",
      "file_path": "/Users/alice/Documents/quarterly_report.pdf"
    }
```

Optional arguments:
- `filename` — display filename for blob metadata; defaults to the basename of `file_path`.
- `mime_type` — MIME type; if omitted, inferred from the file extension. If inference fails, the action errors with `mime_type_unknown` (no fallback to a generic type).
- `metadata` — additional key-value pairs merged with the auto-derived fields (`filename`, `mime_type`, `source_path`, `byte_count`, `artifact_type`).
- `artifact_type` — semantic type tag (e.g. `"audio"`, `"image"`); defaults to the MIME type's primary class.

The returned `blob_id` is the canonical reference to pass to downstream actions that consume blobs (for example `soundcloud_artist_studio_plugin::upload_track`).

Use this method for finished renders (M4A, FLAC, WAV, MP3), cover art (JPEG, PNG, WebP), video deliverables (MP4, MOV), large generated documents — any binary artifact too large to base64-encode through `store_blob`'s `content` parameter.

Errors: `file_path_not_absolute`, `file_not_found`, `file_not_regular`, `file_unreadable`, `file_empty`, `mime_type_unknown`. All are fail-fast — no retries, no fallbacks.

## Key Rules

- Most audio/TTS plugin processes handle blob storage internally, so explicit blob-storage steps are often unnecessary for plugin-generated files.
- Use `retrieve_blob_by_name` when referencing user-uploaded files by their original filename.
- Use `file_command` for discovery when the exact filename is unknown; use `search_blobs` when the criterion is a metadata field (tag, MIME type, a plugin-specific value) rather than a filename.
- For ingesting large finished binary artifacts produced outside the platform, use `store_blob_from_file` — the agent supplies the path, never the bytes.
- Blob storage is for binary content. For text/structured data, use memory operations instead.

## Filenames live in metadata, not in the on-disk blob path

Blobs are persisted to disk under `default_blob_storage_plugin/blobs/` as `bmd-<id>` (no extension). The original filename and the original file extension are **not** preserved in the path on disk — they live in the blob's metadata under `metadata.filename` (and a duplicate is sometimes carried under `metadata.original_name` depending on the ingest path).

Plugins that need the original name (for example, to negotiate a downstream content-type, to set a SoundCloud upload's display filename, or to reproduce the original extension when streaming the blob back to a multipart upload) **must** read it from `metadata.filename`. Reading it off the on-disk path or the blob_id is wrong; both lose the extension.

`store_blob_from_file` populates `metadata.filename` from the basename of the supplied `file_path` by default, and accepts an explicit `filename` argument to override.
