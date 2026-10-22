import copy
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core


def api(name='Router A'):
    return {'id': '', 'name': name, 'kind': 'api', 'url': 'https://example.invalid/v1',
            'protocol': 'responses', 'api_key': 'SYNTHETIC_KEY', 'models': [
                {'alias': 'gpt-6-astra', 'upstream': 'infi/glm-5.3',
                 'effort': 'low', 'context': 128000, 'reasoning': 'off'}]}


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # This suite covers private-edition file storage; Community's empty
        # defaults and Keychain behavior are covered by test_community.
        self.store = core.Store(self.root / 'control', self.root / 'external',
                                mounted=lambda: True, community=False)
        self.store.initialize_storage('local')

    def tearDown(self):
        self.tmp.cleanup()

    def test_secrets_never_appear_in_public_profile(self):
        p = self.store.save_profile(api())
        self.assertNotIn('SYNTHETIC_KEY', json.dumps(p))
        self.assertTrue(p['has_key'])
        self.assertEqual(self.store.secret(p['id']), 'SYNTHETIC_KEY')
        self.assertEqual(self.store.secret_path(p['id']).stat().st_mode & 0o777, 0o600)

    def test_empty_key_edit_preserves_key_and_new_profile_is_independent(self):
        a = self.store.save_profile(api())
        b = self.store.save_profile(api('Router B'))
        edit = dict(a, api_key='', name='Renamed')
        self.store.save_profile(edit)
        self.assertEqual(self.store.secret(a['id']), 'SYNTHETIC_KEY')
        self.assertNotEqual(a['id'], b['id'])

    def test_two_profiles_share_history_but_not_auth_or_electron_state(self):
        a = self.store.save_profile(api())
        b = self.store.save_profile(api('Router B'))
        pa = self.store.prepare(a['id'], 'local', 'gpt-6-astra')
        pb = self.store.prepare(b['id'], 'local', 'gpt-6-astra')
        self.assertEqual(pa['history'], pb['history'])
        self.assertNotEqual(pa['home'], pb['home'])
        self.assertNotEqual(pa['electron'], pb['electron'])
        self.assertEqual((Path(pa['home'])/'sessions').resolve(), Path(pa['history'])/'sessions')
        self.assertEqual((Path(pa['home'])/'thread-writer-locks').resolve(), Path(pa['history'])/'thread-writer-locks')
        self.assertFalse((Path(pa['home'])/'auth.json').exists())

    def test_manual_local_wins_even_with_disk_connected(self):
        p = self.store.save_profile(api())
        a = self.store.prepare(p['id'], 'local', 'gpt-6-astra')
        self.store.initialize_storage('external')
        b = self.store.prepare(p['id'], 'external', 'gpt-6-astra')
        self.assertEqual(a['storage'], 'local')
        self.assertNotEqual(a['history'], b['history'])

    def test_absent_disk_auto_uses_local_explicit_external_fails(self):
        self.store.mounted = lambda: False
        p = self.store.save_profile(api())
        self.assertEqual(self.store.prepare(p['id'], 'auto', 'gpt-6-astra')['storage'], 'local')
        with self.assertRaises(core.UserError):
            self.store.prepare(p['id'], 'external', 'gpt-6-astra')
        self.assertFalse(self.store.external.exists())

    def test_symlinked_history_root_is_rejected_without_touching_target(self):
        p = self.store.save_profile(api())
        outside = self.root/'outside'
        outside.mkdir()
        hist = self.store.root/'stores/local/history'
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.UserError):
            self.store.prepare(p['id'], 'local', 'gpt-6-astra')
        self.assertEqual(list(outside.iterdir()), [])

    def test_duplicate_alias_unknown_model_and_unsafe_url_rejected(self):
        for mutate in [lambda p: p['models'].append(copy.deepcopy(p['models'][0])),
                       lambda p: p['models'][0].update(alias='imaginary-model'),
                       lambda p: p.update(url='http://remote.invalid/v1'),
                       lambda p: p.update(url='https://user:secret@example.invalid/v1'),
                       lambda p: p.update(id='../../escape')]:
            p=api(); mutate(p)
            with self.assertRaises(core.UserError): self.store.save_profile(p)

    def test_mapping_replaces_name_never_silently_falls_back(self):
        p=api()
        request={'model': 'gpt-6-astra', 'input': 'TEST', 'reasoning': {'effort':'high'}}
        result=core.map_request(p,request)
        self.assertEqual(result['model'],'infi/glm-5.3')
        self.assertNotIn('reasoning',result)
        self.assertEqual(request['model'],'gpt-6-astra')
        with self.assertRaises(core.UserError):
            core.map_request(p,dict(request,model='gpt-6-luna'))

    def test_official_profile_does_not_create_or_import_auth(self):
        p=self.store.save_profile({'name':'Account 1','kind':'official','models':[]})
        prepared=self.store.prepare(p['id'],'local','gpt-6-luna')
        self.assertFalse((Path(prepared['home'])/'auth.json').exists())
        self.assertFalse(p['has_key'])

    def test_api_catalog_uses_direct_tools_for_every_mapped_model(self):
        original=core.catalog()
        data=api()
        for alias in ['gpt-6-luna','gpt-6.1-sol']:
            data['models'].append(dict(data['models'][0],alias=alias))
        p=self.store.save_profile(data)
        prepared=self.store.prepare(p['id'],'local','gpt-6-astra')
        core.configure(prepared,'http://127.0.0.1:9/v1','SYNTHETIC_LOOPBACK')
        models=json.loads((Path(prepared['home'])/'smart2-models.json').read_text())['models']
        self.assertEqual({m['slug'] for m in models},{m['alias'] for m in data['models']})
        for model in models:
            self.assertEqual(model['tool_mode'],'direct')
            self.assertEqual(model['apply_patch_tool_type'],'freeform')
        self.assertEqual(core.catalog(),original)

    def test_official_catalog_keeps_original_tool_modes(self):
        p=self.store.save_profile({'name':'Official','kind':'official'})
        prepared=self.store.prepare(p['id'],'local','gpt-6-luna')
        core.configure(prepared)
        models=json.loads((Path(prepared['home'])/'smart2-models.json').read_text())['models']
        self.assertEqual(models,core.catalog())

    def test_profile_route_and_key_publish_as_one_atomic_snapshot(self):
        p=self.store.save_profile(api())
        private=json.loads(self.store.secret_path(p['id']).read_text())
        self.assertEqual(private['profile']['url'],'https://example.invalid/v1')
        self.assertEqual(private['key'],'SYNTHETIC_KEY')

    def test_running_instance_blocks_profile_and_key_edit(self):
        p=self.store.save_profile(api())
        locks=core.directory(self.store.root/'locks')
        with (locks/f"local-{p['id']}.lock").open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(core.UserError):
                self.store.save_profile(dict(p,url='https://changed.invalid/v1',api_key='NEW_TEST_KEY'))
        self.assertEqual(self.store.secret(p['id']),'SYNTHETIC_KEY')

    def test_failed_atomic_publish_preserves_both_original_route_and_key(self):
        p=self.store.save_profile(api())
        with patch('core.os.replace',side_effect=OSError('synthetic disk full')):
            with self.assertRaises(OSError):
                self.store.save_profile(dict(p,url='https://changed.invalid/v1',api_key='NEW_TEST_KEY'))
        self.assertEqual(self.store.profile(p['id'])['url'],'https://example.invalid/v1')
        self.assertEqual(self.store.secret(p['id']),'SYNTHETIC_KEY')

    def test_orphan_desktop_blocks_edit_without_a_supervisor_lock(self):
        p=self.store.save_profile(api())
        core.write_json(self.store.root/'jobs'/'test.json',{'id':p['id'],'storage':'local',
            'pid':os.getpid(),'pid_start':core.process_stamp(os.getpid()),'worker_pid':9999999})
        with self.assertRaises(core.UserError):
            self.store.save_profile(dict(p,api_key='NEW_TEST_KEY'))
        self.assertEqual(self.store.secret(p['id']),'SYNTHETIC_KEY')

    def test_recycled_pid_does_not_mark_profile_active(self):
        p=self.store.save_profile(api())
        core.write_json(self.store.root/'jobs'/'test.json',{'id':p['id'],'storage':'local',
            'pid':os.getpid(),'pid_start':'Thu Jan  1 00:00:00 1970'})
        self.assertFalse(self.store.running_profile(p['id']))


if __name__=='__main__': unittest.main()
