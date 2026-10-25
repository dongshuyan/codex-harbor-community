"""Daily launches inherit native behavior; registration never migrates data."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import storage


class DailyDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name).resolve()
        self.homes={'original-local':('本地',str(self.root/'original'))}
        self.patch=patch.object(storage,'EXISTING_HOMES',self.homes);self.patch.start()
        self.store=core.Store(self.root/'control',self.root/'disk',mounted=lambda:True, community=False)

    def tearDown(self):
        self.patch.stop();self.tmp.cleanup()

    def test_normal_launch_has_no_test_permission_or_feature_overrides(self):
        self.store.initialize_storage('local')
        p=self.store.save_profile({'kind':'official','name':'Synthetic'})
        prepared=self.store.prepare(p['id'],'local');core.configure(prepared)
        value=tomllib.loads((Path(prepared['home'])/'config.toml').read_text())
        for key in ['permissions','default_permissions','approval_policy','sandbox_mode','web_search','memories','features']:
            self.assertNotIn(key,value)
        self.assertEqual(value['sqlite_home'],prepared['history'])

    def test_original_retarget_only_changes_registration_and_keeps_old_files(self):
        old=core.directory(self.root/'original');(old/'sentinel').write_text('HISTORY')
        target=self.root/'new';snapshot=self.store.storage_snapshot()
        row=next(s for s in snapshot['storages'] if s['id']=='original-local')
        row.update(path=str(target),enabled=True)
        self.store.save_storages(snapshot['storages'],snapshot['revision'])
        self.assertFalse(target.exists())
        self.assertEqual((old/'sentinel').read_text(),'HISTORY')
        self.store.initialize_storage(row['id'])
        self.assertTrue(target.is_dir())
        self.assertEqual(self.store.registry.history_path(self.store.registry.entry(row['id'])),target)

    def test_new_registration_uses_selected_directory_for_native_history(self):
        root=core.directory(self.root/'native');core.directory(root/'sessions')
        (root/'sessions/synthetic.jsonl').write_text('UNCHANGED')
        value=self.store.storage_snapshot()
        value['storages'].append(dict(id='a'*32,name='Custom',path=str(root),enabled=True))
        self.store.save_storages(value['storages'],value['revision'])
        p=self.store.save_profile({'kind':'official','name':'Synthetic'})
        prepared=self.store.prepare(p['id'],'a'*32)
        self.assertEqual(Path(prepared['history']),root)
        self.assertFalse((root/'history').exists())
        self.assertFalse((root/'instances').exists())
        self.assertEqual((root/'sessions/synthetic.jsonl').read_text(),'UNCHANGED')
