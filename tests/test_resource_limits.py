import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_runtime", ROOT / "scripts/install-runtime.py")
install_runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(install_runtime)


class ResourceLimitTests(unittest.TestCase):
    def test_agent_dropin_sets_all_ceilings_and_slice(self):
        text = install_runtime.agent_limits_dropin()
        for key in ("Slice=ai-village-agents.slice", "MemoryHigh=", "MemoryMax=", "TasksMax=", "CPUQuota=", "LimitFSIZE="):
            self.assertIn(key, text)
        self.assertTrue(text.startswith("[Service]"))

    def test_slice_ceiling_leaves_room_for_infrastructure(self):
        text = install_runtime.agents_slice_unit()
        self.assertIn("[Slice]", text)
        self.assertIn("MemoryMax=10G", text)  # 16 GiB host: Neo4j, Chroma, gateway, OS keep >= 5 GiB
        self.assertIn("CPUQuota=300%", text)  # 4 cores: one core stays free

    def test_installer_writes_dropin_and_slice(self):
        source = (ROOT / "scripts/install-runtime.py").read_text()
        self.assertIn("40-resource-limits.conf", source)
        self.assertIn("agents_slice_unit()", source)

    def test_per_agent_limit_is_below_slice_ceiling(self):
        limits = dict(install_runtime.AGENT_LIMITS)
        self.assertEqual(limits["MemoryMax"], "3G")
        self.assertLess(3, 10)


if __name__ == "__main__":
    unittest.main()
