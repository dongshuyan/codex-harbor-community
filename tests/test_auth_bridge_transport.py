"""Exercise the bridge with real anonymous pipes and a synthetic native child."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import auth_bridge


NATIVE = r'''
import json
import sys

mode = sys.argv[1]
deferred = None

def reply(value, newline=True):
    sys.stdout.buffer.write(json.dumps(value).encode() + (b'\n' if newline else b''))
    sys.stdout.buffer.flush()

for raw in sys.stdin.buffer:
    value = json.loads(raw)
    if 'id' not in value:
        continue
    method = value.get('method')
    response = {'id': value['id'], 'result': {'ok': True}}
    if method == 'account/login/start':
        if mode == 'login-error':
            response = {'id': value['id'], 'error': {'code': -32000,
                'message': 'SYNTHETIC_NATIVE_PRIVATE_DETAIL'}}
        else:
            response['result'] = {'type': 'chatgptAuthTokens'}
    if method == 'reply-at-eof':
        deferred = response
        continue
    reply(response, newline=mode != 'unterminated-tail' or method != 'final-request')
    if method == 'final-request':
        break
else:
    if deferred is not None:
        reply(deferred)
'''


class ExitScheduledProcess:
    """Give the real child time to exit before a caller's next poll check.

    The first check can precede the write to native stdin, so it is bounded.
    Once that write completes, wait() deterministically exposes the final-response
    race in a transport that equates process exit with an empty stdout pipe.
    """
    def __init__(self, proc, final_queued):
        self.proc = proc
        self.final_queued = final_queued
        self.stdin = proc.stdin
        self.stdout = proc.stdout

    def poll(self):
        if self.final_queued.is_set():
            try:
                return self.proc.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                return None
        return self.proc.poll()

    def terminate(self):
        self.proc.terminate()


class PipeSession:
    def __init__(self, case, *, schedule_exit=False):
        self.temporary = tempfile.TemporaryDirectory(prefix='harbor-bridge-pipes-', dir='/private/tmp')
        root = Path(self.temporary.name)
        script = root / 'native.py'
        script.write_text(NATIVE)
        self.proc = subprocess.Popen(
            [sys.executable, '-I', '-B', str(script), case],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=root, env={'HOME': str(root), 'TMPDIR': str(root), 'PATH': '/usr/bin:/bin'},
        )
        self.input_read, self.input_write = os.pipe()
        self.output_read, output_write = os.pipe()
        self.pending = bytearray()
        self.finished = threading.Event()
        self.final_queued = threading.Event()
        self.outcome = {}
        proc = ExitScheduledProcess(self.proc, self.final_queued) if schedule_exit else self.proc

        def rewrite(raw):
            if json.loads(raw).get('method') == 'final-request':
                self.final_queued.set()
            return raw

        def run():
            try:
                self.outcome['result'] = auth_bridge.run(
                    proc, lambda **kwargs: {
                        'type': 'chatgptAuthTokens', 'accessToken': 'SYNTHETIC_ACCESS',
                        'chatgptAccountId': 'synthetic-account', 'chatgptPlanType': 'pro',
                    }, input_fd=self.input_read, output_fd=output_write, rewrite=rewrite,
                )
            except BaseException as error:
                self.outcome['error'] = error
            finally:
                os.close(output_write)
                self.finished.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close_input()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.wait(timeout=2)
        self.thread.join(timeout=2)
        for fd in (self.input_read, self.output_read):
            os.close(fd)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.temporary.cleanup()

    def send(self, method, ident):
        os.write(self.input_write, auth_bridge.encode({'id': ident, 'method': method, 'params': {}}))

    def close_input(self):
        if self.input_write is not None:
            os.close(self.input_write)
            self.input_write = None

    def receive(self):
        deadline = time.monotonic() + 4
        while b'\n' not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.output_read], [], [], max(0, remaining))[0]:
                raise AssertionError('Bridge did not deliver a response or close its output pipe')
            raw = os.read(self.output_read, 65536)
            if not raw:
                if not self.pending:
                    return None
                raw = bytes(self.pending)
                self.pending.clear()
                return json.loads(raw)
            self.pending.extend(raw)
        raw, rest = self.pending.split(b'\n', 1)
        self.pending = bytearray(rest)
        return json.loads(raw)

    def result(self):
        if not self.finished.wait(timeout=2):
            raise AssertionError('Bridge did not stop after native stdout closed')
        if 'error' in self.outcome:
            raise self.outcome['error']
        return self.outcome['result']


class AuthBridgeTransportTests(unittest.TestCase):
    def initialize(self, session):
        session.send('initialize', 1)
        self.assertEqual(session.receive(), {'id': 1, 'result': {'ok': True}})

    def test_final_response_is_drained_after_native_exit_with_client_input_open(self):
        for case in ('normal', 'unterminated-tail'):
            with self.subTest(case=case), PipeSession(case, schedule_exit=True) as session:
                self.initialize(session)
                session.send('final-request', 2)
                self.assertEqual(session.receive(), {'id': 2, 'result': {'ok': True}})
                self.assertIsNone(session.receive())
                self.assertTrue(session.result())
                self.assertEqual(session.proc.wait(timeout=2), 0)

    def test_client_eof_flushes_request_then_drains_native_response(self):
        with PipeSession('normal') as session:
            self.initialize(session)
            session.send('reply-at-eof', 2)
            session.close_input()
            self.assertEqual(session.receive(), {'id': 2, 'result': {'ok': True}})
            self.assertIsNone(session.receive())
            self.assertTrue(session.result())
            self.assertEqual(session.proc.wait(timeout=2), 0)

    def test_native_termination_does_not_wait_for_idle_client_input(self):
        with PipeSession('normal') as session:
            self.initialize(session)
            session.proc.terminate()
            self.assertIsNone(session.receive())
            self.assertTrue(session.result())
            self.assertLess(session.proc.wait(timeout=2), 0)

    def test_failed_internal_login_flushes_generic_error_and_terminates_native(self):
        with PipeSession('login-error') as session:
            session.send('initialize', 1)
            response = session.receive()
            self.assertEqual(response['id'], 1)
            self.assertIn('error', response)
            self.assertNotIn('SYNTHETIC_NATIVE_PRIVATE_DETAIL', json.dumps(response))
            self.assertIsNone(session.receive())
            self.assertFalse(session.result())
            self.assertLess(session.proc.wait(timeout=2), 0)


if __name__ == '__main__':
    unittest.main()
