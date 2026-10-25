"""Pure allowlisted local desktop views; account and unknown fields stay private."""
import copy
from core import UserError
from organization_domain import BACKING,private_domain,preserve_domain,blend_order

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
             'unified-sidebar-project-order-v1')
EXPANDED = 'sidebar-project-expanded-v1-codex:'
ABSENT = object()

def checked(value,kind,label):
    valid=type(value) is bool if kind is bool else isinstance(value,kind)
    if not valid:raise UserError(label+'格式异常，未覆盖。')
    return value


def strings(value,label):
    checked(value,list,label)
    if any(not isinstance(v,str) for v in value):raise UserError(label+'格式异常，未覆盖。')
    return copy.deepcopy(value)


def fields(value,schema,label,required=()):
    checked(value,dict,label)
    if any(k not in value for k in required):raise UserError(label+'字段缺失，未覆盖。')
    return {k:copy.deepcopy(checked(value[k],kind,label)) for k,kind in schema.items() if k in value}


def projection(value, home, local_ids=frozenset()):
    """Never carry auth, permissions, plugins or account-bound cloud projects."""
    checkpoints=checked(value.get(CHECKPOINT_KEYS[0],{}),dict,'原生项目迁移记录')
    checkpoint=checked(checkpoints.get('local:'+str(home),{}),dict,'原生项目迁移记录')
    for key in ('threadAssignmentsMigrated','threadAssignmentsReadMigrated'):
        if key in checkpoint:checked(checkpoint[key],bool,'原生项目迁移记录')
        if checkpoint.get(key) is True:
            raise UserError('此窗口已启用尚未验证的原生项目成员索引；为避免旧分组覆盖新分组，本次未投影。已有窗口和历史均保留。')
    result={}
    for key in LIST_KEYS:
        if key in value:result[key]=strings(value[key],'项目组织列表')
    for key in DICT_KEYS:
        if key in value:result[key]=copy.deepcopy(checked(value[key],dict,'项目组织字段'))
    projects=result.get('local-projects',{});excluded=set()
    for ident,item in list(projects.items()):
        checked(item,dict,'本地项目')
        if any(k in item for k in BACKING):
            excluded.add(ident);del projects[ident];continue
        projects[ident]=fields(item,{'id':str,'name':str,'rootPaths':list},'本地项目',('id','name','rootPaths'))
        if item['id']!=ident:raise UserError('本地项目标识不一致，未覆盖。')
        projects[ident]['rootPaths']=strings(item['rootPaths'],'项目目录')
        for key in ('createdAt','updatedAt'):
            if key in item:
                import math
                if type(item[key]) not in (int,float) or not math.isfinite(item[key]):
                    raise UserError('项目时间格式异常，未覆盖。')
                projects[ident][key]=item[key]
    for key in ('project-order','pinned-project-ids'):
        if key in result:result[key]=[v for v in result[key] if v in projects]
    assignments=result.get('thread-project-assignments',{});hidden_threads=set()
    for ident,item in list(assignments.items()):
        if ident not in local_ids:del assignments[ident];continue
        item=fields(item,{'projectKind':str,'projectId':str,'projectOrigin':str,'hostId':str},
                    '会话项目归属',('projectKind','projectId'))
        if item['projectKind'] not in ('local','remote') or ('projectOrigin' in item and item['projectOrigin']!='chatgpt'):
            raise UserError('会话项目归属格式尚未验证，未覆盖。')
        if item['projectKind']=='remote' or item.get('projectOrigin')=='chatgpt' or item['projectId'] in excluded:
            hidden_threads.add(ident);del assignments[ident]
        elif item['projectId'] not in projects:
            # Removing a project can leave an old assignment. The local chat
            # and its own pin/root remain valid and must not be hidden with it.
            del assignments[ident]
        else:assignments[ident]={k:item[k] for k in ('projectKind','projectId')}
    for key in ('pinned-thread-ids','projectless-thread-ids'):
        if key in result:result[key]=[v for v in result[key] if v in local_ids and v not in hidden_threads]
    for key in ('thread-workspace-root-hints','thread-projectless-output-directories','thread-project-membership-host-ids'):
        if key in result:
            result[key]={k:checked(v,str,'会话目录或主机') for k,v in result[key].items()
                         if k in local_ids and k not in hidden_threads}
    if 'thread-project-membership-host-ids' in result:
        result['thread-project-membership-host-ids']={k:v for k,v in result['thread-project-membership-host-ids'].items() if v=='local'}
    if 'electron-workspace-root-labels' in result:
        result['electron-workspace-root-labels']={k:checked(v,str,'项目目录名称') for k,v in result['electron-workspace-root-labels'].items()}
    for key in ('sidebar-project-thread-orders','project-appearances'):
        if key in result:result[key]={k:v for k,v in result[key].items() if k in projects}
    for key,order in result.get('sidebar-project-thread-orders',{}).items():
        chosen=fields(order,{'threadIds':list,'sortKey':str},'项目会话排序',('threadIds',))
        if 'sortKey' in chosen and chosen['sortKey'] not in ('created_at','updated_at'):
            raise UserError('项目会话排序格式异常，未覆盖。')
        chosen['threadIds']=[v for v in strings(chosen['threadIds'],'项目会话排序') if v in local_ids and v not in hidden_threads]
        result['sidebar-project-thread-orders'][key]=chosen
    for key,appearance in result.get('project-appearances',{}).items():
        chosen=fields(appearance,{'color':str,'marker':dict},'项目外观',('color','marker'))
        if chosen['color'] not in ('black','blue','green','orange','pink','purple','red','yellow'):
            raise UserError('项目颜色格式尚未验证，未覆盖。')
        marker=chosen['marker'];kind=marker.get('kind')
        if kind not in ('icon','emoji'):raise UserError('项目图标格式尚未验证，未覆盖。')
        chosen['marker']=fields(marker,{'kind':str,kind:str},'项目图标',('kind',kind))
        result['project-appearances'][key]=chosen
    selected=value.get('selected-project')
    if selected is not None:
        checked(selected,dict,'选中项目')
        checked(selected.get('type'),str,'选中项目类型')
        if selected.get('type')=='local':
            selected=fields(selected,{'type':str,'projectId':str},'选中项目',('type','projectId'))
            # Selection is a window preference, never shared organization.
    maps=checked(value.get(MAP_KEY,{}),dict,'项目映射')
    mapping=checked(maps.get('local:'+str(home),{}),dict,'项目映射')
    if mapping:result['__project-id-map']={k:checked(v,str,'项目映射') for k,v in mapping.items() if k in projects}
    atoms=checked(value.get('electron-persisted-atom-state',{}),dict,'项目界面配置')
    chosen={k:copy.deepcopy(v) for k,v in atoms.items() if k in ATOM_KEYS}
    for key in ('unified-sidebar-pinned-order-v1','unified-sidebar-project-order-v1'):
        if key in chosen:
            chosen[key]=[v for v in strings(chosen[key],'项目排序配置') if
                         (v.startswith('codex:project:') and v[len('codex:project:'):] in projects or
                          v.startswith('codex:thread:local:') and v[len('codex:thread:local:'):] in local_ids and v[len('codex:thread:local:'):] not in hidden_threads)]
    if 'flat-project-sidebar-preferences-v1' in chosen:
        chosen['flat-project-sidebar-preferences-v1']=fields(chosen['flat-project-sidebar-preferences-v1'],
            {'chatSortMode':str,'projectSortMode':str,'mode':str,'initialized':bool},'侧栏显示偏好')
    if chosen:result['electron-persisted-atom-state']=chosen
    return result


