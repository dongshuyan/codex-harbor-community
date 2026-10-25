import json
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import recovery
from core import UserError


class RecoveryTests(unittest.TestCase):
    def test_corrupted_saved_backup_blocks_launch(self):
        with tempfile.TemporaryDirectory() as tmp,patch('recovery.url_handler',return_value='com.openai.codex'),patch('recovery.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'<plist/>',b'')):
            manifest=Path(recovery.snapshot(tmp));data=json.loads(manifest.read_text())
            file=manifest.parent/data.get('file','com.openai.codex.plist')
            file.write_bytes(b'corrupt')
            with self.assertRaises(UserError):recovery.snapshot(tmp)

    def test_failed_export_never_creates_an_absent_domain_backup(self):
        with tempfile.TemporaryDirectory() as tmp,patch('recovery.url_handler',return_value='com.openai.codex'),patch('recovery.subprocess.run',return_value=subprocess.CompletedProcess([],1,b'',b'read failed')):
            with self.assertRaises(UserError):recovery.snapshot(tmp)
            self.assertFalse((Path(tmp)/'recovery/macos-before-first-launch.json').exists())

    def test_failed_manifest_publication_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp,patch('recovery.url_handler',return_value='com.openai.codex'),patch('recovery.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'<plist/>',b'')):
            with patch('recovery.write_json',side_effect=OSError('synthetic disk full')):
                with self.assertRaises(OSError):recovery.snapshot(tmp)
            self.assertTrue(Path(recovery.snapshot(tmp)).is_file())

    def test_restore_cannot_race_a_preparing_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            with recovery.lifecycle_lock(tmp):
                with self.assertRaises(UserError):recovery.restore(tmp)

    def test_restore_refuses_while_original_codex_runs(self):
        with tempfile.TemporaryDirectory() as tmp,patch('recovery.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'123',b'')):
            with self.assertRaises(UserError):recovery.restore(tmp)

    def test_restore_requires_readback_and_does_not_hide_mismatch(self):
        value=plistlib.dumps({'synthetic':'before'})
        for matches in [True,False]:
            with tempfile.TemporaryDirectory() as tmp,patch('recovery.url_handler',return_value='com.openai.codex'),patch('recovery.subprocess.run') as run:
                run.return_value=subprocess.CompletedProcess([],0,value,b'')
                recovery.snapshot(tmp)
                run.side_effect=[subprocess.CompletedProcess([],1,b'',b''),
                    subprocess.CompletedProcess([],0,b'',b''),
                    subprocess.CompletedProcess([],0,value if matches else plistlib.dumps({'synthetic':'different'}),b'')]
                if matches:self.assertTrue(recovery.restore(tmp)['restored'])
                else:
                    with self.assertRaises(UserError):recovery.restore(tmp)


if __name__=='__main__':unittest.main()
