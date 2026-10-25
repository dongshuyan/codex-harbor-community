"""Per-history desktop organization, without sharing a whole writable Codex home.

Native desktop state is an atomic whole-file writer with no cross-process merge.
At launch/exit, merge only changes since each writer's acknowledged snapshot.
Running windows are never rewritten. The original profile can also receive a
projection, but only through the common controlled entry after an idle check.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import uuid

from core import ID, UserError, directory, write_json
from organization_conflicts import fingerprint
from organization_domain import private_domain,preserve_domain,rejoined
from organization_policy import initialize as initialize_policy, reconcile

from organization_projection import (STATE, MAP_KEY, CHECKPOINT_KEYS, HOST_KEYS,
                                     projection, apply_projection, clean_projection, reference_ids)

class OrganizationConflictError(UserError):
    pass


def fingerprint_scope(history,volume_uuid):
    return hashlib.sha256((str(history)+'\0'+volume_uuid).encode()).hexdigest()


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



class Organization:
    def __init__(self,store):
        self.store=store
        from distribution import default_root
        production={default_root(community=False).resolve(),default_root(community=True).resolve()}
        shared=default_root(community=False).parent/'Codex Harbor Shared'/'organization'
        self.root=directory(getattr(store,'shared_organization_root',
                                   shared if store.root in production else store.root/'organization'))
        self.path=self.root/'state.json'

    @contextmanager
    def locked(self,root,name='state.lock'):
        lock=directory(root)/name
        fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            yield

    @staticmethod
    def upgrade(value):
        value=copy.deepcopy(value)
        if value.get('version') not in (1,2,4) or not isinstance(value.get('scopes'),dict) or not isinstance(value.get('writers'),dict):
            raise UserError('项目组织缓存版本异常，已停止启动。')
        if value['version']==1:
            value['version']=2
            for writer in value['writers'].values():
                writer['ack_snapshot']=writer.pop('baseline',{})
                writer['launch_snapshot']=copy.deepcopy(writer['ack_snapshot'])
                writer.setdefault('origin','harbor')
                writer['uncertain_legacy_ack']=True
        for scope in value['scopes'].values():
            scope.setdefault('generation',0);scope.setdefault('conflicts',{})
        if value['version']==4:
            def valid_versions(items,maximum):
                return isinstance(items,dict) and all(isinstance(k,str) and type(v) is int and 0<=v<=maximum
                                                      for k,v in items.items())
            for scope in value['scopes'].values():
                clock=scope.get('clock')
                if type(clock) is not int or clock<1 or not valid_versions(scope.get('versions'),clock):
                    raise UserError('共享侧栏版本记录异常；已保留原文件，未重新初始化。')
            for writer in value['writers'].values():
                scope=value['scopes'].get(writer.get('scope'))
                if not scope or not isinstance(writer.get('registration'),str) or not writer['registration'] or not valid_versions(writer.get('base_versions'),scope['clock']):
                    raise UserError('窗口侧栏版本记录异常；已保留原文件，未重新初始化。')
        if value['version']!=4:
            # Old snapshots cannot reconstruct operations or true edit order.
            # Preserve valid positive evidence once; never infer removals.
            for scope in value['scopes'].values():
                initialize_policy(scope)
                for item in scope['conflicts'].values():
                    incoming=copy.deepcopy(scope['data'])
                    if item['incoming'].get('present'):
                        if not item['path']:incoming=item['incoming']['value']
                        else:
                            parent=incoming
                            for key in item['path'][:-1]:
                                if not isinstance(parent.get(key),dict):parent[key]={}
                                parent=parent[key]
                            parent[item['path'][-1]]=item['incoming']['value']
                        record={'registration':'legacy-conflict:'+item['id'],'base_versions':{}}
                        reconcile(scope,record,{},incoming,legacy=True)
                scope['conflicts']={}
            for writer in value['writers'].values():
                writer['needs_bootstrap']=True
                writer['base_versions']={}
                writer.setdefault('registration',uuid.uuid4().hex)
            value['version']=4
        return value

    def import_legacy(self,value):
        root=self.store.root/'organization'
        if root==self.root:return
        # Always central -> legacy lock. Old runtimes only take legacy, then
        # reject version 3; they can no longer create a second authority.
        with self.locked(root):
            path=root/'state.json'
            raw=read_object(path)
            if raw is None:raw={'version':2,'scopes':{},'writers':{}}
            if raw.get('version')==3:
                if raw.get('coordinator')!=str(self.root):
                    raise UserError('共享项目组织入口不一致，未覆盖。')
                legacy=raw['legacy'];ident=raw['import_id']
                if fingerprint(legacy)!=ident:raise UserError('旧组织快照校验失败，未覆盖。')
            else:
                legacy=raw;ident=fingerprint(raw)
                self.upgrade(legacy)  # Validate before freezing old publishers.
                write_json(path,{'version':3,'coordinator':str(self.root),'import_id':ident,'legacy':legacy})
            token=str(root)+':'+ident
            if token in value.setdefault('imports',[]):return
            legacy=self.upgrade(legacy)
            for key,scope in legacy['scopes'].items():
                if key not in value['scopes']:
                    value['scopes'][key]=copy.deepcopy(scope)
                elif value['scopes'][key]['data']!=scope['data']:
                    current=value['scopes'][key]
                    record={'registration':'legacy-import:'+token,'base_versions':{}}
                    reconcile(current,record,{},scope['data'],legacy=True)
            for key,writer in legacy['writers'].items():
                if key in value['writers'] and value['writers'][key]!=writer:
                    raise UserError('旧窗口归属记录有冲突，已保留完整快照，未合并。')
                value['writers'][key]=copy.deepcopy(writer)
            value['imports'].append(token)

    @contextmanager
    def transaction(self):
        with self.locked(self.root):
            raw=read_object(self.path)
            enrollment=self.root/'enrolled.json'
            if raw is None and enrollment.exists():
                raise UserError('共享侧栏状态缺失；已保留各窗口和历史，请恢复缓存后重试。')
            if raw is None:raw={'version':4,'scopes':{},'writers':{}}
            value=self.upgrade(raw)
            if raw['version'] in (1,2) and not (self.root/'state-before-v4.json').exists():
                write_json(self.root/'state-before-v4.json',raw)
            self.import_legacy(value)
            roots=value.setdefault('control_roots',[])
            if str(self.store.root) not in roots:roots.append(str(self.store.root))
            yield value
            write_json(self.path,value)
            if not enrollment.exists():write_json(enrollment,{'version':1,'coordinator':str(self.root)})

    def identities(self,history):
        registry=self.store.registry.read()
        identities={s.get('volume_uuid','') for s in registry.get('history_bindings',[]) if s['path']==str(history)}
        for location in registry['storages']+registry.get('removed_storages',[]):
            if (location.get('enabled') or location.get('volume_uuid')) and str(self.store.registry.registered_history_path(location))==str(history):
                identities.add(location.get('volume_uuid',''))
        return identities

    def scope(self,value,history,volume_uuid=None):
        if volume_uuid is None:
            matches=[s.get('volume_uuid','') for s in self.store.registry.entries()
                     if str(self.store.registry.registered_history_path(s))==str(history)]
            if len(matches)!=1:raise UserError('项目组织目录归属不明确，已停止启动。')
            volume_uuid=matches[0]
        key=fingerprint_scope(history,volume_uuid)
        scope=value['scopes'].setdefault(key,{'history':str(history),'data':{},'source':{}})
        scope['volume_uuid']=volume_uuid
        scope.setdefault('generation',0);scope.setdefault('conflicts',{})
        initialize_policy(scope)
        return key,scope

    def local_ids(self,scope):
        from sidebar_catalog import _connect,_require
        from storage import volume_identity
        history=Path(scope['history'])
        mounted=volume_identity(history)
        if mounted is not None and (not mounted or mounted!=scope.get('volume_uuid')):
            raise UserError('共享会话位置暂不可用或卷身份已变；未读取替代目录。')
        source=history/'state_5.sqlite'
        if not source.exists() and not source.is_symlink():
            if reference_ids(scope['data']):
                raise UserError('共享会话主索引缺失，无法确认会话归属；未覆盖。')
            return set()
        try:
            with _connect(source,readonly=True) as db:
                _require(db,'threads',['id'])
                return {r['id'] for r in db.execute('SELECT id FROM threads')}
        except (OSError,sqlite3.Error) as exc:
            raise UserError('无法只读核对共享会话归属；未覆盖。') from exc

    def writer(self,home,key,baseline,phase):
        from storage import volume_identity
        return dict(home=str(home),scope=key,ack_snapshot=copy.deepcopy(baseline),
                    launch_snapshot=copy.deepcopy(baseline),phase=phase,origin='harbor',
                    home_volume_uuid=volume_identity(Path(home)),registration=uuid.uuid4().hex,
                    base_versions={})

    def accept(self,scope,writer,before,after,*,trusted=True,uncertain_domain=None):
        if uncertain_domain:
            # Returning from an account-private/cloud domain does not grant
            # authority to replace the local values it never received.
            after=preserve_domain(after,before,uncertain_domain)
        previous=scope['data']
        if not reconcile(scope,writer,{} if not trusted else before,after,legacy=not trusted):return
        proposed=copy.deepcopy(scope['data'])
        projects=proposed.get('local-projects',{})
        for ident,assignment in proposed.get('thread-project-assignments',{}).items():
            if assignment['projectId'] in projects:continue
            prior=previous.get('thread-project-assignments',{}).get(ident)
            if prior and prior['projectId'] in projects:
                proposed['thread-project-assignments'][ident]=copy.deepcopy(prior)
        # Referential validity belongs to the same transaction: a stale move
        # cannot send a chat to a deleted project, nor recreate the project.
        # Keep a still-valid prior assignment; otherwise leave the chat ungrouped.
        normalized=clean_projection(proposed,reference_ids(proposed))
        if normalized!=scope['data']:
            validator={'registration':'local-reference-validation',
                       'base_versions':copy.deepcopy(scope['versions'])}
            reconcile(scope,validator,scope['data'],normalized)

    def native_pins(self,scope,writer,state,before,after):
        """Do not replay already-consumed legacy pins into a new native home.

        Only acknowledge completion actually persisted by this native writer.
        Native sections remain authoritative; no section or migration marker is
        invented, copied to another home, or written to the source database.
        """
        markers=state.get('app-server-migrated-pinned-thread-ids-by-host',{})
        completed=markers.get('local:'+writer['home'],[]) if isinstance(markers,dict) else None
        if not isinstance(completed,list) or any(not isinstance(v,str) for v in completed):
            raise UserError('原生置顶迁移记录无法核对；未覆盖。')
        known=set(scope.get('consumed_legacy_pins',[]))|set(completed)
        scope['consumed_legacy_pins']=sorted(known)
        for data in (scope['data'],before,after):
            if 'pinned-thread-ids' in data:
                data['pinned-thread-ids']=[v for v in data['pinned-thread-ids'] if v not in known]

    def register_source(self,value,key,scope,history):
        home=str(history)
        identity='native:'+key
        if identity in value['writers']:return identity
        legacy=value['writers'].get(home)
        if legacy and legacy.get('origin')=='native' and legacy['scope']==key:
            value['writers'][identity]=value['writers'].pop(home)
            return identity
        before=scope.pop('source',{})
        writer=self.writer(history,key,before,'ready');writer['origin']='native';writer['needs_bootstrap']=True
        value['writers'][identity]=writer
        return identity

    def adopt_legacy(self,value,known,current_home=None):
        # Before the first 0.8 launch, accounts/APIs can already have private
        # projects. Discover only Harbor-owned homes, including retired runtimes.
        candidates={home:[] for home in (self.store.root/'accounts').glob('*/codex')}
        for location in self.store.registry.credential_locations():
            if location.get('history_path') in known and self.store.registry.disk_available(location):
                for home in (Path(location['path'])/'instances').glob('*/codex'):
                    candidates.setdefault(home,[]).append(location)
        for home,locations in candidates.items():
            if not ID.fullmatch(home.parent.name) or str(home) in value['writers'] or not (home/'sessions').is_symlink():continue
            link=home/'sessions'
            target=link.readlink()
            prior=Path(os.path.normpath(target if target.is_absolute() else link.parent/target)).parent
            if str(prior) not in known:
                # A primary home can be retargeted. Preserve its old private
                # edits before replacing its projection, without reading the
                # former history or enumerating that storage's other homes.
                registry=self.store.registry.read()
                registered={str(self.store.registry.registered_history_path(s)) for s in registry['storages']}
                registered.update(registry.get('retired_histories',[]))
                if home!=current_home or str(prior) not in registered:continue
            bindings={s['history_volume_uuid'] for s in locations if s.get('history_path')==str(prior)}
            if not bindings:bindings=self.identities(prior)
            # A path reused by another disk cannot identify an old official
            # home. Preserve its file/backup instead of guessing the owner.
            if len(bindings)!=1:continue
            from storage import volume_identity
            mounted=volume_identity(prior)
            if mounted and mounted not in bindings:
                continue  # A retired runtime belongs to another physical disk.
            try:
                state=read_object(home/STATE)
                if state is None:continue
                key,scope=self.scope(value,prior,next(iter(bindings)))
                data=projection(state,home,self.local_ids(scope))
            except (OSError,UserError) as exc:
                if str(prior) in known:
                    raise UserError('这个会话位置的旧窗口组织信息无法核对；已保留原文件，未静默跳过。') from exc
                continue
            record=self.writer(home,key,data,'ready')
            self.accept(scope,record,{},data,trusted=False)
            value['writers'][str(home)]=record
            value['writers'][str(home)]['owned_thread_ids']=sorted(reference_ids(data))

    def harvest(self,value,*,scope_key=None,home=None):
        # All paths were recorded by prepare, never accepted from UI input.
        local_by_scope={}
        for writer in value['writers'].values():
            if writer.get('origin')=='native' and scope_key is not None and writer['scope']!=scope_key:continue
            if (scope_key is not None or home is not None) and writer['scope']!=scope_key and writer['home']!=home:continue
            try:
                scope=value['scopes'][writer['scope']]
                if writer['scope'] not in local_by_scope:
                    if 'volume_uuid' not in scope:
                        # V1 encoded the registered volume in the scope hash,
                        # but did not store it separately. Recover only an
                        # exact historical registration match, never the disk
                        # currently mounted at a reused path by assumption.
                        matches=[v for v in self.identities(Path(scope['history']))
                                 if fingerprint_scope(scope['history'],v)==writer['scope']]
                        if len(matches)!=1:
                            raise UserError('旧共享目录的卷身份无法确认；已保留记录，未覆盖。')
                        scope['volume_uuid']=matches[0]
                    local_by_scope[writer['scope']]=self.local_ids(scope)
                    # Index hydration is not deletion evidence. Keep already
                    # validated local metadata dormant until its source row is
                    # readable again; native archive flags still own visibility.
                    scope['data']=clean_projection(scope['data'],local_by_scope[writer['scope']] | reference_ids(scope['data']))
                local_ids=local_by_scope[writer['scope']]
                from storage import volume_identity
                mounted=volume_identity(Path(writer['home']))
                if mounted is not None and (not mounted or mounted!=writer.get('home_volume_uuid')):
                    raise UserError('相关窗口的目录暂不可用或卷身份已变；未把它当成空状态。')
                state=read_object(Path(writer['home'])/STATE)
                after=projection(state,writer['home'],local_ids) if state is not None else None
            except OSError as exc:
                raise UserError('相关窗口的项目组织状态无法读取；未覆盖，请检查目录后重试。') from exc
            if state is None:
                if writer.get('phase')=='pending' and writer.get('previous_absent') is True:
                    continue  # First projection never reached the new file.
                if writer.get('ack_snapshot'):
                    raise UserError('相关窗口的项目组织文件缺失；未把缺失当成取消置顶。')
                continue
            if writer.get('phase')=='pending':
                # A crash between journal and private-file writes is not a user
                # edit. Only a completed write may publish to the new location.
                if after==writer['ack_snapshot']:
                    writer['phase']='ready'
                    writer.pop('previous_owned_thread_ids',None)
                elif after==writer.get('previous_snapshot'):continue
                else:raise UserError('上次项目投影未完成且文件发生变化；已保留原文件，未覆盖。')
            scope=value['scopes'][writer['scope']]
            observed=copy.deepcopy(after)
            remembered=reference_ids(scope['data']) | reference_ids(writer['ack_snapshot'])
            before=clean_projection(writer['ack_snapshot'],local_ids | remembered)
            missing={'projects':[],'threads':sorted(remembered-local_ids),'selected':False}
            before=preserve_domain(before,scope['data'],missing)
            after=preserve_domain(after,scope['data'],missing)
            domain=private_domain(state,local_ids,before,scope['data'],previous=writer.get('private_domain'))
            returning=rejoined(writer.get('private_domain',{}),domain,local_ids,before,after,scope['data'])
            before=preserve_domain(before,scope['data'],domain)
            after=preserve_domain(after,scope['data'],domain)
            self.native_pins(scope,writer,state,before,after)
            self.accept(scope,writer,before,after,trusted=not writer.get('needs_bootstrap',False),
                        uncertain_domain=returning)
            writer['ack_snapshot']=observed
            writer['private_domain']=domain
            writer['owned_thread_ids']=sorted(reference_ids(after))
            writer.pop('uncertain_legacy_ack',None)
            writer.pop('needs_bootstrap',None)
        return local_by_scope

    def prepare(self,home,history):
        home=Path(home);history=Path(history)
        with self.transaction() as value:
            # Import an old location when it is selected, not by walking every
            # registered disk during an unrelated launch. Preserve other scopes.
            self.adopt_legacy(value,{str(history)},home)
            key,scope=self.scope(value,history)
            source_writer=self.register_source(value,key,scope,history)
            sampled_ids=self.harvest(value,scope_key=key,home=str(home))
            original=read_object(home/STATE)
            private=original or {}
            local_ids=sampled_ids[key]
            baseline=projection(private,home,local_ids)  # Reject corrupt state before overwriting it.
            projected=copy.deepcopy(scope['data'])
            writer=self.writer(home,key,baseline,'pending')
            identity=source_writer if home==history else str(home)
            previous=value['writers'].get(identity,{})
            same_binding=(previous.get('scope')==key and previous.get('phase')=='ready' and
                          previous.get('home_volume_uuid')==writer['home_volume_uuid'] and
                          writer['home_volume_uuid']!='')
            prior_ids=set(previous.get('owned_thread_ids',[]))|set(previous.get('previous_owned_thread_ids',[]))
            private=apply_projection(private,projected,home,same_binding=same_binding,local_ids=local_ids|prior_ids)
            # Keep a one-time rollback of this private state, never source auth.
            backup=(directory(self.root/'original-profile-backups')/(key+'.json') if home==history
                    else home/'.harbor-organization-before.json')
            if (home/STATE).exists() and not backup.exists():write_json(backup,read_object(home/STATE))
            # Commit a pending baseline first. If interrupted, recovery must not
            # interpret the old location's state as edits to the new location.
            writer['previous_snapshot']=baseline
            writer['previous_absent']=original is None
            writer['previous_owned_thread_ids']=sorted(prior_ids)
            writer['ack_snapshot']=projection(private,home,local_ids)
            writer['owned_thread_ids']=sorted(reference_ids(writer['ack_snapshot']))
            writer['launch_snapshot']=copy.deepcopy(writer['ack_snapshot'])
            writer['private_domain']=private_domain(private,local_ids,writer['ack_snapshot'],scope['data'])
            writer['generation']=scope['generation']
            writer['base_versions']=copy.deepcopy(scope['versions'])
            if same_binding:writer['registration']=previous['registration']
            if home==history:writer['origin']='native'
            value['writers'][identity]=writer
            write_json(self.path,value)
            write_json(home/STATE,private)
            value['writers'][identity]['phase']='ready'
            value['writers'][identity].pop('previous_owned_thread_ids',None)
            return {'generation':scope['generation'],'scope':key,'verified':True}

    def publish(self,home=None):
        with self.transaction() as value:self.harvest(value,home=str(home) if home else None)

    def prepare_original(self,history):
        """Caller also holds the common entry lock through actual OS launch."""
        from native_profile import require_idle
        with self.transaction() as value:roots=list(value['control_roots'])
        require_idle(self.store.root,history,known_roots=roots)
        return self.prepare(history,history)

    def conflicts(self,history):
        # Compatibility for old local tooling. Presentation differences are
        # reconciled automatically; there is no user-choice queue.
        with self.transaction() as value:
            key,_=self.scope(value,Path(history))
            self.harvest(value,scope_key=key)
        return []

    def resolve(self,*args):
        raise UserError('侧栏差异现在自动处理，请直接重新启动；旧的手动选择未应用。')

    conflict_view = conflicts
