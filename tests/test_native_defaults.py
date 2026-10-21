"""Synthetic native homes: only routing changes, source files stay untouched."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import core
import configuration
import native_defaults


class NativeDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.user = core.directory(self.root / 'real-user')
        self.store = core.Store(self.root / 'control', self.root / 'disk', mounted=lambda: True,
                                community=False, user_home=self.user)
        for location in ['local', 'external']:
            self.store.initialize_storage(location)
        self.account = self.store.save_profile({'kind': 'official', 'name': 'Synthetic A'})
        self.prepared = self.store.prepare(self.account['id'], 'local')
        self.home = Path(self.prepared['home'])
        self.source = Path(self.prepared['history'])
        self.source_config = self.source / 'config.toml'

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, prepared=None):
        prepared = prepared or self.prepared
        core.configure(prepared)
        return tomllib.loads((Path(prepared['home']) / 'config.toml').read_text())

    def test_bundled_marketplace_registration_belongs_to_current_native_home(self):
        source = str(self.source / '.tmp/bundled-marketplaces/openai-bundled')
        original = ('[marketplaces.openai-bundled]\nsource_type="local"\nsource=' + json.dumps(source) +
                    '\n[marketplaces.custom]\nsource_type="local"\nsource="custom-market"\n'
                    '[plugins."browser@openai-bundled"]\nenabled=false\n')
        self.source_config.write_text(original)
        value = self.configure()
        self.assertNotIn('openai-bundled', value.get('marketplaces', {}))
        self.assertEqual(value['marketplaces']['custom']['source'], str(self.source / 'custom-market'))
        self.assertFalse(value['plugins']['browser@openai-bundled']['enabled'])
        self.assertEqual(self.source_config.read_text(), original)

    def test_stale_native_bundled_registration_from_another_home_is_not_inherited(self):
        for name in ('openai-bundled', 'openai-bundled-alpha'):
            with self.subTest(name=name):
                original = configuration.dumps({'marketplaces': {name: {
                    'source_type': 'local',
                    'source': '/previous/home/.tmp/bundled-marketplaces/' + name,
                }}})
                self.source_config.write_text(original)
                self.assertNotIn(name, self.configure().get('marketplaces', {}))
                self.assertEqual(self.source_config.read_text(), original)

    def test_explicit_nonstandard_reserved_name_source_is_not_silently_removed(self):
        entry = {'source_type': 'local', 'source': '/user/explicit/custom-source'}
        self.source_config.write_text(configuration.dumps({'marketplaces': {'openai-bundled': entry}}))
        self.assertEqual(self.configure()['marketplaces']['openai-bundled'], entry)

    def test_withdraw_only_unchanged_ledger_proven_bundled_registration(self):
        entry = {'source_type': 'local', 'source': str(self.source / '.tmp/bundled-marketplaces/openai-bundled')}
        previous = {'marketplaces': {'openai-bundled': entry}}
        # Reproduce the old Harbor inheritance without touching original files.
        with patch.object(native_defaults, 'source_configuration', return_value=previous):
            self.configure()
        self.assertNotIn('openai-bundled', self.configure().get('marketplaces', {}))
        self.assertEqual(self.configure(), self.configure())

    def test_changed_bundled_registration_is_preserved_as_a_whole(self):
        entry = {'source_type': 'local', 'source': str(self.source / '.tmp/bundled-marketplaces/openai-bundled')}
        with patch.object(native_defaults, 'source_configuration', return_value={'marketplaces': {'openai-bundled': entry}}):
            self.configure()
        path = self.home / 'config.toml'
        changed = dict(entry, source='/user/explicit/custom-source')
        value = tomllib.loads(path.read_text()); value['marketplaces']['openai-bundled'] = changed
        core.atomic_write(path, configuration.patch_document(path.read_text(), value))
        self.assertEqual(self.configure()['marketplaces']['openai-bundled'], changed)
        self.assertEqual(self.configure()['marketplaces']['openai-bundled'], changed)

    def test_untracked_bundled_registration_is_not_removed(self):
        entry = {'source_type': 'local', 'source': str(self.source / '.tmp/bundled-marketplaces/openai-bundled')}
        (self.home / 'config.toml').write_text(configuration.dumps({'marketplaces': {'openai-bundled': entry}}))
        self.assertEqual(self.configure()['marketplaces']['openai-bundled'], entry)

    def test_source_defaults_and_route_isolation_without_original_writes(self):
        self.source_config.write_text('''model = "gpt-6-astra"
model_reasoning_effort = "max"
model_provider = "unrelated"
openai_base_url = "https://example.invalid"
forced_chatgpt_workspace_id = "SYNTHETIC_DIFFERENT_ACCOUNT"
[desktop]
enabled-reasoning-efforts = ["low", "max"]
theme = "light"
[projects."/synthetic/workspace"]
trust_level = "trusted"
[mcp_servers.fixture]
command = "example"
cwd = "tools"
[mcp_servers.fixture.env]
SYNTHETIC_PRIVATE_VALUE = "LOCAL_ONLY"
[shell_environment_policy.set]
CODEX_HOME = "/wrong-home"
OPENAI_API_KEY = "SYNTHETIC_WRONG_KEY"
SYNTHETIC_TOOL_OPTION = "keep"
[plugins.fixture]
enabled = false
remote_plugin_id = "SYNTHETIC_OTHER_ACCOUNT_PLUGIN"
''')
        (self.source / 'auth.json').write_text('DO NOT LOAD THIS AUTH FILE')
        before = {p.name: p.read_bytes() for p in [self.source_config, self.source / 'auth.json']}
        value = self.configure()
        self.assertEqual(value['model'], 'gpt-6-astra')
        self.assertEqual(value['model_reasoning_effort'], 'max')
        self.assertEqual(value['desktop']['enabled-reasoning-efforts'], ['low', 'max'])
        self.assertEqual(value['model_provider'], 'openai')
        self.assertNotIn('openai_base_url', value)
        self.assertNotIn('forced_chatgpt_workspace_id', value)
        self.assertEqual(value['shell_environment_policy']['set'], {'SYNTHETIC_TOOL_OPTION': 'keep'})
        self.assertEqual(value['plugins']['fixture'], {'enabled': False})
        self.assertEqual(value['mcp_servers']['fixture']['cwd'], str(self.source / 'tools'))
        self.assertEqual(value['mcp_servers']['fixture']['env']['SYNTHETIC_PRIVATE_VALUE'], 'LOCAL_ONLY')
        self.assertFalse((self.home / 'auth.json').exists())
        for name, content in before.items():
            self.assertEqual((self.source / name).read_bytes(), content)

    def test_local_overrides_and_removals_survive_source_changes_and_storage_switch(self):
        self.source_config.write_text('[desktop]\ntheme="light"\nshow-context-window-usage=true\n')
        self.configure()
        path = self.home / 'config.toml'
        value = tomllib.loads(path.read_text())
        value['desktop']['theme'] = 'dark'
        del value['desktop']['show-context-window-usage']
        path.write_text(configuration.patch_document(path.read_text(), value))
        self.source_config.write_text('[desktop]\ntheme="system"\nshow-context-window-usage=true\nnew-option=true\n')
        value = self.configure()
        self.assertEqual(value['desktop'], {'theme': 'dark', 'new-option': True})
        other = self.store.prepare(self.account['id'], 'external')
        (Path(other['history']) / 'config.toml').write_text('[desktop]\ntheme="light"\nshow-context-window-usage=true\nnew-option=false\n')
        value = self.configure(other)
        self.assertEqual(value['desktop'], {'theme': 'dark', 'new-option': False})
        self.assertEqual(value['sqlite_home'], other['history'])

    def test_existing_private_settings_take_priority_over_first_import(self):
        (self.home / 'config.toml').write_text('[desktop]\ntheme="dark"\n')
        self.source_config.write_text('[desktop]\ntheme="light"\nenabled-reasoning-efforts=["max"]\n')
        value = self.configure()
        self.assertEqual(value['desktop'], {'theme': 'dark', 'enabled-reasoning-efforts': ['max']})

    def test_comment_format_and_noop_mtime_preserved(self):
        raw = '# Native user comment\nmodel = "gpt-6-astra" # my choice\nmodel_reasoning_effort = "max"\n\n[desktop] # keep heading\ntheme  =  "dark"  # keep spaces\n'
        path = self.home / 'config.toml'; path.write_text(raw)
        self.configure()
        updated = path.read_text()
        self.assertIn('model = "gpt-6-astra" # my choice', updated)
        self.assertIn('[desktop] # keep heading\ntheme  =  "dark"  # keep spaces', updated)
        stamp = path.stat().st_mtime_ns
        self.configure()
        self.assertEqual(path.read_text(), updated)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_source_user_skills_links_keep_system_and_local_additions(self):
        skills = core.directory(self.source / 'skills')
        core.directory(skills / 'fixture-skill')
        core.directory(skills / '.system')
        private = core.directory(self.home / 'skills')
        core.directory(private / '.system')
        core.directory(private / 'private-skill')
        self.configure()
        self.assertTrue((private / 'fixture-skill').is_symlink())
        self.assertFalse((private / '.system').is_symlink())
        other = self.store.prepare(self.account['id'], 'external')
        self.configure(other)
        self.assertFalse(os.path.lexists(private / 'fixture-skill'))
        self.assertTrue((private / 'private-skill').is_dir())
        self.assertTrue((skills / 'fixture-skill').is_dir())

    def test_instructions_rules_are_private_copies_preserving_user_changes(self):
        (self.source / 'AGENTS.md').write_text('SYNTHETIC GLOBAL INSTRUCTION')
        rules = core.directory(self.source / 'rules')
        (rules / 'default.rules').write_text('SYNTHETIC RULE')
        self.configure()
        self.assertEqual((self.home / 'AGENTS.md').read_text(), 'SYNTHETIC GLOBAL INSTRUCTION')
        self.assertFalse((self.home / 'AGENTS.md').is_symlink())
        (self.home / 'AGENTS.md').write_text('PRIVATE EDIT')
        (self.source / 'AGENTS.md').write_text('UPDATED SOURCE')
        (rules / 'default.rules').write_text('UPDATED RULE')
        self.configure()
        self.assertEqual((self.home / 'AGENTS.md').read_text(), 'PRIVATE EDIT')
        self.assertEqual((self.home / 'rules/default.rules').read_text(), 'UPDATED RULE')
        self.assertEqual((self.source / 'AGENTS.md').read_text(), 'UPDATED SOURCE')

    def test_native_home_and_normal_zsh_customizations_cannot_change_window_route(self):
        (self.user / '.zshenv').write_text('export HARBOR_SYNTHETIC_GLOBAL=loaded\n')
        (self.user / '.zshrc').write_text('export CODEX_HOME=/wrong\nexport OPENAI_API_KEY=SYNTHETIC_WRONG_KEY\nexport HARBOR_SYNTHETIC_INTERACTIVE=loaded\n')
        self.configure()
        env = core.environment(self.prepared)
        self.assertEqual(env['HOME'], str(self.user))
        self.assertNotIn('CFFIXED_USER_HOME', env)
        result = subprocess.run(['/bin/zsh', '-lic', 'print -r -- "$HOME|$CODEX_HOME|${OPENAI_API_KEY:-absent}|$HARBOR_SYNTHETIC_GLOBAL|$HARBOR_SYNTHETIC_INTERACTIVE"'],
                                env=env, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), f'{self.user}|{self.home}|absent|loaded|loaded')

    def test_invalid_source_is_preserved_and_dotfile_links_are_read_only(self):
        (self.home / 'config.toml').write_text('# KEEP\n')
        self.source_config.write_text('SYNTHETIC_PRIVATE_INVALID=[\n')
        with self.assertRaises(core.UserError) as context:
            self.configure()
        self.assertNotIn('SYNTHETIC_PRIVATE_INVALID', str(context.exception))
        self.assertEqual((self.home / 'config.toml').read_text(), '# KEEP\n')
        self.source_config.unlink()
        target = self.root / 'untouched'; target.write_text('# KEEP\n')
        self.source_config.symlink_to(target)
        self.configure()
        self.assertEqual(target.read_text(), '# KEEP\n')
        private = self.home / 'config.toml'; private.unlink(); private.symlink_to(target)
        with self.assertRaises(OSError): self.configure()
        self.assertEqual(target.read_text(), '# KEEP\n')

    def test_resource_ledger_cannot_escape_private_home(self):
        (self.home / native_defaults.FILES).write_text(json.dumps({'links': {'../outside': '/tmp'}, 'files': {}}))
        with self.assertRaises(core.UserError): self.configure()


if __name__ == '__main__':
    unittest.main()
