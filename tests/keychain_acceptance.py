"""Native Keychain acceptance using only owned, isolated, synthetic keychains.

Run explicitly: python3 -B tests/keychain_acceptance.py
No login/default keychain is selected, searched, read, or written by this suite.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import argparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
import core
from keychain_store import KeychainStore

REF = 'chc_' + 'a' * 32 + '_' + 'b' * 32
OTHER = 'chc_' + 'c' * 32 + '_' + 'd' * 32
PASSWORD = 'SYNTHETIC_TEST_KEYCHAIN_PASSWORD_ONLY'
SECRET = 'SYNTHETIC_API_KEY_ONLY_测试'
BUNDLED_HELPER = None


class NativeKeychain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='harbor-keychain-build-')
        cls.helper = Path(BUNDLED_HELPER).resolve() if BUNDLED_HELPER else Path(cls.build.name).resolve() / 'HarborKeychain'
        cls.addClassCleanup(cls.build.cleanup)
        if not BUNDLED_HELPER:
            subprocess.run(['/usr/bin/swiftc', '-suppress-warnings', '-framework', 'Security',
                            str(ROOT / 'native/Sources/HarborKeychain/main.swift'), '-o', str(cls.helper)],
                           check=True, capture_output=True, timeout=60)
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', str(cls.helper)],
                           check=True, capture_output=True, timeout=15)
        updated = Path(cls.build.name).resolve() / 'updated'
        updated.mkdir(mode=0o700)
        cls.updated_helper = updated / 'HarborKeychain'
        subprocess.run(['/usr/bin/swiftc', '-O', '-suppress-warnings', '-framework', 'Security',
                        str(ROOT / 'native/Sources/HarborKeychain/main.swift'), '-o', str(cls.updated_helper)],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', str(cls.updated_helper)],
                       check=True, capture_output=True, timeout=15)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='codex-harbor-keychain-test-')
        self.keychain = Path(self.temp.name).resolve() / 'test.keychain-db'
        self.addCleanup(self.temp.cleanup)
        result = self.raw({'op':'test_create', 'secret':PASSWORD})
        self.assertEqual(result, {'ok':True})
        self.addCleanup(self.destroy)
        self.store = KeychainStore(self.helper, test_keychain=self.keychain)

    def raw(self, payload, *, test=True):
        if test:
            payload = dict(payload, test_keychain=str(self.keychain))
        result = subprocess.run([str(self.helper)] + (['--test-keychain'] if test else []),
                                input=json.dumps(payload).encode(), capture_output=True, timeout=12)
        self.assertEqual(result.stderr, b'')
        self.assertLess(len(result.stdout), 32768)
        return json.loads(result.stdout)

    def destroy(self):
        self.assertEqual(self.raw({'op':'test_destroy'}), {'ok':True})

    def test_synthetic_create_read_update_delete_and_idempotent_delete(self):
        with self.assertRaises(core.UserError):
            self.store.get(REF)
        self.store.put(REF, SECRET)
        self.assertEqual(self.store.get(REF), SECRET)
        self.store.put(REF, SECRET + '_updated')
        self.assertEqual(self.store.get(REF), SECRET + '_updated')
        self.store.delete(REF)
        self.store.delete(REF)
        with self.assertRaises(core.UserError):
            self.store.get(REF)

    def test_account_references_are_isolated(self):
        self.store.put(REF, SECRET)
        self.store.put(OTHER, SECRET + '_other')
        self.store.delete(REF)
        self.assertEqual(self.store.get(OTHER), SECRET + '_other')

    def test_maximum_secret_round_trips_with_json_escaping(self):
        secret = '\\' * (16 * 1024)
        self.store.put(REF, secret)
        self.assertEqual(self.store.get(REF), secret)

    def test_native_rejects_arbitrary_service_or_account_before_lookup(self):
        for data in [{'op':'get','ref':'not-a-community-reference'},
                     {'op':'get','ref':REF,'service':'unrelated-service'},
                     {'op':'get','ref':REF,'account':'unrelated-account'}]:
            with self.subTest(fields=list(data)):
                self.assertEqual(self.raw(data), {'ok':False,'error':'invalid_request'})

    def test_native_test_path_never_works_without_explicit_flag(self):
        self.assertEqual(self.raw({'op':'get','ref':REF, 'test_keychain':str(self.keychain)}, test=False),
                         {'ok':False,'error':'invalid_request'})

    def test_test_mode_refuses_interaction_before_any_keychain_prompt(self):
        for value in [True, 1, 'true', None]:
            with self.subTest(value=value):
                self.assertEqual(self.raw({'op':'get','ref':REF,'allow_interaction':value}),
                                 {'ok':False,'error':'invalid_request'})
        self.assertEqual(self.raw({'op':'get','ref':REF,'allow_interaction':False}),
                         {'ok':False,'error':'not_found'})

    def test_changed_ad_hoc_helper_requires_authorization_for_older_secret(self):
        def cdhash(path):
            result = subprocess.run(['/usr/bin/codesign', '--display', '--verbose=4', str(path)],
                                    check=True, capture_output=True, text=True, timeout=5)
            return next(line for line in result.stderr.splitlines() if line.startswith('CDHash='))
        self.assertNotEqual(cdhash(self.helper), cdhash(self.updated_helper))
        self.store.put(REF, SECRET)
        updated = KeychainStore(self.updated_helper, test_keychain=self.keychain)
        with self.assertRaises(core.UserError):
            updated.get(REF)
        updated.put(OTHER, SECRET + '_replacement')
        self.assertEqual(updated.get(OTHER), SECRET + '_replacement')
        self.assertEqual(self.store.get(REF), SECRET)

    def test_native_rejects_oversized_request_without_echo(self):
        self.assertEqual(self.raw({'op':'put','ref':REF,'secret':'x' * 40000}),
                         {'ok':False,'error':'invalid_request'})

    def test_native_rejects_symlinked_test_directory(self):
        link = self.keychain.parent / 'nested-link'
        link.symlink_to(self.keychain.parent, target_is_directory=True)
        result = subprocess.run([str(self.helper), '--test-keychain'], input=json.dumps({
            'op':'get','ref':REF,'test_keychain':str(link / self.keychain.name)}).encode(),
            capture_output=True, timeout=12)
        self.assertEqual(json.loads(result.stdout), {'ok':False,'error':'invalid_request'})


if __name__ == '__main__':
    parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--helper',type=Path)
    options,rest=parser.parse_known_args();BUNDLED_HELPER=options.helper
    unittest.main(argv=[sys.argv[0],*rest])
