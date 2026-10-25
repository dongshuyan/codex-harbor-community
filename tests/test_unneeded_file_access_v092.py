"""Synthetic file-access diagnostics; no desktop, credentials or model calls.

The explicit-selection test is an intentional RED reproduction against
06e2a45. Filesystem observations do not identify or count macOS TCC prompts.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import main
import storage
from organization import STATE
from test_background_access_v09 import accesses_under


class UnneededFileAccess(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='harbor-access-v092-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.desktop = self.root / 'synthetic-user' / 'Desktop'
        self.desktop.mkdir(parents=True)
        original_homes = patch.object(storage, 'EXISTING_HOMES', {})
        original_homes.start()
        self.addCleanup(original_homes.stop)
        self.store = core.Store(self.root / 'control', self.root / 'selected-disk',
                                mounted=lambda: True, community=False)
        self.store.initialize_storage('external')
        self.profile = self.store.save_profile({'kind': 'official', 'name': 'Synthetic account'})

    def add_disabled_desktop_history(self, layout='native'):
        history = self.desktop / 'unselected-history'
        history.mkdir()
        value = self.store.registry.read()
        value['storages'].append(dict(id='d' * 32, name='Disabled Desktop history',
            path=str(history), enabled=False, layout=layout))
        core.write_json(self.store.registry.path, value)
        return history

    def assert_no_desktop_access(self, accesses):
        self.assertEqual([(operation, str(Path(path).relative_to(self.root)))
                          for operation, path in accesses], [],
                         'Explicit external selection probed an unrelated Desktop path')

    def test_external_prepare_does_not_probe_disabled_desktop_history(self):
        self.add_disabled_desktop_history()

        with accesses_under(self.desktop) as accesses:
            prepared = self.store.prepare(self.profile['id'], 'external',
                                         launch_job='a' * 32, instance_slot='primary')

        self.assertEqual(prepared['storage'], 'external')
        self.assertTrue(Path(prepared['history']).is_relative_to(self.root / 'selected-disk'))
        self.assert_no_desktop_access(accesses)

    def test_external_prepare_does_not_enumerate_disabled_desktop_runtimes(self):
        history = self.add_disabled_desktop_history(layout='harbor')
        (history / 'instances').mkdir()

        with accesses_under(self.desktop) as accesses:
            self.store.prepare(self.profile['id'], 'external',
                               launch_job='a' * 32, instance_slot='primary')

        self.assertEqual([(operation, str(Path(path).relative_to(self.root)))
                          for operation, path in accesses if operation == 'scandir'], [],
                         'Legacy discovery enumerated an unrelated disabled Desktop runtime')

    def test_status_does_not_probe_disabled_desktop_history(self):
        self.add_disabled_desktop_history()

        with accesses_under(self.desktop) as accesses:
            result = main.status(self.store)

        disabled = next(item for item in result['storages'] if item['id'] == 'd' * 32)
        self.assertEqual(disabled['reason'], '已停用')
        self.assert_no_desktop_access(accesses)

    def test_project_metadata_does_not_scan_desktop_project_roots(self):
        history = self.store.registry.history_path(self.store.registry.entry('external'))
        core.write_json(history / STATE, {'local-projects': {
            'synthetic-project': {'id': 'synthetic-project', 'name':'Synthetic', 'rootPaths': [str(self.desktop)]}},
            'project-order': ['synthetic-project'], 'electron-saved-workspace-roots': [str(self.desktop)]})

        with accesses_under(self.desktop) as accesses:
            self.store.prepare(self.profile['id'], 'external',
                               launch_job='b' * 32, instance_slot='primary')

        self.assert_no_desktop_access(accesses)


if __name__ == '__main__':
    unittest.main()
