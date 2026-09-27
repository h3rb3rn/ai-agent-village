"""Regression for a Village-Board navigation bug reported 2026-09-27: clicking a
topic on the dashboard showed a real post, but the navigation menu highlighted a
DIFFERENT topic than the one clicked (most visible on the single-column mobile
layout).

Root cause: renderBoard() rendered thread rows using a position index (data-thread
="0","1",...) into a list SORTED most-recent-first, but the click handler
independently rebuilt an UNSORTED Map from the raw events and indexed into that
with the same position - so index i pointed at a different thread than the one
rendered at position i. The fix makes both use one shared, order-independent
string key instead of a positional index.

This runs the real web/observatory.js in a minimal DOM-free Node vm sandbox (no
jsdom, no new dependency - Node's built-in vm module) and clicks a rendered row
exactly as a browser would. Skipped where `node` is not on PATH (e.g. a minimal
host without Node.js); web/observatory.js is otherwise only exercised in a real
browser, so this is strictly additional coverage, never required for the suite.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "fixtures" / "board_navigation_harness.js"


@unittest.skipUnless(shutil.which("node"), "Node.js not available on PATH")
class BoardNavigationTests(unittest.TestCase):
    def run_harness(self):
        result = subprocess.run(
            ["node", str(HARNESS), str(ROOT / "web/observatory.js")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_clicking_a_thread_selects_that_same_thread(self):
        outcome = self.run_harness()
        self.assertNotIn("error", outcome, outcome)
        # The clicked row was the most-recently-posted thread ("Gamma"), rendered
        # first because the list is sorted most-recent-first while the underlying
        # events were inserted oldest-first - exactly the order mismatch that
        # exposed the bug. The selected thread's title must match what was clicked.
        self.assertEqual(outcome["selectedTitle"], "Gamma topic first line.", outcome)

    def test_data_thread_attribute_is_a_stable_key_not_a_position_index(self):
        # The original bug used a plain position index ("0","1","2") as the
        # click target, which is exactly what let it silently point at the wrong
        # thread once row order and insertion order diverged. Guard against that
        # shape coming back.
        outcome = self.run_harness()
        self.assertFalse(outcome["clickedKey"].isdigit(), outcome)


if __name__ == "__main__":
    unittest.main()
