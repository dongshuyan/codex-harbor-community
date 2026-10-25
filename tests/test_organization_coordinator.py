"""Cross-edition coordination, using exclusively temporary synthetic stores."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
from organization import Organization,STATE


class Coordinator(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.a=core.Store(self.root/'a',self.root/'disk',mounted=lambda:False, community=False)
        self.a.initialize_storage('local')
        self.history=self.a.registry.history_path(self.a.registry.entry('local'))
        core.write_json(self.history/STATE,{'local-projects':{'one':{'id':'one','name':'One','rootPaths':[]}},'pinned-project-ids':[]})
        self.shared=self.root/'shared'

    def test_one_authority_imports_old_ledger_once_and_preserves_ack(self):
        p=self.a.save_profile({'kind':'official','name':'Synthetic'})
        home=Path(self.a.prepare(p['id'],'local')['home'])
        v=__import__('json').loads((home/STATE).read_text());v['pinned-project-ids']=['one'];core.write_json(home/STATE,v)
        Organization(self.a).publish(home)
        old=__import__('json').loads((self.a.root/'organization/state.json').read_text())
        self.a.shared_organization_root=self.shared
        org=Organization(self.a)
        with org.transaction() as value:
            self.assertEqual(len(value['scopes']),1)
            saved=copy.deepcopy(value)
        self.assertEqual(org.path,self.shared/'state.json')
        with org.transaction() as value:self.assertEqual(value,saved)
        marker=__import__('json').loads((self.a.root/'organization/state.json').read_text())
        self.assertEqual(marker['version'],3)
        self.assertEqual(marker['legacy'],old)
        self.assertEqual(saved['writers'][str(home)]['ack_snapshot']['pinned-project-ids'],['one'])

    def test_crash_after_redirect_before_commit_retries_full_import(self):
        from unittest.mock import patch
        self.a.shared_organization_root=self.shared
        # Force only the central commit to fail; legacy redirect must retain all input.
        original=core.write_json
        def fail(path,value):
            if path==self.shared/'state.json':raise OSError('synthetic interrupted commit')
            original(path,value)
        with patch('organization.write_json',side_effect=fail):
            with self.assertRaises(OSError):
                with Organization(self.a).transaction():pass
        with Organization(self.a).transaction() as value:
            self.assertIn(str(self.a.root),value['control_roots'])

    def test_two_roots_use_same_scope_and_no_overwrite_on_second_import(self):
        self.a.shared_organization_root=self.shared
        with Organization(self.a).transaction() as value:
            _,scope=Organization(self.a).scope(value,self.history)
            scope['data']={'pinned-project-ids':['one']}
        b=core.Store(self.root/'b',self.root/'disk',mounted=lambda:False, community=False)
        b.shared_organization_root=self.shared
        with Organization(b).transaction() as value:
            self.assertEqual(next(iter(value['scopes'].values()))['data']['pinned-project-ids'],['one'])
            self.assertEqual(set(value['control_roots']),{str(self.a.root),str(b.root)})
