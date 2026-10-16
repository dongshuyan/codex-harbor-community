"""One serialized native refresh authority; desktop instances receive access tokens.

Only auth.json enters the private refresh home. A journal retains a rotated token
when the caller dies or times out. An ambiguous attempt is never retried with its
old refresh token. No credential is passed through argv, environment, or logs.
"""
from contextlib import contextmanager, ExitStack
import base64
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import time

from core import CODEX, ID, UserError, atomic_write, directory, process_matches, process_stamp, write_json
from login import RPC

MAX_AUTH = 1024 * 1024
REFRESH_SKEW = 90
OWNER_CONFIG = '''cli_auth_credentials_store = "file"
mcp_oauth_credentials_store = "file"
model_provider = "harbor_auth_owner"
[model_providers.harbor_auth_owner]
name = "Harbor native auth refresh"
base_url = "http://127.0.0.1:1"
wire_api = "responses"
requires_openai_auth = false
[features]
apps = false
plugins = false
remote_plugin = false
runtime_metrics = false
[analytics]
enabled = false
[feedback]
enabled = false
'''


def _failure(message='官方账号登录刷新未完成，请在 Harbor 重新登录后重试。'):
    return UserError(message)


def _string(value, limit=65536):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(c.isprintable() and not c.isspace() for c in value))


def _claims(token):
    try:
        part = token.split('.')[1]
        value = json.loads(base64.b64decode(part + '=' * (-len(part) % 4), altchars=b'-_', validate=True))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, UnicodeError, RecursionError):
        return {}


def _identity(value):
    tokens = value['tokens']
    claims = _claims(tokens['id_token'])
    auth = claims.get('https://api.openai.com/auth')
    user = auth.get('chatgpt_user_id') if isinstance(auth, dict) else None
    access_auth = _claims(tokens['access_token']).get('https://api.openai.com/auth')
    access_user = access_auth.get('chatgpt_user_id') if isinstance(access_auth, dict) else None
    if (access_user is not None and not _string(access_user, 512)
            or user is not None and access_user is not None and user != access_user):
        raise _failure('账号身份信息不一致，未刷新登录。')
    user = user or claims.get('sub')
    if user is not None and not _string(user, 512):
        raise _failure('账号身份信息无效，未刷新登录。')
    return [tokens['account_id'], user]


def _read(path):
    directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_AUTH:
                raise _failure('官方账号凭据文件不安全，未刷新登录。')
            raw = handle.read(MAX_AUTH + 1)
        value = json.loads(raw)
        tokens = value.get('tokens') if isinstance(value, dict) else None
        if (len(raw) > MAX_AUTH or not isinstance(tokens, dict)
                or value.get('auth_mode') not in (None, 'chatgpt')
                or not all(_string(tokens.get(k)) for k in ('access_token', 'refresh_token', 'id_token'))
                or not _string(tokens.get('account_id'), 512)):
            raise _failure()
        _identity(value)
        stamp = f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'
        revision = hashlib.sha256(stamp.encode() + raw).hexdigest()
        return value, raw, revision
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, UserError):
            raise
        raise _failure() from None


def _fresh(value):
    expires = _claims(value['tokens']['access_token']).get('exp')
    return (isinstance(expires, (int, float)) and not isinstance(expires, bool)
            and math.isfinite(expires) and expires > time.time() + REFRESH_SKEW)


def _native_may_refresh_on_startup(value):
    """Native managed auth refreshes within five minutes, or after eight days.

    If that attempt produced no new tokens, do not immediately force another
    request with the same refresh token: its server-side outcome is uncertain.
    """
    expires = _claims(value['tokens']['access_token']).get('exp')
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and math.isfinite(expires):
        return expires <= time.time() + 300
    try:
        last = datetime.datetime.fromisoformat(value['last_refresh'].replace('Z', '+00:00'))
        return last.timestamp() < time.time() - 8 * 86400
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _external(value):
    tokens = value['tokens']
    result = {'type': 'chatgptAuthTokens', 'accessToken': tokens['access_token'],
              'chatgptAccountId': tokens['account_id']}
    for token in (tokens['access_token'], tokens['id_token']):
        auth = _claims(token).get('https://api.openai.com/auth')
        plan = auth.get('chatgpt_plan_type') if isinstance(auth, dict) else None
        if _string(plan, 80):
            result['chatgptPlanType'] = plan
            break
    return result


