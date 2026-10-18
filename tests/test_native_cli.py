"""Only move configuration flags; preserve native command boundaries."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from native_cli import app_server_config_scope, app_server_index


class NativeCliScope(unittest.TestCase):
    def test_profile_value_is_not_the_subcommand_and_stays_in_root_scope(self):
        args = ['-p', 'app-server', '-c', 'model="before"', 'app-server',
                '--config=model="after"', '--listen', 'stdio://']
        expected = ['-p', 'app-server', 'app-server', '-c', 'model="before"',
                    '--config=model="after"', '--listen', 'stdio://']
        self.assertEqual(app_server_index(args), 4)
        self.assertEqual(app_server_config_scope(args), expected)
        self.assertEqual(app_server_config_scope(expected), expected)
        self.assertEqual(args[2:4], ['-c', 'model="before"'])

    def test_inline_forms_and_terminator_are_preserved(self):
        args = ['--profile=app-server', '-cfoo=1', '-c=bar=2', '--config=baz=3',
                'app-server', '--', '-cprompt=unchanged']
        expected = ['--profile=app-server', 'app-server', '-cfoo=1', '-c=bar=2',
                    '--config=baz=3', '--', '-cprompt=unchanged']
        self.assertEqual(app_server_config_scope(args), expected)

    def test_other_commands_and_unknown_prefixes_are_not_guessed(self):
        for args in (['exec', 'app-server'], ['-c', 'app-server'],
                     ['--', 'app-server'], ['--unknown', 'app-server']):
            self.assertIsNone(app_server_index(args))
            with self.assertRaises(ValueError):
                app_server_config_scope(args)


if __name__ == '__main__':
    unittest.main()
