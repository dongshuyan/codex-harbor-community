"""Permission-path adaptation never edits message text or resolves child links."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import backend_guard


class BackendPaths(unittest.TestCase):
    def test_only_config_flags_can_override_ephemeral_auth(self):
        args=['-c','cli_auth_credentials_store = "file"','app-server',
              '--config=cli_auth_credentials_store="auto"','--config','other="unchanged"']
        self.assertEqual(backend_guard.ephemeral_arguments(args),[
            '-c','cli_auth_credentials_store="ephemeral"',
            '-c','cli_auth_credentials_store="ephemeral"','app-server',
            '--config=cli_auth_credentials_store="ephemeral"','--config','other="unchanged"'])
        prompt='cli_auth_credentials_store="file"'
        self.assertEqual(backend_guard.ephemeral_arguments(['exec',prompt])[-1],prompt)
        for option in ['-ccli_auth_credentials_store="file"','-c=cli_auth_credentials_store="file"']:
            args=[option,'app-server']
            self.assertTrue(backend_guard.is_stdio_app_server(args))
            self.assertIn('-ccli_auth_credentials_store="ephemeral"',backend_guard.ephemeral_arguments(args))
            self.assertTrue(backend_guard.is_stdio_app_server(['app-server',option]))
            self.assertEqual(backend_guard.ephemeral_arguments(['exec','--',option])[-1],option)

    def test_explicit_local_environment_paths_change_and_other_fields_are_preserved(self):
        alias = '/private/tmp/current/h'
        for method in ['thread/start', 'turn/start']:
            with self.subTest(method=method):
                payload = {'id':7, 'method':method, 'params':{
                    'cwd':'/safe/project',
                    'environments':[
                        {'environmentId':'remote-server', 'cwd':alias+'/worktrees/remote',
                         'runtimeWorkspaceRoots':[alias+'/visualizations/remote']},
                        {'environmentId':'local', 'cwd':alias+'/worktrees/demo',
                         'runtimeWorkspaceRoots':[alias, alias+'/visualizations/thread', '/safe/project'],
                         'sandboxPolicy':{'writableRoots':[alias+'/unknown-policy']},
                         'future':{'cwd':alias+'/unknown-field'}},
                        {'environmentId':'local', 'cwd':alias},
                    ],
                    'input':[{'type':'text', 'text':'Keep '+alias+'/visualizations/thread literal.'},
                             {'type':'localImage', 'path':alias+'/image.png'}],
                    'future':{'environments':[{'environmentId':'local', 'cwd':alias}]},
                }}
                raw = (json.dumps(payload, indent=2)+'\n').encode()
                expected = json.loads(raw)
                expected['params']['environments'][1]['cwd'] = '/real/home/worktrees/demo'
                expected['params']['environments'][1]['runtimeWorkspaceRoots'] = [
                    '/real/home', '/real/home/visualizations/thread', '/safe/project']
                expected['params']['environments'][2]['cwd'] = '/real/home'
                result = backend_guard.rewrite_permission_paths(raw, alias, '/real/home')
                self.assertEqual(json.loads(result), expected)
                self.assertEqual(json.loads(raw), payload)

    def test_nonlocal_missing_and_malformed_environment_ids_pass_unchanged(self):
        alias = '/private/tmp/current/h'
        environments = [{'cwd':alias, 'runtimeWorkspaceRoots':[alias+'/visualizations/thread']}]
        for environment_id in ['remote-server', 'LOCAL', 'local ', 'local/other', '', None, 0, True, [], {}]:
            environments.append({'environmentId':environment_id, 'cwd':alias,
                                 'runtimeWorkspaceRoots':[alias+'/visualizations/thread']})
        environments.extend([None, 'local', [], 7])
        for method in ['thread/start', 'turn/start']:
            with self.subTest(method=method):
                payload = {'method':method, 'params':{'environments':environments}}
                raw = (json.dumps(payload, indent=2)+'\n').encode()
                self.assertEqual(backend_guard.rewrite_permission_paths(raw, alias, '/real/home'), raw)

    def test_malformed_environment_containers_and_local_paths_pass_unchanged(self):
        alias = '/private/tmp/current/h'
        for environments in [None, {}, 'local', 1,
                             [{'environmentId':'local'}],
                             [{'environmentId':'local', 'cwd':None, 'runtimeWorkspaceRoots':alias}],
                             [{'environmentId':'local', 'cwd':{}, 'runtimeWorkspaceRoots':[None, 1, {}, []]}]]:
            with self.subTest(environments=environments):
                raw = (json.dumps({'method':'turn/start', 'params':{'environments':environments}}, indent=2)+'\n').encode()
                self.assertEqual(backend_guard.rewrite_permission_paths(raw, alias, '/real/home'), raw)

    def test_local_environment_path_boundaries_and_traversal_pass_unchanged(self):
        alias = '/private/tmp/current/h'
        paths = [alias+'-other/visualizations', '/private/tmp/old/h/visualizations',
                 alias+'/../outside', alias+'/./child', alias+'/child/../../outside',
                 alias+'/child/../sibling', '/safe/project', 'relative/path']
        for path in paths:
            with self.subTest(path=path):
                payload = {'method':'turn/start', 'params':{'environments':[
                    {'environmentId':'local', 'cwd':path, 'runtimeWorkspaceRoots':paths}]}}
                raw = (json.dumps(payload, indent=2)+'\n').encode()
                self.assertEqual(backend_guard.rewrite_permission_paths(raw, alias, '/real/home'), raw)

    def test_current_local_permission_paths_change_but_message_text_does_not(self):
        for method in ['thread/start', 'thread/resume', 'thread/fork', 'turn/start',
                       'thread/settings/update', 'command/exec']:
            with self.subTest(method=method):
                payload = {'id':7, 'method':method, 'params':{
                    'cwd':'/private/tmp/current/h/worktrees/demo',
                    'runtimeWorkspaceRoots':['/private/tmp/current/h/visualizations/t1', '/safe/project'],
                    'sandboxPolicy':{'type':'workspaceWrite', 'writableRoots':['/private/tmp/current/h/visualizations/t1']},
                    'input':[{'type':'text', 'text':'Keep /private/tmp/current/h/visualizations/t1 literal.'}],
                    'environments':[{'runtimeWorkspaceRoots':['/private/tmp/current/h/remote']}],
                }}
                raw = (json.dumps(payload)+'\n').encode()
                result = json.loads(backend_guard.rewrite_permission_paths(raw, '/private/tmp/current/h', '/real/home'))
                self.assertEqual(result['params']['cwd'], '/real/home/worktrees/demo')
                self.assertEqual(result['params']['runtimeWorkspaceRoots'], ['/real/home/visualizations/t1', '/safe/project'])
                self.assertEqual(result['params']['sandboxPolicy']['writableRoots'], ['/real/home/visualizations/t1'])
                self.assertEqual(result['params']['input'], payload['params']['input'])
                self.assertEqual(result['params']['environments'], payload['params']['environments'])

    def test_other_aliases_parent_traversal_and_unknown_messages_pass_unchanged(self):
        cases = [
            {'method':'turn/start', 'params':{'runtimeWorkspaceRoots':[
                '/private/tmp/current/h-other/visualizations', '/private/tmp/old/h/visualizations',
                '/private/tmp/current/h/../outside', '/private/tmp/current/h/./child']}},
            {'method':'custom/action', 'params':{'runtimeWorkspaceRoots':['/private/tmp/current/h/visualizations']}},
            {'method':'custom/action', 'params':{'environments':[
                {'environmentId':'local', 'cwd':'/private/tmp/current/h',
                 'runtimeWorkspaceRoots':['/private/tmp/current/h/visualizations']}] }},
            {'method':'turn/start', 'params':{'input':[{'text':'/private/tmp/current/h/visualizations'}]}},
            {'id':8, 'result':{'runtimeWorkspaceRoots':['/private/tmp/current/h/visualizations']}},
            {'method':[], 'params':{}},
            {'method':{}, 'params':{}},
        ]
        for payload in cases:
            raw = (json.dumps(payload, indent=2)+'\n').encode()
            self.assertEqual(backend_guard.rewrite_permission_paths(raw, '/private/tmp/current/h', '/real/home'), raw)
        for raw in [b'not JSON\n', b'[]\n', b'{"method":"turn/start","params":null}\n']:
            self.assertEqual(backend_guard.rewrite_permission_paths(raw, '/private/tmp/current/h', '/real/home'), raw)

    def test_nested_symlink_is_not_followed_by_the_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root/'home'; home.mkdir()
            outside = root/'outside'; outside.mkdir()
            (home/'escape').symlink_to(outside, target_is_directory=True)
            raw = json.dumps({'method':'turn/start', 'params':{
                'runtimeWorkspaceRoots':['/alias/h/escape/sub'],
                'environments':[{'environmentId':'local', 'cwd':'/alias/h/escape',
                                 'runtimeWorkspaceRoots':['/alias/h/escape/sub']}],
            }}).encode()
            result = json.loads(backend_guard.rewrite_permission_paths(raw, '/alias/h', str(home)))
            self.assertEqual(result['params']['runtimeWorkspaceRoots'], [str(home/'escape/sub')])
            self.assertEqual(result['params']['environments'][0]['cwd'], str(home/'escape'))
            self.assertEqual(result['params']['environments'][0]['runtimeWorkspaceRoots'], [str(home/'escape/sub')])
            self.assertNotIn(str(outside), json.dumps(result))

    def test_only_stdio_app_server_invocations_are_adapted(self):
        for args in [['app-server'], ['-c','features.code_mode_host=true','app-server','--analytics-default-enabled'],
                     ['app-server','-c','openai_base_url="http://localhost"','--stdio'],
                     ['app-server','--listen','stdio://']]:
            self.assertTrue(backend_guard.is_stdio_app_server(args), args)
        for args in [['--version'], ['app-server','generate-json-schema'], ['app-server','daemon'],
                     ['app-server','proxy'], ['app-server','--help'], ['app-server','--listen','ws://127.0.0.1:9'],
                     ['-c','app-server'], ['app-server','--unknown-option']]:
            self.assertFalse(backend_guard.is_stdio_app_server(args), args)

    def test_guard_forwards_json_and_eof_with_real_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root/'real home'; home.mkdir()
            alias = root/'h'; alias.symlink_to(home, target_is_directory=True)
            child = root/'fake-native'
            child.write_text('#!'+sys.executable+'\nimport sys\nfor line in sys.stdin: sys.stdout.write(line); sys.stdout.flush()\n')
            child.chmod(0o700)
            env = dict(os.environ, CODEX_HOME=str(home), HARBOR_DESKTOP_CODEX_HOME_ALIAS=str(alias))
            payload = {'id':1, 'method':'turn/start', 'params':{
                'runtimeWorkspaceRoots':[str(alias/'visualizations/thread')],
                'environments':[{'environmentId':'local', 'cwd':str(alias),
                                 'runtimeWorkspaceRoots':[str(alias/'visualizations/thread')]}],
            }}
            raw = (json.dumps(payload)+'\n').encode()
            command = [sys.executable, '-I', '-B', str(Path(backend_guard.__file__)), '-', '-', '-', str(child),
                       '-c', 'features.code_mode_host=true', 'app-server', '--analytics-default-enabled']
            result = subprocess.run(command, input=raw, capture_output=True, env=env, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            value = json.loads(result.stdout)
            self.assertEqual(value['params']['runtimeWorkspaceRoots'], [str(home/'visualizations/thread')])
            self.assertEqual(value['params']['environments'][0]['cwd'], str(home))
            self.assertEqual(value['params']['environments'][0]['runtimeWorkspaceRoots'], [str(home/'visualizations/thread')])

    def test_native_exit_keeps_zero_status_while_desktop_stdin_remains_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            home = root/'home'; home.mkdir()
            alias = root/'h'; alias.symlink_to(home, target_is_directory=True)
            child = root/'fake-native'
            child.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(0.2)\n')
            child.chmod(0o700)
            env = dict(os.environ, CODEX_HOME=str(home), HARBOR_DESKTOP_CODEX_HOME_ALIAS=str(alias))
            command = [sys.executable, '-I', '-B', str(Path(backend_guard.__file__)), '-', '-', '-', str(child), 'app-server']
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            try:
                code = proc.wait(timeout=8)
                self.assertEqual(code, 0, proc.stderr.read().decode())
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()
                for pipe in [proc.stdin, proc.stdout, proc.stderr]: pipe.close()

    def test_invalid_alias_stops_before_native_is_started(self):
        for case in ['wrong-target', 'public-parent', 'not-a-link']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                home = root/'home'; home.mkdir()
                other = root/'other'; other.mkdir()
                parent = root/'private'; parent.mkdir(mode=0o700)
                alias = parent/'h'
                if case == 'not-a-link': alias.mkdir()
                else: alias.symlink_to(other if case == 'wrong-target' else home, target_is_directory=True)
                if case == 'public-parent': parent.chmod(0o755)
                marker = root/'native-started'
                child = root/'fake-native'
                child.write_text('#!'+sys.executable+'\nfrom pathlib import Path\nPath('+repr(str(marker))+').touch()\n')
                child.chmod(0o700)
                env = dict(os.environ, CODEX_HOME=str(home), HARBOR_DESKTOP_CODEX_HOME_ALIAS=str(alias))
                command = [sys.executable, '-I', '-B', str(Path(backend_guard.__file__)), '-', '-', '-', str(child), 'app-server']
                result = subprocess.run(command, input=b'', capture_output=True, env=env, timeout=8)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
