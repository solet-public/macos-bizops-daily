from setuptools import find_packages, setup

setup(
    name="ananta",
    version="2.0.0",  # Must be > 1.3.9 (external PyPI 'ananta' package) to avoid conflicts
    description="A flexible framework for AI-driven applications",
    license="Apache-2.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    python_requires="==3.13.*",
    install_requires=[
        "requests>=2.32.0",
        "python-dotenv>=1.0.0",
        "click>=8.0.0",
        "solet-setup-contracts",
    ],
    extras_require={
        # The platform commit gate's own toolchain (git-controller-commit
        # skill, Steps 2/3/4-5), undeclared until this audit:
        # workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
        # Deliberately NOT absence-tolerant, unlike code_vetting_plugin's
        # `typecheck`/`coverage` extras (correctly optional: a missing
        # foreign-target scanner is a disclosed coverage gap, never a gate
        # failure). These three back the commit gate itself: an
        # absence-tolerant commit gate would silently skip static analysis
        # on every commit. Do not "harmonise" this group with `typecheck`.
        "gate": [
            "ruff>=0.15",
            "pyright>=1.1",
            "radon>=6.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "ananta=ananta.cli:sync_main",
        ],
    },
)
