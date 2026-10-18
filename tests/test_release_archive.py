"""Release archives retain program bytes/modes, not local filesystem metadata."""
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_artifacts as artifacts


@unittest.skipUnless(sys.platform == 'darwin', 'Requires the macOS archive tool')
class ReleaseArchive(unittest.TestCase):
    def test_archive_omits_xattrs_and_keeps_program_bytes_and_modes(self):
        with tempfile.TemporaryDirectory(prefix='harbor-archive-', suffix='.noindex',
                                         dir='/private/tmp') as temporary:
            root = Path(temporary)
            bundle = root / 'Synthetic.bundle'
            executable = bundle / 'Contents/MacOS/Synthetic'
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'#!/bin/sh\nexit 0\n')
            executable.chmod(0o755)
            attribute = 'com.codexharbor.synthetic'
            sentinel = b'SYNTHETIC_PRIVATE_METADATA'
            for path in (bundle, executable):
                subprocess.run(['/usr/bin/xattr', '-w', attribute, sentinel.decode(), str(path)], check=True)
            original = artifacts.inventory(bundle)

            archive = root / 'release.zip'
            artifacts.make_archive(bundle, archive)
            with zipfile.ZipFile(archive) as zipped:
                self.assertFalse(any('__MACOSX' in Path(name).parts or
                                     Path(name).name.startswith('._')
                                     for name in zipped.namelist()))
                self.assertFalse(any(sentinel in zipped.read(name)
                                     for name in zipped.namelist()))
            artifacts.extract_archive(archive, root / 'roundtrip')
            extracted = root / 'roundtrip' / bundle.name
            self.assertEqual(artifacts.inventory(extracted), original)
            for path in (extracted, extracted / executable.relative_to(bundle)):
                self.assertNotIn(attribute, subprocess.check_output(['/usr/bin/xattr', str(path)], text=True).splitlines())
            for path in (bundle, executable):
                self.assertEqual(subprocess.check_output(['/usr/bin/xattr', '-p', attribute, str(path)]).strip(), sentinel)


if __name__ == '__main__': unittest.main()
