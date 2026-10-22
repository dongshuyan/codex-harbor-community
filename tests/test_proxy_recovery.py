"""Localhost-only transport recovery contracts; no credentials or real API calls."""
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import queue
import socket
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from official_responses import OfficialResponsesBridge
from proxy import Gateway
import proxy
from test_core import api


PARTIAL = b'data: {"type":"response.output_text.delta","delta":"synthetic"}\n\n'
COMPLETE = b'data: {"type":"response.completed","response":{"id":"synthetic","output":[]}}\n\n'


@dataclass
class Action:
    kind: str
    started: threading.Event = field(default_factory=threading.Event)
    disconnected: threading.Event = field(default_factory=threading.Event)


class RecoveryFixture:
    """A real HTTP endpoint with explicitly queued, bounded socket failures."""

    def setUp(self):
        super().setUp()
        # Limit both hops to loopback even on a machine with ambient proxies.
        environment = dict.fromkeys(('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY',
                                     'https_proxy', 'ALL_PROXY', 'all_proxy'), '')
        environment.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
        proxy_environment = patch.dict(os.environ, environment)
        proxy_environment.start()
        self.addCleanup(proxy_environment.stop)
        self.actions = queue.Queue()
        self.accepted = []
        self.upstream_sockets = []
        self.clients = []
        self.responses = []
        self.events = []
        self.recorded = threading.Condition()
        self.handler_errors = []
        owner = self

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                action = owner.actions.get_nowait()
                owner.accepted.append(action.kind)
                owner.upstream_sockets.append(self.connection)
                self.close_connection = True
                action.started.set()
                if action.kind == 'disconnect_before_headers':
                    return
                if action.kind == 'stall_before_headers':
                    self.wait_for_disconnect(action)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                if action.kind == 'truncated_content_length':
                    self.send_header('Content-Length', str(len(PARTIAL) + len(COMPLETE)))
                if action.kind == 'complete_with_length':
                    self.send_header('Content-Length', str(len(COMPLETE)))
                if action.kind in ('truncated_chunked', 'complete_chunked'):
                    self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                if action.kind == 'truncated_chunked':
                    # A complete first chunk, followed by EOF before the required
                    # terminating chunk. No upstream application completion exists.
                    self.wfile.write(f'{len(PARTIAL):x}\r\n'.encode() + PARTIAL + b'\r\n')
                elif action.kind == 'complete_chunked':
                    self.wfile.write(f'{len(COMPLETE):x}\r\n'.encode() + COMPLETE + b'\r\n0\r\n\r\n')
                else:
                    self.wfile.write(COMPLETE if action.kind.startswith('complete') else PARTIAL)
                self.wfile.flush()
                if action.kind == 'stall_stream':
                    self.wait_for_disconnect(action)

            def wait_for_disconnect(self, action):
                self.connection.settimeout(10)
                try:
                    if self.connection.recv(1) == b'':
                        action.disconnected.set()
                except (ConnectionResetError, BrokenPipeError):
                    action.disconnected.set()
                except OSError:
                    pass

        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream_thread = threading.Thread(
            target=lambda: self.upstream.serve_forever(poll_interval=.02), daemon=True)
        self.upstream_thread.start()
        self.base = f'http://127.0.0.1:{self.upstream.server_port}'
        self.create_transport()

        def capture_handler_error(*_):
            self.handler_errors.append(sys.exc_info()[0].__name__)

        self.transport.server.handle_error = capture_handler_error
        self.transport.start()

    def tearDown(self):
        # Release synthetic stalls even when an assertion fails. Production
        # gateways, account state and settings are never involved.
        for response in self.responses:
            response.close()
        for client in self.clients:
            client.close()
        for connection in self.upstream_sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.transport.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        super().tearDown()

    def enqueue(self, kind):
        action = Action(kind)
        self.actions.put(action)
        return action

    def record(self, event):
        with self.recorded:
            self.events.append(event)
            self.recorded.notify_all()

    def send(self):
        parsed = urllib.parse.urlsplit(self.transport.url)
        client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        self.clients.append(client)
        client.request('POST', parsed.path + '/responses', json.dumps({
            'model': 'gpt-6-astra', 'input': 'SYNTHETIC_RECOVERY_INPUT', 'stream': True,
        }).encode(), self.headers)
        return client

    def response(self, client):
        response = client.getresponse()
        self.responses.append(response)
        return response

    def test_preheader_disconnect_is_not_replayed_and_fresh_request_recovers(self):
        self.enqueue('disconnect_before_headers')
        response = self.response(self.send())
        self.assertEqual(response.status, 502)
        payload = json.loads(response.read())
        self.assertIn(self.disconnect_message, payload['error']['message'])
        self.assertEqual(self.accepted, ['disconnect_before_headers'])

        self.enqueue('complete')
        recovered = self.response(self.send())
        self.assertEqual(recovered.status, 200)
        self.assertEqual(recovered.read(), COMPLETE)
        self.assertEqual(self.accepted, ['disconnect_before_headers', 'complete'])

    def test_client_cancel_before_headers_closes_upstream(self):
        action = self.enqueue('stall_before_headers')
        client = self.send()
        self.assertTrue(action.started.wait(2), 'synthetic upstream was not reached')
        client.close()
        self.assertTrue(action.disconnected.wait(2),
                        'cancelled client left the upstream request alive before headers')
        self.assertEqual(self.accepted, ['stall_before_headers'])

    def test_client_cancel_during_stalled_stream_closes_upstream(self):
        action = self.enqueue('stall_stream')
        client = self.send()
        response = self.response(client)
        self.assertEqual(response.read(len(PARTIAL)), PARTIAL)
        response.close()
        client.close()
        self.assertTrue(action.disconnected.wait(2),
                        'cancelled client left the stalled upstream response alive')
        self.assertEqual(self.accepted, ['stall_stream'])

    def test_truncated_content_length_remains_observable_to_client(self):
        self.enqueue('truncated_content_length')
        response = self.response(self.send())
        self.assertEqual(response.status, 200)
        with self.assertRaises(http.client.IncompleteRead) as caught:
            response.read()
        self.assertEqual(caught.exception.partial, PARTIAL)
        self.assertEqual(self.accepted, ['truncated_content_length'])


