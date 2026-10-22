"""Official transport stays native; only credential coordination uses local RPC."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import backend_guard


class OfficialDirectTests(unittest.TestCase):
    def launch(self, args, *, official=True):
        env = {'HOME': '/synthetic', 'CODEX_HOME': '/synthetic/codex',
               'NO_PROXY': 'example.invalid', 'no_proxy': 'another.invalid',
               'HTTPS_PROXY': 'http://proxy.invalid:9'}
        if official:
            env.update(HARBOR_OFFICIAL_PROFILE='a' * 32,
                       HARBOR_CONTROL_ROOT='/synthetic/control')
        child = MagicMock()
        child.poll.return_value = child.wait.return_value = 0
        credential_payload = {'accessToken': 'SYNTHETIC_ACCESS',
                              'chatgptAccountId': 'synthetic-account'}
        def coordinate(proc, credentials, **kwargs):
            self.assertIs(proc, child)
            self.assertEqual(credentials(), credential_payload)
            self.assertEqual(credentials(reason='unauthorized', previous_account_id='synthetic-account'),
                             credential_payload)
            return True
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, 'argv', ['guard', '-', '-', '-', '/synthetic/codex-cli', *args]), \
             patch('core.Store') as store, \
             patch('official_responses.OfficialResponsesBridge',
                   return_value=MagicMock(url='http://127.0.0.1:1/backend-api/codex')) as http_bridge, \
             patch('official_auth.credentials_for_instance', return_value=credential_payload) as credentials, \
             patch('auth_bridge.run', side_effect=coordinate) as rpc, \
             patch('signal.signal'), patch('subprocess.Popen', return_value=child) as spawn:
            with self.assertRaises(SystemExit) as stopped:
                backend_guard.main()
            self.assertEqual(stopped.exception.code, 0)
            http_bridge.assert_not_called()
            actual = spawn.call_args.args[0]
            options = spawn.call_args.kwargs
            if official and backend_guard.is_stdio_app_server(args):
                rpc.assert_called_once()
                self.assertEqual(credentials.call_count, 2)
                self.assertEqual(credentials.call_args.kwargs, {
                    'reason': 'unauthorized', 'previous_account_id': 'synthetic-account'})
                self.assertEqual(credentials.call_args.args, (store.return_value, 'a' * 32))
                self.assertEqual(options['stdin'], subprocess.PIPE)
                self.assertEqual(options['stdout'], subprocess.PIPE)
                effective_env = options['env'] if options['env'] is not None else env
                self.assertEqual(effective_env, env)
            else:
                rpc.assert_not_called()
                credentials.assert_not_called()
            return actual, options

    def test_official_app_server_uses_direct_native_transport_and_refresh_callbacks(self):
        actual, _ = self.launch(['app-server'])
        self.assertFalse(any('127.0.0.1' in item or 'enable_request_compression' in item
                             for item in actual))
        self.assertIn('openai_base_url="https://chatgpt.com/backend-api/codex"', actual)
        self.assertIn('cli_auth_credentials_store="ephemeral"', actual)

    def test_non_app_server_helper_keeps_ephemeral_auth_without_http_bridge(self):
        actual, _ = self.launch(['exec', 'synthetic'])
        self.assertIn('cli_auth_credentials_store="ephemeral"', actual)

    def test_router_does_not_enter_official_auth_and_retains_local_bypass(self):
        actual, options = self.launch(['app-server'], official=False)
        self.assertEqual(actual, ['/synthetic/codex-cli', 'app-server'])
        self.assertIn('127.0.0.1', options['env']['NO_PROXY'])
        self.assertEqual(options['env']['NO_PROXY'],
                         'example.invalid,another.invalid,127.0.0.1,localhost')
        self.assertEqual(options['env']['NO_PROXY'], options['env']['no_proxy'])
        self.assertEqual(options['env']['HTTPS_PROXY'], 'http://proxy.invalid:9')

    def test_router_non_app_server_helper_also_bypasses_local_proxy(self):
        actual, options = self.launch(['exec', 'synthetic'], official=False)
        self.assertEqual(actual, ['/synthetic/codex-cli', 'exec', 'synthetic'])
        self.assertIsNotNone(options['env'])
        self.assertIn('127.0.0.1', options['env']['NO_PROXY'])
        self.assertEqual(options['env']['NO_PROXY'],
                         'example.invalid,another.invalid,127.0.0.1,localhost')
        self.assertEqual(options['env']['NO_PROXY'], options['env']['no_proxy'])
        self.assertEqual(options['env']['HTTPS_PROXY'], 'http://proxy.invalid:9')


if __name__ == '__main__':
    unittest.main()
