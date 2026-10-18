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
from official_responses import native_arguments
import backend_guard


@unittest.skipUnless(Path(CODEX).is_file(), 'Installed Codex CLI is required')
class NativeConfigArguments(unittest.TestCase):
    def guarded_arguments(self, arguments, url):
        """Capture the production guard's final spawn, then exercise the native parser."""
        transport = MagicMock(url=url)
        child = MagicMock()
        child.poll.return_value = 0
        child.wait.return_value = 0
        env = {'HOME': '/synthetic', 'CODEX_HOME': '/synthetic/codex',
               'HARBOR_OFFICIAL_PROFILE': 'a' * 32, 'HARBOR_CONTROL_ROOT': '/synthetic/control'}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, 'argv', ['guard', '-', '-', '-', str(CODEX), *arguments]), \
             patch('core.Store'), patch('official_responses.OfficialResponsesBridge', return_value=transport), \
             patch('auth_bridge.run', return_value=True), patch('signal.signal'), \
             patch('subprocess.Popen', return_value=child) as spawn:
            with self.assertRaises(SystemExit) as stopped:
                backend_guard.main()
            self.assertEqual(stopped.exception.code, 0)
            actual, options = spawn.call_args.args[0], spawn.call_args.kwargs
            self.assertEqual(actual[0], str(CODEX))
            self.assertIn('127.0.0.1', options['env']['NO_PROXY'])
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

    def test_desktop_plugin_flags_keep_transport_auth_and_user_overrides(self):
        url = 'http://127.0.0.1:1/backend-api/codex'
        arguments = ['-c', 'features.code_mode_host=true',
                     '-c', 'model_reasoning_effort="low"', 'app-server',
                     '--analytics-default-enabled',
                     '-c', 'plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=false',
                     '-c', 'model_reasoning_effort="high"']
        result = self.read_config(self.guarded_arguments(arguments, url))
        self.assertEqual(result.get('openai_base_url'), url)
        self.assertIs(result.get('features', {}).get('enable_request_compression'), False)
        self.assertEqual(result.get('cli_auth_credentials_store'), 'ephemeral')
        self.assertIs(result.get('features', {}).get('code_mode_host'), True)
        self.assertEqual(result.get('model_reasoning_effort'), 'high')
        self.assertIs(result['plugins']['codex-app-tools@openai-bundled']['mcp_servers']['codex_app']['enabled'], False)

    def test_feature_switch_precedence_is_unchanged(self):
        arguments = ['-c', 'features.shell_snapshot=false', '--enable', 'shell_snapshot',
                     'app-server', '-c', 'model_reasoning_effort="high"']
        result = self.read_config(native_arguments(ephemeral_arguments(arguments),
                                                  'http://127.0.0.1:1/backend-api/codex'))
        # Native --enable has precedence independent of its textual position.
        self.assertIs(result.get('features', {}).get('shell_snapshot'), True)
        self.assertEqual(result.get('model_reasoning_effort'), 'high')

    def test_inline_and_repeated_overrides_remain_effective(self):
        url = 'http://127.0.0.1:1/backend-api/codex'
        arguments = ['--config=openai_base_url="https://example.invalid"',
                     '-c=cli_auth_credentials_store="file"',
                     '-cfeatures.code_mode_host=true', 'app-server',
                     '--config=features.enable_request_compression=true',
                     '-copenai_base_url="https://example.invalid"']
        result = self.read_config(native_arguments(ephemeral_arguments(arguments), url))
        self.assertEqual(result.get('openai_base_url'), url)
        self.assertIs(result.get('features', {}).get('enable_request_compression'), False)
        self.assertEqual(result.get('cli_auth_credentials_store'), 'ephemeral')
        self.assertIs(result.get('features', {}).get('code_mode_host'), True)

    def test_feature_enable_and_table_overrides_cannot_bypass_official_boundary(self):
        url = 'http://127.0.0.1:1/backend-api/codex'
        for enable in (['--enable', 'enable_request_compression'],
                       ['--enable=enable_request_compression']):
            with self.subTest(enable=enable):
                arguments = [*enable, 'app-server',
                    '-c', 'features={enable_request_compression=true,code_mode_host=true}',
                    '-c', '"openai_base_url"="https://example.invalid"',
                    '-c', '"cli_auth_credentials_store"="file"']
                result = self.read_config(self.guarded_arguments(arguments, url))
                self.assertEqual(result.get('openai_base_url'), url)
                self.assertIs(result.get('features', {}).get('enable_request_compression'), False)
                self.assertIs(result.get('features', {}).get('code_mode_host'), True)
                self.assertEqual(result.get('cli_auth_credentials_store'), 'ephemeral')


if __name__ == '__main__':
    unittest.main()
