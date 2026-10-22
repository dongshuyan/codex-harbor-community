"""Observe native official destinations with synthetic auth and a refusing proxy.

No real login, model service or original history is used. The macOS sandbox
denies all external networking. Only CONNECT hosts and boolean credential
presence are observed, never request bodies or token values.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from native_multi_instance_acceptance import NativeRPC
from test_official_auth import auth
import core
import storage
import launcher


def main():
    root = Path(tempfile.mkdtemp(prefix='hb-direct-route-', suffix='.noindex', dir='/private/tmp'))
    report = {'status': 'running', 'real_credentials_used': False}
    events, clients = [], []
    phase = 'setup'

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_CONNECT(self):
            events.append({'phase': phase, 'method': 'CONNECT', 'target': self.path})
            self.reply({'error': 'Synthetic proxy refuses all tunnels'}, 403)

        def do_GET(self):
            if self.path.startswith('/sentinel') or self.path.startswith('http'):
                events.append({'phase': phase, 'method': 'sentinel',
                               'has_authorization': bool(self.headers.get('Authorization'))})
                return self.reply({}, 403)
            if 'accounts/check' in self.path:
                return self.reply({'accounts': [{'id': 'synthetic-account',
                    'workspace_backend_origin': 'https://chatgpt.com',
                    'account_routing_override': 'NO_CONSTRAINT'}]})
            if 'models' in self.path:
                return self.reply({'models': core.catalog()})
            self.reply({})

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            events.append({'phase': phase, 'method': 'sentinel',
                           'has_authorization': bool(self.headers.get('Authorization'))})
            self.reply({}, 403)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    server.daemon_threads = True
    base = f'http://127.0.0.1:{server.server_port}'
    threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True).start()
    canonical = None
    try:
        with patch.object(storage, 'EXISTING_HOMES', {}):
            store = core.Store(root / 'control', root / 'external', mounted=lambda: True,
                               user_home=core.directory(root / 'synthetic-user'))
            store.initialize_storage('local')
            profile = store.save_profile({'kind': 'official', 'name': 'Synthetic official'})
            canonical = store.root / 'accounts' / profile['id'] / 'codex' / 'auth.json'
            core.write_json(canonical, auth('direct-route'))
            digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
            item = store.prepare(profile['id'], 'local', 'gpt-6-luna',
                                 launch_job='a' * 32, instance_slot='primary')
            core.configure(item)
            home = Path(item['home'])
            config = home / 'config.toml'
            config.write_text('''model="gpt-6-luna"
model_provider="custom"
cli_auth_credentials_store="ephemeral"
check_for_update_on_startup=false
[analytics]
enabled=false
[feedback]
enabled=false
[features]
apps=false
plugins=false
remote_plugin=false
shell_snapshot=false
shell_snapshot_v2=false
api_key_model_discovery=false
'''+f'\n[model_providers.custom]\nname="synthetic"\nbase_url={json.dumps(base+"/sentinel-provider")}\nrequires_openai_auth=true\n')
            original_config = config.read_bytes()
            seatbelt = ('(version 1)(allow default)(deny network*)'
                '(allow network-outbound (remote ip "localhost:*"))'
                '(deny file-write*)(allow file-write* (subpath ' + json.dumps(str(root)) + '))'
                '(allow file-write* (literal "/dev/null") (literal "/dev/tty"))')
            boundary = subprocess.run(['/usr/bin/sandbox-exec', '-p', seatbelt,
                sys.executable, '-I', '-B', '-c',
                'import socket\ntry: socket.create_connection(("192.0.2.1",9),timeout=1)\n'
                'except PermissionError: pass\nelse: raise SystemExit(1)'], capture_output=True)
            assert boundary.returncode == 0, 'External network denial not verified'
            pinned = {'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': item['history'],
                      'HARBOR_OFFICIAL_PROFILE': profile['id'],
                      'HARBOR_CONTROL_ROOT': str(store.root)}
            guard = [sys.executable, '-I', '-B', str(core.ENGINE / 'backend_guard.py'),
                     '-', '-', '-', str(core.CODEX)]
            script = root / 'native-backend'
            script.write_text(launcher.backend_script(item, pinned, guard))
            script.chmod(0o700)
            env = {'HOME': str(root), 'PATH': '/usr/bin:/bin', 'TMPDIR': str(root),
                   'OPENAI_BASE_URL': base + '/sentinel-environment',
                   'HTTPS_PROXY': base, 'https_proxy': base, 'HTTP_PROXY': base, 'http_proxy': base,
                   'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
            arguments = ['-c', 'openai_base_url=' + json.dumps(base + '/sentinel-root'),
                '-c', 'features.code_mode_host=true', 'app-server',
                '-c', 'chatgpt_base_url=' + json.dumps(base),
                '-c', 'openai_base_url=' + json.dumps(base + '/sentinel-trailing'),
                '-c', '"openai_base_url"=' + json.dumps(base + '/sentinel-trailing'),
                '-c', 'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=false']
            command = ['/usr/bin/sandbox-exec', '-p', seatbelt, str(script), *arguments]
            phase = 'initialize'
            rpc = NativeRPC(command, env, root, root / 'native.stderr')
            clients.append(rpc)
            rpc.call('initialize', {'clientInfo': {'name': 'codex_desktop', 'version': 'synthetic'},
                                   'capabilities': {'experimentalApi': True}})
            rpc.send('initialized', {})
            effective = rpc.call('config/read', {'includeLayers': False})['config']
            assert effective['model_provider'] == 'openai'
            assert effective['openai_base_url'] == 'https://chatgpt.com/backend-api/codex'
            assert effective['chatgpt_base_url'] == 'https://chatgpt.com/backend-api/'
            assert effective['cli_auth_credentials_store'] == 'ephemeral'
            assert effective.get('features', {}).get('enable_request_compression') is not False
            # Synthetic credentials cannot complete real official account
            # discovery through a refusing proxy. Verify that this production
            # path reaches only the official destination, never a stale Router.
            try:
                rpc.call('account/read', {'refreshToken': False})
            except RuntimeError:
                pass
            control_hosts = {e['target'] for e in events if e['method'] == 'CONNECT'}
            assert control_hosts == {'chatgpt.com:443'}, control_hosts
            assert not any(e['method'] == 'sentinel' for e in events)
            report['production_control_destinations'] = sorted(control_hosts)
            rpc.close(); clients.remove(rpc)
            assert hashlib.sha256(canonical.read_bytes()).hexdigest() == digest
            # A refused synthetic account discovery can request token refresh.
            # Its deliberately invalid refresh credential remains confined to
            # this first fake account. Use a fresh fake account for the next
            # independent protocol case, never reset its recovery journal.
            profile = store.save_profile({'kind': 'official', 'name': 'Synthetic protocol account'})
            canonical = store.root / 'accounts' / profile['id'] / 'codex' / 'auth.json'
            core.write_json(canonical, auth('direct-protocol'))
            digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
            home = core.directory(root / 'protocol-home')
            config = home / 'config.toml'
            config.write_bytes(original_config)
            pinned.update(CODEX_HOME=str(home), HARBOR_OFFICIAL_PROFILE=profile['id'])
            # Test-only metadata endpoint supplies a synthetic account. Its
            # routing origin remains the real official host. No production
            # hook/environment variable can set this module constant.
            bootstrap = ('import sys;sys.path.insert(0,sys.argv.pop(1));'
                'import native_cli;native_cli.OFFICIAL_CHATGPT_URL=sys.argv.pop(1);'
                'import backend_guard;backend_guard.main()')
            fixture_guard = [sys.executable, '-I', '-B', '-c', bootstrap, str(core.ENGINE), base,
                             '-', '-', '-', str(core.CODEX)]
            script.write_text(launcher.backend_script(item, pinned, fixture_guard))
            phase = 'synthetic-account'
            rpc = NativeRPC(command, env, root, root / 'native-fixture.stderr')
            clients.append(rpc)
            rpc.call('initialize', {'clientInfo': {'name': 'codex_desktop', 'version': 'synthetic'},
                                   'capabilities': {'experimentalApi': True}})
            rpc.send('initialized', {})
            assert rpc.call('account/read', {'refreshToken': False})['account'] is not None
            thread = rpc.call('thread/start', {'model': 'gpt-6-luna', 'cwd': str(root),
                'approvalPolicy': 'never', 'sandbox': 'read-only'})['thread']['id']
            phase = 'model-turn'
            requested = rpc.call('turn/start', {'threadId': thread, 'input': [
                {'type': 'text', 'text': 'Synthetic network route test. Do not use tools.'}], 'effort': 'low'})
            deadline = time.monotonic() + 20
            while not any(e['phase'] == phase and e['method'] == 'CONNECT' for e in events):
                rpc.read(deadline)
            turn_id = requested['turn']['id']
            try:
                rpc.call('turn/interrupt', {'threadId': thread, 'turnId': turn_id})
            except RuntimeError as error:
                if 'no active turn to interrupt' not in str(error):
                    raise  # A refused synthetic request may already have ended.
            destinations = {e['target'] for e in events if e['phase'] == 'model-turn' and e['method'] == 'CONNECT'}
            assert destinations == {'chatgpt.com:443'}, destinations
            assert not any(e['method'] == 'sentinel' for e in events), 'Credential destination drifted'
            report.update(native_model_destinations=sorted(destinations),
                          stale_router_sentinel_untouched=True, native_compression_preserved=True)
            phase = 'logout'
            rpc.call('account/logout', {})
            assert rpc.call('account/read', {'refreshToken': False})['account'] is None
            # A refresh request must not silently reauthenticate a detached window.
            assert rpc.call('account/read', {'refreshToken': True})['account'] is None
            report['native_logout_stays_detached'] = True
            rpc.close(); clients.remove(rpc)
            phase = 'reserved-provider'
            script.write_text(launcher.backend_script(item, pinned, guard))
            reserved = command + ['-c', 'model_providers.openai={name="synthetic",base_url=' +
                                  json.dumps(base + '/sentinel-reserved') + ',requires_openai_auth=true}']
            rejected = subprocess.run(reserved, env=env, cwd=root, input=b'', capture_output=True, timeout=15)
            assert rejected.returncode != 0
            assert b'reserved built-in provider IDs' in rejected.stderr
            assert not any(e['phase'] == phase for e in events), 'Reserved provider reached network'
            report['reserved_provider_fails_before_auth'] = True
            assert config.read_bytes() == original_config
            assert hashlib.sha256(canonical.read_bytes()).hexdigest() == digest
            report.update(status='passed', account_and_config_unchanged=True)
    except Exception as error:
        report.update(status='failed', phase=phase, error_type=type(error).__name__)
        # These messages contain only synthetic test material; retained locally.
        (root / 'failure.txt').write_text(str(error))
    finally:
        for rpc in clients:
            rpc.close()
        server.shutdown(); server.server_close()
        report['observed_events'] = events
        core.write_json(root / 'result.json', report)
        print(json.dumps({'status': report['status'], 'report': str(root / 'result.json')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
