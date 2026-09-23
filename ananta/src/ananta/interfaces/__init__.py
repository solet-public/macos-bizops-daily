"""Legacy interface exports for backward compatibility.

NOTE: This module only exports legacy interfaces and errors.
For new service-owned interfaces, import directly from the service:
  - from ananta.services.discovery_service.interfaces import DiscoveryServiceAPI
  - from ananta.services.inference_service.interfaces import InferenceServiceAPI, InferenceProvider
  - from ananta.services.state_service.interfaces import StateManagementAPI, StateProvider
  - from ananta.services.embedding_service.interfaces import EmbeddingServiceAPI, EmbeddingProvider
  - from ananta.services.vector_service.interfaces import VectorServiceAPI, VectorProvider
"""

# New service interfaces
from .address_book_service_interface import (
    AddressBookServiceInterface as AddressBookServiceInterface,
)
from .blob_storage_service_interface import (
    BlobStorageServiceInterface as BlobStorageServiceInterface,
)

# NOTE: ContextManagementContract not exported here to avoid circular import.
# Import directly: from ananta.interfaces.context_management_contract import ContextManagementContract
from .edge_process_provider import EdgeProcessDefinition as EdgeProcessDefinition
from .edge_process_provider import EdgeProcessProvider as EdgeProcessProvider
from .embedding_aware_plugin import EmbeddingAwarePlugin as EmbeddingAwarePlugin
from .embedding_service_interface import EmbeddingServiceInterface as EmbeddingServiceInterface

# Errors
from .inference_errors import (
    InferenceError as InferenceError,
)
from .inference_errors import (
    InferenceServiceUnavailableError as InferenceServiceUnavailableError,
)
from .inference_errors import (
    InferenceTimeoutError as InferenceTimeoutError,
)
from .inference_errors import (
    InferenceValidationError as InferenceValidationError,
)

# Legacy interfaces (will be removed)
from .inference_service_interface import InferenceRequest as InferenceRequest
from .inference_service_interface import InferenceServiceInterface as InferenceServiceInterface
from .io_interface_plugin import AtCommandProcessorProtocol as AtCommandProcessorProtocol
from .io_interface_plugin import IOInterfacePlugin as IOInterfacePlugin
from .memory_service_interface import MemoryServiceInterface as MemoryServiceInterface
from .state_aware_plugin import StateAwarePlugin as StateAwarePlugin
from .state_management_interface import StateManagementInterface as StateManagementInterface
from .state_provider_interface import ActionExecutionRecord as ActionExecutionRecord
from .state_provider_interface import StateProviderInterface as StateProviderInterface
from .vault_qualification_interface import VaultQualificationInterface as VaultQualificationInterface
from .vault_service_interface import VaultServiceInterface as VaultServiceInterface
from .vector_service_interface import VectorServiceInterface as VectorServiceInterface

# No __all__ list - all imported names are automatically available
# This prevents manual maintenance and follows DRY principles
