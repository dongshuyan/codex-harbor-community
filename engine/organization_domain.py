"""Ownership boundaries for one native writer, independent of merge policy.

Becoming account-private is not a deletion from the shared local view. These
IDs live only in that writer's acknowledgement; no cloud content is projected.
"""
import copy

BACKING=('chatGptBacking','chatGptBackingCreation','retiredChatGptBacking','chatGptBackingRecoveryPending')
PROJECT_DICTS=('local-projects','sidebar-project-thread-orders','project-appearances','__project-id-map')
THREAD_DICTS=('thread-project-assignments','thread-workspace-root-hints',
              'thread-projectless-output-directories','thread-project-membership-host-ids')
PROJECT_LISTS=('project-order','pinned-project-ids')
THREAD_LISTS=('pinned-thread-ids','projectless-thread-ids')
ATOM_LISTS=('unified-sidebar-pinned-order-v1','unified-sidebar-project-order-v1')
EXPANDED='sidebar-project-expanded-v1-codex:'


def blend_order(original,wanted,protected):
    """Keep private entries beside their next surviving neighbor, in O(n)."""
    wanted=list(dict.fromkeys(wanted));present=set(wanted);buckets={};anchor=None
    for item in reversed(original):
        if item in present:anchor=item
        elif protected(item):buckets.setdefault(anchor,[]).append(item)
    result=[]
    for item in wanted:
        result.extend(reversed(buckets.get(item,[])));result.append(item)
    result.extend(reversed(buckets.get(None,[])))
    return list(dict.fromkeys(result))


def private_domain(raw,local_ids,*views,previous=None):
    # Called only after projection has validated owned shapes.
    projects={k for k,v in raw.get('local-projects',{}).items() if any(f in v for f in BACKING)}
    previous=previous or {}
    # Removing a formerly private item is not a local shared deletion. Only an
    # explicit local item can rejoin; a fresh projection resets this boundary.
    projects.update(set(previous.get('projects',[]))-raw.get('local-projects',{}).keys())
    threads={k for k,v in raw.get('thread-project-assignments',{}).items() if k in local_ids and
             (v.get('projectKind')=='remote' or v.get('projectOrigin')=='chatgpt' or v.get('projectId') in projects)}
    threads.update((set(previous.get('direct_threads',[]))-raw.get('thread-project-assignments',{}).keys()) & local_ids)
    direct_threads=set(threads)
    for view in (raw,*views):
        threads.update(k for k,v in view.get('thread-project-assignments',{}).items()
                       if k in local_ids and isinstance(v,dict) and v.get('projectId') in projects)
    selected=raw.get('selected-project')
    return {'projects':sorted(projects),'threads':sorted(threads),'direct_threads':sorted(direct_threads),
            'selected':bool(selected and (selected.get('type')!='local' or selected.get('projectId') in projects))}


def rejoined(previous,current,local_ids,*views):
    projects=set(previous.get('projects',[]))-set(current['projects'])
    threads=(set(previous.get('direct_threads',[]))-set(current['direct_threads'])) & local_ids
    for view in views:
        threads.update(k for k,v in view.get('thread-project-assignments',{}).items()
                       if k in local_ids and v.get('projectId') in projects)
    return {'projects':sorted(projects),'threads':sorted(threads),'selected':False} if projects or threads else None


def preserve_domain(value,reference,domain):
    """Replace protected fields with the same reference in both merge inputs.

With an empty reference this masks a projection for writing to a private home.
Whole projects, related thread rows and mixed atom lists use one ownership rule.
"""
    result=copy.deepcopy(value);projects=set(domain['projects']);threads=set(domain['threads'])
    def mapping(key,protected,target=result,source=reference):
        desired=target.get(key,{});prior=source.get(key,{})
        for ident in protected:
            if ident in prior:desired[ident]=copy.deepcopy(prior[ident])
            else:desired.pop(ident,None)
        if desired or key in target:target[key]=desired
    def ordered(key,protected,target=result,source=reference):
        desired=[v for v in target.get(key,[]) if not protected(v)]
        values=blend_order(source.get(key,[]),desired,protected)
        if values or key in target:target[key]=values
    for key in PROJECT_DICTS:mapping(key,projects)
    for key in THREAD_DICTS:mapping(key,threads)
    for key in PROJECT_LISTS:ordered(key,projects.__contains__)
    for key in THREAD_LISTS:ordered(key,threads.__contains__)
    orders=result.get('sidebar-project-thread-orders',{})
    for ident,item in orders.items():
        if ident not in projects:
            ordered('threadIds',threads.__contains__,item,reference.get('sidebar-project-thread-orders',{}).get(ident,{}))
    selected=value.get('selected-project',{})
    prior=reference.get('selected-project',{})
    if domain['selected'] or selected.get('projectId') in projects or prior.get('projectId') in projects:
        if 'selected-project' in reference:result['selected-project']=copy.deepcopy(reference['selected-project'])
        else:result.pop('selected-project',None)
    atoms=result.get('electron-persisted-atom-state',{})
    source=reference.get('electron-persisted-atom-state',{})
    for ident in projects:
        key=EXPANDED+ident
        if key in source:atoms[key]=copy.deepcopy(source[key])
        else:atoms.pop(key,None)
    def protected_atom(item):
        return (item.startswith('codex:project:') and item[len('codex:project:'):] in projects or
                item.startswith('codex:thread:local:') and item[len('codex:thread:local:'):] in threads)
    for key in ATOM_LISTS:ordered(key,protected_atom,atoms,source)
    if atoms:result['electron-persisted-atom-state']=atoms
    elif 'electron-persisted-atom-state' in result:result.pop('electron-persisted-atom-state')
    return result
