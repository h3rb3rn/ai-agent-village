"""P34: deterministic error auditor.

Detects known resident mistakes from already-recorded events (never invents an
error), explains problem + solution, and routes: a category seen from >=2
distinct agents becomes shared community knowledge; a single-agent category
stays private (delivered as a direct inbox message). Every finding is also
persisted as a structured (rejected, corrected) example for later fine-tuning
export (the operator's stated goal: synthesize training samples for LUMI-G /
unsloth from this exact data).
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from village.auditor import (
    AuditStore, audit_cycle, deliver, detect_foreign_home_access,
    detect_format_violation, detect_repeated_action, scan,
)
from village.coordinator import CoordinationStore


def event(agent, kind, detail, event_id=None, ts="2026-09-27T20:00:00+00:00", model=""):
    return {"agent": agent, "event": kind, "detail": detail, "event_id": event_id or f"{agent}-{kind}-{ts}",
            "timestamp": ts, "model": model}


class DetectorTests(unittest.TestCase):
    def test_foreign_home_access_extracts_target_and_command(self):
        e = event("01-king", "foreign_home_blocked",
                  "target_agent=08-logician; command_prefix=cd /var/lib/ai-village/users/logician/venv && pip install chromadb")
        findings = detect_foreign_home_access([e])
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.agent, "01-king")
        self.assertIn("08-logician", f.problem)
        self.assertIn("08-logician", f.solution)
        self.assertIn("pip install chromadb", f.rejected_example)

    def test_repeated_action_extracts_action_name(self):
        e = event("09-chronicler", "escalation", "repeated action blocked: board_message")
        findings = detect_repeated_action([e])
        self.assertEqual(findings[0].agent, "09-chronicler")
        self.assertIn("board_message", findings[0].problem)

    def test_format_violation_only_fires_for_known_reasons(self):
        good = event("04-artisan", "invalid_decision_detail",
                     "reason=multiple action blocks; preview=```village-action\\n{a}\\n``` more text")
        unknown_reason = event("04-artisan", "invalid_decision_detail", "reason=some new reason; preview=x")
        self.assertEqual(len(detect_format_violation([good])), 1)
        self.assertEqual(len(detect_format_violation([unknown_reason])), 0)
        self.assertIn("multiple action blocks", detect_format_violation([good])[0].problem)

    def test_unrelated_events_produce_no_findings(self):
        e = event("02-explorer", "command_result", "result=success; command=ls")
        self.assertEqual(scan([e]), [])

    def test_scan_runs_every_signature(self):
        events = [
            event("01-king", "foreign_home_blocked", "target_agent=08-logician; command_prefix=x"),
            event("09-chronicler", "escalation", "repeated action blocked: board_message"),
        ]
        categories = {f.category for f in scan(events)}
        self.assertEqual(categories, {"foreign_home_access", "repeated_action"})


class RoutingAndRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-audit-"))
        self.store = AuditStore(self.tmp / "audit.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, agent, category="foreign_home_access", event_id=None):
        return next(iter(detect_foreign_home_access([
            event(agent, "foreign_home_blocked", "target_agent=08-logician; command_prefix=x", event_id=event_id)
        ])))

    def test_first_offender_is_private(self):
        scope = self.store.route(self.make("01-king", event_id="e1"))
        self.assertEqual(scope, "private")

    def test_second_distinct_agent_makes_it_shared(self):
        self.store.route(self.make("01-king", event_id="e1"))
        scope = self.store.route(self.make("06-operator", event_id="e2"))
        self.assertEqual(scope, "shared")
        # and the SAME category now stays shared for a third, still-new agent too
        scope3 = self.store.route(self.make("02-explorer", event_id="e3"))
        self.assertEqual(scope3, "shared")

    def test_same_agent_repeat_within_an_hour_is_rate_limited(self):
        self.store.route(self.make("01-king", event_id="e1"), now="2026-09-27T20:00:00+00:00")
        scope = self.store.route(self.make("01-king", event_id="e2"), now="2026-09-27T20:10:00+00:00")
        self.assertIsNone(scope)

    def test_same_agent_repeat_after_the_window_is_allowed_again(self):
        self.store.route(self.make("01-king", event_id="e1"), now="2026-09-27T20:00:00+00:00")
        scope = self.store.route(self.make("01-king", event_id="e2"), now="2026-09-27T21:01:00+00:00")
        self.assertEqual(scope, "private")

    def test_the_exact_same_source_event_is_never_recorded_twice(self):
        finding = self.make("01-king", event_id="e1")
        self.assertEqual(self.store.route(finding), "private")
        self.assertIsNone(self.store.route(finding))  # identical (source_event_id, category)

    def test_export_pairs_are_finetune_ready_and_never_fabricated(self):
        self.store.route(self.make("01-king", event_id="e1"))
        pairs = self.store.export_finetune_pairs()
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair["rejected"], "x")  # exactly the recorded command, not invented
        self.assertIn("08-logician", pair["chosen"])
        self.assertEqual(pair["category"], "foreign_home_access")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-audit-deliver-"))
        self.coord = CoordinationStore(self.tmp / "coordination.sqlite3", self.tmp)
        self.memory_writer = MagicMock(return_value={"id": "mem1"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_private_finding_becomes_a_direct_inbox_message_not_a_memory_write(self):
        finding = detect_repeated_action([event("09-chronicler", "escalation", "repeated action blocked: board_message")])[0]
        deliver(finding, "private", coordination_store=self.coord, memory_writer=self.memory_writer)
        self.memory_writer.assert_not_called()
        msgs = self.coord.fetch_unacknowledged_messages("09-chronicler", source="direct")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["sender"], "village-auditor")
        self.assertIn("board_message", msgs[0]["content"])

    def test_shared_finding_becomes_a_memory_write_not_an_inbox_message(self):
        finding = detect_repeated_action([event("09-chronicler", "escalation", "repeated action blocked: board_message")])[0]
        deliver(finding, "shared", coordination_store=self.coord, memory_writer=self.memory_writer)
        self.memory_writer.assert_called_once()
        payload = self.memory_writer.call_args.args[0]
        self.assertEqual(payload["scope"], "shared")
        self.assertEqual(payload["kind"], "correction")
        self.assertEqual(self.coord.fetch_unacknowledged_messages("09-chronicler", source="direct"), [])


class AuditCycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-audit-cycle-"))
        self.store = AuditStore(self.tmp / "audit.sqlite3")
        self.coord = CoordinationStore(self.tmp / "coordination.sqlite3", self.tmp)
        self.memory_writer = MagicMock(return_value={"id": "mem1"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_end_to_end_first_offender_private_then_second_offender_shared(self):
        first = [event("01-king", "foreign_home_blocked", "target_agent=08-logician; command_prefix=x", event_id="e1")]
        n1 = audit_cycle(first, self.store, coordination_store=self.coord, memory_writer=self.memory_writer)
        self.assertEqual(n1, 1)
        self.assertEqual(len(self.coord.fetch_unacknowledged_messages("01-king", source="direct")), 1)
        self.memory_writer.assert_not_called()

        second = [event("06-operator", "foreign_home_blocked", "target_agent=08-logician; command_prefix=y", event_id="e2")]
        n2 = audit_cycle(second, self.store, coordination_store=self.coord, memory_writer=self.memory_writer)
        self.assertEqual(n2, 1)
        self.memory_writer.assert_called_once()

    def test_running_the_same_batch_twice_delivers_nothing_the_second_time(self):
        events = [event("01-king", "foreign_home_blocked", "target_agent=08-logician; command_prefix=x", event_id="e1")]
        self.assertEqual(audit_cycle(events, self.store, coordination_store=self.coord, memory_writer=self.memory_writer), 1)
        self.assertEqual(audit_cycle(events, self.store, coordination_store=self.coord, memory_writer=self.memory_writer), 0)


if __name__ == "__main__":
    unittest.main()
