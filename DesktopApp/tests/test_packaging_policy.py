"""Safety gates for the reviewed native-library exclusions."""
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('quaber_packaging_policy', ROOT / 'packaging/policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
check_spec = importlib.util.spec_from_file_location('quaber_release_check', ROOT / 'packaging/release_check.py')
release_check = importlib.util.module_from_spec(check_spec)
with patch.dict(sys.modules, {'policy': policy}):
    check_spec.loader.exec_module(release_check)


class PackagingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy_dir = self.root / 'packaging'
        self.policy_dir.mkdir()
        (self.root / 'requirements.lock').write_text('locked')
        self.source = self.root / 'library.dll'
        self.source.write_bytes(b'reviewed DLL')
        self.row = {'path': 'PyQt5/Qt5/bin/Qt5Quick.dll', 'bytes': self.source.stat().st_size,
                    'sha256': policy.digest(self.source)}
        self.fixture = {'lock_sha256': policy.digest(self.root / 'requirements.lock'),
                        'qt_paths': [self.row['path']],
                        'groups': {'webgl_qml_quick': [self.row], 'alternate_platforms': [],
                                   'portal_dbus': [], 'translations_except_ru': []}}
        (self.policy_dir / 'policy-files.json').write_text(json.dumps(self.fixture))
        self.entries = [(self.row['path'], str(self.source), 'BINARY')]
        patcher = patch.object(policy, 'HERE', self.policy_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_baseline_retains_full_composition(self):
        self.assertEqual(policy.apply_policy(self.entries, [], 'baseline', self.root / 'report'),
                         (self.entries, []))

    def test_runtime_assets_keep_ui_and_license_but_exclude_build_inputs(self):
        with patch.object(policy, 'HERE', ROOT / 'packaging'):
            resources = policy.runtime_resources()
        self.assertTrue({'assets/check.svg', 'assets/fonts/Roboto.ttf', 'assets/fonts/OFL.txt',
                         'public/icon.png', 'public/logo.png'} <= resources.keys())
        self.assertFalse({'assets/fonts/README.md', 'public/icon.ico', 'assets/logo.png'} & resources.keys())

    def test_release_rejects_even_empty_legacy_folders(self):
        for name in ('addons', 'dumps', 'UserData', 'reports', 'tests', 'Builds', 'tzdata', 'dateutil', 'zoneinfo'):
            with self.subTest(folder=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / name).mkdir()
                with patch.dict(sys.modules, {'policy': policy}), self.assertRaisesRegex(RuntimeError, 'Unexpected release'):
                    release_check.verify_clean_payload(root, {})
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {'policy': policy}):
            root = Path(folder)
            (root / 'public').mkdir()
            release_check.verify_clean_payload(root, {'public/icon.png': root / 'public/icon.png'})
            with self.assertRaisesRegex(RuntimeError, 'icon.ico'):
                release_check.verify_clean_payload(root, {'public/icon.ico': root / 'public/icon.ico'})

    def test_unknown_native_dependency_stops_build(self):
        with self.assertRaisesRegex(RuntimeError, 'Qt inventory changed'):
            policy.apply_policy(self.entries + [('PyQt5/new.dll', str(self.source), 'BINARY')],
                                [], 'baseline', self.root / 'report')

    def test_changed_lock_stops_even_baseline(self):
        (self.root / 'requirements.lock').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'Lock changed'):
            policy.apply_policy(self.entries, [], 'baseline', self.root / 'report')

    def test_changed_exclusion_hash_stops_candidate(self):
        self.source.write_bytes(b'unreviewed!!')
        with self.assertRaisesRegex(RuntimeError, 'Policy file changed'):
            policy.apply_policy(self.entries, [], 'minimal-candidate', self.root / 'report')

    def test_raster_cannot_be_selected(self):
        with self.assertRaisesRegex(RuntimeError, 'unapproved'):
            policy.apply_policy(self.entries, [], 'raster-candidate', self.root / 'report')

    def test_missing_reviewed_path_stops_build(self):
        with self.assertRaisesRegex(RuntimeError, 'Qt inventory changed'):
            policy.apply_policy([], [], 'minimal-candidate', self.root / 'report')

    def test_exclusion_logs_evidence_without_deleting_source(self):
        logos = [('assets/logo.png', str(self.source), 'DATA'),
                 ('public/logo.png', str(self.source), 'DATA')]
        binaries, datas = policy.apply_policy(self.entries, logos, 'minimal-candidate', self.root / 'report')
        self.assertEqual(binaries, [])
        self.assertEqual(datas, logos)
        self.assertEqual(self.source.read_bytes(), b'reviewed DLL')
        evidence = json.loads((self.root / 'report/exclusions.json').read_text())
        self.assertEqual(evidence['files'][0]['sha256'], self.row['sha256'])
        self.assertTrue(evidence['files'][0]['reason'])
