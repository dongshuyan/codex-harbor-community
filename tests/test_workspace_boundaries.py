"""Organization upgrade boundaries use synthetic files and disk identities only."""
import json
from pathlib import Path
import sys
import tempfile
import sqlite3
from contextlib import closing
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import storage
from organization import Organization, STATE


class WorkspaceBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.native = core.directory(self.root / 'synthetic-mount' / 'history')
        self.volume = 'SYNTHETIC_VOLUME_A'
        homes = patch.object(storage, 'EXISTING_HOMES', {
            'original-local': ('Synthetic original', str(self.native)),
        })
        homes.start()
        self.addCleanup(homes.stop)
        identity = patch.object(storage, 'volume_identity', side_effect=lambda path:
                                self.volume if Path(path) == self.native else None)
        identity.start()
        self.addCleanup(identity.stop)
        self.store = core.Store(self.root / 'control', self.root / 'external', mounted=lambda: True, community=False)
        snapshot = self.store.storage_snapshot()
        next(row for row in snapshot['storages'] if row['id'] == 'original-local')['enabled'] = True
        self.store.save_storages(snapshot['storages'], snapshot['revision'])
        sessions = core.directory(self.native / 'sessions')
        self.history_file = sessions / 'synthetic.jsonl'
        self.history_file.write_text('SYNTHETIC_HISTORY_UNCHANGED\n')
        self.watch_unchanged(self.history_file)

    def watch_unchanged(self, path):
        before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)

        def verify():
            self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), before)

        self.addCleanup(verify)

    def profile(self, name, kind='official'):
        value = {'name': name, 'kind': kind}
        if kind == 'api':
            value.update(url='https://example.invalid/v1', protocol='responses',
                         api_key='SYNTHETIC_BOUNDARY_KEY', models=[{
                             'alias': 'gpt-6-luna', 'upstream': 'synthetic/boundary',
                             'context': 128000, 'effort': 'low', 'reasoning': 'off',
                         }])
        return self.store.save_profile(value)

    def metadata(self, label, history=None):
        project, thread = label + '-project', label + '-thread'
        history=core.directory(history or self.native)
        with closing(sqlite3.connect(history/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY)')
            db.execute('INSERT OR IGNORE INTO threads VALUES(?)',(thread,))
        root = str(self.root / 'projects' / label)
        return {
            'local-projects': {project: {'id': project, 'name': label, 'rootPaths': [root]}},
            'project-order': [project], 'pinned-project-ids': [project],
            'thread-project-assignments': {thread: {'projectId': project, 'projectKind': 'local'}},
            'pinned-thread-ids': [thread], 'sidebar-project-thread-orders': {project: {'threadIds':[thread]}},
        }

    def read_state(self, prepared):
        return json.loads((Path(prepared['home']) / STATE).read_text())

    def assert_metadata(self, prepared, expected):
        state = self.read_state(prepared)
        self.assertEqual({key: state.get(key) for key in expected}, expected)
        self.assertNotIn('synthetic-account-only', state)

    def legacy_home(self, kind):
        profile = self.profile('Synthetic legacy ' + kind, kind)
        # The old launcher created the native history links but no organization journal.
        with patch.object(Organization, 'prepare', return_value=None):
            prepared = self.store.prepare(profile['id'], 'original-local')
        home = Path(prepared['home'])
        metadata = self.metadata('volume-A')
        core.write_json(home / STATE, dict(metadata, **{'synthetic-account-only': 'KEEP_PRIVATE'}))
        return profile, prepared, metadata

    def replace_volume_registration(self):
        self.store.registry.remove('original-local', self.store.storage_snapshot()['revision'])
        self.volume = 'SYNTHETIC_VOLUME_B'
        snapshot = self.store.storage_snapshot()
        ident = 'b' * 32
        snapshot['storages'].append({
            'id': ident, 'name': 'Synthetic replacement disk', 'path': str(self.native), 'enabled': True,
        })
        self.store.save_storages(snapshot['storages'], snapshot['revision'])
        self.assertEqual(self.store.registry.entry(ident)['volume_uuid'], self.volume)
        return ident

    def retarget(self, profile, path):
        snapshot = self.store.storage_snapshot()
        next(row for row in snapshot['storages'] if row['id'] == 'original-local')['path'] = str(path)
        self.store.save_storages(snapshot['storages'], snapshot['revision'])
        self.store.initialize_storage('original-local')
        return self.store.prepare(profile['id'], 'original-local')

    def test_nested_null_in_current_home_is_rejected_without_overwrite(self):
        profile = self.profile('Synthetic malformed current')
        prepared = self.store.prepare(profile['id'], 'original-local')
        path = Path(prepared['home']) / STATE
        core.write_json(path, {'electron-persisted-atom-state': {
            'unified-sidebar-pinned-order-v1': None,
        }, 'synthetic-account-only': 'KEEP_PRIVATE'})
        self.watch_unchanged(path)

        with self.assertRaises(core.UserError):
            self.store.prepare(profile['id'], 'original-local')

    def test_corrupt_untracked_legacy_writer_for_selected_history_is_not_silently_skipped(self):
        _,old,_=self.legacy_home('official')
        path=Path(old['home'])/STATE;core.write_json(path,{'local-projects':{'broken':None}})
        self.watch_unchanged(path)
        reader=self.profile('Synthetic new reader')
        with self.assertRaises(core.UserError):self.store.prepare(reader['id'],'original-local')

    def test_nested_null_in_other_writer_does_not_block_another_history(self):
        profile = self.profile('Synthetic malformed other')
        prepared = self.store.prepare(profile['id'], 'original-local')
        path = Path(prepared['home']) / STATE
        core.write_json(path, {'electron-persisted-atom-state': {
            'unified-sidebar-pinned-order-v1': None,
        }, 'synthetic-account-only': 'KEEP_PRIVATE'})
        self.watch_unchanged(path)
        self.store.initialize_storage('local')
        other_history = core.directory(self.store.registry.history_path(self.store.registry.entry('local')))
        expected = self.metadata('other-history',other_history)
        core.write_json(other_history / STATE, expected)
        self.watch_unchanged(other_history / STATE)
        reader = self.profile('Synthetic unaffected reader')

        output = self.store.prepare(reader['id'], 'local')

        self.assert_metadata(output, expected)

    def test_legacy_official_home_is_not_adopted_into_replacement_volume_at_same_path(self):
        _, legacy, _ = self.legacy_home('official')
        self.watch_unchanged(Path(legacy['home']) / STATE)
        replacement = self.replace_volume_registration()
        expected = self.metadata('volume-B')
        core.write_json(self.native / STATE, expected)
        self.watch_unchanged(self.native / STATE)
        reader = self.profile('Synthetic volume B reader')

        output = self.store.prepare(reader['id'], replacement)

        self.assert_metadata(output, expected)

    def test_retired_api_runtime_remains_associated_with_its_original_volume(self):
        _, legacy, original = self.legacy_home('api')
        self.watch_unchanged(Path(legacy['home']) / STATE)
        replacement = self.replace_volume_registration()
        expected = self.metadata('volume-B')
        source = self.native / STATE
        core.write_json(source, expected)
        source_before = (source.read_bytes(), source.stat().st_ino, source.stat().st_mtime_ns)
        reader = self.profile('Synthetic replacement reader')

        output = self.store.prepare(reader['id'], replacement)

        self.assert_metadata(output, expected)
        self.assertEqual((source.read_bytes(), source.stat().st_ino, source.stat().st_mtime_ns), source_before)
        self.store.registry.remove(replacement, self.store.storage_snapshot()['revision'])
        self.volume = 'SYNTHETIC_VOLUME_A'
        self.store.registry.restore('original-local', self.store.storage_snapshot()['revision'])
        # Reconnecting A replaces B's synthetic source state with A's original empty state.
        core.write_json(source, {})
        self.watch_unchanged(source)
        original_reader = self.profile('Synthetic original volume reader')

        restored = self.store.prepare(original_reader['id'], 'original-local')

        self.assert_metadata(restored, original)

    def assert_legacy_retarget_roundtrip(self, kind):
        profile, legacy, original = self.legacy_home(kind)
        changed = self.retarget(profile, self.root / 'replacement-history')
        self.assertFalse(self.read_state(changed).get('local-projects'))
        if kind == 'api':
            self.assertNotEqual(changed['home'], legacy['home'])
            self.watch_unchanged(Path(legacy['home']) / STATE)
        changed_metadata = self.metadata('replacement-history',self.root/'replacement-history')
        core.write_json(Path(changed['home']) / STATE, changed_metadata)
        Organization(self.store).publish()

        restored = self.retarget(profile, self.native)

        state = self.read_state(restored)
        self.assertEqual({key: state.get(key) for key in original}, original)
        changed_again = self.retarget(profile, self.root / 'replacement-history')
        state = self.read_state(changed_again)
        self.assertEqual({key: state.get(key) for key in changed_metadata}, changed_metadata)

    def test_legacy_official_retarget_roundtrip_keeps_each_history_metadata(self):
        self.assert_legacy_retarget_roundtrip('official')

    def test_v1_scope_recovers_volume_identity_before_first_retarget(self):
        profile=self.profile('Synthetic upgraded primary')
        original=self.metadata('original')
        core.write_json(self.native/STATE,original)
        self.store.prepare(profile['id'],'original-local')
        org=Organization(self.store);value=json.loads(org.path.read_text());value['version']=1
        for scope in value['scopes'].values():scope.pop('volume_uuid',None)
        for writer in value['writers'].values():writer['baseline']=writer.pop('ack_snapshot')
        core.write_json(org.path,value)
        changed=self.retarget(profile,self.root/'replacement-history')
        self.assertFalse(self.read_state(changed).get('local-projects'))
        restored=self.retarget(profile,self.native)
        self.assertEqual(self.read_state(restored)['local-projects'],original['local-projects'])

    def test_legacy_api_retarget_roundtrip_keeps_each_history_metadata(self):
        self.assert_legacy_retarget_roundtrip('api')


if __name__ == '__main__':
    unittest.main()
