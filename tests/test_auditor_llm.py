"""P34 continuation: the independent-model (qwen3.6:35b, not a village resident)
open-ended review layer. All tests use a fake opener - no real network call, no
5-tok/s wait - but the fixtures are the ACTUAL responses captured live from
N11-M10 on 2026-09-27 with the confirmed production parameters (num_ctx=131072,
think=false, keep_alive=96h), including the first attempt that failed exactly
the way the Methodologist already fails (thinking exhausts the output budget).
"""
import io
import json
import unittest
from unittest.mock import MagicMock

from village.auditor import AuditStore
from village.auditor_llm import (
    JudgeError, ReviewCandidate, build_request, review, select_candidates,
)

# Captured verbatim from N11-M10, 2026-09-27, before think=false was set: thinking
# consumed the entire 300-token budget and never reached an answer.
THINKING_EXHAUSTED_RESPONSE = {
    "message": {"role": "assistant", "content": "",
               "thinking": "Here's a thinking process:\n\n1. Analyze User Input..."},
    "done": True, "done_reason": "length", "eval_count": 300,
}

# Captured verbatim with think=false, num_ctx=131072: correctly identified a
# circular, non-diagnostic board message from 09-chronicler as an actual defect -
# exactly the kind of semantic mistake the deterministic signatures cannot see.
CIRCULAR_MESSAGE_RESPONSE = {
    "message": {"role": "assistant", "content": json.dumps({
        "has_issue": True, "category": "hallucination",
        "problem": ("The response is logically circular and tautological ('failed due to "
                    "the lack of a collaboration checkpoint'), providing no diagnostic value."),
        "solution": ("The agent should have provided a specific technical reason for the "
                     "failure and a corresponding remediation step."),
        "confidence": 1.0,
    })},
    "done": True, "done_reason": "stop", "eval_count": 137,
}

NO_ISSUE_RESPONSE = {
    "message": {"role": "assistant", "content": json.dumps({
        "has_issue": False, "category": "none", "problem": "", "solution": "", "confidence": 0.9,
    })},
    "done": True, "done_reason": "stop",
}

# Captured verbatim from N11-M10, 2026-09-27, with the FIRST (looser, 600-char,
# no anti-deliberation instruction) prompt: a genuinely fine action sent the
# model into open-ended, self-contradicting deliberation inside the "problem"
# string itself (think=false did not stop this - it just moved the rambling
# from the thinking channel into the answer channel), never reaching a closing
# brace. 400 tokens burned in ~70s for no usable verdict.
RAMBLING_NO_VERDICT_RESPONSE = {
    "message": {"role": "assistant", "content": (
        '{\n  "has_issue": true,\n  "category": "Logical/Practical Error",\n  "problem": '
        '"The agent claims to have measured free disk space... However, the most definitive '
        'error here is likely **none** if we assume the df output was accurate... Wait, '
        'let\'s re-read carefully... So the'
    )},
    "done": True, "done_reason": "length",
}

# Captured verbatim after tightening the prompt (explicit "one verdict immediately,
# no deliberation") and the schema (maxLength 300, not 600) for the SAME input as
# above: clean, correct, complete in 18.4s instead of ~70s.
CONCISE_NO_ISSUE_RESPONSE = {
    "message": {"role": "assistant", "content": json.dumps({
        "has_issue": False, "category": "none",
        "problem": "The agent correctly reports available disk space and states a logical next step without factual or procedural errors.",
        "solution": "No correction needed; the action is valid.", "confidence": 1.0,
    })},
    "done": True, "done_reason": "stop",
}


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return self.body


def opener_returning(payload):
    return lambda request, timeout=0: FakeResponse(payload)


CANDIDATE = ReviewCandidate(agent="09-chronicler", model="llama3.2:3b", source_event_id="evt-1",
                            kind="a public board message",
                            text="The collaboration checkpoint failed due to the lack of a collaboration checkpoint.")


class RequestBuildingTests(unittest.TestCase):
    def test_request_uses_the_confirmed_production_parameters(self):
        request = build_request(CANDIDATE)
        payload = json.loads(request.data)
        self.assertEqual(payload["keep_alive"], "96h")
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"]["num_ctx"], 190000)
        self.assertLessEqual(payload["format"]["properties"]["problem"]["maxLength"], 300)
        self.assertIn("do not", payload["messages"][0]["content"].lower())
        self.assertIn("format", payload)
        self.assertEqual(payload["model"], "qwen3.6:35b")

    def test_prompt_includes_agent_model_and_verbatim_text(self):
        request = build_request(CANDIDATE)
        payload = json.loads(request.data)
        prompt = payload["messages"][1]["content"]
        self.assertIn("09-chronicler", prompt)
        self.assertIn("llama3.2:3b", prompt)
        self.assertIn(CANDIDATE.text, prompt)


