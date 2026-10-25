import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import os
import signal
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
from accounts import account_lock_paths, take_lock


class Accounts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name).resolve()
        self.store=core.Store(self.root/'control',self.root/'external',mounted=lambda:True, community=False)
        self.store.initialize_storage('local');self.store.initialize_storage('external')
        self.a=self.store.save_profile({'name':'合成 A','kind':'official'})
        self.b=self.store.save_profile({'name':'合成 B','kind':'official'})

    def tearDown(self): self.tmp.cleanup()

    def exited_legacy(self):
        path=self.store.root/'jobs'/'legacy.json'
        core.write_json(path,{'id':self.a['id'],'pid':99999999,'pid_start':'synthetic','status':'exited'})
        os.utime(path,(time.time()-120,time.time()-120))

    def test_one_auth_home_across_storage_distinct_accounts(self):
        a=self.store.prepare(self.a['id'],'local')
        auth=Path(a['home'])/'auth.json';core.write_json(auth,{'synthetic':'A'})
        original=auth.read_bytes()
        ext=self.store.prepare(self.a['id'],'external')
        b=self.store.prepare(self.b['id'],'external')
        self.assertEqual(a['home'],ext['home'])
        self.assertNotEqual(a['history'],ext['history'])
        self.assertNotEqual(ext['home'],b['home'])
        self.assertEqual(auth.read_bytes(),original)
        self.assertEqual((Path(ext['home'])/'sessions').resolve(),Path(ext['history'])/'sessions')

    def test_legacy_import_once_and_logout_never_reimports(self):
        legacy=self.store.root/'stores/local/instances'/self.a['id']/'codex/auth.json'
        core.write_json(legacy,{'synthetic':'A'})
        self.exited_legacy()
        a=self.store.prepare(self.a['id'],'local')
        auth=Path(a['home'])/'auth.json'
        self.assertEqual(auth.read_bytes(),legacy.read_bytes())
        auth.unlink()  # native logout semantics
        ext=self.store.prepare(self.a['id'],'external')
        self.assertFalse((Path(ext['home'])/'auth.json').exists())
        self.assertTrue(legacy.exists())

    def test_conflicting_legacy_accounts_fail_without_overwrite(self):
        for base,marker in [(self.store.root/'stores/local','A'),(self.store.external,'B')]:
            core.write_json(base/'instances'/self.a['id']/'codex/auth.json',{'synthetic':marker})
        self.exited_legacy()
        with self.assertRaises(core.UserError): self.store.prepare(self.a['id'],'local')
        self.assertFalse((self.store.root/'accounts'/self.a['id']/'codex/auth.json').exists())

    def test_native_replaced_index_is_restored_for_its_own_storage(self):
        a=self.store.prepare(self.a['id'],'local');home=Path(a['home'])
        core.atomic_write(home/'session_index.jsonl','LOCAL-SYNTHETIC\n')
        self.store.prepare(self.a['id'],'external')
        self.assertFalse((home/'session_index.jsonl').exists())
        core.atomic_write(home/'session_index.jsonl','EXTERNAL-SYNTHETIC\n')
        self.store.prepare(self.a['id'],'local')
        self.assertEqual((home/'session_index.jsonl').read_text(),'LOCAL-SYNTHETIC\n')

    def test_retargeting_one_storage_keeps_each_path_cache_and_history_separate(self):
        prepared=self.store.prepare(self.a['id'],'local');home=Path(prepared['home'])
        old_base=Path(prepared['base']);new_base=self.root/'replacement'
        sentinel=Path(prepared['history'])/'sessions'/'synthetic.jsonl'
        sentinel.write_text('SYNTHETIC_ROLLOUT\n')
        for name in ['session_index.jsonl','history.jsonl']:
            core.atomic_write(home/name,'OLD-PATH\n')

        def retarget(path):
            snapshot=self.store.storage_snapshot()
            for row in snapshot['storages']:
                if row['id']=='local':row['path']=str(path)
            self.store.save_storages(snapshot['storages'],snapshot['revision'])
            self.store.initialize_storage('local')
            return self.store.prepare(self.a['id'],'local')

        changed=retarget(new_base)
        self.assertEqual(list((Path(changed['history'])/'sessions').iterdir()),[])
        for name in ['session_index.jsonl','history.jsonl']:
            self.assertFalse((home/name).exists())
            core.atomic_write(home/name,'NEW-PATH\n')
        retarget(old_base)
        for name in ['session_index.jsonl','history.jsonl']:
            self.assertEqual((home/name).read_text(),'OLD-PATH\n')
        retarget(new_base)
        for name in ['session_index.jsonl','history.jsonl']:
            self.assertEqual((home/name).read_text(),'NEW-PATH\n')
        self.assertEqual(sentinel.read_text(),'SYNTHETIC_ROLLOUT\n')

    def test_legacy_binding_uses_live_link_after_registered_path_changes(self):
        prepared=self.store.prepare(self.a['id'],'local');home=Path(prepared['home'])
        old_base=Path(prepared['base'])
        core.atomic_write(home/'history.jsonl','OLD-LEGACY-PATH\n')
        core.write_json(home.parent/'cache-binding.json',{'phase':'ready','storage':'local'})
        for destination in [self.root/'replacement',old_base]:
            snapshot=self.store.storage_snapshot()
            for row in snapshot['storages']:
                if row['id']=='local':row['path']=str(destination)
            self.store.save_storages(snapshot['storages'],snapshot['revision'])
            self.store.initialize_storage('local')
            self.store.prepare(self.a['id'],'local')
            if destination!=old_base:self.assertFalse((home/'history.jsonl').exists())
        self.assertEqual((home/'history.jsonl').read_text(),'OLD-LEGACY-PATH\n')

    def test_legacy_dormant_cache_with_unknown_retired_path_is_preserved_not_loaded(self):
        prepared=self.store.prepare(self.a['id'],'external');home=Path(prepared['home'])
        legacy=home.parent/'storage-cache'/'local'/'history.jsonl'
        core.atomic_write(legacy,'UNKNOWN-OLD-PATH\n')
        snapshot=self.store.storage_snapshot()
        for row in snapshot['storages']:
            if row['id']=='local':row['path']=str(self.root/'replacement')
        self.store.save_storages(snapshot['storages'],snapshot['revision'])
        self.store.initialize_storage('local')
        registry=self.store.registry.read()
        registry.pop('retired_locations')  # Older registries kept only path strings.
        core.write_json(self.store.registry.path,registry)
        self.store.prepare(self.a['id'],'local')
        self.assertFalse((home/'history.jsonl').exists())
        self.assertEqual(legacy.read_text(),'UNKNOWN-OLD-PATH\n')

    def test_legacy_loading_journal_recovers_before_requested_storage(self):
        from accounts import account_home
        home=account_home(self.store,self.a['id'])
        for storage,label in [('local','LOCAL'),('external','EXTERNAL')]:
            core.atomic_write(home.parent/'storage-cache'/storage/'history.jsonl',label+'\n')
        core.atomic_write(home/'history.jsonl','INTERRUPTED\n')
        core.write_json(home.parent/'cache-binding.json',{'phase':'loading','storage':'external'})
        self.store.prepare(self.a['id'],'local')
        self.assertEqual((home/'history.jsonl').read_text(),'LOCAL\n')
        for storage,label in [('local','LOCAL'),('external','EXTERNAL')]:
            self.assertEqual((home.parent/'storage-cache'/storage/'history.jsonl').read_text(),label+'\n')
        self.store.prepare(self.a['id'],'external')
        self.assertEqual((home/'history.jsonl').read_text(),'EXTERNAL\n')

    def test_backend_cleanup_lease_blocks_account_edits(self):
        _,path=account_lock_paths(self.store,self.a['id'])
        with take_lock(path,shared=True):
            self.assertTrue(self.store.running_profile(self.a['id']))
            with self.assertRaises(core.UserError): self.store.save_profile(dict(self.a,name='changed'))
        self.assertFalse(self.store.running_profile(self.a['id']))

    def test_late_old_backend_generation_is_rejected(self):
        _,path=account_lock_paths(self.store,self.a['id'])
        marker=self.root/'generation.json';core.write_json(marker,{'job':'new'})
        guard=Path(core.__file__).with_name('backend_guard.py')
        result=subprocess.run([sys.executable,'-I','-B',str(guard),str(path),str(marker),'old','/usr/bin/true'],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'expired instance',result.stderr)

    def test_daily_child_keeps_native_helper_refresh_and_only_tests_skip_it(self):
        a=self.store.prepare(self.a['id'],'local')
        self.assertNotIn('CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH',core.environment(a))
        a['access_mode']='readonly'
        self.assertEqual(core.environment(a)['CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH'],'1')

    def test_legacy_import_rejects_parent_symlink(self):
        outside=core.directory(self.root/'outside');core.write_json(outside/'auth.json',{'synthetic':'OUTSIDE'})
        folder=core.directory(self.store.root/'stores/local/instances'/self.a['id'])
        (folder/'codex').symlink_to(outside)
        with self.assertRaises(core.UserError): self.store.prepare(self.a['id'],'local')
        self.assertFalse((self.store.root/'accounts'/self.a['id']/'codex/auth.json').exists())

    def test_old_untracked_auth_is_not_imported(self):
        legacy=self.store.root/'stores/local/instances'/self.a['id']/'codex/auth.json'
        core.write_json(legacy,{'synthetic':'A'})
        with self.assertRaises(core.UserError): self.store.prepare(self.a['id'],'local')

    def test_legacy_import_rejects_missing_process_identity(self):
        legacy=self.store.root/'stores/local/instances'/self.a['id']/'codex/auth.json'
        core.write_json(legacy,{'synthetic':'A'})
        self.exited_legacy()
        path=self.store.root/'jobs/legacy.json'
        value=json.loads(path.read_text());value.pop('pid_start')
        core.write_json(path,value);os.utime(path,(time.time()-120,time.time()-120))
        with self.assertRaises(core.UserError): self.store.prepare(self.a['id'],'local')
        self.assertFalse((self.store.root/'accounts'/self.a['id']/'codex/auth.json').exists())

    def test_legacy_import_rejects_incomplete_process_inspection(self):
        from accounts import legacy_idle
        self.exited_legacy()
        for returncode,stdout,stderr in [(1,b'',b'permission denied'),(0,b'',b'cannot inspect'),
                                         (1,b'p123\n',b'')]:
            with self.subTest(returncode=returncode,stdout=stdout,stderr=stderr):
                responses=[subprocess.CompletedProcess([],0,b'',b''),
                           subprocess.CompletedProcess([],returncode,stdout,stderr)]
                with patch('core.process_matches',return_value=False), patch('accounts.subprocess.run',side_effect=responses):
                    with self.assertRaises(core.UserError):legacy_idle(self.store,self.a['id'],[])

    def test_launcher_imports_quiet_legacy_login_excluding_only_current_job(self):
        from launcher import run_instance
        legacy=self.store.root/'stores/local/instances'/self.a['id']/'codex/auth.json'
        core.write_json(legacy,{'synthetic':'A'})
        self.exited_legacy()
        job='a'*32
        core.write_json(self.store.root/'jobs'/f'{job}.json',
                        {'job':job,'id':self.a['id'],'storage':'local','status':'starting',
                         'storage_path':str(self.store.root/'stores/local')})
        # Stop after prepare, before any desktop, preferences or network effects.
        with patch('launcher.snapshot',side_effect=core.UserError('synthetic stop after prepare')):
            result=run_instance(self.store,job,self.a['id'],'local',None)
        self.assertEqual(result['message'],'synthetic stop after prepare')
        self.assertEqual((self.store.root/'accounts'/self.a['id']/'codex/auth.json').read_bytes(),legacy.read_bytes())

    def test_launcher_can_retry_import_after_legacy_quiet_period(self):
        from launcher import run_instance
        legacy=self.store.root/'stores/local/instances'/self.a['id']/'codex/auth.json'
        core.write_json(legacy,{'synthetic':'A'});self.exited_legacy()
        old_job=self.store.root/'jobs/legacy.json';os.utime(old_job,None)
        def attempt(job):
            core.write_json(self.store.root/'jobs'/f'{job}.json',
                            {'job':job,'id':self.a['id'],'storage':'local','status':'starting',
                             'storage_path':str(self.store.root/'stores/local')})
            with patch('launcher.snapshot',side_effect=core.UserError('synthetic stop after prepare')):
                return run_instance(self.store,job,self.a['id'],'local',None)
        first=attempt('a'*32)
        self.assertIn('等待一分钟',first['message'])
        self.assertFalse((self.store.root/'accounts'/self.a['id']/'codex/auth.json').exists())
        os.utime(old_job,(time.time()-120,time.time()-120))
        second=attempt('b'*32)
        self.assertEqual(second['message'],'synthetic stop after prepare')
        self.assertEqual((self.store.root/'accounts'/self.a['id']/'codex/auth.json').read_bytes(),legacy.read_bytes())

    def test_live_backend_blocks_storage_removal_after_desktop_exits(self):
        self.store.prepare(self.a['id'],'local')
        marker=self.store.root/'accounts'/self.a['id']/'launch-generation.json'
        core.write_json(marker,{'job':'synthetic','storage':'local'})
        _,lease=account_lock_paths(self.store,self.a['id'])
        with take_lock(lease,shared=True):
            value=self.store.storage_snapshot()
            rows=[s for s in value['storages'] if s['id']!='local']
            with self.assertRaises(core.UserError): self.store.save_storages(rows,value['revision'])

    def test_guardian_kill_keeps_native_child_lease_until_child_exit(self):
        _,lease=account_lock_paths(self.store,self.a['id'])
        marker=self.root/'generation.json';core.write_json(marker,{'job':'current'})
        child=self.root/'child.py';pidfile=self.root/'child.pid'
        child.write_text('import os,time,pathlib\npathlib.Path('+repr(str(pidfile))+').write_text(str(os.getpid()))\ntime.sleep(15)\n')
        guard=Path(core.__file__).with_name('backend_guard.py')
        env={k:v for k,v in os.environ.items() if not k.startswith('HARBOR_')}
        proc=subprocess.Popen([sys.executable,'-I','-B',str(guard),str(lease),str(marker),'current',sys.executable,str(child)],env=env)
        child_pid=None
        try:
            deadline=time.monotonic()+5
            while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(pidfile.exists());child_pid=int(pidfile.read_text())
            proc.kill();proc.wait(timeout=5)
            with self.assertRaises(core.UserError): take_lock(lease)
            os.kill(child_pid,signal.SIGTERM)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try:
                    with take_lock(lease):break
                except core.UserError:time.sleep(.02)
            else:self.fail('Child lease was not released')
        finally:
            if proc.poll() is None:proc.kill();proc.wait(timeout=5)
            if child_pid:
                try:os.kill(child_pid,signal.SIGTERM)
                except ProcessLookupError:pass

    def test_cache_loading_journal_recovers_without_overwriting_previous(self):
        a=self.store.prepare(self.a['id'],'local');home=Path(a['home'])
        core.atomic_write(home/'session_index.jsonl','LOCAL\n')
        import accounts
        original=accounts.copy_private
        def fail_loading(source,target):
            if target==home/'session_index.jsonl':raise OSError('synthetic crash')
            return original(source,target)
        cache=core.directory(home.parent/'storage-cache/external')
        core.atomic_write(cache/'session_index.jsonl','EXTERNAL\n')
        with patch('accounts.copy_private',side_effect=fail_loading):
            with self.assertRaises(OSError):self.store.prepare(self.a['id'],'external')
        self.store.prepare(self.a['id'],'local')
        self.assertEqual((home/'session_index.jsonl').read_text(),'LOCAL\n')
        self.assertEqual((cache/'session_index.jsonl').read_text(),'EXTERNAL\n')


if __name__=='__main__': unittest.main()