class RouterRecoveryTests(RecoveryFixture, unittest.TestCase):
    disconnect_message = 'Harbor 转发层未重试'

    def create_transport(self):
        profile = api()
        profile.update(url=self.base + '/v1', id='a' * 32)
        self.transport = Gateway(profile, 'SYNTHETIC_UPSTREAM_KEY', self.record)
        self.headers = {'Authorization': 'Bearer ' + self.transport.token,
                        'Content-Type': 'application/json'}

    def test_non_ascii_authorization_is_rejected_without_handler_error(self):
        parsed = urllib.parse.urlsplit(self.transport.url)
        client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        self.clients.append(client)
        client.request('GET', '/v1/models', headers={'Authorization': 'Bearer caf\u00e9'})
        response = self.response(client)
        self.assertEqual(response.status, 401)
        response.read()
        self.assertEqual(self.accepted, [])
        self.assertEqual(self.handler_errors, [])

    def test_invalid_model_payload_is_not_recorded(self):
        parsed = urllib.parse.urlsplit(self.transport.url)
        client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        self.clients.append(client)
        client.request('POST', '/v1/responses', json.dumps({
            'model': {'unexpected': 'SYNTHETIC_PRIVATE_PAYLOAD'}, 'input': 'unused',
        }).encode(), self.headers)
        response = self.response(client)
        self.assertEqual(response.status, 422)
        self.assertNotIn(b'SYNTHETIC_PRIVATE_PAYLOAD', response.read())
        with self.recorded:
            self.assertTrue(self.recorded.wait_for(lambda: bool(self.events), timeout=2))
        self.assertIsNone(self.events[-1]['alias'])
        self.assertIsNone(self.events[-1]['upstream_model'])
        self.assertNotIn('SYNTHETIC_PRIVATE_PAYLOAD', json.dumps(self.events))
        self.assertEqual(self.accepted, [])

    def test_local_upload_timeout_is_not_reported_as_upstream_failure(self):
        handler = self.transport.server.RequestHandlerClass
        original_setup = handler.setup
        def short_timeout(instance):
            original_setup(instance)
            instance.connection.settimeout(.15)
        parsed = urllib.parse.urlsplit(self.transport.url)
        client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        self.clients.append(client)
        with patch.object(handler, 'setup', short_timeout):
            client.request('POST', '/v1/responses', b'{',
                           {**self.headers, 'Content-Length': '100'})
            response = self.response(client)
            self.assertEqual(response.status, 408)
            response.read()
        with self.recorded:
            self.assertTrue(self.recorded.wait_for(lambda: bool(self.events), timeout=2))
        self.assertEqual(self.events[-1]['error_kind'], 'client_timeout')
        self.assertEqual(self.accepted, [])
        self.enqueue('complete')
        self.assertEqual(self.response(self.send()).read(), COMPLETE)

    def test_cancelled_requests_release_all_slots_for_a_fresh_request(self):
        clients = []
        for _ in range(8):
            action = self.enqueue('stall_before_headers')
            clients.append(self.send())
            self.assertTrue(action.started.wait(2), 'synthetic upstream was not reached')
        for client in clients:
            client.close()
        with self.recorded:
            self.recorded.wait_for(lambda: len(self.events) == 8, timeout=2)

        self.enqueue('complete')
        response = self.response(self.send())
        self.assertEqual(response.status, 200,
                         'cancelled requests still occupied the Router concurrency slots')
        self.assertEqual(response.read(), COMPLETE)
        self.assertEqual(self.accepted, ['stall_before_headers'] * 8 + ['complete'])

    def test_broken_chunk_framing_is_handled_without_server_exception(self):
        self.enqueue('truncated_chunked')
        response = self.response(self.send())
        with self.assertRaises(http.client.IncompleteRead) as caught:
            response.read()
        self.assertEqual(caught.exception.partial, PARTIAL)
        with self.recorded:
            self.assertTrue(self.recorded.wait_for(lambda: bool(self.events), timeout=2))
        self.assertEqual(self.handler_errors, [],
                         'upstream truncation escaped the gateway transport error handler')
        self.assertEqual(self.accepted, ['truncated_chunked'])
        self.assertEqual(self.events[-1]['outcome'], 'failed')
        self.assertEqual(self.events[-1]['error_kind'], 'incomplete_response')

    def test_close_cancels_active_upstream_and_is_idempotent(self):
        action = self.enqueue('stall_before_headers')
        client = self.send()
        self.assertTrue(action.started.wait(2))
        self.transport.close()
        self.transport.close()
        self.assertTrue(action.disconnected.wait(2))
        with self.recorded:
            self.assertTrue(self.recorded.wait_for(lambda: bool(self.events), timeout=2))
        self.assertEqual(self.events[-1]['outcome'], 'cancelled')
        client.close()

    def test_timeout_is_bounded_and_next_request_recovers_without_replay(self):
        with patch.object(proxy, 'READ_TIMEOUT', .3, create=True):
            self.enqueue('stall_before_headers')
            response = self.response(self.send())
            self.assertEqual(response.status, 502)
            response.read()
        with self.recorded:
            self.assertTrue(self.recorded.wait_for(lambda: bool(self.events), timeout=2))
        self.assertEqual(self.events[-1]['outcome'], 'failed')
        self.assertEqual(self.events[-1]['error_kind'], 'timeout')
        self.assertEqual(self.accepted, ['stall_before_headers'])
        self.enqueue('complete')
        response = self.response(self.send())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), COMPLETE)
        self.assertEqual(self.accepted, ['stall_before_headers', 'complete'])

    def test_complete_framing_and_metadata_do_not_expose_request_content(self):
        for kind in ('complete_with_length', 'complete_chunked'):
            self.enqueue(kind)
            response = self.response(self.send())
            self.assertEqual(response.version, 11)
            self.assertEqual(response.getheader('Connection'), 'close')
            self.assertEqual(response.read(), COMPLETE)
            if kind == 'complete_with_length':
                self.assertEqual(response.getheader('Content-Length'), str(len(COMPLETE)))
                self.assertIsNone(response.getheader('Transfer-Encoding'))
            else:
                self.assertEqual(response.getheader('Transfer-Encoding'), 'chunked')
                self.assertIsNone(response.getheader('Content-Length'))
        with self.recorded:
            self.assertTrue(self.recorded.wait_for(lambda: len(self.events) == 2, timeout=2))
        for event in self.events:
            self.assertEqual(set(event), {'time', 'profile', 'alias', 'upstream_model',
                                         'status', 'outcome', 'error_kind'})
            self.assertEqual((event['status'], event['outcome'], event['error_kind']),
                             (200, 'complete', None))
            self.assertEqual(event['alias'], 'gpt-6-astra')
            self.assertEqual(event['upstream_model'], 'infi/glm-5.3')
        serialized = json.dumps(self.events)
        for forbidden in ('SYNTHETIC_UPSTREAM_KEY', 'SYNTHETIC_RECOVERY_INPUT', self.base):
            self.assertNotIn(forbidden, serialized)

    def test_router_does_not_inherit_ambient_proxy(self):
        reached = []
        class Trap(BaseHTTPRequestHandler):
            def do_POST(self):
                reached.append(True)
                self.send_error(502)
            def log_message(self, *args):
                pass
        trap = ThreadingHTTPServer(('127.0.0.1', 0), Trap)
        threading.Thread(target=lambda: trap.serve_forever(poll_interval=.02), daemon=True).start()
        try:
            env = dict.fromkeys(('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy'),
                                f'http://127.0.0.1:{trap.server_port}')
            env.update(NO_PROXY='', no_proxy='')
            with patch.dict(os.environ, env):
                self.enqueue('complete')
                response = self.response(self.send())
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), COMPLETE)
            self.assertEqual(reached, [])
        finally:
            trap.shutdown(); trap.server_close()


class OfficialRecoveryControls(RecoveryFixture, unittest.TestCase):
    disconnect_message = 'Official connection closed; Harbor did not retry the request.'

    def create_transport(self):
        self.transport = OfficialResponsesBridge(upstream_base=self.base + '/backend-api/codex')
        self.transport.set_credentials('SYNTHETIC_OFFICIAL_TOKEN', 'synthetic-account')
        self.headers = {'Authorization': 'Bearer SYNTHETIC_OFFICIAL_TOKEN',
                        'ChatGPT-Account-Id': 'synthetic-account',
                        'Content-Type': 'application/json'}


if __name__ == '__main__':
    unittest.main()
