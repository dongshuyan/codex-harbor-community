"""Private slots reuse preferences and never move existing session data."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
import instances
from accounts import take_lock


class InstanceSlots(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='harbor-slots-')
        self.root=Path(self.temp.name)
        self.store=core.Store(self.root/'control',self.root/'disk',mounted=lambda:True,community=False)
        self.store.initialize_storage('local');self.store.initialize_storage('external')
        self.profile=self.store.save_profile({'kind':'official','name':'Synthetic account'})
        self.ident=self.profile['id'];self.leases=ExitStack()

    def tearDown(self):self.leases.close();self.temp.cleanup()

    def reserve(self,storage='local'):
        slot,paths,guards,claim,switch=instances.reserve(self.store,self.ident,storage)
        self.leases.enter_context(guards)
        return slot,paths,guards,switch

    def test_busy_primary_uses_secondary_and_sequential_launch_reuses_settings(self):
        primary,_,_,_=self.reserve()
        self.assertEqual(primary,'primary')
        first,paths,guards,_=self.reserve()
        prepared=self.store.prepare(self.ident,'local',launch_job='a'*32,instance_slot=first)
        core.configure(prepared)
        config=Path(prepared['home'])/'config.toml'
        config.write_text(config.read_text()+'\n[desktop]\nsynthetic_preference = "retained"\n')
        guards.close()
        again,_,_,_=self.reserve()
        self.assertEqual(again,first)
        core.configure(self.store.prepare(self.ident,'local',launch_job='b'*32,instance_slot=again))
        self.assertIn('synthetic_preference = "retained"',config.read_text())

    def test_primary_official_home_is_exclusive_across_storage_locations(self):
        self.reserve('local')
        slot,paths,_,_=self.reserve('external')
        self.assertNotEqual(slot,'primary')
        self.assertNotEqual(paths['home'],self.store.root/'accounts'/self.ident/'codex')

    def test_native_orphan_lease_prevents_slot_reuse(self):
        slot,paths,guards,switch=self.reserve()
        switch.close()
        backend=take_lock(paths['control']/'backend.lock',shared=True)
        try:
            guards.close()
            next_slot,_,_,_=self.reserve()
            self.assertNotEqual(next_slot,slot)
        finally:backend.close()

    def test_seed_failure_never_leaves_half_initialized_runtime(self):
        source=core.directory(self.root/'seed');target=self.root/'destination'
        (source/'config.toml').write_text('[desktop]\nfoo="bar"\n')
        with patch('instances.copy_private',side_effect=OSError('synthetic interruption')):
            with self.assertRaises(OSError):instances.seed_home(source,target)
        self.assertFalse(target.exists())
        instances.seed_home(source,target)
        self.assertEqual((target/'config.toml').read_bytes(),(source/'config.toml').read_bytes())

    def test_seed_includes_preferences_and_skills_but_not_credentials_or_history(self):
        source=core.directory(self.root/'source');target=self.root/'target'
        (source/'config.toml').write_text('[desktop]\ntheme="dark"\n')
        (source/'auth.json').write_text('SYNTHETIC_DO_NOT_COPY')
        (source/'.credentials.json').write_text('SYNTHETIC_DO_NOT_COPY')
        (core.directory(source/'sessions')/'private-session.jsonl').write_text('SYNTHETIC_DO_NOT_COPY')
        skill=core.directory(source/'skills/custom');(skill/'SKILL.md').write_text('Synthetic skill')
        instances.seed_home(source,target)
        self.assertTrue((target/'config.toml').is_file())
        self.assertEqual((target/'skills/custom').resolve(),skill)
        self.assertFalse((target/'auth.json').exists())
        self.assertFalse((target/'.credentials.json').exists())
        self.assertFalse((target/'sessions').exists())

    def test_v2_orphan_blocks_edit_and_storage_retarget(self):
        lease=take_lock(self.store.instance_backend_lock('local',self.ident,v2=True),shared=True)
        try:
            self.assertTrue(self.store.running_profile(self.ident))
            self.assertTrue(self.store.storage_running('local'))
            self.assertFalse(self.store.storage_running('external'))
            with self.assertRaises(core.UserError):
                with self.store.edit_guard(self.ident):pass
        finally:lease.close()

    def test_explicit_purge_removes_slot_credentials_and_keeps_shared_history(self):
        paths=instances.slot_paths(self.store,self.ident,'local','c'*32)
        account=self.store.root/'accounts'/self.ident
        credentials=[account/'codex/auth.json',account/'refresh-owner/codex/auth.json',
                     paths['home']/'auth.json',paths['home']/'.credentials.json']
        for path in credentials:
            core.directory(path.parent)
            path.write_text('SYNTHETIC_CREDENTIAL')
        history=self.store.registry.history_path(self.store.registry.entry('local'))
        session=core.directory(history/'sessions')/'synthetic-session.jsonl'
        session.write_text('SYNTHETIC_HISTORY_MUST_REMAIN')
        self.store.remove_profile(self.ident)
        removed=self.store.removed_profiles()[0]
        self.store.purge_profile(self.ident,removed['revision'])
        self.assertTrue(all(not path.exists() for path in credentials))
        self.assertEqual(session.read_text(),'SYNTHETIC_HISTORY_MUST_REMAIN')
        self.assertEqual(self.store.removed_profiles(),[])


if __name__=='__main__':unittest.main()
