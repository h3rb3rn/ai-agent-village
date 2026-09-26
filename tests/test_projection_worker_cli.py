import ast
import unittest
from pathlib import Path


class ProjectionWorkerCliTests(unittest.TestCase):
    def test_worker_is_a_loop_without_remediation_commands(self):
        source = Path("scripts/memory-projection-worker.py").read_text()
        tree = ast.parse(source)
        self.assertIn("create_default_projection_worker", source)
        self.assertIn("process_all", source)
        self.assertNotIn("systemctl", source)
        self.assertNotIn("subprocess", source)
        self.assertTrue(any(isinstance(node, ast.While) for node in ast.walk(tree)))
