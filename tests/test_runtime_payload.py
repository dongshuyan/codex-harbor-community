"""Immutable runtime packages: real files, subprocess leases and lazy imports."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
import runtime_payload as runtime


CHILD = '''import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_payload as runtime
runtime.activate(Path(__file__).resolve().parent)
assert not any(key in os.environ for key in runtime.ENV), 'Runtime control environment leaked into native tools'
print("ready", flush=True)
for line in sys.stdin:
    if line.strip() == 'value':
        import deferred
        print(deferred.VALUE, flush=True)
    elif line.strip() == 'spawn':
        import subprocess
        print(subprocess.check_output([sys.executable, '-I', '-B',
            str(Path(__file__).with_name('late.py'))], text=True).strip(), flush=True)
'''


class RuntimePayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='harbor-payload-', dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / 'control'
        self.control.mkdir(mode=0o700)
        self.cache = runtime.Cache(self.control)

    def package(self, version='old', edition='private', coordination=1, complete=False):
        resource = self.root / ('package-' + version) / 'Contents/Resources'
        engine = resource / 'engine'
        engine.mkdir(parents=True)
        if complete:
            for name in json.loads((ROOT/'release_manifest.json').read_text())['runtime']:
                target=resource/name; target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(ROOT/name,target)
        (engine / 'edition.json').write_text(json.dumps({'edition': edition, 'version': version}))
        shutil.copyfile(ROOT / 'engine/runtime_payload.py', engine / 'runtime_payload.py')
        (engine / 'main.py').write_text(CHILD)
        (engine / 'deferred.py').write_text('VALUE = ' + repr(version))
        (engine / 'late.py').write_text('import sys; from pathlib import Path; sys.path.insert(0,str(Path(__file__).resolve().parent)); from deferred import VALUE; print(VALUE)')
        for name in ('HarborLaunch', 'HarborKeychain'):
            (resource / name).write_text('#!/bin/sh\nexit 0\n')
            (resource / name).chmod(0o755)
        paths = [str(p.relative_to(resource)) for p in engine.rglob('*') if p.is_file()] + ['HarborLaunch', 'HarborKeychain']
        runtime.write_manifest(resource, paths, coordination=coordination)
        return resource

    def spawn(self, lease):
        child = subprocess.Popen([sys.executable, '-I', '-B', str(lease.engine / 'main.py')],
                                 env={'HOME': str(self.root), **lease.environment()},
                                 pass_fds=lease.pass_fds, cwd=self.root,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close_child, child)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        return child

    @staticmethod
    def close_child(child):
        if child.poll() is None:
            child.stdin.close()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        child.stdout.close(); child.stderr.close()

    def test_running_code_can_lazy_import_after_original_app_resources_are_removed(self):
        source = self.package()
        with self.cache.acquire(source, edition='private') as lease:
            child = self.spawn(lease)
            pinned = lease.path
        shutil.rmtree(source.parent.parent)
        child.stdin.write('value\n'); child.stdin.flush()
        self.assertEqual(child.stdout.readline().strip(), 'old')
        child.stdin.write('spawn\n'); child.stdin.flush()
        self.assertEqual(child.stdout.readline().strip(), 'old')
        self.assertNotIn('.app', str(pinned))
        self.assertEqual((pinned / 'engine/deferred.py').read_text(), "VALUE = 'old'")

    def test_active_old_payload_survives_several_upgrades_and_is_reclaimed_after_exit(self):
        with self.cache.acquire(self.package(), edition='private') as lease:
            child = self.spawn(lease)
            old = lease.path
        for version in ('new', 'third', 'fourth'):
            with self.cache.acquire(self.package(version), edition='private') as current:
                self.assertNotEqual(current.path, old)
        self.assertTrue(old.exists())
        child.stdin.write('value\n'); child.stdin.flush()
        self.assertEqual(child.stdout.readline().strip(), 'old')
        self.close_child(child)
        with self.cache.acquire(self.package('fifth'), edition='private'):
            self.assertFalse(old.exists())
            self.assertEqual(len(list(self.cache.path.glob('[0-9a-f]' * 64))), 2)

    def test_relaunch_and_rollback_select_exact_payload_without_mutating_newer_one(self):
        old, new = self.package(), self.package('new')
        with self.cache.acquire(old, edition='private') as a:
            first = a.path
            with self.cache.acquire(new, edition='private') as b:
                second = b.path
                with self.cache.acquire(old, edition='private') as back:
                    self.assertEqual(back.path, first)
                    self.assertEqual((second / 'engine/deferred.py').read_text(), "VALUE = 'new'")

    def test_changed_source_is_rejected_and_inactive_cache_is_rebuilt_without_deleting_damage(self):
        source = self.package()
        (source / 'engine/deferred.py').write_text('untrusted')
        with self.assertRaises(runtime.PayloadError): self.cache.acquire(source, edition='private')
        source = self.package('new')
        with self.cache.acquire(source, edition='private') as lease: payload = lease.path
        target = payload / 'engine/deferred.py'
        target.chmod(0o600); target.write_text('damaged')
        with self.cache.acquire(source, edition='private') as recovered:
            self.assertEqual(recovered.path, payload)
            self.assertEqual((recovered.path / 'engine/deferred.py').read_text(), "VALUE = 'new'")
        saved = list(self.cache.path.glob('.damaged-*/engine/deferred.py'))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), 'damaged')

    def test_active_or_uncertain_damaged_cache_is_never_moved(self):
        source = self.package()
        with self.cache.acquire(source, edition='private') as lease:
            target = lease.path / 'engine/deferred.py'
            target.chmod(0o600); target.write_text('damaged')
            with self.assertRaises(runtime.PayloadError): self.cache.acquire(source, edition='private')
            self.assertTrue(lease.path.exists())
        jobs = self.control / 'jobs'; jobs.mkdir(mode=0o700)
        (jobs/'uncertain.json').write_text(json.dumps({'runtime_id':lease.path.name, 'desktop_launch_phase':'requested'}))
        with self.assertRaises(runtime.PayloadError): self.cache.acquire(source, edition='private')
        self.assertFalse(list(self.cache.path.glob('.damaged-*')))

    def test_cleanup_failure_does_not_prevent_current_payload_launch(self):
        paths = []
        for version in ('old', 'second'):
            with self.cache.acquire(self.package(version), edition='private') as lease: paths.append(lease.path)
        stage=self.cache.path/('.stage-'+'a'*32);stage.mkdir(mode=0o700)
        (stage/'.owned').write_text('harbor-runtime-v1\n')
        real = shutil.rmtree
        def cleanup(path, *args, **kwargs):
            if path in (paths[0], stage): raise PermissionError('synthetic cleanup failure')
            return real(path,*args,**kwargs)
        with patch.object(runtime.shutil,'rmtree',side_effect=cleanup):
            with self.cache.acquire(self.package('third'), edition='private') as lease:
                self.assertEqual((lease.engine/'deferred.py').read_text(), "VALUE = 'third'")
        self.assertTrue(paths[0].exists());self.assertTrue(stage.exists())

    def test_missing_payload_with_live_lease_blocks_compatibility_check(self):
        with self.cache.acquire(self.package(), edition='private') as lease:
            shutil.rmtree(lease.path)
            with self.assertRaises(runtime.PayloadError), runtime.installation_guard(self.control,1): pass

    def test_job_identity_uses_one_process_snapshot_and_matches_core_start_time(self):
        import core
        with self.cache.acquire(self.package(), edition='private') as lease: ident=lease.path.name
        jobs=self.control/'jobs';jobs.mkdir(mode=0o700)
        stamp=core.process_stamp(os.getpid())
        for i in range(100):
            (jobs/(str(i)+'.json')).write_text(json.dumps({'runtime_id':ident,'pid':100000+i,'pid_start':stamp}))
        (jobs/'live.json').write_text(json.dumps({'runtime_id':ident,'pid':os.getpid(),'pid_start':stamp}))
        real=runtime.subprocess.run
        with patch.object(runtime.subprocess,'run',wraps=real) as calls:
            with patch.dict(os.environ, {'LANG':'fr_FR.UTF-8','LC_ALL':'fr_FR.UTF-8'}):
                self.assertEqual(self.cache._job_roots(),{ident})
        self.assertEqual(calls.call_count,1)
        self.assertEqual(calls.call_args.args[0],['/bin/ps','-axo','pid=,lstart='])
        self.assertEqual(calls.call_args.kwargs['env']['LC_ALL'],'C')
        with patch.dict(os.environ, {'LANG':'fr_FR.UTF-8','LC_ALL':'fr_FR.UTF-8'}):
            self.assertEqual(core.process_stamp(os.getpid()),stamp)

    def test_blocked_intent_diagnostic_identifies_job_phase_and_runtime(self):
        with self.cache.acquire(self.package(), edition='private') as lease: ident=lease.path.name
        jobs=self.control/'jobs';jobs.mkdir(mode=0o700)
        (jobs/'uncertain.json').write_text(json.dumps({'runtime_id':ident,'desktop_launch_phase':'requested'}))
        with self.assertRaises(runtime.PayloadError) as raised:
            with runtime.installation_guard(self.control,None): pass
        for text in ('uncertain.json','requested',ident): self.assertIn(text,str(raised.exception))

    def test_runtime_inventory_contains_all_engine_modules_and_can_import_them_without_repo(self):
        import ast
        inventory=set(json.loads((ROOT/'release_manifest.json').read_text())['runtime'])
        modules={str(p.relative_to(ROOT)) for p in (ROOT/'engine').rglob('*.py')}
        self.assertLessEqual(modules,inventory)
        # Parse function-local and top-level imports; local modules must be present.
        local={p.stem for p in (ROOT/'engine').glob('*.py')}
        third_party=set()
        def runtime_nodes(node):
            # Type-checker-only imports (e.g. TOML Kit's _typeshed) are not runtime dependencies.
            if isinstance(node,ast.If) and (isinstance(node.test,ast.Name) and node.test.id=='TYPE_CHECKING'
                    or isinstance(node.test,ast.Attribute) and node.test.attr=='TYPE_CHECKING'):
                for child in node.orelse: yield from runtime_nodes(child)
                return
            yield node
            for child in ast.iter_child_nodes(node): yield from runtime_nodes(child)
        for name in modules:
            tree=ast.parse((ROOT/name).read_text())
            for node in runtime_nodes(tree):
                imports=([a.name.split('.')[0] for a in node.names] if isinstance(node,ast.Import)
                         else [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module and not node.level else [])
                for module in set(imports)&local:self.assertIn('engine/'+module+'.py',inventory)
                third_party.update(set(imports)-local-sys.stdlib_module_names)
        for name in ('models.json','context-catalog.json','cherry-catalog.json'):
            self.assertIn('engine/'+name,inventory)
        source=self.package(complete=True)
        # Replace synthetic entrypoint with the production one; no App/repo path in child imports.
        shutil.copyfile(ROOT/'engine/main.py',source/'engine/main.py')
        runtime.write_manifest(source)
        with self.cache.acquire(source,edition='private') as lease:
            vendored=sorted(name.removeprefix('engine/vendor/').removesuffix('.py').replace('/','.')
                            for name in modules if name.startswith('engine/vendor/') and not name.endswith('/__init__.py'))
            self.assertIn('tomlkit.parser',vendored)
            code='''import importlib,importlib.util,sys
from pathlib import Path
engine=Path(sys.argv[1]).resolve();sys.path.insert(0,str(engine))
import configuration
for name in '''+repr(sorted(local)+vendored)+''': importlib.import_module(name)
for name in '''+repr(sorted(third_party))+''':
    spec=importlib.util.find_spec(name)
    assert spec is not None and spec.origin is not None, name
    origin=Path(spec.origin).resolve()
    assert origin.is_relative_to(engine), (name,origin)
    assert 'engine/'+str(origin.relative_to(engine)) in '''+repr(inventory)+'''
print('ok')
'''
            result=subprocess.run([sys.executable,'-S','-I','-B','-c',code,str(lease.engine)],cwd=self.root,
                                  text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'ok')

    def test_unreadable_jobs_block_installer_even_for_matching_protocol(self):
        self.cache.setup()
        jobs=self.control/'jobs';jobs.mkdir(mode=0o700);(jobs/'broken.json').write_text('{')
        with self.assertRaisesRegex(runtime.PayloadError,'未更新或回退'):
            with runtime.installation_guard(self.control,1): pass

    def test_default_data_roots_match_validated_settings_in_both_editions(self):
        import distribution
        for community in (False,True):
            engine=self.root/('community' if community else 'private');engine.mkdir()
            if community:(engine/'COMMUNITY').write_text('community\n')
            settings=distribution.settings(engine)
            with patch.object(distribution,'EDITION',settings):
                self.assertEqual(distribution.default_root(),distribution.default_root(community=community))
                self.assertEqual(distribution.default_root().name,settings['data_directory'])

    def test_source_symlink_hardlink_and_path_escape_are_rejected(self):
        for kind in ('symlink', 'hardlink', 'escape'):
            with self.subTest(kind=kind):
                source = self.package(kind)
                target = source / 'engine/deferred.py'
                if kind != 'escape':
                    other = self.root / (kind + '-outside'); target.rename(other)
                    if kind == 'symlink': target.symlink_to(other)
                    else: os.link(other, target)
                else:
                    manifest = json.loads((source / runtime.MANIFEST).read_text())
                    manifest['files']['../outside'] = manifest['files'].pop('engine/deferred.py')
                    (source / runtime.MANIFEST).write_text(json.dumps(manifest))
                with self.assertRaises(runtime.PayloadError): self.cache.acquire(source, edition='private')

    def test_edition_and_active_coordination_mismatch_are_rejected(self):
        with self.assertRaises(runtime.PayloadError):
            self.cache.acquire(self.package('community', 'community'), edition='private')
        with self.cache.acquire(self.package(), edition='private'):
            with self.assertRaises(runtime.PayloadError):
                self.cache.acquire(self.package('breaking', coordination=2), edition='private')

    def test_only_allowlisted_code_is_copied(self):
        source = self.package()
        for name in ('auth.json', 'profiles.json', 'private.key'):
            (source / name).write_text('SYNTHETIC_PRIVATE_DATA')
        with self.cache.acquire(source, edition='private') as lease:
            for name in ('auth.json', 'profiles.json', 'private.key'):
                self.assertFalse((lease.path / name).exists())
            self.assertFalse(list(lease.path.rglob('*.app')))

    def test_interrupted_copy_is_never_published_and_next_attempt_recovers(self):
        source = self.package()
        with patch.object(runtime.shutil, 'copyfile', side_effect=OSError('simulated disk full')):
            with self.assertRaises(OSError): self.cache.acquire(source, edition='private')
        self.assertFalse(list(self.cache.path.glob('[0-9a-f]' * 64)))
        with self.cache.acquire(source, edition='private') as lease:
            self.assertEqual((lease.path / 'engine/deferred.py').read_text(), "VALUE = 'old'")

    def test_unknown_directory_is_preserved_during_gc(self):
        self.cache.setup()
        unknown = self.cache.path / ('f' * 64); unknown.mkdir()
        (unknown / 'precious').write_text('do not remove')
        link=self.cache.path / ('e'*64);link.symlink_to(self.root/'absent')
        for i in range(3):
            with self.cache.acquire(self.package(str(i)), edition='private'): pass
        self.assertEqual((unknown / 'precious').read_text(), 'do not remove')
        self.assertTrue(link.is_symlink())

    def test_live_desktop_identity_and_unresolved_intent_survive_without_any_lease(self):
        source = self.package()
        with self.cache.acquire(source, edition='private') as lease: old = lease.path
        jobs = self.control / 'jobs'; jobs.mkdir(mode=0o700)
        from core import process_stamp
        stamp = process_stamp(os.getpid())
        job = jobs / 'fixture.json'
        for state in ({'desktop_launch_phase': 'running', 'pid': os.getpid(), 'pid_start': stamp},
                      {'desktop_launch_phase': 'requested'}, {'desktop_launch_phase': 'pending'}):
            job.write_text(json.dumps({'runtime_id': old.name, **state}))
            for i in range(3):
                with self.cache.acquire(self.package('state-' + state['desktop_launch_phase'] + str(i)), edition='private'): pass
            self.assertTrue(old.is_dir())
        job.write_text(json.dumps({'runtime_id': old.name, 'desktop_launch_phase': 'exited'}))
        with self.cache.acquire(self.package('last'), edition='private'): self.assertFalse(old.exists())

    def test_malformed_job_retains_payload_instead_of_guessing_that_it_exited(self):
        with self.cache.acquire(self.package(), edition='private') as lease: old = lease.path
        jobs = self.control / 'jobs'; jobs.mkdir(mode=0o700)
        (jobs / 'broken.json').write_text('{')
        for i in range(3):
            with self.cache.acquire(self.package(str(i)), edition='private'): pass
        self.assertTrue(old.exists())

    def test_legacy_rollback_is_blocked_until_detached_process_finishes(self):
        with self.cache.acquire(self.package(), edition='private') as lease:
            child = self.spawn(lease)
        with self.assertRaises(runtime.PayloadError), runtime.installation_guard(self.control, None): pass
        with runtime.installation_guard(self.control, 1): pass
        with self.assertRaises(runtime.PayloadError), runtime.installation_guard(self.control, 2): pass
        self.close_child(child)
        with runtime.installation_guard(self.control, None): pass

    def test_child_without_inherited_fd_takes_lease_for_backend_respawn(self):
        with self.cache.acquire(self.package(), edition='private') as lease:
            command = [sys.executable, '-I', '-B', str(lease.engine / 'main.py')]
            child = subprocess.Popen(command, env={'HOME': str(self.root), **lease.environment(inherit=False)},
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(self.close_child, child)
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            old = lease.path
        for i in range(3):
            with self.cache.acquire(self.package(str(i)), edition='private'): pass
        self.assertTrue(old.exists())

    def test_missing_manifest_in_packaged_app_does_not_fall_back_to_development_mode(self):
        source = self.package()
        (source.parent / 'Info.plist').write_text('synthetic bundle marker')
        (source / runtime.MANIFEST).unlink()
        with self.assertRaises(runtime.PayloadError):
            runtime.for_launch(source / 'engine', self.control, community=False)

    def test_two_processes_publish_one_complete_payload(self):
        source = self.package()
        code = ('import sys; from pathlib import Path; sys.path.insert(0, ' + repr(str(ROOT / 'engine')) + '); '
                'import runtime_payload as r; x=r.Cache(Path(' + repr(str(self.control)) + ')).acquire(Path(' +
                repr(str(source)) + '),edition="private"); print(x.path.name); x.close()')
        children = [subprocess.Popen([sys.executable, '-I', '-B', '-c', code],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        outputs = []
        for child in children:
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err); outputs.append(out)
        self.assertEqual(len(set(outputs)), 1)
        self.assertFalse(list(self.cache.path.glob('.stage-*')))

    def test_two_runtime_versions_share_one_official_refresh_authority(self):
        from test_official_auth import auth, FIXTURE
        import core
        store=core.Store(self.control,self.root/'external',community=False)
        profile=store.save_profile({'kind':'official','name':'Synthetic mixed versions'})
        canonical=store.root/'accounts'/profile['id']/'codex/auth.json'
        old,new=auth('old'),auth('new')
        core.write_json(canonical,old)
        replacement=self.root/'replacement.json';core.write_json(replacement,new)
        fixture=self.root/'native.py';fixture.write_text(FIXTURE)
        count=self.root/'rotations.txt'
        command=[sys.executable,'-I','-B',str(fixture),'delay',str(replacement),str(count),str(canonical)]
        barrier=self.root/'go'
        children=[]
        code='''import hashlib,json,sys,time
from pathlib import Path
engine,control,external,profile,barrier,command=sys.argv[1:]
sys.path.insert(0,engine)
import runtime_payload as runtime
runtime.activate(Path(engine))
import core,official_auth
store=core.Store(Path(control),Path(external),community=False)
old=json.loads((store.root/'accounts'/profile/'codex/auth.json').read_text())['tokens']['access_token']
print('ready',flush=True)
while not Path(barrier).exists():time.sleep(.02)
value=official_auth.credentials_for_instance(store,profile,force_refresh=True,rejected_access_token=old,command=json.loads(command))
print(hashlib.sha256(value['accessToken'].encode()).hexdigest(),flush=True)
'''
        for version in ('old','new'):
            with self.cache.acquire(self.package(version,complete=True),edition='private') as lease:
                child=subprocess.Popen([sys.executable,'-I','-B','-c',code,str(lease.engine),str(store.root),str(store.external),
                    profile['id'],str(barrier),json.dumps(command)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    text=True,env={'HOME':str(self.root),'PATH':'/usr/bin:/bin',**lease.environment()},pass_fds=lease.pass_fds)
                self.addCleanup(self.close_child,child);children.append(child)
                self.assertEqual(child.stdout.readline().strip(),'ready')
        barrier.touch()
        import hashlib
        expected=hashlib.sha256(new['tokens']['access_token'].encode()).hexdigest()
        for child in children:
            out,err=child.communicate(timeout=12)
            self.assertEqual(child.returncode,0,err)
            self.assertEqual(out.strip(),expected)
        self.assertEqual(count.read_text().splitlines(),['refresh'])
        self.assertEqual(json.loads(canonical.read_text()),new)


if __name__ == '__main__': unittest.main()
