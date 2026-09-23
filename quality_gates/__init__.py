"""Platform quality gates.

Marks ``quality_gates`` as a package. The standalone gate scripts
(``god_class_check.py``, ``radon_cc_check.py``, …) are invoked directly as
scripts, not imported. The deterministic code-vetting suite that briefly lived
here as ``quality_gates.vetting`` was promoted to the ``code_vetting_plugin``
package at W3 (2026-07-20).
"""
