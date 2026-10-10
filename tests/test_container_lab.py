"""P134: the container lab and the local GPUs are named concretely in each
resident's habitat line, with the exact first commands. Local, synthetic."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from runtime import lab_guidance
from village.containers import HostResourceAudit, describe_agent_capabilities

M10 = [f'GPU {i}: Tesla M10 (UUID: GPU-{i}aaaa)' for i in range(4)]
ROOT = Path(__file__).resolve().parents[1]


class LabGuidanceTests(unittest.TestCase):
    def test_ready_agent_gets_pinned_pull_recipe_and_gpu_line(self):
        text = lab_guidance({'containers': {'rootless_supported': True}, 'gpu': {'devices': M10}})
        self.assertIn('podman build', text)
        self.assertIn('skopeo inspect', text)
        self.assertIn('@sha256:', text)
        self.assertIn('Never use --privileged', text)
        self.assertIn('4x Tesla M10', text)
        self.assertIn('sm_50', text)
        self.assertIn('--device nvidia.com/gpu=all', text)

    def test_agent_without_podman_is_told_how_to_get_it(self):
        text = lab_guidance({'containers': {'rootless_supported': False}, 'gpu': {}})
        self.assertIn("'containers' capability", text)
        self.assertNotIn('skopeo', text)
        self.assertNotIn('GPUs', text)

    def test_capability_description_exposes_device_list(self):
        audit = HostResourceAudit(
            cpu_count=4, ram_total_bytes=1, ram_available_bytes=1, disk_mounts=[],
            subuid_mappings={'village-x': (1, 2)}, subgid_mappings={'village-x': (1, 2)},
            xdg_runtime_dirs={}, cdi_spec_files=['/etc/cdi/nvidia.yaml'],
            driver_status={'installed': True, 'responsive': True, 'devices': M10},
            podman_status={'installed': True}, compose_provider=None, detected_defects=[])
        desc = describe_agent_capabilities('x', 'village-x', {}, audit)
        self.assertEqual(desc['gpu']['devices'], M10)
        self.assertTrue(desc['gpu']['gpu_compute_ready'])

    def test_full_prompt_names_the_lab_but_keeps_the_safety_lines(self):
        full = (ROOT / 'prompts/resident-system.txt').read_text()
        self.assertIn('pull public images pinned by digest', full)
        self.assertIn('yours to experiment, train or serve', full)
        self.assertIn('Inspect third-party recipes before running them', full)
        core = (ROOT / 'prompts/resident-core.txt').read_text()
        self.assertIn('no privileged containers', core)


if __name__ == '__main__':
    unittest.main()
