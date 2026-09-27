"""Pinned Nomic Core AI asset contract for runtime and installer callers."""

from .acquisition import (
    ARCHIVE_PIN,
    ASSET_DIRECTORY,
    FILE_PINS,
    LICENSE,
    MODEL_ID,
    UPSTREAM_REVISION,
    VARIANT,
    AssetCorruptError,
    AssetDownloadError,
    AssetError,
    AssetManifestError,
    AssetMissingError,
    DistributionManifest,
    InstalledAsset,
    acquire_asset,
    verify_installed_asset,
    verify_source_files,
)
from .archive import hash_archive, stream_archive

__all__ = [
    "ARCHIVE_PIN",
    "ASSET_DIRECTORY",
    "FILE_PINS",
    "LICENSE",
    "MODEL_ID",
    "UPSTREAM_REVISION",
    "VARIANT",
    "AssetCorruptError",
    "AssetDownloadError",
    "AssetError",
    "AssetManifestError",
    "AssetMissingError",
    "DistributionManifest",
    "InstalledAsset",
    "acquire_asset",
    "hash_archive",
    "stream_archive",
    "verify_installed_asset",
    "verify_source_files",
]
