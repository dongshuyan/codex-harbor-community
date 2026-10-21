"""Hold a backend lease until the actual native process finishes cleanup."""
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading

# -I excludes the script directory. Match the other bundled entrypoints: only
# this trusted engine directory is added, never the caller's cwd or PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_payload
if __name__=='__main__':runtime_payload.activate(Path(__file__).resolve().parent)
from native_cli import VALUE_OPTIONS, app_server_index


PATH_METHODS = {'thread/start', 'thread/resume', 'thread/fork', 'turn/start',
                'thread/settings/update', 'command/exec'}
PLUGIN_PATH_METHODS = {'plugin/read', 'plugin/install'}
MAX_RPC_LINE = 64 * 1024 * 1024


def ephemeral_arguments(args):
    """Pin only this auth option, without rewriting prompts or other arguments."""
    result=list(args)
    for index,value in enumerate(result):
        if value=='--':break
        if value in ('-c','--config') and index+1<len(result):
            setting=result[index+1]
            if setting.split('=',1)[0].strip()=='cli_auth_credentials_store':
                result[index+1]='cli_auth_credentials_store="ephemeral"'
        elif value.startswith('--config='):
            setting=value[len('--config='):]
            if setting.split('=',1)[0].strip()=='cli_auth_credentials_store':
                result[index]='--config=cli_auth_credentials_store="ephemeral"'
        elif value.startswith('-c') and len(value)>2:
            setting=value[2:].removeprefix('=')
            if setting.split('=',1)[0].strip()=='cli_auth_credentials_store':
                result[index]='-ccli_auth_credentials_store="ephemeral"'
    return ['-c','cli_auth_credentials_store="ephemeral"',*result]


def is_stdio_app_server(args):
    """Recognize only the supported stdio command shapes; leave other CLI uses alone."""
    i = app_server_index(args)
    if i is None:
        return False
    i += 1
    while i < len(args):
        value = args[i]
        if value in VALUE_OPTIONS:
            if i+1 >= len(args): return False
            i += 2
        elif value.startswith('-c') and len(value)>2 or any(value.startswith(option+'=') for option in VALUE_OPTIONS if option.startswith('--')):
            i += 1
        elif value in {'--analytics-default-enabled', '--stdio'}:
            i += 1
        elif value == '--listen' and i+1 < len(args) and args[i+1] == 'stdio://':
            i += 2
        elif value == '--listen=stdio://':
            i += 1
        else:
            return False
    return True


def rewrite_permission_paths(raw, alias, home):
    """Normalize typed desktop permission/plugin paths for this instance only.

    Do not realpath suffixes: child symlinks must still reach Codex's native
    validation. Old aliases and non-local environment paths stay untouched.
    Unchanged messages retain their original bytes.
    """
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return raw
    if not isinstance(value, dict) or not isinstance(value.get('method'), str) or value['method'] not in PATH_METHODS | PLUGIN_PATH_METHODS:
        return raw
    params = value.get('params')
    if not isinstance(params, dict): return raw
    changed = False
    def path(item):
        nonlocal changed
        if not isinstance(item, str) or alias == '/' or not alias:
            return item
        if item != alias and not item.startswith(alias+'/'):
            return item
        suffix = item[len(alias):]
        if any(part in {'.', '..'} for part in suffix.split('/')):
            return item
        changed = True
        return home+suffix
    if value['method'] in PLUGIN_PATH_METHODS:
        # Native marketplace/add already canonicalizes and validates its source.
        # read/install instead compare the supplied manifest path with CODEX_HOME.
        # Send the same identity there; leave validation and registration native.
        item = params.get('marketplacePath')
        if params.get('remoteMarketplaceName') is None and isinstance(item, str) and '\x00' not in item and '//' not in item:
            params['marketplacePath'] = path(item)
        return json.dumps(value, separators=(',', ':')).encode()+b'\n' if changed else raw
    if 'cwd' in params: params['cwd'] = path(params['cwd'])
    if isinstance(params.get('runtimeWorkspaceRoots'), list):
        params['runtimeWorkspaceRoots'] = [path(item) for item in params['runtimeWorkspaceRoots']]
    policy = params.get('sandboxPolicy')
    if isinstance(policy, dict) and isinstance(policy.get('writableRoots'), list):
        policy['writableRoots'] = [path(item) for item in policy['writableRoots']]
    # Current desktop turns carry the built-in local executor's roots here,
    # even when the top-level runtimeWorkspaceRoots field is absent. Remote
    # executors use their own path namespace, so never rewrite those entries.
    if isinstance(params.get('environments'), list):
        for environment in params['environments']:
            if not isinstance(environment, dict) or environment.get('environmentId') != 'local':
                continue
            if 'cwd' in environment: environment['cwd'] = path(environment['cwd'])
            if isinstance(environment.get('runtimeWorkspaceRoots'), list):
                environment['runtimeWorkspaceRoots'] = [path(item) for item in environment['runtimeWorkspaceRoots']]
    if not changed: return raw
    return json.dumps(value, separators=(',', ':')).encode()+b'\n'


