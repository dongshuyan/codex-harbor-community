"""Existing Codex homes are represented by synthetic roots, never user history."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import storage


class ExistingHomes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.homes = {key: (name, str(self.root / key)) for key, name in
                      [('original-local', '本地'), ('original-external', '移动硬盘')]}
        # Patch only the registered paths. No real credentials or sessions are used.
        self.paths = patch.object(storage, 'EXISTING_HOMES', self.homes, create=True)
        self.paths.start()
        self.store = core.Store(self.root / 'control', self.root / 'test-disk', mounted=lambda: True, community=False)
        for _, path in self.homes.values():
            base = core.directory(path)
            core.directory(base / 'sessions')
            (base / 'sessions' / 'synthetic.jsonl').write_text('SYNTHETIC_HISTORY\n')
            (base / 'config.toml').write_text('SYNTHETIC_ORIGINAL_CONFIG\n')
            (base / 'auth.json').write_text('SYNTHETIC_ORIGINAL_AUTH\n')

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def enable(self):
        value = self.store.storage_snapshot()
        rows = {s['id']: s for s in value['storages']}
        for key in self.homes:
            rows[key].update(enabled=True, name=self.homes[key][0])
        ordered = [rows[key] for key in ['original-external', 'original-local', 'external', 'local']]
        return self.store.save_storages(ordered, value['revision'])

    def test_enable_and_priority_only_write_registry(self):
        before = {p: p.read_bytes() for _, path in self.homes.values() for p in Path(path).rglob('*') if p.is_file()}
        value = self.enable()
        self.assertEqual([s['id'] for s in value['storages']],
                         ['original-external', 'original-local', 'external', 'local'])
        self.assertTrue(all(s['available'] and not s['protected'] for s in value['storages'][:2]))
        self.assertEqual(self.store.select_storage('auto'), 'original-external')
        self.assertEqual(self.store.select_storage('original-local'), 'original-local')
        self.assertEqual(before, {p: p.read_bytes() for _, path in self.homes.values() for p in Path(path).rglob('*') if p.is_file()})

    def test_original_history_is_used_in_place_and_runtime_is_private(self):
        self.enable()
        p = self.store.save_profile({'name': 'Synthetic', 'kind': 'api', 'url': 'https://example.invalid/v1',
             'protocol': 'responses', 'api_key': 'SYNTHETIC', 'models': [{'alias': 'gpt-6-luna',
             'upstream': 'synthetic/model', 'context': 128000, 'reasoning': 'off', 'effort': 'low'}]})
        q = self.store.prepare(p['id'], 'original-local')
        original = Path(self.homes['original-local'][1])
        self.assertEqual(Path(q['history']), original)
        self.assertEqual((Path(q['home']) / 'sessions').resolve(), original / 'sessions')
        self.assertEqual((Path(q['home']) / 'sessions/synthetic.jsonl').read_text(), 'SYNTHETIC_HISTORY\n')
        for key in ['base', 'home', 'electron', 'os_home', 'workspace']:
            self.assertTrue(Path(q[key]).is_relative_to(self.store.root))
        q['access_mode']='readonly'  # Explicit synthetic protection, not daily default.
        core.configure(q, 'http://127.0.0.1:9/v1', 'SYNTHETIC_PROXY')
        config = tomllib.loads((Path(q['home']) / 'config.toml').read_text())
        self.assertEqual(config['sqlite_home'], str(original))
        self.assertEqual(config['permissions']['harbor-read-only']['filesystem'][str(original)], 'deny')
        self.assertFalse((original / 'history').exists())
        self.assertFalse((original / 'instances').exists())
        self.assertEqual((original / 'config.toml').read_text(), 'SYNTHETIC_ORIGINAL_CONFIG\n')
        self.assertEqual((original / 'auth.json').read_text(), 'SYNTHETIC_ORIGINAL_AUTH\n')

    def test_official_cache_and_auth_survive_original_test_original_switch(self):
        self.enable(); self.store.initialize_storage('local')
        p = self.store.save_profile({'name': 'Synthetic A', 'kind': 'official'})
        q = self.store.prepare(p['id'], 'original-local')
        home = Path(q['home'])
        self.assertFalse((home / 'auth.json').exists())
        core.write_json(home / 'auth.json', {'synthetic': 'PRIVATE_ACCOUNT'})
        (home / 'session_index.jsonl').write_text('SYNTHETIC_ORIGINAL_INDEX\n')
        for destination in ['original-external', 'local', 'original-local']:
            switched = self.store.prepare(p['id'], destination)
            self.assertEqual(switched['home'], q['home'])
            self.assertEqual(json.loads((home / 'auth.json').read_text()), {'synthetic': 'PRIVATE_ACCOUNT'})
        self.assertEqual((home / 'session_index.jsonl').read_text(), 'SYNTHETIC_ORIGINAL_INDEX\n')

    def test_offline_original_falls_back_and_does_not_create_missing_home(self):
        self.enable()
        external = Path(self.homes['original-external'][1])
        external.rename(external.with_name('offline'))
        self.assertEqual(self.store.select_storage('auto'), 'original-local')
        with self.assertRaises(core.UserError): self.store.select_storage('original-external')
        with self.assertRaises(core.UserError): self.store.initialize_storage('original-external')
        self.assertFalse(external.exists())

    def test_retarget_changes_registration_but_duplicate_paths_are_rejected(self):
        value = self.enable()
        value['storages'][0]['path'] = str(self.root / 'replacement')
        self.store.save_storages(value['storages'], value['revision'])
        self.assertFalse((self.root/'replacement').exists())
        value = self.store.storage_snapshot()
        value['storages'].append(dict(id='b'*32, name='Duplicate', enabled=True, path=self.homes['original-local'][1]))
        with self.assertRaises(core.UserError): self.store.save_storages(value['storages'], value['revision'])

    def test_disappearing_home_after_selection_is_not_recreated(self):
        self.enable()
        p = self.store.save_profile({'name': 'Synthetic A', 'kind': 'official'})
        original = Path(self.homes['original-local'][1])
        select = self.store.select_storage
        def disappear(selection):
            result = select(selection)
            original.rename(original.with_name('offline'))
            return result
        with patch.object(self.store, 'select_storage', side_effect=disappear):
            with self.assertRaises((core.UserError, OSError)): self.store.prepare(p['id'], 'original-local')
        self.assertFalse(original.exists())

    def test_explicit_enable_pins_volume_identity_and_wrong_disk_is_rejected(self):
        disk = Path(self.homes['original-external'][1])
        with patch('storage.volume_identity', side_effect=lambda p: 'SYNTHETIC_UUID' if p == disk else None):
            self.enable()
            self.assertEqual(self.store.registry.entry('original-external')['volume_uuid'], 'SYNTHETIC_UUID')
            self.assertEqual(self.store.select_storage('auto'), 'original-external')
        with patch('storage.volume_identity', side_effect=lambda p: 'WRONG_UUID' if p == disk else None):
            self.assertEqual(self.store.select_storage('auto'), 'original-local')
            with self.assertRaises(core.UserError): self.store.select_storage('original-external')
            snapshot = self.store.storage_snapshot()
            self.store.save_storages(snapshot['storages'], snapshot['revision'])
            self.assertEqual(self.store.registry.entry('original-external')['volume_uuid'], 'SYNTHETIC_UUID')

    def test_remove_restore_and_purge_only_affect_registration(self):
        value = self.enable()
        value = self.store.registry.remove('original-local', value['revision'])
        self.assertIn('original-local', [s['id'] for s in value['removed_storages']])
        value = self.store.registry.restore('original-local', value['revision'])
        self.assertTrue(self.store.registry.state(self.store.registry.entry('original-local'))['available'])
        value = self.store.registry.remove('original-local', value['revision'])
        self.store.registry.purge('original-local', value['revision'])
        original = Path(self.homes['original-local'][1])
        self.assertTrue((original / 'sessions/synthetic.jsonl').exists())
        self.assertEqual((original / 'auth.json').read_text(), 'SYNTHETIC_ORIGINAL_AUTH\n')

    def test_purge_api_credentials_never_targets_original_credentials(self):
        self.enable()
        p = self.store.save_profile({'name': 'Synthetic A', 'kind': 'official'})
        q = self.store.prepare(p['id'], 'original-local')
        core.write_json(Path(q['home']) / 'auth.json', {'synthetic': 'PRIVATE_ACCOUNT'})
        self.store.remove_profile(p['id'], p['revision'])
        archived = self.store.removed_profiles()[0]
        # Disable the separate test disk: purging still requires all registered
        # Harbor credential locations to be reachable, not the original auth.
        self.store.purge_profile(p['id'], archived['revision'])
        for _, path in self.homes.values():
            self.assertEqual((Path(path) / 'auth.json').read_text(), 'SYNTHETIC_ORIGINAL_AUTH\n')


if __name__ == '__main__': unittest.main()
