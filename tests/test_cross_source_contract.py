"""Offline checks for the cross-source acceptance contract; no native/model calls."""
import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cross_source_acceptance import (
    HOSTED_SEARCH, SEARCH_URL, SOURCE_FACT, COLOR,
    append_only, frozen_files, unchanged_files, replay_error,
    required_history_present, resume_params, native_transport_override_observed,
)


class CrossSourceContract(unittest.TestCase):
    def test_resume_loads_the_same_persisted_thread_without_replacement_history(self):
        params = resume_params('original-thread')
        self.assertEqual(params['threadId'], 'original-thread')
        self.assertNotIn('history', params)
        self.assertNotIn('path', params)

    def test_undeclared_hosted_replay_fails_in_both_dialects(self):
        request = {'input': [copy.deepcopy(HOSTED_SEARCH)]}
        self.assertIsNotNone(replay_error(request, lite=False))
        self.assertIsNotNone(replay_error(request, lite=True))

    def test_ordinary_hosted_replay_accepts_the_declared_tool(self):
        for tool_type in ('web_search', 'web_search_preview'):
            request = {'input': [copy.deepcopy(HOSTED_SEARCH)],
                       'tools': [{'type': tool_type, 'external_web_access': False}]}
            self.assertIsNone(replay_error(request, lite=False))

    def test_lite_replay_accepts_additional_tools_and_rejects_top_level_search(self):
        declaration = {'type': 'additional_tools', 'role': 'developer',
                       'tools': [{'type': 'web_search', 'external_web_access': False}]}
        request = {'input': [declaration, copy.deepcopy(HOSTED_SEARCH)]}
        self.assertIsNone(replay_error(request, lite=True))
        request = {'input': [copy.deepcopy(HOSTED_SEARCH)],
                   'tools': declaration['tools']}
        self.assertIsNotNone(replay_error(request, lite=True))

    def test_quoted_search_text_does_not_count_as_a_hosted_tool_item(self):
        request = {'input': [{'type': 'message', 'role': 'user',
                   'content': 'A string mentions web_search_call, but is not one.'}]}
        self.assertIsNone(replay_error(request, lite=True))

    def test_fixture_requires_prior_information_instead_of_always_returning_success(self):
        items = [copy.deepcopy(HOSTED_SEARCH), {'type': 'message', 'role': 'assistant',
                 'content': f'{COLOR} {SOURCE_FACT} {SEARCH_URL}'}]
        self.assertTrue(required_history_present({'input': items}))
        self.assertFalse(required_history_present({'input': items[1:]}))
        self.assertFalse(required_history_present({'input': items[:1]}))

    def test_appended_compaction_preserves_original_bytes_but_rewriting_does_not(self):
        original = (json.dumps({'type': 'response_item', 'payload': HOSTED_SEARCH}) + '\n').encode()
        with tempfile.TemporaryDirectory() as temp:
            rollout = Path(temp) / 'rollout.jsonl'
            rollout.write_bytes(original + b'{"type":"compacted","payload":{}}\n')
            self.assertTrue(append_only(rollout, original))
            rollout.write_bytes(b'{"type":"compacted","payload":{}}\n')
            self.assertFalse(append_only(rollout, original))
            rollout.write_bytes(original.replace(b'web_search_call', b'message'))
            self.assertFalse(append_only(rollout, original))

    def test_account_settings_and_original_history_changes_cannot_pass_the_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / name for name in ('auth.json', 'config.toml', 'original.jsonl')]
            for path in paths:
                path.write_text('SYNTHETIC_UNCHANGED')
            before = frozen_files(paths)
            self.assertTrue(unchanged_files(before))
            paths[1].write_text('CHANGED')
            self.assertFalse(unchanged_files(before))
            paths[1].write_text('SYNTHETIC_UNCHANGED')
            paths[2].unlink()
            self.assertFalse(unchanged_files(before))

    def test_native_transport_override_needs_matching_uncompressed_bridge_traffic(self):
        ingress = [{'method': 'POST', 'path': '/backend-api/codex/responses',
                    'request_bytes': 123, 'content_encoding': None}]
        upstream = [{'path': '/A/backend-api/codex/responses', 'identity': 'A'}]
        self.assertTrue(native_transport_override_observed(ingress, upstream))
        failures = [([], upstream), (ingress, []), (ingress * 2, upstream),
                    ([{**ingress[0], 'content_encoding': 'zstd'}], upstream),
                    ([{**ingress[0], 'request_bytes': 0}], upstream),
                    ([{**ingress[0], 'path': '/sentinel/config/responses'}], upstream),
                    (ingress, [{**upstream[0], 'path': '/sentinel/config/responses'}])]
        for observed, forwarded in failures:
            with self.subTest(observed=observed, forwarded=forwarded):
                self.assertFalse(native_transport_override_observed(observed, forwarded))


