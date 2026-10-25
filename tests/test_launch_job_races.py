"""Late helper receipts survive caller/worker cleanup; no app is launched."""
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import launcher
import storage


class LaunchJobRaces(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='harbor-job-race-', dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        originals = patch.object(storage, 'EXISTING_HOMES', {})
        originals.start()
        self.addCleanup(originals.stop)
        self.store = core.Store(self.root / 'control', self.root / 'external', mounted=lambda: True,
                                community=False, user_home=self.root / 'synthetic-home')
        self.store.initialize_storage('local')
        self.profile = self.store.save_profile({'kind': 'official', 'name': 'Synthetic job race'})
        self.job = 'b' * 32
        self.path = self.store.root / 'jobs' / (self.job + '.json')

    def receipt(self, phase):
        state = json.loads(self.path.read_text())
        state.update(lifecycle_version=3, desktop_spawn_attempted=True, desktop_launch_phase=phase,
                     launch_helper_pid=99999998, launch_helper_pid_start='synthetic-helper')
        if phase == 'exited':
            state.update(status='exited', pid=99999997, pid_start='synthetic-desktop')
        core.write_json(self.path, state)
        return state

    def test_worker_keeps_late_terminal_receipt_after_cleanup_wait_expires(self):
        core.write_json(self.path, {'job': self.job, 'id': self.profile['id'],
                                   'storage': 'local', 'status': 'starting'})
        app = self.root / 'Synthetic.app'
        binary = core.directory(app / 'Contents/MacOS') / 'Synthetic'
        binary.write_text('Synthetic fixture; never executed')
        binary.chmod(0o700)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Synthetic'}))
        cli = core.directory(app / 'Contents/Resources') / 'codex'
        cli.touch()
        cli.chmod(0o700)

        class Helper:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.exited = False
                self.failed_poll = False

            def poll(self):
                if not self.failed_poll:
                    self.failed_poll = True
                    raise OSError('Synthetic worker-side callback observation failure')
                return 0 if self.exited else None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                if timeout is None:
                    raise OSError('Synthetic worker-side wait failure')
                raise subprocess.TimeoutExpired('synthetic-helper', timeout)

        helper = Helper()
        terminal = []

        def spawn(*args, **kwargs):
            self.receipt('requested')
            return helper

        def callback_during_cleanup(*args, **kwargs):
            terminal.append(self.receipt('exited'))
            helper.exited = True

        def environment(prepared):
            return {'HOME': prepared['os_home'], 'ZDOTDIR': prepared['os_home'],
                    'CODEX_HOME': prepared['home'], 'CODEX_SQLITE_HOME': prepared['history']}

        with patch.object(launcher, 'APP', app), \
                patch('native_contract.require_supported',return_value=('synthetic','fixture')), \
                patch.object(launcher, 'snapshot', return_value='synthetic-backup'), \
                patch.object(launcher, 'process_stamp', return_value='synthetic-worker'), \
                patch.object(launcher, 'configure'), patch.object(launcher, 'environment', side_effect=environment), \
                patch.object(launcher.tempfile, 'mkdtemp', return_value=str(core.directory(self.root / 'ipc'))), \
                patch.object(launcher.subprocess, 'Popen', side_effect=spawn), \
                patch.object(launcher.signal, 'signal'), \
                patch('organization.Organization.publish', side_effect=callback_during_cleanup):
            launcher.run_instance(self.store, self.job, self.profile['id'], 'local', None)

        self.assertEqual(len(terminal), 1, 'The late callback must actually run during cleanup')
        self.assertEqual(json.loads(self.path.read_text()), terminal[0],
                         'Worker cleanup overwrote the helper terminal receipt with an older intent')

    def test_caller_keeps_callback_receipt_after_worker_exit(self):
        def spawn(*args, **kwargs):
            with patch.object(Path, 'read_text', original_read):
                self.receipt('requested')
            return SimpleNamespace(poll=lambda: 0)

        original_read = Path.read_text
        reads = 0
        terminal = []

        def callback_after_snapshot(path, *args, **kwargs):
            nonlocal reads
            result = original_read(path, *args, **kwargs)
            if path == self.path:
                reads += 1
                if reads == 2:
                    # The caller has its stale snapshot; the helper now finishes.
                    with patch.object(Path, 'read_text', original_read):
                        terminal.append(self.receipt('exited'))
            return result

        with patch.object(launcher.uuid, 'uuid4', return_value=SimpleNamespace(hex=self.job)), \
                patch.object(launcher.time, 'sleep'), \
                patch.object(launcher.subprocess, 'Popen', side_effect=spawn), \
                patch.object(Path, 'read_text', callback_after_snapshot):
            launcher.launch_request(self.store, self.profile['id'], 'local')

        self.assertEqual(len(terminal), 1, 'The helper callback must actually race the caller')
        self.assertEqual(json.loads(self.path.read_text()), terminal[0],
                         'Caller overwrote the helper receipt after observing worker exit')


if __name__ == '__main__':
    unittest.main()
