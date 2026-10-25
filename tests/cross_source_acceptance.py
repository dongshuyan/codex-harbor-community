"""Same persisted thread: official -> Router hosted search -> official + compact.

Run with ``python3 -B tests/cross_source_acceptance.py``. Uses the installed
native CLI, real Store/Gateway/backend_guard, and localhost fixtures only. A
test-only bootstrap injects the official bridge's upstream constructor seam
and observes ingress headers; production request handling runs unchanged. It
loads no real credentials or private history and makes no live model/search
requests. Evidence stays in a newly created /private/tmp directory.

The official temporary config deliberately points at a failing sentinel URL
and enables request compression. The real native child must still reach the
guard's bridge uncompressed, without changing the config file.

The strict fixture models the observed hosted-replay declaration requirement;
it is not evidence that the real service accepts a request. It recognizes both
ordinary Responses tools and Lite additional_tools, without choosing a repair.
Do not replace the ID-only resumes with thread/resume.history: that unstable
API creates another thread and would invalidate this regression.
"""
import argparse
import base64
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import tomllib
import traceback
import uuid


COLOR = 'teal'
OFFICIAL_FACT = 'OFFICIAL_MEMORY=teal'
SOURCE_FACT = 'ROUTER_SEARCH_RESULT=kite-7'
SEARCH_URL = 'https://example.invalid/cross-source/reference'
HOSTED_SEARCH = {
    'id': 'ws_cross_source_synthetic', 'type': 'web_search_call', 'status': 'completed',
    'action': {'type': 'search', 'query': 'synthetic cross source query',
               'queries': ['synthetic cross source query', 'synthetic alternate query']},
}
COMPACTION_PREFIX = 'SYNTHETIC_COMPACTION:'
OFFICIAL_BOOTSTRAP = '''
import json
import os
import sys
engine, upstream, evidence, *arguments = sys.argv[1:]
sys.path.insert(0, engine)
import official_responses
original = official_responses.OfficialResponsesBridge
def record(value):
    with open(evidence, 'a') as handle:
        handle.write(json.dumps(value) + '\\n')
def synthetic_bridge(*args, **kwargs):
    kwargs['upstream_base'] = upstream
    bridge = original(*args, **kwargs)
    handler = bridge.server.RequestHandlerClass
    class ObservedHandler(handler):
        def observe(self):
            record({'kind': 'request', 'method': self.command, 'path': self.path,
                    'request_bytes': int(self.headers.get('Content-Length', '0')),
                    'content_encoding': self.headers.get('Content-Encoding'),
                    'lite': self.headers.get('x-openai-internal-codex-responses-lite') == 'true',
                    'upgrade': self.headers.get('Upgrade')})
        def do_GET(self):
            self.observe()
            return super().do_GET()
        def do_POST(self):
            self.observe()
            return super().do_POST()
    bridge.server.RequestHandlerClass = ObservedHandler
    record({'kind': 'bridge_created', 'url': bridge.url,
            'environment_openai_base_url': os.environ.get('OPENAI_BASE_URL')})
    return bridge
official_responses.OfficialResponsesBridge = synthetic_bridge
import backend_guard
sys.argv = [str(backend_guard.__file__), *arguments]
backend_guard.main()
'''


def resume_params(thread_id):
    return {'threadId': thread_id, 'model': None, 'modelProvider': None}


def frozen_files(paths):
    return {str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}


def unchanged_files(before):
    try:
        return frozen_files(before) == before
    except OSError:
        return False


def append_only(path, original):
    try:
        with Path(path).open('rb') as handle:
            return handle.read(len(original)) == original
    except OSError:
        return False


def native_transport_override_observed(ingress, upstream):
    """Require actual bridge traffic; argv strings or an empty trace cannot pass."""
    posts = [row for row in ingress if row.get('method') == 'POST']
    return bool(upstream) and len(posts) == len(upstream) and all(
        row.get('path') == '/backend-api/codex/responses'
        and row.get('content_encoding') in (None, 'identity')
        and row.get('request_bytes', 0) > 0 for row in posts) and all(
        row.get('path') == '/A/backend-api/codex/responses'
        and row.get('identity') == 'A' for row in upstream)


