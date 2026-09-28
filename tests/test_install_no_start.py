"""P50 regression: --no-start must leave already-running resident services
completely untouched, not stop them and then simply skip the restart.

Observed live during the Gazette (P47/P48/P49) validation: after a
--no-start install into an actively running village, 8 of 9 residents were
found stopped ("inactive (dead)", clean exit code 0/SUCCESS, no crash) and
never came back - only the one agent an operator happened to restart
afterward for scoped validation survived. The flag's own help text
("without starting or restarting resident services") never mentioned
stopping anything.
"""
import unittest
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('install_runtime', Path(__file__).parents[1] / 'scripts/install-runtime.py')
install_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_runtime)


class ShouldStopActiveServicesTests(unittest.TestCase):
    def test_no_start_leaves_active_services_untouched(self):
        # The exact bug: previously this ignored no_start entirely for the
        # stop decision and only gated the later restart, so --no-start
        # still stopped every active resident.
        self.assertFalse(install_runtime.should_stop_active_services(
            active=['ai-village-agent-02-explorer.service'], provision_only=False, no_start=True))

    def test_normal_install_still_stops_active_services_to_swap_code(self):
        # Without --no-start, stopping first is still required: a running
        # Python process holds its old module in memory and would not pick
        # up new files until restarted, so a normal (non-provision-only,
        # non-no-start) install must still stop what it is about to replace.
        self.assertTrue(install_runtime.should_stop_active_services(
            active=['ai-village-agent-02-explorer.service'], provision_only=False, no_start=False))

    def test_nothing_to_stop_when_none_active(self):
        self.assertFalse(install_runtime.should_stop_active_services(
            active=[], provision_only=False, no_start=False))

    def test_provision_only_never_stops_anything(self):
        self.assertFalse(install_runtime.should_stop_active_services(
            active=['ai-village-agent-02-explorer.service'], provision_only=True, no_start=False))


if __name__ == '__main__':
    unittest.main()