@contextmanager
def _lock(path, deadline, *, shared=False):
    directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        while True:
            try:
                fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _failure('官方账号正在刷新或修改，等待超时，请稍后重试。') from None
                time.sleep(min(.03, remaining))
        if time.monotonic() >= deadline:
            raise _failure('官方账号刷新等待超时。')
        yield handle


def _legacy_idle(store, ident):
    for path in (store.root / 'jobs').glob('*.json'):
        if path.is_symlink():
            raise _failure('实例状态路径不安全，未刷新登录。')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise _failure('实例状态无效，未刷新登录。')
        if value.get('id') != ident:
            continue
        version = value.get('lifecycle_version', 1)
        if not isinstance(version, int) or isinstance(version, bool):
            raise _failure('实例状态版本无效，未刷新登录。')
        if version >= 2:
            continue
        worker = {'pid': value.get('worker_pid'), 'pid_start': value.get('worker_pid_start')}
        if process_matches(value) or process_matches(worker):
            raise _failure('此账号的旧版实例仍在运行，请退出旧版实例后重试。')


def _cleanup(owner, journal):
    if owner.is_symlink() or journal.is_symlink():
        raise _failure('账号刷新恢复路径不安全，已保留登录状态。')
    if owner.exists():
        shutil.rmtree(owner)
    journal.unlink(missing_ok=True)


def _recover(canonical, owner, journal):
    """Adopt a completed native rotation only after its process is known dead."""
    if not journal.exists():
        if journal.is_symlink() or owner.exists() or owner.is_symlink():
            raise _failure('存在无法核对的账号刷新目录，请先重新登录。')
        return
    if journal.is_symlink():
        raise _failure('账号刷新恢复记录不安全。')
    try:
        state = json.loads(journal.read_text())
        if (not isinstance(state, dict) or state.get('version') != 1
                or not isinstance(state.get('source_revision'), str)):
            raise ValueError()
    except (ValueError, OSError, RecursionError):
        raise _failure('账号刷新恢复记录无效，已保留登录状态。') from None
    if state.get('phase') in ('prepared', 'not_started', 'committed'):
        _cleanup(owner, journal)
        return
    if state.get('phase') not in ('running', 'finished') or not state.get('pid_start'):
        raise _failure('上次账号刷新是否结束尚不能确认，已保留恢复文件，请重新登录。')
    if process_matches(state):
        raise _failure('上次账号刷新进程仍在运行，请等待结束后重试。')
    candidate, raw, _ = _read(owner / 'codex' / 'auth.json')
    current, current_raw, revision = _read(canonical)
    digest = hashlib.sha256(raw).hexdigest()
    if state.get('candidate_digest') == digest and current_raw == raw:
        _cleanup(owner, journal)  # Atomic commit completed before journal cleanup.
        return
    if revision != state['source_revision']:
        raise _failure('刷新期间账号登录已改变，未覆盖新登录；请重新登录以确认账号。')
    if _identity(candidate) != state.get('source_identity') or _identity(candidate) != _identity(current):
        raise _failure('刷新返回了不同账号，已保留原登录并停止。')
    if all(candidate['tokens'][key] == current['tokens'][key] for key in ('access_token', 'refresh_token')):
        raise _failure('上次刷新没有可确认的新凭据，已保留恢复文件；请重新登录。')
    # Journal before commit so a crash after os.replace is recognizable.
    state['candidate_digest'] = digest
    write_json(journal, state)
    # Read again immediately before publication; cooperating writers hold the account lease.
    if _read(canonical)[2] != state['source_revision']:
        raise _failure('刷新期间账号登录已改变，未覆盖新登录。')
    atomic_write(canonical, raw.decode('utf-8'))
    state['phase'] = 'committed'
    write_json(journal, state)
    _cleanup(owner, journal)


