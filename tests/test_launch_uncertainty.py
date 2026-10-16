"""An interrupted OS launch must never free a possibly live private home."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
import instances
import main


class LaunchUncertainty(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='harbor-launch-state-',dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.store=core.Store(Path(self.temp.name)/'control',Path(self.temp.name)/'disk',mounted=lambda:True,community=False)
        self.store.initialize_storage('local')
        self.profile=self.store.save_profile({'kind':'official','name':'Synthetic'})
        self.ident=self.profile['id']
        self.job='a'*32
        self.path=self.store.root/'jobs'/f'{self.job}.json'

    def record(self,phase):
        core.write_json(self.path,dict(id=self.ident,job=self.job,storage='local',status='starting',
            lifecycle_version=3,desktop_spawn_attempted=True,desktop_launch_phase=phase,
            worker_pid=99999999,worker_pid_start='synthetic-dead-worker'))

    def test_missing_callback_blocks_reuse_and_profile_edits(self):
        for phase in ('pending','requested'):
            with self.subTest(phase=phase):
                self.record(phase)
                with self.assertRaises(core.UserError):instances.reserve(self.store,self.ident,'local')
                self.assertTrue(self.store.running_profile(self.ident))
                self.assertTrue(self.store.storage_running('local'))
                with self.assertRaises(core.UserError):
                    with self.store.edit_guard(self.ident):pass

    def test_reported_os_failure_allows_retry(self):
        self.record('failed')
        _,_,guards,_,_=instances.reserve(self.store,self.ident,'local')
        guards.close()

    def test_missing_receipt_is_not_reported_as_safe_to_retry(self):
        self.record('requested')
        job=next(v for v in main.status(self.store)['jobs'] if v['job']==self.job)
        self.assertIn('无法确认',job.get('message',''))
        self.assertNotIn('重新启动',job.get('message',''))


if __name__=='__main__':unittest.main()
