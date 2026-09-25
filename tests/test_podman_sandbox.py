#!/usr/bin/env python3
"""Unit tests for village.containers (Podman sandbox, storage isolation, and GPU verification)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from village.containers import (
    HostResourceAudit,
    PodmanSandbox,
    StorageManager,
    audit_host_environment,
    describe_agent_capabilities,
    verify_gpu_compute,
)


class TestPodmanSandboxAndStorage(unittest.TestCase):
    """Test suite for rootless containers, isolated storage, and GPU verification."""

    def setUp(self):
        """Set up isolated temporary directory structure for tests."""
        self.tmp_dir = tempfile.mkdtemp(prefix="test_village_containers_")
        self.tmp_path = Path(self.tmp_dir)

        # Mock /etc, /proc, /run
        self.mock_etc = self.tmp_path / "etc"
        self.mock_etc.mkdir()
        self.mock_proc = self.tmp_path / "proc"
        self.mock_proc.mkdir()
        self.mock_run = self.tmp_path / "run"
        self.mock_run.mkdir()

        # Mock storage root
        self.storage_root = self.tmp_path / "storage"
        self.storage_mgr = StorageManager(storage_root=self.storage_root)

        # Mock /etc/subuid & /etc/subgid
        with open(self.mock_etc / "subuid", "w", encoding="utf-8") as f:
            f.write("village_scholar:100000:65536\nvillage_artisan:165536:65536\n")
        with open(self.mock_etc / "subgid", "w", encoding="utf-8") as f:
            f.write("village_scholar:100000:65536\nvillage_artisan:165536:65536\n")

        # Mock /proc/meminfo
        with open(self.mock_proc / "meminfo", "w", encoding="utf-8") as f:
            f.write("MemTotal:       32808468 kB\nMemFree:         4123456 kB\nMemAvailable:   16404234 kB\n")

        # Mock /proc/mounts
        with open(self.mock_proc / "mounts", "w", encoding="utf-8") as f:
            f.write(
                "/dev/sda1 / ext4 rw,relatime 0 0\n"
                "/dev/sda2 /var ext4 rw,relatime 0 0\n"
                "tmpfs /run tmpfs rw,nosuid,nodev 0 0\n"
            )

        # Mock CDI directory
        self.mock_cdi = self.mock_etc / "cdi"
        self.mock_cdi.mkdir()
        with open(self.mock_cdi / "nvidia.yaml", "w", encoding="utf-8") as f:
            f.write("cdiVersion: 0.5.0\nkind: nvidia.com/gpu\ndevices:\n  - name: all\n")

        self.executed_cmds: list[list[str]] = []

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def mock_cmd_runner(self, cmd, capture_output=True, text=True, timeout=None):
        """Mock subprocess command runner recording executed commands."""
        self.executed_cmds.append(cmd)
        cmd_str = " ".join(cmd)

        if "podman --version" in cmd_str:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="podman version 5.2.2", stderr="")
        if "nvidia-smi -L" in cmd_str:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="GPU 0: Tesla M10 (UUID: GPU-1111)\nGPU 1: Tesla M10 (UUID: GPU-2222)\n",
                stderr="",
            )
        if "podman run" in cmd_str:
            if "COMPUTE_RESULT" in cmd_str:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="COMPUTE_RESULT=42.0\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Container output test", stderr="")
        if "podman ps -a" in cmd_str:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ai-village-scholar-123\nai-village-scholar-456\n", stderr=""
            )
        if "podman rm -f" in cmd_str:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="removed", stderr="")

        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    def test_audit_host_environment(self):
        """Audit accurately parses CPU, RAM, mounts, subuids, CDI, driver, and podman."""
        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}" if x in ("podman", "nvidia-smi", "podman-compose") else None):
            audit = audit_host_environment(
                cmd_runner=self.mock_cmd_runner,
                etc_dir=self.mock_etc,
                proc_dir=self.mock_proc,
                run_dir=self.mock_run,
            )

        self.assertGreater(audit.cpu_count, 0)
        self.assertEqual(audit.ram_total_bytes, 32808468 * 1024)
        self.assertEqual(audit.ram_available_bytes, 16404234 * 1024)
        self.assertIn("village_scholar", audit.subuid_mappings)
        self.assertEqual(audit.subuid_mappings["village_scholar"], (100000, 65536))
        self.assertIn("village_artisan", audit.subgid_mappings)
        self.assertTrue(audit.driver_status["installed"])
        self.assertTrue(audit.driver_status["responsive"])
        self.assertEqual(len(audit.driver_status["devices"]), 2)
        self.assertTrue(audit.podman_status["installed"])
        self.assertEqual(audit.compose_provider, "podman-compose")
        self.assertEqual(len(audit.cdi_spec_files), 1)

    def test_audit_detects_driver_and_subuid_defects(self):
        """Audit records clear defect diagnostics when driver or subuid configs fail."""
        def failing_runner(cmd, capture_output=True, text=True, timeout=None):
            if any("nvidia-smi" in str(arg) for arg in cmd):
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="podman version 5.0", stderr="")

        empty_etc = self.tmp_path / "empty_etc"
        empty_etc.mkdir()

        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}" if x in ("podman", "nvidia-smi") else None):
            audit = audit_host_environment(
                cmd_runner=failing_runner,
                etc_dir=empty_etc,
                proc_dir=self.mock_proc,
                run_dir=self.mock_run,
            )

        self.assertFalse(audit.driver_status["responsive"])
        self.assertIn("couldn't communicate with the NVIDIA driver", audit.driver_status["error"])
        self.assertTrue(any("Nvidia driver communication failed" in d for d in audit.detected_defects))
        self.assertTrue(any("Missing /etc/subuid" in d for d in audit.detected_defects))

    def test_storage_layout_and_data_preservation(self):
        """Storage manager creates shared and private dirs and preserves existing files."""
        # Pre-create a file in scholar's private dir to check preservation
        scholar_dir = self.storage_mgr.get_agent_storage_dir("scholar")
        scholar_dir.mkdir(parents=True)
        test_file = scholar_dir / "my_experiment.txt"
        test_file.write_text("pre-existing data", encoding="utf-8")

        res = self.storage_mgr.ensure_storage_layout(agent="scholar")
        self.assertTrue(res["shared"].exists())
        self.assertTrue(res["private"].exists())
        self.assertTrue(test_file.exists())
        self.assertEqual(test_file.read_text(encoding="utf-8"), "pre-existing data")

    def test_mount_path_validation_boundaries(self):
        """Storage manager validates allowed paths and rejects directory escapes or system mounts."""
        self.storage_mgr.ensure_storage_layout(agent="scholar")
        scholar_dir = self.storage_mgr.get_agent_storage_dir("scholar")
        shared_dir = self.storage_mgr.get_shared_storage_dir()

        # Valid paths
        p1 = self.storage_mgr.validate_mount_path(scholar_dir / "subfolder", agent="scholar")
        self.assertEqual(p1, (scholar_dir / "subfolder").resolve())
        p2 = self.storage_mgr.validate_mount_path(shared_dir / "dataset", agent="scholar")
        self.assertEqual(p2, (shared_dir / "dataset").resolve())

        # Path traversal rejection
        with self.assertRaises(ValueError) as ctx:
            self.storage_mgr.validate_mount_path(scholar_dir / "../artisan/secret", agent="scholar")
        self.assertIn("Path traversal", str(ctx.exception))

        # Rejection of foreign agent private directory
        artisan_dir = self.storage_mgr.get_agent_storage_dir("artisan")
        with self.assertRaises(ValueError) as ctx:
            self.storage_mgr.validate_mount_path(artisan_dir, agent="scholar")
        self.assertIn("outside authorized agent storage", str(ctx.exception))

        # Rejection of sensitive system directories
        for bad_path in ("/etc/shadow", "/proc/1", "/sys/kernel", "/dev/null"):
            with self.assertRaises(ValueError) as ctx:
                self.storage_mgr.validate_mount_path(bad_path, agent="scholar")
            self.assertTrue("forbidden" in str(ctx.exception) or "outside authorized" in str(ctx.exception))

    def test_podman_sandbox_build_args_security_hardening(self):
        """PodmanSandbox builds hardened arguments with resource limits and pull-never policy."""
        sandbox = PodmanSandbox(
            storage_manager=self.storage_mgr,
            cmd_runner=self.mock_cmd_runner,
            podman_path="/usr/bin/podman",
            etc_dir=self.mock_etc,
        )

        args = sandbox.build_run_args(
            image="docker.io/library/alpine:3.20",
            cmd=["echo", "hello"],
            agent="scholar",
            network_mode="none",
            read_only_rootfs=True,
            allow_pull=False,
            use_gpu=True,
            memory_limit="256m",
            cpu_limit="0.5",
            pids_limit=64,
        )

        # Check essential security flags
        self.assertIn("--network=none", args)
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop=ALL", args)
        self.assertIn("--security-opt=no-new-privileges", args)
        self.assertIn("--pull=never", args)
        self.assertIn("--memory=256m", args)
        self.assertIn("--cpus=0.5", args)
        self.assertIn("--pids-limit=64", args)
        self.assertIn("--device=nvidia.com/gpu=all", args)
        self.assertTrue(any("/storage/private:rw" in a for a in args))
        self.assertTrue(any("/storage/shared:rw" in a for a in args))
        self.assertEqual(args[-2:], ["echo", "hello"])

    def test_run_sandbox_subuid_rejection(self):
        """Sandbox execution aborts early if the agent user has no subuid allocation."""
        sandbox = PodmanSandbox(
            storage_manager=self.storage_mgr,
            cmd_runner=self.mock_cmd_runner,
            etc_dir=self.mock_etc,
        )

        res = sandbox.run_sandbox(
            image="alpine:3.20",
            cmd=["ls"],
            agent="intruder",
            agent_user="village_intruder",  # Not in mock /etc/subuid
        )

        self.assertFalse(res["ok"])
        self.assertEqual(res["exit_code"], -1)
        self.assertIn("has no subuid/subgid namespace mappings", res["errors"][0])
        self.assertEqual(len(self.executed_cmds), 0)

    def test_run_sandbox_success_and_cleanup(self):
        """Sandbox execution runs via runner and cleanup removes matching containers."""
        sandbox = PodmanSandbox(
            storage_manager=self.storage_mgr,
            cmd_runner=self.mock_cmd_runner,
            etc_dir=self.mock_etc,
        )

        res = sandbox.run_sandbox(
            image="alpine:3.20",
            cmd=["echo", "test"],
            agent="scholar",
            agent_user="village_scholar",
        )

        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["stdout"], "Container output test")

        # Now test cleanup
        clean_res = sandbox.cleanup_agent_containers("scholar")
        self.assertTrue(clean_res["ok"])
        self.assertEqual(clean_res["removed"], ["ai-village-scholar-123", "ai-village-scholar-456"])

    def test_gpu_compute_verification(self):
        """GPU compute verification checks that real mathematical calculation succeeds."""
        sandbox = PodmanSandbox(
            storage_manager=self.storage_mgr,
            cmd_runner=self.mock_cmd_runner,
            etc_dir=self.mock_etc,
        )

        verify_res = verify_gpu_compute(
            sandbox=sandbox,
            agent="scholar",
            agent_user="village_scholar",
            expected_sum=42.0,
        )

        self.assertTrue(verify_res["compute_verified"])
        self.assertTrue(verify_res["result_match"])
        self.assertIn("COMPUTE_RESULT=42.0", verify_res["stdout"])

    def test_describe_agent_capabilities(self):
        """Agent capability description outputs storage paths, available tools, and authority link."""
        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}" if x in ("podman", "git", "python3", "nvidia-smi") else None):
            audit = audit_host_environment(
                cmd_runner=self.mock_cmd_runner,
                etc_dir=self.mock_etc,
                proc_dir=self.mock_proc,
                run_dir=self.mock_run,
            )
            desc = describe_agent_capabilities(
                agent="scholar",
                agent_user="village_scholar",
                config={},
                audit=audit,
                storage_manager=self.storage_mgr,
            )

        self.assertEqual(desc["agent"], "scholar")
        self.assertEqual(desc["user"], "village_scholar")
        self.assertIn("storage/agents/scholar", desc["storage"]["private_directory"])
        self.assertIn("storage/shared", desc["storage"]["shared_directory"])
        self.assertTrue(desc["containers"]["rootless_supported"])
        self.assertIn("pull_policy", desc["containers"])
        self.assertTrue(desc["gpu"]["driver_responsive"])
        self.assertIn("village-authority request", desc["authority"]["request_channel"])


if __name__ == "__main__":
    unittest.main()
