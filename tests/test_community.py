"""Public edition never inherits developer configuration; use only fake secrets."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core


class FakeKeychain:
    def __init__(self):self.values={};self.denied=False
    def put(self,ref,value):
        if self.denied:raise core.UserError('Keychain denied')
        self.values[ref]=value
    def get(self,ref):
        if self.denied or ref not in self.values:raise core.UserError('Keychain unavailable')
        return self.values[ref]
    def delete(self,ref):
        if self.denied:raise core.UserError('Keychain denied')
        self.values.pop(ref,None)


def api():
    return dict(kind='api',name='Example Router',url='https://example.invalid/v1',protocol='responses',
                api_key='SYNTHETIC_COMMUNITY_KEY',models=[dict(alias='gpt-6-luna',upstream='example/model',context=128000,reasoning='off',effort='low')])


class CommunityStorage(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.vault=FakeKeychain()
        self.store=core.Store(self.root/'control',self.root/'external',mounted=lambda:True,community=True,keychain=self.vault)
    def tearDown(self):self.tmp.cleanup()
    def test_fresh_root_has_no_api_account_path_or_history(self):
        from main import status
        state=status(self.store)
        for key in ['profiles','removed_profiles','storages','removed_storages','jobs']:
            self.assertEqual(state[key],[])
        self.assertEqual(state['accounts'],{})
        self.assertFalse((self.root/'external').exists())
        self.assertFalse((self.root/'control/stores').exists())
    def test_new_paths_work_without_seeded_originals_and_last_can_be_removed(self):
        target=self.root/'user-history';snap=self.store.storage_snapshot()
        row=dict(id='a'*32,name='User choice',path=str(target),enabled=True)
        saved=self.store.save_storages([row],snap['revision'])
        self.assertFalse(target.exists())
        self.store.initialize_storage(row['id']);self.assertEqual(self.store.select_storage('auto'),row['id'])
        self.store.registry.remove(row['id'],saved['revision'])
        self.assertEqual(self.store.storage_snapshot()['storages'],[])
        self.assertTrue(target.is_dir())
    def test_registering_history_never_imports_old_login_files(self):
        from accounts import account_home
        p=self.store.save_profile(dict(id='f'*32,kind='official',name='New login'))
        history=self.root/'existing-history';legacy=history/'instances'/p['id']/'codex'
        legacy.mkdir(parents=True);(legacy/'auth.json').write_text('{"synthetic":"old login"}')
        self.store.save_storages([dict(id='a'*32,name='Existing history',path=str(history),enabled=True)],0)
        home=account_home(self.store,p['id'])
        self.assertFalse((home/'auth.json').exists())
        self.assertEqual((legacy/'auth.json').read_text(),'{"synthetic":"old login"}')
    def test_secret_saved_in_keychain_not_json_and_survives_blank_edit(self):
        p=self.store.save_profile(api());raw=self.store.secret_path(p['id']).read_text()
        self.assertNotIn('SYNTHETIC_COMMUNITY_KEY',raw);self.assertNotIn('"key":',raw)
        self.assertTrue(p['has_key']);self.assertEqual(self.store.secret(p['id']),'SYNTHETIC_COMMUNITY_KEY')
        ref=json.loads(raw)['key_ref']
        edited=self.store.save_profile(dict(p,name='Renamed',api_key=''))
        self.assertEqual(json.loads(self.store.secret_path(p['id']).read_text())['key_ref'],ref)
        self.assertEqual(self.store.secret(edited['id']),'SYNTHETIC_COMMUNITY_KEY')
    def test_denial_does_not_publish_metadata_or_fallback_to_plaintext(self):
        self.vault.denied=True
        with self.assertRaises(core.UserError):self.store.save_profile(api())
        self.assertEqual(self.store.profiles(),[])
    def test_profile_write_failure_preserves_previous_url_and_secret(self):
        p=self.store.save_profile(api());before=self.store.secret_path(p['id']).read_bytes()
        write=core.write_json
        def fail_profile(path,value):
            if path==self.store.secret_path(p['id']):raise OSError('Synthetic disk failure')
            return write(path,value)
        with patch('core.write_json',side_effect=fail_profile):
            with self.assertRaises(OSError):self.store.save_profile(dict(p,url='https://changed.invalid/v1',api_key='SYNTHETIC_NEW'))
        self.assertEqual(self.store.secret_path(p['id']).read_bytes(),before)
        self.assertEqual(self.store.secret(p['id']),'SYNTHETIC_COMMUNITY_KEY')
        self.assertNotIn('SYNTHETIC_NEW',self.vault.values.values())
    def test_uncertain_keychain_write_remains_tracked_for_cleanup(self):
        data=api();data['id']='b'*32
        def uncertain(ref,value):
            self.vault.values[ref]=value
            raise core.UserError('Synthetic lost acknowledgement')
        with patch.object(self.vault,'put',side_effect=uncertain),patch.object(self.vault,'delete',side_effect=core.UserError('Synthetic denial')):
            with self.assertRaises(core.UserError):self.store.save_profile(data)
        owned=json.loads((self.store.root/'keychain-refs'/('b'*32+'.json')).read_text())['refs']
        self.assertTrue(set(self.vault.values).issubset(set(owned)))
        self.assertEqual(self.store.profiles(),[])
        self.assertTrue(self.vault.values)
        retry=api();retry['id']='c'*32;self.store.save_profile(retry)
        self.assertFalse((self.store.root/'keychain-refs'/('b'*32+'.json')).exists())
        self.assertTrue(all(ref.startswith('chc_'+('c'*32)+'_') for ref in self.vault.values))
    def test_publication_and_cleanup_failure_keep_old_route_and_owned_pending_ref(self):
        p=self.store.save_profile(api());before=self.store.secret_path(p['id']).read_bytes()
        write=core.write_json
        def fail_profile(path,value):
            if path==self.store.secret_path(p['id']):raise OSError('Synthetic full disk')
            return write(path,value)
        with patch('core.write_json',side_effect=fail_profile),patch.object(self.vault,'delete',side_effect=core.UserError('Synthetic denial')):
            with self.assertRaises(core.UserError):self.store.save_profile(dict(p,api_key='SYNTHETIC_NEW',url='https://changed.invalid/v1'))
        self.assertEqual(self.store.secret_path(p['id']).read_bytes(),before)
        self.assertEqual(self.store.secret(p['id']),'SYNTHETIC_COMMUNITY_KEY')
        owned=self.store.key_refs(p['id']);self.assertTrue(set(self.vault.values).issubset(set(owned)))
        self.assertIn('SYNTHETIC_NEW',self.vault.values.values())
    def test_corrupted_active_reference_cannot_delete_another_profile_key(self):
        p=self.store.save_profile(api());path=self.store.secret_path(p['id'])
        value=json.loads(path.read_text());value['key_ref']='chc_'+('d'*32)+'_'+('e'*32);core.write_json(path,value)
        before=dict(self.vault.values)
        with self.assertRaises(core.UserError):self.store.save_profile(dict(p,kind='official'))
        self.assertEqual(self.vault.values,before)
    def test_removed_profile_keeps_key_until_confirmed_purge(self):
        p=self.store.save_profile(api());self.store.remove_profile(p['id'],p['revision'])
        removed=self.store.removed_profiles()[0];self.assertTrue(removed['has_key'])
        self.store.restore_profile(removed['id'],removed['revision']);self.assertEqual(self.store.secret(p['id']),'SYNTHETIC_COMMUNITY_KEY')
        p=self.store.public({'id':p['id']});self.store.remove_profile(p['id'],p['revision'])
        removed=self.store.removed_profiles()[0];self.store.purge_profile(p['id'],removed['revision'])
        self.assertEqual(self.vault.values,{})


class RuntimeDiscovery(unittest.TestCase):
    def test_public_settings_ignore_private_machine_defaults(self):
        from distribution import settings
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'edition.json').write_text(json.dumps({'edition':'community','app_name':'Codex Harbor Community','data_directory':'Codex Harbor Community','version':'0.9.0'}))
            (root/'machine-defaults.json').write_text(json.dumps({'external':'/private/developer-only'}))
            value=settings(root)
            self.assertEqual(value['machine'],{});self.assertTrue(value['community'])
            self.assertEqual(value['data_directory'],'Codex Harbor Community')
    def test_app_discovery_requires_executable_and_backend(self):
        from distribution import discover_app
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);bad=root/'Bad.app';good=root/'Good.app'
            for app in [bad,good]:
                (app/'Contents/MacOS').mkdir(parents=True)
                (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'Vendor'}))
                (app/'Contents/MacOS/Vendor').touch()
            cli=good/'Contents/Resources/codex';cli.parent.mkdir();cli.touch();cli.chmod(0o700)
            (good/'Contents/MacOS/Vendor').chmod(0o700)
            app,backend,executable=discover_app([bad,good])
            self.assertEqual(app,good);self.assertEqual(backend,cli);self.assertEqual(executable,good/'Contents/MacOS/Vendor')


if __name__=='__main__':unittest.main()
