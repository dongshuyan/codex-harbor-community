"""Official refresh ownership uses synthetic homes and a local protocol fixture."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import official_auth


def synthetic_jwt(label, *, expires=None, user='synthetic-user'):
    claims = {'exp': time.time() + 3600 if expires is None else expires, 'jti': label,
              'sub': user, 'email': 'synthetic@example.invalid',
              'https://api.openai.com/auth': {'chatgpt_account_id': 'synthetic-account',
                                              'chatgpt_user_id': user, 'chatgpt_plan_type': 'pro'}}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': 'none'}) + '.' + encode(claims) + '.synthetic'


def auth(label, *, expires=None, user='synthetic-user'):
    return {'auth_mode': 'chatgpt', 'tokens': {
        'id_token': synthetic_jwt(label, user=user),
        'access_token': synthetic_jwt(label, expires=expires, user=user),
        'refresh_token': 'SYNTHETIC_REFRESH_' + label,
        'account_id': 'synthetic-account'}, 'last_refresh': '2026-10-16T00:00:00Z'}


FIXTURE = r'''
import base64, json, os, pathlib, sys, time
home=pathlib.Path(os.environ['CODEX_HOME'])
mode, replacement, count, canonical = sys.argv[1:]
def rotate():
    with open(count,'a') as out:out.write('refresh\n')
    if mode=='delay':time.sleep(.15)
    if mode!='unchanged' and mode!='timeout_unchanged':
        (home/'auth.json').write_bytes(pathlib.Path(replacement).read_bytes())
    if mode=='canonical_changed':
        pathlib.Path(canonical).write_text(pathlib.Path(replacement).read_text()+'\n')
    if mode in ('timeout_rotated','timeout_unchanged'):time.sleep(10)
    if mode=='crash_after_write':time.sleep(.5)
for line in sys.stdin:
    message=json.loads(line)
    if 'id' not in message: continue
    if message['method']=='initialize':
        token=json.loads((home/'auth.json').read_text())['tokens']['access_token']
        part=token.split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4)))
        if claims.get('exp',float('inf'))<=time.time()+300:rotate()
        result={}
    elif message['method']=='getAuthStatus':
        rotate()
        result={'authMethod':None,'authToken':None,'requiresOpenaiAuth':False}
    else:result={}
    print(json.dumps({'id':message['id'],'result':result}),flush=True)
'''


class OfficialAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='harbor-multi-research-', dir='/private/tmp')
        self.root = Path(self.tmp.name)
        self.store = core.Store(self.root / 'control', self.root / 'external', community=False)
        self.profile = self.store.save_profile({'kind': 'official', 'name': 'Synthetic account'})
        self.ident = self.profile['id']
        self.canonical = self.store.root / 'accounts' / self.ident / 'codex' / 'auth.json'
        self.initial = auth('old')
        core.write_json(self.canonical, self.initial)
        self.replacement = self.root / 'replacement.json'
        core.write_json(self.replacement, auth('new'))
        self.count = self.root / 'requests.txt'
        self.fixture = self.root / 'fake_native.py'
        self.fixture.write_text(FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, mode='replace'):
        return [sys.executable, '-I', '-B', str(self.fixture), mode,
                str(self.replacement), str(self.count), str(self.canonical)]

    def credentials(self, **kwargs):
        return official_auth.credentials_for_instance(self.store, self.ident, **kwargs)

    def test_fresh_credentials_return_only_external_auth_without_spawning(self):
        original = self.canonical.read_bytes()
        with patch('official_auth.subprocess.Popen', side_effect=AssertionError('no refresh needed')):
            value = self.credentials()
        self.assertEqual(value['type'], 'chatgptAuthTokens')
        self.assertEqual(value['accessToken'], self.initial['tokens']['access_token'])
        self.assertEqual(value['chatgptAccountId'], 'synthetic-account')
        self.assertEqual(value['chatgptPlanType'], 'pro')
        self.assertNotIn('refresh', json.dumps(value).lower())
        self.assertEqual(self.canonical.read_bytes(), original)

    def test_due_token_is_refreshed_with_native_owner_and_saved_privately(self):
        core.write_json(self.canonical, auth('old', expires=time.time() + 20))
        value = self.credentials(command=self.command())
        self.assertEqual(value['accessToken'], json.loads(self.replacement.read_text())['tokens']['access_token'])
        self.assertEqual(json.loads(self.canonical.read_text()), json.loads(self.replacement.read_text()))
        self.assertEqual(self.canonical.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_rejected_token_refreshes_even_if_expiration_is_in_future(self):
        result = self.credentials(rejected_access_token=self.initial['tokens']['access_token'],
                                  previous_account_id='synthetic-account', command=self.command())
        self.assertNotEqual(result['accessToken'], self.initial['tokens']['access_token'])

    def test_concurrent_rejections_share_one_refresh(self):
        old = self.initial['tokens']['access_token']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.credentials, rejected_access_token=old,
                        previous_account_id='synthetic-account', command=self.command('delay')) for _ in range(2)]
            values = [future.result() for future in futures]
        self.assertEqual(values[0], values[1])
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_wrong_requested_account_is_rejected_before_any_native_process(self):
        original = self.canonical.read_bytes()
        with self.assertRaises(core.UserError):
            self.credentials(previous_account_id='another-account', command=self.command())
        self.assertFalse(self.count.exists())
        self.assertEqual(self.canonical.read_bytes(), original)

    def test_refreshed_account_cannot_replace_canonical(self):
        replacement = auth('new')
        replacement['tokens']['account_id'] = 'another-account'
        core.write_json(self.replacement, replacement)
        original = self.canonical.read_bytes()
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command())
        self.assertEqual(self.canonical.read_bytes(), original)
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_refreshed_user_cannot_replace_canonical(self):
        core.write_json(self.replacement, auth('new', user='another-user'))
        original = self.canonical.read_bytes()
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command())
        self.assertEqual(self.canonical.read_bytes(), original)
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_successful_rpc_without_changed_tokens_is_not_reported_as_refresh_success(self):
        original = self.canonical.read_bytes()
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command('unchanged'))
        self.assertEqual(self.canonical.read_bytes(), original)
        # The uncertain attempt is retained; it may not rotate the old refresh token again.
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command())
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_timeout_after_native_persisted_rotation_recovers_new_credentials(self):
        value = self.credentials(force_refresh=True, timeout=.6, command=self.command('timeout_rotated'))
        self.assertEqual(value['accessToken'], json.loads(self.replacement.read_text())['tokens']['access_token'])
        self.assertEqual(json.loads(self.canonical.read_text()), json.loads(self.replacement.read_text()))

    def test_timeout_without_rotation_retains_recovery_state_and_never_retries_old_token(self):
        original = self.canonical.read_bytes()
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, timeout=.6, command=self.command('timeout_unchanged'))
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command())
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])
        self.assertEqual(self.canonical.read_bytes(), original)

    def test_canonical_revision_change_during_refresh_is_never_overwritten(self):
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command('canonical_changed'))
        self.assertEqual(self.canonical.read_text(), self.replacement.read_text() + '\n')

    def test_lock_wait_counts_against_total_timeout(self):
        import fcntl
        path = self.store.root / 'locks' / ('auth-refresh-' + self.ident + '.lock')
        core.directory(path.parent)
        with path.open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaises(core.UserError):
                self.credentials(force_refresh=True, timeout=.15, command=self.command())
            self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(self.count.exists())

    def test_live_legacy_official_instance_blocks_refresh(self):
        core.write_json(self.store.root / 'jobs' / ('a' * 32 + '.json'), {
            'id': self.ident, 'job': 'a' * 32, 'lifecycle_version': 1,
            'status': 'running', 'pid': os.getpid(), 'pid_start': core.process_stamp(os.getpid())})
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command())
        self.assertFalse(self.count.exists())

    def test_symlink_auth_is_rejected_without_reading_target(self):
        self.canonical.unlink()
        self.canonical.symlink_to(self.replacement)
        with self.assertRaises(core.UserError):
            self.credentials(command=self.command())
        self.assertFalse(self.count.exists())

    def test_successful_relogin_clears_only_the_obsolete_refresh_attempt(self):
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command('unchanged'))
        replacement = auth('explicit-relogin')
        core.write_json(self.canonical, replacement)
        before = self.canonical.read_bytes()
        official_auth.clear_recovery_after_login(self.store, self.ident)
        self.assertEqual(self.canonical.read_bytes(), before)
        self.assertEqual(self.credentials()['accessToken'], replacement['tokens']['access_token'])
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_recovery_cannot_be_cleared_without_a_new_successful_login(self):
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command('unchanged'))
        with self.assertRaises(core.UserError):
            official_auth.clear_recovery_after_login(self.store, self.ident)
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command())
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_native_proactive_refresh_is_not_followed_by_another_forced_refresh(self):
        core.write_json(self.canonical, auth('old', expires=time.time() + 200))
        value = self.credentials(force_refresh=True, command=self.command())
        self.assertNotEqual(value['accessToken'], self.initial['tokens']['access_token'])
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_old_shared_backend_lease_blocks_refresh_of_an_orphaned_v1_backend(self):
        import fcntl
        path = self.store.root / 'locks' / ('backend-' + self.ident + '.lock')
        with path.open('a') as backend:
            fcntl.flock(backend, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaises(core.UserError):
                self.credentials(force_refresh=True, timeout=.15, command=self.command())
        self.assertFalse(self.count.exists())

    def test_failed_proactive_refresh_is_not_retried_with_the_old_token(self):
        core.write_json(self.canonical, auth('old', expires=time.time() + 20))
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command('unchanged'))
        self.assertEqual(self.count.read_text().splitlines(), ['refresh'])

    def test_access_token_identity_must_agree_with_id_token_identity(self):
        replacement = auth('new')
        replacement['tokens']['access_token'] = synthetic_jwt('new', user='another-user')
        core.write_json(self.replacement, replacement)
        original = self.canonical.read_bytes()
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command())
        self.assertEqual(self.canonical.read_bytes(), original)

    def test_caller_crash_after_native_rotation_is_recovered_without_refreshing_again(self):
        import fcntl
        runner = self.root / 'caller.py'
        runner.write_text('''import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import core, official_auth
root=pathlib.Path(sys.argv[2]);ident=sys.argv[3]
store=core.Store(root/'control',root/'external',community=False)
official_auth.credentials_for_instance(store,ident,force_refresh=True,command=json.loads(sys.argv[4]))
''')
        proc = subprocess.Popen([sys.executable, '-I', '-B', str(runner),
            str(Path(core.__file__).parent), str(self.root), self.ident,
            json.dumps(self.command('crash_after_write'))], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            owner_auth = self.canonical.parent.parent / 'refresh-owner' / 'codex' / 'auth.json'
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if owner_auth.exists() and owner_auth.read_bytes() == self.replacement.read_bytes():
                    break
                time.sleep(.01)
            else:
                self.fail('Synthetic native process did not persist its rotated credential')
            proc.kill()
            proc.wait(timeout=1)
            # The orphan retains every safety lease until it exits, so a login
            # or legacy instance cannot start using the old canonical token.
            for prefix in ('account', 'backend'):
                path = self.store.root / 'locks' / (prefix + '-' + self.ident + '.lock')
                with path.open('a') as held:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            value = self.credentials(timeout=3, command=self.command())
            self.assertEqual(value['accessToken'], json.loads(self.replacement.read_text())['tokens']['access_token'])
            self.assertEqual(self.count.read_text().splitlines(), ['refresh'])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1)

    def test_relogin_cleanup_can_run_under_the_existing_exclusive_account_lease(self):
        import fcntl
        with self.assertRaises(core.UserError):
            self.credentials(force_refresh=True, command=self.command('unchanged'))
        core.write_json(self.canonical, auth('relogin'))
        path = self.store.root / 'locks' / ('account-' + self.ident + '.lock')
        with path.open('a') as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            official_auth.clear_recovery_after_login(self.store, self.ident, timeout=.3)
        self.assertEqual(self.credentials()['accessToken'], json.loads(self.canonical.read_text())['tokens']['access_token'])

    def test_successful_relogin_clears_an_ambiguous_spawn_after_its_lease_is_released(self):
        account = self.canonical.parent.parent
        original, raw, revision = official_auth._read(self.canonical)
        core.write_json(account / 'refresh-transaction.json', {
            'version': 1, 'phase': 'starting', 'source_revision': revision,
            'source_identity': official_auth._identity(original)})
        core.atomic_write(core.directory(account / 'refresh-owner' / 'codex') / 'auth.json', raw.decode())
        with self.assertRaises(core.UserError):
            self.credentials(command=self.command())
        core.write_json(self.canonical, auth('successful-relogin'))
        official_auth.clear_recovery_after_login(self.store, self.ident)
        self.assertFalse((account / 'refresh-transaction.json').exists())
        self.assertFalse((account / 'refresh-owner').exists())
        self.assertEqual(self.credentials()['accessToken'], json.loads(self.canonical.read_text())['tokens']['access_token'])

    def test_crash_before_journal_can_be_recovered_by_successful_relogin(self):
        class SimulatedCrash(BaseException):
            pass

        account = self.canonical.parent.parent
        owner = account / 'refresh-owner'
        journal = account / 'refresh-transaction.json'
        with patch('official_auth.write_json', side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                self.credentials(force_refresh=True, command=self.command())
        self.assertEqual(list(owner.iterdir()), [])
        self.assertFalse(journal.exists())
        with self.assertRaises(core.UserError):
            self.credentials(command=self.command())
        self.assertFalse(self.count.exists())
        # Only a confirmed native login while holding the exclusive account
        # lease authorizes clearing an orphan without a source revision.
        with self.store.edit_guard(self.ident):
            core.write_json(self.canonical, auth('successful-relogin'))
            logged_in = self.canonical.read_bytes()
            official_auth.clear_recovery_after_login(self.store, self.ident)
        self.assertFalse(owner.exists())
        self.assertEqual(self.canonical.read_bytes(), logged_in)
        self.assertEqual(self.credentials()['accessToken'], json.loads(logged_in)['tokens']['access_token'])
        self.assertFalse(self.count.exists())

    def test_successful_relogin_preserves_a_nonempty_owner_without_journal(self):
        owner = self.canonical.parent.parent / 'refresh-owner'
        orphan_auth = core.directory(owner / 'codex') / 'auth.json'
        core.write_json(orphan_auth, auth('untracked-orphan'))
        original = orphan_auth.read_bytes()
        with self.store.edit_guard(self.ident):
            core.write_json(self.canonical, auth('successful-relogin'))
            with self.assertRaises(core.UserError):
                official_auth.clear_recovery_after_login(self.store, self.ident)
        self.assertEqual(orphan_auth.read_bytes(), original)
        with self.assertRaises(core.UserError):
            self.credentials(command=self.command())
        self.assertFalse(self.count.exists())


if __name__ == '__main__':
    unittest.main()
