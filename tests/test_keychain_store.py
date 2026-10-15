"""Synthetic protocol tests; these helpers never access any macOS keychain."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import keychain_store


REF = 'chc_' + '1' * 32 + '_' + '2' * 32
SYNTHETIC = 'SYNTHETIC_KEYCHAIN_TEST_ONLY_测试'


class KeychainProtocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='harbor-protocol-test-')
        self.root = Path(self.temp.name).resolve()
        self.helper = self.root / 'HarborKeychain'
        self.addCleanup(self.temp.cleanup)

    def program(self, body):
        self.helper.write_text(f'#!{sys.executable}\n' + body)
        self.helper.chmod(0o700)
        return keychain_store.KeychainStore(self.helper, timeout=3)

    def test_get_returns_secret_from_stdin_stdout_protocol(self):
        store = self.program('import json,sys\nr=json.load(sys.stdin)\n'
                             'assert r == {"op":"get", "ref":' + repr(REF) + '}\n'
                             'print(json.dumps({"ok":True,"secret":' + repr(SYNTHETIC) + '}))\n')
        self.assertEqual(store.get(REF), SYNTHETIC)

    def test_put_secret_is_not_in_arguments_or_environment(self):
        store = self.program('import json,os,sys\nr=json.load(sys.stdin)\n'
                             'assert r["secret"] == ' + repr(SYNTHETIC) + '\n'
                             'assert r["op"] == "put"\n'
                             'assert len(sys.argv) == 1\n'
                             'assert not any(r["secret"] in v for v in os.environ.values())\n'
                             'assert "DYLD_INSERT_LIBRARIES" not in os.environ\n'
                             'print("{\\"ok\\":true}")\n')
        with patch.dict(os.environ, {'HARBOR_TEST_SECRET': SYNTHETIC,
                                     'DYLD_INSERT_LIBRARIES': '/nonexistent/synthetic.dylib'}):
            self.assertIsNone(store.put(REF, SYNTHETIC))

    def test_delete_acknowledges_success_without_returning_a_secret(self):
        store = self.program('import json,sys\nr=json.load(sys.stdin)\n'
                             'assert r == {"op":"delete", "ref":' + repr(REF) + '}\n'
                             'print("{\\"ok\\":true}")\n')
        self.assertIsNone(store.delete(REF))

    def test_interaction_is_explicit_and_only_sent_as_a_boolean(self):
        self.program('import json,sys\nr=json.load(sys.stdin)\n'
                     'assert r["allow_interaction"] is True\n'
                     'print("{\\"ok\\":true}")\n')
        store = keychain_store.KeychainStore(self.helper, allow_interaction=True)
        self.assertIsNone(store.put(REF, SYNTHETIC))
        for value in [1, 'true', None]:
            with self.subTest(value=value), self.assertRaises(core.UserError):
                keychain_store.KeychainStore(self.helper, allow_interaction=value)

    def test_test_mode_can_never_request_interaction(self):
        with self.assertRaises(core.UserError):
            keychain_store.KeychainStore(self.helper, allow_interaction=True,
                                         test_keychain=self.root / 'test.keychain-db')

    def test_reference_validation_happens_before_execution(self):
        marker = self.root / 'executed'
        store = self.program('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        for ref in ['', None, 3, '../outside', REF.upper(), REF + '\n', '1' * 32]:
            with self.subTest(ref=ref), self.assertRaises(core.UserError):
                store.get(ref)
        self.assertFalse(marker.exists())

    def test_invalid_secrets_are_rejected_without_echoing_them(self):
        store = self.program('raise AssertionError("must not execute")\n')
        for secret in ['', None, 3, SYNTHETIC + '\n', 'x\x00y', 'x\x7fy',
                       'x' * (keychain_store.MAX_SECRET_BYTES + 1), '\ud800']:
            with self.subTest(kind=type(secret).__name__), self.assertRaises(core.UserError) as caught:
                store.put(REF, secret)
            self.assertNotIn(SYNTHETIC, str(caught.exception))

    def test_native_error_codes_have_sanitized_user_messages(self):
        for code in ['not_found', 'access_denied', 'locked', 'unavailable', 'invalid_request', 'failure']:
            store = self.program('print(' + repr(json.dumps({'ok': False, 'error': code})) + ')\n')
            with self.subTest(code=code), self.assertRaises(core.UserError) as caught:
                store.get(REF)
            self.assertNotIn(REF, str(caught.exception))
            self.assertNotIn(SYNTHETIC, str(caught.exception))

    def test_untrusted_error_text_is_never_shown(self):
        store = self.program('import sys\nprint(' + repr(json.dumps({'ok':False, 'error':SYNTHETIC})) + ')\n'
                             'print(' + repr(SYNTHETIC) + ',file=sys.stderr)\n')
        with self.assertRaises(core.UserError) as caught:
            store.get(REF)
        self.assertNotIn(SYNTHETIC, str(caught.exception))

    def test_crash_stdout_and_stderr_are_not_in_user_error(self):
        store = self.program('import sys\nprint(' + repr(SYNTHETIC) + ')\n'
                             'print(' + repr(SYNTHETIC) + ',file=sys.stderr)\nsys.exit(7)\n')
        with self.assertRaises(core.UserError) as caught:
            store.put(REF, SYNTHETIC)
        self.assertNotIn(SYNTHETIC, str(caught.exception))

    def test_malformed_or_ambiguous_success_is_rejected(self):
        for response in ['not json', '[]', '{"ok":1,"secret":"synthetic"}',
                         '{"ok":true}', '{"ok":true,"secret":null}',
                         '{"ok":true,"secret":"synthetic","extra":true}',
                         '{"ok":true,"secret":"synthetic"}\n{"ok":true}',
                         '{"ok":false,"error":[]}']:
            store = self.program('print(' + repr(response) + ')\n')
            with self.subTest(response=response), self.assertRaises(core.UserError):
                store.get(REF)

    def test_put_rejects_secret_in_success_response(self):
        store = self.program('print(' + repr(json.dumps({'ok':True,'secret':SYNTHETIC})) + ')\n')
        with self.assertRaises(core.UserError):
            store.put(REF, SYNTHETIC)

    def test_timeout_covers_helper_that_does_not_read_stdin(self):
        store = self.program('import time\ntime.sleep(20)\n')
        store.timeout = 0.3
        started = time.monotonic()
        with self.assertRaises(core.UserError):
            store.put(REF, 'x' * keychain_store.MAX_SECRET_BYTES)
        self.assertLess(time.monotonic() - started, 3)

    def test_stdout_and_stderr_floods_are_bounded(self):
        for stream in ['stdout', 'stderr']:
            store = self.program('import sys\nsys.' + stream + '.buffer.write(b"x" * 4000000)\n'
                                 'sys.' + stream + '.flush()\n')
            started = time.monotonic()
            with self.subTest(stream=stream), self.assertRaises(core.UserError):
                store.get(REF)
            self.assertLess(time.monotonic() - started, 3)

    def test_missing_helper_has_no_fallback(self):
        store = keychain_store.KeychainStore(self.helper)
        with self.assertRaises(core.UserError):
            store.put(REF, SYNTHETIC)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlinked_helper_and_parent_are_rejected(self):
        self.program('print("{\\"ok\\":true}")\n')
        link = self.root / 'link'
        link.symlink_to(self.helper)
        with self.assertRaises(core.UserError):
            keychain_store.KeychainStore(link).delete(REF)
        directory_link = self.root / 'directory-link'
        directory_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(core.UserError):
            keychain_store.KeychainStore(directory_link / self.helper.name).delete(REF)

    def test_group_writable_helper_is_rejected(self):
        store = self.program('print("{\\"ok\\":true}")\n')
        self.helper.chmod(0o770)
        with self.assertRaises(core.UserError):
            store.delete(REF)

    def test_group_writable_helper_directory_is_rejected(self):
        store = self.program('print("{\\"ok\\":true}")\n')
        self.root.chmod(0o770)
        self.addCleanup(self.root.chmod, 0o700)
        with self.assertRaises(core.UserError):
            store.delete(REF)

    def test_maximum_secret_with_json_escaping_can_be_written(self):
        secret = '\\' * keychain_store.MAX_SECRET_BYTES
        store = self.program('import json,sys\nr=json.load(sys.stdin)\n'
                             'assert len(r["secret"]) == ' + str(len(secret)) + '\n'
                             'print("{\\"ok\\":true}")\n')
        self.assertIsNone(store.put(REF, secret))

    def test_maximum_secret_with_json_escaping_can_be_read(self):
        secret = '\\' * keychain_store.MAX_SECRET_BYTES
        store = self.program('import json\nprint(json.dumps({"ok":True,"secret":"\\\\" * '
                             + str(len(secret)) + '}))\n')
        self.assertEqual(store.get(REF), secret)

    def test_test_keychain_requires_explicit_helper(self):
        with self.assertRaises(core.UserError):
            keychain_store.KeychainStore(test_keychain=self.root / 'test.keychain-db')

    def test_test_mode_requires_owned_private_dedicated_directory(self):
        self.program('raise AssertionError("must not execute")\n')
        for path in [self.root / 'test.keychain-db', Path('/Library/Keychains/System.keychain')]:
            with self.subTest(path=path), self.assertRaises(core.UserError):
                keychain_store.KeychainStore(self.helper, test_keychain=path).get(REF)

    def test_default_helper_is_relative_to_engine_not_path_lookup(self):
        store = keychain_store.KeychainStore()
        self.assertEqual(store.helper_path, Path(keychain_store.__file__).absolute().parent.parent / 'HarborKeychain')


if __name__ == '__main__':
    unittest.main()
