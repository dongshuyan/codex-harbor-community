"""Native plugin paths use one instance identity without rewriting other data."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import backend_guard
import core


class NativePluginPaths(unittest.TestCase):
    alias = '/private/tmp/current/h'
    home = '/private/canonical-home'

    def test_read_and_install_normalize_only_the_typed_local_marketplace_path(self):
        for method in ('plugin/read', 'plugin/install'):
            with self.subTest(method=method):
                params = {
                    'marketplacePath': self.alias + '/.tmp/bundled-marketplaces/openai-bundled/.agents/plugins/marketplace.json',
                    'pluginName': 'browser',
                    'remoteMarketplaceName': None,
                    'future': {'marketplacePath': self.alias + '/literal'},
                    'cwd': self.alias,
                    'input': [{'text': self.alias}],
                }
                raw = json.dumps({'id': 3, 'method': method, 'params': params}).encode()
                result = json.loads(backend_guard.rewrite_permission_paths(raw, self.alias, self.home))
                expected = dict(params, marketplacePath=params['marketplacePath'].replace(self.alias, self.home, 1))
                self.assertEqual(result['params'], expected)

    def test_other_instances_remote_custom_paths_and_invalid_values_are_untouched(self):
        for method in ('plugin/read', 'plugin/install'):
            for path in (None, 3, {}, [], '', '/private/tmp/old/h/marketplace.json',
                         self.alias + '-other/marketplace.json', '/outside/custom/marketplace.json',
                         self.alias + '/../escape', self.alias + '/child/../escape',
                         self.alias + '/./marketplace.json', self.alias + '//marketplace.json',
                         self.alias + '/nul\x00', 'relative/marketplace.json',
                         self.alias.upper() + '/marketplace.json', 'https://example.invalid/marketplace'):
                with self.subTest(method=method, path=path):
                    raw = (json.dumps({'method': method, 'params': {
                        'marketplacePath': path, 'pluginName': 'browser',
                        'remoteMarketplaceName': None}}, indent=2) + '\n').encode()
                    self.assertEqual(backend_guard.rewrite_permission_paths(raw, self.alias, self.home), raw)

    def test_registration_config_and_unknown_protocol_fields_stay_native(self):
        for method, params in (
                ('marketplace/add', {'source': self.alias + '/.tmp/bundled-marketplaces/openai-bundled'}),
                ('config/value/write', {'keyPath': 'example', 'value': self.alias}),
                ('future/plugin', {'marketplacePath': self.alias + '/marketplace.json'}),
                ('plugin/read', {'future': {'marketplacePath': self.alias + '/marketplace.json'}})):
            raw = (json.dumps({'method': method, 'params': params}, indent=2) + '\n').encode()
            self.assertEqual(backend_guard.rewrite_permission_paths(raw, self.alias, self.home), raw)


class NativeFeatureDefaults(unittest.TestCase):
    def test_daily_launch_leaves_native_update_and_computer_use_lifecycle_enabled(self):
        prepared = {'os_home': '/synthetic/os-home', 'home': '/synthetic/codex',
                    'history': '/synthetic/history', 'electron': '/synthetic/electron',
                    'native_user_home': '/synthetic/user'}
        for mode in ({}, {'access_mode': 'native'}):
            env = core.environment(dict(prepared, **mode))
            self.assertNotIn('CODEX_SPARKLE_ENABLED', env)
            self.assertNotIn('CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH', env)
        for mode in ('readonly', 'workspace'):
            env = core.environment(dict(prepared, access_mode=mode))
            self.assertEqual(env['CODEX_SPARKLE_ENABLED'], 'false')
            self.assertEqual(env['CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH'], '1')


if __name__ == '__main__':
    unittest.main()
