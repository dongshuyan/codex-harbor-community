"""Owned build workspaces and verified archives; never imports runtime settings."""
import fcntl
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid
import warnings
import zipfile

CACHE = Path.home() / 'Library/Caches/CodexHarborBuild.noindex'
LSREGISTER = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
OWNED = re.compile(r'work-([0-9a-f]{32})\.noindex\Z')
RETIRED = re.compile(r'retired-([0-9a-f]{32})\.noindex\Z')


def safe_components(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink(): raise ValueError('Artifact path contains a symlink')
    return path


def private_dir(path):
    path = safe_components(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError('Artifact directory is not owned by this user')
    path.chmod(0o700)
    return path


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def flush_fd(fd):
    os.fsync(fd)
    if sys.platform == 'darwin':
        try: fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.ENOTTY): raise
            warnings.warn('F_FULLFSYNC is unavailable on this filesystem; only fsync durability is available', RuntimeWarning)


def sync_file(path):
    fd = os.open(safe_components(path), os.O_RDONLY | os.O_NOFOLLOW)
    try: flush_fd(fd)
    finally: os.close(fd)


def sync_directory(path):
    fd = os.open(safe_components(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: flush_fd(fd)
    finally: os.close(fd)


def sync_tree(root):
    inventory(root)
    for path in Path(root).rglob('*'):
        if path.is_file(): sync_file(path)
    for path in sorted(Path(root).rglob('*'), reverse=True):
        if path.is_dir(): sync_directory(path)
    sync_directory(root)


def inventory(root):
    root = safe_components(root)
    if not root.is_dir(): raise ValueError('Expected a real app directory')
    result = {}
    for p in [root, *sorted(root.rglob('*'))]:
        info = p.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('Bundles with symlinks or special files are not supported')
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError('Bundles with hardlinked files are not supported')
        result[str(p.relative_to(root))] = [stat.S_IMODE(info.st_mode),
                                           sha256(p) if p.is_file() else None]
    return result


def unregister(app):
    # Only called for exact paths inside an owned build/update workspace.
    result = subprocess.run([LSREGISTER, '-u', str(app)], capture_output=True, text=True, timeout=20)
    if result.returncode:
        # A never-registered .noindex bundle reports kLSApplicationNotFoundErr.
        # Absence is already the desired state; other failures keep the workspace.
        expected = 'failed to scan ' + str(app) + ': -10814'
        if result.returncode != 1 or expected not in result.stdout + result.stderr:
            raise ValueError('Could not unregister an owned build app')


def marker_valid(path, cache):
    match = OWNED.fullmatch(path.name) or RETIRED.fullmatch(path.name)
    if not match or path.parent != cache or path.is_symlink(): return False
    marker = path / 'owner.json'
    lock = path / 'owner.lock'
    if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077: return False
    try:
        for file in (marker, lock):
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid(): return False
        value = json.loads(marker.read_text())
        return value == {'version': 1, 'path': str(cache / ('work-' + match[1] + '.noindex')), 'nonce': match[1]}
    except (OSError, ValueError): return False


def remove_workspace(path, cache):
    safe_components(path)
    if not path.exists(): return
    if not marker_valid(path, cache): raise ValueError('Unrecognized workspace; cleanup refused')
    for app in sorted(path.rglob('*.app'), reverse=True):
        if app.is_dir() and not app.is_symlink(): unregister(app)
    # Keep ownership and lock metadata until all program payloads are gone. A
    # crash halfway through rmtree can then be reclaimed on the next run.
    for child in path.iterdir():
        if child.name in ('owner.json', 'owner.lock'): continue
        if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
        else: child.unlink()
    (path / 'owner.json').unlink()
    (path / 'owner.lock').unlink()
    path.rmdir()
    sync_directory(cache)


def retired_path(path):
    match = OWNED.fullmatch(path.name)
    if not match: raise ValueError('Expected an active owned workspace')
    return path.with_name('retired-' + match[1] + '.noindex')


def retire_workspace(path, cache):
    if not marker_valid(path, cache): raise ValueError('Unrecognized workspace; retirement refused')
    retired = safe_components(retired_path(path))
    if retired.exists(): raise ValueError('Both active and retired workspaces exist')
    os.rename(path, retired)
    sync_directory(cache)
    return retired


def reclaim(cache):
    cache = private_dir(cache)
    for path in cache.iterdir():
        if not marker_valid(path, cache): continue
        fd = None
        try:
            fd = os.open(path / 'owner.lock', os.O_RDWR | os.O_NOFOLLOW)
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: continue
            remove_workspace(path, cache)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            warnings.warn(f'Harbor cleanup pending at {path}: {error}', RuntimeWarning)
        finally:
            if fd is not None: os.close(fd)


class Workspace:
    def __init__(self, cache=CACHE):
        self.cache = private_dir(cache)
        reclaim(self.cache)
        nonce = uuid.uuid4().hex
        self.path = private_dir(self.cache / ('work-' + nonce + '.noindex'))
        self.fd = None
        try:
            self.fd = os.open(self.path / 'owner.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = self.path / 'owner.json'
            marker.write_text(json.dumps({'version': 1, 'path': str(self.path), 'nonce': nonce}))
            marker.chmod(0o600)
            sync_file(marker); sync_file(self.path / 'owner.lock')
            sync_directory(self.path); sync_directory(self.cache)
        except BaseException:
            if self.fd is not None: os.close(self.fd)
            shutil.rmtree(self.path)  # Only the directory this constructor created.
            raise
        self.retain = False

    def __enter__(self): return self

    def __exit__(self, *_):
        try:
            if not self.retain: remove_workspace(self.path, self.cache)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            warnings.warn(f'Harbor cleanup pending at {self.path}: {error}', RuntimeWarning)
        finally: os.close(self.fd)


def check_zip(path):
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            p = Path(entry.filename)
            mode = entry.external_attr >> 16
            if (p.is_absolute() or '..' in p.parts or '\\' in entry.filename or
                    stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not
                    (stat.S_ISDIR(mode) or stat.S_ISREG(mode)))):
                raise ValueError('Unsafe archive member')
        if archive.testzip() is not None: raise ValueError('Corrupt archive')


def make_archive(app, destination):
    inventory(app)
    safe_components(destination)
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                    str(app), str(destination)], check=True)
    Path(destination).chmod(0o600)
    check_zip(destination)
    sync_file(destination); sync_directory(Path(destination).parent)


def extract_archive(archive, destination):
    check_zip(archive)
    destination = safe_components(destination)
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(destination)], check=True)
    # Preflight is additional protection; inspect the extracted tree itself.
    inventory(destination)
