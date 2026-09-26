import unittest
from village.research_tasks import ResearchTask, validate_manifest


class ResearchTaskTests(unittest.TestCase):
    def test_manifest_is_validated(self):
        task = ResearchTask("artifact-1", "reproduction", "Create a checksum file", "checksum matches", 256)
        self.assertEqual(validate_manifest([task])[0]["task_id"], "artifact-1")

    def test_duplicate_or_empty_manifest_fails(self):
        task = ResearchTask("same", "x", "p", "s")
        with self.assertRaises(ValueError): validate_manifest([task, task])
        with self.assertRaises(ValueError): validate_manifest([])
