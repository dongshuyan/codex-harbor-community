"""Idle-window cache checks; synthetic databases and filenames, no user data."""
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import storage
from core import UserError
from sidebar_catalog import prepare_catalog, binding, verify_catalog


class SidebarCatalog(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='harbor-catalog-test-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.history = self.root / 'history'
        self.history.mkdir()
        self.home = self.root / 'account'
        (self.home / 'sqlite').mkdir(parents=True)
        self.source = self.history / 'state_5.sqlite'
        self.cache = self.home / 'sqlite/codex-dev.db'
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, project_id TEXT, '
                       'source TEXT, archived INTEGER, preview TEXT, rollout_path TEXT, '
                       'created_at INTEGER DEFAULT 10, updated_at INTEGER DEFAULT 10, recency_at INTEGER DEFAULT 10, '
                       'name TEXT, title TEXT, git_branch TEXT, model_provider TEXT)')
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.executescript('''
                CREATE TABLE local_thread_catalog (host_id TEXT, thread_id TEXT,
                    cwd TEXT, project_id TEXT, missing_candidate INTEGER,
                    display_title TEXT, source_created_at REAL DEFAULT 10, source_updated_at REAL DEFAULT 10,
                    source_recency_at REAL DEFAULT 10, source_kind TEXT, model_provider TEXT, git_branch TEXT,
                    PRIMARY KEY(host_id,thread_id));
                CREATE TABLE local_thread_catalog_sync_state (host_id TEXT PRIMARY KEY,
                    watermark_updated_at REAL, initial_build_complete INTEGER,
                    observation_sequence INTEGER, last_full_reconciled_at INTEGER);
                INSERT INTO local_thread_catalog_sync_state VALUES('local',20000,1,77,100);
                INSERT INTO local_thread_catalog_sync_state VALUES('cloud:test',20000,1,88,100);
                CREATE TABLE local_thread_catalog_scan_checkpoints
                    (host_id TEXT PRIMARY KEY, checkpoint TEXT, failed_at INTEGER);
                INSERT INTO local_thread_catalog_scan_checkpoints VALUES('local','old',NULL);
                INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud:test','keep',NULL);
                CREATE TABLE local_thread_catalog_metadata (id INTEGER PRIMARY KEY,catalog_revision INTEGER);
                INSERT INTO local_thread_catalog_metadata VALUES(1,9);
                CREATE TABLE unrelated (value TEXT);
                INSERT INTO unrelated VALUES('preserve');
            ''')
        self.bind()

    def bind(self, history=None):
        (self.home / '.harbor-catalog-binding.json').write_text(json.dumps(binding(history or self.history)))

    def add(self, ident, *, cached=False, compressed=False, source='vscode',
            archived=0, preview='Synthetic message', cwd='/synthetic/workspace'):
        path = self.history / 'sessions' / (ident + '.jsonl')
        path.parent.mkdir(exist_ok=True)
        # This test checks metadata/existence, not compression decoding.
        Path(str(path) + ('.zst' if compressed else '')).write_bytes(b'synthetic fixture')
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('INSERT INTO threads(id,cwd,project_id,source,archived,preview,rollout_path) VALUES(?,?,?,?,?,?,?)',
                       (ident, cwd, None, source, archived, preview, str(path)))
        if cached:
            with closing(sqlite3.connect(self.cache)) as db, db:
                db.execute('INSERT INTO local_thread_catalog(host_id,thread_id,cwd,project_id,missing_candidate,display_title,source_kind) VALUES(?,?,?,?,?,?,?)',
                           ('local', ident, cwd, None, 0, 'Keep my title',source))
                db.execute("DELETE FROM local_thread_catalog_scan_checkpoints WHERE host_id='local'")
            verify_catalog(self.home,self.history)

    def state(self, host='local'):
        with closing(sqlite3.connect(self.cache)) as db, db:
            return db.execute('SELECT initial_build_complete,last_full_reconciled_at,'
                              'watermark_updated_at,observation_sequence '
                              'FROM local_thread_catalog_sync_state WHERE host_id=?', (host,)).fetchone()

    def test_missing_old_compressed_entries_trigger_native_full_scan_preserving_all_rows(self):
        self.add('recent', cached=True)
        self.add('old', compressed=True, cwd='/another/workspace')
        source_bytes = self.source.read_bytes()
        before_cloud = self.state('cloud:test')
        result = prepare_catalog(self.home, self.history)
        self.assertEqual(result['missing'], 1)
        self.assertTrue(result['rescan'])
        self.assertEqual(self.state(), (0, None, None, 77))
        self.assertEqual(self.state('cloud:test'), before_cloud)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        with closing(sqlite3.connect(self.cache)) as db, db:
            self.assertEqual(db.execute('SELECT display_title FROM local_thread_catalog').fetchall(),
                             [('Keep my title',)])
            self.assertEqual(db.execute('SELECT * FROM unrelated').fetchall(), [('preserve',)])
            self.assertEqual(db.execute('SELECT host_id FROM local_thread_catalog_scan_checkpoints').fetchall(),
                             [('cloud:test',)])

    def test_complete_cache_is_not_rebuilt_or_rewritten(self):
        self.add('kept', cached=True, compressed=True)
        before = self.cache.read_bytes()
        result = prepare_catalog(self.home, self.history)
        self.assertFalse(result['rescan'])
        self.assertEqual(self.cache.read_bytes(), before)

    def test_same_count_with_different_ids_still_detects_missing_entry(self):
        self.add('missing')
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute("INSERT INTO local_thread_catalog(host_id,thread_id,cwd,project_id,missing_candidate,display_title) VALUES('local','foreign','/old',NULL,0,'old')")
        self.assertEqual(prepare_catalog(self.home, self.history)['missing'], 1)

    def test_local_project_membership_is_not_read_from_desktop_project_id(self):
        self.add('assigned', cached=True)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE threads SET project_id='native-project'")
        result = prepare_catalog(self.home, self.history)
        self.assertEqual(result['misgrouped'], 0)
        self.assertFalse(result['rescan'])

    def test_stale_catalog_row_is_not_a_valid_complete_result(self):
        self.add('stale', cached=True)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('UPDATE threads SET archived=1')
        result = prepare_catalog(self.home, self.history)
        self.assertEqual(result['extra'], 1)
        self.assertTrue(result['rescan'])

    def test_archived_subagent_and_empty_native_previews_are_not_missing_chats(self):
        self.add('archived', archived=1)
        self.add('child', source='subagent')
        self.add('empty', preview='')
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute("DELETE FROM local_thread_catalog_scan_checkpoints WHERE host_id='local'")
        self.assertEqual(verify_catalog(self.home,self.history)['status'],'verified')
        self.assertFalse(prepare_catalog(self.home, self.history)['rescan'])

    def test_native_grouping_mismatch_and_hidden_row_request_rescan(self):
        for edit in ["cwd='/old/workspace'", "project_id='old-project'", 'missing_candidate=1']:
            with self.subTest(edit=edit):
                with closing(sqlite3.connect(self.source)) as db, db:
                    db.execute('DELETE FROM threads')
                with closing(sqlite3.connect(self.cache)) as db, db:
                    db.execute('DELETE FROM local_thread_catalog')
                self.add('same', cached=True)
                with closing(sqlite3.connect(self.cache)) as db, db:
                    db.execute('UPDATE local_thread_catalog SET '+edit)
                self.assertTrue(prepare_catalog(self.home, self.history)['rescan'])

    def test_history_switch_and_untracked_legacy_cache_request_rescan(self):
        for marker in ['foreign', 'absent']:
            with self.subTest(marker=marker):
                if marker == 'foreign':
                    self.bind(self.root / 'another-history')
                else:
                    (self.home / '.harbor-catalog-binding.json').unlink()
                self.assertTrue(prepare_catalog(self.home, self.history)['rescan'])

    def test_missing_rollout_is_reported_without_inventing_catalog_rows(self):
        self.add('stale')
        (self.history / 'sessions/stale.jsonl').unlink()
        result = prepare_catalog(self.home, self.history)
        self.assertEqual(result['unavailable'], 1)
        self.assertFalse(result['rescan'])

    def test_fresh_home_does_not_create_a_desktop_database(self):
        home = self.root / 'fresh'
        home.mkdir()
        self.assertFalse(prepare_catalog(home, self.history)['rescan'])
        self.assertFalse((home / 'sqlite').exists())

    def test_unsupported_cache_schema_leaves_database_untouched(self):
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('DROP TABLE local_thread_catalog_sync_state')
        before = self.cache.read_bytes()
        with self.assertRaises(UserError):
            prepare_catalog(self.home, self.history)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_busy_cache_is_not_modified(self):
        self.add('old', compressed=True)
        lock = sqlite3.connect(self.cache)
        lock.execute('BEGIN IMMEDIATE')
        self.addCleanup(lock.close)
        with self.assertRaises(UserError):
            prepare_catalog(self.home, self.history)
        self.assertEqual(self.state()[0], 1)

    def test_symlink_cache_never_writes_another_accounts_database(self):
        other = self.root / 'other.db'
        self.cache.rename(other)
        self.cache.symlink_to(other)
        before = other.read_bytes()
        with self.assertRaises(UserError):
            prepare_catalog(self.home, self.history)
        self.assertEqual(other.read_bytes(), before)

    def test_marker_write_interruption_is_safe_to_repeat(self):
        (self.home / '.harbor-catalog-binding.json').unlink()
        self.add('old', compressed=True)
        with patch('sidebar_catalog.write_json', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(UserError):
                prepare_catalog(self.home, self.history)
        self.assertTrue(prepare_catalog(self.home, self.history)['rescan'])
        self.assertEqual(self.state(), (0, None, None, 77))

    def test_actual_launch_preparation_checks_only_its_own_home(self):
        with patch.object(storage, 'EXISTING_HOMES', {}):
            store = core.Store(self.root / 'control', self.root / 'external',
                               mounted=lambda: True, community=False,
                               user_home=self.root / 'synthetic-user')
            store.initialize_storage('local')
            first = store.save_profile({'kind': 'official', 'name': 'Synthetic first'})
            second = store.save_profile({'kind': 'official', 'name': 'Synthetic active sibling'})
            a = store.prepare(first['id'], 'local', instance_slot='primary')
            b = store.prepare(second['id'], 'local', instance_slot='primary')
            for prepared in (a, b):
                dest = Path(prepared['home']) / 'sqlite/codex-dev.db'
                dest.parent.mkdir(exist_ok=True)
                shutil.copyfile(self.cache, dest)
            self.home = Path(a['home'])
            self.cache = self.home / 'sqlite/codex-dev.db'
            self.history = Path(a['history'])
            shutil.copyfile(self.source, self.history / 'state_5.sqlite')
            self.source = self.history / 'state_5.sqlite'
            self.bind()
            self.add('old-in-another-workspace', compressed=True)
            other = Path(b['home']) / 'sqlite/codex-dev.db'
            protected = other.read_bytes(), self.source.read_bytes()

            store.prepare(first['id'], 'local', instance_slot='primary')

            self.assertEqual(self.state(), (0, None, None, 77))
            self.assertEqual((other.read_bytes(), self.source.read_bytes()), protected)


if __name__ == '__main__':
    unittest.main()
