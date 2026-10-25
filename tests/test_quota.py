"""Quota reads use synthetic credentials/RPC; never a model or real account."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import quota


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = core.Store(self.root/'control', self.root/'external', mounted=lambda: True, community=False)
        self.profile = self.store.save_profile({'kind':'official', 'name':'Synthetic A'})
        self.ident = self.profile['id']
        self.auth = self.store.root/'accounts'/self.ident/'codex'/'auth.json'
        core.write_json(self.auth, {'auth_mode':'chatgpt', 'tokens':{
            'account_id':'synthetic-A', 'access_token':'SYNTHETIC_ACCESS',
            'refresh_token':'NEVER_COPY_REFRESH', 'id_token':'NEVER_COPY_ID'}})

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, behavior='normal'):
        script=self.root/'fake.py'
        script.write_text('''import json,sys,os,time
from pathlib import Path
behavior = '''+repr(behavior)+'''
assert os.environ['HOME'] != str(Path.home().parent/'real')
assert 'ROUTER_API_KEY' not in os.environ
for raw in sys.stdin:
 v=json.loads(raw);m=v['method'];i=v.get('id');p=v.get('params',{})
 assert 'NEVER_COPY' not in raw
 if m=='initialized':continue
 if m=='initialize':r={}
 elif m=='account/login/start':
  assert p=={'type':'chatgptAuthTokens','accessToken':'SYNTHETIC_ACCESS','chatgptAccountId':'synthetic-A'}
  r={'type':'chatgptAuthTokens'}
 elif m=='account/rateLimits/read':
  assert p=={'excludeResetCreditDetails':True}
  if behavior=='timeout':time.sleep(10)
  if behavior=='expired':
   print(json.dumps({'method':'account/chatgptAuthTokens/refresh','id':99,'params':{}}),flush=True);continue
  if behavior=='error':
   print(json.dumps({'id':i,'error':{'message':'401 SECRET_SERVER_BODY'}}),flush=True);continue
  r={'rateLimits':{'primary':{'usedPercent':25,'windowDurationMins':300,'resetsAt':2000000000}},'secret':'NEVER_PUBLISH'}
 else:raise Exception('unexpected method')
 print(json.dumps({'id':i,'result':r}),flush=True)
''')
        return [sys.executable, '-I', '-B', str(script)]

    def test_native_protocol_isolated_auth_unchanged_and_private_fields_excluded(self):
        before=self.auth.read_bytes(); stamp=self.auth.stat().st_mtime_ns
        result=quota.fetch(self.store,self.ident,command=self.fake())
        self.assertEqual(result['state'],'ready')
        self.assertEqual(result['buckets'][0]['windows'][0]['remaining_percent'],75)
        self.assertEqual(result['credential_revision'],quota.credential_revision(self.auth))
        self.assertNotIn('NEVER_',json.dumps(result))
        self.assertEqual(self.auth.read_bytes(),before)
        self.assertEqual(self.auth.stat().st_mtime_ns,stamp)
        self.assertFalse((self.auth.parent/'config.toml').exists())

    def test_multiple_buckets_preferred_and_missing_values_are_unknown(self):
        value={'rateLimits':{'primary':{'usedPercent':90}},'rateLimitsByLimitId':{
            'codex':{'primary':{'usedPercent':0,'windowDurationMins':300},
                     'secondary':{'usedPercent':100,'windowDurationMins':10080,'resetsAt':2000000000}},
            'model':{'limitName':'Model quota','primary':{'usedPercent':None},'secondary':None}}}
        rows=quota.normalize(value)
        self.assertEqual(rows[0]['id'],'codex')
        self.assertEqual([w['remaining_percent'] for w in rows[0]['windows']],[100,0])
        self.assertIsNone(rows[1]['windows'][0]['remaining_percent'])
        self.assertIsNone(rows[1]['windows'][0]['duration_minutes'])
        self.assertEqual(len(rows[1]['windows']),1)

    def test_invalid_numbers_are_never_shown_as_full_or_negative_quota(self):
        for value in [True,'5',float('nan'),float('inf')]:
            row=quota.normalize({'rateLimits':{'primary':{'usedPercent':value,'windowDurationMins':value,'resetsAt':value}}})[0]['windows'][0]
            self.assertIsNone(row['remaining_percent'])
            self.assertIsNone(row['duration_minutes'])
            self.assertIsNone(row['resets_at'])
        for value,expected in [(-10,100),(110,0)]:
            row=quota.normalize({'rateLimits':{'primary':{'usedPercent':value}}})[0]['windows'][0]
            self.assertEqual(row['remaining_percent'],expected)

    def test_weekly_only_and_empty_response(self):
        rows=quota.normalize({'rateLimits':{'primary':None,'secondary':{'usedPercent':6,'windowDurationMins':10080}}})
        self.assertEqual(len(rows[0]['windows']),1)
        self.assertEqual(rows[0]['windows'][0]['remaining_percent'],94)
        self.assertEqual(quota.normalize({}),[])

    def test_timeout_and_auth_errors_are_bounded_and_never_leak_payload(self):
        for behavior,expected in [('timeout','timeout'),('expired','auth_required'),('error','auth_required')]:
            started=time.monotonic()
            result=quota.fetch(self.store,self.ident,command=self.fake(behavior),timeout=.3)
            self.assertEqual(result['state'],expected)
            self.assertLess(time.monotonic()-started,4)
            self.assertNotIn('SECRET_SERVER_BODY',json.dumps(result))
            self.assertNotIn('SYNTHETIC_ACCESS',json.dumps(result))

    def test_credential_replacement_discards_result(self):
        old=quota.query
        def replaced(*args,**kwargs):
            result=old(*args,**kwargs)
            core.atomic_write(self.auth,self.auth.read_text()+' ')
            return result
        with patch.object(quota,'query',replaced):
            result=quota.fetch(self.store,self.ident,command=self.fake())
        self.assertEqual(result['state'],'changed')
        self.assertNotIn('buckets',result)

    def test_timeout_includes_full_stdin_pipe_and_child_is_reaped(self):
        data=json.loads(self.auth.read_text());data['tokens']['access_token']='x'*65536
        core.write_json(self.auth,data)
        child=self.root/'blocked.py'
        child.write_text('''import sys,json,time
v=json.loads(sys.stdin.readline())
print(json.dumps({'id':v['id'],'result':{}}),flush=True)
time.sleep(10)
''')
        started=time.monotonic()
        value=quota.fetch(self.store,self.ident,command=[sys.executable,'-I',str(child)],timeout=.2)
        self.assertEqual(value['state'],'timeout')
        self.assertLess(time.monotonic()-started,2)

    def test_missing_symlink_or_malformed_credentials_never_start_query(self):
        for text in ['[]','{}','{"tokens": []}','{"auth_mode":"apikey"}']:
            core.atomic_write(self.auth,text)
            with patch.object(quota,'query') as q:
                result=quota.fetch(self.store,self.ident)
                q.assert_not_called()
            self.assertEqual(result['state'],'auth_required')
        self.auth.unlink();self.auth.symlink_to(self.root/'outside')
        self.assertIsNone(quota.credential_revision(self.auth))
        with patch.object(quota,'query') as q:
            self.assertEqual(quota.fetch(self.store,self.ident)['state'],'auth_required');q.assert_not_called()

    def test_router_excluded_and_same_account_duplicate_not_dispatched(self):
        router=self.store.save_profile({'kind':'api','name':'Router','url':'https://example.invalid/v1','protocol':'responses',
            'api_key':'SYNTHETIC','models':[{'alias':'gpt-6-luna','upstream':'synthetic'}]})
        with patch.object(quota,'query') as q:
            with self.assertRaises(core.UserError):quota.fetch(self.store,router['id'])
            q.assert_not_called()
        from accounts import take_lock
        with take_lock(core.directory(self.store.root/'locks')/f'quota-{self.ident}.lock'):
            result=quota.fetch(self.store,self.ident,command=self.fake())
        self.assertEqual(result['state'],'busy')

    def test_read_allowed_during_desktop_lease_but_login_and_removal_excluded(self):
        from accounts import account_lock_paths,take_lock
        with take_lock(account_lock_paths(self.store,self.ident)[0]):
            self.assertEqual(quota.fetch(self.store,self.ident,command=self.fake())['state'],'ready')
        with self.store.operation_guard(self.ident):
            self.assertEqual(quota.fetch(self.store,self.ident,command=self.fake())['state'],'busy')


if __name__=='__main__':unittest.main()
