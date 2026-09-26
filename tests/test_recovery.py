import tempfile
import unittest
from pathlib import Path
from village.recovery import backup_manifest, verify_manifest

class RecoveryTests(unittest.TestCase):
    def test_manifest_round_trip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); item=root/'state.json'; item.write_text('{"ok":true}\n'); manifest=backup_manifest([item], root=root)
            self.assertEqual(verify_manifest(manifest, root=root), []); item.write_text('{"ok":false}\n')
            self.assertIn('checksum mismatch: state.json', verify_manifest(manifest, root=root))
    def test_paths_outside_root_are_rejected(self):
        with tempfile.TemporaryDirectory() as td, tempfile.NamedTemporaryFile() as outside:
            with self.assertRaises(ValueError): backup_manifest([Path(outside.name)], root=Path(td))