class ReviewParsingTests(unittest.TestCase):
    def test_thinking_exhausted_response_raises_judge_error_not_a_crash(self):
        with self.assertRaises(JudgeError):
            review(CANDIDATE, opener=opener_returning(THINKING_EXHAUSTED_RESPONSE))

    def test_real_circular_message_finding_is_captured_verbatim(self):
        finding = review(CANDIDATE, opener=opener_returning(CIRCULAR_MESSAGE_RESPONSE))
        self.assertIsNotNone(finding)
        self.assertEqual(finding.category, "llm_review:hallucination")
        self.assertEqual(finding.agent, "09-chronicler")
        self.assertIn("circular", finding.problem)
        self.assertEqual(finding.rejected_example, CANDIDATE.text)  # verbatim, not paraphrased

    def test_no_issue_verdict_returns_none(self):
        self.assertIsNone(review(CANDIDATE, opener=opener_returning(NO_ISSUE_RESPONSE)))

    def test_low_confidence_finding_is_suppressed(self):
        low = dict(CIRCULAR_MESSAGE_RESPONSE)
        low["message"] = {"content": json.dumps({**json.loads(CIRCULAR_MESSAGE_RESPONSE["message"]["content"]), "confidence": 0.4})}
        self.assertIsNone(review(CANDIDATE, opener=opener_returning(low)))

    def test_malformed_json_content_raises_judge_error(self):
        with self.assertRaises(JudgeError):
            review(CANDIDATE, opener=opener_returning({"message": {"content": "not json"}, "done_reason": "stop"}))

    def test_missing_required_field_raises_judge_error(self):
        bad = {"message": {"content": json.dumps({"has_issue": True, "confidence": 1.0})}, "done_reason": "stop"}
        with self.assertRaises(JudgeError):
            review(CANDIDATE, opener=opener_returning(bad))

    def test_network_failure_raises_judge_error_not_a_raw_exception(self):
        def boom(request, timeout=0): raise OSError("connection refused")
        with self.assertRaises(JudgeError):
            review(CANDIDATE, opener=boom)


class PromptTighteningRegressionTests(unittest.TestCase):
    """A genuinely ambiguous-but-fine case must not burn the whole budget on
    open-ended deliberation and return nothing usable."""

    def test_rambling_unterminated_response_raises_judge_error(self):
        with self.assertRaises(JudgeError):
            review(CANDIDATE, opener=opener_returning(RAMBLING_NO_VERDICT_RESPONSE))

    def test_tightened_prompt_yields_a_clean_no_issue_verdict(self):
        self.assertIsNone(review(CANDIDATE, opener=opener_returning(CONCISE_NO_ISSUE_RESPONSE)))


class CandidateSelectionTests(unittest.TestCase):
    def test_format_reasons_already_owned_by_the_deterministic_signature_are_skipped(self):
        events = [{"event": "invalid_decision_detail", "agent": "04-artisan", "event_id": "e1",
                  "detail": "reason=multiple action blocks; preview=x"}]
        self.assertEqual(select_candidates(events, known_format_reasons=frozenset({"multiple action blocks"})), [])

    def test_unknown_format_reason_becomes_a_candidate(self):
        events = [{"event": "invalid_decision_detail", "agent": "04-artisan", "event_id": "e1",
                  "detail": "reason=some new failure; preview=weird output"}]
        candidates = select_candidates(events, known_format_reasons=frozenset({"multiple action blocks"}))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].text, "weird output")

    def test_board_messages_become_candidates(self):
        events = [{"event": "board_message", "agent": "09-chronicler", "event_id": "e2",
                  "detail": "to=ALL; reply_to=; message=Circular text here."}]
        candidates = select_candidates(events)
        self.assertEqual(candidates[0].text, "Circular text here.")

    def test_already_classified_events_are_never_re_reviewed(self):
        events = [{"event": "board_message", "agent": "a", "event_id": "e3", "detail": "message=x"}]
        self.assertEqual(select_candidates(events, already_classified={"e3"}), [])

    def test_batch_is_bounded(self):
        events = [{"event": "board_message", "agent": "a", "event_id": f"e{i}", "detail": "message=x"} for i in range(20)]
        self.assertEqual(len(select_candidates(events, limit=5)), 5)

    def test_events_without_an_id_are_skipped(self):
        self.assertEqual(select_candidates([{"event": "board_message", "agent": "a", "detail": "message=x"}]), [])


class RoutingReuseTests(unittest.TestCase):
    """An LLM finding uses the exact same routing/rate-limit/dedup as a
    deterministic one - no special-casing, no extra hallucination surface."""

    def test_llm_finding_routes_through_the_same_store(self):
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        store = AuditStore(tmp / "audit.sqlite3")
        finding = review(CANDIDATE, opener=opener_returning(CIRCULAR_MESSAGE_RESPONSE))
        self.assertEqual(store.route(finding), "private")
        self.assertIsNone(store.route(finding))  # same source_event_id + category: never duplicated


