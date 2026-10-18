"""Official replay compatibility: preserve history, credentials and wire semantics."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from official_responses import OfficialResponsesBridge, _Exchange, declare_search, native_arguments, loopback_environment


SEARCH = {'type': 'web_search_call', 'id': 'ws_synthetic', 'status': 'completed',
          'action': {'type': 'search', 'query': 'synthetic', 'queries': ['synthetic']}}
FUNCTION = {'type': 'function', 'name': 'original', 'parameters': {'type': 'object'}}
CACHED = {'type': 'web_search', 'external_web_access': False}


class DeclareSearchTests(unittest.TestCase):
    def patch(self, body, lite=False):
        return json.loads(declare_search(json.dumps(body).encode(), lite=lite))

    def test_unaffected_requests_keep_their_bytes(self):
        for raw in (b'{ "input": "web_search_call" }', b'{"input": []}', b'not json',
                    b'["web_search_call"]', b'{"input":[{"type":"message"}]}'):
            self.assertEqual(declare_search(raw, lite=True), raw)

    def test_normal_replay_preserves_every_history_item_and_original_options(self):
        history = [{'role': 'user', 'content': 'secret stays in memory'}, SEARCH,
                   {'type': 'reasoning', 'encrypted_content': 'synthetic_cipher'},
                   {'type': 'function_call_output', 'call_id': 'call_x', 'output': 'original'},
                   {'type': 'message', 'content': [{'type': 'output_text', 'text': 'answer',
                                                  'annotations': [{'url': 'https://example.invalid'}]}]}]
        body = {'input': history, 'tools': [FUNCTION], 'tool_choice': 'auto',
                'model': 'unchanged', 'reasoning': {'effort': 'high'}, 'counter': 2**62}
        result = self.patch(body)
        self.assertEqual(result['input'], history)
        self.assertEqual(result['tools'], [FUNCTION, CACHED])
        self.assertEqual({k: v for k, v in result.items() if k != 'tools'},
                         {k: v for k, v in body.items() if k != 'tools'})

    def test_lite_uses_additional_tools_and_is_idempotent(self):
        for lite, reasoning in ((True, {}), (False, {'context': 'all_turns'})):
            original = {'input': [SEARCH], 'tools': [], 'reasoning': reasoning}
            result = self.patch(original, lite)
            self.assertEqual(result['input'][1:], [SEARCH])
            self.assertEqual(result['input'][0], {'type': 'additional_tools', 'role': 'developer',
                                                 'tools': [CACHED]})
            self.assertEqual(result['tools'], [])
            self.assertEqual(result['tool_choice'], 'none')
            raw = json.dumps(result, indent=2).encode()
            self.assertEqual(declare_search(raw, lite=lite), raw)

    def test_existing_additional_tools_and_choices_survive(self):
        for choice in ('auto', 'none', {'type': 'function', 'name': 'original'},
                       {'type': 'allowed_tools', 'mode': 'auto', 'tools': [{'type': 'function', 'name': 'original'}]}):
            extra = {'type': 'additional_tools', 'id': 'at_original', 'role': 'developer', 'tools': [FUNCTION]}
            body = {'input': [extra, SEARCH], 'tool_choice': choice}
            result = self.patch(body)
            self.assertEqual(result['input'][0], dict(extra, tools=[FUNCTION, CACHED]))
            self.assertEqual(result['input'][1:], [SEARCH])
            self.assertEqual(result['tool_choice'], choice)

    def test_existing_search_is_not_replaced_or_downgraded(self):
        for name in ('web_search', 'web_search_preview', 'web_search_preview_2025_03_11'):
            tool = {'type': name, 'external_web_access': True}
            for body in ({'input': [SEARCH], 'tools': [tool]},
                         {'input': [SEARCH, {'type': 'additional_tools', 'tools': [tool]}]}):
                raw = json.dumps(body, indent=2).encode()
                self.assertEqual(declare_search(raw, lite=True), raw)

    def test_malformed_tool_lists_are_left_for_native_validation(self):
        for body in ({'input': [SEARCH], 'tools': 'invalid'},
                     {'input': [SEARCH], 'tools': False},
                     {'input': [SEARCH], 'tools': ''},
                     {'input': [SEARCH, {'type': 'additional_tools', 'tools': 'invalid'}]}):
            raw = json.dumps(body).encode()
            self.assertEqual(declare_search(raw, lite=True), raw)

    def test_inconsistent_forced_choice_does_not_become_a_forced_search(self):
        for choice in ('required', {'type': 'function', 'name': 'missing'}, 'future-choice'):
            raw = json.dumps({'input': [SEARCH], 'tools': [], 'tool_choice': choice}).encode()
            self.assertEqual(declare_search(raw, lite=True), raw)


class NativeTransportArguments(unittest.TestCase):
    def test_terminator_is_rejected_before_transport_setup(self):
        from backend_guard import is_stdio_app_server
        for args in (['--', 'app-server'], ['app-server', '--'],
                     ['app-server', '--', '-c', 'model="unchanged"']):
            self.assertFalse(is_stdio_app_server(args))
            with self.assertRaisesRegex(ValueError, 'argument terminator'):
                native_arguments(args, 'http://127.0.0.1:1234/synthetic')

    def test_only_process_routing_and_compression_are_overridden(self):
        url = 'http://127.0.0.1:1234/synthetic/backend-api/codex'
        for args in (['-c', 'openai_base_url="https://example.invalid"', 'app-server'],
                     ['app-server', '--config=openai_base_url="https://example.invalid"'],
                     ['-copenai_base_url="https://example.invalid"', 'app-server'],
                     ['-c=openai_base_url="https://example.invalid"', 'app-server']):
            original = list(args)
            result = native_arguments(args, url)
            self.assertEqual(args, original)
            self.assertNotIn('example.invalid', ' '.join(result))
            self.assertIn('app-server', result)
            self.assertIn('features.enable_request_compression=false', result)
        unchanged = ['--config', 'desktop.show_max_reasoning=true', 'app-server', '--stdio']
        result = native_arguments(unchanged, url)
        self.assertEqual(result[:3], ['--disable', 'enable_request_compression', 'app-server'])
        self.assertEqual(result[result.index('--config') + 1], 'desktop.show_max_reasoning=true')
        self.assertIn('--stdio', result)
        self.assertFalse(any('web_search=' in item or 'model_catalog' in item or 'chatgpt_base_url' in item
                             for item in result))

    def test_proxy_bypass_is_child_only_and_preserves_existing_targets(self):
        env = {'HTTP_PROXY': 'http://sentinel.invalid:1234', 'NO_PROXY': 'example.org',
               'no_proxy': 'example.net', 'HOME': '/synthetic'}
        result = loopback_environment(env)
        self.assertEqual(env['NO_PROXY'], 'example.org')
        self.assertEqual(result['HTTP_PROXY'], env['HTTP_PROXY'])
        for key in ('NO_PROXY', 'no_proxy'):
            self.assertEqual(set(result[key].split(',')), {'example.org', 'example.net', '127.0.0.1', 'localhost'})


class OfficialBridgeTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.status = 200
        self.payload = b'data: {"type":"response.completed"}\n\n'
        self.started = threading.Event()
        self.release = threading.Event()
        self.disconnected = threading.Event()
        self.stall = False
        owner = self

        class Endpoint(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                owner.records.append((self.path, dict(self.headers), raw))
                self.send_response(owner.status)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('x-codex-turn-state', 'synthetic-state')
                self.send_header('x-ratelimit-remaining-requests', '19')
                self.send_header('Connection', 'close, X-Private-Hop')
                self.send_header('X-Private-Hop', 'must-not-forward')
                if owner.status == 307:
                    self.send_header('Location', owner.base + '/credential-trap')
                self.end_headers()
                self.wfile.write(owner.payload); self.wfile.flush()
                owner.started.set()
                if owner.stall:
                    self.connection.settimeout(5)
                    try:
                        if self.connection.recv(1) == b'': owner.disconnected.set()
                    except OSError:
                        pass
                self.close_connection = True

        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        self.upstream.daemon_threads = True
        self.base = f'http://127.0.0.1:{self.upstream.server_port}'
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.bridge = OfficialResponsesBridge(upstream_base=self.base + '/backend-api/codex')
        self.bridge.set_credentials('SYNTHETIC_OFFICIAL_TOKEN', 'synthetic-account')
        self.bridge.start()

    def tearDown(self):
        self.release.set()
        self.bridge.close()
        self.upstream.shutdown(); self.upstream.server_close()

    def request(self, body=None, path='/responses', headers=None, method=None):
        hdr = {'Authorization': 'Bearer SYNTHETIC_OFFICIAL_TOKEN',
               'ChatGPT-Account-Id': 'synthetic-account', 'Content-Type': 'application/json',
               'x-openai-internal-codex-responses-lite': 'true', 'x-codex-turn-state': 'original-state',
               'Connection': 'X-Remove-Me', 'X-Remove-Me': 'hop'}
        hdr.update(headers or {})
        data = json.dumps(body or {'input': [SEARCH]}).encode() if method != 'GET' else None
        return urllib.request.urlopen(urllib.request.Request(self.bridge.url + path, data=data,
                                                            headers=hdr, method=method), timeout=4)

    def test_preserves_auth_headers_response_headers_and_native_lite(self):
        # urllib replaces Connection with close before sending. Use http.client
        # here so the bridge actually receives the nominated hop header.
        parsed = urllib.parse.urlsplit(self.bridge.url)
        connection = http.client.HTTPConnection(parsed.netloc, timeout=4)
        connection.request('POST', parsed.path + '/responses', json.dumps({'input': [SEARCH]}), headers={
            'Authorization': 'Bearer SYNTHETIC_OFFICIAL_TOKEN', 'ChatGPT-Account-Id': 'synthetic-account',
            'x-codex-turn-state': 'original-state', 'x-openai-internal-codex-responses-lite': 'true',
            'Content-Type': 'application/json', 'Connection': 'X-Remove-Me', 'X-Remove-Me': 'hop'})
        with connection.getresponse() as response:
            self.assertEqual(response.read(), self.payload)
            self.assertEqual(response.headers['x-codex-turn-state'], 'synthetic-state')
            self.assertEqual(response.headers['x-ratelimit-remaining-requests'], '19')
            self.assertIsNone(response.headers.get('X-Private-Hop'))
        connection.close()
        path, headers, raw = self.records[0]
        h = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(path, '/backend-api/codex/responses')
        self.assertEqual(h['authorization'], 'Bearer SYNTHETIC_OFFICIAL_TOKEN')
        self.assertEqual(h['chatgpt-account-id'], 'synthetic-account')
        self.assertEqual(h['x-codex-turn-state'], 'original-state')
        self.assertEqual(h['x-openai-internal-codex-responses-lite'], 'true')
        self.assertNotIn('x-remove-me', h)
        self.assertEqual(json.loads(raw)['input'][1:], [SEARCH])

    def test_native_http_errors_are_not_erased(self):
        for status in (400, 401, 403, 429):
            self.status = status
            self.payload = b'{"error":{"code":"context_length_exceeded","message":"synthetic"}}'
            with self.assertRaises(urllib.error.HTTPError) as caught: self.request()
            with caught.exception as error:
                self.assertEqual(error.code, status)
                self.assertEqual(error.read(), self.payload)
                self.assertEqual(error.headers['x-codex-turn-state'], 'synthetic-state')

    def test_redirect_and_paths_cannot_forward_credentials_elsewhere(self):
        self.status = 307
        with self.assertRaises(urllib.error.HTTPError) as caught: self.request()
        with caught.exception as error:
            self.assertEqual(error.code, 502)
            self.assertIsNone(error.headers.get('Location'))
        self.assertEqual(len(self.records), 1)
        for path in ('/../responses', '/%2e%2e/responses', '//elsewhere.invalid/responses'):
            with self.assertRaises(urllib.error.HTTPError) as caught: self.request(path=path)
            caught.exception.close()
        self.assertEqual(len(self.records), 1)

    def test_wrong_capability_and_unsupported_encoding_never_reach_upstream(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(headers={'Authorization': 'Bearer WRONG_TOKEN'})
        caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(headers={'ChatGPT-Account-Id': 'other-account'})
        caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(headers={'Content-Encoding': 'zstd'})
        with caught.exception as error: self.assertEqual(error.code, 415)
        self.assertEqual(self.records, [])

    def test_refresh_uses_new_token_without_rewriting_request_credentials(self):
        self.bridge.set_credentials('SYNTHETIC_REFRESHED_ACCESS', 'synthetic-account')
        with self.request(headers={'Authorization': 'Bearer SYNTHETIC_REFRESHED_ACCESS'}) as response:
            response.read()
        self.assertEqual(self.records[0][1]['Authorization'], 'Bearer SYNTHETIC_REFRESHED_ACCESS')
        self.assertNotIn('SYNTHETIC', self.bridge.url)
        with self.assertRaises(ValueError): self.bridge.set_credentials('OTHER', 'other-account')

    def test_rotation_accepts_only_current_and_previous_token(self):
        self.bridge.set_credentials('SECOND', 'synthetic-account')
        with self.request() as response: response.read()
        self.bridge.set_credentials('THIRD', 'synthetic-account')
        with self.request(headers={'Authorization': 'Bearer SECOND'}) as response: response.read()
        with self.assertRaises(urllib.error.HTTPError) as caught: self.request()
        with caught.exception as error: self.assertEqual(error.code, 401)

    def test_logout_revokes_connection_and_cancels_active_stream(self):
        self.stall = True
        response = self.request()
        self.assertTrue(response.readline())
        self.bridge.revoke()
        self.bridge.revoke()
        response.close()
        self.assertTrue(self.disconnected.wait(2))
        with self.assertRaises(urllib.error.HTTPError) as caught: self.request()
        with caught.exception as error: self.assertEqual(error.code, 401)
        with self.assertRaises(ValueError): self.bridge.set_credentials('LATE', 'synthetic-account')

    def test_https_proxy_is_contacted_using_connect_without_origin_credentials(self):
        requests = []
        def connect(handler):
            requests.append((handler.path, dict(handler.headers)))
            handler.send_response(502)
            handler.send_header('Content-Length', '0')
            handler.send_header('Connection', 'close')
            handler.end_headers(); handler.close_connection = True
        self.upstream.RequestHandlerClass.do_CONNECT = connect
        self.bridge.upstream_base = 'https://synthetic-origin.invalid/backend-api/codex'
        parsed = urllib.parse.urlsplit(self.bridge.url)
        env = dict.fromkeys(('http_proxy', 'HTTP_PROXY', 'all_proxy', 'ALL_PROXY', 'no_proxy', 'NO_PROXY'), '')
        env.update(https_proxy=self.base, HTTPS_PROXY=self.base)
        with patch.dict(os.environ, env):
            connection = http.client.HTTPConnection(parsed.netloc, timeout=3)
            try:
                connection.request('POST', parsed.path + '/responses', b'{"input":[]}', headers={
                    'Authorization': 'Bearer SYNTHETIC_OFFICIAL_TOKEN', 'ChatGPT-Account-Id': 'synthetic-account'})
                with connection.getresponse() as response:
                    self.assertEqual(response.status, 502); response.read()
            finally: connection.close()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], 'synthetic-origin.invalid:443')
        self.assertNotIn('SYNTHETIC_OFFICIAL_TOKEN', json.dumps(requests))
        self.assertEqual(self.records, [])

    def test_local_upload_timeout_is_distinct_from_upstream_failure(self):
        handler = self.bridge.server.RequestHandlerClass
        original_setup = handler.setup
        def short_timeout(instance):
            original_setup(instance); instance.connection.settimeout(.08)
        handler.setup = short_timeout
        parsed = urllib.parse.urlsplit(self.bridge.url)
        connection = http.client.HTTPConnection(parsed.netloc, timeout=3)
        try:
            connection.putrequest('POST', parsed.path + '/responses')
            connection.putheader('Authorization', 'Bearer SYNTHETIC_OFFICIAL_TOKEN')
            connection.putheader('ChatGPT-Account-Id', 'synthetic-account')
            connection.putheader('Content-Length', '100')
            connection.endheaders(b'{')
            with connection.getresponse() as response:
                self.assertEqual(response.status, 408); response.read()
        finally: connection.close()
        self.assertEqual(self.records, [])

    def test_local_limits_and_framing_are_distinguishable_from_upstream_errors(self):
        parsed = urllib.parse.urlsplit(self.bridge.url)
        for headers, status in (({'Content-Length': str(64 * 1024 * 1024 + 1)}, 413),
                                ({'Transfer-Encoding': 'chunked'}, 411)):
            connection = http.client.HTTPConnection(parsed.netloc, timeout=3)
            connection.putrequest('POST', parsed.path + '/responses')
            connection.putheader('Authorization', 'Bearer SYNTHETIC_OFFICIAL_TOKEN')
            connection.putheader('ChatGPT-Account-Id', 'synthetic-account')
            for key, value in headers.items(): connection.putheader(key, value)
            connection.endheaders()
            with connection.getresponse() as response:
                self.assertEqual(response.status, status)
                self.assertEqual(json.loads(response.read())['error']['type'], 'harbor_official_bridge_error')
            connection.close()
        self.assertEqual(self.records, [])

    def test_bounded_concurrency_returns_local_unavailable_not_account_quota(self):
        acquired = 0
        try:
            while self.bridge.limit.acquire(blocking=False): acquired += 1
            self.assertEqual(acquired, 32)
            with self.assertRaises(urllib.error.HTTPError) as caught: self.request()
            with caught.exception as error: self.assertEqual(error.code, 503)
        finally:
            for _ in range(acquired): self.bridge.limit.release()
        with self.request() as response: self.assertEqual(response.read(), self.payload)
        self.assertEqual(len(self.records), 1)

    def test_large_rejected_upload_retains_the_explicit_http_error(self):
        acquired = 0
        parsed = urllib.parse.urlsplit(self.bridge.url)
        connection = http.client.HTTPConnection(parsed.netloc, timeout=4)
        try:
            while self.bridge.limit.acquire(blocking=False): acquired += 1
            connection.request('POST', parsed.path + '/responses', b'x' * (4 * 1024 * 1024), headers={
                'Authorization': 'Bearer SYNTHETIC_OFFICIAL_TOKEN', 'ChatGPT-Account-Id': 'synthetic-account'})
            with connection.getresponse() as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(json.loads(response.read())['error']['type'], 'harbor_official_bridge_error')
        finally:
            connection.close()
            for _ in range(acquired): self.bridge.limit.release()
        self.assertEqual(self.records, [])

    def test_websocket_upgrade_selects_immediate_native_http_fallback(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(method='GET', headers={'Upgrade': 'websocket', 'Connection': 'Upgrade'})
        with caught.exception as error: self.assertEqual(error.code, 426)
        self.assertEqual(self.records, [])

    def test_stream_is_forwarded_before_upstream_finishes_and_cancel_closes_socket(self):
        self.stall = True
        with self.request() as response:
            self.assertEqual(response.readline(), self.payload.splitlines(keepends=True)[0])
        self.assertTrue(self.disconnected.wait(3), 'client cancellation did not close the upstream socket')

    def test_close_interrupts_active_stream_and_is_idempotent(self):
        self.stall = True
        response = self.request()
        self.assertEqual(response.readline(), self.payload.splitlines(keepends=True)[0])
        began = time.monotonic()
        self.bridge.close(); self.bridge.close()
        response.close()
        self.assertLess(time.monotonic() - began, 3)
        self.assertTrue(self.disconnected.wait(2))

    def test_cancellation_with_a_descriptor_above_select_limit(self):
        import fcntl
        import resource
        if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1050:
            self.skipTest('OS descriptor limit is below the boundary; it is not changed by this test')
        left, right = socket.socketpair()
        high = None
        try:
            high = socket.socket(fileno=fcntl.fcntl(left.fileno(), fcntl.F_DUPFD, 1050))
            left.close()
            exchange = _Exchange(high)
            worker = threading.Thread(target=exchange.watch, daemon=True)
            worker.start(); right.close()
            self.assertTrue(exchange.done.wait(1), 'high-descriptor cancellation stopped working')
            worker.join(timeout=1)
        finally:
            left.close(); right.close()
            if high: high.close()


if __name__ == '__main__':
    unittest.main()
