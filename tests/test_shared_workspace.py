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
from organization import Organization, STATE


class SharedWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.store=core.Store(self.root/'control',self.root/'external',mounted=lambda:False, community=False)
        self.store.initialize_storage('local')
        self.history=self.store.registry.history_path(self.store.registry.entry('local'))
        core.directory(self.history)
        with closing(sqlite3.connect(self.history/'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY)')
            db.execute("INSERT INTO threads VALUES('old-pin')")
        self.initial={'local-projects':{'a':{'id':'a','name':'Alpha','rootPaths':['/synthetic/a']},
                                        'b':{'id':'b','name':'Beta','rootPaths':['/synthetic/b']}},
                      'pinned-project-ids':['a'],'project-order':['a','b']}
        core.write_json(self.history/STATE,self.initial)

    def window(self):
        p=self.store.save_profile({'kind':'official','name':'Synthetic'})
        return Path(self.store.prepare(p['id'],'local')['home'])

    def read(self,home):return json.loads((home/STATE).read_text())

    def edit(self,home,func):
        value=self.read(home);func(value);core.write_json(home/STATE,value)

    def test_local_to_account_private_is_not_a_shared_deletion(self):
        self.edit(self.history,lambda v:v.update(**{'pinned-thread-ids':['old-pin'],
            'thread-project-assignments':{'old-pin':{'projectKind':'local','projectId':'a'}},
            'electron-persisted-atom-state':{'unified-sidebar-pinned-order-v1':[
                'codex:project:a','codex:thread:local:old-pin']}}))
        a=self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(chatGptBacking={'id':'PRIVATE'}))
        org.publish(a)
        newer=self.read(self.window())
        self.assertEqual(newer['local-projects']['a'],self.initial['local-projects']['a'])
        self.assertEqual(newer['pinned-project-ids'],['a'])
        self.assertEqual(newer['pinned-thread-ids'],['old-pin'])
        self.assertEqual(newer['thread-project-assignments']['old-pin']['projectId'],'a')
        self.assertEqual(newer['electron-persisted-atom-state']['unified-sidebar-pinned-order-v1'],
                         ['codex:project:a','codex:thread:local:old-pin'])

    def test_private_to_local_rejoin_cannot_silently_replace_shared_rename(self):
        a=self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(chatGptBacking={'id':'PRIVATE'}))
        org.publish(a)
        b=self.window();self.edit(b,lambda v:v['local-projects']['a'].update(name='Shared new name'));org.publish(b)
        org.publish(a)  # An observation while private must not authorize an old name later.
        self.edit(a,lambda v:v['local-projects']['a'].pop('chatGptBacking'))
        org.publish(a)
        self.assertEqual(self.read(self.window())['local-projects']['a']['name'],'Shared new name')
        with org.transaction() as value:
            self.assertEqual(next(iter(value['scopes'].values()))['data']['local-projects']['a']['name'],'Shared new name')

    def test_selecting_local_after_cloud_keeps_unrelated_edits_automatic(self):
        a=self.window();org=Organization(self.store)
        self.edit(a,lambda v:v.update(**{'selected-project':{'type':'remote','projectId':'remote'}}));org.publish(a)
        self.edit(a,lambda v:(v.update(**{'selected-project':{'type':'local','projectId':'b'}}),
                             v['local-projects']['b'].update(name='Normal edit')))
        org.publish(a)
        self.assertFalse(org.conflicts(self.history))
        self.assertEqual(self.read(self.window())['local-projects']['b']['name'],'Normal edit')

    def test_rejoin_conflict_does_not_hold_back_unrelated_project_edits(self):
        a=self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(chatGptBacking={'id':'PRIVATE'}));org.publish(a)
        b=self.window();self.edit(b,lambda v:v['local-projects']['a'].update(name='Shared newer'));org.publish(b);org.publish(a)
        self.edit(a,lambda v:(v['local-projects']['a'].pop('chatGptBacking'),v['local-projects']['b'].update(name='Independent')))
        org.publish(a)
        with org.transaction() as value:
            data=next(iter(value['scopes'].values()))['data']
            self.assertEqual(data['local-projects']['b']['name'],'Independent')
            self.assertEqual(data['local-projects']['a']['name'],'Shared newer')
        self.assertFalse(org.conflicts(self.history))

    def test_removing_private_project_is_not_deleting_its_shared_local_counterpart(self):
        a=self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(chatGptBacking={'id':'PRIVATE'}));org.publish(a)
        self.edit(a,lambda v:v['local-projects'].pop('a'));org.publish(a);org.publish(a)
        self.assertIn('a',self.read(self.window())['local-projects'])

    def test_canonical_only_membership_leaving_private_scope_is_not_a_rejoin(self):
        a=self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(chatGptBacking={'id':'PRIVATE'}));org.publish(a)
        b=self.window()
        self.edit(b,lambda v:v.update(**{'thread-project-assignments':{'old-pin':{'projectKind':'local','projectId':'a'}}}));org.publish(b);org.publish(a)
        self.edit(b,lambda v:v['thread-project-assignments'].clear());org.publish(b);org.publish(a)
        self.edit(a,lambda v:v['local-projects']['b'].update(name='Independent edit'));org.publish(a)
        self.assertFalse(org.conflicts(self.history))
        self.assertEqual(self.read(self.window())['local-projects']['b']['name'],'Independent edit')

    def test_first_projection_write_failure_recovers_a_previously_absent_state(self):
        home=core.directory(self.root/'fresh-window');org=Organization(self.store)
        original=organization.write_json
        def fail(path,value):
            if path==home/STATE:raise OSError('synthetic full disk')
            return original(path,value)
        with patch('organization.write_json',side_effect=fail):
            with self.assertRaises(OSError):org.prepare(home,self.history)
        self.assertFalse((home/STATE).exists())
        org.prepare(home,self.history)
        self.assertEqual(self.read(home)['local-projects'],self.initial['local-projects'])

    def test_pending_existing_empty_state_is_not_mistaken_for_initial_absence(self):
        home=core.directory(self.root/'existing-empty');core.write_json(home/STATE,{})
        org=Organization(self.store);original=organization.write_json
        def fail(path,value):
            if path==home/STATE:raise OSError('synthetic full disk')
            return original(path,value)
        with patch('organization.write_json',side_effect=fail):
            with self.assertRaises(OSError):org.prepare(home,self.history)
        (home/STATE).unlink()
        with self.assertRaisesRegex(core.UserError,'缺失'):org.prepare(home,self.history)

    def test_old_ack_does_not_resurrect_pin_after_other_writer_unpins(self):
        a,b=self.window(),self.window()
        self.edit(a,lambda v:v['pinned-project-ids'].append('b'))
        Organization(self.store).publish(a)
        c=self.window()
        self.edit(c,lambda v:v['pinned-project-ids'].remove('b'))
        Organization(self.store).publish(c)
        self.edit(a,lambda v:v.update(windowWidth=900))
        Organization(self.store).publish(a)
        self.assertEqual(self.read(self.window())['pinned-project-ids'],['a'])
        self.assertEqual(self.read(b)['pinned-project-ids'],['a'])

    def alternatives(self,org):
        with org.transaction() as value:
            return copy.deepcopy(list(next(iter(value['scopes'].values()))['alternatives'].values()))

    def test_conflicting_edit_is_retained_without_blocking_launch_or_touching_writers(self):
        a,b=self.window(),self.window()
        self.edit(a,lambda v:v['local-projects']['a'].update(name='First rename'))
        self.edit(b,lambda v:v['local-projects']['a'].update(name='Other rename'))
        org=Organization(self.store)
        org.publish(a);org.publish(b)
        rows=self.alternatives(org)
        self.assertEqual(len(rows),1)
        org.publish(b)
        self.assertEqual(self.alternatives(org),rows)
        before=(a/STATE).read_bytes(),(b/STATE).read_bytes()
        self.assertEqual(self.read(self.window())['local-projects']['a']['name'],'First rename')
        self.assertEqual(((a/STATE).read_bytes(),(b/STATE).read_bytes()),before)
        self.assertFalse(org.conflicts(self.history))

    def test_pending_conflict_tracks_later_writer_edit_without_duplicate_choice(self):
        a,b=self.window(),self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(name='First'))
        self.edit(b,lambda v:v['local-projects']['a'].update(name='Second'))
        org.publish(a);org.publish(b)
        old=self.alternatives(org)[0]
        self.edit(b,lambda v:v['local-projects']['a'].update(name='Corrected second'))
        org.publish(b)
        rows=self.alternatives(org)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['incoming']['value'],'Corrected second')
        self.assertNotEqual(old['incoming'],rows[0]['incoming'])
        self.assertEqual(self.read(self.window())['local-projects']['a']['name'],'First')

    def test_completed_native_pin_migration_is_not_replayed_by_a_fresh_window(self):
        self.edit(self.history,lambda v:v.update(**{'pinned-thread-ids':['old-pin']}))
        a=self.window();org=Organization(self.store)
        # Native API migration completed, followed by native unpin. Legacy JSON
        # still contains old-pin; propagating it re-pins it in every fresh home.
        self.edit(a,lambda v:v.update(**{'app-server-migrated-pinned-thread-ids-by-host':{
            'local:'+str(a):['old-pin']}}))
        org.publish(a)
        b=self.window()
        self.assertEqual(self.read(b).get('pinned-thread-ids',[]),[])
        self.assertNotIn('app-server-migrated-pinned-thread-ids-by-host',self.read(b))

    def test_old_launch_baseline_is_not_treated_as_confirmed_ack_on_upgrade(self):
        a=self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['pinned-project-ids'].append('b'))
        org.publish(a)
        value=json.loads(org.path.read_text());value['version']=1
        for scope in value['scopes'].values():
            scope.pop('versions',None);scope.pop('clock',None);scope.pop('alternatives',None)
        for writer in value['writers'].values():
            writer['baseline']=writer.pop('ack_snapshot')
        value['writers'][str(a)]['baseline']['pinned-project-ids']=['a']
        next(iter(value['scopes'].values()))['data']['pinned-project-ids']=['a']
        core.write_json(org.path,value)
        before=(a/STATE).read_bytes()
        self.window()
        with org.transaction() as value:
            self.assertEqual(next(iter(value['scopes'].values()))['data']['pinned-project-ids'],['a','b'])
        self.assertEqual((a/STATE).read_bytes(),before)
        self.assertFalse(org.conflicts(self.history))

    def test_idle_native_entry_receives_harbor_pins_then_becomes_a_tracked_writer(self):
        a=self.window();self.edit(a,lambda v:v['pinned-project-ids'].append('b'))
        self.edit(self.history,lambda v:v.update(**{'permission':'KEEP','plugin-private':{'x':1}}))
        org=Organization(self.store)
        with patch('native_profile.require_idle') as check:
            org.prepare_original(self.history)
            check.assert_called_once()
        value=self.read(self.history)
        self.assertEqual(value['pinned-project-ids'],['a','b'])
        self.assertEqual(value['permission'],'KEEP');self.assertEqual(value['plugin-private'],{'x':1})
        self.edit(self.history,lambda v:v['pinned-project-ids'].remove('a'))
        self.assertEqual(self.read(self.window())['pinned-project-ids'],['b'])

    def test_unknown_native_occupancy_never_writes_original_state(self):
        a=self.window();self.edit(a,lambda v:v['pinned-project-ids'].append('b'))
        before=(self.history/STATE).read_bytes()
        with patch('native_profile.require_idle',side_effect=core.UserError('busy')):
            with self.assertRaisesRegex(core.UserError,'busy'):
                Organization(self.store).prepare_original(self.history)
        self.assertEqual((self.history/STATE).read_bytes(),before)

    def test_relevant_corrupt_writer_blocks_instead_of_showing_empty_history(self):
        a=self.window();(a/STATE).write_text('{invalid')
        with self.assertRaises(core.UserError):self.window()
        self.assertEqual((a/STATE).read_text(),'{invalid')

    def test_private_cloud_and_unknown_fields_survive_projection(self):
        a=self.window()
        self.edit(a,lambda v:v['local-projects'].update(cloud={'id':'cloud','chatGptBacking':{'private':'KEEP'}}))
        self.edit(a,lambda v:v['pinned-project-ids'].append('cloud'))
        self.edit(a,lambda v:v.update(**{'electron-persisted-atom-state':{
            'unified-sidebar-pinned-order-v1':['chatgpt:private','codex:project:a'],
            'private-atom':True}}))
        Organization(self.store).prepare(a,self.history)
        v=self.read(a)
        self.assertEqual(v['local-projects']['cloud']['chatGptBacking'],{'private':'KEEP'})
        self.assertIn('cloud',v['pinned-project-ids'])
        self.assertIn('chatgpt:private',v['electron-persisted-atom-state']['unified-sidebar-pinned-order-v1'])
        self.assertTrue(v['electron-persisted-atom-state']['private-atom'])
        other=self.read(self.window())
        self.assertNotIn('cloud',other.get('local-projects',{}))

    def test_unassigned_cloud_thread_references_stay_in_their_own_home(self):
        a=self.window()
        self.edit(a,lambda v:v.update(**{
            'pinned-thread-ids':['old-pin','private-cloud-thread'],
            'projectless-thread-ids':['private-cloud-thread'],
            'thread-workspace-root-hints':{'old-pin':'/synthetic/a','private-cloud-thread':'PRIVATE'},
            'sidebar-project-thread-orders':{'a':{'threadIds':['old-pin','private-cloud-thread']}},
            'electron-persisted-atom-state':{
                'unified-sidebar-pinned-order-v1':['codex:thread:local:old-pin',
                                                 'codex:thread:local:private-cloud-thread'],
                'sidebar-collapsed-sections-v1':{'private-section':True}}}))
        before=(a/STATE).read_bytes()
        b=self.read(self.window())
        self.assertEqual(b['pinned-thread-ids'],['old-pin'])
        self.assertEqual(b['projectless-thread-ids'],[])
        self.assertEqual(b['sidebar-project-thread-orders'],{'a':{'threadIds':['old-pin']}})
        self.assertNotIn('PRIVATE',json.dumps(b))
        self.assertNotIn('private-cloud-thread',json.dumps(b))
        self.assertNotIn('private-section',json.dumps(b))
        self.assertEqual((a/STATE).read_bytes(),before)
        Organization(self.store).prepare(a,self.history)
        self.assertIn('private-cloud-thread',self.read(a)['pinned-thread-ids'])

    def test_native_thread_order_object_and_sort_key_share_only_local_threads(self):
        a=self.window()
        self.edit(a,lambda v:v.update(**{'sidebar-project-thread-orders':{'a':{
            'threadIds':['old-pin','private-cloud-thread'],'sortKey':'created_at','unknownPrivate':'KEEP'}}}))
        b=self.read(self.window())
        self.assertEqual(b['sidebar-project-thread-orders']['a'],
                         {'threadIds':['old-pin'],'sortKey':'created_at'})
        Organization(self.store).prepare(a,self.history)
        saved=self.read(a)['sidebar-project-thread-orders']['a']
        self.assertEqual(saved['unknownPrivate'],'KEEP')
        self.assertIn('private-cloud-thread',saved['threadIds'])
        self.assertEqual(saved['sortKey'],'created_at')

    def test_unknown_project_alias_is_never_shared(self):
        self.edit(self.history,lambda v:v.update(**{organization.MAP_KEY:{
            'local:'+str(self.history):{'a':'native-a','private-cloud':'native-private'}}}))
        projected=self.read(self.window())
        mapping=next(iter(projected[organization.MAP_KEY].values()))
        self.assertEqual(mapping,{'a':'native-a'})

    def test_later_stale_parent_deletion_does_not_erase_newer_child(self):
        a,b=self.window(),self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(name='First'))
        self.edit(b,lambda v:v['local-projects']['a'].update(name='Second'))
        org.publish(a);org.publish(b)
        self.edit(b,lambda v:v['local-projects'].pop('a'))
        org.publish(b)
        rows=self.alternatives(org)
        parent=next(r for r in rows if r['path']==['local-projects','a'])
        self.assertEqual(parent['incoming'],{'present':False})
        self.assertEqual(self.read(self.window())['local-projects']['a']['name'],'First')

    def test_stale_parent_delete_and_child_edit_leave_whole_valid_project(self):
        a,b,c=self.window(),self.window(),self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['local-projects']['a'].update(name='First'))
        self.edit(b,lambda v:v['local-projects']['a'].update(name='Second'))
        self.edit(c,lambda v:v['local-projects'].pop('a'))
        org.publish(a);org.publish(b);org.publish(c)
        project=self.read(self.window())['local-projects']['a']
        self.assertEqual(project,dict(self.initial['local-projects']['a'],name='First'))
        self.assertFalse(org.conflicts(self.history))

    def test_stale_assignment_removal_retains_newer_membership_without_user_choice(self):
        with closing(sqlite3.connect(self.history/'state_5.sqlite')) as db,db:
            db.execute('ALTER TABLE threads ADD COLUMN name TEXT')
            db.execute("UPDATE threads SET name='Readable test chat'")
        self.edit(self.history,lambda v:v.update(**{'thread-project-assignments':{
            'old-pin':{'projectKind':'local','projectId':'a'}}}))
        a,b=self.window(),self.window();org=Organization(self.store)
        self.edit(a,lambda v:v['thread-project-assignments']['old-pin'].update(projectId='b'))
        self.edit(b,lambda v:v['thread-project-assignments'].pop('old-pin'))
        org.publish(a);org.publish(b)
        self.assertEqual(self.read(self.window())['thread-project-assignments']['old-pin']['projectId'],'b')
        self.assertFalse(org.conflict_view(self.history))

    def test_same_binding_keeps_native_migration_checkpoints_when_pins_change(self):
        a=self.window();identity='local:'+str(a)
        markers={identity:{'version':1,'projectsMigrated':True,'threadAssignmentsMigrated':False}}
        migrated={identity:['thread-previously-migrated']}
        self.edit(a,lambda v:v.update(**{organization.CHECKPOINT_KEYS[0]:markers,
                                         organization.CHECKPOINT_KEYS[1]:migrated}))
        b=self.window();self.edit(b,lambda v:v['pinned-project-ids'].append('b'))
        Organization(self.store).prepare(a,self.history)
        self.assertEqual(self.read(a)[organization.CHECKPOINT_KEYS[0]],markers)
        self.assertEqual(self.read(a)[organization.CHECKPOINT_KEYS[1]],migrated)


if __name__=='__main__':unittest.main()