class _NativeRPC(RPC):
    def __init__(self, command, env, cwd, lease_fds):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, cwd=cwd, close_fds=True,
            start_new_session=True, pass_fds=lease_fds)
        self.buffer = b''
        self.pending = []

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=.2)
        self.proc.stdin.close()
        self.proc.stdout.close()


def _run_native(owner, journal, deadline, command, lease_fds):
    state = json.loads(journal.read_text())
    home = owner / 'codex'
    before = _read(home / 'auth.json')[0]
    os_home = directory(owner / 'os-home')
    env = {'HOME': str(os_home), 'CODEX_HOME': str(home), 'ZDOTDIR': str(os_home),
           'CODEX_SQLITE_HOME': str(directory(owner / 'state')),
           'TMPDIR': str(directory(owner / 'tmp')), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
           'LANG': 'en_US.UTF-8'}
    rpc = None
    try:
        if deadline - time.monotonic() <= .25:
            state['phase'] = 'not_started'
            write_json(journal, state)
            raise TimeoutError()
        state['phase'] = 'starting'
        write_json(journal, state)
        try:
            rpc = _NativeRPC(command if command is not None else [str(CODEX), 'app-server'], env, owner, lease_fds)
        except OSError:
            state['phase'] = 'not_started'
            write_json(journal, state)
            raise
        state.update(phase='running', pid=rpc.proc.pid, pid_start=process_stamp(rpc.proc.pid))
        write_json(journal, state)
        if not state['pid_start']:
            raise _failure('无法核对原生账号刷新进程，已停止刷新。')
        rpc_deadline = deadline - .25
        rpc.request('initialize', {'clientInfo': {'name': 'codex_harbor_auth', 'version': '0.9.0'}},
                    'harbor-auth-init', rpc_deadline)
        rpc.send('initialized', {})
        after = _read(home / 'auth.json')[0]
        if (any(before['tokens'][key] != after['tokens'][key] for key in ('access_token', 'refresh_token'))
                or _native_may_refresh_on_startup(before)):
            return  # Startup already rotated, or may have attempted this token.
        # This native method does not wait for unrelated workspace discovery.
        rpc.request('getAuthStatus', {'includeToken': False, 'refreshToken': True},
                    'harbor-auth-refresh', rpc_deadline)
    finally:
        if rpc:
            rpc.close()
            # The process was reaped; even a too-fast exit is now unambiguous.
            state.update(phase='finished', pid_start=state.get('pid_start') or 'reaped-before-stamp')
            write_json(journal, state)