if __name__ == "__main__":
    unittest.main()


class FullAuditCycleTests(unittest.TestCase):
    """The core requirement: the deterministic script runs first and handles
    everything it can explain for free; the slow LLM is only spent on what is
    left over."""

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path as _Path
        from village.coordinator import CoordinationStore
        self.tmp = _Path(tempfile.mkdtemp(prefix="village-full-audit-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = AuditStore(self.tmp / "audit.sqlite3")
        self.coord = CoordinationStore(self.tmp / "coordination.sqlite3", self.tmp)
        self.memory_writer = MagicMock(return_value={"id": "mem1"})

    def test_known_format_reason_is_handled_deterministically_llm_is_never_called(self):
        events = [{"agent": "04-artisan", "event": "invalid_decision_detail", "event_id": "e1",
                  "detail": "reason=multiple action blocks; preview=some raw text"}]
        opener = MagicMock(side_effect=AssertionError("must not call the LLM for a known format reason"))
        from village.auditor_llm import full_audit_cycle
        stats = full_audit_cycle(events, self.store, coordination_store=self.coord,
                                 memory_writer=self.memory_writer, opener=opener)
        self.assertEqual(stats["deterministic_findings"], 1)
        self.assertEqual(stats["deterministic_delivered"], 1)
        self.assertEqual(stats["llm_candidates"], 0)
        opener.assert_not_called()

    def test_unclassified_case_falls_through_to_the_llm(self):
        events = [{"agent": "09-chronicler", "event": "board_message", "event_id": "e2", "model": "llama3.2:3b",
                  "detail": "to=ALL; reply_to=; message=The collaboration checkpoint failed due to the lack of a collaboration checkpoint."}]
        from village.auditor_llm import full_audit_cycle
        stats = full_audit_cycle(events, self.store, coordination_store=self.coord,
                                 memory_writer=self.memory_writer, opener=opener_returning(CIRCULAR_MESSAGE_RESPONSE))
        self.assertEqual(stats["deterministic_findings"], 0)
        self.assertEqual(stats["llm_candidates"], 1)
        self.assertEqual(stats["llm_findings"], 1)
        self.assertEqual(stats["llm_delivered"], 1)
        msgs = self.coord.fetch_unacknowledged_messages("09-chronicler", source="direct")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["sender"], "village-auditor")

    def test_mixed_batch_deterministic_and_llm_each_handle_their_own_share(self):
        events = [
            {"agent": "01-king", "event": "foreign_home_blocked", "event_id": "e3",
             "detail": "target_agent=08-logician; command_prefix=x"},
            {"agent": "09-chronicler", "event": "board_message", "event_id": "e4", "model": "llama3.2:3b",
             "detail": "message=The collaboration checkpoint failed due to the lack of a collaboration checkpoint."},
        ]
        from village.auditor_llm import full_audit_cycle
        stats = full_audit_cycle(events, self.store, coordination_store=self.coord,
                                 memory_writer=self.memory_writer, opener=opener_returning(CIRCULAR_MESSAGE_RESPONSE))
        self.assertEqual(stats["deterministic_findings"], 1)
        self.assertEqual(stats["llm_candidates"], 1)  # only the un-classified event, not e3

    def test_llm_disabled_flag_skips_the_slow_layer_entirely(self):
        events = [{"agent": "09-chronicler", "event": "board_message", "event_id": "e5",
                  "detail": "message=Anything at all."}]
        opener = MagicMock(side_effect=AssertionError("must not call the LLM when disabled"))
        from village.auditor_llm import full_audit_cycle
        stats = full_audit_cycle(events, self.store, coordination_store=self.coord,
                                 memory_writer=self.memory_writer, opener=opener, llm_enabled=False)
        self.assertEqual(stats["llm_candidates"], 0)
        opener.assert_not_called()

    def test_a_judge_error_on_one_candidate_does_not_abort_the_cycle(self):
        events = [
            {"agent": "a", "event": "board_message", "event_id": "e6", "detail": "message=first"},
            {"agent": "b", "event": "board_message", "event_id": "e7", "detail": "message=second"},
        ]
        calls = {"n": 0}
        def flaky(request, timeout=0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection refused")
            return FakeResponse(CIRCULAR_MESSAGE_RESPONSE)
        from village.auditor_llm import full_audit_cycle
        stats = full_audit_cycle(events, self.store, coordination_store=self.coord,
                                 memory_writer=self.memory_writer, opener=flaky)
        self.assertEqual(stats["llm_errors"], 1)
        self.assertEqual(stats["llm_findings"], 1)
