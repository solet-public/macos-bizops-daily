"""Interface for I/O plugins that provide external interfaces to Ananta.

I/O Interface Plugins are plugins that:
1. Receive input from external sources (console, Telegram, HTTP, Unix sockets, etc.)
2. Submit actions to ActionFactory
3. Send output/messages back to clients
4. Manage sessions/connections with external clients

Examples:
- default_console_plugin: Interactive console interface
- jsonrpc_plugin: JSON-RPC over Unix sockets
- agent_messaging_plugin: MCP bridge IO interface (localhost HTTP, /api/v1/bridge/*)
- (future) websocket_plugin: WebSocket interface

These plugins are OPTIONAL and modular - systems may have zero, one, or multiple
I/O interface plugins enabled depending on their needs.

Contract Requirements (ENFORCED via ABC):
=========================================

REQUIRED ACTIONS (must be implemented):
---------------------------------------
1. start_interface() - Initiates the I/O interface
   - Console: starts console prompt loop
   - Telegram: connects to Telegram API
   - JSON-RPC: starts Unix socket server
   - REST: starts HTTP server

2. post_message() - Sends messages to clients
   - MUST accept: params with 'message' key, state with 'session_id'
   - MUST send message to the appropriate client session
   - MUST return ActionResult dict

3. stop_interface() - Stops the I/O interface and cleans up
   - MUST stop accepting new connections/input
   - MUST clean up resources (sockets, connections, etc.)
   - MUST return ActionResult dict

4. get_supported_capabilities() - Declares plugin's delivery capabilities
   - MUST return a set of IOCapability values
   - Used by IOInterfaceService to determine artifact delivery method
   - Common: TEXT, RICH_TEXT, FILE_UPLOAD, IMAGE_UPLOAD, URL_ONLY

INBOUND INPUT HANDLING (transport-specific):
--------------------------------------------
Current concrete IO plugins do NOT expose a discoverable inbound platform
process such as receive_user_input().

Instead, they accept external input through their transport runtime and submit
directly into the normal action pipeline by:
- creating or resolving a session
- creating a flow
- building the initial action definition
- calling ActionFactory.submit_action_definition()

For chat-style input, current concrete plugins typically submit directly to
process_results (VERTEX inference) via build_initial_vertex_action(), with
legacy @ commands handled through at_command_processor.parse_at_command().

REQUIRED DEPENDENCIES (injected automatically):
-----------------------------------------------
1. action_factory: Injected via PluginBase.set_action_factory()
   - Used to submit actions from external input

2. flow_manager: Injected via IOInterfacePlugin.set_flow_manager()
   - Used to track action/flow completion
   - Required for turn-based interfaces (JSON-RPC, REST)

3. memory_service: Injected via IOInterfacePlugin.set_memory_service()
   - Used to store/retrieve interaction history across all interfaces

4. at_command_processor: Injected via IOInterfacePlugin.set_at_command_processor()
   - Shared service for parsing and executing @ commands
   - Handles @file.json, @{"action": "inline"}, @[{...}] formats

5. compilation_context_builder: Injected via IOInterfacePlugin.set_compilation_context_builder()
   - Shared service for building compilation context for action submission
   - Provides consistent runtime_args (DATE, TIME, TIMEZONE, SESSION_ID, etc.)

6. context_management_service: Injected via IOInterfacePlugin.set_context_management_service()
   - Available for plugins that need context management
   - NOTE: IO interfaces should NOT set context_id - plugins handling actions own their contexts

IMPLEMENTATION PATTERN:
-----------------------
class MyIOPlugin(ServicePlugin, IOInterfacePlugin):
    def __init__(self):
        super().__init__()
        self._flow_manager = None
        self._memory_service = None
        self._at_command_processor = None
        self._compilation_context_builder = None
        self._context_management_service = None
        self._app_home: str | None = None

    def prepare_for_readiness(self) -> None:
        super().prepare_for_readiness()
        # Get APP_HOME from orchestrator
        if self.orchestrator_ref:
            self._app_home = getattr(self.orchestrator_ref, "APP_HOME", None)

    @platform_process(name="start_interface", ...)
    def start_interface(self, params, state):
        # Start listening for external input
        # Use self._app_home for application home directory
        pass

    def _handle_external_input(self, user_input: str, session_id: str, flow_id: str):
        # Resolve @ commands or build the initial vertex action
        # Then submit via ActionFactory.submit_action_definition(...)
        pass

    @platform_process(name="post_message", ...)
    def post_message(self, params, state):
        # Send message to client session
        pass

    @platform_process(name="stop_interface", ...)
    def stop_interface(self, params, state):
        # Stop interface and cleanup
        pass
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Protocol

from ananta.interfaces.io_capabilities import IOCapability

if TYPE_CHECKING:
    from ananta.core.orchestration.interfaces import ISessionManager
    from ananta.core.orchestration.managers.flow_manager import FlowManager
    from ananta.core.services.compilation_context_builder import CompilationContextBuilder
    from ananta.services.context_management import ContextManagementService


class AtCommandProcessorProtocol(Protocol):
    """Protocol for @ command processor interface."""

    def is_at_command(self, user_input: str) -> bool:
        """Check if input is an @ command."""
        ...

    def parse_at_command(
        self,
        user_input: str,
        session_id: str | None = None,
        flow_id: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Parse @ command into action definition(s)."""
        ...


