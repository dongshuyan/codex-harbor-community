"""Harbor contracts: synthetic profiles and temporary directories only."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core


def api():
    return dict(name='Synthetic', kind='api', protocol='responses',
                url='https://example.invalid/v1', api_key='SYNTHETIC_KEY', models=[
                    dict(alias='gpt-6-luna', upstream='test/model', effort='low',
                         context=128000, reasoning='off')])


class HarborFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.store = core.Store(self.root/'control', self.root/'external', mounted=lambda: True, community=False)
        self.store.initialize_storage('local')

    def tearDown(self):
        self.tmp.cleanup()

    def test_declared_levels_override_alias_and_reject_unsupported_effort(self):
        data = api()
        data['models'][0].update(reasoning='standard', levels=['low','max'], effort='max')
        p = self.store.save_profile(data)
        prepared = self.store.prepare(p['id'], 'local')
        core.configure(prepared, 'http://127.0.0.1:9/v1', 'SYNTHETIC')
        m = json.loads((Path(prepared['home'])/'smart2-models.json').read_text())['models'][0]
        self.assertEqual([x['effort'] for x in m['supported_reasoning_levels']], ['low','max'])
        self.assertEqual(core.map_request(p, {'model':'gpt-6-luna','reasoning':{'effort':'max'}})['reasoning'], {'effort':'max'})
        with self.assertRaises(core.UserError):
            core.map_request(p, {'model':'gpt-6-luna','reasoning':{'effort':'high'}})

    def test_unknown_capability_hides_efforts_and_omits_parameters(self):
        p = self.store.save_profile(api())
        prepared = self.store.prepare(p['id'], 'local')
        core.configure(prepared, 'http://127.0.0.1:9/v1', 'SYNTHETIC')
        m = json.loads((Path(prepared['home'])/'smart2-models.json').read_text())['models'][0]
        self.assertEqual(m['supported_reasoning_levels'], [])
        self.assertIsNone(m['default_reasoning_level'])
        body = core.map_request(p, {'model':'gpt-6-luna','reasoning':{'effort':'ultra','summary':'auto'}})
        self.assertNotIn('reasoning', body)

    def test_custom_field_values_are_explicit_and_cannot_override_model_or_auth(self):
        data = api()
        data['models'][0].update(reasoning='custom', levels=['low','high'],
                                 effort_field='reasoning.level', effort_values={'low':'shallow','high':'deep'})
        p = self.store.save_profile(data)
        body = core.map_request(p, {'model':'gpt-6-luna','reasoning':{'effort':'high','summary':'auto'}})
        self.assertEqual(body['reasoning'], {'level':'deep'})
        for field in ['model','Authorization','input','__proto__.effort','extra_body.model','reasoning.effort.bad']:
            bad = copy.deepcopy(data); bad['models'][0]['effort_field'] = field
            with self.assertRaises(core.UserError): self.store.save_profile(bad)
        bad = copy.deepcopy(data); bad['models'][0]['levels'] = ['low','ultra']
        with self.assertRaises(core.UserError): self.store.save_profile(bad)

    def test_stale_profile_editor_cannot_overwrite_newer_save(self):
        p = self.store.save_profile(api())
        changed = self.store.save_profile(dict(p, name='Fresh', api_key=''))
        self.assertNotEqual(p['revision'], changed['revision'])
        with self.assertRaises(core.UserError): self.store.save_profile(dict(p, name='Stale', api_key=''))
        self.assertEqual(self.store.profile(p['id'])['name'], 'Fresh')

    def test_public_profile_and_revision_always_share_one_snapshot(self):
        p=self.store.save_profile(api());old=self.store.profile(p['id'])
        newer=self.store.save_profile(dict(p,name='Fresh',api_key=''))
        public=self.store.public(old)
        self.assertEqual((public['name'],public['revision']),('Fresh',newer['revision']))

    def test_test_external_cannot_bypass_physical_disk_verification(self):
        result=subprocess.run([sys.executable,'-I','-B',str(Path(core.__file__).with_name('main.py')),
            '--root',str(self.root/'other-control'),'--test-external','/Volumes/harbor-missing-disk/test'],
            input='{"action":"status"}',text=True,capture_output=True,check=True)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertFalse((self.root/'other-control').exists())

    def test_initialize_action_rejects_stale_path_without_creating_either_directory(self):
        original = self.store.storage_snapshot()
        first = self.root/'first'
        second = self.root/'second'
        next(s for s in original['storages'] if s['id'] == 'local')['path'] = str(first)
        confirmed = self.store.save_storages(original['storages'], original['revision'])
        changed = copy.deepcopy(confirmed['storages'])
        next(s for s in changed if s['id'] == 'local')['path'] = str(second)
        self.store.save_storages(changed, confirmed['revision'])
        result = subprocess.run([sys.executable, '-I', '-B', str(Path(core.__file__).with_name('main.py')),
            '--root', str(self.store.root), '--test-external', str(self.store.external)],
            input=json.dumps({'action':'initialize_storage', 'id':'local',
                              'revision':confirmed['revision'], 'path':str(first)}),
            text=True, capture_output=True, check=True)
        self.assertFalse(json.loads(result.stdout)['ok'])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_worker_preserves_physical_volume_verification(self):
        import launcher
        p=self.store.save_profile(api())
        self.store.external=Path('/Volumes/harbor-synthetic-disk/data')
        with patch.object(launcher.subprocess,'Popen') as popen, patch.object(launcher.time,'sleep'):
            popen.return_value.poll.return_value=0
            launcher.launch_request(self.store,p['id'],'local')
        args=popen.call_args.args[0]
        self.assertIn('--external',args)
        self.assertNotIn('--test-external',args)

    def test_purge_refuses_wrong_or_offline_disk_before_any_deletion(self):
        self.store.initialize_storage('external')
        p=self.store.save_profile(api())
        auth=self.store.external/'instances'/p['id']/'codex'/'auth.json'
        core.atomic_write(auth,'SYNTHETIC_AUTH')
        self.store.remove_profile(p['id']);self.store.mounted=lambda:False
        with self.assertRaises(core.UserError):self.store.purge_profile(p['id'],self.store.removed_profiles()[0]['revision'])
        self.assertEqual(auth.read_text(),'SYNTHETIC_AUTH')
        self.assertEqual(len(self.store.removed_profiles()),1)

    def test_generated_test_permissions_deny_old_launcher_and_removed_keys(self):
        p=self.store.save_profile(api());prepared=self.store.prepare(p['id'],'local')
        prepared['access_mode']='readonly'
        core.configure(prepared,'http://127.0.0.1:9/v1','SYNTHETIC')
        text=(Path(prepared['home'])/'config.toml').read_text()
        for path in [self.store.root/'removed-profiles',Path.home()/'Library/Application Support/Codex Smart-2',
                     core.VOLUME/'Harnesses/CodexSmart2']:
            self.assertIn(json.dumps(str(path))+' = "deny"',text)

    def test_workspace_mode_preserves_credential_denials_and_native_approvals(self):
        import tomllib
        p=self.store.save_profile(api());prepared=self.store.prepare(p['id'],'local')
        prepared['access_mode']='workspace'
        core.configure(prepared,'http://127.0.0.1:9/v1','SYNTHETIC')
        value=tomllib.loads((Path(prepared['home'])/'config.toml').read_text())
        self.assertEqual(value['default_permissions'],'harbor-workspace')
        self.assertEqual(value['approval_policy'],'on-request')
        permissions=value['permissions']['harbor-workspace']
        self.assertEqual(permissions['extends'],':workspace')
        self.assertEqual(permissions['filesystem'][str(self.store.root/'profiles')],'deny')
        self.assertEqual(permissions['filesystem'][str(Path.home()/'.codex')],'deny')
        self.assertFalse(permissions['network']['enabled'])

    def test_invalid_access_mode_is_rejected_before_rewriting_configuration(self):
        p=self.store.save_profile(api());prepared=self.store.prepare(p['id'],'local')
        core.configure(prepared,'http://127.0.0.1:9/v1','SYNTHETIC')
        path=Path(prepared['home'])/'config.toml';before=path.read_bytes()
        prepared['access_mode']='full-access'
        with self.assertRaises(core.UserError):core.configure(prepared,'http://127.0.0.1:9/v1','SYNTHETIC')
        self.assertEqual(path.read_bytes(),before)

    def test_launch_pins_explicit_access_mode_and_rejects_unknown_mode(self):
        import launcher
        p=self.store.save_profile(api())
        with patch.object(launcher.subprocess,'Popen') as popen,patch.object(launcher.time,'sleep'):
            popen.return_value.poll.return_value=0
            job=launcher.launch_request(self.store,p['id'],'local',access_mode='workspace')
            self.assertEqual(job['access_mode'],'workspace')
            self.assertIn('--worker',popen.call_args.args[0])
            before=set((self.store.root/'jobs').iterdir())
            with self.assertRaises(core.UserError):launcher.launch_request(self.store,p['id'],'local',access_mode='full-access')
            self.assertEqual(set((self.store.root/'jobs').iterdir()),before)

    def test_storage_remove_restore_purge_only_changes_registration(self):
        sentinel = self.store.root/'stores/local/history.txt'; sentinel.write_text('SYNTHETIC_HISTORY')
        rev = self.store.storage_snapshot()['revision']
        self.store.registry.remove('local', rev)
        self.assertNotIn('local', [s['id'] for s in self.store.registry.entries()])
        self.assertEqual(self.store.storage_snapshot()['removed_storages'][0]['id'], 'local')
        self.store.registry.restore('local', self.store.storage_snapshot()['revision'])
        self.assertEqual(self.store.select_storage('local'), 'local')
        self.store.registry.remove('local', self.store.storage_snapshot()['revision'])
        self.store.registry.purge('local', self.store.storage_snapshot()['revision'])
        self.assertEqual(self.store.storage_snapshot()['removed_storages'], [])
        self.assertEqual(sentinel.read_text(), 'SYNTHETIC_HISTORY')

    def test_profile_purge_requires_removed_entry_and_preserves_history(self):
        p = self.store.save_profile(api())
        prepared = self.store.prepare(p['id'], 'local')
        sentinel = Path(prepared['history'])/'sessions/synthetic.jsonl'; sentinel.write_text('SYNTHETIC_HISTORY')
        with self.assertRaises(core.UserError): self.store.purge_profile(p['id'],p['revision'])
        self.store.remove_profile(p['id'])
        self.store.purge_profile(p['id'],self.store.removed_profiles()[0]['revision'])
        self.assertEqual(self.store.removed_profiles(), [])
        self.assertEqual(sentinel.read_text(), 'SYNTHETIC_HISTORY')
        with self.assertRaises(core.UserError): self.store.save_profile(dict(p, api_key='NEW_SYNTHETIC'))

    def test_stale_removal_confirmation_cannot_purge_a_recreated_archive(self):
        p=self.store.save_profile(api())
        self.store.remove_profile(p['id'])
        confirmed=self.store.removed_profiles()[0]
        self.store.restore_profile(p['id'])
        # Even restoring and removing without a config edit invalidates confirmation.
        self.store.remove_profile(p['id'])
        with self.assertRaises(core.UserError):self.store.purge_profile(p['id'],confirmed['revision'])
        self.assertEqual(len(self.store.removed_profiles()),1)
        self.assertFalse((self.store.root/'purged-profiles'/p['id']).exists())


if __name__ == '__main__': unittest.main()
