from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import native_shortcut as entry


class Shortcut(unittest.TestCase):
    def test_success_waits_for_verified_catalog_and_never_relaunches(self):
        replies=[{'job':'synthetic','status':'starting'},
                 {'job':'synthetic','status':'running','pid':23,'catalog_readiness':{'status':'verified'}}]
        with patch.object(entry,'call',side_effect=replies) as call,patch.object(entry.time,'sleep'):
            result=entry.launch({'action':'native_launch','storage':'auto'})
        self.assertEqual(result,{'pid':23,'warning':None})
        self.assertEqual([v.args[0]['action'] for v in call.call_args_list],['native_launch','job_status'])

    def test_conflict_is_reported_without_bypassing_shared_entry(self):
        with patch.object(entry,'call',return_value={'job':'synthetic','status':'error','message':'Conflict'}) as call:
            with self.assertRaisesRegex(entry.EntryError,'Conflict'):entry.launch({'action':'native_launch'})
        self.assertEqual(call.call_count,1)
