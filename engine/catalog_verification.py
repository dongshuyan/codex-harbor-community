"""Read-only verification of the installed native local catalog contract.

The history index, native UI cache and raw rollout inventory have distinct roles.
This module never repairs original files or interprets conversation bodies.
"""
import hashlib
import json
import os
from pathlib import Path
import re

from core import UserError

ROLLOUT=re.compile(r'^rollout-.+-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl(?:\.zst)?$')
SOURCE_FIELDS='id,cwd,source,archived,preview,rollout_path,created_at,updated_at,recency_at,name,title,git_branch,model_provider'
CACHE_FIELDS='thread_id,cwd,project_id,missing_candidate,display_title,source_created_at,source_updated_at,source_recency_at,source_kind,model_provider,git_branch'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def snapshot(home,history):
    from sidebar_catalog import _connect,_require
    source=Path(history)/'state_5.sqlite';cache=Path(home)/'sqlite/codex-dev.db'
    if not source.exists() or not cache.exists():return None
    with _connect(source,readonly=True) as db:
        _require(db,'threads',SOURCE_FIELDS.split(','))
        all_rows=[dict(r) for r in db.execute('SELECT '+SOURCE_FIELDS+' FROM threads ORDER BY id')]
    expected={r['id']:r for r in all_rows if r['archived']==0 and r['preview'] and r['source'] in ('cli','vscode')}
    with _connect(cache,readonly=True) as db:
        _require(db,'local_thread_catalog',CACHE_FIELDS.split(','))
        _require(db,'local_thread_catalog_sync_state',['host_id','initial_build_complete','last_full_reconciled_at'])
        _require(db,'local_thread_catalog_scan_checkpoints',['host_id'])
        rows=[dict(r) for r in db.execute('SELECT '+CACHE_FIELDS+" FROM local_thread_catalog WHERE host_id='local' ORDER BY thread_id")]
        state=db.execute("SELECT initial_build_complete,last_full_reconciled_at FROM local_thread_catalog_sync_state WHERE host_id='local'").fetchone()
        pending=db.execute("SELECT COUNT(*) FROM local_thread_catalog_scan_checkpoints WHERE host_id='local'").fetchone()[0]
    active={r['thread_id']:r for r in rows if r['missing_candidate']==0}
    metadata=0
    for ident,row in expected.items():
        if ident not in active:continue
        cached=active[ident]
        pairs=[('cwd','cwd'),('source_created_at','created_at'),('source_updated_at','updated_at'),
               ('source_recency_at','recency_at'),('source_kind','source'),
               ('model_provider','model_provider'),('git_branch','git_branch')]
        def expected_value(key):
            value=row[key]
            return row['updated_at'] if key=='recency_at' and value is None else value
        if cached['project_id'] is not None or any(cached[k]!=expected_value(v) for k,v in pairs):metadata+=1
    return {'source':all_rows,'expected':expected,'active':active,
            'source_digest':digest(all_rows),'cache_digest':digest(rows),
            'missing':len(expected.keys()-active.keys()),'extra':len(active.keys()-expected.keys()),
            'metadata':metadata,'native_complete':bool(state and state[0] and state[1] is not None and not pending)}


def audit_rollouts(history,rows,*,missing_cache_ids=frozenset()):
    from sidebar_catalog import _rollout_exists
    ids={r['id'] for r in rows};found=set();uncertain=0
    # scandir does not read or decompress any history body. No rglob silently
    # swallowing permission failures, and no following arbitrary symlink trees.
    def scan(folder):
        nonlocal uncertain
        if not folder.exists():return
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_symlink():
                    uncertain+=1;continue
                if entry.is_dir(follow_symlinks=False):scan(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    match=ROLLOUT.fullmatch(entry.name)
                    if match:found.add(match[1])
    for name in ('sessions','archived_sessions'):
        folder=Path(history)/name
        if folder.is_symlink():raise UserError('历史清单目录指向其他路径，完整性检查尚未验证；未修改。')
        scan(folder)
    missing={r['id'] for r in rows if not _rollout_exists(r['rollout_path'])}
    return {'indexed_threads':len(ids),'rollout_ids':len(found),'missing_rollouts':len(missing),
            'missing_cache_rollouts':len(missing & missing_cache_ids),
            'unindexed_rollouts':len(found-ids),'uncertain_paths':uncertain}


def receipt(snapshot):
    # A whole-index digest changes on every normal incremental edit. Preserve
    # native title observations per unchanged input instead, so ordinary new
    # conversations do not force an unconditional full history scan.
    return {'version':2,'titles':{ident:{
        'input':digest([row['name'],row['title'],row['preview']]),
        'output':digest(snapshot['active'][ident]['display_title'])}
        for ident,row in snapshot['expected'].items() if ident in snapshot['active']}}


def titles_consistent(previous,snapshot):
    if not isinstance(previous,dict) or previous.get('version')!=2 or not isinstance(previous.get('titles'),dict):
        return False
    observed=receipt(snapshot)['titles']
    for ident,old in previous['titles'].items():
        if (not isinstance(old,dict) or set(old)!={'input','output'} or
                any(not isinstance(old[k],str) or not re.fullmatch(r'[0-9a-f]{64}',old[k])
                    for k in ('input','output'))):
            return False
        new=observed.get(ident)
        if new is not None and new.get('input')==old.get('input') and new.get('output')!=old.get('output'):
            return False
    return True
