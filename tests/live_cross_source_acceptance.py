"""Opt-in real-service acceptance; importing or showing help makes no requests.

Synthetic example (replace every path explicitly):
  python3 -B tests/live_cross_source_acceptance.py --run-live \
    --official-home /chosen/account/codex --router-profile /chosen/profile.json \
    --output /private/tmp/chosen-cross-source.noindex

With --rollout /chosen/rollout.jsonl, only the selected official account receives
the byte-for-byte temporary copy. That mode never constructs or contacts Router.
All resumes use the persisted thread ID. Source accounts/config/history are
read-only; no refresh credential is supplied to any child or copied to disk.
Runtime history, native caches, and temporary configs are deleted on exit.
The retained report contains checks, counts and hashes, never request bodies,
model output, server error text, source paths, or credentials. A failed/expired
access token is reported without refreshing or changing the original account.
"""
import argparse
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid


ENGINE = Path(__file__).resolve().parents[1] / 'engine'
KNOWN_ITEM_TYPES = frozenset({
    'message', 'reasoning', 'function_call', 'function_call_output',
    'custom_tool_call', 'custom_tool_call_output', 'web_search_call',
    'computer_call', 'computer_call_output', 'image_generation_call',
    'local_shell_call', 'local_shell_call_output', 'tool_search_call',
    'tool_search_output', 'additional_tools', 'configuration_update',
    'compaction', 'compaction_trigger', 'context_compaction', 'item_reference',
})
KNOWN_ERRORS = frozenset({
    'contextWindowExceeded', 'usageLimitExceeded', 'httpConnectionFailed',
    'responseStreamConnectionFailed', 'responseStreamDisconnected',
    'responseTooManyFailedAttempts', 'internalServerError', 'unauthorized',
    'badRequest', 'sandboxError', 'threadRollbackFailed', 'other',
})


class AcceptanceFailure(Exception):
    """Its message is a harness-controlled classification, never server text."""


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def prefix_digest(path, size):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        remaining = size
        while remaining:
            chunk = handle.read(min(remaining, 1024 * 1024))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def read_official_auth(home):
    path = Path(home) / 'auth.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise AcceptanceFailure('invalid_official_auth_file')
        value = json.load(handle)
    tokens = value.get('tokens', {})
    access, account = tokens.get('access_token'), tokens.get('account_id')
    if not all(isinstance(item, str) and item and not any(char.isspace() for char in item)
               for item in (access, account)):
        raise AcceptanceFailure('missing_access_credentials')
    return {'type': 'chatgptAuthTokens', 'accessToken': access, 'chatgptAccountId': account}


def copy_rollout(source, target):
    """Copy bytes without repairing, filtering or reserializing the original."""
    source, target = Path(source), Path(target)
    if source.is_symlink():
        raise AcceptanceFailure('rollout_symlink_not_allowed')
    with source.open('rb') as incoming:
        first = incoming.readline(1024 * 1024)
        row = json.loads(first)
        if row.get('type') != 'session_meta':
            raise AcceptanceFailure('missing_rollout_session_metadata')
        thread_id = str(uuid.UUID(row['payload']['id']))
        if thread_id != row['payload']['id']:
            raise AcceptanceFailure('noncanonical_thread_id')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as outgoing:
            outgoing.write(first)
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    if file_digest(source) != file_digest(target):
        raise AcceptanceFailure('source_changed_while_copying')
    return thread_id


def error_kind(error):
    info = error.get('codexErrorInfo') if isinstance(error, dict) else None
    kind = next(iter(info), 'other') if isinstance(info, dict) else info
    return kind if isinstance(kind, str) and kind in KNOWN_ERRORS else 'other'


def wire_summary(raw, content_encoding):
    try:
        body = json.loads(raw)
        items = body.get('input', []) if isinstance(body, dict) else []
        counts = Counter(item.get('type', 'message') if item.get('type', 'message') in KNOWN_ITEM_TYPES
                         else 'other' for item in items if isinstance(item, dict)) if isinstance(items, list) else {}
    except (ValueError, TypeError):
        counts = {}
    return {'request_bytes': len(raw), 'content_encoding': content_encoding,
            'input_type_counts': dict(counts)}


