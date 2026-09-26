import tempfile
import unittest
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('install_runtime', Path(__file__).parents[1] / 'scripts/install-runtime.py')
install_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_runtime)


class InstallReleaseGuardTests(unittest.TestCase):
    def test_manifest_sources_are_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'web').mkdir()
            with self.assertRaises(ValueError):
                install_runtime.validate_release_sources(root)

    def test_current_checkout_contains_manifest_sources(self):
        root = Path(__file__).parents[1]
        hashes = install_runtime.validate_release_sources(root)
        self.assertGreater(len(hashes), 20)


if __name__ == '__main__':
    unittest.main()
