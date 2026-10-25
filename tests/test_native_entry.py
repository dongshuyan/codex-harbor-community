"""Original-entry orchestration only; no real App/credentials/history used."""
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
import native_entry
from startup_gate import acquire


class NativeEntry(unittest.TestCase):
    def setUp(self):
        stamp=patch('native_entry.process_stamp',return_value='synthetic-worker-start')
        stamp.start();self.addCleanup(stamp.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.store=core.Store(self.root/'control',self.root/'disk',mounted=lambda:False, community=False)
        self.store.initialize_storage('local')
        self.history=self.store.registry.history_path(self.store.registry.entry('local'))
        self.original={'config.toml':b'# untouched original\n', 'auth.json':b'SYNTHETIC_ONLY',
                       'sessions/test.jsonl':b'opaque historical fixture'}
        for name,data in self.original.items():
            path=self.history/name;path.parent.mkdir(exist_ok=True);path.write_bytes(data)
        self.job='a'*32;self.path=core.directory(self.store.root/'jobs')/(self.job+'.json')
        core.write_json(self.path,{'job':self.job,'status':'starting','storage_path':self.store.registry.entry('local')['path']})

    def tearDown(self):
        for name,data in self.original.items():self.assertEqual((self.history/name).read_bytes(),data)

    def test_launch_only_helper_releases_gate_before_bounded_readiness(self):
        captured={}
        class Pipe:
            def write(self,data):captured.update(json.loads(data))
            def close(self):pass
        outer=self
        class Process:
            stdin=Pipe()
            def wait(self,timeout=None):
                state=json.loads(outer.path.read_text());state.update(status='running',desktop_launch_phase='running',pid=123,pid_start='synthetic')
                core.write_json(outer.path,state);return 0
        def verified(*args,**kwargs):
            with acquire(self.store,'other'):pass
        with patch('native_profile.require_idle'),patch('native_entry.subprocess.Popen',return_value=Process()),patch('native_entry.check_startup',side_effect=verified) as check:
            result=native_entry.run(self.store,self.job,'local')
        check.assert_called_once()
        self.assertEqual(result['status'],'running')
        self.assertTrue(captured['launchOnly'])
        self.assertEqual(captured['arguments'],[])
        self.assertEqual(captured['environment'],{'CODEX_HOME':str(self.history),'CODEX_SQLITE_HOME':str(self.history)})

    def test_late_system_callback_keeps_intent_without_holding_startup_gate(self):
        outer=self
        class Process:
            stdin=io.BytesIO()
            def wait(self,timeout=None):
                self.timeout=timeout
                state=json.loads(outer.path.read_text());state['desktop_launch_phase']='requested'
                core.write_json(outer.path,state)
                raise subprocess.TimeoutExpired('synthetic helper',timeout)
        proc=Process()
        with patch('native_profile.require_idle'),patch('native_entry.subprocess.Popen',return_value=proc):
            result=native_entry.run(self.store,self.job,'local')
        self.assertEqual(proc.timeout,30)
        self.assertEqual(result['desktop_launch_phase'],'requested')
        self.assertEqual(json.loads(self.path.read_text())['desktop_launch_phase'],'requested')
        # The lock is released, but the unresolved OS intent still prevents a
        # duplicate request. A late, confirmed callback makes it usable again.
        with self.assertRaisesRegex(core.UserError,'上一次'):acquire(self.store,'other')
        state=json.loads(self.path.read_text());state.update(status='error',desktop_launch_phase='failed');core.write_json(self.path,state)
        with acquire(self.store,'other'):pass

    def test_unknown_occupancy_never_projects_or_launches(self):
        path=self.history/'.codex-global-state.json';path.write_text('{"private":"KEEP"}')
        before=path.read_bytes()
        with patch('native_profile.require_idle',side_effect=core.UserError('Synthetic occupied profile')),patch('native_entry.subprocess.Popen') as spawn:
            result=native_entry.run(self.store,self.job,'local')
            spawn.assert_not_called()
        self.assertEqual(result['status'],'error');self.assertEqual(path.read_bytes(),before)

    def test_changed_storage_selection_is_rejected_before_prepare(self):
        core.write_json(self.path,{'storage_path':'/synthetic/changed'})
        with patch('native_entry.Organization.prepare_original') as prepare,patch('native_entry.subprocess.Popen') as spawn:
            result=native_entry.run(self.store,self.job,'local')
            prepare.assert_not_called();spawn.assert_not_called()
        self.assertEqual(result['status'],'error')