class LiveAcceptanceContract(unittest.TestCase):
    def test_live_cli_requires_explicit_opt_in_before_accessing_supplied_paths(self):
        script = Path(__file__).with_name('live_cross_source_acceptance.py')
        result = subprocess.run([sys.executable, '-B', str(script), '--official-home', '/nonexistent/never-read',
                                 '--router-profile', '/nonexistent/never-read-profile'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--run-live is required', result.stderr)

    def test_official_auth_exports_access_only_and_does_not_change_source(self):
        from live_cross_source_acceptance import read_official_auth
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'auth.json'
            raw = json.dumps({'tokens': {'access_token': 'SYNTHETIC_ACCESS',
                             'account_id': 'synthetic-account', 'refresh_token': 'NEVER_COPY_THIS'}}).encode()
            path.write_bytes(raw)
            auth = read_official_auth(Path(temp))
            self.assertEqual(auth, {'type': 'chatgptAuthTokens', 'accessToken': 'SYNTHETIC_ACCESS',
                                    'chatgptAccountId': 'synthetic-account'})
            self.assertNotIn('NEVER_COPY_THIS', json.dumps(auth))
            self.assertEqual(path.read_bytes(), raw)

    def test_original_rollout_copy_keeps_all_bytes_and_id_with_private_permissions(self):
        from live_cross_source_acceptance import copy_rollout, file_digest
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tid = str(uuid.uuid4())
            source = root / ('rollout-' + tid + '.jsonl')
            raw = (json.dumps({'type': 'session_meta', 'payload': {'id': tid}}) + '\n' +
                   json.dumps({'type': 'response_item', 'payload': HOSTED_SEARCH}) + '\n').encode()
            source.write_bytes(raw)
            target = root / 'copy' / source.name
            self.assertEqual(copy_rollout(source, target), tid)
            self.assertEqual(target.read_bytes(), raw)
            self.assertEqual(file_digest(source), file_digest(target))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(source.read_bytes(), raw)

    def test_original_rollout_copy_refuses_overwriting_an_existing_file(self):
        from live_cross_source_acceptance import copy_rollout
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / 'source.jsonl', root / 'existing.jsonl'
            source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': str(uuid.uuid4())}}) + '\n')
            target.write_text('DO_NOT_OVERWRITE')
            with self.assertRaises(FileExistsError):
                copy_rollout(source, target)
            self.assertEqual(target.read_text(), 'DO_NOT_OVERWRITE')

    def test_evidence_errors_retain_classification_without_private_messages(self):
        from live_cross_source_acceptance import error_kind
        error = {'message': 'SYNTHETIC_PRIVATE_TEXT', 'additionalDetails': 'SYNTHETIC_ACCESS',
                 'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': 400}}}
        self.assertEqual(error_kind(error), 'responseStreamDisconnected')
        self.assertEqual(error_kind({'codexErrorInfo': {'SYNTHETIC_SECRET': 'private'}}), 'other')

    def test_live_wire_evidence_has_counts_and_bytes_without_body_or_private_type_values(self):
        from live_cross_source_acceptance import wire_summary
        raw = json.dumps({'input': [{'type': 'web_search_call', 'action': {'query': 'PRIVATE_QUERY'}},
                                   {'type': 'PRIVATE_TYPE', 'content': 'PRIVATE_TEXT'}]}).encode()
        summary = wire_summary(raw, None)
        self.assertEqual(summary['request_bytes'], len(raw))
        self.assertEqual(summary['input_type_counts'], {'web_search_call': 1, 'other': 1})
        self.assertIsNone(summary['content_encoding'])
        self.assertNotIn('PRIVATE', json.dumps(summary))

    def test_live_turn_retains_outcome_without_model_text_or_command_output(self):
        from live_cross_source_acceptance import NativeClient
        with tempfile.TemporaryDirectory(suffix='.noindex') as temp:
            client = object.__new__(NativeClient)
            client.pending, client.timeout, client.denied_callbacks = [], 1, 0
            client.cwd = Path(temp).resolve()
            client.call = lambda *args: {'turn': {'id': 'turn'}}
            events = iter([
                {'method': 'item/agentMessage/delta', 'params': {'threadId': 'thread', 'delta': 'PRIVATE_TEXT teal'}},
                {'method': 'item/completed', 'params': {'threadId': 'thread', 'item': {
                    'id': 'command', 'type': 'commandExecution', 'command': '/usr/bin/printf PRIVATE_MARKER',
                    'exitCode': 0, 'cwd': temp, 'aggregatedOutput': 'PRIVATE_MARKER'}}},
                {'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed'}}},
            ])
            client.read = lambda *args: next(events)
            result = client.turn('thread', 'synthetic prompt', expected=('teal',), printf_marker='PRIVATE_MARKER')
            self.assertTrue(result['expected_values_recalled'])
            self.assertTrue(result['printf_completed_in_workspace'])
            self.assertNotIn('PRIVATE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
