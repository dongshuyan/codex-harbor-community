"""A fake app-server verifies the browser-login bridge without real accounts."""
import json
import base64
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core


class BrowserLogin(unittest.TestCase):
    def test_credential_replacement_cannot_label_new_quota_with_previous_email(self):
        import login
        from quota import credential_revision
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            profile=store.save_profile({'kind':'official','name':'Synthetic'})
            auth=store.root/'accounts'/profile['id']/'codex'/'auth.json'
            def write_auth(email):
                payload=base64.urlsafe_b64encode(json.dumps({'email':email}).encode()).decode().rstrip('=')
                core.write_json(auth,{'auth_mode':'chatgpt','tokens':{'id_token':'e30.'+payload+'.synthetic'}})
            write_auth('a@example.invalid')
            read_email=login.local_account_email
            def replace_after_read(path):
                email=read_email(path)
                write_auth('b@example.invalid')
                return email
            with patch.object(login,'local_account_email',replace_after_read):
                value=login.snapshot(store,profile['id'])
            self.assertNotIn('account_email',value)
            self.assertIsNone(value['credential_revision'])
            next_value=login.snapshot(store,profile['id'])
            self.assertEqual(next_value['account_email'],'b@example.invalid')
            self.assertEqual(next_value['credential_revision'],credential_revision(auth))

    def test_display_email_is_read_only_and_never_returns_tokens(self):
        import login
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            p=store.save_profile({'kind':'official','name':'Synthetic'})
            auth=store.root/'accounts'/p['id']/'codex'/'auth.json'
            def write_auth(claims):
                payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
                core.write_json(auth,{'auth_mode':'chatgpt','tokens':{'id_token':'e30.'+payload+'.synthetic',
                    'access_token':'NEVER_DISPLAY_ACCESS','refresh_token':'NEVER_DISPLAY_REFRESH'}})
            core.write_json(store.root/'login'/f"{p['id']}.json",{'state':'success','masked_account':'st***@example.invalid'})
            for claims in [{'email':'complete.address@example.invalid'},
                           {'https://api.openai.com/profile':{'email':'another.account@example.invalid'}}]:
                write_auth(claims)
                before=auth.read_bytes();stamp=auth.stat().st_mtime_ns
                value=login.snapshot(store,p['id'])
                expected=claims.get('email') or claims['https://api.openai.com/profile']['email']
                self.assertEqual(value.get('account_email'),expected)
                self.assertNotIn('NEVER_DISPLAY',json.dumps(value))
                self.assertNotIn('id_token',json.dumps(value))
                self.assertEqual(auth.read_bytes(),before)
                self.assertEqual(auth.stat().st_mtime_ns,stamp)
            auth.unlink()
            self.assertNotIn('account_email',login.snapshot(store,p['id']))

    def test_display_email_ignores_unsafe_or_invalid_auth_without_breaking_status(self):
        import login
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            p=store.save_profile({'kind':'official','name':'Synthetic'})
            auth=store.root/'accounts'/p['id']/'codex'/'auth.json'
            for data in ['{partial','[]','['*1200+']'*1200,json.dumps({'tokens':[]}),json.dumps({'tokens':{'id_token':'bad'}})]:
                core.atomic_write(auth,data)
                value=login.snapshot(store,p['id'])
                self.assertTrue(value['credential_present'])
                self.assertNotIn('account_email',value)
            for email in ['wrong', 'x@example.invalid\nother', 'x\u202e@example.invalid', 'x'*400+'@example.invalid']:
                payload=base64.urlsafe_b64encode(json.dumps({'email':email}).encode()).decode().rstrip('=')
                core.write_json(auth,{'tokens':{'id_token':'e30.'+payload+'.synthetic'}})
                self.assertNotIn('account_email',login.snapshot(store,p['id']))
            payload=base64.urlsafe_b64encode(json.dumps({'email':'stale@example.invalid'}).encode()).decode().rstrip('=')
            core.write_json(auth,{'auth_mode':'apikey','tokens':{'id_token':'e30.'+payload+'.synthetic'}})
            self.assertNotIn('account_email',login.snapshot(store,p['id']))
            auth.unlink();auth.symlink_to(root/'elsewhere')
            self.assertFalse(login.snapshot(store,p['id'])['credential_present'])

    def test_flow_publishes_only_auth_url_and_masked_account(self):
        import login
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            p=store.save_profile({'kind':'official','name':'Synthetic'})
            fake=root/'fake.py'
            fake.write_text('''import json,sys
for raw in sys.stdin:
 v=json.loads(raw);i=v.get('id');m=v['method']
 if m=='initialize': r={}
 elif m=='account/login/start': r={'type':'chatgpt','authUrl':'https://auth.openai.com/authorize?state=SYNTHETIC','loginId':'fake'}
 elif m=='account/read': r={'account':{'type':'chatgpt','email':'sample@example.invalid','accessToken':'NEVER_DISPLAY'}}
 else: continue
 print(json.dumps({'id':i,'result':r}),flush=True)
 if m=='account/login/start': print(json.dumps({'method':'account/login/completed','params':{'loginId':'fake','success':True}}),flush=True)
''')
            shown=[]
            close=login.RPC.close
            cleanup_checked=[]
            def guarded_close(rpc):
                with self.assertRaises(core.UserError):store.remove_profile(p['id'])
                cleanup_checked.append(True)
                close(rpc)
            with patch.object(login.RPC,'close',guarded_close):
                result=login.run_flow(store,p['id'],shown.append,command=[sys.executable,'-I',str(fake)],timeout=3)
            self.assertEqual(cleanup_checked,[True])
            self.assertEqual(result['state'],'success')
            self.assertEqual(result['masked_account'],'sa***@example.invalid')
            self.assertEqual(len(shown),1)
            safe=json.dumps(login.snapshot(store,p['id']))
            self.assertNotIn('NEVER_DISPLAY',safe)
            self.assertNotIn('sample@',safe)
            self.assertNotIn('authorize?',safe)

    def test_auth_url_must_be_official_and_no_userinfo(self):
        import login
        for url in ['https://evil.invalid/login','http://auth.openai.com/login',
                    'https://key@auth.openai.com/login','https://auth.openai.com.evil.invalid/login']:
            with self.assertRaises(core.UserError):login.check_url(url)

    def test_missing_worker_is_not_shown_as_waiting_forever(self):
        import login
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            p=store.save_profile({'kind':'official','name':'Synthetic'})
            core.write_json(store.root/'login'/f"{p['id']}.json",{'state':'waiting','pid':9999999,'pid_start':'synthetic'})
            self.assertEqual(login.snapshot(store,p['id'])['state'],'error')

    def test_current_credential_presence_is_separate_from_last_login_result(self):
        import login
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            p=store.save_profile({'kind':'official','name':'Synthetic'})
            auth=store.root/'accounts'/p['id']/'codex'/'auth.json'
            core.atomic_write(auth,'SYNTHETIC_AUTH')
            state=store.root/'login'/f"{p['id']}.json"
            core.write_json(state,{'state':'cancelled','message':'已取消登录。'})
            value=login.snapshot(store,p['id'])
            self.assertTrue(value['credential_present'])
            self.assertEqual(value['state'],'cancelled')
            auth.unlink()
            core.write_json(state,{'state':'success','masked_account':'sa***@example.invalid'})
            value=login.snapshot(store,p['id'])
            self.assertFalse(value['credential_present'])
            self.assertNotIn('masked_account',value)

    def test_competing_login_cannot_overwrite_active_flow_status(self):
        import login
        from accounts import take_lock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            p=store.save_profile({'kind':'official','name':'Synthetic'})
            state=store.root/'login'/f"{p['id']}.json"
            core.write_json(state,{'state':'waiting','message':'SYNTHETIC_FIRST_FLOW'})
            original=state.read_bytes();shown=[]
            with take_lock(core.directory(store.root/'locks')/'browser-login.lock'):
                result=login.run_flow(store,p['id'],shown.append,command=['/usr/bin/false'],timeout=1)
            self.assertEqual(result['state'],'error')
            self.assertEqual(state.read_bytes(),original)
            self.assertEqual(shown,[])

    def test_failed_flow_publishes_error_before_releasing_account_lock(self):
        import login
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=core.Store(root/'control',root/'external',mounted=lambda:True, community=False)
            p=store.save_profile({'kind':'official','name':'Synthetic'})
            original=login.write_json;checked=[]
            def write_state(path,value):
                if value.get('state')=='error':
                    with self.assertRaises(core.UserError):store.remove_profile(p['id'])
                    checked.append(True)
                original(path,value)
            with patch.object(login,'write_json',write_state):
                result=login.run_flow(store,p['id'],lambda _:None,command=['/usr/bin/false'],timeout=1)
            self.assertEqual(result['state'],'error')
            self.assertEqual(checked,[True])


if __name__=='__main__':unittest.main()
