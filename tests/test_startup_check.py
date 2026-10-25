import json
import time
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import core
import startup_check


class StartupCheck(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.job=self.root/'job.json';self.out=self.root/'check.json'
        core.write_json(self.job,{'status':'running','home':str(self.root/'home'),'history':str(self.root/'history')})

    def test_completion_stops_polling_and_never_rewrites_launch_journal(self):
        before=self.job.read_bytes()
        with patch('startup_check.process_matches',return_value=True),patch('startup_check.check_once',side_effect=[{'status':'loading'},{'status':'verified'}]) as verify:
            startup_check.run(self.job,self.out,threading.Event(),timeout=1,interval=.001)
            self.assertEqual(verify.call_count,2)
        self.assertEqual(json.loads(self.out.read_text())['status'],'verified')
        self.assertEqual(self.job.read_bytes(),before)

    def test_timeout_is_unverified_not_success(self):
        with patch('startup_check.process_matches',return_value=True),patch('startup_check.check_once',return_value={'status':'loading'}):
            startup_check.run(self.job,self.out,threading.Event(),timeout=.01,interval=.001)
        self.assertEqual(json.loads(self.out.read_text())['status'],'unverified')

    def test_exited_instance_is_not_read_or_marked_ready(self):
        with patch('startup_check.process_matches',return_value=False),patch('startup_check.check_once') as verify:
            startup_check.run(self.job,self.out,threading.Event(),timeout=.1,interval=.001)
            verify.assert_not_called()
        self.assertEqual(json.loads(self.out.read_text())['status'],'cancelled')

    def test_blocked_read_is_terminated_within_budget_and_cannot_write_receipt(self):
        started=time.monotonic()
        result=startup_check.check_once({},threading.Event(),.1,
            command=[sys.executable,'-I','-c','import time;time.sleep(30)'])
        self.assertLess(time.monotonic()-started,2)
        self.assertEqual(result['status'],'unverified')
        self.assertFalse((self.root/'home/.harbor-catalog-receipt.json').exists())

    def test_cancellation_discards_a_completed_child_receipt(self):
        cancel=threading.Event()
        def completed(*args):
            cancel.set();return {'status':'verified','_receipt':{'version':2,'titles':{}}}
        with patch('startup_check.process_matches',return_value=True),patch('startup_check.check_once',side_effect=completed):
            startup_check.run(self.job,self.out,cancel,timeout=1)
        self.assertEqual(json.loads(self.out.read_text())['status'],'cancelled')
        self.assertFalse((self.root/'home/.harbor-catalog-receipt.json').exists())
