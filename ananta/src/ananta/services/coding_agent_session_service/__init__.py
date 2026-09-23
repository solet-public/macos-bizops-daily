"""Coding agent session service package.

Holds the ``@service_interface_process`` public-API surface for the
:class:`CodingAgentSessionServiceInterface` ABC. The actual contract
lives in ``ananta.interfaces.coding_agent_session_service_interface``;
this package's :mod:`interfaces.public` module carries the decorator
registrations that make the four verbs reachable via
``service_interface::coding_agent_session_service::*`` keys.
"""
