"""Exercise the installed guardian command shape, never an importing wrapper."""
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from native_multi_instance_acceptance import NativeRPC
from test_official_auth import auth

import core
import storage

ENGINE = Path(__file__).resolve().parents[1] / 'engine'
NATIVE = r'''
import json, sys
assert 'cli_auth_credentials_store="ephemeral"' in sys.argv
authenticated = False
for raw in sys.stdin:
    value = json.loads(raw)
    if 'id' not in value: continue
    method = value['method']
    if method == 'initialize':
        assert value['params']['capabilities']['experimentalApi'] is True
        result = {'userAgent': 'synthetic-native'}
    elif method == 'account/login/start':
        params = value['params']
        assert params['type'] == 'chatgptAuthTokens'
        assert params['chatgptAccountId'] == 'synthetic-account'
        assert set(params) == {'type', 'accessToken', 'chatgptAccountId', 'chatgptPlanType'}
        authenticated = True
        result = {'type': 'chatgptAuthTokens'}
    elif method == 'configRequirements/read':
        assert authenticated
        print(json.dumps({'id': value['id'], 'error': {
            'code': -32000, 'message': 'SYNTHETIC_POLICY_DENIED',
            'data': {'requirement': 'native-only'}}}), flush=True)
        continue
    else:
        assert authenticated
        result = {'config': {'nativeSetting': 'preserved'}}
    print(json.dumps({'id': value['id'], 'result': result}), flush=True)
'''


class BackendEntrypoint(unittest.TestCase):
    def test_official_isolated_entrypoint_authenticates_and_preserves_native_policy(self):
        with tempfile.TemporaryDirectory(prefix='harbor-entry-', dir='/private/tmp') as temporary:
            root = Path(temporary)
            with patch.object(storage, 'EXISTING_HOMES', {}):
                store = core.Store(root / 'control', root / 'external', community=False)
                profile = store.save_profile({'kind': 'official', 'name': 'Synthetic'})
            canonical = store.root / 'accounts' / profile['id'] / 'codex' / 'auth.json'
            initial = auth('entrypoint')
            core.write_json(canonical, initial)
            original = canonical.read_bytes()
            fake = root / 'native.py'
            fake.write_text(NATIVE)
            hostile = root / 'hostile'
            hostile.mkdir()
            for name in ('core.py', 'auth_bridge.py', 'official_auth.py'):
                (hostile / name).write_text('raise RuntimeError("UNTRUSTED_MODULE_LOADED")\n')
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'TMPDIR': str(root),
                   'PYTHONPATH': str(hostile), 'HARBOR_OFFICIAL_PROFILE': profile['id'],
                   'HARBOR_CONTROL_ROOT': str(store.root)}
            # The native child is a small executable script so app-server argv
            # parsing and the -I production entrypoint are both exercised.
            binary = root / 'native'
            binary.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) +
                             ' -I -B ' + shlex.quote(str(fake)) + ' "$@"\n')
            binary.chmod(0o700)
            marker = root / 'generation.json'
            job = 'a' * 32
            core.write_json(marker, {'job': job})
            command = [sys.executable, '-I', '-B', str(ENGINE / 'backend_guard.py'),
                       str(root / 'backend.lock'), str(marker), job, str(binary), 'app-server']
            stderr = root / 'stderr'
            rpc = NativeRPC(command, env, hostile, stderr)
            try:
                self.assertEqual(rpc.call('initialize', {'clientInfo': {'name': 'synthetic'}}, timeout=4),
                                 {'userAgent': 'synthetic-native'})
                rpc.send('initialized', {})
                self.assertEqual(rpc.call('config/read', {'includeLayers': False}, timeout=4),
                                 {'config': {'nativeSetting': 'preserved'}})
                rpc.send('configRequirements/read', {}, 'policy')
                self.assertEqual(rpc.read(time.monotonic() + 4), {'id': 'policy', 'error': {
                    'code': -32000, 'message': 'SYNTHETIC_POLICY_DENIED',
                    'data': {'requirement': 'native-only'}}})
            except core.UserError:
                self.fail('Production guardian failed: ' + stderr.read_text())
            finally:
                rpc.close()
            self.assertEqual(canonical.read_bytes(), original)
            # official_auth.py constructs this owner below the account root.
            self.assertFalse((canonical.parent.parent / 'refresh-owner').exists())
            self.assertNotIn('UNTRUSTED_MODULE_LOADED', stderr.read_text())
            for field in ('access_token', 'refresh_token', 'id_token'):
                self.assertNotIn(initial['tokens'][field], stderr.read_text())

    def test_cold_isolated_import_constructs_store_at_requested_root(self):
        with tempfile.TemporaryDirectory(prefix='harbor-import-', dir='/private/tmp') as temporary:
            root = Path(temporary)
            # run_path reproduces script entry sys.path without importing its
            # engine modules in the test process first.
            code = ('import runpy; runpy.run_path(' + repr(str(ENGINE / 'backend_guard.py')) + '); '
                    'import auth_bridge; from core import Store; '
                    's=Store(' + repr(str(root / 'control')) + '); print(s.root)')
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code],
                                    cwd=root, env={'HOME': str(root), 'PATH': '/usr/bin:/bin'},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(root / 'control'))


if __name__ == '__main__':
    unittest.main()
