from setuptools import find_packages, setup

setup(
    name="actr_memory_plugin",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[],
    entry_points={
        "console_scripts": [
            "actr_memory=actr_memory_plugin.cli:main",
        ],
        "ananta.plugins": [
            "actr_memory_plugin=actr_memory_plugin.plugin:ACTRMemoryPlugin",
        ],
    },
    extras_require={
        "dev": [
            "black==25.1.0",
            "flake8==7.1.2",
            "mypy==1.15.0",
            "pytest==8.3.5",
            "pytest-cov==6.0.0",
        ],
    },
    python_requires="==3.13.*",
)
