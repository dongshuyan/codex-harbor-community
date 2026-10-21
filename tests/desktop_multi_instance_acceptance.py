"""Short real-desktop smoke with fresh synthetic official auth and private homes.

No turn, UI automation, real account, or original history is used. Backend URLs
are loopback; a rejecting proxy contains incidental desktop HTTP requests. Only
the two exact launcher jobs are stopped. Shared macOS UI prefs use the launcher's
normal read-only backup; this harness never restores or edits that domain.
"""
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

from native_multi_instance_acceptance import synthetic_jwt

ENGINE = Path(__file__).resolve().parents[1] / 'engine'
sys.path.insert(0, str(ENGINE))
import core
import configuration
import launcher
import storage
from accounts import account_home


def assert_no_model_turns(lines):
    if not lines:
        raise AssertionError('Native desktop log is missing or empty')
    if any('method=turn/start' in line for line in lines):
        raise AssertionError('Unexpected model turn in native desktop log')


def main(*, native_features=False):
    # Positive/negative controls exercise the same external observer used below.
    assert_no_model_turns(['response_routed method=config/read errorCode=null'])
    for control in ([], ['request method=turn/start id=synthetic-unexpected']):
        try:
            assert_no_model_turns(control)
        except AssertionError:
            pass
        else:
            raise AssertionError('Native log observer failed its control')
    root = Path(tempfile.mkdtemp(prefix='harbor-desktop-multi-', dir='/private/tmp'))
    metadata_requests, blocked_requests, prepared_by_job = [], [], {}
    workers, jobs, snapshots = [], [], []
    result = {'status': 'running', 'root': str(root), 'real_credentials_loaded': False,
              'model_turns_sent': 0, 'ui_automation_used': False, 'observation_seconds': 14,
              'simultaneous_start': True}
    result['native_features'] = native_features
    stage = 'setup'
    store = None
    helper=root/'HarborLaunch'
    subprocess.run(['/usr/bin/swiftc','-swift-version','5',
        str(ENGINE.parent/'native/Sources/HarborLaunch/main.swift'),'-o',str(helper)],check=True)
    library=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    responsible=library.responsibility_get_pid_responsible_for_pid
    responsible.argtypes=[ctypes.c_int];responsible.restype=ctypes.c_int

    class Metadata(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = self.path.split('?')[0]
            metadata_requests.append({'method': 'GET', 'path': path})
            if 'accounts/check' in path:
                return self.reply({'accounts': [{'id': 'synthetic-account',
                    'workspace_backend_origin': base.replace('http://', 'https://', 1),
                    'account_routing_override': 'NO_CONSTRAINT'}]})
            if 'models' in path:
                return self.reply({'models': core.catalog(), 'data': []})
            if 'usage' in path or 'wham' in path:
                return self.reply({'plan_type': 'pro', 'rate_limit': {'allowed': True, 'limit_reached': False}})
            if 'settings/user' in path:
                return self.reply({'commit_attribution_enabled': False})
            self.reply({})

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            metadata_requests.append({'method': 'POST', 'path': self.path.split('?')[0]})
            self.reply({'error': {'message': 'Synthetic smoke does not allow inference or refresh'}}, 403)

        def reply(self, value, code=200):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class RejectProxy(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reject(self):
            blocked_requests.append({'method': self.command, 'target': self.path.split('?')[0]})
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.end_headers()

        do_CONNECT = do_GET = do_POST = do_PUT = do_DELETE = reject

    api = ThreadingHTTPServer(('127.0.0.1', 0), Metadata)
    proxy = ThreadingHTTPServer(('127.0.0.1', 0), RejectProxy)
    base = 'http://127.0.0.1:' + str(api.server_port)
    proxy_url = 'http://127.0.0.1:' + str(proxy.server_port)
    for server in (api, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch.object(storage, 'EXISTING_HOMES', {}):
            store = core.Store(root / 'control', root / 'external', mounted=lambda: True,
                               community=False)
            store.initialize_storage('local')
            profile = store.save_profile({'kind': 'official', 'name': 'Synthetic desktop smoke'})
            canonical = account_home(store, profile['id']) / 'auth.json'
            token = synthetic_jwt()
            core.write_json(canonical, {'auth_mode': 'chatgpt', 'tokens': {
                'access_token': token, 'id_token': token,
                'refresh_token': 'SYNTHETIC_UNUSED_REFRESH_TOKEN', 'account_id': 'synthetic-account'},
                'last_refresh': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            original_auth = hashlib.sha256(canonical.read_bytes()).hexdigest()
            def fixture(prepared, env, args, job):
                assert prepared['native_user_home'] is None
                assert Path(env['HOME']).is_relative_to(root)
                assert Path(env['CFFIXED_USER_HOME']).is_relative_to(root)
                assert Path(prepared['home']).is_relative_to(root)
                assert Path(prepared['history']).is_relative_to(root)
                assert Path(prepared['electron']).is_relative_to(root)
                home = Path(prepared['home'])
                path = home / 'config.toml'
                value = {
                    'chatgpt_base_url': base, 'openai_base_url': base + '/v1',
                    'cli_auth_credentials_store': 'ephemeral',
                    'mcp_oauth_credentials_store': 'file', 'check_for_update_on_startup': False,
                    'analytics': {'enabled': False}, 'feedback': {'enabled': False},
                    'features': {'apps': False, 'plugins': False, 'remote_plugin': False,
                        'runtime_metrics': False, 'computer_use': False,
                        'api_key_model_discovery': False, 'shell_snapshot': False,
                        'shell_snapshot_v2': False},
                    'memories': {'generate_memories': False, 'use_memories': False},
                    'otel': {'exporter': 'none', 'trace_exporter': 'none', 'metrics_exporter': 'none'},
                }
                if native_features:
                    for feature in ('plugins', 'computer_use'):
                        value['features'].pop(feature)
                import tomllib
                configured = tomllib.loads(path.read_text())
                configured.update(value)
                core.atomic_write(path, configuration.patch_document(path.read_text(), configured))
                # The bundled CLI recognizes this override. Contain accidental
                # refresh attempts in the synthetic fixture as well as inference.
                env.update(CODEX_REFRESH_TOKEN_URL_OVERRIDE=base + '/refresh',
                           CODEX_SPARKLE_ENABLED='false',
                           HTTP_PROXY=proxy_url, HTTPS_PROXY=proxy_url, ALL_PROXY=proxy_url,
                           http_proxy=proxy_url, https_proxy=proxy_url, all_proxy=proxy_url,
                           NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
                args.extend(['--proxy-server=' + proxy_url,
                    '--proxy-bypass-list=127.0.0.1;localhost',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
                    '--disable-background-networking', '--disable-component-update'])
                if not native_features:
                    env['CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH'] = '1'
                prepared_by_job[job] = prepared

            with patch.object(launcher,'LAUNCH_HELPER',helper):
                start_barrier = threading.Barrier(3)

                def launch(job):
                    start_barrier.wait(timeout=5)
                    launcher.run_instance(store, job, profile['id'], 'local', None,
                        lab=lambda prepared, env, args: fixture(prepared, env, args, job))

                for _ in range(2):
                    job = uuid.uuid4().hex
                    jobs.append(job)
                    core.write_json(core.directory(store.root / 'jobs') / (job + '.json'),
                                    {'created_at': time.time(), 'access_mode': 'native'})
                    worker = threading.Thread(target=launch, args=(job,))
                    workers.append(worker)
                    worker.start()
                start_barrier.wait(timeout=5)
                stage = 'await-two-desktops'
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    snapshots = [json.loads((store.root / 'jobs' / (job + '.json')).read_text()) for job in jobs]
                    if all(state.get('status') == 'running' and core.process_matches(state) for state in snapshots):
                        break
                    if any(state.get('status') == 'error' for state in snapshots):
                        raise AssertionError([state.get('message') for state in snapshots])
                    time.sleep(.1)
                else:
                    raise TimeoutError('Two synthetic desktop processes did not become running')
                stage = 'observe-initialization'
                start = time.monotonic()
                while time.monotonic() - start < 14:
                    if not all(core.process_matches(state) for state in snapshots):
                        raise AssertionError('A synthetic desktop exited during the observation window')
                    time.sleep(.2)
                for field in ('home', 'electron', 'pid', 'instance_slot'):
                    assert len({state[field] for state in snapshots}) == 2, field
                for state in snapshots:
                    prepared = prepared_by_job[state['job']]
                    logroot = Path(prepared['os_home']) / 'Library/Logs/com.openai.codex'
                    lines = [line for path in logroot.rglob('*.log') for line in path.read_text().splitlines()]
                    assert_no_model_turns(lines)
                    assert not any('Desktop startup policy read failed' in line for line in lines)
                    # Check observed desktop startup and renderer reads. The CLI
                    # harness separately checks configRequirements/read; current
                    # desktop builds do not always log that bootstrap RPC.
                    assert any('Host startup critical-path phases completed' in line for line in lines)
                    assert any('Codex CLI initialized' in line for line in lines)
                    for method in ('account/read', 'config/read'):
                        assert any('response_routed' in line and 'errorCode=null' in line and
                                   ('method=' + method + ' ') in line for line in lines), method
                    if native_features:
                        assert not any('reserved and cannot be loaded from this source' in line for line in lines)
                        assert not any('bundled_plugins_marketplace_install_failed' in line for line in lines)
                        import tomllib
                        home = Path(prepared['home'])
                        manifest = home / '.tmp/bundled-marketplaces/openai-bundled/.agents/plugins/marketplace.json'
                        if manifest.exists():
                            config = tomllib.loads((home / 'config.toml').read_text())
                            assert config['marketplaces']['openai-bundled']['source'] == str(home / '.tmp/bundled-marketplaces/openai-bundled')
                            plugin_observation = 'native_marketplace_registered'
                        else:
                            # A synthetic identity cannot obtain account feature
                            # availability while remote HTTP is blocked. Record
                            # that limit instead of claiming browser acceptance.
                            assert any('bundled_plugins_reconcile_skipped_features_unavailable' in line for line in lines)
                            plugin_observation = 'not_tested_offline_feature_availability_missing'
                        helper_app = home / 'computer-use/Codex Computer Use.app'
                        assert helper_app.is_dir(), 'Native computer-use helper refresh did not run'
                        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(helper_app)], check=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                        result.setdefault('native_feature_checks', []).append({
                            'plugin_observation': plugin_observation, 'computer_use_helper_signature_valid': True,
                            'computer_use_permission_actions_tested': False,
                            'updater_disabled_in_synthetic_test_only': True})
                    assert responsible(state['pid']) == state['pid'], 'Desktop must own its file access'
                    # Observe child attribution externally; never replace the
                    # production entrypoint or import its modules in an observer.
                    process_rows = subprocess.check_output(['/bin/ps', '-ww', '-axo', 'pid=,ppid=,command='], text=True)
                    guardians = []
                    for row in process_rows.splitlines():
                        parts = row.strip().split(None, 2)
                        if len(parts) == 3 and 'backend_guard.py' in parts[2] and state['job'] in parts[2].split():
                            guardians.append(int(parts[0]))
                    assert guardians, 'No live production guardian observed'
                    for pid in guardians:
                        assert responsible(pid) == state['pid'], 'Backend must belong to its own desktop'
                assert hashlib.sha256(canonical.read_bytes()).hexdigest() == original_auth
                assert not any('responses' in item['path'] or 'refresh' in item['path'] for item in metadata_requests)
                result.update(status='passed', native_backends_initialized=True, production_entrypoint_used=True, desktop_startup_logs_observed=True,
                    guardians_responsible_to_desktop=True,
                    canonical_auth_unchanged=True, distinct_slots_homes_electron_pids=True,
                    shared_synthetic_history=len({state['history'] for state in snapshots}) == 1)
    except Exception as error:
        result.update(status='failed', failure_stage=stage, failure_type=type(error).__name__,
                      failure_message=str(error))
    finally:
        if store:
            result['stop_results'] = []
            for job in jobs:
                try:
                    result['stop_results'].append(launcher.stop_instance(store, job))
                except (OSError, ValueError) as error:
                    result['stop_results'].append({'error': type(error).__name__})
        for worker in workers:
            worker.join(timeout=12)
        if any(worker.is_alive() for worker in workers):
            result.update(status='failed', cleanup_incomplete=True)
        elif all(not core.process_matches(state) for state in snapshots):
            # Retain JSON evidence, not copied application bundles in /tmp.
            for prepared in prepared_by_job.values():
                helper_app = Path(prepared['home']) / 'computer-use/Codex Computer Use.app'
                if helper_app.is_relative_to(root):
                    if helper_app.is_symlink():
                        helper_app.unlink()
                    elif helper_app.is_dir():
                        shutil.rmtree(helper_app)
        result['instances'] = [{key: state.get(key) for key in
            ('job', 'pid', 'pid_start', 'home', 'history', 'electron', 'instance_slot')} for state in snapshots]
        result['metadata_requests'] = metadata_requests
        result['blocked_desktop_requests'] = blocked_requests
        for server in (api, proxy):
            server.shutdown()
            server.server_close()
        core.write_json(root / 'result.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main(native_features='--native-features' in sys.argv))
