"""Named storage registration. Registration never imports or creates history."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import uuid

from core import ID, UserError, directory, write_json, MACHINE

EXISTING_HOMES = MACHINE.get('original_homes',{})


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def safe_path(value):
    raw = str(value).strip()
    if not raw or any(ord(c) < 32 for c in raw):
        raise UserError('请填写有效的绝对目录路径。')
    path = Path(raw).expanduser()
    if not path.is_absolute(): raise UserError('会话目录必须使用绝对路径。')
    path = Path(os.path.normpath(path))
    for part in [path, *path.parents]:
        if part.is_symlink() and part not in (Path('/var'), Path('/tmp'), Path('/etc')):
            raise UserError('会话目录不能经过符号链接。')
    return path.resolve()


def volume_identity(path):
    """Never treat an ordinary /Volumes child as a mounted disk."""
    if len(path.parts) < 3 or path.parts[1] != 'Volumes': return None
    mount = Path(*path.parts[:3])
    if not mount.is_mount(): return ''
    try:
        result = subprocess.run(['/usr/sbin/diskutil','info','-plist',str(mount)],
                                capture_output=True, check=True, timeout=5)
        return plistlib.loads(result.stdout).get('VolumeUUID') or ''
    except (OSError, ValueError, subprocess.SubprocessError): return ''


class StorageRegistry:
    def __init__(self, store):
        self.store = store
        self.path = store.root/'storage-paths.json'

    def defaults(self):
        if self.store.community:return []
        result = [dict(id='external', name='测试移动盘', path=str(self.store.external), enabled=True),
                  dict(id='local', name='测试本地', path=str(self.store.root/'stores/local'), enabled=True)]
        # Existing homes require an explicit enable/save; app updates never
        # silently opt a previously disabled history into use.
        return result + [dict(id=i, name=n, path=p, enabled=False, protected=False)
                         for i,(n,p) in EXISTING_HOMES.items()]

    @contextmanager
    def guard(self):
        path = directory(self.store.root/'locks')/'storage-registry.lock'
        if path.is_symlink(): raise UserError('目录设置锁不安全。')
        with path.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def read(self):
        if self.path.is_symlink(): raise UserError('目录设置文件不安全。')
        if not self.path.exists(): return {'revision': 0, 'storages': self.defaults()}
        value = json.loads(self.path.read_text())
        if not isinstance(value.get('revision'), int) or not isinstance(value.get('storages'), list):
            raise UserError('目录设置格式无效，未启动。')
        return value

    def entries(self):
        return self.read()['storages']

    def sensitive_paths(self):
        value=self.read()
        return sorted(set(value.get('retired_paths',[])+[self.runtime_location(s)['path'] for s in value['storages']]))

    def runtime_location(self, item):
        # Credentials, desktop state and scratch workspaces stay in Harbor.
        # Only native session files and SQLite state use the existing home.
        if self.layout(item)=='native':
            key=item.get('runtime_key',item['id'])
            runtime=dict(id=item['id'], path=str(self.store.root/'stores'/key))
        else: runtime=dict(item)
        # Registry metadata is already normalized on save. Deriving another
        # location's identity must not stat it (or request Files & Folders
        # access). Actual use still goes through select/open_history/safe_path.
        return dict(runtime,history_path=str(self.registered_history_path(item)),
                    history_volume_uuid=item.get('volume_uuid',''))

    def layout(self, item):
        return item.get('layout','native' if item['id'] in EXISTING_HOMES else 'harbor')

    def history_path(self, item):
        path=self.checked_path(item['path'], item['id'])
        return path if self.layout(item)=='native' else path/'history'

    def registered_history_path(self, item):
        """Pure metadata only; never use this as authorization to access a path."""
        path=Path(item['path'])
        return path if self.layout(item)=='native' else path/'history'

    @contextmanager
    def open_history(self, item):
        path=self.history_path(item)
        if self.layout(item)=='harbor': directory(path)
        # An existing home disappearing after selection must fail, never become
        # a newly created empty home. Keep all setup writes anchored to its fd.
        flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd=os.open(path, flags)
        try:
            for name in ['sessions','archived_sessions','thread-writer-locks','rollout-migrations','.tmp']:
                try: os.mkdir(name,mode=0o700,dir_fd=fd)
                except FileExistsError: pass
                child=os.open(name,flags,dir_fd=fd)
                try:
                    if name=='.tmp':
                        lock=os.open('rollout-maintenance.lock',os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                                     0o600,dir_fd=child)
                        try:
                            if not stat.S_ISREG(os.fstat(lock).st_mode): raise UserError('会话维护锁不是普通文件。')
                        finally: os.close(lock)
                finally: os.close(child)
            original=os.fstat(fd)
            current=path.stat(follow_symlinks=False)
            if (original.st_dev,original.st_ino)!=(current.st_dev,current.st_ino):
                raise UserError('历史目录已改变，请刷新后重新启动。')
            yield path
            current=path.stat(follow_symlinks=False)
            if (original.st_dev,original.st_ino)!=(current.st_dev,current.st_ino):
                raise UserError('历史目录在准备期间改变，已停止启动。')
        finally: os.close(fd)

    def credential_locations(self):
        value=self.read()
        items=[self.runtime_location(s) for s in value['storages']]+value.get('retired_locations',[])
        known={s['path'] for s in items}
        # Old registries retained path strings without identity. Do not bind
        # those to whichever disk happens to be mounted during deletion.
        items += [dict(id='retired',path=p) for p in value.get('retired_paths',[]) if p not in known]
        return items

    def entry(self, ident):
        for item in self.entries():
            if item['id'] == ident: return item
        raise UserError('找不到这个会话位置，请刷新列表。')

    def disk_available(self, item, allow_unbound=False):
        if item['id'] == 'external' and item['path'] == str(self.store.external):
            return self.store.mounted()
        current = volume_identity(Path(item['path']))
        return current is None or bool(current and (current == item.get('volume_uuid') or (allow_unbound and not item.get('volume_uuid'))))

    def checked_path(self, value, ident=None):
        path=safe_path(value)
        if ident in EXISTING_HOMES and path == Path(EXISTING_HOMES[ident][1]):
            return path
        forbidden=[self.store.root/'stores'/key for key in EXISTING_HOMES]
        forbidden += [self.store.root/k for k in ['profiles','removed-profiles','purged-profiles','accounts','instances','instance-slots','locks','jobs','events','bindings','organization','keychain-refs','application-backups','probe-control','login']]
        forbidden += [Path.home()/'Library/Application Support/Codex Smart-2',Path.home()/'Library/Application Support/Codex',Path.home()/'Library/Application Support/ChatGPT']
        if MACHINE.get('volume'):forbidden.append(Path(MACHINE['volume'])/'Harnesses/CodexSmart2')
        forbidden += [Path('/Applications'),Path.home()/'Applications']
        if path in [Path('/'),Path.home(),self.store.root] or any(overlaps(path,p) for p in forbidden):
            raise UserError('该路径属于应用或启动器配置目录，不能作为会话位置。')
        return path

    def state(self, item):
        # Background snapshots do not inspect paths that the user disabled.
        if not item['enabled']:
            return dict(item, protected=False, exists=False, available=False, reason='已停用')
        try:
            path = self.checked_path(item['path'], item['id'])
            disk = self.disk_available(item)
            exists = disk and path.is_dir()
            writable = exists and os.access(path, os.R_OK | os.W_OK | os.X_OK)
            reason = ('磁盘未挂载或身份不匹配' if not disk
                      else '目录不存在，请先建立目录' if not exists
                      else '目录不可写或无法访问，请检查权限' if not writable else '可用')
            return dict(item, protected=False, exists=exists, available=bool(writable), reason=reason)
        except (UserError, OSError):
            return dict(item, protected=False, exists=False, available=False, reason='目录不安全或无法访问')

    def snapshot(self):
        value = self.read()
        return {'revision': value['revision'], 'storages': [self.state(s) for s in value['storages']],
                # Keep the saved enablement for restoration, without probing
                # removed paths during every status refresh.
                'removed_storages': [dict(s, protected=False, exists=False, available=False, reason='已移除')
                                     for s in value.get('removed_storages',[])]}

    def select(self, ident):
        if ident == 'auto':
            for item in self.entries():
                if self.state(item)['available']: return item['id']
            raise UserError('没有可用的会话目录；请建立或启用一个可用位置。')
        item = self.state(self.entry(ident))
        if not item['available']: raise UserError(item['name']+'：'+item['reason'])
        return ident

    def save(self, items, revision):
        if not isinstance(items, list) or not 0 <= len(items) <= 50:
            raise UserError('会话位置数量应为 0 到 50 个。')
        with self.guard():
            previous = self.read()
            if revision != previous['revision']: raise UserError('目录设置已改变，请关闭管理页并重新打开。')
            old = {s['id']: s for s in previous['storages']}
            archived = {s['id']:s for s in previous.get('removed_storages',[])}
            cleaned=[]; ids=set(); paths=[]
            for item in items:
                ident = item.get('id') or uuid.uuid4().hex
                if ident in ids or (ident not in ['local','external',*EXISTING_HOMES] and not ID.fullmatch(ident)):
                    raise UserError('会话位置标识重复或无效。')
                ids.add(ident)
                if ident in archived and ident not in old:
                    original=archived[ident]
                    if item.get('path') != original['path']: raise UserError('恢复位置不能同时更改路径。')
                    old[ident]=original
                name = str(item.get('name','')).strip()
                if not name or len(name)>60 or any(ord(c)<32 for c in name): raise UserError('位置名称需为 1 到 60 个可见字符。')
                path = self.checked_path(item.get('path',''), ident)
                if any(overlaps(path,p) for p in paths): raise UserError('会话位置不能重复或互相包含。')
                paths.append(path)
                entry=dict(id=ident,name=name,path=str(path),enabled=bool(item.get('enabled',True)))
                if ident in old and old[ident]['path']==str(path):
                    for key in ['layout','runtime_key','preserve_missing']:
                        if key in old[ident]: entry[key]=old[ident][key]
                else:
                    # New/retargeted registrations point directly at native
                    # history. Legacy Harbor roots remain in place, never moved.
                    entry['layout']='harbor' if (path/'history').is_dir() and (path/'instances').is_dir() and not (path/'sessions').exists() else 'native'
                    entry['runtime_key']='location-'+hashlib.sha256((ident+'\0'+str(path)).encode()).hexdigest()[:32]
                    entry['preserve_missing']=False
                # Identity is obtained locally, never trusted from UI input.
                if ident in old and old[ident]['path']==str(path):
                    if old[ident].get('volume_uuid'): entry['volume_uuid']=old[ident]['volume_uuid']
                    elif ident in EXISTING_HOMES and entry['enabled'] and not old[ident].get('enabled'):
                        # Binding is allowed only by this explicit enable, not
                        # by later reads or changing priority on an unbound disk.
                        identity=volume_identity(path)
                        if identity: entry['volume_uuid']=identity
                else:
                    identity=volume_identity(path)
                    if identity: entry['volume_uuid']=identity
                cleaned.append(entry)
            new={s['id']:s for s in cleaned}
            for ident, before in old.items():
                after=new.get(ident)
                if after is None or any(before.get(k)!=after.get(k) for k in ['path','enabled']):
                    if self.store.storage_running(ident): raise UserError('该会话位置仍有窗口运行，不能改路径、停用或移除。')
            retired=set(previous.get('retired_paths',[]))
            retired_histories=set(previous.get('retired_histories',[]))
            history_bindings={(s['path'],s.get('volume_uuid','')) for s in previous.get('history_bindings',[])}
            history_bindings.update((str(self.history_path(s)),s.get('volume_uuid','')) for s in old.values()
                                    if s.get('enabled') or s.get('volume_uuid'))
            retired_histories.update(str(self.history_path(s)) for ident,s in old.items() if
                                     ident not in new or new[ident]['path']!=s['path'])
            retired_locations={s['path']:s for s in previous.get('retired_locations',[])}
            retired_locations.update({self.runtime_location(s)['path']:self.runtime_location(s) for ident,s in old.items() if
                                      (ident not in new or new[ident]['path']!=s['path'])})
            retired.update(self.runtime_location(s)['path'] for ident,s in old.items() if
                           (ident not in new or new[ident]['path']!=s['path']))
            archived.update({i:s for i,s in old.items() if i not in new})
            archived={i:s for i,s in archived.items() if i not in new}
            write_json(self.path, {'revision': previous['revision']+1, 'storages':cleaned,'retired_paths':sorted(retired),
                                   'retired_histories':sorted(retired_histories),
                                   'history_bindings':[dict(path=p,volume_uuid=v) for p,v in sorted(history_bindings)],
                                   'retired_locations':list(retired_locations.values()),
                                   'removed_storages':list(archived.values())})
        return self.snapshot()

    def remove(self, ident, revision):
        self.entry(ident)
        return self.save([s for s in self.entries() if s['id'] != ident], revision)

    def restore(self, ident, revision):
        value=self.read()
        item=next((s for s in value.get('removed_storages',[]) if s['id']==ident),None)
        if item is None: raise UserError('找不到可恢复的位置。')
        # Preserve every existing priority; restored locations start last.
        items=value['storages']+[item]
        return self.save(items, revision)

    def purge(self, ident, revision):
        with self.guard():
            value=self.read()
            if revision != value['revision']: raise UserError('位置列表已改变，请刷新后重试。')
            items=value.get('removed_storages',[])
            if not any(s['id']==ident for s in items): raise UserError('只能永久移除可恢复列表中的位置登记。')
            value['removed_storages']=[s for s in items if s['id']!=ident]
            value['revision']+=1
            write_json(self.path,value)
        return self.snapshot()

    def initialize(self, ident, *, revision=None, path=None):
        with self.guard():
            if revision is not None and revision != self.read()['revision']:
                raise UserError('目录设置已改变，请刷新后重新确认建立目录。')
            item=self.entry(ident)
            if path is not None and path != item['path']:
                raise UserError('目录路径已改变，请刷新后重新确认建立目录。')
            if item.get('preserve_missing',ident in EXISTING_HOMES): raise UserError('现有历史目录缺失时不会新建空目录；请检查磁盘和原路径。')
            path=self.checked_path(item['path'],ident)
            if not self.disk_available(item,allow_unbound=True): raise UserError('磁盘未挂载或身份不匹配，未建立目录。')
            identity=volume_identity(path)
            if identity and not item.get('volume_uuid'):
                value=self.read()
                value['storages']=[dict(s,volume_uuid=identity) if s['id']==ident else s for s in value['storages']]
                value['revision']+=1
                write_json(self.path,value)
                item=self.entry(ident)
            # mkdir only. Existing files/history are never removed or imported.
            directory(path)
        return self.state(item)
