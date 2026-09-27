"""Build and install the wheel to verify its pinned acquisition resources."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "coreai_embeddings_plugin"
RESOURCE_DIGESTS = {
    "LICENSE-2.0.txt": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "distribution_manifest.json": "8b732f8d83177b2ac18efffef0897789cdfd0fb48bcee08cfe8c98fe7bc7fd74",
}


class WheelResourcesSmoke(unittest.TestCase):
    def test_installed_wheel_resources_and_acquisition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="coreai-wheel-resources-") as directory:
            root = Path(directory)
            source = root / "source"
            shutil.copytree(PLUGIN_ROOT, source, ignore=shutil.ignore_patterns("__pycache__"))
            wheels = root / "wheels"
            wheels.mkdir()
            build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheels),
                    str(source),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            wheel_files = list(wheels.glob("*.whl"))
            self.assertEqual(len(wheel_files), 1)
            wheel = wheel_files[0]
            with zipfile.ZipFile(wheel) as archive:
                for name, digest in RESOURCE_DIGESTS.items():
                    member = f"{PACKAGE}/assets/{name}"
                    self.assertEqual(hashlib.sha256(archive.read(member)).hexdigest(), digest)

            installed = root / "installed"
            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-index",
                    "--target",
                    str(installed),
                    str(wheel),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

            probe = """
import hashlib
import importlib
import pathlib
import sys
import unittest

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
package = importlib.import_module('coreai_embeddings_plugin')
assert pathlib.Path(package.__file__).resolve().is_relative_to(root)
assets = pathlib.Path(importlib.import_module('coreai_embeddings_plugin.assets').__file__).parent
expected = {
    'LICENSE-2.0.txt': 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30',
    'distribution_manifest.json': '8b732f8d83177b2ac18efffef0897789cdfd0fb48bcee08cfe8c98fe7bc7fd74',
}
for name, digest in expected.items():
    assert hashlib.sha256((assets / name).read_bytes()).hexdigest() == digest
test = unittest.defaultTestLoader.loadTestsFromName(
    'coreai_embeddings_plugin.assets.tests.test_acquisition.'
    'AcquisitionTests.test_license_copy_is_required_and_pinned'
)
result = unittest.TextTestRunner(verbosity=2).run(test)
assert result.wasSuccessful()
"""
            verify = subprocess.run(
                [sys.executable, "-I", "-c", probe, str(installed)],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)


if __name__ == "__main__":
    unittest.main()
