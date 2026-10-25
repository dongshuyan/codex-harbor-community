"""Named storage selection uses only synthetic directories and accounts."""
import copy
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import storage


class StorageSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.originals = {key: [key, str(self.root/key)] for key in ('original-local', 'original-external')}
        for mocked in (patch.object(storage, 'EXISTING_HOMES', self.originals),
                       patch.dict(core.MACHINE, {'original_homes': self.originals})):
            mocked.start(); self.addCleanup(mocked.stop)
        self.store = core.Store(self.root/'control', self.root/'disk', mounted=lambda: True, community=False)

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, path, name='第三处'):
        snapshot = self.store.storage_snapshot()
        snapshot['storages'].append({'id': '', 'name': name, 'path': str(path), 'enabled': True})
        return self.store.save_storages(snapshot['storages'], snapshot['revision'])['storages'][-1]

    def test_original_paths_stay_disabled_until_explicit_enable(self):
        value = self.store.storage_snapshot()
        original = [s for s in value['storages'] if s['id'].startswith('original-')]
        self.assertEqual(len(original), 2)
        self.assertTrue(all(not s['enabled'] and not s['available'] for s in original))
        self.assertTrue(all(not s['protected'] for s in original))
        for item in original:
            with self.assertRaises(core.UserError): self.store.select_storage(item['id'])
        # Live enabling is exercised only with synthetic homes in test_existing_homes.

    def test_auto_uses_priority_and_existence_manual_overrides(self):
        self.store.initialize_storage('local')
        self.assertEqual(self.store.select_storage('auto'), 'local')
        self.store.initialize_storage('external')
        self.assertEqual(self.store.select_storage('auto'), 'external')
        self.assertEqual(self.store.select_storage('local'), 'local')
        value = self.store.storage_snapshot()
        value['storages'][:2] = reversed(value['storages'][:2])
        self.store.save_storages(value['storages'], value['revision'])
        self.assertEqual(self.store.select_storage('auto'), 'local')

    def test_registration_does_not_create_path_and_init_is_explicit(self):
        item = self.register(self.root/'new')
        self.assertFalse((self.root/'new').exists())
        with self.assertRaises(core.UserError): self.store.select_storage(item['id'])
        self.store.initialize_storage(item['id'])
        self.assertEqual(self.store.select_storage(item['id']), item['id'])

    def test_missing_disk_never_recreated_and_auto_falls_back(self):
        self.store.initialize_storage('local')
        self.store.mounted = lambda: False
        with self.assertRaises(core.UserError): self.store.initialize_storage('external')
        self.assertFalse(self.store.external.exists())
        self.assertEqual(self.store.select_storage('auto'), 'local')

    def test_read_only_directory_is_unavailable_and_auto_uses_writable_local(self):
        self.store.initialize_storage('external')
        self.store.initialize_storage('local')
        self.store.external.chmod(0o500)
        try:
            self.assertFalse(os.access(self.store.external, os.W_OK | os.X_OK),
                             'This check requires an unprivileged process that cannot write chmod 0500 directories.')
            item = next(s for s in self.store.storage_snapshot()['storages'] if s['id'] == 'external')
            self.assertTrue(item['exists'])
            self.assertFalse(item['available'])
            self.assertIn('不可写', item['reason'])
            self.assertEqual(self.store.select_storage('auto'), 'local')
            with self.assertRaises(core.UserError): self.store.select_storage('external')
        finally:
            self.store.external.chmod(0o700)

    def test_initialize_rejects_stale_revision_and_target_path(self):
        item = self.register(self.root/'first')
        confirmed = self.store.storage_snapshot()
        changed = copy.deepcopy(confirmed['storages'])
        next(s for s in changed if s['id'] == item['id'])['path'] = str(self.root/'second')
        updated = self.store.save_storages(changed, confirmed['revision'])
        with self.assertRaises(core.UserError):
            self.store.initialize_storage(item['id'], revision=confirmed['revision'], path=item['path'])
        with self.assertRaises(core.UserError):
            self.store.initialize_storage(item['id'], revision=updated['revision'], path=item['path'])
        self.assertFalse((self.root/'first').exists())
        self.assertFalse((self.root/'second').exists())
        self.store.initialize_storage(item['id'], revision=updated['revision'], path=str(self.root/'second'))
        self.assertTrue((self.root/'second').is_dir())

    def test_no_available_directory_fails_without_making_one(self):
        with self.assertRaises(core.UserError): self.store.select_storage('auto')
        self.assertFalse((self.store.root/'stores/local').exists())

    def test_symlink_overlapping_and_original_paths_are_rejected(self):
        target = self.root/'target'; target.mkdir()
        link = self.root/'link'; link.symlink_to(target)
        for path in [link, self.store.root/'stores/local/child', self.root/'original-local',
                     self.root/'original-external/subdir', self.store.root/'profiles']:
            with self.assertRaises(core.UserError): self.register(path)
        self.assertEqual(list(target.iterdir()), [])

    def test_stale_save_does_not_replace_newer_settings(self):
        old = self.store.storage_snapshot()
        self.register(self.root/'new')
        with self.assertRaises(core.UserError): self.store.save_storages(old['storages'], old['revision'])
        self.assertEqual(len(self.store.storage_snapshot()['storages']), 5)

    def test_restored_location_can_move_ahead_of_active_locations(self):
        snapshot = self.store.storage_snapshot()
        custom = dict(id='c'*32, name='第三处', path=str(self.root/'custom'), enabled=True)
        snapshot['storages'].insert(2, custom)
        self.store.save_storages(snapshot['storages'], snapshot['revision'])
        for ident in ['external', 'local', custom['id']]:
            self.store.initialize_storage(ident)
        protected = [s for s in self.store.storage_snapshot()['storages'] if s['protected']]
        self.store.registry.remove('local', self.store.storage_snapshot()['revision'])
        restored = self.store.registry.restore('local', self.store.storage_snapshot()['revision'])

        # Restoration keeps every existing priority, including disabled homes.
        # The restored item starts last and can move ahead of any location.
        self.assertEqual([s['id'] for s in restored['storages']],
                         ['external', custom['id'], 'original-local', 'original-external', 'local'])
        self.assertEqual(self.store.select_storage('auto'), 'external')
        items = restored['storages']
        index = next(i for i, s in enumerate(items) if s['id'] == 'local')
        while index:
            self.assertFalse(items[index-1]['protected'])
            items[index-1], items[index] = items[index], items[index-1]
            index -= 1
        self.store.save_storages(items, restored['revision'])
        self.assertEqual(self.store.select_storage('auto'), 'local')
        self.assertEqual([s for s in self.store.storage_snapshot()['storages'] if s['protected']], protected)

    def test_restore_preserves_active_priority_from_an_older_interspersed_registry(self):
        custom = self.register(self.root/'custom')
        self.store.registry.remove(custom['id'], self.store.storage_snapshot()['revision'])
        snapshot = self.store.storage_snapshot()
        local = next(s for s in snapshot['storages'] if s['id'] == 'local')
        interspersed = [s for s in snapshot['storages'] if s['id'] != 'local'] + [local]
        self.store.save_storages(interspersed, snapshot['revision'])
        restored = self.store.registry.restore(custom['id'], self.store.storage_snapshot()['revision'])
        self.assertEqual([s['id'] for s in restored['storages']],
                         ['external', 'original-local', 'original-external', 'local', custom['id']])

    def test_running_custom_store_blocks_profile_edit_and_storage_retarget(self):
        item = self.register(self.root/'custom')
        self.store.initialize_storage(item['id'])
        p = self.store.save_profile({'name':'合成账号', 'kind':'official'})
        with self.store.lock_path(item['id'], p['id']).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(core.UserError): self.store.save_profile(dict(p, name='改名'))
            value = self.store.storage_snapshot()
            value['storages'][-1]['path'] = str(self.root/'other')
            with self.assertRaises(core.UserError): self.store.save_storages(value['storages'], value['revision'])

    def test_prepare_without_model_enables_all_mappings(self):
        self.store.initialize_storage('local')
        data={'name':'合成 API', 'kind':'api', 'url':'https://example.invalid/v1',
              'api_key':'SYNTHETIC', 'protocol':'responses', 'models':[
                  {'alias':a, 'upstream':u, 'context':128000, 'effort':'low','reasoning':'off'}
                  for a,u in [('gpt-6-luna','test/one'),('gpt-6-astra','test/two')]]}
        p=self.store.save_profile(data)
        prepared=self.store.prepare(p['id'],'local')
        core.configure(prepared,'http://127.0.0.1:9/v1','SYNTHETIC')
        models=json.loads((Path(prepared['home'])/'smart2-models.json').read_text())['models']
        self.assertEqual({m['slug'] for m in models}, {'gpt-6-luna','gpt-6-astra'})

    def test_removed_path_keeps_sensitive_file_protection(self):
        self.store.initialize_storage('local')
        retired=self.register(self.root/'retired')
        private=Path(self.store.registry.runtime_location(retired)['path'])
        value=self.store.storage_snapshot()
        self.store.save_storages([s for s in value['storages'] if s['id']!=retired['id']],value['revision'])
        p=self.store.save_profile({'name':'合成账号','kind':'official'})
        prepared=self.store.prepare(p['id'],'local');prepared['access_mode']='readonly';core.configure(prepared)
        text=(Path(prepared['home'])/'config.toml').read_text()
        self.assertIn(str(private/'instances/*/codex/auth.json'),text)
        self.assertIn(str(private/'instances/*/desktop'),text)

    def test_removed_before_worker_start_produces_error_job(self):
        import launcher
        item=self.register(self.root/'retired');p=self.store.save_profile({'name':'合成账号','kind':'official'})
        value=self.store.storage_snapshot()
        self.store.save_storages([s for s in value['storages'] if s['id']!=item['id']],value['revision'])
        job='f'*32
        core.write_json(self.store.root/'jobs'/f'{job}.json',{'status':'starting'})
        with patch('launcher.snapshot',side_effect=AssertionError('must not snapshot')),patch('launcher.subprocess.Popen',side_effect=AssertionError('must not launch')):
            result=launcher.run_instance(self.store,job,p['id'],item['id'],None)
        self.assertEqual(result['status'],'error')
        self.assertEqual(json.loads((self.store.root/'jobs'/f'{job}.json').read_text())['status'],'error')

    def test_unbound_disk_requires_explicit_initialization_and_pins_identity(self):
        with patch('storage.volume_identity',return_value=''):
            item=self.register(self.root/'removable')
        with patch('storage.volume_identity',return_value='SYNTHETIC-UUID'):
            self.assertFalse(self.store.registry.disk_available(item))
            self.store.initialize_storage(item['id'])
            bound=self.store.registry.entry(item['id'])
            self.assertTrue(self.store.registry.disk_available(bound))
        with patch('storage.volume_identity',return_value='OTHER-UUID'):
            self.assertFalse(self.store.registry.disk_available(bound))


if __name__ == '__main__': unittest.main()
