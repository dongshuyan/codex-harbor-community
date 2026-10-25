"""Never interpret untracked or unreadable native processes as an idle profile."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from core import UserError
import native_profile


class NativeProfile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'jobs').mkdir()
        self.home = self.root / 'native-home'
        self.home.mkdir()
        self.state = self.home / '.codex-global-state.json'
        self.state.write_text('{"private-setting":"preserve"}')
        self.original = self.state.read_bytes()

    def tearDown(self):
        self.assertEqual(self.state.read_bytes(), self.original)

    def record(self, *, home=None, stamp='start-a', electron=None):
        row = {'pid': 123, 'pid_start': stamp, 'home': str(home or self.home),
               'electron': str(electron or self.root / 'desktop'), 'desktop_launch_phase': 'running'}
        (self.root / 'jobs/fixture.json').write_text(json.dumps(row))

    def check(self, processes):
        with patch.object(native_profile, 'desktop_processes', return_value=processes):
            native_profile.require_idle(self.root, self.home)

    def test_no_desktop_is_idle(self):
        self.check([])

    def test_live_profile_blocks_even_when_launcher_is_closed(self):
        self.record()
        with self.assertRaises(UserError):
            self.check([{'pid': 123, 'start': 'start-a', 'argv': ['ChatGPT', '--user-data-dir=' + str(self.root/'desktop')]}])

    def test_another_tracked_private_instance_does_not_block(self):
        self.record(home=self.root / 'another-home')
        self.check([{'pid': 123, 'start': 'start-a', 'argv': ['ChatGPT', '--user-data-dir=' + str(self.root/'desktop')]}])

    def test_pid_reuse_is_not_evidence_of_a_known_profile(self):
        self.record(home=self.root / 'another-home', stamp='old-start')
        with self.assertRaises(UserError):
            self.check([{'pid': 123, 'start': 'start-a', 'argv': ['ChatGPT', '--user-data-dir=' + str(self.root/'desktop')]}])

    def test_unknown_native_app_blocks_without_reading_its_credentials(self):
        with self.assertRaises(UserError):
            self.check([{'pid': 123, 'start': 'start-a', 'argv': ['ChatGPT']}])

    def test_unreadable_native_arguments_fail_closed(self):
        with self.assertRaises(UserError):
            self.check([{'pid': 123, 'start': 'start-a', 'argv': None}])

    def test_record_and_actual_user_data_must_agree(self):
        self.record(home=self.root / 'another-home')
        with self.assertRaises(UserError):
            self.check([{'pid': 123, 'start': 'start-a', 'argv': ['ChatGPT', '--user-data-dir=/different/path']}])

    def test_parser_returns_argv_but_never_environment(self):
        block = ((3).to_bytes(4, sys.byteorder, signed=True) +
                 b'/bin/example\0\0example\0--user-data-dir\0/private/fixture\0'
                 b'API_KEY=SYNTHETIC_SECRET_DO_NOT_RETURN\0HOME=/user\0')
        args = native_profile.decode_argv(block)
        self.assertEqual(args, ['example', '--user-data-dir', '/private/fixture'])
        self.assertNotIn('SECRET', repr(args))

    def test_truncated_kernel_block_is_rejected(self):
        for block in (b'', b'\xff'*4, (2).to_bytes(4,sys.byteorder)+b'/bin/x\0\0x\0'):
            with self.subTest(block=block), self.assertRaises(ValueError):
                native_profile.decode_argv(block)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS process API')
    def test_real_kernel_arguments_for_owned_child_and_exit(self):
        proc = subprocess.Popen(['/bin/sleep', '30'], env={'SYNTHETIC_KEY':'DO_NOT_RETURN'})
        try:
            args = native_profile.process_argv(proc.pid)
            self.assertEqual(args[-1], '30')
            self.assertNotIn('DO_NOT_RETURN', repr(args))
        finally:
            proc.terminate();proc.wait(timeout=5)
        with self.assertRaises(OSError):
            native_profile.process_argv(proc.pid)


if __name__ == '__main__':
    unittest.main()
