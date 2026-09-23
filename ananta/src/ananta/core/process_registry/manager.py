"""
Process Registry Manager Service

Responsibility: Handle all process registry synchronization and management operations
Dependencies: StateService, ValidationService, ProcessRegistryUtil, DiscoveryService, logging
Complexity: High - focused on complex process synchronization with validation and error handling

Extracted from ActionManager god class (B10 complexity sync_process_registry method)
"""

import logging

logger = logging.getLogger(__name__)


class ProcessRegistryManager:
    """
    Service for managing process registry synchronization operations.

    ARCHITECTURAL ROLE: Supporting service that extracts process registry management logic
    from ActionManager while maintaining action management integrity.

    This service handles:
        pass
    - Discovery service integration and validation
    - Process retrieval and batch processing from discovery service
    - Process validation and record preparation for persistent storage
    - Batch writing operations with error handling and recovery
    - Synchronization status tracking and detailed logging
    - Error count management and success validation
    """

    def __init__(  # type: ignore[no-untyped-def]
        self,
        state_service=None,
        validation_service=None,
        process_registry_util=None,
        discovery_service=None,
    ) -> None:
        # SAFE: Optional dependencies injected at runtime, explicit types would require imports risking circular dependencies
        """Initialize ProcessRegistryManager with required dependencies."""
        self.state_service = state_service
        self.validation_service = validation_service
        self.process_registry_util = process_registry_util
        self.discovery_service = discovery_service

    async def sync_process_registry(self) -> bool:
        """Synchronize process registry with persistent storage from discovery service.

        Returns:
            bool: True if synchronization completed without errors, False otherwise
        """
        try:
            if not self._validate_sync_prerequisites():
                return False

            # Type narrowing: after validation, these are guaranteed non-None
            assert self.discovery_service is not None
            assert self.validation_service is not None
            assert self.process_registry_util is not None

            logger.debug(
                "🔄 PROCESS_SYNC_001: Synchronizing process registry with persistent storage"
            )

            all_processes = self.discovery_service.get_all_processes()
            logger.debug(
                f"📊 PROCESS_SYNC_004: Retrieved {len(all_processes)} processes from discovery service"
            )

            records_to_sync, _synced_count, error_count = self._prepare_sync_records(
                all_processes, self.validation_service
            )

            if not self._write_sync_records(records_to_sync, self.process_registry_util):
                return False

            logger.debug(
                f"🎯 PROCESS_SYNC_013: Process registry synchronization successful - "
                f"database now has {_synced_count} processes"
            )
            return error_count == 0

        except Exception as e:
            logger.error(f"❌ PROCESS_SYNC_014: Error during process registry sync: {e}")
            return False

    def _validate_sync_prerequisites(self) -> bool:
        """Validate all required services are available for sync."""
        if not self.state_service:
            logger.error("State service not available for process registry sync")
            return False

        if not hasattr(self, "discovery_service") or not self.discovery_service:
            logger.error(
                "❌ PROCESS_SYNC_002: Discovery service not available for process registry sync"
            )
            return False

        if not self.validation_service:
            logger.error("❌ PROCESS_SYNC_002A: Validation service not available")
            return False

        if not self.process_registry_util:
            logger.error("❌ PROCESS_SYNC_002B: Process registry util not available")
            return False

        logger.debug(
            "✅ PROCESS_SYNC_003: Discovery service available, retrieving all processes"
        )
        return True

    def _prepare_sync_records(
        self, all_processes: dict[str, object], validation_service: object
    ) -> tuple[list[object], int, int]:
        """Prepare records for synchronization.

        Returns:
            Tuple of (records_to_sync, synced_count, error_count)
        """
        logger.debug(
            "🗑️ PROCESS_SYNC_005: Skipping manual clear - database recreated during startup"
        )

        records_to_sync: list[object] = []
        synced_count = 0
        error_count = 0

        for process_key, process_data in all_processes.items():
            record = validation_service.validate_and_prepare_process_record(  # type: ignore[attr-defined]
                process_key, process_data
            )
            if record:
                records_to_sync.append(record)
                synced_count += 1
                if synced_count <= 3:
                    logger.debug(f"📝 PROCESS_SYNC_007: Prepared record for {process_key}")
            else:
                error_count += 1

        return records_to_sync, synced_count, error_count

    def _write_sync_records(
        self, records_to_sync: list[object], process_registry_util: object
    ) -> bool:
        """Write prepared records to persistent storage."""
        try:
            write_success = process_registry_util.sync_records(records_to_sync)  # type: ignore[attr-defined]
            logger.debug(
                f"✅ PROCESS_SYNC_009: Batch write completed for {len(records_to_sync)} records"
            )
            logger.debug(f"📊 PROCESS_SYNC_010A: Write success: {write_success}")

            if not write_success:
                logger.error("❌ PROCESS_SYNC_011: Database write failed")
                return False

            return True

        except Exception as e:
            logger.error(f"❌ PROCESS_SYNC_012: Error writing process registry to database: {e}")
            return False
