"""Address book plugin constants."""

# Plugin identification
PLUGIN_NAME = "default_address_book_plugin"
PLUGIN_VERSION = "0.1.0"


class EntryFieldType:
    """Common entry field types (not enforced, just suggestions)."""

    URL = "url"
    HOST = "host"
    PORT = "port"
    PATH = "path"
    ENDPOINT = "endpoint"
    HEADER = "header"
    NOTE = "note"
    QUERY_PARAM = "query_param"
    CUSTOM = "custom"


class AddressType:
    """Common address types (not enforced, just suggestions)."""

    URL = "url"
    API_ENDPOINT = "endpoint"
    WEBHOOK = "webhook"
    FILE_PATH = "path"
    DATABASE = "database"
    SERVICE = "service"
    SOCKET = "socket"
    CUSTOM = "custom"


class ErrorCode:
    """Error codes for address book operations."""

    NOT_FOUND = "address_book.not_found"
    ENTRY_NOT_FOUND = "address_book.entry_not_found"
    INVALID_ENTRY = "address_book.invalid_entry"
    MEMORY_ERROR = "address_book.memory_error"
