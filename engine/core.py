"""Harbor account/storage preparation. Source credentials are never imported."""
from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import secrets
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid

ENGINE = Path(__file__).resolve().parent
from distribution import EDITION,discover_app,default_root
MACHINE=EDITION['machine']
USER_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
DEFAULT_ROOT = default_root()
VOLUME = Path(MACHINE.get('volume','/Volumes/HarborExternal'))
VOLUME_UUID = MACHINE.get('volume_uuid','')
EXTERNAL = Path(MACHINE.get('external',str(DEFAULT_ROOT/'stores/external')))
APP,CODEX,APP_EXECUTABLE=discover_app()
PROTOCOLS = ['responses']
ID = re.compile(r'^[a-f0-9]{32}$')


class UserError(ValueError):
    pass


def catalog():
    return json.loads((ENGINE / 'models.json').read_text())['models']


def model_options():
    return [{'id': m['slug'], 'name': m['display_name'],
             'efforts': [x['effort'] for x in m['supported_reasoning_levels']],
             'context': m['context_window']}
            for m in catalog() if m.get('visibility') == 'list']


def directory(path):
    """Reject symlinks at every component below an existing trusted root."""
    path = Path(path)
    for part in [path,*path.parents]:
        if part.is_symlink() and part not in (Path('/var'),Path('/tmp'),Path('/etc')):
            raise UserError(f'拒绝使用符号链接目录：{part}')
    if not path.parent.exists():
        directory(path.parent)
    elif path.parent.is_symlink():
        raise UserError(f'父目录不能是符号链接：{path.parent}')
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise UserError(f'路径不是目录：{path}')
    return path


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def atomic_write(path, text):
    path = Path(path)
    directory(path.parent)
    if path.is_symlink():
        raise UserError(f'拒绝覆盖符号链接：{path}')
    fd, temporary = tempfile.mkstemp(prefix='.smart2-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def disk_mounted():
    if not VOLUME.is_mount(): return False
    try:
        p = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(VOLUME)],
                           capture_output=True, timeout=5, check=True)
        return plistlib.loads(p.stdout).get('VolumeUUID') == VOLUME_UUID
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def validate_url(value):
    value = value.strip().rstrip('/')
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise UserError('API URL 必须是完整的 http(s) 基础地址，不能带用户名、密钥、查询参数或 #。')
    if p.scheme == 'http' and p.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise UserError('远程 API 必须使用 HTTPS；HTTP 仅允许本机测试。')
    try: p.port
    except ValueError: raise UserError('API URL 端口无效。') from None
    if p.path.endswith(('/responses', '/chat/completions', '/messages')):
        raise UserError('请填写基础 URL（例如 https://api.example.com/v1），不要包含 /responses、/messages 或 /chat/completions。')
    return value


def validated_profile(data):
    p = copy.deepcopy(data)
    profile_id = p.get('id') or uuid.uuid4().hex
    if not ID.fullmatch(profile_id): raise UserError('配置 ID 无效。')
    name = str(p.get('name', '')).strip()
    if not name or len(name) > 80: raise UserError('请填写 1–80 字的配置名称。')
    kind = p.get('kind')
    if kind not in ('official', 'api'): raise UserError('账号类型无效。')
    result = {'id':profile_id, 'name':name, 'kind':kind, 'models':[]}
    if kind == 'official': return result
    protocol = p.get('protocol')
    if protocol not in PROTOCOLS: raise UserError('请选择支持的 API 协议。')
    result.update(url=validate_url(str(p.get('url',''))), protocol=protocol)
    options = {m['id']:m for m in model_options()}
    seen = set()
    for m in p.get('models', []):
        alias = m.get('alias')
        if alias not in options: raise UserError('请选择当前 Codex 模型目录中的映射名称。')
        if alias in seen: raise UserError('同一 API 中每个 Codex 映射名称只能使用一次。')
        seen.add(alias)
        upstream = str(m.get('upstream','')).strip()
        if not upstream or len(upstream)>200 or any(ord(c)<32 for c in upstream):
            raise UserError('请填写 API 实际接受的 ModelName。')
        from model_metadata import context_number, parameter_state
        context = context_number(m.get('context',128000))
        if context is None:
            raise UserError('上下文长度必须为 4096–2000000 的整数；实际支持上限取决于 API。')
        from reasoning import validate
        result['models'].append(dict(alias=alias,upstream=upstream,context=context,parameters=parameter_state(m),**validate(m)))
    if not result['models']: raise UserError('请至少添加一个模型映射。')
    return result


