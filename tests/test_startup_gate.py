from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
from organization import Organization
from startup_gate import acquire


class StartupGate(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.a=core.Store(self.root/'a',self.root/'disk',mounted=lambda:False, community=False)
        self.b=core.Store(self.root/'b',self.root/'disk',mounted=lambda:False, community=False)
        for s in (self.a,self.b):s.shared_organization_root=self.root/'shared'

    def test_entries_share_one_preflight_lease_until_released(self):
        with acquire(self.a,'one'):
            with self.assertRaises(core.UserError):acquire(self.b,'two')
        with acquire(self.b,'two'):pass

    def test_unresolved_os_launch_blocks_a_later_entry_across_roots(self):
        with Organization(self.a).transaction():pass
        core.write_json(core.directory(self.a.root/'jobs')/'old.json',{'job':'old','desktop_launch_phase':'pending'})
        with self.assertRaisesRegex(core.UserError,'尚未确认'):acquire(self.b,'two')

    def test_already_running_private_window_does_not_hold_startup_gate(self):
        core.write_json(core.directory(self.a.root/'jobs')/'old.json',{'job':'old','desktop_launch_phase':'running'})
        with acquire(self.a,'one'):pass
