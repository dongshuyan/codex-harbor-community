import plistlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from native_contract import require_supported,VERIFIED_DESKTOPS
from core import UserError


class Contract(unittest.TestCase):
    def test_unknown_desktop_build_is_not_assumed_compatible_from_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            app=Path(temp);(app/'Contents').mkdir()
            path=app/'Contents/Info.plist'
            path.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'future','CFBundleVersion':'99999'}))
            with self.assertRaisesRegex(UserError,'尚未通过'):require_supported(app)
            version=next(iter(VERIFIED_DESKTOPS))
            path.write_bytes(plistlib.dumps(dict(CFBundleShortVersionString=version[0],CFBundleVersion=version[1])))
            self.assertEqual(require_supported(app),version)
