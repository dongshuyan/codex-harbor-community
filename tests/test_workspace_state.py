"""Workspace metadata tests use only synthetic homes; no desktop or provider runs."""
import copy
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
import organization
import storage
from organization import Organization


WORKSPACE_KEYS = (
    'local-projects', 'project-order', 'pinned-project-ids',
    'thread-project-assignments', 'pinned-thread-ids',
    'thread-workspace-root-hints', 'sidebar-project-thread-orders',
)
GLOBAL_STATE = '.codex-global-state.json'


class WorkspaceState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.homes = {key: (key, str(self.root / key)) for key in
                      ('original-local', 'original-external')}
        paths = patch.object(storage, 'EXISTING_HOMES', self.homes)
        paths.start()
        self.addCleanup(paths.stop)
        for _, path in self.homes.values():
            home = core.directory(path)
            sessions = core.directory(home / 'sessions')
            (sessions / 'synthetic.jsonl').write_text('SYNTHETIC_SESSION\n')
            (home / 'auth.json').write_text('SYNTHETIC_SOURCE_AUTH\n')
            (home / 'config.toml').write_text('SYNTHETIC_SOURCE_CONFIG\n')
            for filename in ('sessions/synthetic.jsonl', 'auth.json', 'config.toml'):
                self.watch_source(home / filename)
        self.store = core.Store(self.root / 'control', self.root / 'disk', mounted=lambda: True, community=False)
        snapshot = self.store.storage_snapshot()
        for item in snapshot['storages']:
            if item['id'] in self.homes:
                item['enabled'] = True
        self.store.save_storages(snapshot['storages'], snapshot['revision'])

    def watch_source(self, path):
        before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)

        def unchanged():
            after = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
            self.assertEqual(after, before, f'Source file was changed: {path.name}')

        self.addCleanup(unchanged)

    def organization(self, label):
        first, second = f'{label}-alpha', f'{label}-beta'
        first_thread, second_thread = f'{label}-thread-a', f'{label}-thread-b'
        roots = {ident: str(core.directory(self.root / 'projects' / ident))
                 for ident in (first, second)}
        return {
            'local-projects': {
                ident: {'id': ident, 'name': ident, 'rootPaths': [roots[ident]],
                        'createdAt': 1000, 'updatedAt': 2000}
                for ident in (first, second)
            },
            'project-order': [second, first],
            'pinned-project-ids': [second],
            'thread-project-assignments': {
                first_thread: {'projectId': first, 'projectKind': 'local'},
                second_thread: {'projectId': second, 'projectKind': 'local'},
            },
            'pinned-thread-ids': [second_thread],
            'thread-workspace-root-hints': {
                first_thread: roots[first], second_thread: roots[second],
            },
            'sidebar-project-thread-orders': {
                first: {'threadIds':[first_thread]}, second: {'threadIds':[second_thread]},
            },
        }

    def seed_source(self, selection, organization, *, watch=True, **private_fields):
        path = Path(self.homes[selection][1]) / GLOBAL_STATE
        with closing(sqlite3.connect(path.parent/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY)')
            db.executemany('INSERT OR IGNORE INTO threads VALUES(?)',
                           [(v,) for v in organization.get('thread-project-assignments',{})])
        core.write_json(path, dict(organization, **private_fields))
        path.chmod(0o400)
        if watch:
            self.watch_source(path)
        return path

    def prepare_official(self, name, selection='original-local'):
        profile = self.store.save_profile({'name': name, 'kind': 'official'})
        return self.store.prepare(profile['id'], selection)

    def read_state(self, prepared):
        path = Path(prepared['home']) / GLOBAL_STATE
        self.assertTrue(path.is_file(), 'prepare must make project organization visible in CODEX_HOME')
        self.assertFalse(path.is_symlink(), 'The full global state must remain private to each writer')
        return json.loads(path.read_text())

    def assert_organization(self, state, expected):
        self.assertEqual({key: state.get(key) for key in WORKSPACE_KEYS}, expected)

    def test_prepare_exposes_same_location_projects_to_official_accounts_and_api(self):
        expected = self.organization('local')
        source = self.seed_source('original-local', expected, **{
            'electron-persisted-atom-state': {'synthetic-source-account-setting': True},
            'synthetic-unknown-field': {'private': 'SOURCE_ONLY'},
        })
        profiles = [self.store.save_profile({'name': name, 'kind': 'official'})
                    for name in ('Synthetic account A', 'Synthetic account B')]
        profiles.append(self.store.save_profile({
            'name': 'Synthetic API', 'kind': 'api', 'url': 'https://example.invalid/v1',
            'protocol': 'responses', 'api_key': 'SYNTHETIC_API_KEY', 'models': [{
                'alias': 'gpt-6-luna', 'upstream': 'synthetic/model',
                'context': 128000, 'effort': 'low', 'reasoning': 'off',
            }],
        }))
        homes = []
        for profile in profiles:
            with self.subTest(profile=profile['name']):
                prepared = self.store.prepare(profile['id'], 'original-local')
                home = Path(prepared['home'])
                homes.append(home)
                self.assertEqual(Path(prepared['history']), source.parent)
                self.assertEqual((home / 'sessions').resolve(), source.parent / 'sessions')
                state = self.read_state(prepared)
                self.assert_organization(state, expected)
                self.assertNotIn('electron-persisted-atom-state', state)
                self.assertNotIn('synthetic-unknown-field', state)
                self.assertFalse((home / 'auth.json').exists())
                self.assertFalse((home / GLOBAL_STATE).samefile(source))
        self.assertEqual(len(set(homes)), len(profiles))

    def test_prepare_loads_source_metadata_without_replacing_existing_private_state(self):
        profile = self.store.save_profile({'name': 'Synthetic account', 'kind': 'official'})
        prepared = self.store.prepare(profile['id'], 'original-local')
        home = Path(prepared['home'])
        private = {
            'electron-persisted-atom-state': {'synthetic-account-preference': 'KEEP_PRIVATE'},
            'synthetic-private-field': {'permission': 'KEEP_PRIVATE'},
        }
        core.write_json(home / GLOBAL_STATE, private)
        core.write_json(home / 'auth.json', {'synthetic': 'PRIVATE_AUTH'})
        auth_before = (home / 'auth.json').read_bytes()
        expected = self.organization('local')
        self.seed_source('original-local', expected, **{
            'electron-persisted-atom-state': {'synthetic-account-preference': 'SOURCE_ONLY'},
            'synthetic-private-field': {'permission': 'SOURCE_ONLY'},
            'synthetic-plugin-field': {'enabled': True},
        })

        updated = self.store.prepare(profile['id'], 'original-local')

        state = self.read_state(updated)
        self.assert_organization(state, expected)
        for key, value in private.items():
            self.assertEqual(state[key], value)
        self.assertNotIn('synthetic-plugin-field', state)
        self.assertEqual((home / 'auth.json').read_bytes(), auth_before)

    def test_switching_location_replaces_old_projects_and_clears_them_for_empty_history(self):
        local = self.organization('local')
        external = self.organization('external')
        self.seed_source('original-local', local)
        self.seed_source('original-external', external)
        self.store.initialize_storage('local')
        profile = self.store.save_profile({'name': 'Synthetic account', 'kind': 'official'})
        prepared = self.store.prepare(profile['id'], 'original-local')
        home = Path(prepared['home'])
        private = {'electron-persisted-atom-state': {'synthetic-account-preference': 'KEEP_PRIVATE'}}
        # Model an existing desktop's previous-location state even on the broken version.
        core.write_json(home / GLOBAL_STATE, dict(local, **private))

        for selection, expected in (
                ('original-external', external), ('local', None), ('original-local', local)):
            with self.subTest(selection=selection):
                switched = self.store.prepare(profile['id'], selection)
                self.assertEqual(Path(switched['home']), home)
                state = self.read_state(switched)
                if expected is None:
                    for key in WORKSPACE_KEYS:
                        self.assertFalse(state.get(key), f'Previous location leaked through {key}')
                else:
                    self.assert_organization(state, expected)
                for key, value in private.items():
                    self.assertEqual(state[key], value)

    def test_stale_windows_merge_independent_edits_without_rewriting_open_homes(self):
        original = self.organization('local')
        self.seed_source('original-local', original)
        first = self.prepare_official('Synthetic account A')
        second = self.prepare_official('Synthetic account B')
        first_state, second_state = self.read_state(first), self.read_state(second)
        first_state['local-projects']['local-alpha']['name'] = 'Renamed by A'
        added = copy.deepcopy(first_state['local-projects']['local-alpha'])
        added.update(id='local-gamma', name='Created by A',
                     rootPaths=[str(core.directory(self.root / 'projects/local-gamma'))])
        first_state['local-projects']['local-gamma'] = added
        first_state['project-order'].append('local-gamma')
        first_state['synthetic-private-field'] = 'ACCOUNT_A_ONLY'
        first_path = Path(first['home']) / GLOBAL_STATE
        core.write_json(first_path, first_state)
        Organization(self.store).publish()

        # B still holds the snapshot from before A's additions and rename.
        second_state['local-projects']['local-beta']['name'] = 'Renamed by B'
        second_state['pinned-project-ids'] = []
        second_state['synthetic-private-field'] = 'ACCOUNT_B_ONLY'
        second_path = Path(second['home']) / GLOBAL_STATE
        core.write_json(second_path, second_state)
        open_homes = {path: path.read_bytes() for path in (first_path, second_path)}
        Organization(self.store).publish()

        merged = self.read_state(self.prepare_official('Synthetic account C'))

        self.assertEqual(merged['local-projects']['local-alpha']['name'], 'Renamed by A')
        self.assertEqual(merged['local-projects']['local-beta']['name'], 'Renamed by B')
        self.assertEqual(merged['local-projects']['local-gamma'], added)
        self.assertEqual(merged['project-order'], ['local-beta', 'local-alpha', 'local-gamma'])
        self.assertEqual(merged['pinned-project-ids'], [])
        self.assertNotIn('synthetic-private-field', merged)
        self.assertEqual({path: path.read_bytes() for path in open_homes}, open_homes)

    def test_stale_window_edit_does_not_revive_a_deleted_project(self):
        original = self.organization('local')
        self.seed_source('original-local', original)
        first = self.prepare_official('Synthetic account A')
        second = self.prepare_official('Synthetic account B')
        first_state, stale_state = self.read_state(first), self.read_state(second)
        del first_state['local-projects']['local-beta']
        first_state['project-order'] = ['local-alpha']
        first_state['pinned-project-ids'] = []
        del first_state['thread-project-assignments']['local-thread-b']
        del first_state['sidebar-project-thread-orders']['local-beta']
        core.write_json(Path(first['home']) / GLOBAL_STATE, first_state)
        Organization(self.store).publish()

        stale_state['local-projects']['local-beta']['name'] = 'Stale rename must not revive'
        stale_state['local-projects']['local-alpha']['name'] = 'Independent surviving edit'
        stale_state['project-order'] = ['local-alpha', 'local-beta']
        core.write_json(Path(second['home']) / GLOBAL_STATE, stale_state)
        Organization(self.store).publish()

        # A stale rename cannot revive the deletion. Resolve automatically and
        # retain evidence, without asking the user or blocking a fresh window.
        org=Organization(self.store)
        conflicts=org.conflicts(Path(self.homes['original-local'][1]))
        self.assertFalse(conflicts)
        with org.transaction() as value:
            self.assertTrue(any(s['alternatives'] for s in value['scopes'].values()))
        merged = self.read_state(self.prepare_official('Synthetic account C'))

        self.assertNotIn('local-beta', merged['local-projects'])
        self.assertEqual(merged['project-order'], ['local-alpha'])
        self.assertEqual(merged['pinned-project-ids'], [])
        self.assertNotIn('local-thread-b', merged['thread-project-assignments'])
        self.assertNotIn('local-beta', merged['sidebar-project-thread-orders'])
        self.assertEqual(merged['local-projects']['local-alpha']['name'], 'Independent surviving edit')

    def test_external_source_updates_merge_without_writing_back_harbor_edits(self):
        original = self.organization('local')
        source = self.seed_source('original-local', original, watch=False)
        source_before = (source.read_bytes(), source.stat().st_ino, source.stat().st_mtime_ns)
        first = self.prepare_official('Synthetic account A')
        first_state = self.read_state(first)
        first_state['local-projects']['local-alpha']['name'] = 'Harbor edit'
        core.write_json(Path(first['home']) / GLOBAL_STATE, first_state)
        Organization(self.store).publish()
        self.assertEqual((source.read_bytes(), source.stat().st_ino, source.stat().st_mtime_ns),
                         source_before)

        # Only this fixture write simulates an independent native desktop update.
        external_update = copy.deepcopy(original)
        external_update['local-projects']['local-beta']['name'] = 'External source edit'
        external_update['synthetic-private-field'] = 'SOURCE_ONLY'
        core.write_json(source, external_update)
        source.chmod(0o400)
        self.watch_source(source)

        merged = self.read_state(self.prepare_official('Synthetic account B'))
        Organization(self.store).publish()

        self.assertEqual(merged['local-projects']['local-alpha']['name'], 'Harbor edit')
        self.assertEqual(merged['local-projects']['local-beta']['name'], 'External source edit')
        self.assertNotIn('synthetic-private-field', merged)

    def test_same_account_keeps_edited_organization_separate_when_switching_locations(self):
        local, external = self.organization('local'), self.organization('external')
        self.seed_source('original-local', local)
        self.seed_source('original-external', external)
        prepared = self.prepare_official('Synthetic account')
        home = Path(prepared['home'])
        local_edit = self.read_state(prepared)
        local_edit['local-projects']['local-alpha']['name'] = 'Local edit'
        local_edit['pinned-project-ids'] = ['local-alpha']
        core.write_json(home / GLOBAL_STATE, local_edit)
        Organization(self.store).publish()

        switched = self.store.prepare(prepared['id'], 'original-external')
        self.assertEqual(Path(switched['home']), home)
        self.assert_organization(self.read_state(switched), external)
        external_edit = self.read_state(switched)
        external_edit['local-projects']['external-beta']['name'] = 'External edit'
        external_edit['pinned-project-ids'] = []
        core.write_json(home / GLOBAL_STATE, external_edit)
        Organization(self.store).publish()

        for selection, expected in (('original-local', local_edit),
                                    ('original-external', external_edit)):
            with self.subTest(selection=selection):
                result = self.store.prepare(prepared['id'], selection)
                self.assertEqual(Path(result['home']), home)
                self.assert_organization(self.read_state(result), expected)

    def test_upgrade_from_empty_legacy_private_state_retains_original_projects(self):
        original = self.organization('local')
        self.seed_source('original-local', original)
        # The old launcher bound history without creating an organization journal.
        with patch.object(Organization, 'prepare', return_value=None):
            legacy = self.prepare_official('Synthetic legacy account')
        home = Path(legacy['home'])
        core.write_json(home / GLOBAL_STATE, {
            'local-projects': {}, 'project-order': [], 'pinned-project-ids': [],
            'electron-persisted-atom-state': {'synthetic-private-preference': 'KEEP_PRIVATE'},
        })

        upgraded = self.store.prepare(legacy['id'], 'original-local')

        state = self.read_state(upgraded)
        self.assert_organization(state, original)
        self.assertEqual(state['electron-persisted-atom-state'],
                         {'synthetic-private-preference': 'KEEP_PRIVATE'})

    def test_two_windows_first_projects_merge_when_organization_fields_were_absent(self):
        projects = self.organization('local')['local-projects']
        first = self.prepare_official('Synthetic account A')
        second = self.prepare_official('Synthetic account B')
        self.assertNotIn('local-projects', self.read_state(first))
        self.assertNotIn('local-projects', self.read_state(second))

        for prepared, project_id in ((first, 'local-alpha'), (second, 'local-beta')):
            core.write_json(Path(prepared['home']) / GLOBAL_STATE, {
                'local-projects': {project_id: projects[project_id]},
                'project-order': [project_id], 'pinned-project-ids': [project_id],
            })
            Organization(self.store).publish()

        merged = self.read_state(self.prepare_official('Synthetic account C'))

        self.assertEqual(merged['local-projects'], projects)
        self.assertEqual(set(merged['project-order']), set(projects))
        self.assertEqual(set(merged['pinned-project-ids']), set(projects))

    def assert_interrupted_switch_recovers_without_cross_location_edits(self, after_home_write):
        local, external = self.organization('local'), self.organization('external')
        self.seed_source('original-local', local)
        self.seed_source('original-external', external)
        prepared = self.prepare_official('Synthetic interrupted account')
        home_state = Path(prepared['home']) / GLOBAL_STATE
        local_edit = self.read_state(prepared)
        local_edit['local-projects']['local-alpha']['name'] = 'Unsaved local edit'
        local_edit['synthetic-private-field'] = 'KEEP_PRIVATE'
        core.write_json(home_state, local_edit)
        write_json = organization.write_json
        home_was_written = False

        def fail_at_switch_boundary(path, value):
            nonlocal home_was_written
            if Path(path) == home_state:
                if not after_home_write:
                    raise OSError('SYNTHETIC_INTERRUPTED_SWITCH')
                write_json(path, value)
                home_was_written = True
            elif home_was_written:
                raise OSError('SYNTHETIC_INTERRUPTED_SWITCH')
            else:
                write_json(path, value)

        with patch('organization.write_json', side_effect=fail_at_switch_boundary):
            with self.assertRaisesRegex(OSError, 'SYNTHETIC_INTERRUPTED_SWITCH'):
                self.store.prepare(prepared['id'], 'original-external')
        self.assertEqual(home_was_written, after_home_write)

        # Recovery may run during another account's startup or an exit callback.
        Organization(self.store).publish()
        local_reader = self.prepare_official('Synthetic local reader')
        external_reader = self.prepare_official('Synthetic external reader', 'original-external')
        expected_local = {key: local_edit[key] for key in WORKSPACE_KEYS}
        self.assert_organization(self.read_state(local_reader), expected_local)
        self.assert_organization(self.read_state(external_reader), external)
        retried = self.store.prepare(prepared['id'], 'original-external')
        state = self.read_state(retried)
        self.assert_organization(state, external)
        self.assertEqual(state['synthetic-private-field'], 'KEEP_PRIVATE')

    def test_interrupted_switch_before_private_write_recovers_without_mixing_locations(self):
        self.assert_interrupted_switch_recovers_without_cross_location_edits(False)

    def test_interrupted_switch_after_private_write_recovers_without_mixing_locations(self):
        self.assert_interrupted_switch_recovers_without_cross_location_edits(True)

    def test_malformed_unrelated_writer_does_not_block_or_contaminate_other_history(self):
        external = self.organization('external')
        self.seed_source('original-external', external)
        bad_writer = self.prepare_official('Synthetic malformed writer')
        bad_path = Path(bad_writer['home']) / GLOBAL_STATE
        core.write_json(bad_path, {'local-projects': [], 'synthetic-private-field': 'KEEP_ORIGINAL'})
        bad_before = bad_path.read_bytes()

        unrelated = self.prepare_official('Synthetic unrelated reader', 'original-external')

        self.assert_organization(self.read_state(unrelated), external)
        self.assertEqual(bad_path.read_bytes(), bad_before)

    def test_prepare_rejects_malformed_own_private_state_without_overwriting_it(self):
        prepared = self.prepare_official('Synthetic malformed writer')
        path = Path(prepared['home']) / GLOBAL_STATE
        core.write_json(path, {'local-projects': [], 'synthetic-private-field': 'KEEP_ORIGINAL'})
        before = path.read_bytes()

        with self.assertRaises(core.UserError):
            self.store.prepare(prepared['id'], 'original-local')

        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
