"""Read the installed native CLI's effective config; no login or model calls."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from backend_guard import ephemeral_arguments
from core import CODEX
import backend_guard


@unittest.skipUnless(Path(CODEX).is_file(), 'Installed Codex CLI is required')
class NativeConfigArguments(unittest.TestCase):
    def test_native_non_app_server_auth_override_semantics(self):
        from test_official_auth import auth
        with tempfile.TemporaryDirectory(prefix='harbor-auth-helper-', dir='/private/tmp') as tmp:
            home = Path(tmp) / 'codex'
            home.mkdir()
            canonical = home / 'auth.json'
            canonical.write_text(json.dumps(auth('helper-test')))
            original = canonical.read_bytes()
            env = {'HOME': tmp, 'CODEX_HOME': str(home), 'TMPDIR': tmp,
                   'PATH': '/usr/bin:/bin'}
            def status(arguments):
                return subprocess.run(['/usr/bin/sandbox-exec', '-p',
                    '(version 1)(allow default)(deny network*)', str(CODEX), *arguments],
                    env=env, cwd=tmp, capture_output=True, timeout=5)
            # The control proves that the synthetic file is a readable login.
            control = status(['-c', 'cli_auth_credentials_store="file"', 'login', 'status'])
            self.assertEqual(control.returncode, 0)
            self.assertIn(b'Logged in', control.stdout + control.stderr)
            for key in ('cli_auth_credentials_store', '"cli_auth_credentials_store"',
                        "'cli_auth_credentials_store'"):
                with self.subTest(key=key):
                    result = status(ephemeral_arguments(['-c', key + '="file"', 'login', 'status']))
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(b'Not logged in', result.stdout + result.stderr)
            self.assertEqual(canonical.read_bytes(), original)

    def guarded_arguments(self, arguments):
        """Capture the production guard's final spawn, then exercise the native parser."""
        transport = MagicMock(url="http://127.0.0.1:1/backend-api/codex")
        child = MagicMock()
        child.poll.return_value = 0
        child.wait.return_value = 0
        env = {'HOME': '/synthetic', 'CODEX_HOME': '/synthetic/codex',
               'HARBOR_OFFICIAL_PROFILE': 'a' * 32, 'HARBOR_CONTROL_ROOT': '/synthetic/control'}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, 'argv', ['guard', '-', '-', '-', str(CODEX), *arguments]), \
             patch('core.Store'), patch('official_responses.OfficialResponsesBridge', return_value=transport) as http_bridge, \
             patch('auth_bridge.run', return_value=True), patch('signal.signal'), \
             patch('subprocess.Popen', return_value=child) as spawn:
            with self.assertRaises(SystemExit) as stopped:
                backend_guard.main()
            self.assertEqual(stopped.exception.code, 0)
            actual, options = spawn.call_args.args[0], spawn.call_args.kwargs
            self.assertEqual(actual[0], str(CODEX))
            http_bridge.assert_not_called()
            self.assertIn(options['env'], (None, env))
            self.assertTrue(options['close_fds'])
            return actual[1:]

    def read_config(self, arguments):
        with tempfile.TemporaryDirectory(prefix='harbor-config-read-', suffix='.noindex') as tmp:
            root = Path(tmp)
            home, codex = root / 'home', root / 'codex'
            home.mkdir(); codex.mkdir()
            config = codex / 'config.toml'
            original = ('check_for_update_on_startup = false\n'
                        '[analytics]\nenabled = false\n[feedback]\nenabled = false\n'
                        '[features]\napps = false\nplugins = false\nremote_plugin = false\n')
            config.write_text(original)
            env = {'HOME': str(home), 'CODEX_HOME': str(codex), 'TMPDIR': tmp,
                   'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
            proc = subprocess.Popen([str(CODEX), *arguments], cwd=root, env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            pending = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)

                def send(value):
                    proc.stdin.write(json.dumps(value).encode() + b'\n'); proc.stdin.flush()

                def response(ident):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        while b'\n' in pending:
                            raw, _, rest = pending.partition(b'\n'); pending[:] = rest
                            value = json.loads(raw)
                            if value.get('id') == ident:
                                self.assertNotIn('error', value)
                                return value['result']
                        if selector.select(.2):
                            chunk = os.read(proc.stdout.fileno(), 65536)
                            if not chunk: break
                            pending.extend(chunk)
                    self.fail('Native config/read did not complete')

                try:
                    send({'id': 1, 'method': 'initialize', 'params': {
                        'clientInfo': {'name': 'harbor_config_regression', 'version': '1.0'},
                        'capabilities': {'experimentalApi': True}}})
                    response(1)
                    send({'method': 'initialized', 'params': {}})
                    send({'id': 2, 'method': 'config/read', 'params': {'includeLayers': False}})
                    result = response(2)['config']
                    self.assertEqual(config.read_text(), original)
                    self.assertFalse((codex / 'auth.json').exists())
                    return result
                finally:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)
                    proc.stdin.close(); proc.stdout.close()

    def test_desktop_plugin_flags_keep_native_transport_auth_and_user_overrides(self):
        arguments = ['-c', 'features.code_mode_host=true',
                     '-c', 'model_reasoning_effort="low"', 'app-server',
                     '--analytics-default-enabled',
                     '-c', 'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=false',
                     '-c', 'model_reasoning_effort="high"']
        result = self.read_config(self.guarded_arguments(arguments))
        baseline = self.read_config(['app-server'])
        self.assertEqual(result.get('openai_base_url'), 'https://chatgpt.com/backend-api/codex')
        self.assertEqual(result.get('features', {}).get('enable_request_compression'),
                         baseline.get('features', {}).get('enable_request_compression'))
        self.assertEqual(result.get('cli_auth_credentials_store'), 'ephemeral')
        self.assertIs(result.get('features', {}).get('code_mode_host'), True)
        self.assertEqual(result.get('model_reasoning_effort'), 'high')
        self.assertIs(result['plugins']['codex-app-tools@openai-bundled']['mcp_servers']['codex_app']['enabled'], False)

    def test_feature_switch_precedence_is_unchanged(self):
        arguments = ['-c', 'features.shell_snapshot=false', '--enable', 'shell_snapshot',
                     'app-server', '-c', 'model_reasoning_effort="high"']
        result = self.read_config(self.guarded_arguments(arguments))
        self.assertIs(result.get('features', {}).get('shell_snapshot'), True)
        self.assertEqual(result.get('model_reasoning_effort'), 'high')

    def test_inline_and_repeated_overrides_remain_effective(self):
        arguments = ['--config=model_reasoning_effort="low"',
                     '-c=cli_auth_credentials_store="file"',
                     '-cfeatures.code_mode_host=true', 'app-server',
                     '--config=features.enable_request_compression=true',
                     '-cmodel_reasoning_effort="high"']
        result = self.read_config(self.guarded_arguments(arguments))
        self.assertEqual(result.get('openai_base_url'), 'https://chatgpt.com/backend-api/codex')
        self.assertIs(result.get('features', {}).get('enable_request_compression'), True)
        self.assertEqual(result.get('cli_auth_credentials_store'), 'ephemeral')
        self.assertIs(result.get('features', {}).get('code_mode_host'), True)
        self.assertEqual(result.get('model_reasoning_effort'), 'high')

    def test_native_compression_and_table_overrides_preserve_ephemeral_credentials(self):
        for enable in (['--enable', 'enable_request_compression'],
                       ['--enable=enable_request_compression']):
            with self.subTest(enable=enable):
                arguments = [*enable, 'app-server',
                    '-c', 'features={enable_request_compression=true,code_mode_host=true}',
                    '-c', '"cli_auth_credentials_store"="file"']
                result = self.read_config(self.guarded_arguments(arguments))
                self.assertEqual(result.get('openai_base_url'), 'https://chatgpt.com/backend-api/codex')
                self.assertIs(result.get('features', {}).get('enable_request_compression'), True)
                self.assertIs(result.get('features', {}).get('code_mode_host'), True)
                self.assertEqual(result.get('cli_auth_credentials_store'), 'ephemeral')

    def test_account_selection_pins_official_provider_and_destination(self):
        arguments = ['-c', 'model_provider="custom"',
                     '-c', 'chatgpt_base_url="https://control.invalid"',
                     '--config=openai_base_url="https://root.invalid"', 'app-server',
                     '-c', 'model_providers.custom={name="custom",base_url="https://provider.invalid",requires_openai_auth=true}',
                     '-c', '"openai_base_url"="https://trailing.invalid"']
        result = self.read_config(self.guarded_arguments(arguments))
        self.assertEqual(result['model_provider'], 'openai')
        self.assertEqual(result['openai_base_url'], 'https://chatgpt.com/backend-api/codex')
        self.assertEqual(result['chatgpt_base_url'], 'https://chatgpt.com/backend-api/')


if __name__ == '__main__':
    unittest.main()
