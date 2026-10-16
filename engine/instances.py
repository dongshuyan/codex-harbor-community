"""Persistent, independently leased desktop slots; history is never copied."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from core import ID, UserError, directory, write_json
from accounts import account_home, copy_private, take_lock


def scope_root(store, ident, storage):
    if not ID.fullmatch(ident):raise UserError('账号标识无效。')
    location=store.registry.entry(storage)
    identity=str(store.registry.history_path(location))+'\0'+location.get('volume_uuid','')
    scope=hashlib.sha256(identity.encode()).hexdigest()
    return directory(store.root/'instances'/ident/scope)


def slot_paths(store, ident, storage, slot):
    if slot=='primary':
        location=store.registry.entry(storage)
        base=Path(store.registry.runtime_location(location)['path'])/'instances'/ident
        official=store.profile(ident)['kind']=='official'
        home=store.root/'accounts'/ident/'codex' if official else base/'codex'
        # The official home is shared only by sequential primary launches.
        name=ident if official else storage+'-'+ident
        control=directory(store.root/'instance-slots'/name)
        return dict(home=home,electron=base/'desktop',os_home=base/'os-home',control=control)
    if not ID.fullmatch(slot):raise UserError('窗口目录标识无效。')
    base=directory(scope_root(store,ident,storage)/slot)
    return dict(home=base/'codex',electron=base/'desktop',os_home=base/'os-home',control=base)


def reject_legacy_instance(store, ident):
    from core import process_matches
    for path in (store.root/'jobs').glob('*.json'):
        if path.is_symlink():raise UserError('实例记录路径不安全。')
        value=json.loads(path.read_text())
        if value.get('id')==ident and value.get('lifecycle_version',1)<2 and process_matches(value):
            raise UserError('此账号/API 还有旧版窗口，请先退出该旧窗口；新版窗口可同时打开多个。')


def reserve(store, ident, storage):
    """Called under the registry lock; a backend orphan also keeps its slot busy."""
    reject_legacy_instance(store,ident)
    root=scope_root(store,ident,storage)
    previous=[p for p in root.iterdir() if ID.fullmatch(p.name) and not p.is_symlink() and p.is_dir()]
    candidates=['primary']+[p.name for p in sorted(previous,key=lambda p:p.stat().st_mtime,reverse=True)]
    candidates.append(uuid.uuid4().hex)
    for slot in candidates:
        paths=slot_paths(store,ident,storage,slot)
        guards=ExitStack()
        try:
            claim=guards.enter_context(take_lock(paths['control']/'desktop.lock'))
            switch=guards.enter_context(take_lock(paths['control']/'backend.lock'))
        except UserError:
            guards.close();continue
        # The native app might have closed inherited descriptors. Its recorded
        # PID/start time is an additional guard against reusing a live profile.
        from core import process_matches
        occupied=False
        for record in (store.root/'jobs').glob('*.json'):
            if record.is_symlink():continue
            value=json.loads(record.read_text())
            if value.get('home')==str(paths['home']) and process_matches(value):occupied=True;break
        if occupied:guards.close();continue
        write_json(paths['control']/'last-use.json',{'at':time.time()})
        os.utime(paths['control'],None)
        return slot,paths,guards,claim,switch
    raise UserError('暂时无法分配独立窗口目录，请稍后再试。')


def seed_home(source, target):
    """Seed preferences once, excluding tokens, history and browser databases."""
    if target.is_symlink():raise UserError('窗口配置目录不能是符号链接。')
    if target.exists():return
    directory(target.parent)
    staging=Path(tempfile.mkdtemp(prefix='.seed-',dir=target.parent))
    try:
        if source.is_dir():
            for name in ('config.toml','.harbor-managed-config.toml','.codex-global-state.json',
                         'AGENTS.md','AGENTS.override.md','.harbor-native-resources.json'):
                origin=source/name
                if origin.is_file() and not origin.is_symlink():copy_private(origin,staging/name)
            for folder in ('skills','rules'):
                origin=source/folder
                if not origin.is_dir():continue
                dest=directory(staging/folder)
                for item in origin.iterdir():
                    if item.name.startswith('.'):continue
                    if folder=='skills' and item.is_dir():
                        (dest/item.name).symlink_to(item.resolve(),target_is_directory=True)
                    elif folder=='rules' and item.is_file() and not item.is_symlink():
                        copy_private(item,dest/item.name)
        os.rename(staging,target)
    finally:
        if staging.exists():shutil.rmtree(staging)


def runtime(store, ident, storage, slot, launch_job):
    paths=slot_paths(store,ident,storage,slot)
    official=store.profile(ident)['kind']=='official'
    if official:source=account_home(store,ident,launch_job)
    else:source=slot_paths(store,ident,storage,'primary')['home']
    if slot!='primary':seed_home(source,paths['home'])
    for key in ('home','electron','os_home'):directory(paths[key])
    return paths
