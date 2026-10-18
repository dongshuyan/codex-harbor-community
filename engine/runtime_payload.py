"""Pin launch code outside the replaceable app; never copy account or history data.

Lock order: existing application locks -> cache manager -> payload lease. No
account/storage locks are taken here. Lease files are permanent (no inode ABA).
All entrypoints acquire before spawn; a live desktop/uncertain job is a second
GC root if its worker and helper are killed. Unknown state is retained.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
import uuid
import warnings

MANIFEST = 'runtime-manifest.json'
CACHE = 'runtime-payloads.noindex'
COORDINATION = 1  # Shared auth/instance/organization protocols; change with incompatible changes.
HEX = re.compile(r'[0-9a-f]{64}\Z')
STAGE = re.compile(r'\.stage-[0-9a-f]{32}\Z')
ENV = ('HARBOR_RUNTIME_CACHE', 'HARBOR_RUNTIME_ID', 'HARBOR_RUNTIME_FD')
NOTICES = ('LICENSE', 'LICENSE-Codex', 'LICENSE-CHERRY-PROVIDER-REGISTRY',
           'LICENSE-TOMLKIT', 'LICENSE-MAGPIE', 'THIRD-PARTY-NOTICES.md', 'CREDITS.md')
_active = None


class PayloadError(ValueError): pass


def safe_path(path):
    path = Path(path).absolute()
    if path != Path(os.path.normpath(path)) or any(p.is_symlink() for p in (path, *path.parents)):
        raise PayloadError('运行组件路径不安全，未启动或清理。')
    return path


def private_dir(path):
    path = safe_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PayloadError('运行组件目录权限不安全，未启动或清理。')
    return path


def file_info(path):
    path = safe_path(path)
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022):
        raise PayloadError('运行组件文件类型或权限无效。')
    return info


def digest(path):
    file_info(path)
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''): value.update(block)
    return value.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def cleanup(path):
    """Optional cleanup must not abort a valid launch or mask its real failure."""
    try: shutil.rmtree(path)
    except OSError:
        warnings.warn('旧运行组件清理未完成，已保留供下次检查：' + path.name, RuntimeWarning)


def relative(name):
    if (not isinstance(name, str) or not name or '\\' in name
            or any(p in ('', '.', '..') for p in name.split('/')) or PurePosixPath(name).is_absolute()
            or not (name.startswith('engine/') or name in ('HarborLaunch', 'HarborKeychain', *NOTICES))):
        raise PayloadError('运行组件清单含有非程序路径。')
    return name


def write_manifest(resources, paths=None, *, coordination=COORDINATION):
    """Build/signing only: call after signing helpers, before signing the app."""
    resources = safe_path(resources)
    if paths is None: paths = read_manifest(resources)['files']
    edition = json.loads((resources / 'engine/edition.json').read_text())
    value = dict(schema=1, coordination=coordination, edition=edition['edition'], version=edition['version'],
                 files={relative(name): dict(sha256=digest(resources / name),
                        executable=bool(file_info(resources / name).st_mode & 0o111)) for name in sorted(paths)})
    (resources / MANIFEST).write_bytes(canonical(value) + b'\n')
    return value


def read_manifest(resources):
    path = resources / MANIFEST
    if file_info(path).st_size > 2 * 1024 * 1024: raise PayloadError('运行组件清单过大。')
    try: value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError): raise PayloadError('运行组件清单损坏。') from None
    if (not isinstance(value, dict) or set(value) != {'schema', 'coordination', 'edition', 'version', 'files'}
            or type(value['schema']) is not int or value['schema'] != 1 or type(value['coordination']) is not int or value['coordination'] < 1
            or value['edition'] not in ('private', 'community') or not isinstance(value['version'], str)
            or not isinstance(value['files'], dict) or not 1 <= len(value['files']) <= 4096):
        raise PayloadError('运行组件清单版本无效。')
    for name, item in value['files'].items():
        relative(name)
        if (not isinstance(item, dict) or set(item) != {'sha256', 'executable'}
                or not isinstance(item['sha256'], str) or not HEX.fullmatch(item['sha256'])
                or type(item['executable']) is not bool): raise PayloadError('运行组件文件清单损坏。')
    if not {'engine/main.py', 'engine/runtime_payload.py', 'engine/edition.json', 'HarborLaunch', 'HarborKeychain'} <= value['files'].keys():
        raise PayloadError('运行组件清单不完整。')
    return value


def identifier(manifest): return hashlib.sha256(canonical(manifest)).hexdigest()


def verify(resources, manifest, *, exact=False):
    if read_manifest(resources) != manifest: raise PayloadError('运行组件清单已变化。')
    for name, entry in manifest['files'].items():
        path = resources / name
        if digest(path) != entry['sha256'] or bool(path.stat().st_mode & 0o111) != entry['executable']:
            raise PayloadError('运行组件校验失败，未使用损坏的程序。')
    if exact:
        actual = set()
        for path in resources.rglob('*'):
            safe_path(path)
            if path.is_dir(): continue
            file_info(path)
            actual.add(str(path.relative_to(resources)))
        if actual != set(manifest['files']) | {MANIFEST}: raise PayloadError('运行组件目录中有未知文件。')


def open_lock(path):
    path = safe_path(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise PayloadError('运行组件锁权限无效。')
        return fd
    except BaseException:
        os.close(fd); raise


@contextmanager
def manager(path):
    fd = open_lock(path / 'manager.lock')
    try:
        deadline = time.monotonic() + 15
        while True:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline: raise PayloadError('运行组件正在更新，请稍后重试。') from None
                time.sleep(.03)
        yield
    finally: os.close(fd)


class Lease:
    def __init__(self, path, cache=None, fd=None):
        self.path, self.cache, self.fd = path, cache, fd
        self.engine = path / 'engine'

    @property
    def pass_fds(self): return (self.fd,) if self.fd is not None else ()

    def environment(self, *, inherit=True):
        if self.fd is None: return {}
        result = {ENV[0]: str(self.cache), ENV[1]: self.path.name}
        if inherit: result[ENV[2]] = str(self.fd)
        return result

    def close(self):
        if self.fd is not None: os.close(self.fd); self.fd = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class Cache:
    def __init__(self, control):
        self.control = safe_path(control)
        self.path = self.control / CACHE

    def setup(self):
        private_dir(self.path)
        private_dir(self.path / 'leases')

    def _job_roots(self):
        """None means an unreadable journal: retain everything, never guess."""
        roots = set()
        identities = None
        self.job_details = {}
        try:
            for path in (self.control / 'jobs').glob('*.json'):
                file_info(path)
                value = json.loads(path.read_bytes())
                if not isinstance(value, dict): return None
                ident = value.get('runtime_id')
                if not ident: continue  # Legacy jobs use app files, protected by the installer.
                if not isinstance(ident, str) or not HEX.fullmatch(ident): return None
                self.job_details.setdefault(ident, []).append(path.name + ' phase=' + str(value.get('desktop_launch_phase', 'unknown')))
                if value.get('desktop_launch_phase') in ('pending', 'requested'): roots.add(ident); continue
                for prefix in ('', 'worker_', 'launch_helper_'):
                    state = {'pid': value.get(prefix + 'pid'), 'pid_start': value.get(prefix + 'pid_start')}
                    pid, stamp = state['pid'], state['pid_start']
                    if pid is None: continue
                    if type(pid) is not int or pid <= 1 or not isinstance(stamp, str) or not stamp:
                        roots.add(ident); break
                    if identities is None:
                        result = subprocess.run(['/bin/ps', '-axo', 'pid=,lstart='], capture_output=True,
                                                text=True, timeout=3, env={**os.environ, 'LANG':'C', 'LC_ALL':'C'})
                        if result.returncode or result.stderr.strip() or not result.stdout.strip(): return None
                        identities = {}
                        for line in result.stdout.splitlines():
                            fields = line.strip().split(None, 1)
                            if len(fields) != 2 or not fields[0].isdigit() or not fields[1]: return None
                            identities[int(fields[0])] = fields[1]
                        if os.getpid() not in identities: return None
                    if identities.get(pid) == stamp:
                        roots.add(ident); break
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired): return None
        return roots

    def _entries(self):
        for path in self.path.iterdir():
            if HEX.fullmatch(path.name): yield path

    def _probe(self, ident):
        fd = open_lock(self.path / 'leases' / (ident + '.lock'))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd); return None

    def check_compatibility(self, coordination, roots):
        """Caller holds manager; also used around the installer's atomic swap."""
        if roots is None and coordination is None:
            raise PayloadError('无法确认运行实例状态，请先检查实例记录再回退旧版。')
        identities = {p.name for p in self._entries()} | (roots or set())
        identities.update(p.stem for p in (self.path/'leases').glob('*.lock') if HEX.fullmatch(p.stem))
        for ident in sorted(identities):
            path = self.path / ident
            fd = self._probe(path.name)
            try:
                if fd is not None and roots is not None and path.name not in roots: continue
                detail = '\n运行组件 ' + ident + '\n' + '; '.join(getattr(self, 'job_details', {}).get(ident, ['进程仍持有租约']))
                try:
                    manifest = read_manifest(path)
                    if identifier(manifest) != ident: raise PayloadError('运行组件标识不一致。')
                except (OSError, ValueError): raise PayloadError('无法确认仍被使用或待确认的运行组件版本。请保留记录并检查；未执行替换。' + detail) from None
                if coordination is None or manifest['coordination'] != coordination:
                    raise PayloadError('此版本与仍在运行或待确认的 Codex 实例不兼容。请退出可见窗口；未决启动记录须先核查，不能仅凭超时删除。' + detail)
            finally:
                if fd is not None: os.close(fd)

    def _prune(self, current, roots):
        # Timestamps rank only inactive versions; they never prove inactivity.
        entries = []
        for path in self._entries():
            if path.name == current: continue
            try:
                value = read_manifest(path)
                if identifier(value) != path.name: continue
                entries.append((path.stat().st_mtime, path))
            except (OSError, ValueError): continue  # Unknown/corrupt data is not ours to delete.
        for index, (_, path) in enumerate(sorted(entries, reverse=True)):
            if index == 0 or roots is None or path.name in roots: continue
            fd = None
            try:
                fd = self._probe(path.name)
                if fd is None: continue
                value = read_manifest(path)
                verify(path, value, exact=True)
                cleanup(path)
            except (OSError, ValueError): continue  # Keep uncertain data; cleanup is optional.
            finally:
                if fd is not None: os.close(fd)

    def acquire(self, source, *, edition):
        source = safe_path(source)
        manifest = read_manifest(source)
        if manifest['edition'] != edition: raise PayloadError('运行组件版本不属于此 Harbor 数据目录。')
        ident = identifier(manifest)
        self.setup()
        with manager(self.path):
            roots = self._job_roots()
            self.check_compatibility(manifest['coordination'], roots)
            # Only our marked, abandoned staging directories are reclaimed.
            for stage in self.path.iterdir():
                if STAGE.fullmatch(stage.name) and not stage.is_symlink():
                    try:
                        marker = stage / '.owned'
                        if (not marker.is_symlink() and marker.is_file() and file_info(marker).st_size < 64
                                and marker.read_text() == 'harbor-runtime-v1\n'):
                            cleanup(stage)
                    except (OSError, ValueError): continue
            destination = self.path / ident
            verify(source, manifest)
            if destination.exists() or destination.is_symlink():
                try: verify(destination, manifest, exact=True)
                except (OSError, ValueError):
                    # Never move running code, unknown intents or symlink targets.
                    safe_path(destination)
                    fd = self._probe(ident)
                    try:
                        info = destination.stat()
                        if (fd is None or roots is None or ident in roots or source == destination
                                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()):
                            raise PayloadError('运行组件损坏且仍被使用或待确认。请保留实例记录，退出相关窗口后重新启动；未修改运行文件。')
                        preserved = self.path / ('.damaged-' + uuid.uuid4().hex)
                        os.rename(destination, preserved)
                        sync_directory(self.path)
                        warnings.warn('损坏的运行组件已隔离保留：' + preserved.name, RuntimeWarning)
                    finally:
                        if fd is not None: os.close(fd)
            if not destination.exists():
                stage = private_dir(self.path / ('.stage-' + uuid.uuid4().hex))
                try:
                    (stage / '.owned').write_text('harbor-runtime-v1\n')
                    for name, entry in manifest['files'].items():
                        target = stage / name
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shutil.copyfile(source / name, target)
                        target.chmod(0o500 if entry['executable'] else 0o400)
                        with target.open('rb') as handle: os.fsync(handle.fileno())
                    (stage / MANIFEST).write_bytes(canonical(manifest) + b'\n')
                    (stage / MANIFEST).chmod(0o400)
                    with (stage / MANIFEST).open('rb') as handle: os.fsync(handle.fileno())
                    (stage / '.owned').unlink()
                    verify(stage, manifest, exact=True)
                    for folder in sorted((p for p in stage.rglob('*') if p.is_dir()), key=lambda p:len(p.parts), reverse=True):
                        sync_directory(folder)
                    sync_directory(stage)
                    os.rename(stage, destination)
                    sync_directory(self.path)
                finally:
                    if stage.exists(): cleanup(stage)
            fd = open_lock(self.path / 'leases' / (ident + '.lock'))
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                os.utime(destination, None)
                self._prune(ident, roots)
                return Lease(destination, self.path, fd)
            except BaseException: os.close(fd); raise


