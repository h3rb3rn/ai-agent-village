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

    def test_source_defaults_to_deterministic_and_is_recorded(self):
        self.store.route(self.make("01-king", event_id="e1"))
        summary = self.store.summary()
        self.assertEqual(summary["delivered_by_source"], {"deterministic": 1, "llm": 0})

    def test_source_llm_is_recorded_distinctly(self):
        self.store.route(self.make("01-king", event_id="e1"), source="llm")
        summary = self.store.summary()
        self.assertEqual(summary["delivered_by_source"], {"deterministic": 0, "llm": 1})

    def test_summary_breaks_down_by_scope_and_category(self):
        self.store.route(self.make("01-king", event_id="e1"))  # private
        self.store.route(self.make("06-operator", event_id="e2"))  # shared
        summary = self.store.summary()
        self.assertEqual(summary["delivered_total"], 2)
        self.assertEqual(summary["delivered_by_scope"], {"private": 1, "shared": 1})
        self.assertEqual(summary["delivered_by_category"], {"foreign_home_access": 2})

    def test_rate_limited_finding_is_not_counted_in_summary(self):
        self.store.route(self.make("01-king", event_id="e1"), now="2026-09-27T20:00:00+00:00")
        self.store.route(self.make("01-king", event_id="e2"), now="2026-09-27T20:10:00+00:00")  # rate-limited
        self.assertEqual(self.store.summary()["delivered_total"], 1)

    def test_record_cycle_accumulates_across_calls(self):
        self.store.record_cycle({"deterministic_findings": 3, "deterministic_delivered": 2,
                                  "llm_candidates": 1, "llm_findings": 1, "llm_delivered": 1, "llm_errors": 0})
        self.store.record_cycle({"deterministic_findings": 1, "deterministic_delivered": 0,
                                  "llm_candidates": 2, "llm_findings": 0, "llm_delivered": 0, "llm_errors": 2})
        summary = self.store.summary()
        self.assertEqual(summary["cycles_run"], 2)
        self.assertEqual(summary["deterministic_findings"], 4)
        self.assertEqual(summary["deterministic_delivered"], 2)
        self.assertEqual(summary["llm_candidates"], 3)
        self.assertEqual(summary["llm_findings"], 1)
        self.assertEqual(summary["llm_delivered"], 1)
        self.assertEqual(summary["llm_unresolved"], 2)
        self.assertIsNotNone(summary["last_cycle_at"])

    def test_summary_on_a_fresh_store_is_all_zero_not_an_error(self):
        fresh = AuditStore(self.tmp / "fresh.sqlite3")
        summary = fresh.summary()
        self.assertEqual(summary["delivered_total"], 0)
        self.assertEqual(summary["cycles_run"], 0)
        self.assertIsNone(summary["last_cycle_at"])


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


class FailureTallyTests(unittest.TestCase):
    """P46: live evidence on N06-M10 (2026-09-28) showed a single delivered
    correction is read once and forgotten by the next cycle - 03-librarian was
    corrected for the exact same unit-conversion bug 7 times over 3.5 hours,
    01-king/04-artisan hit format_violation on almost every rate-limit reset.
    tally_for_agent() turns the (rejected, corrected) history already in
    audit_log into the cumulative win/fail count the operator asked for -
    "Strichliste... je gewaehlten Weg" - so a stateless resident can be told
    "you have made this exact mistake N times" instead of starting fresh
    every cycle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-tally-"))
        self.store = AuditStore(self.tmp / "audit.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _foreign_home(self, agent, event_id, ts):
        return next(iter(detect_foreign_home_access([
            event(agent, "foreign_home_blocked", "target_agent=08-logician; command_prefix=x", event_id=event_id, ts=ts)
        ])))

    def _format_violation(self, agent, event_id, ts):
        return next(iter(detect_format_violation([
            event(agent, "invalid_decision_detail", "reason=multiple action blocks; preview=broken json",
                 event_id=event_id, ts=ts)
        ])))

    def test_empty_for_an_agent_never_corrected(self):
        self.assertEqual(self.store.tally_for_agent("03-librarian"), [])

    def test_counts_and_orders_worst_offender_first(self):
        # 3 foreign_home_access, 1 format_violation for the same agent. route()'s
        # own rate limit compares against wall-clock time unless `now` is passed
        # explicitly - it must be, or these three calls (made microseconds apart
        # in real time) would rate-limit each other regardless of their
        # synthetic event timestamps.
        self.store.route(self._foreign_home("03-librarian", "e1", "2026-09-27T20:00:00+00:00"), now="2026-09-27T20:00:00+00:00")
        self.store.route(self._foreign_home("03-librarian", "e2", "2026-09-27T21:01:00+00:00"), now="2026-09-27T21:01:00+00:00")
        self.store.route(self._foreign_home("03-librarian", "e3", "2026-09-27T22:02:00+00:00"), now="2026-09-27T22:02:00+00:00")
        self.store.route(self._format_violation("03-librarian", "e4", "2026-09-27T20:30:00+00:00"), now="2026-09-27T20:30:00+00:00")
        tally = self.store.tally_for_agent("03-librarian")
        self.assertEqual(tally[0]["category"], "foreign_home_access")
        self.assertEqual(tally[0]["count"], 3)
        self.assertEqual(tally[1]["category"], "format_violation")
        self.assertEqual(tally[1]["count"], 1)

    def test_includes_the_most_recent_problem_and_solution_text(self):
        self.store.route(self._foreign_home("03-librarian", "e1", "2026-09-27T20:00:00+00:00"))
        tally = self.store.tally_for_agent("03-librarian")
        self.assertIn("private home directory", tally[0]["last_problem"])
        self.assertTrue(tally[0]["last_solution"])

    def test_other_agents_are_never_mixed_in(self):
        self.store.route(self._foreign_home("03-librarian", "e1", "2026-09-27T20:00:00+00:00"))
        self.store.route(self._foreign_home("01-king", "e2", "2026-09-27T21:01:00+00:00"))
        self.assertEqual(len(self.store.tally_for_agent("01-king")), 1)
        self.assertEqual(self.store.tally_for_agent("01-king")[0]["count"], 1)

    def test_limit_bounds_how_many_categories_are_returned(self):
        # 6 distinct format-violation-shaped categories would need 6 different
        # signatures to produce 6 different categories; simulate directly via
        # route() with synthetic AuditFinding-like categories instead.
        from village.auditor import AuditFinding
        for i in range(6):
            finding = AuditFinding(category=f"cat{i}", agent="03-librarian", source_event_id=f"e{i}",
                                   problem="p", solution="s")
            self.store.route(finding)
        self.assertEqual(len(self.store.tally_for_agent("03-librarian", limit=3)), 3)


if __name__ == "__main__":
    unittest.main()
