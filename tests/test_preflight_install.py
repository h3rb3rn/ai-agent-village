import tempfile
import unittest
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("preflight_install", "scripts/preflight-install.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
check = module.check


class PreflightInstallTests(unittest.TestCase):
    def test_missing_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            errors = check(Path(td))
            self.assertTrue(any("missing release source" in item for item in errors))

    def test_current_checkout_passes_preflight(self):
        errors = check(Path(__file__).resolve().parents[1])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