def for_launch(engine, control, *, community):
    resources = safe_path(engine).parent
    if not (resources / MANIFEST).exists():
        if (resources.parent / 'Info.plist').exists() or not (resources / 'release_manifest.json').is_file():
            raise PayloadError('安装包缺少独立运行组件，请重新安装完整版本。')
        return Lease(resources)  # Source-only development, never a packaged app.
    return Cache(control).acquire(resources, edition='community' if community else 'private')


def activate(engine):
    """Adopt an inherited lease before work, or acquire for a desktop respawn."""
    global _active
    if not any(name in os.environ for name in ENV): return
    cache, ident = os.environ.get(ENV[0], ''), os.environ.get(ENV[1], '')
    if not cache or not HEX.fullmatch(ident): raise PayloadError('运行组件租约标识无效。')
    cache = safe_path(cache)
    resources = safe_path(engine).parent
    if cache.name != CACHE or resources != cache / ident: raise PayloadError('运行组件租约与程序路径不一致。')
    manifest = read_manifest(resources)
    if identifier(manifest) != ident: raise PayloadError('运行组件版本校验失败。')
    inherited = os.environ.pop(ENV[2], None)
    if inherited is not None:
        try:
            fd = int(inherited)
            if fd < 3: raise ValueError()
            actual, expected = os.fstat(fd), file_info(cache / 'leases' / (ident + '.lock'))
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino): raise ValueError()
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (ValueError, OSError): raise PayloadError('运行组件租约接管失败。') from None
        os.set_inheritable(fd, False)
        verify(resources, manifest, exact=True)
        _active = Lease(resources, cache, fd)
    else:
        try: _active = Cache(cache.parent).acquire(resources, edition=manifest['edition'])
        except OSError: raise PayloadError('无法准备运行组件；请检查磁盘空间和目录权限后重试。') from None
    # These are bootstrap controls, not part of Codex's tool environment.
    # Future Harbor children receive fresh explicit metadata from the lease.
    for name in ENV: os.environ.pop(name, None)


def inherited_fds(): return _active.pass_fds if _active else ()
def environment(): return _active.environment(inherit=False) if _active else {}
def runtime_id(): return _active.path.name if _active else None


@contextmanager
def installation_guard(control, coordination):
    cache = Cache(control)
    if not cache.path.exists(): yield; return
    cache.setup()
    with manager(cache.path):
        roots = cache._job_roots()
        if roots is None:
            raise PayloadError('无法读取实例记录并确认共享协议兼容性，未更新或回退。请保留 jobs 记录供核查。')
        cache.check_compatibility(coordination, roots)
        yield
