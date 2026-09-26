import ast
import unittest
from pathlib import Path


class EventProducerSourceTests(unittest.TestCase):
    def test_authority_uses_shared_event_envelope(self):
        tree = ast.parse(Path("village/authority.py").read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertTrue(any(node.module == "village.events" and any(a.name == "append_event" for a in node.names) for node in imports))

    def test_webui_uses_shared_event_envelope_for_contact(self):
        tree = ast.parse(Path("web/webui.py").read_text())
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertTrue(any(node.module == "village.events" and any(a.name == "append_event" for a in node.names) for node in imports))
        self.assertIn('kind="organic_message_received"', Path("web/webui.py").read_text())


if __name__ == "__main__":
    unittest.main()
