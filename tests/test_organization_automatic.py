"""Automatic decisions must preserve independent edits and reject stale ABA writes."""
import copy
from contextlib import closing
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from organization_policy import initialize, reconcile


class AutomaticPolicy(unittest.TestCase):
    def setUp(self):
        self.scope = {'data': {'pins': [], 'order': ['a', 'b', 'c'],
                               'names': {'a': 'A', 'b': 'B'}}, 'generation': 0}
        initialize(self.scope)

    def writer(self):
        return {'registration': uuid.uuid4().hex, 'base_versions': copy.deepcopy(self.scope['versions']),
                'snapshot': copy.deepcopy(self.scope['data'])}

    def save(self, writer, edit, **options):
        before = copy.deepcopy(writer['snapshot'])
        after = copy.deepcopy(before)
        edit(after)
        previous_tree=self.scope['data'];previous_value=copy.deepcopy(previous_tree)
        reconcile(self.scope, writer, before, after, **options)
        self.assertEqual(previous_tree,previous_value,'Previous canonical values must remain available for referential validation')
        writer['snapshot'] = after

    def test_independent_pins_survive_without_any_manual_choice(self):
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v['pins'].append('x'))
        self.save(b, lambda v: v['pins'].append('y'))
        self.assertEqual(set(self.scope['data']['pins']), {'x', 'y'})

    def test_old_unseen_pin_then_unpin_cannot_be_resurrected(self):
        old, a = self.writer(), self.writer()
        self.save(a, lambda v: v['pins'].append('x'))
        self.save(a, lambda v: v['pins'].remove('x'))
        self.save(old, lambda v: v['pins'].append('x'))
        self.assertEqual(self.scope['data']['pins'], [])
        self.assertTrue(self.scope['alternatives'])

    def test_rejected_change_does_not_give_old_writer_new_edit_authority(self):
        old, a = self.writer(), self.writer()
        self.save(a, lambda v: v['names'].update(a='New'))
        self.save(old, lambda v: v['names'].update(a='Old edit'))
        self.save(old, lambda v: v['names'].update(a='Another old edit', b='Independent'))
        self.assertEqual(self.scope['data']['names'], {'a': 'New', 'b': 'Independent'})

    def test_explicit_same_value_convergence_authorizes_later_edit_of_that_key(self):
        old, a = self.writer(), self.writer()
        self.save(a, lambda v: v['names'].update(a='New'))
        self.save(old, lambda v: v['names'].update(a='New'))
        self.save(old, lambda v: v['names'].update(a='Next'))
        self.assertEqual(self.scope['data']['names']['a'], 'Next')

    def test_stale_reorder_keeps_committed_order_and_new_members(self):
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v.update(order=['c', 'a', 'b']))
        self.save(b, lambda v: v.update(order=['b', 'a', 'new', 'c']))
        self.assertEqual(self.scope['data']['order'], ['new', 'c', 'a', 'b'])

    def test_independent_insertion_does_not_make_reorder_stale(self):
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v['order'].insert(1, 'new'))
        self.save(b, lambda v: v.update(order=['c', 'a', 'b']))
        self.assertEqual(self.scope['data']['order'], ['c', 'a', 'new', 'b'])

    def test_stale_parent_removal_cannot_erase_a_new_child_edit(self):
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v['names'].update(a='New'))
        self.save(b, lambda v: v.pop('names'))
        self.assertEqual(self.scope['data']['names'], {'a': 'New', 'b': 'B'})

    def test_stale_child_edit_cannot_resurrect_a_removed_parent(self):
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v.pop('names'))
        self.save(b, lambda v: v['names'].update(a='Old edit'))
        self.assertNotIn('names', self.scope['data'])

    def test_legacy_missing_values_are_not_deletions_and_tombstones_win(self):
        a = self.writer()
        self.save(a, lambda v: v['pins'].append('x'))
        self.save(a, lambda v: v['pins'].remove('x'))
        legacy = {'registration': 'legacy', 'base_versions': {}}
        reconcile(self.scope, legacy, {}, {'pins': ['x', 'y'], 'names': {'a': 'Old'}}, legacy=True)
        self.assertEqual(self.scope['data']['pins'], ['y'])
        self.assertEqual(self.scope['data']['names'], {'a': 'A', 'b': 'B'})

    def test_unchanged_save_does_not_touch_versions_or_duplicate_evidence(self):
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v['names'].update(a='New'))
        self.save(b, lambda v: v['names'].update(a='Old'))
        saved = copy.deepcopy(self.scope)
        self.save(b, lambda v: None)
        self.assertEqual(self.scope, saved)

    def test_project_roots_are_one_value_not_a_set_to_union(self):
        self.scope = {'data': {'local-projects': {'a': {'rootPaths': ['/one']}}}}
        initialize(self.scope)
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v['local-projects']['a'].update(rootPaths=['/two']))
        self.save(b, lambda v: v['local-projects']['a'].update(rootPaths=['/three']))
        self.assertEqual(self.scope['data']['local-projects']['a']['rootPaths'], ['/two'])

    def test_omitted_empty_pin_list_removes_own_pin_but_keeps_other_addition(self):
        seed = self.writer()
        self.save(seed, lambda v: v['pins'].append('x'))
        a, b = self.writer(), self.writer()
        self.save(a, lambda v: v['pins'].append('y'))
        self.save(b, lambda v: v.pop('pins'))
        self.assertEqual(self.scope['data']['pins'], ['y'])