class NativeClient:
    def __init__(self, command, env, cwd, auth, *, timeout):
        from login import RPC
        self.rpc = RPC(command, env, cwd)
        self.counter = 0
        self.pending = []
        self.timeout = timeout
        self.cwd = Path(cwd).resolve()
        self.denied_callbacks = 0
        try:
            self.call('initialize', {'clientInfo': {'name': 'harbor_explicit_live_acceptance', 'version': '1'},
                                    'capabilities': {'experimentalApi': True}})
            self.rpc.send('initialized', {})
            self.call('account/login/start', auth)
        except BaseException:
            self.close()
            raise

    def read(self, deadline):
        event = self.rpc.read(deadline)
        if 'method' in event and 'id' in event:
            self.denied_callbacks += 1
            # No refresh authority, browser actions, approvals, or external
            # callback tools are available to this acceptance process.
            self.rpc.proc.stdin.write(json.dumps({'id': event['id'], 'error': {
                'code': -32601, 'message': 'Live acceptance does not allow this callback'}}).encode() + b'\n')
            self.rpc.proc.stdin.flush()
        return event

    def call(self, method, params):
        self.counter += 1
        ident = self.counter
        self.rpc.send(method, params, ident)
        deadline = time.monotonic() + min(self.timeout, 45)
        while True:
            event = self.read(deadline)
            if event.get('id') == ident and 'method' not in event:
                if 'error' in event:
                    raise AcceptanceFailure('native_rpc_rejected')
                return event.get('result') or {}
            self.pending.append(event)

    def turn(self, thread_id, prompt=None, *, expected=(), printf_marker=None, exact_reply=None, effort='low'):
        self.pending.clear()
        result = self.call('thread/compact/start', {'threadId': thread_id}) if prompt is None else self.call(
            'turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}], 'effort': effort})
        turn_id = (result.get('turn') or {}).get('id')
        deadline = time.monotonic() + self.timeout
        text, errors, item_counts = '', [], Counter()
        printf_ok, command_output = False, {}
        while True:
            event = self.pending.pop(0) if self.pending else self.read(deadline)
            method, params = event.get('method'), event.get('params', {})
            if params.get('threadId') not in (None, thread_id):
                continue
            if method == 'item/agentMessage/delta':
                text += params.get('delta', '')
            if method == 'item/commandExecution/outputDelta' and printf_marker:
                ident = params.get('itemId')
                command_output[ident] = (command_output.get(ident, '') + params.get('delta', ''))[-1024:]
            if method == 'item/completed':
                item = params.get('item', {})
                kind = item.get('type')
                if kind in ('agentMessage', 'commandExecution', 'webSearch', 'contextCompaction'):
                    item_counts[kind] += 1
                if kind == 'commandExecution' and printf_marker:
                    printf_ok |= (item.get('exitCode') == 0
                                  and printf_marker in ((item.get('aggregatedOutput') or '') + command_output.get(item.get('id'), ''))
                                  and bool(re.search(r'\bprintf\b', item.get('command', '')))
                                  and Path(item.get('cwd', '')).resolve() == self.cwd)
            if method == 'error':
                errors.append(error_kind(params.get('error')))
                if params.get('willRetry') and params.get('turnId'):
                    self.call('turn/interrupt', {'threadId': thread_id, 'turnId': params['turnId']})
            if method == 'turn/completed' and (turn_id is None or params['turn']['id'] == turn_id):
                return {'status': params['turn']['status'], 'error_kinds': errors,
                        'expected_values_recalled': all(value.lower() in text.lower() for value in expected),
                        'exact_reply_matched': exact_reply is None or text.strip() == exact_reply,
                        'printf_completed_in_workspace': printf_ok,
                        'item_type_counts': dict(item_counts), 'denied_callbacks': self.denied_callbacks}

    def close(self):
        self.rpc.close()


def _write_private(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)


def _config(home, history, protected, model, *, web_search):
    home.mkdir(mode=0o700, parents=True)
    for name in ('sessions', 'archived_sessions', 'thread-writer-locks', 'rollout-migrations'):
        (history / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        (home / name).symlink_to(history / name, target_is_directory=True)
    (history / '.tmp').mkdir(mode=0o700, exist_ok=True)
    (home / '.tmp').mkdir(mode=0o700)
    (home / '.tmp' / 'rollout-maintenance.lock').symlink_to(history / '.tmp' / 'rollout-maintenance.lock')
    lines = [f'model = {json.dumps(model)}', 'model_provider = "openai"',
             f'sqlite_home = {json.dumps(str(history))}', 'cli_auth_credentials_store = "ephemeral"',
             'mcp_oauth_credentials_store = "file"', 'approval_policy = "never"',
             'default_permissions = "acceptance-read-only"', f'web_search = {json.dumps(web_search)}',
             'project_doc_max_bytes = 0', 'check_for_update_on_startup = false',
             '[permissions.acceptance-read-only]', 'extends = ":read-only"',
             '[permissions.acceptance-read-only.filesystem]']
    lines.extend(json.dumps(str(path)) + ' = "deny"' for path in sorted(set(protected)))
    lines.extend(['[permissions.acceptance-read-only.network]', 'enabled = false',
                  '[features]', 'enable_request_compression = false', 'apps = false',
                  'plugins = false', 'remote_plugin = false', 'computer_use = false',
                  'shell_snapshot = false', 'shell_snapshot_v2 = false', 'runtime_metrics = false',
                  'api_key_model_discovery = false', '[memories]',
                  'generate_memories = false', 'use_memories = false',
                  '[analytics]', 'enabled = false', '[feedback]', 'enabled = false',
                  '[otel]', 'exporter = "none"', 'trace_exporter = "none"', 'metrics_exporter = "none"'])
    _write_private(home / 'config.toml', '\n'.join(lines) + '\n')


def main(args):
    sys.path.insert(0, str(ENGINE))
    import core
    import official_responses
    import proxy
    from reasoning import configure_model

    source_home = args.official_home.resolve(strict=True)
    source_rollout = args.rollout.resolve(strict=True) if args.rollout else None
    source_profile = args.router_profile.resolve(strict=True) if args.router_profile and not source_rollout else None
    protected = [source_home, Path.home()]
    protected.extend(path for path in (source_rollout, source_profile) if path)
    if args.output:
        output = args.output.resolve()
        if output.suffix != '.noindex' or output.exists():
            raise AcceptanceFailure('output_must_be_new_noindex_directory')
        if any(output == path or path in output.parents for path in protected):
            raise AcceptanceFailure('output_overlaps_source_location')
        output.mkdir(mode=0o700, parents=False)
    else:
        output = Path(tempfile.mkdtemp(prefix='harbor-live-cross-source-', suffix='.noindex', dir='/private/tmp'))
    runtime = output / 'private-runtime'
    runtime.mkdir(mode=0o700)
    workspace = runtime / 'workspace'
    workspace.mkdir(mode=0o700)
    history = runtime / 'history'
    history.mkdir(mode=0o700)
    official_home, router_home = runtime / 'official', runtime / 'router'
    source_files = [source_home / 'auth.json']
    if (source_home / 'config.toml').is_file():
        source_files.append(source_home / 'config.toml')
    source_files.extend(path for path in (source_profile, source_rollout) if path)
    original_hashes = {}
    report = {'mode': 'official_original_copy' if source_rollout else 'synthetic_cross_source',
              'checks': [], 'stages': [], 'wire': [], 'live_requests_authorized': True,
              'uses_unstable_history_resume': False, 'original_config_modified': False,
              'refresh_credentials_copied': False, 'runtime_removed': False, 'passed': False}
    clients, bridge, gateway = [], None, None
    copied_rollout = None
    preserved_prefix = None
    frozen_configs = {}
    current_stage = 'setup'
    original_declare, original_map = official_responses.declare_search, proxy.map_request

    def write_report():
        core.atomic_write(output / 'result.json', json.dumps(report, indent=2, ensure_ascii=False) + '\n')

    def check(name, passed):
        report['checks'].append({'name': name, 'passed': bool(passed)})
        write_report()
        if not passed:
            raise AcceptanceFailure(name)

    def record_declare(raw, *values, **options):
        result = original_declare(raw, *values, **options)
        item = wire_summary(raw, None)
        item.update(stage=current_stage, source='official', rewritten_bytes=len(result))
        try:
            before, after = json.loads(raw), json.loads(result)
            original_items = [row for row in before.get('input', []) if row.get('type') != 'additional_tools']
            forwarded_items = [row for row in after.get('input', []) if row.get('type') != 'additional_tools']
            item['original_history_items_preserved'] = original_items == forwarded_items
        except (ValueError, AttributeError, TypeError):
            item['history_comparison_available'] = False
        report['wire'].append(item)
        return result

    def record_map(profile, body):
        raw = json.dumps(body).encode()
        item = wire_summary(raw, None)
        item.update(stage=current_stage, source='router', bytes_are_reserialized=True)
        report['wire'].append(item)
        return original_map(profile, body)

    def record_headers(server, source):
        handler = server.RequestHandlerClass

        class ObservedHandler(handler):
            def do_POST(self):
                report['wire'].append({'stage': current_stage, 'source': source, 'kind': 'raw_headers',
                    'content_encoding': self.headers.get('Content-Encoding'),
                    'request_bytes': int(self.headers.get('Content-Length', '0')),
                    'lite': self.headers.get('x-openai-internal-codex-responses-lite') == 'true'})
                return super().do_POST()

        server.RequestHandlerClass = ObservedHandler

    def start(home, login, official):
        osh = runtime / ('os-' + home.name)
        osh.mkdir(mode=0o700, exist_ok=True)
        temporary = osh / 'tmp'
        temporary.mkdir(mode=0o700, exist_ok=True)
        env = {'HOME': str(osh), 'ZDOTDIR': str(osh), 'CODEX_HOME': str(home),
               'CODEX_SQLITE_HOME': str(history), 'TMPDIR': str(temporary),
               'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin', 'LANG': 'en_US.UTF-8',
               'PYTHONDONTWRITEBYTECODE': '1', 'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
        for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
            if key in os.environ:
                env[key] = os.environ[key]
        # Match the desktop's root override plus trailing plugin override. A
        # bare app-server missed Codex's mixed-scope -c parsing defect.
        arguments = ['-c', 'features.code_mode_host=true', 'app-server',
                     '--analytics-default-enabled', '-c',
                     'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=false']
        if official:
            from backend_guard import ephemeral_arguments
            arguments = official_responses.native_arguments(ephemeral_arguments(arguments), bridge.url)
        instance = NativeClient([str(core.CODEX), *arguments], env, str(workspace), login, timeout=args.timeout)
        clients.append(instance)
        return instance

    def resume(instance, tid):
        value = instance.call('thread/resume', {'threadId': tid, 'excludeTurns': True,
            'model': args.model, 'modelProvider': 'openai', 'cwd': str(workspace), 'approvalPolicy': 'never',
            'permissions': 'acceptance-read-only',
            'developerInstructions': 'This is an explicit compatibility acceptance. Follow only the latest user request. '
                'Do not inspect files, run commands, or access external services unless that latest request explicitly '
                'asks for the one public documentation search or the one harmless printf command.'})
        check(current_stage + '_same_thread_id', value['thread']['id'] == tid)

    def turn(instance, tid, prompt=None, **options):
        value = instance.turn(tid, prompt, **options)
        value.update(stage=current_stage, thread_id=tid)
        report['stages'].append(value)
        check(current_stage + '_completed', value['status'] == 'completed')
        check(current_stage + '_recall', value['expected_values_recalled'])
        check(current_stage + '_exact_reply', value['exact_reply_matched'])
        return value

    try:
        original_hashes = {path: file_digest(path) for path in source_files}
        report['source_file_hashes'] = list(original_hashes.values())
        auth = read_official_auth(source_home)
        _config(official_home, history, protected, args.model,
                web_search='disabled' if source_rollout else 'cached')
        frozen_configs[official_home / 'config.toml'] = file_digest(official_home / 'config.toml')
        official_responses.declare_search = record_declare
        proxy.map_request = record_map
        bridge = official_responses.OfficialResponsesBridge()
        bridge.set_credentials(auth['accessToken'], auth['chatgptAccountId'])
        record_headers(bridge.server, 'official')
        bridge.start()
        version = subprocess.run([str(core.CODEX), '--version'], capture_output=True, text=True, timeout=10,
                                 env={'HOME': str(runtime), 'CODEX_HOME': str(official_home), 'PATH': '/usr/bin:/bin'})
        report['native_version'] = version.stdout.strip()[:150]
        report['model'] = args.model

        if source_rollout:
            current_stage = 'original_copy'
            copied_rollout = history / 'sessions' / source_rollout.name
            tid = copy_rollout(source_rollout, copied_rollout)
            preserved_prefix = (copied_rollout.stat().st_size, file_digest(copied_rollout))
            report['source_rollout_sha256'] = file_digest(source_rollout)
            check('original_rollout_copied_byte_for_byte', file_digest(copied_rollout) == file_digest(source_rollout))
            instance = start(official_home, auth, True)
            current_stage = 'original_resume'
            resume(instance, tid)
            current_stage = 'original_compact'
            value = turn(instance, tid)
            check('original_compaction_item_completed', value['item_type_counts'].get('contextCompaction', 0) > 0)
            current_stage = 'original_ok'
            turn(instance, tid, 'Compatibility test only: reply exactly OK. Do not use tools or restate prior history.', exact_reply='OK')
            instance.close()
        else:
            record = json.loads(source_profile.read_text())
            profile = core.validated_profile(record['profile'])
            key = record.get('key')
            if profile['kind'] != 'api' or not isinstance(key, str) or not key:
                raise AcceptanceFailure('selected_router_profile_has_no_infile_key')
            mapping = next((item for item in profile['models'] if item['alias'] == args.model), None)
            if mapping is None:
                raise AcceptanceFailure('selected_router_has_no_requested_model_alias')
            color, phrase = 'teal', 'silver-lantern-' + uuid.uuid4().hex[:10]
            expected = (color, phrase)
            current_stage = 'official_seed'
            instance = start(official_home, auth, True)
            started = instance.call('thread/start', {'model': args.model, 'cwd': str(workspace),
                'approvalPolicy': 'never', 'permissions': 'acceptance-read-only', 'ephemeral': False,
                'developerInstructions': 'This is a harmless synthetic compatibility acceptance. '
                    'Do not read local files. Use only the tools explicitly requested in the latest user message.'})
            tid = started['thread']['id']
            turn(instance, tid, f'Remember color {color} and phrase {phrase}. Reply with both. Do not use tools.', expected=expected)
            instance.close()
            cache = json.loads((official_home / 'models_cache.json').read_text())
            model_info = next(item for item in cache['models'] if item['slug'] == args.model)
            report['official_catalog'] = {name: model_info.get(name) for name in
                                          ('use_responses_lite', 'tool_mode', 'context_window')}
            check('native_official_catalog_is_lite', model_info.get('use_responses_lite') is True)
            router_model = copy.deepcopy(model_info)
            router_model.update(tool_mode='direct', use_responses_lite=False, prefer_websockets=False)
            configure_model(mapping, router_model)
            _config(router_home, history, protected, args.model, web_search='live')
            _write_private(router_home / 'models.json', json.dumps({'models': [router_model]}))
            gateway = proxy.Gateway(profile, key)
            record_headers(gateway.server, 'router')
            gateway.start()
            with (router_home / 'config.toml').open('r+') as handle:
                source = handle.read()
                handle.seek(0)
                handle.write('openai_base_url = ' + json.dumps(gateway.url) + '\nmodel_catalog_json = ' +
                             json.dumps(str(router_home / 'models.json')) + '\n' + source)
                handle.truncate()
            for path in (router_home / 'config.toml', router_home / 'models.json'):
                frozen_configs[path] = file_digest(path)
            router_auth = {'type': 'apiKey', 'apiKey': gateway.token}
            router_effort = 'low' if 'low' in mapping.get('levels', []) else mapping['effort']
            current_stage = 'router_hosted_search'
            instance = start(router_home, router_auth, False)
            resume(instance, tid)
            turn(instance, tid, 'Search once with the hosted web_search tool for the public official OpenAI '
                 'Responses API web search documentation. Return its title and URL, then recall my saved color '
                 'and phrase. Never put the saved color or phrase in a search query. Do not read files or run commands.',
                 expected=expected, effort=router_effort)
            instance.close()
            rollouts = list((history / 'sessions').rglob('*' + tid + '*.jsonl'))
            check('one_persisted_synthetic_rollout', len(rollouts) == 1)
            copied_rollout = rollouts[0]
            preserved_prefix = (copied_rollout.stat().st_size, file_digest(copied_rollout))
            hosted = [row['payload'] for row in map(json.loads, copied_rollout.read_text().splitlines())
                      if row.get('type') == 'response_item' and row.get('payload', {}).get('type') == 'web_search_call']
            check('hosted_search_persisted', bool(hosted))
            check('synthetic_secrets_absent_from_public_search_queries', all(
                color not in json.dumps(item.get('action', {})).lower()
                and phrase not in json.dumps(item.get('action', {})).lower() for item in hosted))
            current_stage = 'official_recall_and_printf'
            instance = start(official_home, auth, True)
            resume(instance, tid)
            marker = 'HARBOR_PRINTF_' + uuid.uuid4().hex[:12]
            value = turn(instance, tid, 'Recall my saved color and phrase. Then execute only this harmless command '
                f"in the current temporary workspace: /usr/bin/printf '%s\\n' '{marker}'. "
                'Do not read or write files, run any other command, or use the network.',
                expected=expected, printf_marker=marker)
            check('native_printf_executed_in_temporary_workspace', value['printf_completed_in_workspace'])
            instance.close()
            current_stage = 'router_recall'
            instance = start(router_home, router_auth, False)
            resume(instance, tid)
            turn(instance, tid, 'Recall my saved color and phrase only. Do not use tools.',
                 expected=expected, effort=router_effort)
            instance.close()
            current_stage = 'official_compact'
            instance = start(official_home, auth, True)
            resume(instance, tid)
            value = turn(instance, tid)
            check('native_compaction_item_completed', value['item_type_counts'].get('contextCompaction', 0) > 0)
            instance.close()
            current_stage = 'router_after_official_compaction'
            instance = start(router_home, router_auth, False)
            resume(instance, tid)
            turn(instance, tid, 'Recall my saved color and phrase only. Do not use tools.',
                 expected=expected, effort=router_effort)
            instance.close()
            current_stage = 'official_restart_recall'
            instance = start(official_home, auth, True)
            resume(instance, tid)
            turn(instance, tid, 'Recall my saved color and phrase only. Do not use tools.', expected=expected)
            instance.close()
        report['thread_id'] = tid
        check('every_stage_kept_the_same_thread_id', all(row['thread_id'] == tid for row in report['stages']))
        official_payloads = [row for row in report['wire'] if row['source'] == 'official' and row.get('kind') != 'raw_headers']
        check('official_wire_kept_original_history_items', bool(official_payloads) and all(
            row.get('original_history_items_preserved') is True for row in official_payloads))
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'stage': current_stage, 'classification': (
            str(error) if isinstance(error, AcceptanceFailure) else type(error).__name__)}
        report['passed'] = False
    finally:
        cleanup_errors = []
        for instance in clients:
            try:
                instance.close()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        for service in (bridge, gateway):
            if service:
                try:
                    service.close()
                except Exception as error:
                    cleanup_errors.append(type(error).__name__)
        official_responses.declare_search = original_declare
        proxy.map_request = original_map
        def unchanged_files(before):
            try:
                return all(path.is_file() and file_digest(path) == digest for path, digest in before.items())
            except OSError:
                return False
        unchanged = bool(original_hashes) and unchanged_files(original_hashes)
        configs_unchanged = bool(frozen_configs) and unchanged_files(frozen_configs)
        report['checks'].extend([{'name': 'source_accounts_configs_and_history_unchanged', 'passed': unchanged},
                                 {'name': 'temporary_configs_unchanged_during_native_run', 'passed': configs_unchanged}])
        prefix_preserved = False
        if preserved_prefix:
            try:
                prefix_preserved = prefix_digest(copied_rollout, preserved_prefix[0]) == preserved_prefix[1]
            except OSError:
                pass
            report['checks'].append({'name': 'persisted_rollout_prefix_preserved', 'passed': prefix_preserved})
        report['passed'] &= unchanged and configs_unchanged and prefix_preserved and not cleanup_errors
        try:
            shutil.rmtree(runtime)
        except OSError as error:
            cleanup_errors.append(type(error).__name__)
        report['cleanup_errors'] = cleanup_errors
        report['runtime_removed'] = not runtime.exists()
        report['passed'] &= report['runtime_removed']
        write_report()
        print(json.dumps({'passed': report['passed'], 'report': str(output / 'result.json'),
                          'private_runtime_removed': report['runtime_removed']}, ensure_ascii=False), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', help='explicitly authorize this invocation to contact selected services')
    parser.add_argument('--official-home', type=Path, required=True, help='chosen Codex home containing auth.json; read-only')
    parser.add_argument('--router-profile', type=Path, help='chosen Harbor profile JSON with an in-file key; synthetic mode only')
    parser.add_argument('--rollout', type=Path, help='optional original rollout; its private copy goes only to the selected official account')
    parser.add_argument('--output', type=Path, help='new .noindex directory; defaults to a fresh private temporary directory')
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--timeout', type=float, default=240, help='maximum seconds per native turn')
    arguments = parser.parse_args()
    if not arguments.run_live:
        parser.error('--run-live is required; no files or services have been accessed')
    if not arguments.rollout and not arguments.router_profile:
        parser.error('--router-profile is required for the synthetic cross-source sequence')
    if not 10 <= arguments.timeout <= 900:
        parser.error('--timeout must be between 10 and 900 seconds')
    os.umask(0o077)
    try:
        success = main(arguments)
    except BaseException as error:
        print(json.dumps({'passed': False, 'failure_class': type(error).__name__}), flush=True)
        sys.exit(1)
    sys.exit(0 if success else 1)