# IO interfaces submit user input directly to process_results (VERTEX inference).
# The model determines the appropriate next action via the ReAct loop.


class IOInterfacePlugin(ABC):
    """Abstract base class for I/O interface plugins.

    Plugins implementing this interface MUST implement:
    - start_interface(): Start the I/O interface
    - post_message(): Send messages to clients
    - stop_interface(): Stop the interface and cleanup
    - get_supported_capabilities(): Declare supported delivery capabilities

    Dependencies are automatically injected:
    - flow_manager: For tracking action/flow completion
    - memory_service: For interaction history
    - at_command_processor: For @ command parsing

    Subclasses should decorate their implementations with @platform_process
    to expose them as discoverable processes.

    Inbound input handling is transport-specific and is not currently modeled as
    an abstract discoverable platform action on this interface. Concrete chat
    plugins accept transport input and submit directly into the normal action
    pipeline, typically by building the initial process_results VERTEX action.
    """

    # Instance attributes for dependency injection
    name: str  # Plugin name (required for IOInterfacePluginProtocol)
    _flow_manager: FlowManager | None
    _memory_service: Any
    _at_command_processor: AtCommandProcessorProtocol | None
    _compilation_context_builder: CompilationContextBuilder | None
    _session_manager: ISessionManager | None
    _context_management_service: ContextManagementService | None

    @abstractmethod
    def start_interface(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Start the I/O interface.

        This method should be decorated with @platform_process in the
        implementation. Current concrete IO plugins use the action name
        'start_interface', with the plugin namespace distinguishing the
        discoverable process key.

        APP_HOME should be obtained from self.orchestrator_ref.APP_HOME in
        prepare_for_readiness() and stored as self._app_home.

        Args:
            params: Action parameters (interface-specific configuration)
            state: Current orchestration state

        Returns:
            ActionResult dict with status, data, actions, error, timestamp
        """
        ...

    # IO interfaces submit directly to process_results for all user input

    @abstractmethod
    def post_message(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a message (and optional attachments) to the client.

        This method should be decorated with @platform_process in the implementation.
        The action name MUST be 'post_message' for consistency across interfaces.

        Required params:
        - message (str): The message content to send to the client
        Optional params:
        - attachments (list[dict]): Each attachment includes namespace, artifact_type, blob_id,
          media_type, size_bytes, caption, and additional_metadata.

        Required state:
        - session_id (str): The session to send the message to

        Args:
            params: Action parameters containing 'message' key and optional attachments
            state: Current orchestration state containing 'session_id'

        Returns:
            ActionResult dict with status, data, actions, error, timestamp
        """
        ...

    @abstractmethod
    def stop_interface(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Stop the I/O interface and cleanup resources.

        This method should be decorated with @platform_process in the
        implementation. Current concrete IO plugins use the action name
        'stop_interface', with the plugin namespace distinguishing the
        discoverable process key.

        APP_HOME should be obtained from self.orchestrator_ref.APP_HOME in
        prepare_for_readiness() and stored as self._app_home.

        Args:
            params: Action parameters (usually empty for stop)
            state: Current orchestration state

        Returns:
            ActionResult dict with status, data, actions, error, timestamp
        """
        ...

    def set_flow_manager(self, flow_manager: FlowManager) -> None:
        """Inject flow manager for tracking action/flow completion.

        Called automatically during plugin initialization if plugin implements
        IOInterfacePlugin interface.

        Args:
            flow_manager: The flow manager instance to inject
        """
        self._flow_manager = flow_manager

    def set_memory_service(self, memory_service: Any) -> None:
        """Inject memory service for interaction history storage/retrieval.

        Called automatically during plugin initialization if plugin implements
        IOInterfacePlugin interface.

        Args:
            memory_service: The memory service instance to inject
        """
        self._memory_service = memory_service

    def set_at_command_processor(self, at_command_processor: AtCommandProcessorProtocol) -> None:
        """Inject @ command processor for parsing and executing @ commands.

        Called automatically during plugin initialization if plugin implements
        IOInterfacePlugin interface.

        Args:
            at_command_processor: The @ command processor instance to inject
        """
        self._at_command_processor = at_command_processor

    def set_compilation_context_builder(
        self, compilation_context_builder: CompilationContextBuilder
    ) -> None:
        """Inject compilation context builder for action submission.

        Called automatically during plugin initialization if plugin implements
        IOInterfacePlugin interface.

        The compilation context builder provides consistent runtime_args for
        template variable resolution (DATE, TIME, TIMEZONE, SESSION_ID, etc.).

        Args:
            compilation_context_builder: The compilation context builder instance to inject
        """
        self._compilation_context_builder = compilation_context_builder

    def set_session_manager(self, session_manager: ISessionManager) -> None:
        """Inject session manager for session creation and management.

        Called automatically during plugin initialization if plugin implements
        IOInterfacePlugin interface.

        The session manager enables IO plugins to:
        - Create real sessions in core__sessions
        - Reuse existing active sessions within timeout window
        - Update session activity on each interaction

        Args:
            session_manager: The session manager instance to inject
        """
        self._session_manager = session_manager

    def set_context_management_service(
        self, context_management_service: ContextManagementService
    ) -> None:
        """Inject context management service.

        Called automatically during plugin initialization if plugin implements
        IOInterfacePlugin interface.

        NOTE: IO interfaces should NOT set context_id when submitting actions.
        Contexts are plugin-owned - the plugin handling an action determines
        which context to use for INPUT/OUTPUT events.

        Args:
            context_management_service: The context management service instance to inject
        """
        self._context_management_service = context_management_service

    @abstractmethod
    def get_supported_capabilities(self) -> set[IOCapability]:
        """Return the set of delivery capabilities supported by this plugin.

        This method MUST be implemented by all IO interface plugins to declare
        their supported capabilities. The IOInterfaceService uses this to
        determine how to deliver artifacts (inline files vs text fallback).

        Common capabilities:
        - IOCapability.TEXT: Basic text messages (all plugins should support)
        - IOCapability.RICH_TEXT: Formatted text (markdown, etc.)
        - IOCapability.FILE_UPLOAD: Can send files as attachments
        - IOCapability.IMAGE_UPLOAD: Can send images inline
        - IOCapability.URL_ONLY: Can only send URLs, not files

        Returns:
            Set of IOCapability values this plugin supports.
        """
        ...

    def deliver_artifact(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Deliver an artifact (file/image) to the client.

        This is an optional method that provides capability-aware artifact delivery.
        Default implementation delegates to post_message. Plugins with file upload
        capabilities (Slack, Telegram) may override this to provide optimized handling.

        Args:
            params: Action parameters containing:
                - job_result_ref (str): Reference to async job with artifact data
                - message (str, optional): Accompanying text message
            state: Current orchestration state containing 'session_id'

        Returns:
            ActionResult dict with status, data, actions, error, timestamp
        """
        # Default: delegate to post_message with the same params
        return self.post_message(params, state)
