"""P76: AI Village self-improvement fine-tuning governance - store-level tests."""
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from village.finetune import FinetuneStore, GPU_COUNT


class GpuClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-finetune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FinetuneStore(self.tmp / "coordination.sqlite3")

    def test_four_gpus_start_free(self):
        status = self.store.gpu_status()
        self.assertEqual(len(status), GPU_COUNT)
        self.assertTrue(all(g["claimed_by"] is None for g in status))

    def test_claim_returns_a_free_index_and_marks_it_claimed(self):
        index = self.store.claim_gpu("01-a", "run1")
        self.assertIn(index, range(GPU_COUNT))
        status = {g["gpu_index"]: g for g in self.store.gpu_status()}
        self.assertEqual(status[index]["claimed_by"], "01-a")
        self.assertEqual(status[index]["run_id"], "run1")

    def test_claim_prefers_the_requested_index_when_free(self):
        index = self.store.claim_gpu("01-a", "run1", preferred_index=2)
        self.assertEqual(index, 2)

    def test_claim_falls_back_when_preferred_index_is_taken(self):
        self.store.claim_gpu("01-a", "run1", preferred_index=0)
        index = self.store.claim_gpu("02-b", "run2", preferred_index=0)
        self.assertNotEqual(index, 0)

    def test_all_four_claimed_then_none_free(self):
        for i, agent in enumerate(["01-a", "02-b", "03-c", "04-d"]):
            self.store.claim_gpu(agent, f"run{i}")
        with self.assertRaises(ValueError):
            self.store.claim_gpu("05-e", "run5")

    def test_release_frees_the_gpu(self):
        index = self.store.claim_gpu("01-a", "run1")
        self.store.release_gpu(index, "01-a")
        status = {g["gpu_index"]: g for g in self.store.gpu_status()}
        self.assertIsNone(status[index]["claimed_by"])

    def test_release_by_a_non_claimant_is_rejected(self):
        index = self.store.claim_gpu("01-a", "run1")
        with self.assertRaises(ValueError):
            self.store.release_gpu(index, "02-b")

    def test_release_unknown_index_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.release_gpu(99, "01-a")


