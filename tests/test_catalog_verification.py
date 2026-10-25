"""Readiness is native completion + matching identities, not a launched PID."""
from pathlib import Path
import json
from contextlib import closing
import sqlite3
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_sidebar_catalog as fixtures
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import sidebar_catalog as catalog


class Verification(unittest.TestCase):
    bind=fixtures.SidebarCatalog.bind
    def setUp(self):
        fixtures.SidebarCatalog.setUp(self)
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute("DELETE FROM local_thread_catalog_scan_checkpoints WHERE host_id='local'")

    def seed(self):
        self.path=self.history/'sessions/rollout-2026-10-24T00-00-00-00000000-0000-4000-8000-000000000001.jsonl'
        self.path.parent.mkdir(exist_ok=True);self.path.write_bytes(b'opaque synthetic contents')
        self.ident='00000000-0000-4000-8000-000000000001'
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('INSERT INTO threads(id,cwd,source,archived,preview,rollout_path,name) VALUES(?,?,?,?,?,?,?)',
                       (self.ident,'/synthetic/project','vscode',0,'Synthetic',str(self.path),'Native title'))
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('INSERT INTO local_thread_catalog(host_id,thread_id,cwd,project_id,missing_candidate,display_title,source_kind) VALUES(?,?,?,?,?,?,?)',
                       ('local',self.ident,'/synthetic/project',None,0,'Native title','vscode'))

    def test_launch_is_not_complete_while_native_scan_is_pending(self):
        self.seed()
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('UPDATE local_thread_catalog_sync_state SET initial_build_complete=0 WHERE host_id="local"')
        self.assertEqual(catalog.verify_catalog(self.home,self.history)['status'],'loading')

    def test_native_completion_records_receipt_without_rewriting_databases(self):
        self.seed();protected=[self.source,self.cache,self.path];before=[p.read_bytes() for p in protected]
        r=catalog.verify_catalog(self.home,self.history)
        self.assertEqual(r['status'],'verified')
        self.assertEqual([p.read_bytes() for p in protected],before)
        self.assertTrue((self.home/'.harbor-catalog-receipt.json').is_file())

    def test_equal_ids_but_stale_native_metadata_is_not_verified(self):
        self.seed()
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('UPDATE local_thread_catalog SET source_updated_at=0')
        self.assertNotEqual(catalog.verify_catalog(self.home,self.history)['status'],'verified')

    def test_saved_receipt_detects_title_only_corruption(self):
        self.seed();catalog.verify_catalog(self.home,self.history)
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute("UPDATE local_thread_catalog SET display_title='Stale cache title'")
        self.assertTrue(catalog.prepare_catalog(self.home,self.history)['rescan'])

    def test_normal_incremental_update_does_not_force_full_history_rescan(self):
        self.seed();catalog.verify_catalog(self.home,self.history)
        self.bind()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE threads SET name='A legitimate newer title',updated_at=20,recency_at=20")
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute("UPDATE local_thread_catalog SET display_title='A legitimate newer title',source_updated_at=20,source_recency_at=20")
        self.assertFalse(catalog.prepare_catalog(self.home,self.history)['rescan'])

    def test_corrupt_title_receipt_requests_rebuild_instead_of_crashing(self):
        self.seed();catalog.verify_catalog(self.home,self.history);self.bind()
        path=self.home/'.harbor-catalog-receipt.json'
        for invalid in (None, [], 'broken', {}, {'input':'x'}):
            path.write_text(json.dumps({'version':2,'titles':{self.ident:invalid}}))
            with self.subTest(invalid=invalid):
                self.assertTrue(catalog.prepare_catalog(self.home,self.history)['rescan'])

    def test_unindexed_rollout_is_reported_separately_not_hidden_as_cache_success(self):
        self.seed()
        orphan=self.path.with_name(self.path.name.replace('000000000001','000000000002'))
        orphan.write_bytes(b'opaque unindexed fixture')
        result=catalog.verify_catalog(self.home,self.history)
        self.assertEqual(result['status'],'unverified')
        self.assertEqual(result['audit']['unindexed_rollouts'],1)
        self.assertEqual(result['audit']['missing_rollouts'],0)

    def test_unavailable_cache_row_is_classified_after_native_completed(self):
        self.seed();self.path.unlink()
        with closing(sqlite3.connect(self.cache)) as db,db:
            db.execute('DELETE FROM local_thread_catalog')
        result=catalog.verify_catalog(self.home,self.history)
        self.assertEqual(result['status'],'unverified')
        self.assertEqual(result['missing'],1)
        self.assertEqual(result['audit']['missing_cache_rollouts'],1)

    def test_changes_during_audit_retry_without_publishing_a_stale_receipt(self):
        self.seed()
        from catalog_verification import audit_rollouts
        def changing(*args,**kwargs):
            result=audit_rollouts(*args,**kwargs)
            with closing(sqlite3.connect(self.source)) as db,db:
                db.execute('UPDATE threads SET updated_at=25')
            return result
        with patch('catalog_verification.audit_rollouts',side_effect=changing):
            result=catalog.verify_catalog(self.home,self.history)
        self.assertEqual(result['status'],'loading')
        self.assertFalse((self.home/'.harbor-catalog-receipt.json').exists())

    def test_readonly_worker_returns_receipt_without_any_file_write(self):
        self.seed()
        result=catalog.verify_catalog(self.home,self.history,write_receipt=False)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['_receipt']['version'],2)
        self.assertFalse((self.home/'.harbor-catalog-receipt.json').exists())
