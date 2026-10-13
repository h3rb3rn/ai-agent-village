"""P160: a re-run of the same successful command is execution, not new capability (operator view only)."""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('VILLAGE_ROOT', '/tmp/ai-village-outcome-distinct')
os.environ.setdefault('VILLAGE_SIGNAL_AUTH_USER', 'synthetic-user')
os.environ.setdefault('VILLAGE_SIGNAL_AUTH_PASSWORD', 'synthetic-password')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from observer import outcome_stats, result_signature  # noqa: E402
from web.webui import skill_history  # noqa: E402

LIVE = 'cd /var/lib/ai-village/users/explorer && python3 habitat_inventory.py'


def run(agent, command, ok=True, ts='2026-10-13T18:18:22+00:00'):
    detail = (f'result=success; command={command}; output={{"load1": 0.05}}' if ok
              else f'result=failure(1); command={command}; output=boom')
    return [dict(agent=agent, event='command_start', detail=f'command={command}', timestamp=ts),
            dict(agent=agent, event='command_result', detail=detail, timestamp=ts)]


class DistinctSuccessTests(unittest.TestCase):
    def test_live_explorer_eleven_identical_runs_are_one_distinct_success(self):
        events = [e for _ in range(11) for e in run('02-explorer', LIVE)]  # 2026-10-13 18:18-20:26 UTC
        s = outcome_stats(events)['02-explorer']
        self.assertEqual((s['success'], s['distinct_success'], s['repeated_success']), (11, 1, 10))
        self.assertEqual(s['success_rate'], 1.0)  # the exit-code rate itself stays as it was

    def test_whitespace_variants_are_the_same_command(self):
        self.assertEqual(result_signature('result=success; command=ls   -l ; output=x'), 'ls -l')
        s = outcome_stats(run('a', 'ls -l') + run('a', 'ls  -l'))['a']
        self.assertEqual(s['distinct_success'], 1)

    def test_different_commands_and_failures(self):
        s = outcome_stats(run('a', 'ls') + run('a', 'pwd') + run('a', 'ls', ok=False))['a']
        self.assertEqual((s['success'], s['distinct_success'], s['failure']), (2, 2, 1))

    def test_a_result_without_command_is_never_merged(self):
        s = outcome_stats([dict(agent='a', event='command_result', detail='result=success; output=x')] * 2)['a']
        self.assertEqual((s['success'], s['distinct_success']), (2, 2))

    def test_skill_index_is_no_longer_raised_by_repeats(self):
        rows = skill_history([e for _ in range(11) for e in run('02-explorer', LIVE)])
        row = rows[0]
        self.assertEqual((row['success'], row['distinct_success'], row['repeated_success']), (11, 1, 10))
        self.assertEqual(row['capability_index_exitcode'], 90.0)  # the live value before P160
        self.assertLess(row['capability_index'], 30)

    def test_distinct_commands_keep_the_full_index(self):
        events = run('a', 'ls') + run('a', 'pwd') + run('a', 'date')
        row = skill_history(events)[0]
        self.assertEqual(row['capability_index'], row['capability_index_exitcode'])

    def test_each_window_counts_on_its_own(self):
        events = run('a', 'ls', ts='2026-10-13T18:00:00+00:00') + run('a', 'ls', ts='2026-10-13T19:00:00+00:00')
        self.assertEqual([r['distinct_success'] for r in skill_history(events)], [1, 1])


if __name__ == '__main__':
    unittest.main()