def search_declared(tools):
    return isinstance(tools, list) and any(
        isinstance(tool, dict) and tool.get('type') in ('web_search', 'web_search_preview')
        for tool in tools)


def replay_error(body, *, lite):
    """Fixture boundary, not a proposed production implementation."""
    items = body.get('input', [])
    if not isinstance(items, list):
        return 'input is not a list'
    hosted = any(isinstance(item, dict) and item.get('type') == 'web_search_call' for item in items)
    if not hosted:
        return None
    top = search_declared(body.get('tools'))
    additional = any(isinstance(item, dict) and item.get('type') == 'additional_tools'
                     and search_declared(item.get('tools')) for item in items)
    if lite and top:
        return 'Lite hosted search was declared in top-level tools'
    if not additional and not (top and not lite):
        return 'hosted search history has no matching declaration'
    return None


def required_history_present(body):
    """Require information emitted only by prior responses, including the search.

    Compaction is opaque to Codex. Only this synthetic endpoint decodes its own
    fixture capsule, so the post-compaction check still depends on sent input.
    """
    items = copy.deepcopy(body.get('input', []))
    if not isinstance(items, list):
        return False
    for item in list(items):
        capsule = item.get('encrypted_content', '') if isinstance(item, dict) else ''
        if isinstance(capsule, str) and capsule.startswith(COMPACTION_PREFIX):
            try:
                items.extend(json.loads(base64.b64decode(capsule[len(COMPACTION_PREFIX):], validate=True)))
            except (ValueError, TypeError, UnicodeError):
                return False
    text = json.dumps(items, ensure_ascii=False)
    return all(value in text for value in (
        COLOR, SOURCE_FACT, SEARCH_URL, HOSTED_SEARCH['id'],
        HOSTED_SEARCH['action']['query'], *HOSTED_SEARCH['action']['queries']))


def response_events(outputs, model):
    rid = 'resp_' + uuid.uuid4().hex
    events = [{'type': 'response.created', 'response': {'id': rid, 'status': 'in_progress', 'output': []}}]
    for index, item in enumerate(outputs):
        events.append({'type': 'response.output_item.added', 'output_index': index, 'item': item})
        if item['type'] == 'message':
            text = item['content'][0]['text']
            events.append({'type': 'response.output_text.delta', 'item_id': item['id'],
                           'output_index': index, 'content_index': 0, 'delta': text})
            events.append({'type': 'response.output_text.done', 'item_id': item['id'],
                           'output_index': index, 'content_index': 0, 'text': text})
        events.append({'type': 'response.output_item.done', 'output_index': index, 'item': item})
    events.append({'type': 'response.completed', 'response': {
        'id': rid, 'status': 'completed', 'model': model, 'output': outputs,
        'usage': {'input_tokens': 32, 'output_tokens': 16, 'total_tokens': 48}}})
    return events


