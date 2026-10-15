"""Serial official-account homes, using Codex's native file authentication."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import subprocess
import time

from core import ID, UserError, directory, write_json


def account_lock_paths(store, ident):
    if not ID.fullmatch(ident): raise UserError('账号标识无效。')
    root=directory(store.root/'locks')
    return [root/f'account-{ident}.lock', root/f'backend-{ident}.lock']


def take_lock(path, shared=False):
    if path.is_symlink(): raise UserError('账号锁路径不安全。')
    handle=path.open('a')
    try: fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise UserError('此官方账号仍有窗口或后台任务运行，请退出并等待收尾后再换会话位置。') from None
    return handle


def copy_private(source, target):
    from storage import safe_path
    safe_path(source.parent);safe_path(target.parent)
    if source.is_symlink() or target.is_symlink(): raise UserError('账号文件不能经过符号链接。')
    directory(target.parent)
    fd, temporary=tempfile.mkstemp(prefix='.account-', dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as out, source.open('rb') as inp:
            shutil.copyfileobj(inp,out);out.flush();os.fsync(out.fileno())
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def legacy_idle(store, ident, candidates, launch_job=None):
    """Old versions did not hold backend leases: require recorded quiet exits."""
    from core import process_matches
    relevant=[]
    for path in (store.root/'jobs').glob('*.json'):
        if path.is_symlink(): raise UserError('旧实例记录不安全，未导入登录。')
        value=json.loads(path.read_text())
        if value.get('id')==ident:
            if (value.get('lifecycle_version')==1 and value.get('desktop_spawn_attempted') is False and
                    value.get('status') in ('exited','error') and not value.get('pid') and not value.get('pid_start')):
                continue  # New worker positively recorded failure before desktop spawn.
            if (launch_job and ID.fullmatch(launch_job) and path.stem==launch_job and
                    value.get('job')==launch_job and value.get('status')=='starting' and
                    not value.get('pid') and not value.get('pid_start')):
                continue  # This worker holds the account lock; desktop has not spawned.
            relevant.append((path,value))
    if not relevant or any(type(v.get('pid')) is not int or v['pid']<=0 or
                           not isinstance(v.get('pid_start'),str) or not v['pid_start'].strip() or
                           process_matches(v) or v.get('status') not in ('exited','error') or
                           time.time()-p.stat().st_mtime<60 for p,v in relevant):
        raise UserError('旧测试账号尚未确认完全退出。请先退出旧实例，等待一分钟后再导入登录。')
    try:
        # Only filenames/PIDs, never environment, command arguments or contents.
        processes=subprocess.run(['/bin/ps','-axo','pid=,pgid=,comm='],capture_output=True,check=True,timeout=8)
        groups={}
        for line in processes.stdout.decode(errors='replace').splitlines():
            parts=line.strip().split(None,2)
            if len(parts)==3 and Path(parts[2]).name=='codex':groups[int(parts[0])]=int(parts[1])
        old_groups={v['pid'] for _,v in relevant}
        if any(group in old_groups for group in groups.values()):
            raise UserError('旧测试账号仍有后台进程，未导入登录。')
        other_groups=set()
        for path in (store.root/'jobs').glob('*.json'):
            value=json.loads(path.read_text())
            if value.get('id')!=ident and process_matches(value):other_groups.add(value['pid'])
        result=subprocess.run(['/usr/sbin/lsof','-nP','-c','codex','-F','pfn'],capture_output=True,timeout=8)
        if (result.returncode not in (0,1) or result.stderr.strip() or
                (result.returncode==1 and result.stdout.strip())):
            raise UserError('无法完整确认旧后端已退出，未导入登录。')
        parents=[str(p.parent)+'/' for p in candidates]
        workspaces={str(p.parents[3]/'workspace') for p in candidates}
        pid=None;descriptor=None
        for line in result.stdout.decode(errors='replace').splitlines():
            if line.startswith('p'):pid=int(line[1:])
            elif line.startswith('f'):descriptor=line[1:]
            elif line.startswith('n'):
                if any(line[1:].startswith(p) for p in parents):
                    raise UserError('旧测试账号的后端仍在使用登录目录，请等待退出后再试。')
                if descriptor=='cwd' and line[1:] in workspaces and groups.get(pid) not in other_groups:
                    raise UserError('测试目录仍有无法归属的后台进程，未导入登录。')
    except (OSError,subprocess.SubprocessError):
        raise UserError('无法确认旧后端已退出，未导入登录。') from None


def account_home(store, ident, launch_job=None):
    root=directory(store.root/'accounts'/ident)
    home=directory(root/'codex')
    marker=root/'initialized.json'
    if marker.is_symlink(): raise UserError('账号初始化标记不安全。')
    if not marker.exists():
        # Only new Smart-2 instances are candidates. Original homes are excluded.
        candidates=[]
        for s in ([] if store.community else store.registry.entries()):
            if s['id'] in ('original-local','original-external'): continue
            if not store.registry.state(s)['available']: continue
            path=Path(s['path'])/'instances'/ident/'codex'/'auth.json'
            from storage import safe_path
            safe_path(path.parent)
            if path.is_symlink(): raise UserError('旧测试账号文件链接异常，未导入。')
            if path.is_file(): candidates.append(path)
        target=home/'auth.json'
        if target.is_symlink(): raise UserError('固定账号文件不安全。')
        if not target.exists() and candidates:
            legacy_idle(store,ident,candidates,launch_job)
            if len({hashlib.sha256(p.read_bytes()).digest() for p in candidates})>1:
                raise UserError('此账号在多个测试位置有不同的登录文件；未覆盖，请先明确要保留的登录。')
            backup=directory(root/'import-backup')/'auth.json'
            if not backup.exists(): copy_private(candidates[0],backup)
            copy_private(candidates[0],target)
        # A missing auth file after this marker means logout, never re-import.
        write_json(marker,{'version':1,'legacy_copied':bool(candidates)})
    return home


def bind_account_cache(store, ident, storage, home):
    """Keep rebuildable indexes/CLI input cache separate per storage.

    These small account-local caches are not shared rollout history. A journal
    makes an interrupted switch repeatable without overwriting the old cache.
    Native Codex replaces session_index.jsonl, so file symlinks are unsuitable.
    """
    root=home.parent
    marker=root/'cache-binding.json'
    if marker.is_symlink(): raise UserError('账号缓存标记不安全。')
    current=json.loads(marker.read_text()) if marker.exists() else {}
    files=['session_index.jsonl','history.jsonl']
    cache_root=directory(root/'storage-cache')
    locations=store.registry.credential_locations()

    def paths_for(storage_id):
        return {str(Path(s['path']).resolve()) for s in locations if s['id']==storage_id}

    def cache_key(storage_id, base):
        return storage_id+'-'+hashlib.sha256(str(Path(base).resolve()).encode()).hexdigest()

    def cache_for(storage_id, base):
        key=cache_key(storage_id,base);cache=cache_root/key
        if cache.exists() or cache.is_symlink(): return key,directory(cache)
        legacy=cache_root/storage_id
        # Old dormant caches contain no path identity. Reuse only when the
        # registry proves this ID has referred to exactly this one path.
        if (legacy.is_dir() and not any(s['id']=='retired' for s in locations)
                and paths_for(storage_id)=={str(Path(base).resolve())}):
            with tempfile.TemporaryDirectory(prefix='.cache-',dir=cache_root) as temporary:
                staged=Path(temporary)
                for name in files:
                    source=legacy/name
                    if source.is_file() or source.is_symlink():copy_private(source,staged/name)
                os.replace(staged,cache)
        else:directory(cache)
        return key,cache

    requested,_=cache_for(storage,store.registry.entry(storage)['path'])
    previous=current.get('storage')
    if previous:
        # v0.4.2 journals used the ID alone. The live sessions link identifies
        # the ready cache even after that ID has been retargeted in the UI.
        if previous in ('local','external') or ID.fullmatch(previous):
            paths=paths_for(previous)
            if current.get('phase')=='ready' and (home/'sessions').is_symlink():
                linked=(home/'sessions').resolve()
                base=str(linked.parent.parent)
                if linked.name!='sessions' or linked.parent.name!='history' or base not in paths:
                    raise UserError('旧账号缓存的目录归属无法核对，已保留缓存并停止切换。')
            elif len(paths)==1:
                base=next(iter(paths))
            else:
                raise UserError('旧账号缓存的目录归属不明确，已保留缓存并停止切换。')
            previous,_=cache_for(previous,base)
            current=dict(current,storage=previous)
            write_json(marker,current)
        else:
            storage_id,separator,digest=previous.rpartition('-')
            if (not separator or storage_id not in ('local','external','original-local','original-external') and not ID.fullmatch(storage_id)
                    or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
                raise UserError('账号缓存标记无效，已停止切换。')
    if current.get('phase')=='ready' and previous==requested: return
    if current.get('phase')=='loading':
        destination=current['storage']
    else:
        if previous:
            cache=directory(cache_root/previous)
            for name in files:
                source=home/name;target=cache/name
                if source.is_file(): copy_private(source,target)
                elif target.exists(): target.unlink()
        destination=requested
        write_json(marker,{'phase':'loading','storage':destination})
    cache=directory(cache_root/destination)
    for name in files:
        source=cache/name;target=home/name
        if source.is_file(): copy_private(source,target)
        elif target.is_symlink(): raise UserError('账号缓存链接不安全。')
        elif target.exists(): target.unlink()
    write_json(marker,{'phase':'ready','storage':destination})
    if destination!=requested: bind_account_cache(store,ident,storage,home)


def bind_link(link, target, official=False):
    if link.is_symlink():
        if link.resolve()==target.resolve(): return
        if not official: raise UserError('共享目录链接与会话位置不匹配。')
        # The account/backend lifetime locks exclude every previous reader.
        temporary=link.with_name(link.name+'.smart2-next')
        if temporary.exists() or temporary.is_symlink(): raise UserError('残留目录切换文件需检查，未覆盖。')
        temporary.symlink_to(target,target_is_directory=target.is_dir())
        os.replace(temporary,link)
    elif link.exists(): raise UserError('共享目录已被其他文件占用。')
    else: link.symlink_to(target,target_is_directory=target.is_dir())
