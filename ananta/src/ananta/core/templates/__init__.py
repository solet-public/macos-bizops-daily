"""Template processing package.

This package handles all template-related functionality including:
- Template function registry and execution
- Template variable resolution
- Parameter processing
- Template exceptions
- Plugin interface for template systems

Note: To avoid circular imports, exceptions should be imported directly:
    from ananta.core.templates.template_exceptions import TemplateResolutionError
"""

# Note: Minimal imports to avoid circular dependencies
# Import specific classes directly from their modules as needed

__all__ = [
    "parameter_processor",
    "template_exceptions",
    "template_functions",
    "template_plugin_interface",
    "variable_resolver",
]