def map_request(profile, request):
    mapping = next((m for m in profile['models'] if m['alias']==request.get('model')),None)
    if mapping is None:
        raise UserError('当前 API 未配置这个 Codex 模型名称。请在 Harbor 添加对应映射，或在会话中切换模型。')
    body = copy.deepcopy(request)
    body['model'] = mapping['upstream']
    body['store'] = False
    from reasoning import apply
    apply(mapping, body)
    # Routing/account headers are deliberately rebuilt in the proxy, never forwarded.
    return body


def process_stamp(pid):
    """PID plus kernel start time prevents acting on a recycled PID. No argv/env read."""
    if not isinstance(pid,int) or pid<2: return None
    result=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,
                          text=True,timeout=3,env={**os.environ,'LANG':'C','LC_ALL':'C'})
    return result.stdout.strip() if result.returncode==0 else None


def process_matches(job):
    return bool(job.get('pid_start') and process_stamp(job.get('pid'))==job['pid_start'])


def launch_uncertain(job):
    """An OS launch without its callback is not proof that no app was opened."""
    return job.get('desktop_launch_phase') in ('pending','requested')


class Store:
    def __init__(self, root=DEFAULT_ROOT, external=EXTERNAL, mounted=disk_mounted, *, community=None,keychain=None,user_home=None):
        self.community=EDITION['community'] if community is None else community
        self.keychain=keychain
        for value in (root,):
            for part in [Path(value).absolute(),*Path(value).absolute().parents]:
                if part.is_symlink() and part not in (Path('/var'),Path('/tmp'),Path('/etc')):
                    raise UserError(f'数据根目录不能经过符号链接：{part}')
        self.root = Path(root).resolve()
        # Explicit fixture roots stay isolated unless given a synthetic user home.
        self.user_home = Path(user_home).resolve() if user_home is not None else (USER_HOME if self.root == DEFAULT_ROOT else None)
        # Merely constructing the engine (including quota reads) must not touch
        # an unselected removable disk. StorageRegistry validates every path
        # component immediately before selection, initialization and use.
        self.external = Path(os.path.abspath(external))
        self.mounted = mounted
        forbidden=[Path.home()/'.codex',*[Path(p) for _,p in MACHINE.get('original_homes',{}).values()],
                   Path.home()/'Library/Application Support/Codex Smart-2',VOLUME/'Harnesses/CodexSmart2',
                   Path.home()/'Library/Application Support/Codex',Path.home()/'Library/Application Support/ChatGPT']
        for p in (self.root,self.external):
            if any(p==f or f in p.parents for f in forbidden):
                raise UserError('Harbor 不允许把现有 Codex 数据目录作为测试目录。')
        directory(self.root)
        directory(self.root/'profiles')
        from storage import StorageRegistry
        self.registry = StorageRegistry(self)

    def secret_path(self, ident):
        if not ID.fullmatch(ident): raise UserError('配置 ID 无效。')
        # URL, mappings and key are one atomic private snapshot, never split writes.
        return self.root/'profiles'/f'{ident}.json'

    def secret(self, ident):
        path=self.secret_path(ident)
        if path.is_symlink(): raise UserError('密钥文件不能是符号链接。')
        if not path.exists():return ''
        value=json.loads(path.read_text())
        if self.community:
            if value.get('key'):raise UserError('公开版不读取明文 API Key；请在编辑页重新保存到钥匙串。')
            return self.vault().get(self.checked_refs(ident,[value['key_ref']])[0]) if value.get('key_ref') else ''
        return value.get('key','')

    def vault(self):
        if self.keychain is None:
            from keychain_store import KeychainStore
            self.keychain=KeychainStore(allow_interaction=True,timeout=30)
        return self.keychain

    def key_refs(self,ident,add=None):
        path=self.root/'keychain-refs'/f'{ident}.json'
        if path.is_symlink():raise UserError('钥匙串引用目录不安全。')
        refs=self.checked_refs(ident,json.loads(path.read_text())['refs']) if path.exists() else []
        if add is not None:
            refs=list(dict.fromkeys(refs+self.checked_refs(ident,[add])));write_json(path,{'refs':refs})
        return refs

    @staticmethod
    def checked_refs(ident,refs):
        if not isinstance(refs,list) or any(not isinstance(r,str) or not re.fullmatch('chc_'+re.escape(ident)+'_[a-f0-9]{32}',r) for r in refs):
            raise UserError('钥匙串引用与当前配置不匹配，未访问或删除其他配置。')
        return refs

    def reclaim_pending_key(self,exclude):
        # One orphan per explicit save bounds work. Never query Keychain during
        # status refresh, and never delete a reference owned by an active draft.
        for path in sorted((self.root/'keychain-refs').glob('*.json')):
            ident=path.stem
            if ident==exclude or not ID.fullmatch(ident):continue
            if self.secret_path(ident).exists() or (self.root/'removed-profiles'/path.name).exists():continue
            try:
                with self.operation_guard(ident):
                    if self.secret_path(ident).exists() or (self.root/'removed-profiles'/path.name).exists():continue
                    for ref in self.key_refs(ident):self.vault().delete(ref)
                    path.unlink()
            except (UserError,OSError):pass
            break

    def profile(self, ident):
        if not ID.fullmatch(ident): raise UserError('配置 ID 无效。')
        path=self.root/'profiles'/f'{ident}.json'
        if path.is_symlink(): raise UserError('配置文件不能是符号链接。')
        if not path.is_file(): raise UserError('找不到这个账号/API 配置。')
        return validated_profile(json.loads(path.read_text())['profile'])

    def public(self, p):
        path=self.secret_path(p['id'])
        if path.is_symlink():raise UserError('配置路径不安全。')
        value=json.loads(path.read_text())
        current=validated_profile(value['profile'])
        if current['id']!=p['id']:raise UserError('配置 ID 不匹配。')
        return dict(current, has_key=current['kind']=='api' and bool(value.get('key_ref') or value.get('key')), revision=value.get('revision',0))

    def profiles(self):
        from profile_order import ordered
        return ordered(self.root,[self.public({'id':f.stem}) for f in sorted((self.root/'profiles').glob('*.json'))])

    def save_profile(self, data):
        p=validated_profile(data)
        key=str(data.get('api_key','')).strip()
        if any(ord(c)<32 for c in key): raise UserError('API Key 不能包含换行或控制字符。')
        # Hold both storage locks through publication. A worker holds its selected
        # lock across snapshot reads and the complete desktop lifetime.
        with self.edit_guard(p['id']):
            tombstone=self.root/'purged-profiles'/p['id']
            if tombstone.exists() or tombstone.is_symlink():
                raise UserError('该配置已永久删除，请新建条目。')
            current=self.secret_path(p['id'])
            if current.is_symlink(): raise UserError('配置路径不安全。')
            revision=json.loads(current.read_text()).get('revision',0) if current.exists() else 0
            if 'revision' in data and data['revision'] != revision:
                raise UserError('此配置已在其他窗口改变，请放弃旧草稿并重新编辑。')
            removed=self.root/'removed-profiles'
            if removed.is_symlink(): raise UserError('已移除配置的路径不安全。')
            archived=removed/f"{p['id']}.json"
            if archived.exists() or archived.is_symlink():
                raise UserError('该配置已移除，请先恢复后再编辑。')
            previous=json.loads(current.read_text()) if current.exists() else {}
            if self.community:
                if previous.get('key_ref'):self.checked_refs(p['id'],[previous['key_ref']])
                self.reclaim_pending_key(p['id'])
                ref=previous.get('key_ref') if p['kind']=='api' else None
                created=None
                retired=self.checked_refs(p['id'],previous.get('retired_key_refs',[]))
                if ref:self.checked_refs(p['id'],[ref])
                if p['kind']=='api':
                    if key:
                        ref=created='chc_'+p['id']+'_'+uuid.uuid4().hex
                        # Journal ownership first: lost Keychain acknowledgements
                        # must not leave untracked credentials after a crash.
                        self.key_refs(p['id'],ref)
                        try:self.vault().put(ref,key)
                        except BaseException:
                            try:self.vault().delete(ref)
                            except (UserError,OSError):pass
                            raise
                    elif not ref:raise UserError('请填写 API Key。')
                if previous.get('key_ref') and previous['key_ref']!=ref:retired.append(previous['key_ref'])
                try:
                    write_json(current,{'profile':p,'key_ref':ref,'retired_key_refs':list(dict.fromkeys(retired)),'revision':revision+1})
                except BaseException:
                    if created:self.vault().delete(created)
                    raise
                # Never overwrite the old secret before metadata publication.
                for retired_ref in retired:
                    try:self.vault().delete(retired_ref)
                    except (OSError,UserError):pass  # Retained reference makes a later purge retryable.
            elif p['kind']=='api':
                key=key or self.secret(p['id'])
                if not key: raise UserError('请填写 API Key。')
                write_json(current,{'profile':p,'key':key,'revision':revision+1})
            else:write_json(current,{'profile':p,'key':'','revision':revision+1})
        return self.public(p)

    @contextmanager
    def operation_guard(self, ident, *, shared=False):
        if not ID.fullmatch(ident): raise UserError('配置 ID 无效。')
        path=directory(self.root/'locks')/f'operation-{ident}.lock'
        if path.is_symlink(): raise UserError('配置操作锁路径不安全。')
        with path.open('a') as handle:
            try: fcntl.flock(handle,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
            except BlockingIOError:
                raise UserError('该配置正在保存、移除或测活，请等待当前操作完成。') from None
            yield handle

    @contextmanager
    def probe_guard(self, ident, alias):
        # Readers may probe distinct models concurrently; config writes remain
        # exclusive. A second lock prevents duplicate calls from other windows.
        import hashlib
        if not isinstance(alias,str): raise UserError('模型名称无效。')
        with self.operation_guard(ident,shared=True):
            suffix=hashlib.sha256(alias.encode()).hexdigest()
            path=directory(self.root/'locks')/f'probe-{ident}-{suffix}.lock'
            if path.is_symlink(): raise UserError('测活锁路径不安全。')
            with path.open('a') as handle:
                try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    raise UserError('该模型正在测活，本次没有重复发送请求。') from None
                yield

    @contextmanager
    def edit_guard(self, ident):
        with self.operation_guard(ident) as operation, ExitStack() as stack:
            from accounts import account_lock_paths, take_lock
            handles=[operation]
            for path in account_lock_paths(self,ident): handles.append(stack.enter_context(take_lock(path)))
            for storage in [s['id'] for s in self.registry.entries()]:
                handle=stack.enter_context(self.lock_path(storage,ident).open('a'))
                handles.append(handle)
                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise UserError('这个配置仍有运行中的窗口，请先退出对应窗口再修改。') from None
                if self.live_desktop(storage,ident):
                    raise UserError('对应窗口仍在运行，需先从 Harbor 退出该实例。')
                handles.append(stack.enter_context(take_lock(self.instance_backend_lock(storage,ident))))
                handles.append(stack.enter_context(take_lock(self.instance_backend_lock(storage,ident,v2=True))))
            yield tuple(h.fileno() for h in handles)

    def removed_profiles(self):
        result=[]
        removed=self.root/'removed-profiles'
        if removed.is_symlink(): raise UserError('已移除配置的路径不安全。')
        for path in sorted(removed.glob('*.json')):
            if path.is_symlink(): raise UserError('已移除配置的路径不安全。')
            value=json.loads(path.read_text())
            p=validated_profile(value['profile'])
            if p['id']!=path.stem: raise UserError('恢复副本的配置 ID 不匹配。')
            result.append(dict(p,has_key=p['kind']=='api' and bool(value.get('key_ref') or value.get('key')),revision=value.get('revision',0)))
        return result

    def remove_profile(self, ident, revision=None):
        with self.edit_guard(ident):
            self.profile(ident)
            source=self.secret_path(ident)
            target=directory(self.root/'removed-profiles')/source.name
            if target.exists() or target.is_symlink(): raise UserError('已有同名的恢复副本，未覆盖。')
            value=json.loads(source.read_text())
            if revision is not None and revision != value.get('revision',0):
                raise UserError('配置已改变，请刷新后重新确认移除。')
            value['revision']=value.get('revision',0)+1
            write_json(source,value)
            os.rename(source,target)
        return {'message':'已移除配置，可在“已移除配置”中恢复。账号文件和会话历史均保留。'}

    def restore_profile(self, ident, revision=None):
        with self.edit_guard(ident):
            if (self.root/'purged-profiles'/ident).exists():
                raise UserError('该条目已开始永久删除，不能恢复；请重试永久删除以完成清理。')
            target=self.secret_path(ident)
            source=directory(self.root/'removed-profiles')/target.name
            if source.is_symlink() or not source.is_file(): raise UserError('找不到可恢复的配置。')
            if target.exists() or target.is_symlink(): raise UserError('当前配置已存在，未覆盖。')
            value=json.loads(source.read_text())
            if validated_profile(value['profile'])['id']!=ident: raise UserError('恢复副本的配置 ID 不匹配。')
            if revision is not None and revision != value.get('revision',0):
                raise UserError('恢复副本已改变，请刷新后重试。')
            value['revision']=value.get('revision',0)+1
            write_json(source,value)
            os.rename(source,target)
        return self.public(self.profile(ident))

    def purge_profile(self, ident, revision):
        """Only entry-owned credentials; never recursively remove a history tree."""
        from storage import safe_path
        with self.edit_guard(ident), self.registry.guard():
            source=self.root/'removed-profiles'/f'{ident}.json'
            safe_path(source.parent)
            if source.is_symlink() or not source.is_file() or self.secret_path(ident).exists():
                raise UserError('只能永久删除可恢复列表中的配置。')
            value=json.loads(source.read_text())
            if value['profile']['id'] != ident: raise UserError('配置 ID 不匹配。')
            if revision != value.get('revision',0):
                raise UserError('待删除的配置已改变，请刷新后重新确认。未删除任何凭据。')
            targets=[source]
            account=self.root/'accounts'/ident
            targets += [account/'codex'/name for name in ['auth.json','.credentials.json']]
            targets += [account/'import-backup'/'auth.json']
            targets += [account/'refresh-owner'/'codex'/'auth.json']
            private_instances=self.root/'instances'/ident
            safe_path(private_instances)
            if private_instances.exists():
                for scope in private_instances.iterdir():
                    safe_path(scope)
                    if not scope.is_dir():continue
                    for slot in scope.iterdir():
                        safe_path(slot)
                        if slot.is_dir():targets += [slot/'codex'/name for name in ['auth.json','.credentials.json']]
            for location in self.registry.credential_locations():
                if not self.registry.disk_available(location):
                    raise UserError('有登记过的会话磁盘离线或身份不匹配；请连接正确磁盘后再永久删除。未删除任何凭据。')
                targets += [Path(location['path'])/'instances'/ident/'codex'/name for name in ['auth.json','.credentials.json']]
            for path in targets:
                safe_path(path.parent)
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise UserError('凭据路径异常，未删除。')
            atomic_write(directory(self.root/'purged-profiles')/ident, 'purged\n')
            if self.community:
                for ref in set(self.checked_refs(ident,self.key_refs(ident)+value.get('retired_key_refs',[])+([value['key_ref']] if value.get('key_ref') else []))):
                    self.vault().delete(ref)
            # Archive is last, so partial IO failure remains visible and retryable.
            for path in targets[1:]+targets[:1]: path.unlink(missing_ok=True)
            if self.community:(self.root/'keychain-refs'/f'{ident}.json').unlink(missing_ok=True)
        return {'message':'已永久删除该配置和可访问的专属凭据；会话目录与记录保留。'}

    def lock_path(self,storage,ident):
        if (storage not in ['local','external','original-local','original-external'] and not ID.fullmatch(storage)) or not ID.fullmatch(ident):
            raise UserError('实例标识无效。')
        f=directory(self.root/'locks')/f'{storage}-{ident}.lock'
        if f.is_symlink():raise UserError('运行锁路径不安全。')
        return f

    def live_desktop(self,storage,ident):
        for f in (self.root/'jobs').glob('*.json'):
            if f.is_symlink():continue
            job=json.loads(f.read_text())
            if job.get('id')==ident and job.get('storage')==storage and (process_matches(job) or launch_uncertain(job)):return job
        return None

    def instance_backend_lock(self,storage,ident,*,v2=False):
        suffix='v2-backend' if v2 else 'backend'
        return self.lock_path(storage,ident).with_name(f'{storage}-{ident}-{suffix}.lock')

    def running_profile(self, ident):
        from accounts import account_lock_paths, take_lock
        for path in account_lock_paths(self,ident):
            try:
                with take_lock(path): pass
            except UserError: return True
        for storage in [s['id'] for s in self.registry.entries()]:
            if self.live_desktop(storage,ident):return True
            try:
                with take_lock(self.instance_backend_lock(storage,ident)): pass
                with take_lock(self.instance_backend_lock(storage,ident,v2=True)): pass
            except UserError: return True
            f=self.root/'locks'/f'{storage}-{ident}.lock'
            if not f.exists(): continue
            if f.is_symlink(): raise UserError('运行锁路径不安全。')
            with f.open('a') as handle:
                try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: return True
        return False

    def storage_running(self, storage):
        from accounts import account_lock_paths, take_lock
        for p in self.profiles():
            if p['kind']!='official': continue
            _,lease=account_lock_paths(self,p['id'])
            try:
                with take_lock(lease): pass
            except UserError:
                marker=self.root/'accounts'/p['id']/'launch-generation.json'
                if marker.is_symlink(): raise UserError('账号运行标记不安全。')
                if not marker.exists() or json.loads(marker.read_text()).get('storage')==storage: return True
        for path in (self.root/'locks').glob(f'{storage}-*.lock'):
            if path.is_symlink(): raise UserError('运行锁路径不安全。')
            with path.open('a') as handle:
                try: fcntl.flock(handle, fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: return True
        return any(self.live_desktop(storage, p['id']) for p in self.profiles())

    def storage_snapshot(self):
        return self.registry.snapshot()

    def save_storages(self, items, revision):
        return self.registry.save(items, revision)

    def initialize_storage(self, ident, *, revision=None, path=None):
        return self.registry.initialize(ident, revision=revision, path=path)

    def select_storage(self, selection):
        return self.registry.select(selection)

    def prepare(self, ident, selection, model=None, *, launch_job=None, instance_slot=None):
        p=self.profile(ident)
        explicit_model=model is not None
        storage=self.select_storage(selection)
        aliases=[m['alias'] for m in p['models']] if p['kind']=='api' else [m['id'] for m in model_options()]
        if model is None: model='gpt-6-luna' if 'gpt-6-luna' in aliases else aliases[0]
        if model not in aliases: raise UserError('这个账号/API 没有配置所选模型。')
        location=self.registry.entry(storage)
        base=directory(Path(self.registry.runtime_location(location)['path']))
        with self.registry.open_history(location) as history:
            from accounts import account_home, bind_account_cache, bind_link
            official=p['kind']=='official'
            slot=instance_slot or launch_job
            if slot:
                from instances import runtime
                paths=runtime(self,ident,storage,slot,launch_job)
                home,electron,os_home=(paths[key] for key in ('home','electron','os_home'))
            else:
                home=account_home(self,ident,launch_job) if official else directory(base/'instances'/ident/'codex')
                electron=directory(base/'instances'/ident/'desktop')
                os_home=directory(base/'instances'/ident/'os-home')
            from organization import Organization
            Organization(self).prepare(home,history)
            if official and slot in (None,'primary'): bind_account_cache(self,ident,storage,home)
            workspace=directory(base/'workspace')
            for rel in ['sessions','archived_sessions','thread-writer-locks','rollout-migrations']:
                bind_link(home/rel,history/rel,official)
            directory(home/'.tmp')
            bind_link(home/'.tmp/rollout-maintenance.lock',history/'.tmp/rollout-maintenance.lock',official)
            directory(os_home/'tmp')
            # A private shell dot directory prevents .zshrc from injecting old routes.
            for name in ['.zshenv','.zprofile','.zshrc','.zlogin']:
                if not (os_home/name).exists(): atomic_write(os_home/name,'# Codex Harbor private shell environment\n')
            return dict(id=ident,storage=storage,control=str(self.root),external=str(self.external),base=str(base),history=str(history),home=str(home),
                        native_user_home=str(self.user_home) if self.user_home else None,
                        electron=str(electron),os_home=str(os_home),workspace=str(workspace),model=model,explicit_model=explicit_model,
                        profile=p,storage_paths=self.registry.sensitive_paths(),existing_home=history!=base/'history',
                        multi_instance=bool(slot),instance_slot=slot)


def configure(prepared, proxy_url=None, proxy_token=None):
    access_mode=prepared.get('access_mode','native')
    if access_mode not in ['native','readonly','workspace']: raise UserError('实例权限模式无效。')
    permission='harbor-workspace' if access_mode=='workspace' else 'harbor-read-only'
    home=Path(prepared['home']); p=prepared['profile']
    from native_defaults import refresh_resources, shell_environment
    refresh_resources(prepared)
    shell_environment(prepared)
    mapping=next((m for m in p['models'] if m['alias']==prepared['model']),None)
    models=copy.deepcopy(catalog())
    if p['kind']=='api':
        allowed={m['alias']:m for m in p['models']}
        models=[m for m in models if m['slug'] in allowed]
        for m in models:
            item=allowed[m['slug']]
            # GPT aliases otherwise inherit code_mode_only's custom `exec`.
            # Native direct tools keep command/file tools without that extension.
            m['tool_mode']='direct'
            m['prefer_websockets']=False
            m['use_responses_lite']=False
            m['visibility']='list'
            m['display_name']=m['display_name']+' → '+item['upstream']
            m['context_window']=item['context']
            m['max_context_window']=item['context']
            m['auto_compact_token_limit']=int(item['context']*0.8)
            from reasoning import configure_model
            configure_model(item,m)
        if not proxy_url or not proxy_token: raise UserError('API 转发器尚未启动。')
        write_json(home/'auth.json',{'auth_mode':'apikey','OPENAI_API_KEY':proxy_token})
    model_file=home/'smart2-models.json'
    model_text=json.dumps({'models':models},ensure_ascii=False,indent=2)+'\n'
    if model_file.is_symlink(): raise UserError('模型配置不能是符号链接。')
    if not model_file.exists() or model_file.read_text()!=model_text:
        atomic_write(model_file,model_text)
    lines=['model_provider = "openai"', f'sqlite_home = {json.dumps(prepared["history"])}',
           'cli_auth_credentials_store = '+json.dumps('ephemeral' if p['kind']=='official' and prepared.get('multi_instance') else 'file'),
           'mcp_oauth_credentials_store = "file"']
    # Official defaults belong to Codex. A Router needs a configured alias and
    # effort; an explicit official model request changes only the selected model.
    if p['kind']=='api' or prepared.get('explicit_model'):
        lines.append(f'model = {json.dumps(prepared["model"])}')
    if mapping:
        lines.append(f'model_reasoning_effort = {json.dumps(mapping["effort"])}')
    if p['kind']=='api': lines.append(f'model_catalog_json = {json.dumps(str(home/"smart2-models.json"))}')
    if proxy_url: lines.append('openai_base_url = '+json.dumps(proxy_url))
    if access_mode=='native':
        # Leave approvals, filesystem/network access and official feature defaults
        # to Codex. Router transport keeps only required compatibility settings.
        if p['kind']=='api':
            lines.extend(['[features]','enable_request_compression = false','api_key_model_discovery = false'])
        from configuration import save
        save(prepared,'\n'.join(lines)+'\n')
        return
    # Explicit isolated acceptance harnesses can still request test protection.
    lines.extend(['approval_policy = "on-request"', f'default_permissions = "{permission}"',
                  'check_for_update_on_startup = false', 'web_search = "disabled"'])
    protected_homes={str(Path.home()/'.codex'),*[p for _,p in MACHINE.get('original_homes',{}).values()]}
    if prepared.get('existing_home'): protected_homes.add(prepared['history'])
    lines.extend([f'[permissions.{permission}]',
                  'description = "Harbor：原版数据与账号凭据禁止工具访问"',
                  'extends = ":workspace"' if access_mode=='workspace' else 'extends = ":read-only"',
                  f'[permissions.{permission}.filesystem]']+
                 [json.dumps(path)+' = "deny"' for path in sorted(protected_homes)]+[
                  json.dumps(str(Path.home()/'Library/Application Support/Codex'))+' = "deny"',
                  json.dumps(str(Path.home()/'Library/Application Support/ChatGPT'))+' = "deny"',
                  json.dumps(str(Path(prepared['control'])/'profiles'))+' = "deny"',
                  json.dumps(str(Path(prepared['control'])/'removed-profiles'))+' = "deny"',
                  json.dumps(str(Path.home()/'Library/Application Support/Codex Smart-2'))+' = "deny"',
                  json.dumps(str(VOLUME/'Harnesses/CodexSmart2'))+' = "deny"',
                  json.dumps(str(Path(prepared['control'])/'accounts'))+' = "deny"',
                  json.dumps(str(Path(prepared['home'])/'auth.json'))+' = "deny"',
                  json.dumps(str(Path(prepared['home'])/'.credentials.json'))+' = "deny"'])
    for base in map(Path,prepared['storage_paths']):
        for suffix in ['*/codex/auth.json','*/codex/.credentials.json','*/desktop']:
            lines.append(json.dumps(str(base/'instances'/suffix))+' = "deny"')
    lines.extend([f'[permissions.{permission}.network]','enabled = false'])
    lines.extend(['[analytics]','enabled = false','[feedback]','enabled = false',
                  '[features]','enable_request_compression = false',
                  'api_key_model_discovery = false','runtime_metrics = false',
                  'apps = false','remote_plugin = false','computer_use = false',
                  'shell_snapshot = false','shell_snapshot_v2 = false',
                  '[memories]','generate_memories = false','use_memories = false',
                  '[otel]','exporter = "none"','trace_exporter = "none"','metrics_exporter = "none"'])
    from configuration import save
    save(prepared,'\n'.join(lines)+'\n')


def environment(prepared):
    osh=Path(prepared['os_home'])
    env = {'HOME':str(osh),'ZDOTDIR':str(osh),'CFFIXED_USER_HOME':str(osh),
            'CFPREFERENCES_AVOID_DAEMON':'1','XDG_CONFIG_HOME':str(osh/'.config'),
            'XDG_CACHE_HOME':str(osh/'.cache'),'XDG_DATA_HOME':str(osh/'.local/share'),
            'TMPDIR':prepared.get('temporary',str(osh/'tmp')),'CODEX_HOME':prepared.get('short_home',prepared['home']),
            'CODEX_SQLITE_HOME':prepared['history'],
            'CODEX_ELECTRON_USER_DATA_PATH':prepared['electron'],
            'PATH':'/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin',
            'LANG':'zh_CN.UTF-8','LC_ALL':'en_US.UTF-8',
            'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'}
    if prepared.get('access_mode', 'native') != 'native':
        # Explicit restricted acceptance modes never perform application updates.
        # Daily launches leave both lifecycles to the signed native desktop.
        env.update(CODEX_SPARKLE_ENABLED='false',
                   CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH='1')
    if prepared.get('native_user_home'):
        env['HOME'] = prepared['native_user_home']
        # Let Codex and tools discover normal per-user resources. Account auth,
        # runtime caches and browser sessions still have separate explicit paths.
        for k in ('CFFIXED_USER_HOME', 'CFPREFERENCES_AVOID_DAEMON', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME'):
            env.pop(k, None)
        user = pwd.getpwuid(os.getuid())
        env.update(USER=user.pw_name, LOGNAME=user.pw_name, SHELL=user.pw_shell or '/bin/zsh')
        env['PATH'] += ':' + str(Path(prepared['native_user_home']) / '.local/bin') + ':/usr/local/bin'
        if prepared.get('access_mode', 'native') == 'native':
            # NSWorkspace/native shell discovery supplies these ordinary user
            # defaults. Only the backend's loopback transport needs proxy bypass.
            for key in ('PATH', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'SHELL', 'NO_PROXY', 'no_proxy'):
                env.pop(key, None)
    return env
