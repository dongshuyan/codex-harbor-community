"""Native desktop cache reconciliation with only synthetic identities/history.

Starts and stops only this test's NSWorkspace instances. No UI automation,
real login, original history or installed Harbor configuration is used.
The model endpoint is a loopback fixture. Keep results, not disposable apps.
"""
import hashlib
import fcntl
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from unittest.mock import patch

import backend_acceptance as lab
import core
import launcher
import configuration
from proxy import Gateway


def logical_history(history):
    with sqlite3.connect((history / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        values = {}
        for name in ('threads', 'projects', 'project_roots', 'thread_sections'):
            rows = db.execute('SELECT * FROM ' + name).fetchall()
            values[name] = sorted(rows, key=repr)
    return values


def rollouts(history):
    return {str(p.relative_to(history)): hashlib.sha256(p.read_bytes()).hexdigest()
            for dirname in ('sessions', 'archived_sessions')
            for p in (history / dirname).rglob('*.jsonl*') if p.is_file()}


def catalog(home):
    path = home / 'sqlite/codex-dev.db'
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.25) as db:
            db.execute('PRAGMA query_only=ON')
            return {r[0]: {'cwd': r[1], 'project_id': r[2], 'missing': r[3]}
                    for r in db.execute("SELECT thread_id,cwd,project_id,missing_candidate "
                                        "FROM local_thread_catalog WHERE host_id='local'")}
    except sqlite3.OperationalError:
        return {}


def configure_fixture(path, base):
    original = path.read_text()
    wanted = tomllib.loads(original)
    configuration.overlay(wanted, {
        'chatgpt_base_url': base,
        'features': {'plugins': False, 'remote_plugin': False, 'apps': False},
        'check_for_update_on_startup': False,
    })
    value = configuration.patch_document(original, wanted)
    tomllib.loads(value)
    assert tomllib.loads(value)['openai_base_url'].startswith('http://127.0.0.1:')
    core.atomic_write(path, value)
    models_path = path.parent / 'smart2-models.json'
    models = json.loads(models_path.read_text())
    for model in models['models']:
        model.update(prefer_websockets=False, use_responses_lite=False)
    core.write_json(models_path, models)


def main():
    root = lab.ROOT
    report = {'status': 'running', 'real_credentials_used': False,
              'original_history_used': False, 'model_endpoint': 'loopback fixture',
              'scope': 'real installed desktop and CLI; synthetic files only'}
    core.write_json(root / 'catalog-result.json', report)
    server = ThreadingHTTPServer(('127.0.0.1', 0), lab.fixture.Mock)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    store = core.Store(root / 'control', root / 'external', mounted=lambda: False)
    store.initialize_storage('local')
    profile = store.save_profile({'kind': 'api', 'name': 'Synthetic catalog gate',
        'url': base + '/v1', 'protocol': 'responses', 'api_key': 'SYNTHETIC_R',
        'models': [{'alias': 'gpt-6-luna', 'upstream': 'synthetic/catalog',
                    'effort': 'low', 'context': 128000, 'reasoning': 'off'}]})
    prepared = store.prepare(profile['id'], 'local')
    history, home = Path(prepared['history']), Path(prepared['home'])
    gateway = Gateway(profile, 'SYNTHETIC_R')
    gateway.start()
    core.configure(prepared, gateway.url, gateway.token)
    configure_fixture(home / 'config.toml',base)
    cli = lab.Client(prepared, base)
    ids = []
    try:
        project = cli.call('project/create', {'name': 'Catalog synthetic project',
            'roots': [{'path': prepared['workspace']}],
            'idempotencyKey': 'catalog-gate-project'})['project']
        for n in range(6):
            thread = cli.start(cwd=prepared['workspace'])
            assert cli.turn(thread, f'SYNTHETIC_CATALOG_{n}')
            cli.call('thread/name/set', {'threadId': thread, 'name': f'Catalog fixture {n}'})
            cli.call('thread/metadata/update', {'threadId': thread, 'projectId': project['id']})
            ids.append(thread)
    finally:
        cli.stop()
        gateway.close()

    helper = root / 'HarborLaunch'
    source = Path(__file__).resolve().parents[1] / 'native/Sources/HarborLaunch/main.swift'
    subprocess.run(['/usr/bin/swiftc', '-module-cache-path', str(root / 'swift-cache'),
                    str(source), '-o', str(helper)], check=True, capture_output=True, timeout=90)

    tap = root / 'backend-tap.py'
    tap.write_text('''import importlib.util,json,sys
from pathlib import Path
log=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('guard',sys.argv[2])
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
original=guard.rewrite_permission_paths
def observed(raw,alias,home):
    try:
        obj=json.loads(raw)
        if obj.get('method')=='thread/list':
            with log.open('a') as f:
                f.write(json.dumps({'method':'thread/list','params':obj.get('params',{})})+'\\n')
    except (ValueError,TypeError):pass
    return original(raw,alias,home)
guard.rewrite_permission_paths=observed
sys.argv=sys.argv[2:]
guard.main()
''')
    original_script = launcher.backend_script
    current_phase = ['initial']

    def traced_script(prepared, pinned, command):
        command = [*command[:3], str(tap),
                   str(root / (current_phase[0] + '-rpc.jsonl')), *command[3:]]
        return original_script(prepared, pinned, command)

    def fixture(prepared, env, args):
        env.update(CODEX_SPARKLE_ENABLED='false',
                   CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH='1',
                   CODEX_REFRESH_TOKEN_URL_OVERRIDE=base + '/blocked-refresh',
                   CODEX_REVOKE_TOKEN_URL_OVERRIDE=base + '/blocked-revoke')
        path = Path(prepared['home']) / 'config.toml'
        configure_fixture(path,base)  # Validate before launch; no plugin downloads in this gate.

    def phase(name):
        current_phase[0] = name
        job = uuid.uuid4().hex
        path = core.directory(store.root / 'jobs') / (job + '.json')
        core.write_json(path, {'job': job, 'created_at': time.time(), 'access_mode': 'native'})
        worker = threading.Thread(target=launcher.run_instance,
            args=(store, job, profile['id'], 'local', None), kwargs={'lab': fixture})
        start = time.monotonic()
        worker.start()
        try:
            deadline = start + 100
            while time.monotonic() < deadline:
                state = json.loads(path.read_text())
                if state.get('status') == 'error':
                    raise AssertionError(state.get('message'))
                if state.get('desktop_launch_phase') == 'running':
                    assert Path(state['home']) == home, 'Gate must reconcile the corrupted target, not a different slot'
                rows = catalog(home)
                if set(ids).issubset(rows) and not any(rows[x]['missing'] for x in ids):
                    # Allow pending native migrations/transactions to complete.
                    time.sleep(2)
                    return {'elapsed_s': round(time.monotonic() - start, 3),
                            'catalog_check': state.get('catalog_check'),
                            'local_project_ids_are_null': all(rows[x]['project_id'] is None for x in ids),
                            'count': len(rows)}
                if not worker.is_alive():
                    raise AssertionError('Synthetic desktop worker ended before catalog was ready')
                time.sleep(.25)
            raise AssertionError('Synthetic native catalog did not become complete within 100 seconds')
        finally:
            # Do not race NSWorkspace's callback and leave our test window open.
            deadline = time.monotonic() + 25
            while worker.is_alive() and time.monotonic() < deadline:
                state = json.loads(path.read_text())
                if core.process_matches(state) or state.get('status') == 'error':
                    break
                time.sleep(.1)
            launcher.stop_instance(store, job)
            worker.join(timeout=20)
            if worker.is_alive():
                raise RuntimeError('Synthetic worker did not exit; preserve its journal and leases')
            # App exit precedes backend teardown. The product correctly avoids
            # a leased slot; this gate deliberately tests reuse of this one.
            locks = list((store.root / 'instance-slots').glob('*/backend.lock'))
            deadline = time.monotonic() + 20
            for lock in locks:
                with lock.open('r') as handle:
                    while True:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(handle, fcntl.LOCK_UN)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError('Synthetic backend retained its slot beyond teardown bound')
                            time.sleep(.1)

    print('EVIDENCE=' + str(root), flush=True)
    try:
        with patch.object(launcher, 'LAUNCH_HELPER', helper), \
             patch.object(launcher, 'backend_script', traced_script), \
             patch.object(launcher, 'snapshot', return_value='synthetic-no-shared-preferences'):
            report['initial'] = phase('initial')
            before, bodies = logical_history(history), rollouts(history)
            cache = home / 'sqlite/codex-dev.db'
            with sqlite3.connect(cache) as db:
                db.executemany("DELETE FROM local_thread_catalog WHERE host_id='local' AND thread_id=?",
                               [(x,) for x in ids[:3]])
            report['fixture_missing'] = len(set(ids) - set(catalog(home)))
            assert report['fixture_missing'] == 3
            report['recovered'] = phase('recovered')
            report['source_logical_records_unchanged'] = logical_history(history) == before
            report['rollout_bytes_unchanged'] = rollouts(history) == bodies
            requests = [json.loads(line) for line in (root / 'recovered-rpc.jsonl').read_text().splitlines()]
            report['native_list_requests'] = len(requests)
            report['all_list_calls_state_db_only'] = bool(requests) and all(
                item['params'].get('useStateDbOnly') is True for item in requests)
            assert report['source_logical_records_unchanged']
            assert report['rollout_bytes_unchanged']
            assert report['all_list_calls_state_db_only']
            report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, message=str(exc))
        raise
    finally:
        core.write_json(root / 'catalog-result.json', report)
        server.shutdown(); server.server_close()
        # This helper is a plain executable, not a registered application.
        helper.unlink(missing_ok=True)
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        path = lab.ROOT / 'catalog-result.json'
        value = json.loads(path.read_text()) if path.exists() else {}
        value.update(status='failed', error_type=type(exc).__name__, message=str(exc))
        core.write_json(path, value)
        print('EVIDENCE=' + str(lab.ROOT), flush=True)
        raise
