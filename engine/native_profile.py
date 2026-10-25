"""Conservative, read-only occupancy check before projecting an original profile.

macOS can omit the environment of a signed process from KERN_PROCARGS2. Never
infer HOME/CODEX_HOME from missing environment values. Match kernel arguments
to our PID/start-time journal instead; an unrecognized desktop blocks a write.
"""
import ctypes
import errno
import json
from pathlib import Path
import subprocess
import sys

from core import APP_EXECUTABLE, UserError, process_stamp


def decode_argv(block):
    if len(block) < 5:
        raise ValueError('Truncated process arguments')
    count = int.from_bytes(block[:4], sys.byteorder, signed=True)
    if not 0 < count <= 65536:
        raise ValueError('Invalid argument count')
    offset = block.find(b'\0', 4)
    if offset < 0:
        raise ValueError('Missing executable terminator')
    while offset < len(block) and block[offset] == 0:
        offset += 1
    result = []
    for _ in range(count):
        end = block.find(b'\0', offset)
        if end < 0:
            raise ValueError('Truncated process argument')
        result.append(block[offset:end].decode('utf-8', 'surrogateescape'))
        offset = end + 1
    return result  # Remaining bytes may contain secrets. Never decode them.


def process_argv(pid):
    if sys.platform != 'darwin':
        raise OSError(errno.ENOSYS, 'Native occupancy requires macOS')
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.sysctl
    call.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                     ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    call.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t()
    if call(mib, 3, None, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), 'Cannot read process arguments')
    if not 4 < size.value <= 8 * 1024 * 1024:
        raise OSError(errno.EOVERFLOW, 'Unexpected process argument size')
    buffer = ctypes.create_string_buffer(size.value)
    if call(mib, 3, buffer, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), 'Cannot read process arguments')
    try:
        return decode_argv(buffer.raw[:size.value])
    except ValueError as exc:
        raise OSError(errno.EINVAL, 'Invalid process argument block') from exc


def desktop_processes():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True,
                            text=True, timeout=5, check=True)
    paths = {str(APP_EXECUTABLE)}
    # Both supported native distributions may be installed simultaneously.
    from distribution import discover_app
    for base in (Path('/Applications'), Path.home() / 'Applications'):
        for name in ('ChatGPT.app', 'Codex.app'):
            candidate = base / name
            if candidate.exists():
                paths.add(str(discover_app([candidate])[2]))
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[1] not in paths:
            continue
        pid = int(parts[0])
        before = process_stamp(pid)
        if before is None:
            continue
        try:
            argv = process_argv(pid)
        except OSError:
            argv = None
        after = process_stamp(pid)
        if after is None:
            continue
        found.append({'pid': pid, 'start': after,
                      'argv': argv if before == after else None})
    return found


def user_data_dir(argv):
    if argv is None:
        return None
    values = []
    for i, arg in enumerate(argv):
        if arg.startswith('--user-data-dir='):
            values.append(arg.split('=', 1)[1])
        elif arg == '--user-data-dir' and i + 1 < len(argv):
            values.append(argv[i + 1])
    if len(values) != 1 or not values[0] or not Path(values[0]).is_absolute():
        return None
    return Path(values[0]).resolve()


def require_idle(control_root, home, *, known_roots=()):
    """No writes, signals or credentials. Call under the common startup lock."""
    target = Path(home).resolve()
    for process in classify(control_root,known_roots=known_roots):
        if process['private_home'] is None:
            raise UserError('原生 Codex 配置仍在使用或占用无法确认；未写入。请退出原生窗口后重试，Harbor 独立窗口可以继续运行。')
        if Path(process['private_home']).resolve()==target:
            raise UserError('这个原生 Codex 配置仍在使用；未覆盖正在运行的窗口。')


def classify(control_root,*,known_roots=()):
    """Return only process identity/binding, never argv or environment to a UI."""
    try:
        records = []
        paths={path for root in (control_root,*known_roots) for path in (Path(root)/'jobs').glob('*.json')}
        for path in paths:
            try:
                row = json.loads(path.read_text())
                if isinstance(row, dict):
                    records.append(row)
            except (OSError, ValueError):
                continue  # A damaged journal cannot prove a live process safe.
        result=[]
        for process in desktop_processes():
            actual = user_data_dir(process['argv'])
            candidates = [row for row in records
                          if row.get('pid') == process['pid'] and process['start']
                          and row.get('pid_start') == process['start']]
            row=candidates[0] if len(candidates)==1 else {}
            known=bool(actual and row.get('electron') and row.get('home') and
                       not row.get('native_default_profile') and Path(row['electron']).resolve()==actual)
            result.append({'pid':process['pid'],'start':process['start'],
                           'private_home':row['home'] if known else None})
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        raise UserError('无法完成原生 Codex 占用检查；未写入原生配置。') from exc
