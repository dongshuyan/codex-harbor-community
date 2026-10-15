"""Community-only native Keychain transport. Never persists or logs secrets."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time


MAX_SECRET_BYTES = 16 * 1024
# Quotes and backslashes can double in JSON; retain room for the envelope.
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
REFERENCE = re.compile(r'chc_[0-9a-f]{32}_[0-9a-f]{32}\Z')
ERRORS = {
    'not_found': '钥匙串中找不到此 API Key，请重新填写并保存。',
    'locked': '钥匙串已锁定或需要授权；请先在 macOS 中解锁或授权后重试。',
    'access_denied': 'macOS 拒绝访问钥匙串。请重新授权；仍无法读取时，请重新填写 API Key 并保存。',
    'unavailable': 'macOS 钥匙串暂时不可用，请稍后重试。',
    'invalid_request': '钥匙串请求无效，操作已停止。',
    'failure': '钥匙串操作失败，密钥未写入任何备用文件。',
}


def _error(message):
    # core may import this adapter while defining Store.
    from core import UserError
    return UserError(message)


def _reference(value):
    if not isinstance(value, str) or len(value) != 69 or not REFERENCE.fullmatch(value):
        raise _error('Community 密钥引用无效。')
    return value


def _secret(value):
    try:
        valid = (isinstance(value, str) and 0 < len(value) <= MAX_SECRET_BYTES
                 and len(value.encode('utf-8')) <= MAX_SECRET_BYTES
                 and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise _error('API Key 必须是有效文本，不能包含控制字符，且最多为 16384 字节。')
    return value


def _no_symlinks(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            # macOS system aliases are root-owned, conventional entry points.
            if str(component) not in ('/var', '/tmp') or component.resolve() != Path('/private') / component.name:
                raise _error('钥匙串助手或测试路径不能经过符号链接。')


def _test_path(path):
    path = Path(path).absolute()
    _no_symlinks(path)
    canonical = path.resolve()
    parent = canonical.parent
    allowed_roots = {Path(tempfile.gettempdir()).resolve(), Path('/private/tmp')}
    info = parent.stat()
    if (canonical.name != 'test.keychain-db' or parent.parent not in allowed_roots
            or not parent.name.startswith('codex-harbor-keychain-test-')
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise _error('测试钥匙串必须位于专用的私有临时目录。')
    if canonical.exists():
        info = canonical.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise _error('测试钥匙串文件不安全。')
    return canonical


class KeychainStore:
    """Store API keys under opaque refs; missing reads fail, deletes are idempotent.

    Package HarborKeychain next to the engine directory in Contents/Resources.
    Explicit helper/test_keychain arguments are for isolated acceptance tests.
    """
    def __init__(self, helper_path=None, *, timeout=10.0, test_keychain=None, allow_interaction=False):
        self.helper_path = (Path(helper_path).absolute() if helper_path is not None else
                            Path(__file__).absolute().parent.parent / 'HarborKeychain')
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise _error('钥匙串等待时间无效。')
        self.timeout = float(timeout)
        if not isinstance(allow_interaction, bool) or (test_keychain is not None and allow_interaction):
            raise _error('钥匙串授权选项无效；隔离测试不允许交互授权。')
        self.allow_interaction = allow_interaction
        if test_keychain is not None and helper_path is None:
            raise _error('测试钥匙串需要显式指定测试助手。')
        self.test_keychain = test_keychain

    def get(self, ref):
        return self._request('get', ref)['secret']

    def put(self, ref, secret):
        self._request('put', ref, _secret(secret))

    def delete(self, ref):
        self._request('delete', ref)

    def _request(self, operation, ref, secret=None):
        request = {'op':operation, 'ref':_reference(ref)}
        if self.allow_interaction:
            request['allow_interaction'] = True
        if secret is not None:
            request['secret'] = secret
        argv = [str(self.helper_path)]
        try:
            _no_symlinks(self.helper_path)
            info = self.helper_path.stat()
            parent = self.helper_path.parent.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or stat.S_IMODE(info.st_mode) & 0o022 or not os.access(self.helper_path, os.X_OK)
                    or parent.st_uid not in (0, os.getuid()) or stat.S_IMODE(parent.st_mode) & 0o022):
                raise _error('钥匙串助手权限不安全，操作已停止。')
            if self.test_keychain is not None:
                request['test_keychain'] = str(_test_path(self.test_keychain))
                argv.append('--test-keychain')
        except (OSError, RuntimeError, ValueError) as exc:
            from core import UserError
            if isinstance(exc, UserError):
                raise
            raise _error('找不到可用的钥匙串助手，请重新安装完整的 Community 应用。') from None
        payload = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(payload) > MAX_REQUEST_BYTES:
            raise _error('钥匙串请求过长，操作已停止。')
        raw = self._exchange(argv, payload)
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise _error('钥匙串助手返回无效响应，操作已停止。') from None
        if not isinstance(response, dict):
            raise _error('钥匙串助手返回无效响应，操作已停止。')
        if response.get('ok') is False and set(response) == {'ok', 'error'}:
            code = response['error']
            raise _error(ERRORS.get(code, ERRORS['failure']) if isinstance(code, str) else ERRORS['failure'])
        expected = {'ok', 'secret'} if operation == 'get' else {'ok'}
        if response.get('ok') is not True or set(response) != expected:
            raise _error('钥匙串助手返回无效响应，操作已停止。')
        if operation == 'get':
            _secret(response['secret'])
        return response

    def _exchange(self, argv, payload):
        """Bound input, both output streams, and time; never surface child text."""
        process = None
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, close_fds=True,
                                       env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin', 'LANG':'en_US.UTF-8'})
            output = bytearray()
            stderr_size = 0
            offset = 0
            deadline = time.monotonic() + self.timeout
            with selectors.DefaultSelector() as selector:
                for stream, event, label in ((process.stdin, selectors.EVENT_WRITE, 'stdin'),
                                              (process.stdout, selectors.EVENT_READ, 'stdout'),
                                              (process.stderr, selectors.EVENT_READ, 'stderr')):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, event, label)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _error('钥匙串操作超时，请检查钥匙串状态后重试。')
                    for selected, _ in selector.select(remaining):
                        stream = selected.fileobj
                        if selected.data == 'stdin':
                            try:
                                offset += os.write(stream.fileno(), payload[offset:offset + 4096])
                            except BrokenPipeError:
                                offset = len(payload)
                            if offset == len(payload):
                                selector.unregister(stream)
                                stream.close()
                            continue
                        chunk = os.read(stream.fileno(), 4096)
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                        elif selected.data == 'stdout':
                            if len(output) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise _error('钥匙串助手响应超出大小限制，操作已停止。')
                            output.extend(chunk)
                        else:
                            stderr_size += len(chunk)
                            if stderr_size > MAX_RESPONSE_BYTES:
                                raise _error('钥匙串助手响应超出大小限制，操作已停止。')
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if process.returncode != 0 or stderr_size:
                raise _error('钥匙串助手执行失败，密钥未写入任何备用文件。')
            return bytes(output)
        except subprocess.TimeoutExpired:
            raise _error('钥匙串操作超时，请检查钥匙串状态后重试。') from None
        except OSError:
            raise _error('无法运行钥匙串助手，密钥未写入任何备用文件。') from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
