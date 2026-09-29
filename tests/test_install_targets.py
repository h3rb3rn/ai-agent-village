"""P65: build_targets() must actually cover the dashboard's own files.

Live regression: web/webui.py and web/observatory.{html,css,js} were never
in install-runtime.py's target list at all - every other resident-agent-
facing file had a redeploy path, but a P64 Gazette dashboard change was
pushed, "installed" successfully (no error, no warning), and the host kept
serving the old webui.py/observatory.* indefinitely until a manual
install+restart. This test locks in that these files are covered, and that
webui.py's target actually matches its systemd unit's ExecStart path (it is
exec'd directly, not via `python3 <path>`, unlike every other .py target
here - see the explicit chmod(0o755) in main()).
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location('install_runtime', Path(__file__).parents[1] / 'scripts/install-runtime.py')
install_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_runtime)

REPO_ROOT = Path(__file__).parents[1]


class BuildTargetsTests(unittest.TestCase):
    def test_webui_and_observatory_assets_are_covered(self):
        targets = install_runtime.build_targets(REPO_ROOT)
        expected = {
            Path('/usr/local/lib/ai-village/webui.py'): REPO_ROOT / 'web/webui.py',
            Path('/usr/local/share/ai-village/web/observatory.html'): REPO_ROOT / 'web/observatory.html',
            Path('/usr/local/share/ai-village/web/observatory.css'): REPO_ROOT / 'web/observatory.css',
            Path('/usr/local/share/ai-village/web/observatory.js'): REPO_ROOT / 'web/observatory.js',
        }
        for dest, src in expected.items():
            self.assertIn(dest, targets, f'{dest} missing from build_targets()')
            self.assertEqual(targets[dest], src)

    def test_webui_target_matches_the_live_systemd_execstart_path(self):
        # Confirmed live on N06-M10: `systemctl cat ai-village-webui.service`
        # shows ExecStart=/usr/local/lib/ai-village/webui.py (no `python3`
        # prefix) - if this destination path ever drifted from that unit
        # file, redeploys would silently keep running stale code again.
        targets = install_runtime.build_targets(REPO_ROOT)
        self.assertIn(Path('/usr/local/lib/ai-village/webui.py'), targets)

    def test_all_target_sources_exist_in_this_checkout(self):
        targets = install_runtime.build_targets(REPO_ROOT)
        missing = [str(src) for src in targets.values() if not src.is_file()]
        self.assertEqual(missing, [])

    def test_every_release_file_declared_for_web_or_village_is_a_build_target_or_explicitly_exempt(self):
        # Not every RELEASE_FILES entry belongs in this targeted installer
        # (authority.py, telemetry-collector.py, memory/projection.py etc.
        # have their own separate services/lifecycles) - but web/*.py and
        # village/*.py modules are exactly this installer's job, so any of
        # those missing here would be the same class of drift this package
        # fixes, just for a different file.
        from village.release import RELEASE_FILES
        targets = install_runtime.build_targets(REPO_ROOT)
        target_sources = {src.relative_to(REPO_ROOT) for src in targets.values()}
        exempt = {Path('web/observer.py'), Path('web/telemetry-collector.py')}
        missing = [
            src_rel for src_rel, _, _ in RELEASE_FILES
            if (src_rel.startswith('web/') or src_rel.startswith('village/'))
            and Path(src_rel) not in target_sources and Path(src_rel) not in exempt
        ]
        self.assertEqual(missing, [], f'in RELEASE_FILES but not build_targets(): {missing}')


if __name__ == '__main__':
    unittest.main()
