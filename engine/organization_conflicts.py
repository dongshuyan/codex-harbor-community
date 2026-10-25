"""Conflict evidence stays meaningful when a later edit removes its parent.

The merge stays field-based. A missing containing entity promotes a pending
choice to the complete saved entity, never recreates a half project from a name.
"""
import copy
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def field(value,path):
    for key in path:
        if not isinstance(value,dict) or key not in value:return {'present':False}
        value=value[key]
    return {'present':True,'value':copy.deepcopy(value)}


def prefix(parent,child):
    return child[:len(parent)]==parent


def refresh(scope):
    rows=[]
    for item in scope['conflicts'].values():
        item=copy.deepcopy(item)
        context=item.get('context')
        if context and len(item['path'])>len(context['path']):
            container=field(scope['data'],context['path'])
            if not container['present'] or not isinstance(container.get('value'),dict):
                item.update(path=context['path'],baseline=context['baseline'],incoming=context['incoming'])
        item['current']=field(scope['data'],item['path'])
        if item['current']==item['incoming']:continue
        # An ancestor is the latest complete alternative for this writer. Do
        # not leave a contradictory child choice after parent deletion/restore.
        same=[r for r in rows if r['writer']==item['writer']]
        if any(prefix(r['path'],item['path']) for r in same):continue
        rows=[r for r in rows if r['writer']!=item['writer'] or not prefix(item['path'],r['path'])]
        rows.append(item)
    scope['conflicts']={}
    for item in rows:
        item['id']=fingerprint([item['writer'],item['path']])
        item['revision']=fingerprint({k:v for k,v in item.items() if k!='revision'})
        scope['conflicts'][item['id']]=item


def record(scope,writer,before,after,conflicts):
    for item in scope['conflicts'].values():
        if item['writer']!=writer:continue
        if field(before,item['path'])!=field(after,item['path']):item['incoming']=field(after,item['path'])
        if item.get('context'):
            item['context']['incoming']=field(after,item['context']['path'])
    for item in conflicts:
        path=item['path']
        # The first two keys are the containing project/thread/preference item.
        # Lists and whole-field conflicts already carry their complete value.
        unit=path[:2]
        item.update(writer=writer,context={'path':unit,'baseline':field(before,unit),'incoming':field(after,unit)})
        ident=fingerprint([writer,path]);item['id']=ident
        ancestors=[v for v in scope['conflicts'].values() if v['writer']==writer and prefix(v['path'],path) and v['path']!=path]
        if ancestors:continue
        for key,old in list(scope['conflicts'].items()):
            if old['writer']==writer and prefix(path,old['path']):del scope['conflicts'][key]
        scope['conflicts'][ident]=item
    refresh(scope)
