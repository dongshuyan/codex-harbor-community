"""Native preferences survive relaunch without crossing account/history routes."""
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import configuration


class ConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        # Explicit fixture mode also works when this test ships in Community.
        # Its fake API key uses only this temporary tree, never the real Keychain.
        self.store = core.Store(self.root / 'control', self.root / 'external', mounted=lambda: True, community=False)
        for location in ['local', 'external']:
            self.store.initialize_storage(location)
        self.account = self.store.save_profile({'kind': 'official', 'name': 'Synthetic A'})

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, prepared, port=8100):
        if prepared['profile']['kind'] == 'api':
            core.configure(prepared, f'http://127.0.0.1:{port}/v1', 'SYNTHETIC_LOOPBACK')
        else:
            core.configure(prepared)
        return tomllib.loads((Path(prepared['home']) / 'config.toml').read_text())

    def edited(self, prepared):
        path = Path(prepared['home']) / 'config.toml'
        # Native settings now persist here, rather than only in global-state JSON.
        path.write_text('''# Synthetic user preference, keep a rollback copy.
model = "gpt-6-astra"
model_reasoning_effort = "max"
approval_policy = "on-request"
web_search = "cached"
[desktop]
enabled-reasoning-efforts = ["low", "medium", "high", "xhigh", "max"]
theme = "light"
[features]
shell_snapshot = true
[mcp_servers.example]
command = "example"
args = ["--example"]
''')
        return path.read_bytes()

    def test_max_desktop_and_user_settings_survive_relaunch_and_storage_change(self):
        first = self.store.prepare(self.account['id'], 'local')
        self.configure(first)
        original = self.edited(first)
        for location in ['local', 'external', 'local']:
            prepared = self.store.prepare(self.account['id'], location)
            value = self.configure(prepared)
            self.assertIn('max', value['desktop']['enabled-reasoning-efforts'])
            self.assertEqual(value['model_reasoning_effort'], 'max')
            self.assertEqual(value['model'], 'gpt-6-astra')
            self.assertEqual(value['web_search'], 'cached')
            self.assertTrue(value['features']['shell_snapshot'])
            self.assertEqual(value['mcp_servers']['example']['args'], ['--example'])
            self.assertEqual(value['sqlite_home'], prepared['history'])
        backup = Path(first['home']) / '.harbor-config-before-preservation.toml'
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual((Path(first['home']) / 'config.toml').stat().st_mode & 0o777, 0o600)

    def test_account_and_original_history_configuration_remain_independent(self):
        first = self.store.prepare(self.account['id'], 'local')
        self.configure(first)
        self.edited(first)
        history_config = Path(first['history']) / 'config.toml'
        history_config.write_text('# Original history configuration stays intact\n')
        other = self.store.save_profile({'kind': 'official', 'name': 'Synthetic B'})
        value = self.configure(self.store.prepare(other['id'], 'local'))
        self.assertNotIn('desktop', value)
        self.assertEqual(value['model_reasoning_effort'], 'low')
        self.assertEqual(history_config.read_text(), '# Original history configuration stays intact\n')

    def test_router_keeps_preferences_but_refreshes_transport_and_catalog(self):
        p = self.store.save_profile(dict(kind='api', name='Synthetic Router', protocol='responses',
            url='https://example.invalid/v1', api_key='SYNTHETIC_API_KEY', models=[
                dict(alias='gpt-6-astra', upstream='example/model', context=128000,
                     reasoning='standard', levels=['low', 'max'], effort='low')]))
        first = self.store.prepare(p['id'], 'local')
        self.configure(first)
        self.edited(first)
        value = self.configure(self.store.prepare(p['id'], 'local'), port=8200)
        self.assertIn('max', value['desktop']['enabled-reasoning-efforts'])
        self.assertEqual(value['model_reasoning_effort'], 'max')
        self.assertTrue(value['features']['shell_snapshot'])
        self.assertFalse(value['features']['enable_request_compression'])
        self.assertEqual(value['openai_base_url'], 'http://127.0.0.1:8200/v1')
        self.assertEqual(value['model_catalog_json'], str(Path(first['home']) / 'smart2-models.json'))
        self.assertNotIn('desktop', self.configure(self.store.prepare(p['id'], 'external')))

    def test_invalid_configuration_is_not_replaced_or_echoed(self):
        prepared = self.store.prepare(self.account['id'], 'local')
        path = Path(prepared['home']) / 'config.toml'
        original = 'SYNTHETIC_PRIVATE_INVALID_VALUE = [\n'
        path.write_text(original)
        with self.assertRaises(core.UserError) as error:
            self.configure(prepared)
        self.assertNotIn('SYNTHETIC_PRIVATE_INVALID_VALUE', str(error.exception))
        self.assertEqual(path.read_text(), original)

    def test_explicit_test_model_override_does_not_reset_desktop_preferences(self):
        first = self.store.prepare(self.account['id'], 'local')
        self.edited(first)
        value = self.configure(self.store.prepare(self.account['id'], 'local', 'gpt-6-luna'))
        self.assertEqual(value['model'], 'gpt-6-luna')
        self.assertEqual(value['model_reasoning_effort'], 'low')
        self.assertIn('max', value['desktop']['enabled-reasoning-efforts'])

    def test_removed_router_model_or_effort_falls_back_to_current_mapping(self):
        p = self.store.save_profile(dict(kind='api', name='Synthetic Router', protocol='responses',
            url='https://example.invalid/v1', api_key='SYNTHETIC_API_KEY', models=[
                dict(alias='gpt-6-luna', upstream='example/model', context=128000,
                     reasoning='standard', levels=['low'], effort='low')]))
        first = self.store.prepare(p['id'], 'local')
        self.edited(first)
        value = self.configure(first)
        self.assertEqual(value['model'], 'gpt-6-luna')
        self.assertEqual(value['model_reasoning_effort'], 'low')
        self.assertIn('max', value['desktop']['enabled-reasoning-efforts'])

    def test_test_overlay_can_be_withdrawn_without_discarding_user_preferences(self):
        prepared = self.store.prepare(self.account['id'], 'local')
        self.edited(prepared)
        prepared['access_mode'] = 'readonly'
        self.configure(prepared)
        prepared['access_mode'] = 'native'
        value = self.configure(prepared)
        for key in ['default_permissions', 'permissions', 'check_for_update_on_startup']:
            self.assertNotIn(key, value)
        self.assertIn('max', value['desktop']['enabled-reasoning-efforts'])
        self.assertTrue(value['features']['shell_snapshot'])

    def test_symlink_config_is_not_followed_and_invalid_managed_file_fails_closed(self):
        prepared = self.store.prepare(self.account['id'], 'local')
        path = Path(prepared['home']) / 'config.toml'
        outside = self.root / 'outside.toml'; outside.write_text('# intact\n')
        path.symlink_to(outside)
        with self.assertRaises((core.UserError, OSError)):
            self.configure(prepared)
        self.assertEqual(outside.read_text(), '# intact\n')
        path.unlink(); original = self.edited(prepared)
        (path.parent / configuration.MANAGED).write_text('invalid = [')
        with self.assertRaises(core.UserError):
            self.configure(prepared)
        self.assertEqual(path.read_bytes(), original)

    def test_toml_types_and_nested_tables_round_trip_without_losing_values(self):
        text = '''number = 1_050_000
ratio = 0.5
disabled = false
day = 2026-10-16
clock = 12:34:56.123
instant = 2026-10-16T12:34:56+08:00
escaped = "line\\nquote\\\""
mixed = [1, "two", {key = [true, false]}]
"a.b" = {nested = {text = "中文"}}
["mcp_servers"."with.dot"]
command = "example"
[[agents.entries]]
name = "one"
[[agents.entries]]
name = "two"
'''
        value = tomllib.loads(text)
        self.assertEqual(tomllib.loads(configuration.dumps(value)), value)


if __name__ == '__main__':
    unittest.main()
