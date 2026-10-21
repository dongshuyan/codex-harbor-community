"""Execute generated wrappers with a harmless child, never a native account."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import launcher


class BackendShellDefaults(unittest.TestCase):
    def test_native_backend_inherits_desktop_cwd_and_pins_only_its_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / 'private'; workspace.mkdir()
            prepared = dict(native_user_home=tmp, access_mode='native', workspace=str(workspace))
            pinned = dict(CODEX_HOME=str(root / 'account'), CODEX_SQLITE_HOME=str(root / 'history'))
            wrapper = root / 'backend'
            wrapper.write_text(launcher.backend_script(prepared, pinned, ['/bin/sh', '-c',
                'printf "%s|%s|%s|%s" "$PWD" "$CODEX_HOME" "${OPENAI_API_KEY:-absent}" "$ORDINARY_OPTION"']))
            wrapper.chmod(0o700)
            env = dict(HOME=tmp, PATH='/usr/bin:/bin', CODEX_HOME='/wrong',
                       OPENAI_API_KEY='SYNTHETIC_WRONG', ORDINARY_OPTION='keep')
            result = subprocess.run([str(wrapper)], cwd=root, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, f'{root}|{pinned["CODEX_HOME"]}|absent|keep')
            prepared['access_mode'] = 'readonly'
            wrapper.write_text(launcher.backend_script(prepared, pinned, ['/bin/pwd']))
            result = subprocess.run([str(wrapper)], cwd=root, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), str(workspace))


if __name__ == '__main__': unittest.main()