def overlay_owned(original,before,wanted):
    """Change validated fields while retaining unknown fields inside an object."""
    if isinstance(before,dict) and isinstance(wanted,dict) and isinstance(original,dict):
        result=copy.deepcopy(original)
        for key in before.keys()-wanted.keys():result.pop(key,None)
        for key,item in wanted.items():
            result[key]=overlay_owned(original.get(key),before.get(key),item)
        return result
    return copy.deepcopy(wanted)


def clean_projection(value,local_ids):
    """Revalidate old shared snapshots without treating them as a raw home."""
    raw=copy.deepcopy(value)
    if '__project-id-map' in raw:
        raw[MAP_KEY]={'local:':raw.pop('__project-id-map')}
    return projection(raw,'',local_ids)


def reference_ids(value):
    ids=set()
    for key in ('pinned-thread-ids','projectless-thread-ids'):ids.update(value.get(key,[]))
    for key in ('thread-project-assignments','thread-workspace-root-hints',
                'thread-projectless-output-directories','thread-project-membership-host-ids'):
        ids.update(value.get(key,{}))
    for order in value.get('sidebar-project-thread-orders',{}).values():ids.update(order['threadIds'])
    for key in ('unified-sidebar-pinned-order-v1','unified-sidebar-project-order-v1'):
        ids.update(v[len('codex:thread:local:'):] for v in value.get('electron-persisted-atom-state',{}).get(key,[])
                   if v.startswith('codex:thread:local:'))
    return ids



