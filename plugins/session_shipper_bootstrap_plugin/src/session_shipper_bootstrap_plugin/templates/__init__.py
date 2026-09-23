"""Bundled installer templates for the shipper-bootstrap renderer.

The ``*.template`` files in this directory are loaded via
:func:`importlib.resources.files` from
:mod:`session_shipper_bootstrap_plugin.renderer`. The package marker
file is required so the renderer can address the templates as a
sub-package rather than a bare directory.
"""