class AutomaticWindows(unittest.TestCase):
    from test_shared_workspace import SharedWorkspace as Fixture
    setUp = Fixture.setUp
    window = Fixture.window
    read = Fixture.read
    edit = Fixture.edit

    def test_conflicting_names_no_longer_block_launch(self):
        from organization import Organization
        a, b = self.window(), self.window()
        self.edit(a, lambda v: v['local-projects']['a'].update(name='First'))
        self.edit(b, lambda v: v['local-projects']['a'].update(name='Other'))
        org = Organization(self.store)
        org.publish(a); org.publish(b)
        self.assertEqual(self.read(self.window())['local-projects']['a']['name'], 'First')
        self.assertFalse(org.conflicts(self.history))

    def test_selection_and_expansion_stay_private_to_each_window(self):
        from organization import Organization
        a, b = self.window(), self.window()
        self.edit(a, lambda v: v.update(**{'selected-project': {'type': 'local', 'projectId': 'a'},
             'electron-persisted-atom-state': {'sidebar-project-expanded-v1-codex:a': True}}))
        self.edit(b, lambda v: v.update(**{'selected-project': {'type': 'local', 'projectId': 'b'},
             'electron-persisted-atom-state': {'sidebar-project-expanded-v1-codex:a': False}}))
        org = Organization(self.store)
        org.publish(a); org.publish(b)
        org.prepare(b, self.history)  # Synthetic idle target, never a real window.
        self.assertEqual(self.read(b)['selected-project']['projectId'], 'b')
        self.assertFalse(self.read(b)['electron-persisted-atom-state']['sidebar-project-expanded-v1-codex:a'])

    def test_missing_previously_enrolled_coordinator_fails_closed(self):
        from organization import Organization
        from core import UserError
        self.window()
        org = Organization(self.store)
        org.path.unlink()
        with self.assertRaisesRegex(UserError, '缺失'):
            self.window()

    def test_missing_revision_metadata_is_not_silently_reinitialized(self):
        import json
        from organization import Organization
        from core import UserError, write_json
        self.window();org=Organization(self.store)
        value=json.loads(org.path.read_text())
        next(iter(value['scopes'].values())).pop('versions')
        write_json(org.path,value)
        with self.assertRaisesRegex(UserError,'版本'):
            self.window()

    def test_existing_empty_coordinator_is_corruption_not_a_new_install(self):
        from organization import Organization
        from core import UserError, write_json
        self.window();org=Organization(self.store)
        write_json(org.path,{})
        with self.assertRaisesRegex(UserError,'版本'):
            self.window()

    def test_temporary_source_index_gap_does_not_erase_shared_membership(self):
        import sqlite3
        from organization import Organization
        self.edit(self.history, lambda v: v.update(**{'thread-project-assignments': {
            'old-pin': {'projectKind': 'local', 'projectId': 'a'}}}))
        a = self.window()
        org = Organization(self.store)
        with closing(sqlite3.connect(self.history / 'state_5.sqlite')) as db, db:
            db.execute('DELETE FROM threads')
        org.publish(a)
        with org.transaction() as value:
            self.assertIn('old-pin', next(iter(value['scopes'].values()))['data']['thread-project-assignments'])
        with closing(sqlite3.connect(self.history / 'state_5.sqlite')) as db, db:
            db.execute("INSERT INTO threads VALUES('old-pin')")
        self.assertEqual(self.read(self.window())['thread-project-assignments']['old-pin']['projectId'], 'a')

    def test_stale_move_into_deleted_project_keeps_valid_current_membership(self):
        from organization import Organization
        self.edit(self.history, lambda v: v.update(**{'thread-project-assignments': {
            'old-pin': {'projectKind': 'local', 'projectId': 'a'}}}))
        a,b=self.window(),self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects'].pop('b'));org.publish(a)
        self.edit(b,lambda v:v['thread-project-assignments']['old-pin'].update(projectId='b'));org.publish(b)
        new=self.read(self.window())
        self.assertNotIn('b',new['local-projects'])
        self.assertEqual(new['thread-project-assignments']['old-pin']['projectId'],'a')


if __name__ == '__main__':
    unittest.main()
