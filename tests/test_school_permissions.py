"""P145: the shared school database must be readable by every resident. Local, synthetic."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from village.school import Curriculum


class SchoolPermissionTests(unittest.TestCase):
    def test_database_is_group_shared_even_under_umask_077(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.umask(0o077)
            try:
                db = Path(tmp) / 'school.sqlite3'
                Curriculum(db)
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o660)

    def test_existing_private_database_is_repaired_by_its_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'school.sqlite3'
            Curriculum(db)
            db.chmod(0o600)
            Curriculum(db)
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o660)


if __name__ == '__main__':
    unittest.main()
