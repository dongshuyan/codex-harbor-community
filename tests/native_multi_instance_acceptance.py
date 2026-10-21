"""Real native CLI acceptance with synthetic auth, homes and loopback metadata.

Run manually with Python 3.11+. No desktop or model turn is started. A macOS
outer sandbox blocks external networking and writes outside the fixture root;
command/exec uses externalSandbox because nested Seatbelt cannot initialize.
"""
import base64
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1] / 'engine'
sys.path.insert(0, str(ENGINE))
import core
import storage
from accounts import account_home, account_lock_paths
from instances import slot_paths
from login import RPC


def synthetic_jwt():
    claims = {'exp': int(time.time()) + 7200, 'email': 'synthetic@example.invalid',
              'https://api.openai.com/profile': {'email': 'synthetic@example.invalid'},
              'https://api.openai.com/auth': {'chatgpt_account_id': 'synthetic-account',
                  'chatgpt_user_id': 'synthetic-user', 'chatgpt_plan_type': 'pro'}}
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': 'none'}) + '.' + encode(claims) + '.SYNTHETIC_SIGNATURE'


class NativeRPC(RPC):
    def __init__(self, command, env, cwd, stderr):
        self.stderr = open(stderr, 'wb')
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, env=env, cwd=cwd, close_fds=True, start_new_session=True)
        self.buffer = b''
        self.pending = []
        self.sequence = 0

    def call(self, method, params, timeout=20):
        self.sequence += 1
        ident = 'synthetic-desktop-' + str(self.sequence)
        self.send(method, params, ident)
        deadline = time.monotonic() + timeout
        while True:
            message = self.read(deadline)
            if message.get('id') == ident:
                if 'error' in message:
                    raise RuntimeError(method + ': ' + str(message['error']))
                return message.get('result')
            if message.get('method') == 'account/chatgptAuthTokens/refresh':
                raise AssertionError('Auth bridge exposed its native refresh callback to the desktop')
            self.pending.append(message.get('method'))
            if len(self.pending) > 2000:
                raise AssertionError('Unexpected notification volume')

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.stderr.close()


