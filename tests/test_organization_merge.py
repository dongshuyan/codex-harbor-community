import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from organization_merge import merge


class OrganizationMerge(unittest.TestCase):
    def test_unchanged_old_snapshot_cannot_undo_a_new_pin(self):
        value, conflicts = merge({'pins':['a','b']}, {'pins':['a']}, {'pins':['a']})
        self.assertEqual(value, {'pins':['a','b']}); self.assertFalse(conflicts)

    def test_acknowledged_pin_is_not_replayed_after_another_writer_removes_it(self):
        value, conflicts = merge({'pins':[]}, {'pins':['a']}, {'pins':['a']})
        self.assertEqual(value, {'pins':[]}); self.assertFalse(conflicts)

    def test_independent_membership_edits_merge_with_removals(self):
        value, conflicts = merge({'pins':['a','b','c']}, {'pins':['a','b']}, {'pins':['b','d']})
        self.assertEqual(set(value['pins']), {'b','c','d'}); self.assertFalse(conflicts)

    def test_one_reorder_and_other_insert_both_survive(self):
        value, conflicts = merge({'order':['a','b','c','x']},
                                 {'order':['a','b','c']}, {'order':['b','a','c']})
        self.assertEqual(value['order'], ['b','a','c','x']); self.assertFalse(conflicts)

    def test_two_different_reorders_preserve_both_in_conflict(self):
        value, conflicts = merge({'order':['b','a','c']},
                                 {'order':['a','b','c']}, {'order':['a','c','b']})
        self.assertEqual(value['order'], ['b','a','c'])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['path'], ['order'])
        self.assertEqual(conflicts[0]['incoming']['value'], ['a','c','b'])

    def test_same_new_value_is_idempotent_not_a_conflict(self):
        self.assertEqual(merge({'name':'new'}, {'name':'old'}, {'name':'new'}), ({'name':'new'}, []))

    def test_conflict_does_not_discard_independent_changes(self):
        value, conflicts = merge({'projects':{'a':{'name':'A'},'b':{'name':'B'}}},
                                  {'projects':{'a':{'name':'old'},'b':{'name':'B'}}},
                                  {'projects':{'a':{'name':'other'},'b':{'name':'new B'}}})
        self.assertEqual(value['projects']['b']['name'], 'new B')
        self.assertEqual(value['projects']['a']['name'], 'A')
        self.assertEqual(conflicts[0]['path'], ['projects','a','name'])

    def test_delete_versus_edit_is_not_silently_lost(self):
        value, conflicts = merge({'projects':{}}, {'projects':{'a':{'name':'old'}}},
                                 {'projects':{'a':{'name':'edited'}}})
        self.assertEqual(value, {'projects':{}})
        self.assertEqual(conflicts[0]['current'], {'present':False})

    def test_absence_is_not_confused_with_null(self):
        value, conflicts = merge({'a':None}, {'a':'old'}, {})
        self.assertEqual(value, {'a':None}); self.assertTrue(conflicts)
        self.assertEqual(conflicts[0]['incoming'], {'present':False})

    def test_independent_first_projects_merge_when_field_absent(self):
        self.assertEqual(merge({'projects':{'a':1}}, {}, {'projects':{'b':2}}),
                         ({'projects':{'a':1,'b':2}}, []))

    def test_metadata_update_time_does_not_invent_a_user_conflict(self):
        value, conflicts = merge({'local-projects':{'a':{'updatedAt':3,'name':'A'}}},
                                 {'local-projects':{'a':{'updatedAt':1,'name':'A'}}},
                                 {'local-projects':{'a':{'updatedAt':2,'name':'A','rootPaths':['/b']}}})
        self.assertEqual(value['local-projects']['a']['updatedAt'], 3); self.assertFalse(conflicts)

    def test_inputs_are_not_modified_and_large_membership_is_bounded(self):
        before={'pins':[str(i) for i in range(20000)]}
        current=copy.deepcopy(before);current['pins'].append('other')
        after=copy.deepcopy(before);after['pins'].remove('10')
        saved=copy.deepcopy((current,before,after))
        value, conflicts = merge(current,before,after)
        self.assertEqual(len(value['pins']),20000);self.assertFalse(conflicts)
        self.assertEqual((current,before,after), saved)


if __name__=='__main__':unittest.main()
