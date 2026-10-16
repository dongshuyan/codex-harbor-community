"""Per-history desktop organization, without sharing a whole writable Codex home.

Native desktop state is an atomic whole-file writer with no cross-process merge.
At launch/exit, merge only each writer's changes since its last snapshot. Running
windows are never rewritten. Original state is a read-only input, not an output.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

from core import ID, UserError, directory, write_json

STATE = '.codex-global-state.json'
MAP_KEY = 'app-server-project-id-by-legacy-project-id-by-host'
CHECKPOINT_KEYS = ('app-server-projects-migration-by-host',
                   'app-server-migrated-pinned-thread-ids-by-host')
HOST_KEYS = (MAP_KEY, *CHECKPOINT_KEYS, 'app-server-pending-project-deletions-by-host')
LIST_KEYS = ('project-order', 'pinned-project-ids', 'pinned-thread-ids',
             'electron-saved-workspace-roots', 'active-workspace-roots', 'projectless-thread-ids')
DICT_KEYS = ('local-projects', 'thread-project-assignments', 'thread-workspace-root-hints',
             'thread-projectless-output-directories', 'sidebar-project-thread-orders',
             'project-appearances', 'electron-workspace-root-labels', 'thread-project-membership-host-ids')
ATOM_KEYS = ('flat-project-sidebar-preferences-v1', 'unified-sidebar-pinned-order-v1',
             'unified-sidebar-project-order-v1', 'sidebar-collapsed-sections-v1')
EXPANDED = 'sidebar-project-expanded-v1-codex:'
ABSENT = object()


def read_object(path):
    from storage import safe_path
    safe_path(path.parent)
    try:
        fd=os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UserError('项目组织配置不是普通文件，已停止启动。')
        if os.fstat(fd).st_size > 32*1024*1024:
            raise UserError('项目组织配置过大，已保留原文件并停止启动。')
        with os.fdopen(fd, 'r') as handle:
            fd=None
            value=json.load(handle)
        if not isinstance(value,dict): raise ValueError()
        return value
    except (ValueError,UnicodeError):
        raise UserError('项目组织配置无法读取；未覆盖，请先检查该文件。') from None
    finally:
        if fd is not None: os.close(fd)


def projection(value, home):
    """Never carry auth, permissions, plugins or account-bound cloud projects."""
    result={}
    for key,kind in [(k,list) for k in LIST_KEYS]+[(k,dict) for k in DICT_KEYS]:
        if key in value:
            if not isinstance(value[key],kind): raise UserError('项目组织字段格式异常，未覆盖。')
            result[key]=copy.deepcopy(value[key])
    projects=result.get('local-projects',{})
    excluded=set()
    for ident,item in list(projects.items()):
        if not isinstance(item,dict) or any(k in item for k in ('chatGptBacking','chatGptBackingCreation','retiredChatGptBacking')):
            excluded.add(ident);del projects[ident];continue
        projects[ident]={k:copy.deepcopy(item[k]) for k in ('id','name','rootPaths','createdAt','updatedAt') if k in item}
    for key in ('project-order','pinned-project-ids'):
        if key in result: result[key]=[v for v in result[key] if isinstance(v,str) and v not in excluded]
    assignments=result.get('thread-project-assignments',{})
    hidden_threads=set()
    for ident,item in list(assignments.items()):
        if not isinstance(item,dict) or item.get('projectKind')!='local' or item.get('projectId') in excluded:
            hidden_threads.add(ident);del assignments[ident]
    for key in ('pinned-thread-ids','projectless-thread-ids'):
        if key in result:result[key]=[v for v in result[key] if isinstance(v,str) and v not in hidden_threads]
    for key in ('thread-workspace-root-hints','thread-projectless-output-directories'):
        if key in result:result[key]={k:v for k,v in result[key].items() if k not in hidden_threads}
    if 'thread-project-membership-host-ids' in result:
        result['thread-project-membership-host-ids']={k:v for k,v in result['thread-project-membership-host-ids'].items() if v=='local' and k not in hidden_threads}
    for key in ('sidebar-project-thread-orders','project-appearances'):
        if key in result:result[key]={k:v for k,v in result[key].items() if k not in excluded}
    selected=value.get('selected-project')
    if isinstance(selected,dict) and selected.get('type')=='local' and selected.get('projectId') in projects:
        result['selected-project']=copy.deepcopy(selected)
    maps=value.get(MAP_KEY,{})
    if not isinstance(maps,dict):raise UserError('项目映射格式异常，未覆盖。')
    mapping=maps.get('local:'+str(home),{})
    if not isinstance(mapping,dict):raise UserError('项目映射格式异常，未覆盖。')
    if mapping:result['__project-id-map']={k:v for k,v in mapping.items() if k not in excluded and isinstance(v,str)}
    atoms=value.get('electron-persisted-atom-state',{})
    if not isinstance(atoms,dict):raise UserError('项目界面配置格式异常，未覆盖。')
    if isinstance(atoms,dict):
        chosen={k:copy.deepcopy(v) for k,v in atoms.items() if k in ATOM_KEYS or k.startswith(EXPANDED) and k[len(EXPANDED):] in projects}
        for key in ('unified-sidebar-pinned-order-v1','unified-sidebar-project-order-v1'):
            if key in chosen:
                if not isinstance(chosen[key],list):raise UserError('项目排序配置格式异常，未覆盖。')
                chosen[key]=[v for v in chosen[key] if isinstance(v,str) and
                             (v.startswith('codex:project:') and v[len('codex:project:'):] in projects or
                              v.startswith('codex:thread:local:') and v[len('codex:thread:local:'):] not in hidden_threads)]
        if chosen:result['electron-persisted-atom-state']=chosen
    return result


def merge_changes(current, before, after):
    """Three-way delta: unchanged old fields cannot overwrite newer writers.

    Deletion wins over an edit to an already removed map entry; independent map
    changes survive. Ordered lists apply changed membership and latest observed
    ordering, without reviving items another writer removed.
    """
    result=copy.deepcopy(current)
    for key in before.keys() | after.keys():
        old=before.get(key,ABSENT);new=after.get(key,ABSENT)
        if old==new:continue
        present=result.get(key,ABSENT)
        if new is ABSENT:
            result.pop(key,None)
        elif present is ABSENT and old is not ABSENT:
            continue
        elif isinstance(new,dict) and (isinstance(old,dict) or old is ABSENT) and isinstance(present,dict):
            result[key]=merge_changes(present,{} if old is ABSENT else old,new)
        elif isinstance(new,list) and (isinstance(old,list) or old is ABSENT) and isinstance(present,list) and all(isinstance(v,str) for v in new+([] if old is ABSENT else old)+present):
            if old is ABSENT:old=[]
            removed=set(old)-set(new)
            retained=[v for v in present if v not in removed]
            ordered=[v for v in new if v in retained or v not in old]
            result[key]=list(dict.fromkeys(ordered+[v for v in retained if v not in ordered]))
        else:
            result[key]=copy.deepcopy(new)
    return result


class Organization:
    def __init__(self,store):
        self.store=store
        self.root=directory(store.root/'organization')
        self.path=self.root/'state.json'

    @contextmanager
    def transaction(self):
        lock=self.root/'state.lock'
        fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            value=read_object(self.path) or {'version':1,'scopes':{},'writers':{}}
            if value.get('version')!=1 or not isinstance(value.get('scopes'),dict) or not isinstance(value.get('writers'),dict):
                raise UserError('项目组织缓存版本异常，已停止启动。')
            yield value
            write_json(self.path,value)

    def identities(self,history):
        registry=self.store.registry.read()
        identities={s.get('volume_uuid','') for s in registry.get('history_bindings',[]) if s['path']==str(history)}
        for location in registry['storages']+registry.get('removed_storages',[]):
            if (location.get('enabled') or location.get('volume_uuid')) and str(self.store.registry.history_path(location))==str(history):
                identities.add(location.get('volume_uuid',''))
        return identities

    def scope(self,value,history,volume_uuid=None):
        if volume_uuid is None:
            matches=[s.get('volume_uuid','') for s in self.store.registry.entries()
                     if str(self.store.registry.history_path(s))==str(history)]
            if len(matches)!=1:raise UserError('项目组织目录归属不明确，已停止启动。')
            volume_uuid=matches[0]
        key=hashlib.sha256((str(history)+'\0'+volume_uuid).encode()).hexdigest()
        return key,value['scopes'].setdefault(key,{'history':str(history),'data':{},'source':{}})

    def writer(self,home,key,baseline,phase):
        from storage import volume_identity
        return dict(home=str(home),scope=key,baseline=baseline,phase=phase,
                    home_volume_uuid=volume_identity(Path(home)))

    def adopt_legacy(self,value,known):
        # Before the first 0.8 launch, accounts/APIs can already have private
        # projects. Discover only Harbor-owned homes, including retired runtimes.
        candidates={home:[] for home in (self.store.root/'accounts').glob('*/codex')}
        for location in self.store.registry.credential_locations():
            if self.store.registry.disk_available(location):
                for home in (Path(location['path'])/'instances').glob('*/codex'):
                    candidates.setdefault(home,[]).append(location)
        for home,locations in candidates.items():
            if not ID.fullmatch(home.parent.name) or str(home) in value['writers'] or not (home/'sessions').is_symlink():continue
            prior=(home/'sessions').resolve().parent
            if str(prior) not in known:continue
            bindings={s['history_volume_uuid'] for s in locations if s.get('history_path')==str(prior)}
            if not bindings:bindings=self.identities(prior)
            # A path reused by another disk cannot identify an old official
            # home. Preserve its file/backup instead of guessing the owner.
            if len(bindings)!=1:continue
            try:
                state=read_object(home/STATE)
                if state is None:continue
                data=projection(state,home)
            except (OSError,UserError):continue
            key,scope=self.scope(value,prior,next(iter(bindings)))
            scope['data']=merge_changes(scope['data'],{},data)
            value['writers'][str(home)]=self.writer(home,key,data,'ready')

    def harvest(self,value):
        # All paths were recorded by prepare, never accepted from UI input.
        for writer in value['writers'].values():
            try:
                from storage import volume_identity
                mounted=volume_identity(Path(writer['home']))
                if mounted is not None and (not mounted or mounted!=writer.get('home_volume_uuid')):continue
                state=read_object(Path(writer['home'])/STATE)
                after=projection(state,writer['home']) if state is not None else None
            except (OSError,UserError):
                # Offline legacy runtime roots cannot block another location.
                # Do not advance their baseline or treat unreadable as deletion.
                continue
            if state is None:continue
            if writer.get('phase')=='pending':
                # A crash between journal and private-file writes is not a user
                # edit. Only a completed write may publish to the new location.
                if after==writer['baseline']:writer['phase']='ready'
                else:continue
            scope=value['scopes'][writer['scope']]
            scope['data']=merge_changes(scope['data'],writer['baseline'],after)
            writer['baseline']=after

    def prepare(self,home,history):
        home=Path(home);history=Path(history)
        with self.transaction() as value:
            known={str(self.store.registry.history_path(s)) for s in self.store.registry.entries()}
            known.update(self.store.registry.read().get('retired_histories',[]))
            self.adopt_legacy(value,known)
            self.harvest(value)
            key,scope=self.scope(value,history)
            source=read_object(history/STATE)
            if source is not None:
                observed=projection(source,history)
                scope['data']=merge_changes(scope['data'],scope['source'],observed)
                scope['source']=observed
            private=read_object(home/STATE) or {}
            baseline=projection(private,home)  # Reject corrupt state before overwriting it.
            projected=copy.deepcopy(scope['data'])
            writer=self.writer(home,key,baseline,'pending')
            previous=value['writers'].get(str(home),{})
            checkpoints={}
            # Native migration scans every thread when these private markers
            # disappear. Reuse only this home's unchanged, completed binding;
            # never import completion from the source or another writer.
            if (previous.get('scope')==key and previous.get('phase')=='ready' and
                    previous.get('home_volume_uuid')==writer['home_volume_uuid'] and
                    writer['home_volume_uuid']!='' and baseline==projected):
                identity='local:'+str(home)
                for field,kind in zip(CHECKPOINT_KEYS,(dict,list)):
                    entries=private.get(field)
                    if isinstance(entries,dict) and isinstance(entries.get(identity),kind):
                        checkpoints[field]={identity:copy.deepcopy(entries[identity])}
            for field in (*LIST_KEYS,*DICT_KEYS,'selected-project',*HOST_KEYS):private.pop(field,None)
            atoms=private.get('electron-persisted-atom-state',{})
            if isinstance(atoms,dict):
                for atom in list(atoms):
                    if atom in ATOM_KEYS or atom.startswith(EXPANDED):atoms.pop(atom)
            shared_atoms=projected.pop('electron-persisted-atom-state',{})
            if shared_atoms:
                if not isinstance(atoms,dict):atoms={}
                atoms.update(shared_atoms);private['electron-persisted-atom-state']=atoms
            mapping=projected.pop('__project-id-map',{})
            if mapping:private[MAP_KEY]={'local:'+str(home):mapping}
            private.update(projected)
            private.update(checkpoints)
            # Keep a one-time rollback of this private state, never source auth.
            backup=home/'.harbor-organization-before.json'
            if (home/STATE).exists() and not backup.exists():write_json(backup,read_object(home/STATE))
            # Commit a pending baseline first. If interrupted, recovery must not
            # interpret the old location's state as edits to the new location.
            writer['baseline']=projection(private,home)
            value['writers'][str(home)]=writer
            write_json(self.path,value)
            write_json(home/STATE,private)
            value['writers'][str(home)]['phase']='ready'

    def publish(self):
        with self.transaction() as value:self.harvest(value)
