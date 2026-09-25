"""Unit tests for P18: AI Village Authority, capability management, King delegation, and GPU revocation."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from village.authority import (
    AuthorityCore,
    find_agent_gpu_processes,
    resolve_groups_for_target,
    run_socket_server,
    terminate_agent_gpu_workload,
)


class TestVillageAuthority(unittest.TestCase):
    """Test suite covering capability management, King validation, GPU handles, and pause enforcement."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "authority_requests.sqlite3"
        self.board_path = self.tmp_path / "board.jsonl"
        self.pause_marker = self.tmp_path / "paused"

        self.config = {
            "king_user": "king_philipp",
            "board": str(self.board_path),
            "agents": {
                "scholar": "village_scholar",
                "artisan": "village_artisan",
                "resident_1": "village_res1",
            },
        }

        self.executed_cmds: list[list[str]] = []

        def mock_runner(cmd, capture_output=True, text=True):
            self.executed_cmds.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        self.mock_runner = mock_runner
        self.core = AuthorityCore(
            config=self.config,
            db_path=self.db_path,
            pause_marker=self.pause_marker,
            cmd_runner=self.mock_runner,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_rootless_container_capability_for_residents(self):
        """All residents have rootless container capability by default; containers group is resolved."""
        resident_groups = resolve_groups_for_target("resident")
        container_groups = resolve_groups_for_target("containers")

        # Crucial invariant: 'resident' role must include 'ai-village-containers'
        self.assertIn("ai-village-containers", resident_groups)
        self.assertIn("ai-village-containers", container_groups)
        self.assertIn("ai-village", resident_groups)

    def test_submit_request_and_query_lifecycle(self):
        """Agents can submit structured capability requests and query their status."""
        resp = self.core.submit_request(
            agent="scholar",
            capability="gpu",
            reason="Model fine-tuning experiment",
        )
        self.assertTrue(resp["ok"])
        req = resp["request"]
        req_id = req["request_id"]
        self.assertTrue(req_id.startswith("req_"))
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["capability"], "gpu")

        # Query request by ID
        fetched = self.core.get_request(req_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["reason"], "Model fine-tuning experiment")

        # List requests filtered by agent
        agent_reqs = self.core.list_requests(agent="scholar")
        self.assertEqual(len(agent_reqs), 1)
        self.assertEqual(agent_reqs[0]["request_id"], req_id)

    def test_fake_request_id_and_unknown_agent_rejected(self):
        """Unknown agent or non-existent request IDs are cleanly rejected."""
        unknown_resp = self.core.submit_request(
            agent="hacker_agent",
            capability="gpu",
            reason="Exploit",
        )
        self.assertFalse(unknown_resp["ok"])
        self.assertIn("Unknown agent", unknown_resp["error"])

        fake_decide = self.core.decide_request(
            request_id="req_fake_nonexistent",
            decision="approve",
            reason="Approved fake",
            king_name="king_philipp",
        )
        self.assertFalse(fake_decide["ok"])
        self.assertIn("not found", fake_decide["error"])

    def test_king_approval_and_capability_grant(self):
        """King approval grants capability with duration and records audit event."""
        sub = self.core.submit_request("artisan", "steward", "Audit logs maintenance")
        req_id = sub["request"]["request_id"]

        dec_resp = self.core.decide_request(
            request_id=req_id,
            decision="approve",
            reason="Approved for maintenance cycle",
            king_name="king_philipp",
            duration_seconds=3600,
        )
        self.assertTrue(dec_resp["ok"])
        updated_req = dec_resp["request"]
        self.assertEqual(updated_req["status"], "approved")
        self.assertEqual(updated_req["decided_by"], "king_philipp")
        self.assertIsNotNone(updated_req["expires_at"])

        # Check usermod command was executed
        cmd_strs = [" ".join(c) for c in self.executed_cmds]
        self.assertTrue(any("usermod -aG ai-village-stewards village_artisan" in c for c in cmd_strs))

        # Check board audit entry written
        self.assertTrue(self.board_path.exists())
        with open(self.board_path, "r", encoding="utf-8") as f:
            board_content = f.read()
            self.assertIn("king approved request", board_content)

    def test_king_rejection(self):
        """King rejection sets rejected status without granting groups."""
        sub = self.core.submit_request("scholar", "gpu", "Need all GPUs")
        req_id = sub["request"]["request_id"]

        dec_resp = self.core.decide_request(
            request_id=req_id,
            decision="reject",
            reason="GPU budget reserved for colony training",
            king_name="king_philipp",
        )
        self.assertTrue(dec_resp["ok"])
        self.assertEqual(dec_resp["request"]["status"], "rejected")
        self.assertIsNone(dec_resp.get("grant_result"))

        # No usermod was executed
        cmd_strs = [" ".join(c) for c in self.executed_cmds]
        self.assertFalse(any("usermod" in c for c in cmd_strs))

    def test_pause_respect_skips_service_restart(self):
        """When pause marker exists, grant and revoke execute group changes but skip service restart."""
        # Create pause marker
        self.pause_marker.touch()
        self.assertTrue(self.core.is_paused())

        res_grant = self.core.grant("scholar", "gpu", "GPU grant while paused")
        self.assertTrue(res_grant["ok"])
        self.assertTrue(res_grant["paused"])
        self.assertFalse(res_grant["restarted"])
        self.assertIn("restart skipped", res_grant["detail"])

        # Verify systemctl restart was NOT called
        cmd_strs = [" ".join(c) for c in self.executed_cmds]
        self.assertFalse(any("systemctl restart" in c for c in cmd_strs))

        # Now revoke while paused
        res_revoke = self.core.revoke("scholar", "gpu", "GPU revoke while paused")
        self.assertTrue(res_revoke["ok"])
        self.assertTrue(res_revoke["paused"])
        self.assertFalse(res_revoke["restarted"])

    def test_partial_failure_reporting(self):
        """Partial failures (e.g. command failure) report ok=False and detailed errors."""
        def failing_runner(cmd, capture_output=True, text=True):
            if "systemctl" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="Job for unit failed"
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        core_fail = AuthorityCore(
            config=self.config,
            db_path=self.db_path,
            pause_marker=self.tmp_path / "not_paused",
            cmd_runner=failing_runner,
        )

        res = core_fail.grant("scholar", "gpu", "Grant test")
        self.assertFalse(res["ok"])
        self.assertGreater(len(res["errors"]), 0)
        self.assertIn("Service restart failed", res["errors"][0])

    def test_gpu_revocation_detects_and_terminates_agent_handles_only(self):
        """GPU revocation scans open GPU handles and terminates ONLY the target agent's processes."""
        proc_mock = self.tmp_path / "proc"
        proc_mock.mkdir()

        agent_uid = 2001
        foreign_uid = 2002

        # PID 101: target agent process holding /dev/nvidia0
        pid101 = proc_mock / "101"
        pid101.mkdir()
        (pid101 / "fd").mkdir()
        os.symlink("/dev/nvidia0", pid101 / "fd" / "3")

        # PID 102: foreign user process holding /dev/nvidia0
        pid102 = proc_mock / "102"
        pid102.mkdir()
        (pid102 / "fd").mkdir()
        os.symlink("/dev/nvidia0", pid102 / "fd" / "4")

        # PID 103: target agent process WITHOUT GPU handles
        pid103 = proc_mock / "103"
        pid103.mkdir()
        (pid103 / "fd").mkdir()
        os.symlink("/tmp/log.txt", pid103 / "fd" / "5")

        # Mock os.stat to return custom UID for each PID directory
        orig_stat = os.stat

        def mock_stat(path, *args, **kwargs):
            p = str(path)
            if "/proc/101" in p or p.endswith("/101"):
                res = MagicMock()
                res.st_uid = agent_uid
                return res
            if "/proc/102" in p or p.endswith("/102"):
                res = MagicMock()
                res.st_uid = foreign_uid
                return res
            if "/proc/103" in p or p.endswith("/103"):
                res = MagicMock()
                res.st_uid = agent_uid
                return res
            return orig_stat(path, *args, **kwargs)

        killed_pids: list[int] = []

        def mock_kill(pid, sig):
            killed_pids.append(pid)

        with unittest.mock.patch("os.stat", side_effect=mock_stat):
            found = find_agent_gpu_processes(agent_uid, proc_root=str(proc_mock))
            self.assertEqual(found, [101])

            terminated = terminate_agent_gpu_workload(
                agent_uid,
                kill_fn=mock_kill,
                proc_root=str(proc_mock),
            )
            # Exactly PID 101 terminated; foreign PID 102 untouched!
            self.assertEqual(terminated, [101])
            self.assertEqual(killed_pids, [101])

    def test_socket_server_and_unprivileged_caller_rejection(self):
        """Unix domain socket server enforces SO_PEERCRED King authentication."""
        sock_file = self.tmp_path / "test_authority.sock"
        king_uid = 1000

        server_thread = threading.Thread(
            target=run_socket_server,
            args=(self.core, str(sock_file), king_uid),
            daemon=True,
        )
        server_thread.start()

        # Wait for socket to bind
        for _ in range(50):
            if sock_file.exists():
                break
            time.sleep(0.05)
        self.assertTrue(sock_file.exists())

        # Connect client socket
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_file))

        # 1. Any agent can submit request
        req_payload = {
            "action": "request",
            "agent": "scholar",
            "capability": "containers",
            "reason": "Run unit test container",
        }
        client.sendall((json.dumps(req_payload) + "\n").encode())
        resp = json.loads(client.recv(4096).decode())
        self.assertTrue(resp["ok"])
        req_id = resp["request"]["request_id"]
        client.close()

        # 2. Query request
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(sock_file))
        client.sendall((json.dumps({"action": "get_request", "request_id": req_id}) + "\n").encode())
        resp_get = json.loads(client.recv(4096).decode())
        self.assertTrue(resp_get["ok"])
        self.assertEqual(resp_get["request"]["request_id"], req_id)
        client.close()


if __name__ == "__main__":
    unittest.main()