def apply_projection(private, projected, home, *, same_binding, local_ids=frozenset()):
    """Replace local view fields only; preserve excluded cloud/unknown values."""
    old=projection(private,home,local_ids)
    domain=private_domain(private,local_ids,old,projected)
    old=preserve_domain(old,{},domain)
    projected=preserve_domain(projected,{},domain)
    result=copy.deepcopy(private)
    for key in (*LIST_KEYS,*DICT_KEYS):
        before=old.get(key,ABSENT);wanted=projected.get(key,ABSENT)
        if isinstance(before,list) or isinstance(wanted,list):
            owned=set(before if isinstance(before,list) else [])
            value=blend_order(result.get(key,[]),[] if wanted is ABSENT else wanted,lambda v:v not in owned)
            if value or wanted is not ABSENT:result[key]=list(dict.fromkeys(value))
            else:result.pop(key,None)
        elif isinstance(before,dict) and key!='selected-project' or isinstance(wanted,dict) and key!='selected-project':
            value=copy.deepcopy(result.get(key,{}))
            prior=before if isinstance(before,dict) else {}
            incoming={} if wanted is ABSENT else wanted
            for ident in prior:value.pop(ident,None)
            for ident,item in incoming.items():
                original=result.get(key,{}).get(ident)
                if key=='sidebar-project-thread-orders':
                    original=original or {}
                    owned=set(prior.get(ident,{}).get('threadIds',[]))
                    kept={k:v for k,v in original.items() if k not in ('threadIds','sortKey')}
                    value[ident]={**kept,**copy.deepcopy(item),
                                  'threadIds':blend_order(original.get('threadIds',[]),item['threadIds'],lambda v:v not in owned)}
                else:value[ident]=overlay_owned(original,prior.get(ident),item)
            if value or wanted is not ABSENT:result[key]=value
            else:result.pop(key,None)
        elif wanted is not ABSENT:result[key]=overlay_owned(result.get(key),before,wanted)
        elif before is not ABSENT:result.pop(key,None)
    atoms=result.get('electron-persisted-atom-state',{})
    old_atoms=old.get('electron-persisted-atom-state',{})
    wanted_atoms=projected.get('electron-persisted-atom-state',{})
    for key in old_atoms.keys() | wanted_atoms.keys():
        before=old_atoms.get(key,ABSENT);wanted=wanted_atoms.get(key,ABSENT)
        if isinstance(before,list) or isinstance(wanted,list):
            owned=set(before if isinstance(before,list) else [])
            kept=blend_order(atoms.get(key,[]),[] if wanted is ABSENT else wanted,lambda v:v not in owned)
            if kept or wanted is not ABSENT:atoms[key]=kept
            else:atoms.pop(key,None)
        elif isinstance(before,dict) or isinstance(wanted,dict):
            kept={k:v for k,v in atoms.get(key,{}).items() if k not in (before if isinstance(before,dict) else {})}
            if isinstance(wanted,dict):kept.update(wanted)
            if kept:atoms[key]=kept
            else:atoms.pop(key,None)
        elif wanted is ABSENT:atoms.pop(key,None)
        else:atoms[key]=copy.deepcopy(wanted)
    if atoms:result['electron-persisted-atom-state']=atoms
    identity='local:'+str(home)
    private_mapping={k:v for k,v in result.get(MAP_KEY,{}).get(identity,{}).items()
                     if k in domain['projects']}
    # Native mappings/checkpoints are per writer, not shared completion claims.
    # Keeping a same-history checkpoint prevents replaying a legacy pin after
    # native section APIs have already removed it in another window.
    for key in HOST_KEYS:
        entries=result.get(key)
        if isinstance(entries,dict) and not same_binding:
            entries.pop(identity,None)
            if not entries:result.pop(key,None)
    mapping=projected.get('__project-id-map',{})
    if mapping or private_mapping:
        entries=result.setdefault(MAP_KEY,{})
        existing=entries.get(identity,{})
        if same_binding and any(k in existing and existing[k]!=v for k,v in mapping.items()):
            raise UserError('项目标识映射有冲突；已保留双方，未覆盖原生配置。')
        entries[identity]={**existing,**mapping} if same_binding else {**private_mapping,**copy.deepcopy(mapping)}
    return result
