"""Only temporary stores and a localhost HTTP server; no real keys or models."""
import fcntl
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import probe


class ApiManagement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = core.Store(self.root/'control', self.root/'external', mounted=lambda: True, community=False)
        self.store.initialize_storage('local')
        self.received = []
        self.mode = 'success'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.received.append((self.path, body, self.headers.get('Authorization')))
                status = 200
                data = {'object': 'response', 'status': 'completed', 'output': [
                    {'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}],
                    'usage': {'input_tokens': 4, 'output_tokens': 2}}
                if owner.mode == 'slow': time.sleep(.6)
                if owner.mode == 'error':
                    status = 400
                    data = {'error': {'message': 'Do not reveal SYNTHETIC_KEY or PRIVATE_HISTORY',
                                      'code': 'invalid_request_error', 'param': 'input'}}
                if owner.mode == 'incomplete':
                    data.update(status='incomplete', output=[], incomplete_details={'reason': 'max_output_tokens'})
                if owner.mode == 'adaptive' and body['max_output_tokens'] < 512:
                    data.update(status='incomplete',output=[],incomplete_details={'reason':'max_output_tokens'},
                                usage={'input_tokens':4,'output_tokens':body['max_output_tokens']})
                if owner.mode == 'filtered':
                    data.update(status='incomplete',output=[],incomplete_details={'reason':'content_filter'})
                if owner.mode == 'adaptive_then_error':
                    if body['max_output_tokens']==32:
                        data.update(status='incomplete',output=[],incomplete_details={'reason':'max_output_tokens'})
                    else:
                        status=429;data={'error':{'message':'private upstream data','param':None}}
                if owner.mode == 'empty': data['output'] = []
                if owner.mode == 'bad_usage': data['usage'] = {'input_tokens': -1, 'output_tokens': 2**64}
                if owner.mode == 'wrong_object': data['object'] = 'chat.completion'
                if owner.mode == 'redirect': status = 307
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                if owner.mode == 'redirect': self.send_header('Location', '/forbidden-target')
                self.end_headers()
                try: self.wfile.write(raw)
                except OSError: pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.profile = self.store.save_profile({
            'name': 'Synthetic Router', 'kind': 'api',
            'url': f'http://127.0.0.1:{self.server.server_port}/v1',
            'protocol': 'responses', 'api_key': 'SYNTHETIC_KEY', 'models': [
                {'alias': 'gpt-6-luna', 'upstream': 'synthetic/flash', 'effort': 'low',
                 'context': 128000, 'reasoning': 'off'}]})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def probe(self, **kwargs):
        from probe import probe_api
        return probe_api(self.store, self.profile['id'], 'gpt-6-luna', **kwargs)

    def test_probe_one_small_request_no_history_no_session(self):
        sentinel = self.store.root/'stores/local/history/sessions/private.jsonl'
        core.atomic_write(sentinel, 'PRIVATE_HISTORY')
        before = set(self.store.root.rglob('*'))
        result = self.probe()
        self.assertEqual(result['outcome'], 'success')
        self.assertTrue(result['reply_ok'])
        self.assertEqual(result['input_tokens'], 4)
        self.assertEqual(len(self.received), 1)
        path, body, auth = self.received[0]
        self.assertEqual(path, '/v1/responses')
        self.assertEqual(auth, 'Bearer SYNTHETIC_KEY')
        self.assertEqual(body['model'], 'synthetic/flash')
        self.assertEqual(body['input'], 'Reply OK.')
        self.assertEqual(body['max_output_tokens'], 32)
        self.assertFalse(body['stream'])
        self.assertFalse(body['store'])
        self.assertNotIn('tools', body)
        self.assertNotIn('reasoning', body)
        self.assertNotIn('PRIVATE_HISTORY', json.dumps(body))
        self.assertNotIn('SYNTHETIC_KEY', json.dumps(result))
        self.assertEqual(sentinel.read_text(), 'PRIVATE_HISTORY')
        self.assertFalse(any('instances' in p.parts for p in set(self.store.root.rglob('*'))-before))

    def test_probe_honors_enabled_reasoning(self):
        edit = dict(self.profile, api_key='')
        edit['models'][0]['reasoning'] = 'forward'
        self.store.save_profile(edit)
        self.probe()
        self.assertEqual(self.received[0][1]['reasoning'], {'effort': 'low'})

    def test_http_error_is_not_retried_and_does_not_echo_body(self):
        self.mode = 'error'
        result = self.probe()
        self.assertEqual(result['outcome'], 'http_error')
        self.assertEqual(result['http_status'], 400)
        self.assertEqual(result['error_param'], 'input')
        self.assertEqual(len(self.received), 1)
        for value in ['SYNTHETIC_KEY', 'PRIVATE_HISTORY', 'Do not reveal']:
            self.assertNotIn(value, json.dumps(result))

    def test_redirect_is_not_followed(self):
        self.mode = 'redirect'
        self.assertEqual(self.probe()['outcome'], 'http_error')
        self.assertEqual(len(self.received), 1)

    def test_output_limit_and_empty_reply_do_not_claim_success(self):
        self.mode = 'incomplete'
        self.assertEqual(self.probe()['outcome'], 'budget_exhausted')
        self.mode = 'empty'
        self.assertEqual(self.probe()['outcome'], 'invalid_response')

    def test_output_limit_expands_only_until_complete_and_sums_usage(self):
        self.mode='adaptive'
        value=self.probe()
        self.assertEqual(value['outcome'],'success')
        self.assertEqual([x[1]['max_output_tokens'] for x in self.received],[32,128,512])
        self.assertEqual([x['max_output_tokens'] for x in value['attempts']],[32,128,512])
        self.assertEqual(value['input_tokens'],12)
        self.assertEqual(value['output_tokens'],162)
        self.assertTrue(all(x[1]['input']=='Reply OK.' and 'tools' not in x[1] and 'reasoning' not in x[1] for x in self.received))

    def test_expansion_has_a_finite_token_ceiling_and_never_fake_passes(self):
        self.mode='incomplete'
        value=self.probe()
        self.assertEqual(value['outcome'],'budget_exhausted')
        self.assertEqual([x[1]['max_output_tokens'] for x in self.received],[32,128,512,2048,8192])
        self.assertEqual(len(value['attempts']),5)
        self.assertNotIn('SYNTHETIC_KEY',json.dumps(value))

    def test_other_incomplete_reason_does_not_expand_or_pass(self):
        self.mode='filtered'
        value=self.probe()
        self.assertEqual(value['outcome'],'incomplete')
        self.assertEqual(len(self.received),1)
        self.assertEqual(len(value['attempts']),1)

    def test_http_error_after_expansion_stops_without_further_request(self):
        self.mode='adaptive_then_error'
        value=self.probe()
        self.assertEqual(value['outcome'],'http_error')
        self.assertEqual(value['http_status'],429)
        self.assertEqual(len(self.received),2)
        self.assertEqual(len(value['attempts']),2)

    def test_one_total_deadline_covers_all_expansion_attempts(self):
        real_popen=probe.subprocess.Popen
        def delayed(*args,**kwargs):
            time.sleep(.12)
            return real_popen(*args,**kwargs)
        self.mode='incomplete'
        started=time.monotonic()
        with patch('probe.subprocess.Popen',side_effect=delayed):value=self.probe(timeout=.32)
        self.assertEqual(value['outcome'],'timeout')
        self.assertLess(time.monotonic()-started,.8)
        self.assertLessEqual(len(self.received),2)

    def test_cancellation_between_attempts_does_not_send_next_request(self):
        self.mode='incomplete'
        value=self.probe(cancelled=lambda:len(self.received)>0)
        self.assertEqual(value['outcome'],'cancelled')
        self.assertEqual(len(self.received),1)

    def test_total_deadline_stops_worker_and_does_not_retry(self):
        self.mode = 'slow'
        start = time.monotonic()
        result = self.probe(timeout=.2)
        self.assertEqual(result['outcome'], 'timeout')
        self.assertLess(time.monotonic()-start, 1.5)
        self.assertLessEqual(len(self.received), 1)

    def test_process_startup_uses_the_same_deadline(self):
        real_popen = probe.subprocess.Popen
        def delayed_popen(*args, **kwargs):
            time.sleep(.3)
            return real_popen(*args, **kwargs)
        with patch('probe.subprocess.Popen', side_effect=delayed_popen):
            self.assertEqual(self.probe(timeout=.2)['outcome'], 'timeout')
        self.assertEqual(len(self.received), 0)

    def test_invalid_usage_does_not_break_native_result_decoding(self):
        self.mode = 'bad_usage'
        result = self.probe()
        self.assertIsNone(result['input_tokens'])
        self.assertIsNone(result['output_tokens'])

    def test_non_responses_object_is_not_success(self):
        self.mode = 'wrong_object'
        self.assertEqual(self.probe()['outcome'], 'invalid_response')

    def test_concurrent_probe_and_profile_change_are_blocked(self):
        with self.store.operation_guard(self.profile['id']):
            with self.assertRaises(core.UserError): self.probe()
            with self.assertRaises(core.UserError):
                self.store.save_profile(dict(self.profile, name='Changed', api_key=''))
            with self.assertRaises(core.UserError): self.store.remove_profile(self.profile['id'])
        self.assertEqual(len(self.received), 0)

    def test_distinct_models_overlap_but_edits_and_duplicate_probe_are_blocked(self):
        self.mode = 'slow'
        edit = dict(self.profile, api_key='')
        edit['models'] += [dict(edit['models'][0], alias='gpt-6-astra', upstream='synthetic/second')]
        self.profile = self.store.save_profile(edit)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.probe)
            b = pool.submit(probe.probe_api, self.store, self.profile['id'], 'gpt-6-astra')
            limit = time.monotonic() + .5
            while len(self.received) < 2 and time.monotonic() < limit: time.sleep(.005)
            self.assertEqual(len(self.received), 2, 'Both model requests reach the server before either completes')
            self.assertFalse(a.done())
            self.assertFalse(b.done())
            with self.assertRaises(core.UserError): self.probe()
            with self.assertRaises(core.UserError):
                self.store.save_profile(dict(self.profile, api_key='', name='Changed'))
            with self.assertRaises(core.UserError): self.store.remove_profile(self.profile['id'])
            self.assertEqual(a.result()['outcome'], 'success')
            self.assertEqual(b.result()['outcome'], 'success')
        self.assertEqual(len(self.received), 2)
        self.store.save_profile(dict(self.profile, api_key='', name='Unlocked'))

    def test_incomplete_reason_is_not_always_output_limit(self):
        payload = {'object':'response', 'status':'incomplete', 'output':[],
                   'incomplete_details':{'reason':'content_filter'}}
        from unittest.mock import MagicMock
        response = MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.status = 200
        response.__enter__.return_value = response
        with patch('probe.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            value = probe.worker({'profile':self.profile, 'alias':'gpt-6-luna', 'key':'SYNTHETIC_KEY'})
        self.assertEqual(value['outcome'], 'incomplete')
        self.assertNotIn('32 token', value['message'])

    def test_socket_timeout_is_classified_as_timeout_not_generic_network_error(self):
        with patch('probe.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError()
            value = probe.worker({'profile':self.profile, 'alias':'gpt-6-luna', 'key':'SYNTHETIC_KEY'})
        self.assertEqual(value['outcome'], 'timeout')

    def test_stale_probe_preflight_sends_no_request(self):
        original=self.profile
        self.store.save_profile(dict(original,name='Changed',api_key=''))
        with self.assertRaises(core.UserError):
            probe.probe_api(self.store,original['id'],'gpt-6-luna',revision=original['revision'])
        self.assertEqual(self.received,[])

    def test_cancellation_stops_waiting_and_no_followup_request(self):
        self.mode='slow'
        event=threading.Event()
        timer=threading.Timer(.12,event.set);timer.start()
        try:
            start=time.monotonic()
            result=probe.probe_api(self.store,self.profile['id'],'gpt-6-luna',cancelled=event.is_set)
            self.assertEqual(result['outcome'],'cancelled')
            self.assertLess(time.monotonic()-start,.5)
            self.assertLessEqual(len(self.received),1)
        finally:timer.cancel()

    def test_remove_restore_preserves_key_and_history(self):
        prepared = self.store.prepare(self.profile['id'], 'local', 'gpt-6-luna')
        sentinel = Path(prepared['history'])/'sessions/synthetic.jsonl'
        core.atomic_write(sentinel, 'PRIVATE_HISTORY')
        self.store.remove_profile(self.profile['id'])
        self.assertEqual(self.store.profiles(), [])
        self.assertEqual(self.store.removed_profiles()[0]['id'], self.profile['id'])
        with self.assertRaises(core.UserError): self.store.profile(self.profile['id'])
        self.assertEqual(sentinel.read_text(), 'PRIVATE_HISTORY')
        self.store.restore_profile(self.profile['id'])
        self.assertEqual(self.store.secret(self.profile['id']), 'SYNTHETIC_KEY')
        self.assertEqual(len(self.store.profiles()), 1)
        self.assertEqual(self.store.removed_profiles(), [])

    def test_remove_running_profile_is_rejected(self):
        with self.store.lock_path('local', self.profile['id']).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(core.UserError): self.store.remove_profile(self.profile['id'])
        self.assertEqual(len(self.store.profiles()), 1)

    def test_failed_remove_keeps_original_config(self):
        with patch('core.os.rename', side_effect=OSError('synthetic full disk')):
            with self.assertRaises(OSError): self.store.remove_profile(self.profile['id'])
        self.assertEqual(self.store.secret(self.profile['id']), 'SYNTHETIC_KEY')

    def test_stale_editor_cannot_resurrect_removed_profile(self):
        self.store.remove_profile(self.profile['id'])
        with self.assertRaises(core.UserError):
            self.store.save_profile(dict(self.profile, name='Stale', api_key='DIFFERENT_KEY'))
        self.assertEqual(self.store.profiles(), [])
        self.store.restore_profile(self.profile['id'])
        self.assertEqual(self.store.secret(self.profile['id']), 'SYNTHETIC_KEY')

    def test_removed_directory_symlink_is_rejected(self):
        outside = self.root/'outside'
        outside.mkdir()
        (self.store.root/'removed-profiles').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(core.UserError): self.store.removed_profiles()

    def test_restore_does_not_overwrite_existing_profile(self):
        self.store.remove_profile(self.profile['id'])
        backup = (self.store.root/'removed-profiles'/f"{self.profile['id']}.json").read_bytes()
        self.store.secret_path(self.profile['id']).write_bytes(backup)
        with self.assertRaises(core.UserError): self.store.restore_profile(self.profile['id'])
        self.assertEqual((self.store.root/'removed-profiles'/f"{self.profile['id']}.json").read_bytes(), backup)


if __name__ == '__main__': unittest.main()
