"""Native migration checkpoints belong to one private home and history binding.

All homes, metadata and storage locations are synthetic. These tests neither
start the desktop/app-server nor open session bodies or a real state database.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import storage
from organization import MAP_KEY, Organization, STATE


PROJECT_CHECKPOINT = 'app-server-projects-migration-by-host'
PIN_CHECKPOINT = 'app-server-migrated-pinned-thread-ids-by-host'


class WorkspaceLoading(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='harbor-workspace-loading-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        original_homes = patch.object(storage, 'EXISTING_HOMES', {})
        original_homes.start()
        self.addCleanup(original_homes.stop)
        self.store = core.Store(self.root / 'control', self.root / 'external',
                                mounted=lambda: True, community=False,
                                user_home=self.root / 'synthetic-user')
        self.store.initialize_storage('local')
        self.store.initialize_storage('external')
        self.history = self.store.registry.history_path(self.store.registry.entry('local'))
        self.metadata = {
            'local-projects': {
                'synthetic-project': {'id': 'synthetic-project', 'name': 'Synthetic project',
                                      'rootPaths': [str(self.root / 'workspace')]},
            },
            'project-order': ['synthetic-project'],
            'thread-project-assignments': {
                'synthetic-thread': {'projectId': 'synthetic-project', 'projectKind': 'local'},
            },
            'pinned-thread-ids': ['synthetic-thread'],
        }
        core.write_json(self.history / STATE, self.metadata)

    def prepare_profile(self, kind='official', name='Synthetic account'):
        profile = {'kind': kind, 'name': name}
        if kind == 'api':
            profile.update(url='https://example.invalid/v1', protocol='responses',
                           api_key='SYNTHETIC_UNUSED_KEY', models=[{
                               'alias': 'gpt-6-luna', 'upstream': 'synthetic/unused',
                               'context': 128000, 'effort': 'low', 'reasoning': 'off',
                           }])
        saved = self.store.save_profile(profile)
        prepared = self.store.prepare(saved['id'], 'local')
        return saved, prepared

    def state(self, prepared):
        return json.loads((Path(prepared['home']) / STATE).read_text())

    def checkpoint(self, home):
        identity = 'local:' + str(home)
        return {
            PROJECT_CHECKPOINT: {identity: {
                'version': 1, 'projectsMigrated': True,
                'threadAssignmentsMigrated': True, 'threadAssignmentsReadMigrated': True,
                'pendingThreadAssignmentIds': [],
            }},
            PIN_CHECKPOINT: {identity: ['synthetic-thread']},
        }

    def desktop_completed_migration(self, prepared):
        home = Path(prepared['home'])
        state = self.state(prepared)
        state[MAP_KEY] = {'local:' + str(home): {'synthetic-project': 'synthetic-server-project'}}
        state.update(self.checkpoint(home))
        core.write_json(home / STATE, state)
        return self.checkpoint(home)

    def test_unchanged_history_reuses_only_its_private_completed_checkpoints(self):
        for kind in ('official', 'api'):
            with self.subTest(kind=kind):
                profile, prepared = self.prepare_profile(kind, 'Synthetic ' + kind)
                expected = self.desktop_completed_migration(prepared)
                original_source = (self.history / STATE).read_bytes()

                restarted = self.store.prepare(profile['id'], 'local')

                self.assertEqual(restarted['home'], prepared['home'])
                self.assertEqual(restarted['history'], prepared['history'])
                actual = self.state(restarted)
                for key, value in expected.items():
                    self.assertEqual(actual.get(key), value,
                                     'A no-edit restart must not repeat native full-history migration')
                self.assertEqual((self.history / STATE).read_bytes(), original_source)

    def test_switching_history_drops_checkpoint_even_when_organization_is_identical(self):
        profile, prepared = self.prepare_profile()
        self.desktop_completed_migration(prepared)
        other_history = self.store.registry.history_path(self.store.registry.entry('external'))
        core.write_json(other_history / STATE, self.metadata)

        switched = self.store.prepare(profile['id'], 'external')

        self.assertEqual(switched['home'], prepared['home'])
        self.assertNotEqual(switched['history'], prepared['history'])
        state = self.state(switched)
        self.assertNotIn(PROJECT_CHECKPOINT, state)
        self.assertNotIn(PIN_CHECKPOINT, state)

    def test_reuse_drops_checkpoints_for_foreign_host_identities(self):
        profile, prepared = self.prepare_profile()
        expected = self.desktop_completed_migration(prepared)
        state = self.state(prepared)
        for field in (PROJECT_CHECKPOINT, PIN_CHECKPOINT):
            state[field]['local:/synthetic-other-home'] = copy.deepcopy(
                next(iter(expected[field].values())))
        core.write_json(Path(prepared['home']) / STATE, state)

        restarted = self.store.prepare(profile['id'], 'local')

        state = self.state(restarted)
        for field, checkpoint in expected.items():
            self.assertEqual(state.get(field), checkpoint)

    def test_unfinished_prior_binding_does_not_reuse_checkpoint(self):
        profile, prepared = self.prepare_profile()
        self.desktop_completed_migration(prepared)
        journal = self.store.root / 'organization' / 'state.json'
        value = json.loads(journal.read_text())
        writer = value['writers'][prepared['home']]
        writer['phase'] = 'pending'
        writer['baseline'] = {'projectless-thread-ids': ['unwritten-preparation']}
        core.write_json(journal, value)

        restarted = self.store.prepare(profile['id'], 'local')

        state = self.state(restarted)
        self.assertNotIn(PROJECT_CHECKPOINT, state)
        self.assertNotIn(PIN_CHECKPOINT, state)

    def test_source_assignment_change_invalidates_private_project_checkpoint(self):
        profile, prepared = self.prepare_profile()
        self.desktop_completed_migration(prepared)
        changed = copy.deepcopy(self.metadata)
        changed['thread-project-assignments'] = {}
        changed['projectless-thread-ids'] = ['synthetic-thread']
        core.write_json(self.history / STATE, changed)

        restarted = self.store.prepare(profile['id'], 'local')

        state = self.state(restarted)
        self.assertEqual(state['thread-project-assignments'], {})
        self.assertEqual(state['projectless-thread-ids'], ['synthetic-thread'])
        self.assertNotIn(PROJECT_CHECKPOINT, state)

    def test_other_writer_assignment_change_invalidates_reader_checkpoint(self):
        profile, reader = self.prepare_profile(name='Synthetic reader')
        self.desktop_completed_migration(reader)
        _, writer = self.prepare_profile(name='Synthetic writer')
        changed = self.state(writer)
        changed['thread-project-assignments'] = {}
        changed['projectless-thread-ids'] = ['synthetic-thread']
        core.write_json(Path(writer['home']) / STATE, changed)
        Organization(self.store).publish()

        restarted = self.store.prepare(profile['id'], 'local')

        state = self.state(restarted)
        self.assertEqual(state['thread-project-assignments'], {})
        self.assertEqual(state['projectless-thread-ids'], ['synthetic-thread'])
        self.assertNotIn(PROJECT_CHECKPOINT, state)

    def test_new_home_does_not_import_source_or_other_home_checkpoints(self):
        source = copy.deepcopy(self.metadata)
        source.update(self.checkpoint(self.history))
        core.write_json(self.history / STATE, source)
        _, first = self.prepare_profile(name='Synthetic first')
        self.desktop_completed_migration(first)
        Organization(self.store).publish()

        _, second = self.prepare_profile(name='Synthetic second')

        state = self.state(second)
        self.assertNotIn(PROJECT_CHECKPOINT, state)
        self.assertNotIn(PIN_CHECKPOINT, state)
        self.assertEqual(state['local-projects'], self.metadata['local-projects'])


if __name__ == '__main__':
    unittest.main()
