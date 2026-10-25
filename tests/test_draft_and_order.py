import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
import probe
import profile_order
from model_metadata import draft_connection

class DraftAndOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=core.Store(self.root/'control',self.root/'external',mounted=lambda:True, community=False)
        self.profile=self.store.save_profile(dict(name='Synthetic',kind='api',url='https://example.com/v1',protocol='responses',
            api_key='SYNTHETIC_KEY',models=[dict(alias='gpt-6-luna',upstream='old',context=128000,reasoning='off')]))
    def tearDown(self):self.tmp.cleanup()

    def test_draft_probe_uses_unsaved_model_and_key_without_saving(self):
        before=self.store.secret_path(self.profile['id']).read_bytes()
        draft=copy.deepcopy(self.profile);draft['models'][0]['upstream']='new';draft['api_key']='DRAFT_KEY';draft['key_url']=draft['url']
        seen=[]
        def attempt(capsule,*args):
            seen.append(capsule)
            return {'outcome':'success','message':'OK','input_tokens':1,'output_tokens':1}
        with patch('probe.attempt',side_effect=attempt):value=probe.probe_draft(self.store,draft,'gpt-6-luna')
        self.assertEqual(value['outcome'],'success');self.assertEqual(seen[0]['key'],'DRAFT_KEY')
        self.assertEqual(seen[0]['profile']['models'][0]['upstream'],'new')
        self.assertEqual(self.store.secret_path(self.profile['id']).read_bytes(),before)
        self.assertNotIn('DRAFT_KEY',json.dumps(value))

    def test_draft_connection_validation_holds_lock_through_request(self):
        import model_metadata
        original=model_metadata.draft_connection
        def concurrent_remove(*args):
            result=original(*args)
            with self.assertRaises(core.UserError):self.store.remove_profile(self.profile['id'])
            return result
        with patch('model_metadata.draft_connection',side_effect=concurrent_remove), patch('probe.attempt',return_value={'outcome':'success','message':'OK'}):
            self.assertEqual(probe.probe_draft(self.store,self.profile,'gpt-6-luna')['outcome'],'success')
        self.assertTrue(self.store.secret_path(self.profile['id']).exists())

    def test_saved_key_cannot_silently_go_to_changed_url(self):
        draft=dict(self.profile,url='https://other.example/v1')
        with self.assertRaises(core.UserError):draft_connection(self.store,draft,'')
        with self.assertRaises(core.UserError):draft_connection(self.store,draft,'EXPLICIT_KEY')
        draft['key_url']=draft['url']
        self.assertEqual(draft_connection(self.store,draft,'EXPLICIT_KEY')[1],'EXPLICIT_KEY')
        self.store.save_profile(dict(self.profile,name='Changed'))
        with self.assertRaises(core.UserError):draft_connection(self.store,self.profile,'')

    def test_explicit_save_authorizes_new_endpoint_for_existing_key(self):
        # Save is the user's explicit decision to retarget this API; the UI says
        # the existing Key will be used at the saved address. Unsaved drafts above
        # have a stricter rule because fetching starts without a Save click.
        saved=self.store.save_profile(dict(self.profile,url='https://new.example/v1',api_key=''))
        url,key=draft_connection(self.store,saved,'')
        self.assertEqual(url,'https://new.example/v1');self.assertEqual(key,'SYNTHETIC_KEY')

    def test_order_persists_rejects_stale_and_never_changes_profile_files(self):
        other=self.store.save_profile(dict(name='Official',kind='official'))
        before={p.name:p.read_bytes() for p in (self.store.root/'profiles').glob('*.json')}
        profiles=self.store.profiles();rev=profile_order.revision(profiles)
        ids=list(reversed([p['id'] for p in profiles]))
        profile_order.save(self.store,ids,rev)
        self.assertEqual([p['id'] for p in self.store.profiles()],ids)
        with self.assertRaises(core.UserError):profile_order.save(self.store,ids,rev)
        with self.assertRaises(core.UserError):profile_order.save(self.store,[ids[0],ids[0]],profile_order.revision(self.store.profiles()))
        self.assertEqual({p.name:p.read_bytes() for p in (self.store.root/'profiles').glob('*.json')},before)
        self.store.remove_profile(other['id'])
        self.assertEqual([p['id'] for p in self.store.profiles()],[self.profile['id']])
        self.store.restore_profile(other['id'])
        self.assertEqual([p['id'] for p in self.store.profiles()],ids)

if __name__=='__main__':unittest.main()
