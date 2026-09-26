import ast
import unittest
from pathlib import Path


class EventProducerShellTests(unittest.TestCase):
    def test_lifecycle_scripts_call_shared_helper(self):
        for name in ("scripts/village-resume", "scripts/village-update"):
            text = Path(name).read_text()
            self.assertIn("/usr/local/lib/ai-village/append-event.py", text)
            self.assertNotIn("jq -cn", text)

    def test_telemetry_uses_shared_helper(self):
        tree = ast.parse(Path("web/telemetry-collector.py").read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertTrue(any(node.module == "village.events" and any(a.name == "append_event" for a in node.names) for node in imports))


if __name__ == "__main__":
    unittest.main()
