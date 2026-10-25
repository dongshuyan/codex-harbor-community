"""Startup ownership checks use temporary data and never spawn the desktop."""
import json
import os
import plistlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
import launcher
import main


class StartupOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name).resolve()
        self.store=core.Store(self.root/'control',self.root/'external',mounted=lambda:True, community=False)
        self.store.initialize_storage('local')
        self.profile=self.store.save_profile({'name':'Synthetic startup','kind':'official'})
        self.job='a'*32
        self.path=self.store.root/'jobs'/f'{self.job}.json'

    def tearDown(self):self.tmp.cleanup()

    def job_state(self,**updates):
        value={'id':self.profile['id'],'job':self.job,'storage':'local','status':'starting',
               'storage_path':str(self.store.root/'stores/local')}
        value.update(updates)
        core.write_json(self.path,value)
        return value

    def visible_job(self):
        return next(j for j in main.status(self.store)['jobs'] if j['job']==self.job)

    def test_worker_publishes_creation_time_and_identity_before_desktop_spawn(self):
        created=time.time()-15
        self.job_state(created_at=created)
        app=core.directory(self.root/'fake.app'/'Contents'/'MacOS')
        (app/'ChatGPT').write_text('synthetic file, never executed')
        (app/'ChatGPT').chmod(0o700)
        (app.parent/'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'ChatGPT',
            'CFBundleShortVersionString':'26.1021.31345','CFBundleVersion':'21828'}))
        cli=core.directory(app.parent/'Resources')/'codex';cli.touch();cli.chmod(0o700)
        observed=[]
        def stop_before_spawn(*args,**kwargs):
            observed.append(json.loads(self.path.read_text()))
            raise OSError('synthetic stop before desktop spawn')
        with patch('launcher.APP',self.root/'fake.app'),patch('launcher.snapshot',return_value='synthetic-backup'),\
                patch('launcher.process_stamp',return_value='synthetic-worker-start',create=True),\
                patch('launcher.tempfile.mkdtemp',return_value=str(core.directory(self.root/'ipc'))),\
                patch('launcher.subprocess.Popen',side_effect=stop_before_spawn):
            launcher.run_instance(self.store,self.job,self.profile['id'],'local',None)
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0].get('created_at'),created)
        self.assertEqual(observed[0].get('worker_pid'),os.getpid())
        self.assertEqual(observed[0].get('worker_pid_start'),'synthetic-worker-start')

    def test_starting_with_dead_worker_finishes_without_waiting_for_timeout(self):
        self.job_state(created_at=time.time(),worker_pid=99999999,worker_pid_start='synthetic')
        self.assertEqual(self.visible_job()['status'],'error')

    def test_reused_worker_pid_is_not_accepted_as_the_starting_worker(self):
        self.job_state(created_at=time.time(),worker_pid=os.getpid(),worker_pid_start='not-this-process')
        self.assertEqual(self.visible_job()['status'],'error')

    def test_legacy_starting_without_creation_time_uses_file_mtime(self):
        self.job_state()
        old=time.time()-121
        os.utime(self.path,(old,old))
        self.assertEqual(self.visible_job()['status'],'error')

    def test_current_worker_remains_starting_before_deadline(self):
        self.job_state(created_at=time.time(),worker_pid=os.getpid(),worker_pid_start=core.process_stamp(os.getpid()))
        self.assertEqual(self.visible_job()['status'],'starting')

    def test_running_desktop_warns_when_worker_pid_was_reused(self):
        self.job_state(status='running',pid=os.getpid(),pid_start=core.process_stamp(os.getpid()),
                       worker_pid=os.getpid(),worker_pid_start='not-this-process')
        visible=self.visible_job()
        self.assertEqual(visible['status'],'running')
        self.assertIn('监护进程已退出',visible.get('message',''))

    def test_legacy_running_worker_without_stamp_is_not_reported_as_verified(self):
        self.job_state(status='running',pid=os.getpid(),pid_start=core.process_stamp(os.getpid()),worker_pid=os.getpid())
        visible=self.visible_job()
        self.assertEqual(visible['status'],'running')
        self.assertIn('无法核对',visible.get('message',''))

    def test_stop_reports_whether_it_sent_a_signal(self):
        self.job_state(pid=123,pid_start='synthetic')
        with patch('launcher.process_matches',return_value=True),patch('launcher.os.kill') as kill:
            result=launcher.stop_instance(self.store,self.job)
        kill.assert_called_once_with(123,launcher.signal.SIGTERM)
        self.assertIs(result.get('signal_sent'),True)

    def test_stop_does_not_claim_a_signal_for_an_exited_process(self):
        self.job_state(status='error',exit_code=1)
        with patch('launcher.process_matches',return_value=False),patch('launcher.os.kill') as kill:
            result=launcher.stop_instance(self.store,self.job)
        kill.assert_not_called()
        self.assertIs(result.get('signal_sent'),False)

    def test_stop_handles_exit_between_identity_check_and_signal(self):
        self.job_state(pid=123,pid_start='synthetic')
        with patch('launcher.process_matches',return_value=True),patch('launcher.os.kill',side_effect=ProcessLookupError):
            result=launcher.stop_instance(self.store,self.job)
        self.assertIs(result.get('signal_sent'),False)


if __name__=='__main__':unittest.main()
