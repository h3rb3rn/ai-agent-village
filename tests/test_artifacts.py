"""Unit tests for P13: Reproducible artifacts, tamper detection, and independent peer verification."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from village.artifacts import ArtifactStore, compute_file_sha256
from web.runtime import Resident


class TestArtifactVerification(unittest.TestCase):
    """Test suite covering artifact lifecycles, tamper detection, and untrusted test execution."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "artifacts.sqlite3"
        self.store = ArtifactStore(self.db_path, village_root=self.root)
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_true_without_artifact_fails(self):
        """Test command 'true' or missing artifact file must never be accepted as success."""
        # 1. Register planned artifact whose file does not yet exist
        art = self.store.register_artifact(
            artifact_id="art_missing",
            owner="agent_author",
            file_path="work/non_existent.py",
            test_description="Dummy test without artifact",
        )
        self.assertEqual(art["status"], "planned")

        # 2. Attempting to claim success for a non-existent file must fail
        with self.assertRaises(FileNotFoundError):
            self.store.claim_success("art_missing", claimant="agent_author")

        # 3. Peer verification must fail even if command is 'true'
        passed, record = self.store.verify_artifact(
            artifact_id="art_missing",
            verifier="agent_peer",
            test_command="true",
        )
        self.assertFalse(passed)
        self.assertNotEqual(record["status"], "reproduced")
        self.assertEqual(len(record["verifications"]), 1)
        self.assertEqual(record["verifications"][0]["passed"], 0)

    def test_error_masked_by_or_echo_fails(self):
        """Tests attempting to hide non-zero exit codes with '|| echo' must be detected and rejected."""
        target_file = self.work_dir / "broken_script.sh"
        target_file.write_text("#!/bin/bash\nexit 1\n")
        target_file.chmod(0o755)

        self.store.register_artifact(
            artifact_id="art_masked",
            owner="agent_author",
            file_path=str(target_file),
            test_description="Failing script with masked error",
        )

        # Command masks failure with || echo 'all good'
        masking_command = f"bash {target_file} || echo 'all good error suppressed'"
        with self.assertRaises(ValueError):
            self.store.claim_success("art_masked", claimant="agent_author", test_command=masking_command)

        # Peer verification also rejects error masking
        passed, record = self.store.verify_artifact(
            artifact_id="art_masked",
            verifier="agent_peer",
            test_command=masking_command,
        )
        self.assertFalse(passed)
        self.assertNotEqual(record["status"], "reproduced")

    def test_invented_test_result_rejected(self):
        """Failing commands or missing outputs must record failure, never advance status."""
        target_file = self.work_dir / "calc.py"
        target_file.write_text("print('incorrect output')\n")

        self.store.register_artifact(
            artifact_id="art_calc_bad",
            owner="agent_author",
            file_path=str(target_file),
        )

        # Test asserts specific string that is NOT present
        test_cmd = f"python3 {target_file} | grep -q 'expected_number_42'"
        passed, record = self.store.verify_artifact(
            artifact_id="art_calc_bad",
            verifier="agent_peer",
            test_command=test_cmd,
        )
        self.assertFalse(passed)
        self.assertNotEqual(record["status"], "reproduced")
        self.assertEqual(record["verifications"][0]["passed"], 0)

    def test_modified_file_after_verification_detected(self):
        """Modifying artifact content on disk after verification is flagged as tampering."""
        target_file = self.work_dir / "valid_solution.py"
        target_file.write_text("def solve(): return 42\nprint(solve())\n")

        self.store.register_artifact(
            artifact_id="art_valid_tamper",
            owner="agent_author",
            file_path=str(target_file),
        )
        self.store.claim_success("art_valid_tamper", claimant="agent_author")

        # Verify once successfully
        passed, art = self.store.verify_artifact(
            artifact_id="art_valid_tamper",
            verifier="agent_peer",
            test_command=f"python3 {target_file} | grep -q 42",
        )
        self.assertTrue(passed)
        self.assertEqual(art["status"], "reproduced")

        # Now tamper with the file on disk
        target_file.write_text("def solve(): return 999  # MODIFIED\nprint(solve())\n")

        is_tampered, msg = self.store.detect_tampering("art_valid_tamper")
        self.assertTrue(is_tampered)
        self.assertIn("Hash mismatch", msg)

        # Subsequent verification must detect tampering and fail immediately
        passed_again, art_again = self.store.verify_artifact(
            artifact_id="art_valid_tamper",
            verifier="agent_peer_2",
            test_command=f"python3 {target_file} | grep -q 999",
        )
        self.assertFalse(passed_again)

    def test_foreign_owner_cannot_claim_success(self):
        """Only the registered author can claim success on an artifact."""
        target_file = self.work_dir / "module.py"
        target_file.write_text("# module\n")

        self.store.register_artifact(
            artifact_id="art_ownership",
            owner="agent_author",
            file_path=str(target_file),
        )

        with self.assertRaises(ValueError) as ctx:
            self.store.claim_success("art_ownership", claimant="impostor_agent")
        self.assertIn("Only the owner", str(ctx.exception))

    def test_self_verification_rejected(self):
        """Author cannot peer-verify their own artifact."""
        target_file = self.work_dir / "tool.py"
        target_file.write_text("print('tool output')\n")

        self.store.register_artifact(
            artifact_id="art_self_verify",
            owner="agent_author",
            file_path=str(target_file),
        )

        with self.assertRaises(ValueError) as ctx:
            self.store.verify_artifact(
                artifact_id="art_self_verify",
                verifier="agent_author",
                test_command=f"python3 {target_file}",
            )
        self.assertIn("cannot peer-verify their own artifact", str(ctx.exception))

    def test_valid_reproducible_miniexample_and_adoption(self):
        """Complete lifecycle: planned -> created -> claimed_success -> reproduced -> adopted."""
        target_file = self.work_dir / "fibonacci.py"
        target_file.write_text("def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)\nprint(fib(7))\n")

        # 1. Author registers created artifact
        art = self.store.register_artifact(
            artifact_id="fib_algo",
            owner="agent_alice",
            file_path=str(target_file),
            test_description="Computes 7th Fibonacci number",
        )
        self.assertEqual(art["status"], "created")

        # 2. Author claims success with self-test
        art = self.store.claim_success(
            artifact_id="fib_algo",
            claimant="agent_alice",
            test_command=f"python3 {target_file} | grep -q '^13$'",
        )
        self.assertEqual(art["status"], "claimed_success")

        # 3. Peer Agent Bob verifies independently
        passed, art = self.store.verify_artifact(
            artifact_id="fib_algo",
            verifier="agent_bob",
            test_command=f"python3 {target_file} | grep -q '^13$'",
            verdict_type="automated_reproduction",
            details="Reproduced on isolated lane; output is 13 as expected.",
        )
        self.assertTrue(passed)
        self.assertEqual(art["status"], "reproduced")
        self.assertEqual(len(art["verifications"]), 1)
        self.assertEqual(art["verifications"][0]["verifier"], "agent_bob")

        # 4. Peer Agent Charlie adopts artifact into their workflow
        art = self.store.adopt_artifact("fib_algo", adopter="agent_charlie")
        self.assertEqual(art["status"], "adopted")
        self.assertIn("agent_charlie", art["provenance"]["adopters"])

    def test_qualitative_evaluations_distinguished_from_facts(self):
        """Ensure heuristic and human judgments are explicitly labeled, not recorded as automated facts."""
        target_file = self.work_dir / "analysis.md"
        target_file.write_text("# Research Analysis\nHypothesis holds under standard conditions.\n")

        self.store.register_artifact(
            artifact_id="art_analysis",
            owner="agent_alice",
            file_path=str(target_file),
        )

        passed, art = self.store.verify_artifact(
            artifact_id="art_analysis",
            verifier="agent_bob",
            test_command="grep -q 'Hypothesis holds' " + str(target_file),
            verdict_type="heuristic_evaluation",
            details="LLM evaluation indicates logical coherence, but this is a heuristic judgment.",
        )
        self.assertTrue(passed)
        ver = art["verifications"][0]
        self.assertEqual(ver["verdict_type"], "heuristic_evaluation")
        self.assertIn("heuristic judgment", ver["details"])

    def test_runtime_artifact_tool_integration(self):
        """Verify Resident executes artifact_operation tool calls seamlessly."""
        env = {
            "AGENT_ID": "agent_alpha",
            "AGENT_NAME": "alpha",
            "AGENT_ROLE": "researcher",
            "VILLAGE_ROOT": str(self.root),
            "AGENT_IDENTITY_PROMPT": str(self.root / "identity.txt"),
            "OLLAMA_MODEL": "qwen2.5:7b",
        }
        (self.root / "identity.txt").write_text("Test agent identity")
        (self.root / "board").mkdir(parents=True, exist_ok=True)
        resident = Resident(env=env)

        artifact_file = resident.home / "math_helper.py"
        artifact_file.write_text("print('math helper ready')\n")

        # 1. Tool call: register
        resident.execute({
            "tool_call": {
                "name": "artifact_operation",
                "arguments": {
                    "operation": "register",
                    "artifact_id": "math_v1",
                    "file_path": str(artifact_file),
                    "test_description": "Initial math helper",
                },
            }
        })
        last_res = resident.state.get("last_result", {})
        self.assertTrue(last_res.get("ok"))

        # 2. Tool call: claim_success
        resident.execute({
            "tool_call": {
                "name": "artifact_operation",
                "arguments": {
                    "operation": "claim_success",
                    "artifact_id": "math_v1",
                    "test_command": f"python3 {artifact_file} | grep -q 'math helper ready'",
                },
            }
        })
        last_res = resident.state.get("last_result", {})
        self.assertTrue(last_res.get("ok"))
        data = json.loads(last_res.get("result", "{}"))
        self.assertEqual(data.get("status"), "claimed_success")


if __name__ == "__main__":
    unittest.main()
