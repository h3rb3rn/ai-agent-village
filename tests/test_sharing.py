"""P149: own files in the shared village area become group-readable, nothing else changes."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from village import sharing
from village.sharing import share_group_readable, wanted_mode


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class WantedModeTests(unittest.TestCase):
    def test_file_gets_group_read_only(self):
        self.assertEqual(wanted_mode(stat.S_IFREG | 0o600), 0o640)

    def test_executable_and_directory_get_group_execute(self):
        self.assertEqual(wanted_mode(stat.S_IFREG | 0o700), 0o750)
        self.assertEqual(wanted_mode(stat.S_IFDIR | 0o2700), 0o2750)

    def test_never_adds_write(self):
        self.assertFalse(wanted_mode(stat.S_IFREG | 0o600) & (stat.S_IWGRP | stat.S_IWOTH))


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_files_and_dirs_become_group_readable(self):
        (self.root / 'ops').mkdir()
        (self.root / 'ops' / 'gpu_alert.sh').write_text('#!/bin/sh\n')
        os.chmod(self.root / 'ops' / 'gpu_alert.sh', 0o700)
        (self.root / 'shared_gpu_log.txt').write_text('log')
        counts = share_group_readable(self.root)
        self.assertEqual(mode(self.root / 'ops'), 0o750)
        self.assertEqual(mode(self.root / 'ops' / 'gpu_alert.sh'), 0o750)
        self.assertEqual(mode(self.root / 'shared_gpu_log.txt'), 0o640)
        self.assertEqual(counts['fixed'], 3)
        self.assertEqual(share_group_readable(self.root)['fixed'], 0)  # idempotent

    def test_private_and_service_areas_are_untouched(self):
        for name in sorted(sharing.EXCLUDED_TOP):
            (self.root / name).mkdir()
            (self.root / name / 'private.txt').write_text('secret')
        share_group_readable(self.root)
        for name in sharing.EXCLUDED_TOP:
            self.assertEqual(mode(self.root / name), 0o700, name)
            self.assertEqual(mode(self.root / name / 'private.txt'), 0o600, name)

    def test_foreign_entries_are_untouched(self):
        (self.root / 'storage').mkdir()
        (self.root / 'storage' / 'x.txt').write_text('x')
        counts = share_group_readable(self.root, uid=os.geteuid() + 1)
        self.assertEqual(counts['fixed'], 0)
        self.assertEqual(mode(self.root / 'storage' / 'x.txt'), 0o600)

    def test_symlink_to_private_file_is_not_followed(self):
        (self.root / 'users').mkdir()
        secret = self.root / 'users' / 'secret.txt'
        secret.write_text('s')
        (self.root / 'link.txt').symlink_to(secret)
        share_group_readable(self.root)
        self.assertEqual(mode(secret), 0o600)

    def test_swapped_inode_is_not_changed(self):
        """A name swapped between scan and chmod (peer race) is refused."""
        (self.root / 'a.txt').write_text('a')
        other = self.root / 'b.txt'
        other.write_text('b')
        seen = os.stat(other)
        self.assertFalse(sharing._fix(str(self.root / 'a.txt'), seen, os.geteuid()))
        self.assertEqual(mode(self.root / 'a.txt'), 0o600)

    def test_limit_bounds_the_walk(self):
        for i in range(10):
            (self.root / f'f{i}.txt').write_text('x')
        self.assertEqual(share_group_readable(self.root, limit=4)['scanned'], 4)

    def test_missing_root_is_reported_not_raised(self):
        self.assertEqual(share_group_readable(self.root / 'missing')['errors'], 1)


class RuntimeHookTests(unittest.TestCase):
    def setUp(self):
        from web.runtime import Resident
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = Resident.__new__(Resident)
        self.agent.root = self.root
        self.agent.events = []
        self.agent.event = lambda kind, detail='', warn=False: self.agent.events.append((kind, detail))

    def tearDown(self):
        self.tmp.cleanup()

    def test_sweep_runs_and_reports(self):
        self.agent.env = {}
        (self.root / 'note.txt').write_text('n')
        os.chmod(self.root / 'note.txt', 0o600)
        self.agent.share_own_files()
        self.assertEqual(mode(self.root / 'note.txt'), 0o640)
        self.assertEqual(self.agent.events[0][0], 'shared_files_readable')

    def test_switch_off(self):
        self.agent.env = {'VILLAGE_SHARE_SWEEP': '0'}
        (self.root / 'note.txt').write_text('n')
        os.chmod(self.root / 'note.txt', 0o600)
        self.agent.share_own_files()
        self.assertEqual(mode(self.root / 'note.txt'), 0o600)

    def test_failure_never_stops_the_cycle(self):
        self.agent.env = {}
        with patch('web.runtime.share_group_readable', side_effect=RuntimeError('boom')):
            self.agent.share_own_files()
        self.assertEqual(self.agent.events[0][0], 'share_sweep_failed')


if __name__ == '__main__':
    unittest.main()
