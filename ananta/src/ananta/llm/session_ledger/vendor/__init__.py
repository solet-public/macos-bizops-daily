"""Vendor-specific normalizers shared across source plugins.

Per spec §6 + [[no-shared-plugin-base-class]]: parser MACHINERY lives here
(platform-core) so each source plugin can stay a thin adapter without
duplicating JSONL parsing or normalization. Plugin classes themselves
remain per-scenario and do NOT inherit a shared base.
"""
