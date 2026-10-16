"""Multi-instance acceptance reproductions; synthetic state only, no desktop/network.

Every filesystem fixture is under /private/tmp/harbor-multi-research-*.
Explicit launch_job values represent two independently launched desktop instances.
"""
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import launcher


class SyntheticGateway:
    """No listener or upstream request is needed to exercise preparation."""
    url = 'http://127.0.0.1:8011/v1'
    token = 'SYNTHETIC_GATEWAY_TOKEN'
    log_failed = False

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def close(self):
        pass


class MultiInstanceV09(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='harbor-multi-research-', dir='/private/tmp')
        self.root = Path(self.tmp.name)
        self.store = core.Store(self.root / 'control', self.root / 'external',
                                mounted=lambda: True, community=False)
        for storage in ('local', 'external'):
            self.store.initialize_storage(storage)
        self.official = self.store.save_profile({'kind': 'official', 'name': 'Synthetic official'})
        self.router = self.store.save_profile({
            'kind': 'api', 'name': 'Synthetic Router', 'protocol': 'responses',
            'url': 'https://example.invalid/v1', 'api_key': 'SYNTHETIC_API_KEY',
            'models': [{'alias': 'gpt-6-astra', 'upstream': 'synthetic/model',
                        'context': 128000, 'reasoning': 'standard',
                        'levels': ['low', 'max'], 'effort': 'low'}],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, profile, storage, job):
        return self.store.prepare(profile['id'], storage, launch_job=job)

    def assert_private_runtime_paths_differ(self, first, second):
        for name in ('home', 'electron', 'os_home'):
            with self.subTest(path=name):
                self.assertNotEqual(Path(first[name]).resolve(), Path(second[name]).resolve(),
                                    f'Two launches must not share writable {name}')

    def test_same_official_account_has_private_runtime_per_launch_in_one_storage(self):
        first = self.prepare(self.official, 'local', '1' * 32)
        second = self.prepare(self.official, 'local', '2' * 32)
        self.assert_private_runtime_paths_differ(first, second)

    def test_same_router_has_private_runtime_per_launch_in_one_storage(self):
        first = self.prepare(self.router, 'local', '1' * 32)
        second = self.prepare(self.router, 'local', '2' * 32)
        self.assert_private_runtime_paths_differ(first, second)

    def test_official_launch_in_other_storage_cannot_retarget_first_history(self):
        first = self.prepare(self.official, 'local', '1' * 32)
        first_links = {name: (Path(first['home']) / name).resolve()
                       for name in ('sessions', 'archived_sessions', 'thread-writer-locks')}
        second = self.prepare(self.official, 'external', '2' * 32)
        self.assertNotEqual(first['history'], second['history'])
        for name, target in first_links.items():
            with self.subTest(link=name):
                self.assertEqual((Path(first['home']) / name).resolve(), target,
                                 'Starting another instance must preserve the first history binding')

    def test_second_router_keeps_first_gateway_and_credentials_unchanged(self):
        first = self.prepare(self.router, 'local', '1' * 32)
        core.configure(first, 'http://127.0.0.1:8011/v1', 'SYNTHETIC_FIRST_TOKEN')
        first_home = Path(first['home'])
        before = {name: (first_home / name).read_bytes() for name in ('config.toml', 'auth.json')}
        second = self.prepare(self.router, 'local', '2' * 32)
        core.configure(second, 'http://127.0.0.1:8022/v1', 'SYNTHETIC_SECOND_TOKEN')
        self.assertEqual(tomllib.loads((Path(second['home']) / 'config.toml').read_text())
                         ['openai_base_url'], 'http://127.0.0.1:8022/v1')
        for name, original in before.items():
            with self.subTest(file=name):
                self.assertEqual((first_home / name).read_bytes(), original,
                                 'A second Router must not change an already running route')

    def test_second_official_storage_keeps_first_configuration_unchanged(self):
        first = self.prepare(self.official, 'local', '1' * 32)
        core.configure(first)
        config = Path(first['home']) / 'config.toml'
        config.write_text(config.read_text() + '\n[desktop]\ntheme = "synthetic-first"\n')
        original = config.read_bytes()
        second = self.prepare(self.official, 'external', '2' * 32)
        core.configure(second)
        self.assertEqual(config.read_bytes(), original,
                         'A cross-storage launch must not rewrite the first sqlite_home or preferences')

    def test_same_storage_still_shares_native_thread_writer_locks(self):
        first = self.prepare(self.official, 'local', '1' * 32)
        second = self.prepare(self.official, 'local', '2' * 32)
        router = self.prepare(self.router, 'local', '3' * 32)
        for prepared in (second, router):
            self.assertEqual(first['history'], prepared['history'])
            self.assertEqual((Path(first['home']) / 'thread-writer-locks').resolve(),
                             (Path(prepared['home']) / 'thread-writer-locks').resolve())

    def test_cross_storage_launch_preserves_history_in_place(self):
        first = self.prepare(self.official, 'local', '1' * 32)
        marker = Path(first['history']) / 'sessions' / 'synthetic-presence-marker'
        marker.touch()
        second = self.prepare(self.official, 'external', '2' * 32)
        self.assertTrue(marker.is_file())
        self.assertFalse((Path(second['history']) / 'sessions' / marker.name).exists())

    def test_live_sibling_does_not_block_preparation_of_new_private_instance(self):
        # Stop at the lab boundary, before desktop discovery or any process spawn.
        # A reported live sibling is enough to reproduce the current rejection.
        for profile, storage in ((self.official, 'local'), (self.official, 'external'),
                                 (self.router, 'local')):
            with self.subTest(kind=profile['kind'], storage=storage):
                job = ('a' if storage == 'local' else 'b') * 32
                observed = []
                temporary = core.directory(self.root / ('ipc-' + profile['id'] + '-' + storage))

                def live(selected, ident):
                    if selected == 'local' and ident == profile['id']:
                        return {'id': ident, 'storage': 'local', 'job': 'f' * 32, 'status': 'running'}
                    return None

                def stop_before_desktop(prepared, env, args):
                    observed.append(prepared)
                    raise OSError('synthetic stop before desktop spawn')

                with patch.object(self.store, 'live_desktop', side_effect=live), \
                        patch('launcher.snapshot', return_value='synthetic-backup'), \
                        patch('launcher.process_stamp', return_value='synthetic-worker'), \
                        patch('launcher.tempfile.mkdtemp', return_value=str(temporary)), \
                        patch('launcher.Gateway', SyntheticGateway), \
                        patch('launcher.subprocess.Popen', side_effect=AssertionError('desktop is prohibited')):
                    state = launcher.run_instance(self.store, job, profile['id'], storage, None,
                                                  lab=stop_before_desktop)
                self.assertEqual(len(observed), 1, state.get('message'))


if __name__ == '__main__':
    unittest.main()