def validate_home_alias(alias, home):
    link, target = Path(alias), Path(home)
    try:
        parent = link.parent.stat()
        valid = (link.is_absolute() and target.is_absolute() and link.is_symlink()
                 and link.lstat().st_uid == os.getuid()
                 and parent.st_uid == os.getuid() and not stat.S_IMODE(parent.st_mode) & 0o077
                 and link.parent.resolve(strict=True) == link.parent
                 and target.resolve(strict=True) == target and target.is_dir()
                 and link.resolve(strict=True) == target)
    except (OSError, RuntimeError):
        valid = False
    if not valid:
        raise SystemExit('Harbor: private desktop home alias does not match this instance.')


def main():
    lease, marker, generation, binary, *args=sys.argv[1:]
    handle=None
    if lease!='-':
        fd=os.open(lease,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        handle=os.fdopen(fd,'a')
        try: fcntl.flock(handle,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('Harbor: account storage is being switched.')
        if Path(marker).is_symlink() or json.loads(Path(marker).read_text()).get('job')!=generation:
            raise SystemExit('Harbor: expired instance, reopen from the launcher.')
    extra=[]
    for path in json.loads(os.environ.get('HARBOR_BACKEND_SHARED_LEASES','[]')):
        target=Path(path)
        if not target.is_absolute() or target.resolve()!=target:
            raise SystemExit('Harbor: invalid private backend lease.')
        fd=os.open(target,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        guard=os.fdopen(fd,'a');extra.append(guard)
        try:fcntl.flock(guard,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Harbor: account or storage is being edited.')
    # Keep the guardian alive instead of depending on Electron/Rust fd inheritance.
    alias=os.environ.get('HARBOR_DESKTOP_CODEX_HOME_ALIAS')
    adapted=bool(alias and is_stdio_app_server(args))
    home=os.environ.get('CODEX_HOME','')
    if adapted:validate_home_alias(alias,home)
    official=os.environ.get('HARBOR_OFFICIAL_PROFILE')
    bridge=bool(official and is_stdio_app_server(args))
    transport=None
    child_env=None
    if is_stdio_app_server(args):
        from official_responses import loopback_environment
        child_env=loopback_environment(os.environ)
    credentials=None
    if official:
        from core import ID, Store
        if not ID.fullmatch(official):raise SystemExit('Harbor: invalid account identifier.')
        # Ignore the canonical refresh-token file even when using the primary
        # home. Every native child, including non-app-server helpers, is ephemeral.
        args=ephemeral_arguments(args)
        if bridge:
            from auth_bridge import run
            from official_auth import credentials_for_instance
            from official_responses import OfficialResponsesBridge,native_arguments
            store=Store(Path(os.environ['HARBOR_CONTROL_ROOT']))
            transport=OfficialResponsesBridge()
            def credentials(**kwargs):
                payload=credentials_for_instance(store,official,**kwargs)
                transport.set_credentials(payload['accessToken'],payload['chatgptAccountId'])
                return payload
            args=native_arguments(args,transport.url)
            transport.start()
    try:
        proc=subprocess.Popen([binary,*args],close_fds=True,stdin=subprocess.PIPE if adapted or bridge else None,
                              stdout=subprocess.PIPE if bridge else None,env=child_env,
                              pass_fds=tuple(item.fileno() for item in ([handle] if handle else [])+extra))
    except BaseException:
        if transport:transport.close()
        raise
    def forward(sig,_frame):
        if proc.poll() is None: proc.send_signal(sig)
    signal.signal(signal.SIGTERM,forward);signal.signal(signal.SIGINT,forward)
    failed=threading.Event()
    if bridge:
        try:
            rewrite=(lambda raw:rewrite_permission_paths(raw,alias,home)) if adapted else (lambda raw:raw)
            if not run(proc,credentials,rewrite=rewrite,on_logout=transport.revoke):failed.set()
        except (OSError,ValueError,TimeoutError):
            failed.set()
            print('Harbor: official account connection closed; check login in Harbor.',file=sys.stderr)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=3)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
            if transport:transport.close()
    elif adapted:
        def pump():
            try:
                # An idle desktop may keep stdin open after native exit. Do not
                # hold Python's stdin buffer lock in this daemon at shutdown.
                pending=bytearray()
                def send(raw):
                    data=memoryview(rewrite_permission_paths(raw,alias,home))
                    while data:
                        written=os.write(proc.stdin.fileno(),data)
                        if written<=0:raise OSError('Backend input pipe closed')
                        data=data[written:]
                while chunk := os.read(sys.stdin.fileno(),65536):
                    pending.extend(chunk)
                    while (end := pending.find(b'\n')) >= 0:
                        if end+1>MAX_RPC_LINE:raise ValueError('RPC line limit exceeded')
                        send(bytes(pending[:end+1]));del pending[:end+1]
                    if len(pending)>MAX_RPC_LINE:raise ValueError('RPC line limit exceeded')
                if pending:send(bytes(pending))
            except BrokenPipeError:
                pass
            except (OSError, ValueError):
                failed.set()
                print('Harbor: local backend input could not be forwarded.',file=sys.stderr)
                if proc.poll() is None:proc.terminate()
            finally:
                try:proc.stdin.close()
                except (OSError, BrokenPipeError):pass
        threading.Thread(target=pump,daemon=True).start()
    code=proc.wait()
    if handle: handle.close()
    for item in extra:item.close()
    if failed.is_set():raise SystemExit(1)
    raise SystemExit(code if code>=0 else 128-code)


if __name__=='__main__': main()