class GpuPriorityReservationTests(unittest.TestCase):
    """P90 (Gazette game prize: "GPU-/Finetuning-Vorrang")."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-finetune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FinetuneStore(self.tmp / "coordination.sqlite3")

    def test_reserves_one_idle_gpu_for_the_winner(self):
        index = self.store.reserve_gpu_priority("02-explorer")
        self.assertIn(index, range(GPU_COUNT))
        status = {g["gpu_index"]: g for g in self.store.gpu_status()}
        self.assertEqual(status[index]["reserved_for"], "02-explorer")
        self.assertIsNone(status[index]["claimed_by"])  # never touches an actual claim

    def test_never_reserves_an_already_claimed_gpu(self):
        for i, agent in enumerate(["01-a", "02-b", "03-c", "04-d"]):
            self.store.claim_gpu(agent, f"run{i}")
        # all 4 are now claimed - nothing idle left to reserve.
        self.assertIsNone(self.store.reserve_gpu_priority("02-explorer"))

    def test_a_different_agent_cannot_claim_the_reserved_gpu_while_others_are_free(self):
        reserved = self.store.reserve_gpu_priority("02-explorer")
        # claim 3 of the remaining free GPUs so only the reserved one is left.
        others = [i for i in range(GPU_COUNT) if i != reserved]
        for i, agent in zip(others, ["01-a", "03-c", "04-d"]):
            self.store.claim_gpu(agent, f"run-{agent}", preferred_index=i)
        with self.assertRaises(ValueError):
            self.store.claim_gpu("99-bystander", "run-bystander")

    def test_the_winner_itself_may_claim_the_reserved_gpu(self):
        reserved = self.store.reserve_gpu_priority("02-explorer")
        index = self.store.claim_gpu("02-explorer", "run-explorer")
        self.assertEqual(index, reserved)
        status = {g["gpu_index"]: g for g in self.store.gpu_status()}
        self.assertEqual(status[reserved]["claimed_by"], "02-explorer")
        self.assertIsNone(status[reserved]["reserved_for"])  # claimed clears the reservation

    def test_a_different_agent_may_still_claim_a_different_free_gpu(self):
        self.store.reserve_gpu_priority("02-explorer")
        index = self.store.claim_gpu("99-bystander", "run-bystander")
        self.assertIn(index, range(GPU_COUNT))

    def test_claiming_the_reserved_gpu_by_anyone_clears_the_reservation(self):
        # Not forced open for a bystander (see the "cannot jump" test
        # above) - but once that GPU is claimed by ANYONE after its
        # reservation legitimately expires, the reservation is gone.
        reserved = self.store.reserve_gpu_priority("02-explorer")
        with sqlite3.connect(self.store.db_path) as c:
            expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            c.execute("UPDATE finetune_gpu_reservations SET expires_at=? WHERE gpu_index=?", (expired, reserved))
            c.commit()
        index = self.store.claim_gpu("99-bystander", "run-bystander", preferred_index=reserved)
        self.assertEqual(index, reserved)

    def test_an_expired_reservation_never_blocks_a_claim(self):
        reserved = self.store.reserve_gpu_priority("02-explorer")
        with sqlite3.connect(self.store.db_path) as c:
            expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            c.execute("UPDATE finetune_gpu_reservations SET expires_at=? WHERE gpu_index=?", (expired, reserved))
            c.commit()
        others = [i for i in range(GPU_COUNT) if i != reserved]
        for i, agent in zip(others, ["01-a", "03-c", "04-d"]):
            self.store.claim_gpu(agent, f"run-{agent}", preferred_index=i)
        # with the reservation expired, the bystander may now take it.
        index = self.store.claim_gpu("99-bystander", "run-bystander")
        self.assertEqual(index, reserved)


class ProposeRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-finetune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FinetuneStore(self.tmp / "coordination.sqlite3")

    def test_propose_creates_a_run_and_claims_a_gpu(self):
        run = self.store.propose_run("01-a", "ornith:9b", "LoRA", "own event log, last 30 days")
        self.assertEqual(run["status"], "proposed")
        self.assertIn(run["gpu_index"], range(GPU_COUNT))
        self.assertEqual(run["evaluations"], [])

    def test_propose_requires_all_three_fields(self):
        with self.assertRaises(ValueError):
            self.store.propose_run("01-a", "", "LoRA", "data")
        with self.assertRaises(ValueError):
            self.store.propose_run("01-a", "ornith:9b", "", "data")
        with self.assertRaises(ValueError):
            self.store.propose_run("01-a", "ornith:9b", "LoRA", "")

    def test_propose_with_no_free_gpu_raises_and_creates_no_orphan_run(self):
        for i, agent in enumerate(["02-b", "03-c", "04-d", "05-e"]):
            self.store.claim_gpu(agent, f"other{i}")
        with self.assertRaises(ValueError):
            self.store.propose_run("01-a", "ornith:9b", "LoRA", "data")
        self.assertEqual(self.store.list_for_agent("01-a"), [])


class UpdateStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-finetune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FinetuneStore(self.tmp / "coordination.sqlite3")
        self.run = self.store.propose_run("01-a", "ornith:9b", "LoRA", "data")

    def test_unknown_run_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.update_status("nope", "01-a", "running")

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.update_status(self.run["id"], "01-a", "paused")

    def test_only_the_owning_agent_may_update(self):
        with self.assertRaises(ValueError):
            self.store.update_status(self.run["id"], "02-b", "running")

    def test_running_keeps_the_gpu_claimed(self):
        self.store.update_status(self.run["id"], "01-a", "running", job_reference="job_123")
        status = {g["gpu_index"]: g for g in self.store.gpu_status()}
        self.assertEqual(status[self.run["gpu_index"]]["claimed_by"], "01-a")

    def test_completed_releases_the_gpu(self):
        result = self.store.update_status(self.run["id"], "01-a", "completed", output_path="/home/01-a/ft-out")
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["completed_at"])
        status = {g["gpu_index"]: g for g in self.store.gpu_status()}
        self.assertIsNone(status[self.run["gpu_index"]]["claimed_by"])

    def test_failed_also_releases_the_gpu(self):
        self.store.update_status(self.run["id"], "01-a", "failed", notes="OOM")
        status = {g["gpu_index"]: g for g in self.store.gpu_status()}
        self.assertIsNone(status[self.run["gpu_index"]]["claimed_by"])

    def test_notes_accumulate_rather_than_overwrite(self):
        self.store.update_status(self.run["id"], "01-a", "running", notes="started")
        result = self.store.update_status(self.run["id"], "01-a", "completed", notes="finished")
        self.assertIn("started", result["notes"])
        self.assertIn("finished", result["notes"])


class EvaluationAndSwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-finetune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FinetuneStore(self.tmp / "coordination.sqlite3")
        self.run = self.store.propose_run("01-a", "ornith:9b", "LoRA", "data")

    def test_record_evaluation_against_unknown_run_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.record_evaluation("nope", "01-a", "eval_pass_rate", 0.8)

    def test_record_evaluation_requires_a_metric_name(self):
        with self.assertRaises(ValueError):
            self.store.record_evaluation(self.run["id"], "01-a", "", 0.8)

    def test_record_evaluation_is_attached_to_the_run(self):
        result = self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82, baseline_value=0.75,
                                              notes="held-out 20 tasks")
        self.assertEqual(len(result["evaluations"]), 1)
        entry = result["evaluations"][0]
        self.assertEqual(entry["metric_value"], 0.82)
        self.assertEqual(entry["baseline_value"], 0.75)

    def test_request_swap_requires_completed_status(self):
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82)
        with self.assertRaises(ValueError):
            self.store.request_swap(self.run["id"], "01-a")

    def test_request_swap_requires_at_least_one_evaluation(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        with self.assertRaises(ValueError):
            self.store.request_swap(self.run["id"], "01-a")

    def test_request_swap_requires_the_owning_agent(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82)
        with self.assertRaises(ValueError):
            self.store.request_swap(self.run["id"], "02-b")

    def test_request_swap_end_to_end(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82, baseline_value=0.75)
        request = self.store.request_swap(self.run["id"], "01-a")
        self.assertEqual(request["status"], "pending")
        self.assertEqual(request["agent_id"], "01-a")

    def test_review_swap_unknown_decision_is_rejected(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82)
        request = self.store.request_swap(self.run["id"], "01-a")
        with self.assertRaises(ValueError):
            self.store.review_swap(request["id"], "01-king", "maybe")

    def test_review_swap_approve_and_reject(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82)
        approved_request = self.store.request_swap(self.run["id"], "01-a")
        approved = self.store.review_swap(approved_request["id"], "01-king", "approve", "clear win")
        self.assertEqual(approved["status"], "king_approved")
        self.assertEqual(approved["reviewed_by"], "01-king")
        self.assertEqual(approved["review_note"], "clear win")

    def test_review_swap_cannot_be_decided_twice(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82)
        request = self.store.request_swap(self.run["id"], "01-a")
        self.store.review_swap(request["id"], "01-king", "approve")
        with self.assertRaises(ValueError):
            self.store.review_swap(request["id"], "01-king", "reject")

    def test_pending_and_approved_queues_are_separate(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.82)
        request = self.store.request_swap(self.run["id"], "01-a")
        self.assertEqual(len(self.store.pending_swap_requests()), 1)
        self.assertEqual(len(self.store.king_approved_swaps_awaiting_operator()), 0)
        self.store.review_swap(request["id"], "01-king", "approve")
        self.assertEqual(len(self.store.pending_swap_requests()), 0)
        self.assertEqual(len(self.store.king_approved_swaps_awaiting_operator()), 1)

    def test_rejected_swap_never_reaches_the_operator_queue(self):
        self.store.update_status(self.run["id"], "01-a", "completed")
        self.store.record_evaluation(self.run["id"], "01-a", "eval_pass_rate", 0.4, baseline_value=0.75)
        request = self.store.request_swap(self.run["id"], "01-a")
        self.store.review_swap(request["id"], "01-king", "reject", "regression vs. baseline")
        self.assertEqual(self.store.king_approved_swaps_awaiting_operator(), [])


class QueryHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-finetune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = FinetuneStore(self.tmp / "coordination.sqlite3")

    def test_has_ever_engaged_false_until_a_run_exists(self):
        self.assertFalse(self.store.has_ever_engaged("01-a"))
        self.store.propose_run("01-a", "ornith:9b", "LoRA", "data")
        self.assertTrue(self.store.has_ever_engaged("01-a"))

    def test_list_for_agent_is_scoped_and_newest_first(self):
        self.store.propose_run("01-a", "ornith:9b", "LoRA", "data one")
        self.store.propose_run("02-b", "qwen3.5:4b", "QLoRA", "data two")
        run2 = self.store.propose_run("01-a", "ornith:9b", "LoRA", "data three")
        runs = self.store.list_for_agent("01-a")
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["id"], run2["id"])  # newest first


if __name__ == "__main__":
    unittest.main()