def credentials_for_instance(store, ident, *, previous_account_id=None,
                             rejected_access_token=None, force_refresh=False, timeout=20, command=None):
    """Return native external-auth params, refreshing once when needed.

    A caller reporting 401 must pass its rejected access token. Another caller's
    completed rotation then satisfies the request without spending a new refresh
    token. Timeout includes lock waiting; use eight seconds for native callbacks.
    ``command`` is an isolated test/native-compatibility injection, never user input.
    """
    if (not isinstance(ident, str) or not ID.fullmatch(ident)
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise _failure('账号刷新参数无效。')
    if previous_account_id is not None and not _string(previous_account_id, 512):
        raise _failure('账号刷新身份无效。')
    if rejected_access_token is not None and not _string(rejected_access_token):
        raise _failure('账号刷新凭据无效。')
    deadline = time.monotonic() + timeout
    account = directory(store.root / 'accounts' / ident)
    canonical = account / 'codex' / 'auth.json'
    owner, journal = account / 'refresh-owner', account / 'refresh-transaction.json'
    try:
        _legacy_idle(store, ident)
        with ExitStack() as stack:
            account_lease = stack.enter_context(_lock(store.root / 'locks' / f'account-{ident}.lock', deadline, shared=True))
            lock = stack.enter_context(_lock(store.root / 'locks' / f'auth-refresh-{ident}.lock', deadline))
            # v1 wrappers used this global lease; v2 uses independent slot leases.
            legacy_lease = stack.enter_context(_lock(store.root / 'locks' / f'backend-{ident}.lock', deadline))
            if store.profile(ident)['kind'] != 'official':
                raise _failure('只有官方账号可以使用登录刷新。')
            _legacy_idle(store, ident)
            value, _, _ = _read(canonical)
            if previous_account_id is not None and value['tokens']['account_id'] != previous_account_id:
                raise _failure('实例账号与当前登录不一致，未切换账号。')
            _recover(canonical, owner, journal)
            value, raw, revision = _read(canonical)
            if (rejected_access_token is not None and value['tokens']['access_token'] != rejected_access_token
                    and _fresh(value)):
                return _external(value)
            if not force_refresh and rejected_access_token is None and _fresh(value):
                return _external(value)
            directory(owner)
            write_json(journal, {'version': 1, 'phase': 'prepared', 'source_revision': revision,
                                 'source_identity': _identity(value)})
            atomic_write(directory(owner / 'codex') / 'auth.json', raw.decode('utf-8'))
            atomic_write(owner / 'codex' / 'config.toml', OWNER_CONFIG)
            try:
                # A caller crash must not admit login or a v1 backend while the
                # native owner can still rotate the old canonical credential.
                _run_native(owner, journal, deadline, command,
                            (account_lease.fileno(), lock.fileno(), legacy_lease.fileno()))
            except (OSError, TimeoutError, UserError, subprocess.SubprocessError):
                pass  # The durable candidate, identity and process state decide recovery.
            _recover(canonical, owner, journal)
            refreshed, _, _ = _read(canonical)
            if refreshed['tokens']['access_token'] == value['tokens']['access_token'] or not _fresh(refreshed):
                raise _failure()
            # A timeout/error after native persistence is recoverable once reaped.
            return _external(refreshed)
    except (OSError, ValueError, RecursionError) as error:
        if isinstance(error, UserError):
            raise
        raise _failure() from None


def clear_recovery_after_login(store, ident, *, timeout=20):
    """Discard an obsolete attempt after explicit native login succeeded.

    The login caller must still hold store.edit_guard(ident), including its
    exclusive account lease. This helper never writes the new auth.json.
    Only that confirmed login authorizes removing an empty orphan whose creation
    preceded its journal. Unrecorded directories containing any data are kept.
    """
    if (not isinstance(ident, str) or not ID.fullmatch(ident)
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise _failure('账号刷新参数无效。')
    account = directory(store.root / 'accounts' / ident)
    owner, journal = account / 'refresh-owner', account / 'refresh-transaction.json'
    try:
        with _lock(store.root / 'locks' / f'auth-refresh-{ident}.lock', time.monotonic() + timeout):
            _, _, revision = _read(account / 'codex' / 'auth.json')
            if not journal.exists():
                if journal.is_symlink() or owner.is_symlink():
                    raise _failure('登录已完成，但旧刷新目录无法核对；已保留文件。')
                if owner.exists():
                    # Both exclusive leases protect this successful-login
                    # cleanup. rmdir atomically refuses any nonempty orphan;
                    # no unrecorded token is adopted, removed, or retried.
                    owner.rmdir()
                return
            if journal.is_symlink():
                raise _failure('账号刷新恢复记录不安全。')
            state = json.loads(journal.read_text())
            if (not isinstance(state, dict) or state.get('version') != 1
                    or not isinstance(state.get('source_revision'), str)
                    or state.get('source_revision') == revision):
                raise _failure('尚未确认新的登录，已保留旧刷新恢复记录。')
            # The inherited refresh lease is now exclusive here. Even a crash
            # between spawn and recording the PID can no longer have a native
            # owner using this transaction. A new login supersedes its tokens.
            if (state.get('phase') not in ('prepared', 'starting', 'not_started', 'committed', 'running', 'finished')
                    or process_matches(state)):
                raise _failure('登录已完成，但旧刷新进程尚未确认退出；已保留恢复文件。')
            _cleanup(owner, journal)
    except (OSError, ValueError, RecursionError) as error:
        if isinstance(error, UserError):
            raise
        raise _failure('登录已完成，但旧刷新恢复记录无法清理；已保留文件。') from None