def message(text, *, citation=False):
    annotations = [{'type': 'url_citation', 'url': SEARCH_URL,
                    'title': 'Synthetic search reference', 'start_index': 0, 'end_index': len(text)}] if citation else []
    return {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': annotations}]}


def run(model, web_search='cached', official_web_search=None):
    # Reuse the existing native RPC/process harness. Importing this module for
    # unit tests does not create temp homes, load accounts, or start processes.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backend_acceptance as lab
    import configuration
    from proxy import Gateway

    core = lab.core
    root = lab.ROOT.with_name(lab.ROOT.name + '.noindex')
    lab.ROOT.rename(root)
    lab.ROOT = lab.fixture.ROOT = root
    records, checks, clients = [], [], lab.CLIENTS
    gateway = None
    immutable = {}
    original = None
    rollout = None
    native_turns = []
    completed_flow = False
    ingress_path = root / 'cross-source-bridge-ingress.jsonl'

    def check(name, passed, **detail):
        checks.append({'name': name, 'passed': bool(passed), **detail})
        core.write_json(root / 'cross-source-results.json', checks)
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
        return bool(passed)

    class Endpoint(lab.fixture.Mock):
        protocol_version = 'HTTP/1.1'
        phase = 'official_seed'
        official_token = None

        def reject_sentinel(self):
            if '/sentinel/' not in self.path:
                return False
            records.append({'phase': self.phase, 'sentinel_hit': True,
                            'method': self.command, 'path': self.path,
                            'content_encoding': self.headers.get('Content-Encoding')})
            self.send_error(502, 'The native child reached a conflicting sentinel URL')
            self.close_connection = True
            return True

        def do_GET(self):
            if self.reject_sentinel():
                return
            if self.path.endswith('/responses') and self.headers.get('Upgrade', '').lower() == 'websocket':
                self.send_response(426)
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                return
            super().do_GET()

        def do_CONNECT(self):
            records.append({'phase': self.phase, 'blocked_external_connection': True, 'destination': self.path})
            self.send_error(403, 'External connections are forbidden in this fixture')

        def do_POST(self):
            if self.reject_sentinel():
                return
            if not self.path.endswith('/responses'):
                records.append({'phase': self.phase, 'unexpected_post': self.path})
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            body = json.loads(raw)
            auth = self.headers.get('Authorization', '')
            identity = 'A' if auth == 'Bearer ' + self.official_token else (
                'R' if auth == 'Bearer SYNTHETIC_R' else 'UNKNOWN')
            lite = self.headers.get('x-openai-internal-codex-responses-lite', '').lower() == 'true'
            record = {'phase': self.phase, 'path': self.path, 'identity': identity,
                      'account': self.headers.get('ChatGPT-Account-Id'), 'lite': lite,
                      'body': body, 'request_bytes': len(raw),
                      'content_encoding': self.headers.get('Content-Encoding'),
                      'input_type_counts': dict(Counter(item.get('type', 'message') for item in body.get('input', [])))}
            records.append(record)
            problem = replay_error(body, lite=lite)
            text = json.dumps(body.get('input', []))
            if self.phase == 'official_seed':
                if COLOR not in text:
                    problem = 'initial user memory was not sent'
                outputs = [message(OFFICIAL_FACT)]
            elif self.phase == 'router_search':
                if OFFICIAL_FACT not in text:
                    problem = 'Router did not receive prior official answer'
                if not search_declared(body.get('tools')):
                    problem = 'Router search tool is not declared'
                outputs = [copy.deepcopy(HOSTED_SEARCH),
                           message(f'{COLOR}; {SOURCE_FACT}; {SEARCH_URL}', citation=True)]
            else:
                if not required_history_present(body):
                    problem = 'prior search query, citation, answer or memory was lost'
                if self.phase == 'official_compact':
                    if not any(item.get('type') == 'compaction_trigger' for item in body.get('input', [])):
                        problem = 'manual compact did not request native compaction'
                    capsule = base64.b64encode(json.dumps(body.get('input', [])).encode()).decode()
                    outputs = [{'id': 'cmp_cross_source', 'type': 'compaction',
                                'encrypted_content': COMPACTION_PREFIX + capsule}]
                else:
                    outputs = [message(f'{COLOR}; {SOURCE_FACT}; {SEARCH_URL}')]
            record['rejection'] = problem
            core.write_json(root / 'cross-source-requests.json', records)
            events = response_events(outputs, body.get('model')) if problem is None else [
                {'type': 'response.created', 'response': {'id': 'resp_rejected', 'output': []}},
                {'type': 'error', 'code': 'internal_error',
                 'message': 'Synthetic hosted replay rejection: ' + problem}]
            data = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    api = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{api.server_port}'
    config_sentinel = base + '/sentinel/config/backend-api/codex'
    environment_sentinel = base + '/sentinel/environment/backend-api/codex'
    store = core.Store(root / 'control', root / 'external', mounted=lambda: True,
                       user_home=core.directory(root / 'synthetic-user'))

    def client(prepared, official=False):
        env = {name: base for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                      'http_proxy', 'https_proxy', 'all_proxy')}
        options = {'env_overrides': env}
        if official:
            env.update(HARBOR_OFFICIAL_PROFILE=prepared['id'], HARBOR_CONTROL_ROOT=str(store.root),
                       OPENAI_BASE_URL=environment_sentinel)
            options['command'] = [sys.executable, '-I', '-B', '-c', OFFICIAL_BOOTSTRAP, str(core.ENGINE),
                                  base + '/A/backend-api/codex', str(ingress_path),
                                  '-', '-', '-', str(core.CODEX), '-c', 'features.code_mode_host=true',
                                  'app-server', '--analytics-default-enabled', '-c',
                                  'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=false']
        result = lab.Client(prepared, base, **options)
        return result

    def finish(client, tid, phase, prompt=None):
        Endpoint.phase = phase
        client.pending.clear()
        requested = client.call('thread/compact/start', {'threadId': tid}) if prompt is None else client.call(
            'turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text': prompt}], 'effort': 'low'})
        turn_id = (requested.get('turn') or {}).get('id')
        deadline = time.monotonic() + 45
        errors, text, compacted = [], '', False
        while True:
            event = client.pending.pop(0) if client.pending else client.next(deadline)
            method, params = event.get('method'), event.get('params', {})
            if params.get('threadId') not in (None, tid):
                continue
            if method == 'item/agentMessage/delta':
                text += params.get('delta', '')
            if method == 'item/completed' and params.get('item', {}).get('type') == 'contextCompaction':
                compacted = True
            if method == 'error':
                errors.append(params)
                if params.get('willRetry') and params.get('turnId'):
                    # Expose the first boundary failure without waiting through
                    # the native retry backoff; never misreport this as completed.
                    client.call('turn/interrupt', {'threadId': tid, 'turnId': params['turnId']})
            if method == 'turn/completed' and (turn_id is None or params['turn']['id'] == turn_id):
                result = {'phase': phase, 'thread_id': tid, 'status': params['turn']['status'],
                          'errors': errors, 'text': text, 'compaction_item': compacted}
                native_turns.append(result)
                core.write_json(root / 'cross-source-turns.json', native_turns)
                return result

    try:
        store.initialize_storage('local')
        profiles = {
            'A': store.save_profile({'kind': 'official', 'name': 'Synthetic official'}),
            'R': store.save_profile({'kind': 'api', 'name': 'Synthetic Router', 'url': base + '/R/v1',
                 'protocol': 'responses', 'api_key': 'SYNTHETIC_R',
                 'models': [{'alias': model, 'upstream': 'synthetic/' + model, 'context': 272000,
                             'reasoning': 'standard', 'effort': 'low', 'levels': ['low']}]}),
        }
        prepared = {}
        for label, profile in profiles.items():
            item = store.prepare(profile['id'], 'local', model)
            item['access_mode'] = 'readonly'
            prepared[label] = item
            home = Path(item['home'])
            if label == 'R':
                gateway = Gateway(profile, store.secret(profile['id']))
                gateway.start()
                core.configure(item, gateway.url, gateway.token)
            else:
                core.configure(item)
                token = lab.fixture.jwt('A')
                Endpoint.official_token = token
                core.write_json(home / 'auth.json', {'auth_mode': 'chatgpt', 'tokens': {
                    'id_token': token, 'access_token': token, 'refresh_token': 'SYNTHETIC_REFRESH_A',
                    'account_id': 'synthetic-A'},
                    'last_refresh': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')})
            config_path = home / 'config.toml'
            source = config_path.read_text()
            config = tomllib.loads(source)
            config.update(web_search=(official_web_search or web_search) if label=='A' else web_search, project_doc_max_bytes=0)
            config.setdefault('features', {})['plugins'] = False
            if label == 'A':
                config.update(openai_base_url=config_sentinel, chatgpt_base_url=base + '/A',
                              model_catalog_json=str(home / 'smart2-models.json'))
                config['features']['enable_request_compression'] = True
            core.atomic_write(config_path, configuration.patch_document(source, config))
        protected = []
        for item in prepared.values():
            home = Path(item['home'])
            protected.extend([home / 'auth.json', home / 'config.toml', home / 'smart2-models.json',
                              store.root / 'profiles' / (item['id'] + '.json')])
        immutable = frozen_files(protected)
        official_config_path = Path(prepared['A']['home']) / 'config.toml'
        official_config = tomllib.loads(official_config_path.read_text())
        check('official_transport_conflicts_set_in_temporary_config',
              official_config.get('openai_base_url') == config_sentinel
              and official_config.get('features', {}).get('enable_request_compression') is True)

        a = client(prepared['A'], official=True)
        started = a.call('thread/start', {'model': model, 'cwd': prepared['A']['workspace'],
                    'approvalPolicy': 'never', 'ephemeral': False})
        tid = started['thread']['id']
        if not check('official_seed_completed', finish(a, tid, 'official_seed',
              f'Remember the synthetic color {COLOR}. Reply with the color.')['status'] == 'completed'):
            raise AssertionError('Official seed did not complete; hosted replay has not been tested')
        a.stop()
        r = client(prepared['R'])
        resumed = r.call('thread/resume', resume_params(tid))
        check('same_thread_official_to_router', resumed['thread']['id'] == tid)
        if not check('router_search_completed', finish(r, tid, 'router_search',
              'Read the synthetic reference using web search and keep the previous color.')['status'] == 'completed'):
            raise AssertionError('Router seed did not complete; hosted replay has not been tested')
        r.stop()
        matches = list(Path(prepared['R']['history']).glob('sessions/**/*' + tid + '*.jsonl'))
        if not check('one_original_rollout', len(matches) == 1):
            raise AssertionError('Cannot establish original persisted rollout')
        rollout = matches[0]
        original = rollout.read_bytes()
        source_rows = [json.loads(line) for line in original.splitlines() if line.strip()]
        if not check('hosted_search_persisted_exactly', any(
              row.get('type') == 'response_item' and all(row.get('payload', {}).get(key) == value
              for key, value in HOSTED_SEARCH.items()) for row in source_rows)):
            raise AssertionError('No exact hosted history; continuation regression cannot be tested')

        a = client(prepared['A'], official=True)
        resumed = a.call('thread/resume', resume_params(tid))
        check('same_thread_router_to_official', resumed['thread']['id'] == tid)
        continuation = finish(a, tid, 'official_return',
                              'Repeat the saved color, reference result and reference URL. No new search.')
        check('official_continuation_completed', continuation['status'] == 'completed')
        check('official_continuation_kept_prior_information', all(
              value in continuation['text'] for value in (COLOR, SOURCE_FACT, SEARCH_URL)))
        compact = finish(a, tid, 'official_compact')
        check('official_compaction_completed', compact['status'] == 'completed' and compact['compaction_item'])
        if compact['status'] == 'completed':
            after = finish(a, tid, 'after_compaction',
                           'Repeat the saved color, reference result and reference URL. No new search.')
            check('post_compaction_continuation_completed', after['status'] == 'completed')
            check('post_compaction_information_preserved', all(
                  value in after['text'] for value in (COLOR, SOURCE_FACT, SEARCH_URL)))
        else:
            check('post_compaction_continuation_completed', False, blocked_by='official_compaction_failed')
        a.stop()
        requests = [record for record in records if 'body' in record]
        check('all_upstream_requests_use_the_same_thread', bool(requests) and all(
              record.get('body', {}).get('prompt_cache_key') == tid
              for record in requests))
        official_requests = [record for record in requests if record['phase'] != 'router_search']
        router_requests = [record for record in requests if record['phase'] == 'router_search']
        check('official_routes_use_official_account', bool(official_requests) and all(
              record['identity'] == 'A' and record['account'] == 'synthetic-A'
              for record in official_requests))
        check('router_routes_use_router_key_without_official_header', bool(router_requests) and all(
              record['identity'] == 'R' and record['account'] is None
              for record in router_requests))
        hosted_replays = [record for record in official_requests
                          if record['phase'] in ('official_return', 'official_compact')]
        check('official_replay_retains_exact_hosted_item', len(hosted_replays) >= 2 and all(any(
              all(item.get(key) == value for key, value in HOSTED_SEARCH.items())
              for item in record['body'].get('input', []) if item.get('type') == 'web_search_call')
              for record in hosted_replays))
        check('official_replay_keeps_native_lite', bool(hosted_replays) and all(record['lite'] for record in hosted_replays))
        check('uncompressed_model_requests', all(record['content_encoding'] in (None, 'identity') for record in requests))
        ingress = [json.loads(line) for line in ingress_path.read_text().splitlines() if line.strip()]
        bridges = [row for row in ingress if row.get('kind') == 'bridge_created']
        check('official_runtime_override_observed_at_native_bridge_ingress',
              native_transport_override_observed(ingress, official_requests),
              official_posts=len(official_requests),
              bridge_posts=sum(row.get('method') == 'POST' for row in ingress),
              content_encodings=[row.get('content_encoding') for row in ingress if row.get('method') == 'POST'])
        check('official_environment_conflict_reached_guard', bool(bridges) and all(
              row.get('environment_openai_base_url') == environment_sentinel for row in bridges))
        check('official_transport_sentinels_never_reached', not any(row.get('sentinel_hit') for row in records))
        check('official_conflicting_config_bytes_unchanged', unchanged_files({
              str(official_config_path): immutable[str(official_config_path)]}))
        check('all_model_requests_use_known_loopback_routes', not any('unexpected_post' in record for record in records))
        completed_flow = True
    except Exception:
        core.atomic_write(root / 'cross-source-error.txt', traceback.format_exc())
        raise
    finally:
        for item in clients:
            item.stop()
        if gateway:
            gateway.close()
        api.shutdown()
        api.server_close()
        if immutable:
            check('accounts_profiles_settings_unchanged', unchanged_files(immutable))
        if original is not None:
            check('original_rollout_bytes_preserved', append_only(rollout, original))
        check('acceptance_flow_reached_terminal_checks', completed_flow)
        core.write_json(root / 'cross-source-run.json', {
            'codex': str(core.CODEX), 'model': model, 'synthetic_only': True,
            'real_credentials_loaded': False, 'live_model_requests': 0,
            'web_search_config': web_search,
            'official_web_search_config': official_web_search or web_search,
            'transport_conflicts': {'config_openai_base_url': config_sentinel,
                'environment_OPENAI_BASE_URL': environment_sentinel,
                'config_enable_request_compression': True},
            'blocked_external_connect_attempts': sum(bool(row.get('blocked_external_connection')) for row in records),
            'official_upstream_fixture': 'constructor injection only; backend guard and HTTP handlers unchanged',
            'native_turns': native_turns, 'checks': checks,
            'clients_exited': all(item.proc.poll() is not None for item in clients),
            'passed': completed_flow and all(row['passed'] for row in checks),
            'fixture_boundary': 'hosted replay needs a declaration in the correct dialect',
        })
        core.write_json(root / 'cross-source-requests.json', records)
        print('EVIDENCE=' + str(root), flush=True)
    return all(row['passed'] for row in checks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='gpt-6-astra', help='installed model alias; endpoints remain synthetic')
    parser.add_argument('--web-search', choices=('cached', 'live'), default='cached',
                        help='native declaration setting; fixture never searches the network')
    parser.add_argument('--official-web-search', choices=('disabled','cached','live'),
                        help='official-only setting after Router has stored hosted-search history')
    args = parser.parse_args()
    try:
        passed = run(args.model, args.web_search,args.official_web_search)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0 if passed else 1)