def main():
    root = Path(tempfile.mkdtemp(prefix='harbor-native-multi-', dir='/private/tmp'))
    events, clients, prepared_instances = [], [], []
    result = {'status': 'running', 'root': str(root), 'real_credentials_loaded': False,
              'desktop_started': False, 'model_turns_started': 0}
    stage = 'setup'

    class Metadata(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            events.append({'method': 'GET', 'path': self.path.split('?')[0]})
            if 'accounts/check' in self.path:
                return self.reply({'accounts': [{'id': 'synthetic-account',
                    'workspace_backend_origin': base.replace('http://', 'https://', 1),
                    'account_routing_override': 'NO_CONSTRAINT'}]})
            if 'models' in self.path:
                return self.reply({'models': []})
            self.reply({})

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            events.append({'method': 'POST', 'path': self.path.split('?')[0]})
            self.reply({'error': {'message': 'No inference or refresh is allowed in this fixture'}}, 403)

        def reply(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Metadata)
    base = 'http://127.0.0.1:' + str(server.server_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch.object(storage, 'EXISTING_HOMES', {}):
            store = core.Store(root / 'control', root / 'external', mounted=lambda: True,
                               community=False, user_home=root / 'synthetic-user')
            for selected in ('local', 'external'):
                store.initialize_storage(selected)
            profile = store.save_profile({'kind': 'official', 'name': 'Synthetic official'})
            canonical = account_home(store, profile['id']) / 'auth.json'
            token = synthetic_jwt()
            core.write_json(canonical, {'auth_mode': 'chatgpt', 'tokens': {
                'access_token': token, 'id_token': token,
                'refresh_token': 'SYNTHETIC_UNUSED_REFRESH_TOKEN', 'account_id': 'synthetic-account'},
                'last_refresh': datetime.datetime.now(datetime.timezone.utc).isoformat()})
            original_auth = hashlib.sha256(canonical.read_bytes()).hexdigest()
            # This protection is independent of the native model/provider config.
            seatbelt = ('(version 1)(allow default)(deny network*)'
                '(allow network-outbound (remote ip "localhost:*"))'
                # The production compatibility bridge owns a loopback listener.
                # External destinations remain denied. The bridge itself binds
                # 127.0.0.1 explicitly (macOS local-bind filters include wildcard).
                '(allow network-bind (local ip "localhost:*"))'
                '(allow network-inbound (local ip "localhost:*"))'
                '(deny file-write*)(allow file-write* (subpath ' + json.dumps(str(root)) + '))'
                '(allow file-write* (literal "/dev/null") (literal "/dev/tty"))')
            probe = subprocess.run(['/usr/bin/sandbox-exec', '-p', seatbelt, sys.executable, '-I', '-B', '-c',
                'import socket\n'
                'try: socket.create_connection(("192.0.2.1", 9), timeout=1)\n'
                'except PermissionError: pass\n'
                'else: raise SystemExit("Sandbox network boundary failed")\n'], capture_output=True)
            assert probe.returncode == 0, 'Sandbox must still reject external requests'
            for index, selected in enumerate(('local', 'local', 'external'), 1):
                stage = 'prepare-' + str(index)
                job = str(index) * 32
                slot_name = 'primary' if index == 1 else job
                prepared = store.prepare(profile['id'], selected, launch_job=job,
                                         instance_slot=slot_name)
                core.configure(prepared)
                prepared_instances.append(prepared)
                home = Path(prepared['home'])
                config = tomllib.loads((home / 'config.toml').read_text())
                assert config['cli_auth_credentials_store'] == 'ephemeral'
                assert config['sqlite_home'] == prepared['history']
                if home != canonical.parent:
                    assert not (home / 'auth.json').exists()
                temporary = core.directory(root / ('ipc-' + str(index)))
                alias = temporary / 'h'
                alias.symlink_to(home, target_is_directory=True)
                prepared.update(temporary=str(temporary), short_home=str(alias))
                workspace = core.directory(home / 'synthetic-workspace')
                slot = slot_paths(store, profile['id'], selected, slot_name)
                marker = slot['control'] / 'generation.json'
                core.write_json(marker, {'job': job})
                env = core.environment(prepared)
                env.update(CODEX_HOME=str(home), HARBOR_DESKTOP_CODEX_HOME_ALIAS=str(alias),
                    HARBOR_OFFICIAL_PROFILE=profile['id'], HARBOR_CONTROL_ROOT=str(store.root),
                    CODEX_REFRESH_TOKEN_URL_OVERRIDE=base + '/refresh')
                env['HARBOR_BACKEND_SHARED_LEASES'] = json.dumps([
                    str(account_lock_paths(store, profile['id'])[0]),
                    str(store.instance_backend_lock(selected, profile['id'], v2=True))])
                options = {
                    'chatgpt_base_url': base, 'openai_base_url': base + '/v1',
                    'analytics.enabled': False, 'feedback.enabled': False,
                    'features.apps': False, 'features.plugins': False,
                    'features.remote_plugin': False, 'features.runtime_metrics': False,
                    'features.api_key_model_discovery': False,
                    'features.shell_snapshot': False, 'features.shell_snapshot_v2': False,
                    'check_for_update_on_startup': False,
                }
                native_args = [part for key, value in options.items()
                               for part in ('-c', key + '=' + json.dumps(value))] + ['app-server']
                command = ['/usr/bin/sandbox-exec', '-p', seatbelt, sys.executable, '-I', '-B',
                    str(ENGINE / 'backend_guard.py'), str(slot['control'] / 'backend.lock'),
                    str(marker), job, str(core.CODEX), *native_args]
                rpc = NativeRPC(command, env, workspace, root / ('native-' + str(index) + '.stderr'))
                clients.append(rpc)
                stage = 'initialize-' + str(index)
                initialized = rpc.call('initialize', {'clientInfo': {
                    'name': 'codex_desktop', 'version': 'synthetic-0.9'},
                    'capabilities': {'experimentalApi': True}})
                assert isinstance(initialized, dict)
                rpc.send('initialized', {})
                stage = 'account-read-' + str(index)
                account = rpc.call('account/read', {'refreshToken': False})
                assert account['account']['email'] == 'synthetic@example.invalid', account
                assert account['account']['type'] == 'chatgpt', account
                if home != canonical.parent:
                    assert not (home / 'auth.json').exists()
                result.setdefault('initialized_instances', []).append(index)

            first, second, third = prepared_instances
            assert len({value['home'] for value in prepared_instances}) == 3
            assert first['history'] == second['history'] != third['history']
            history_links = {value['home']: {name: str((Path(value['home']) / name).resolve())
                for name in ('sessions', 'archived_sessions', 'thread-writer-locks')}
                for value in prepared_instances}
            for value in prepared_instances:
                for name, actual in history_links[value['home']].items():
                    assert actual == str(Path(value['history']) / name)
            result['private_homes_and_history_bindings'] = True
            result['canonical_fresh_token_did_not_start_refresh_owner'] = not (
                canonical.parent.parent / 'refresh-owner').exists()
            assert result['canonical_fresh_token_did_not_start_refresh_owner']

            stage = 'native-thread-permission-roots'
            home = Path(second['home'])
            alias_workspace = second['short_home'] + '/synthetic-workspace'
            started = clients[1].call('thread/start', {
                'cwd': alias_workspace, 'runtimeWorkspaceRoots': [alias_workspace],
                'environments': [{'environmentId': 'local', 'cwd': alias_workspace,
                                  'runtimeWorkspaceRoots': [alias_workspace]}],
                'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'ephemeral': False,
            })
            assert started['cwd'] == str(home / 'synthetic-workspace'), started['cwd']
            assert started['sandbox']['type'] == 'workspaceWrite', started['sandbox']
            rollout = Path(started['thread']['path']).resolve()
            assert rollout.is_relative_to(Path(second['history']) / 'sessions'), rollout
            for rpc, prepared in zip(clients, prepared_instances):
                requirements = rpc.call('configRequirements/read', {})
                assert isinstance(requirements, dict) and 'requirements' in requirements, requirements
                native_config = rpc.call('config/read', {'includeLayers': False})
                assert native_config['config']['sqlite_home'] == prepared['history']
            result['native_thread_cwd_and_history_location'] = True

            stage = 'native-command-write'
            execution = clients[1].call('command/exec', {
                'command': ['/bin/sh', '-c', "set -eu; printf 'SYNTHETIC_NATIVE_WRITE\\n' > native-proof.txt; pwd -P"],
                'cwd': alias_workspace, 'timeoutMs': 10000,
                'sandboxPolicy': {'type': 'externalSandbox', 'networkAccess': 'restricted'},
            })
            assert execution['exitCode'] == 0, execution
            assert (home / 'synthetic-workspace/native-proof.txt').read_text() == 'SYNTHETIC_NATIVE_WRITE\n'
            assert execution['stdout'].strip() == str(home / 'synthetic-workspace'), execution
            result['native_command_write_and_resolved_cwd'] = True
            result['production_guardian_and_native_policy_read'] = True

            stage = 'logout-first'
            clients[0].call('account/logout', {})
            assert clients[0].call('account/read', {'refreshToken': False})['account'] is None
            for rpc in clients[1:]:
                assert rpc.call('account/read', {'refreshToken': False})['account']['email'] == 'synthetic@example.invalid'
            result['logout_is_local'] = True
            clients[0].close()
            stage = 'close-first-keeps-siblings'
            for rpc in clients[1:]:
                assert rpc.call('account/read', {'refreshToken': False})['account']['email'] == 'synthetic@example.invalid'
            result['closing_first_keeps_sibling_login'] = True
            for value in prepared_instances:
                if Path(value['home']) != canonical.parent:
                    assert not (Path(value['home']) / 'auth.json').exists()
                for name, expected in history_links[value['home']].items():
                    assert str((Path(value['home']) / name).resolve()) == expected
            assert hashlib.sha256(canonical.read_bytes()).hexdigest() == original_auth
            assert not any('responses' in item['path'] or 'refresh' in item['path'] for item in events), events
            result.update(status='passed', canonical_auth_unchanged=True,
                          additional_instance_auth_files_absent=True, histories_unchanged=True,
                          primary_logout_preserves_canonical_auth=True)
    except Exception as error:
        result.update(status='failed', failure_stage=stage, failure_type=type(error).__name__,
                      failure_message=str(error))
    finally:
        for rpc in clients:
            rpc.close()
        server.shutdown()
        server.server_close()
        result['loopback_metadata_requests'] = events
        core.write_json(root / 'result.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
