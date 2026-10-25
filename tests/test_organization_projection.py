"""Native-shaped metadata: corrupted owned values fail, opaque fields stay private."""
import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from core import UserError
from organization_projection import projection,apply_projection,MAP_KEY,CHECKPOINT_KEYS,EXPANDED


class Projection(unittest.TestCase):
    def seed(self):
        return {'local-projects':{'p':{'id':'p','name':'Project','rootPaths':['/synthetic'],
                                     'createdAt':1,'updatedAt':1}},
                'thread-project-assignments':{'t':{'projectKind':'local','projectId':'p'}},
                'pinned-thread-ids':['t']}
    def project(self,value):return projection(value,'/synthetic/home',{'t'})

    def test_malformed_owned_values_stop_instead_of_becoming_deletions(self):
        fields=[('local-projects',{'p':None}),
                ('thread-project-assignments',{'t':None}),
                ('thread-project-assignments',{'t':{'projectKind':'future','projectId':'p'}}),
                ('pinned-project-ids',['p',1]),('pinned-thread-ids',['t',False]),
                ('selected-project',[]),('thread-workspace-root-hints',{'t':{'unexpected':1}}),
                ('selected-project',{'projectId':'p'}),('selected-project',{'type':5,'projectId':'p'}),
                ('local-projects',{'p':{'id':'different','name':'Wrong','rootPaths':[]}}),
                ('thread-project-membership-host-ids',{'t':None}),('project-appearances',{'p':None}),
                ('electron-persisted-atom-state',{'flat-project-sidebar-preferences-v1':{'initialized':1}}),
                (MAP_KEY,{'local:/synthetic/home':{'p':None}}),
                (CHECKPOINT_KEYS[0],{'local:/synthetic/home':{'threadAssignmentsMigrated':'true'}})]
        for key,value in fields:
            state=self.seed();state[key]=value
            with self.subTest(field=key,value=value),self.assertRaises(UserError):self.project(state)

    def test_dangling_local_assignment_does_not_remove_local_pin_or_root(self):
        state=self.seed();state['thread-project-assignments']['t']['projectId']='removed-project'
        state['thread-workspace-root-hints']={'t':'/synthetic'}
        got=self.project(state)
        self.assertEqual(got['thread-project-assignments'],{})
        self.assertEqual(got['pinned-thread-ids'],['t'])
        self.assertEqual(got['thread-workspace-root-hints'],{'t':'/synthetic'})

    def test_explicit_account_origin_stays_private_even_with_local_sql_id(self):
        state=self.seed();state['thread-project-assignments']['t']['projectOrigin']='chatgpt'
        got=self.project(state)
        self.assertEqual(got['thread-project-assignments'],{})
        self.assertEqual(got['pinned-thread-ids'],[])

    def test_unknown_nested_fields_stay_in_writer_and_never_cross_accounts(self):
        state=self.seed()
        state['local-projects']['p']['privateProject']='PRIVATE'
        state['thread-project-assignments']['t']['privateAssignment']='PRIVATE'
        state['selected-project']={'type':'local','projectId':'p','privateSelected':'PRIVATE'}
        state['project-appearances']={'p':{'color':'blue','marker':{'kind':'emoji','emoji':'x','privateMarker':'PRIVATE'},'privateStyle':'PRIVATE'}}
        got=self.project(state)
        self.assertNotIn('PRIVATE',json.dumps(got))
        newer=copy.deepcopy(got);newer['local-projects']['p']['name']='Updated'
        saved=apply_projection(state,newer,'/synthetic/home',same_binding=True,local_ids={'t'})
        self.assertEqual(saved['local-projects']['p']['privateProject'],'PRIVATE')
        self.assertEqual(saved['thread-project-assignments']['t']['privateAssignment'],'PRIVATE')
        self.assertEqual(saved['selected-project']['privateSelected'],'PRIVATE')
        self.assertEqual(saved['project-appearances']['p']['privateStyle'],'PRIVATE')
        self.assertEqual(saved['project-appearances']['p']['marker']['privateMarker'],'PRIVATE')
        self.assertEqual(saved['local-projects']['p']['name'],'Updated')

    def test_newer_membership_mode_never_bypasses_gate_with_non_boolean_value(self):
        for invalid in (1,0,'false',None):
            state=self.seed();state[CHECKPOINT_KEYS[0]]={'local:/synthetic/home':{'threadAssignmentsReadMigrated':invalid}}
            with self.subTest(invalid=invalid),self.assertRaises(UserError):self.project(state)

    def test_target_private_project_and_remote_assignment_survive_same_id_projection(self):
        for backing in (True,False):
            private=self.seed();incoming=self.project(self.seed())
            private['selected-project']={'type':'remote','projectId':'remote'}
            private['thread-project-assignments']['t']={'projectKind':'remote','projectId':'remote','hostId':'server'}
            private['thread-workspace-root-hints']={'t':'/private'}
            private['pinned-thread-ids']=[]
            if backing:
                private['local-projects']['p']['chatGptBacking']={'id':'PRIVATE'}
                private['project-appearances']={'p':{'private':'KEEP'}}
                private['electron-persisted-atom-state']={EXPANDED+'p':False}
                incoming['project-appearances']={'p':{'color':'blue','marker':{'kind':'emoji','emoji':'x'}}}
                incoming['electron-persisted-atom-state']={EXPANDED+'p':True}
            got=apply_projection(private,incoming,'/synthetic/home',same_binding=True,local_ids={'t'})
            for key in ('selected-project','thread-project-assignments','thread-workspace-root-hints','pinned-thread-ids'):
                self.assertEqual(got[key],private[key])
            if backing:
                for key in ('local-projects','project-appearances','electron-persisted-atom-state'):
                    self.assertEqual(got[key],private[key])

    def test_private_list_positions_are_retained_between_local_neighbors(self):
        state=self.seed();state['local-projects'].update({'q':{'id':'q','name':'Other','rootPaths':[]},
            'cloud':{'id':'cloud','chatGptBacking':{'id':'PRIVATE'}}})
        state['project-order']=['p','cloud','q']
        projected=self.project(state)
        got=apply_projection(state,projected,'/synthetic/home',same_binding=True,local_ids={'t'})
        self.assertEqual(got['project-order'],state['project-order'])

    def test_first_projection_preserves_account_private_project_native_mapping(self):
        state=self.seed();state['local-projects']['p']['chatGptBacking']={'id':'PRIVATE'}
        state[MAP_KEY]={'local:/synthetic/home':{'p':'private-native-id'}}
        result=apply_projection(state,{},'/synthetic/home',same_binding=False,local_ids={'t'})
        self.assertEqual(result[MAP_KEY],state[MAP_KEY])
