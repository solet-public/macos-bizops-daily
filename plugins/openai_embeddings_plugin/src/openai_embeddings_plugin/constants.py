"""Constants for OpenAI Embeddings Plugin."""

from enum import StrEnum

PLUGIN_NAME = "openai_embeddings_plugin"

# Address book entry name for this plugin's configuration
ADDRESS_BOOK_ENTRY_NAME = "openai_embeddings"


class ErrorCode(StrEnum):
    """Error codes for the plugin."""

    CONNECTION_FAILED = "openai_embeddings.connection_failed"
    API_ERROR = "openai_embeddings.api_error"
    INVALID_RESPONSE = "openai_embeddings.invalid_response"
    EMPTY_INPUTS = "openai_embeddings.empty_inputs"
    INVALID_INPUTS = "openai_embeddings.invalid_inputs"
    UNSUPPORTED_INPUT_TYPE = "openai_embeddings.unsupported_input_type"
    NOT_INITIALIZED = "openai_embeddings.not_initialized"


class EntryField(StrEnum):
    """Address book entry field types used by this plugin."""

    BASE_URL = "base_url"
    MODEL = "model"
    API_KEY = "api_key"
    TIMEOUT_SECONDS = "timeout_seconds"
