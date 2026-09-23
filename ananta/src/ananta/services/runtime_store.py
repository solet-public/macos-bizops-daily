import fnmatch
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, TypeVar, cast, overload

from ananta.core.domain.enums import ErrorSeverity
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StorageScope(StrEnum):
    GLOBAL = "global"
    SESSION = "session"
    FLOW = "flow"


class RuntimeStore:
    def __init__(self, enable_persistence: bool = False, persistence_path: str | None = None):
        self._storage: dict[str, dict[str, object]] = {
            StorageScope.GLOBAL.value: {},
            StorageScope.SESSION.value: {},
            StorageScope.FLOW.value: {},
        }
        self._ttl_index: dict[str, dict[str, datetime]] = {
            StorageScope.GLOBAL.value: {},
            StorageScope.SESSION.value: {},
            StorageScope.FLOW.value: {},
        }
        self._lock = threading.RLock()
        self._enable_persistence = enable_persistence
        self._persistence_path = persistence_path

        if self._enable_persistence and self._persistence_path:
            self._load_persisted_data()

    def _make_key(self, key: str, namespace: str | None = None) -> str:
        if namespace:
            return f"{namespace}:{key}"
        return key

    def _validate_scope(self, scope: StorageScope) -> None:
        if scope not in StorageScope:
            raise FrameworkError(
                message=f"Invalid storage scope: {scope}",
                error_code="runtime_store.invalid_scope",
                details={"scope": scope, "valid_scopes": [s.value for s in StorageScope]},
                severity=ErrorSeverity.ERROR,
            )

    def _is_expired(self, scope: StorageScope, full_key: str) -> bool:
        scope_value = scope.value
        if full_key in self._ttl_index.get(scope_value, {}):
            expiry_time = self._ttl_index[scope_value][full_key]
            if datetime.now(UTC) > expiry_time:
                return True
        return False

    def _cleanup_expired(self, scope: StorageScope, full_key: str) -> None:
        scope_value = scope.value
        if self._is_expired(scope, full_key):
            self._storage[scope_value].pop(full_key, None)
            self._ttl_index[scope_value].pop(full_key, None)

    def set(
        self,
        key: str,
        value: object,
        scope: StorageScope = StorageScope.SESSION,
        ttl: int | None = None,
        namespace: str | None = None,
    ) -> None:
        self._validate_scope(scope)

        if not key:
            raise FrameworkError(
                message="Key cannot be empty",
                error_code="runtime_store.empty_key",
                severity=ErrorSeverity.ERROR,
            )

        full_key = self._make_key(key, namespace)
        scope_value = scope.value

        with self._lock:
            if scope_value not in self._storage:
                self._storage[scope_value] = {}

            self._storage[scope_value][full_key] = value

            if ttl and ttl > 0:
                expiry_time = datetime.now(UTC) + timedelta(seconds=ttl)
                self._ttl_index[scope_value][full_key] = expiry_time
            elif full_key in self._ttl_index.get(scope_value, {}):
                self._ttl_index[scope_value].pop(full_key, None)

            if self._enable_persistence:
                self._persist_data()

    @overload
    def get(
        self,
        key: str,
        scope: StorageScope = StorageScope.SESSION,
        namespace: str | None = None,
        default: None = None,
    ) -> object | None: ...

    @overload
    def get(
        self,
        key: str,
        scope: StorageScope = StorageScope.SESSION,
        namespace: str | None = None,
        default: T = ...,
    ) -> object | T: ...

    def get(
        self,
        key: str,
        scope: StorageScope = StorageScope.SESSION,
        namespace: str | None = None,
        default: T | None = None,
    ) -> object | T | None:
        self._validate_scope(scope)

        if not key:
            raise FrameworkError(
                message="Key cannot be empty",
                error_code="runtime_store.empty_key",
                severity=ErrorSeverity.ERROR,
            )

        full_key = self._make_key(key, namespace)
        scope_value = scope.value

        with self._lock:
            self._cleanup_expired(scope, full_key)

            stored_value: object | None = self._storage.get(scope_value, {}).get(full_key)

            if stored_value is None:
                return default

            return stored_value

    def exists(
        self, key: str, scope: StorageScope = StorageScope.SESSION, namespace: str | None = None
    ) -> bool:
        self._validate_scope(scope)

        if not key:
            raise FrameworkError(
                message="Key cannot be empty",
                error_code="runtime_store.empty_key",
                severity=ErrorSeverity.ERROR,
            )

        full_key = self._make_key(key, namespace)
        scope_value = scope.value

        with self._lock:
            self._cleanup_expired(scope, full_key)
            exists = full_key in self._storage.get(scope_value, {})
            return exists

    def delete(
        self, key: str, scope: StorageScope = StorageScope.SESSION, namespace: str | None = None
    ) -> bool:
        self._validate_scope(scope)

        if not key:
            raise FrameworkError(
                message="Key cannot be empty",
                error_code="runtime_store.empty_key",
                severity=ErrorSeverity.ERROR,
            )

        full_key = self._make_key(key, namespace)
        scope_value = scope.value

        with self._lock:
            deleted = False
            if full_key in self._storage.get(scope_value, {}):
                self._storage[scope_value].pop(full_key, None)
                self._ttl_index.get(scope_value, {}).pop(full_key, None)
                deleted = True
            else:
                pass

            if self._enable_persistence and deleted:
                self._persist_data()

            return deleted

    def clear_scope(self, scope: StorageScope) -> int:
        self._validate_scope(scope)

        scope_value = scope.value

        with self._lock:
            count = len(self._storage.get(scope_value, {}))
            self._storage[scope_value] = {}
            self._ttl_index[scope_value] = {}

            if self._enable_persistence and count > 0:
                self._persist_data()

            return count

    def get_all_by_pattern(
        self,
        pattern: str,
        scope: StorageScope = StorageScope.SESSION,
        namespace: str | None = None,
    ) -> dict[str, object]:
        self._validate_scope(scope)

        if not pattern:
            raise FrameworkError(
                message="Pattern cannot be empty",
                error_code="runtime_store.empty_pattern",
                severity=ErrorSeverity.ERROR,
            )

        scope_value = scope.value
        full_pattern = self._make_key(pattern, namespace)

        with self._lock:
            result: dict[str, object] = {}
            keys_to_cleanup = []

            for full_key in list(self._storage.get(scope_value, {}).keys()):
                if fnmatch.fnmatch(full_key, full_pattern):
                    if self._is_expired(scope, full_key):
                        keys_to_cleanup.append(full_key)
                    else:
                        if namespace:
                            original_key = full_key.removeprefix(f"{namespace}:")
                        else:
                            original_key = full_key
                        result[original_key] = self._storage[scope_value][full_key]

            for key in keys_to_cleanup:
                self._cleanup_expired(scope, key)

            return result

    def _persist_data(self) -> None:
        if not self._persistence_path:
            return

        try:
            data = {
                "storage": self._storage,
                "ttl_index": {
                    scope: {key: expiry.isoformat() for key, expiry in ttl_data.items()}
                    for scope, ttl_data in self._ttl_index.items()
                },
                "timestamp": datetime.now(UTC).isoformat(),
            }

            with open(self._persistence_path, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            raise FrameworkError(
                message=f"Failed to persist runtime store data: {str(e)}",
                error_code="runtime_store.persistence_error",
                details={"path": self._persistence_path},
                original_error=e,
                severity=ErrorSeverity.WARNING,
            ) from e

    def _load_persisted_data(self) -> None:
        if not self._persistence_path:
            return

        try:
            data = self._read_persistence_file()
            if data is None:
                return

            self._restore_storage(data)
            self._restore_ttl_index(data)
            self._cleanup_all_expired()

        except Exception as e:
            raise FrameworkError(
                message=f"Failed to load persisted runtime store data: {str(e)}",
                error_code="runtime_store.load_error",
                details={"path": self._persistence_path},
                original_error=e,
                severity=ErrorSeverity.WARNING,
            ) from e

    def _read_persistence_file(self) -> dict[str, Any] | None:
        """Read and parse persistence file if it exists."""
        import os

        persistence_path = self._persistence_path
        if persistence_path is None or not os.path.exists(persistence_path):
            return None

        with open(persistence_path) as f:
            result: dict[str, Any] = json.load(f)
            return result

    def _restore_storage(self, data: dict[str, Any]) -> None:
        """Restore storage from persisted data."""
        default_storage: dict[str, dict[str, object]] = {
            StorageScope.GLOBAL.value: {},
            StorageScope.SESSION.value: {},
            StorageScope.FLOW.value: {},
        }
        self._storage = cast(dict[str, dict[str, object]], data.get("storage", default_storage))

    def _restore_ttl_index(self, data: dict[str, Any]) -> None:
        """Restore TTL index from persisted data."""
        self._ttl_index = {}
        ttl_index_data: dict[str, dict[str, str]] = data.get("ttl_index", {})
        for scope, ttl_data in ttl_index_data.items():
            self._ttl_index[scope] = {
                key: datetime.fromisoformat(expiry_str) for key, expiry_str in ttl_data.items()
            }

        for scope in StorageScope:
            if scope.value not in self._ttl_index:
                self._ttl_index[scope.value] = {}

    def _cleanup_all_expired(self) -> None:
        """Clean up expired entries across all scopes."""
        for scope in StorageScope:
            scope_value = scope.value
            expired_keys = [
                key
                for key in list(self._storage.get(scope_value, {}).keys())
                if self._is_expired(scope, key)
            ]
            for key in expired_keys:
                self._cleanup_expired(scope, key)

    def get_stats(self) -> dict[str, object]:
        with self._lock:
            stats: dict[str, object] = {}
            for scope in StorageScope:
                scope_value = scope.value
                scope_data = self._storage.get(scope_value, {})
                ttl_data = self._ttl_index.get(scope_value, {})

                scope_stats: dict[str, object] = {
                    "total_keys": len(scope_data),
                    "keys_with_ttl": len(ttl_data),
                    "size_bytes": sum(len(str(v).encode()) for v in scope_data.values()),
                }
                stats[scope_value] = scope_stats

            return stats
