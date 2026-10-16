"""Explicit macOS GUI-session diagnostic; never launches Codex or reads accounts.

Run: python3 -B tests/native_launch_responsibility_acceptance.py --keep-evidence
Add --red-direct to require independent responsibility from the old direct path.
The direct path mirrors the real Swift -> Python request -> detached worker ->
detached Python exec -> native app -> Python backend topology. It does not run
production preparation, authentication, or model code. LaunchServices runs only
new synthetic applications under /private/tmp; no protected access or TCC calls.
"""
import argparse
import ctypes
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CHAIN = r'''import ctypes,json,os,subprocess,sys
from pathlib import Path
mode,root=sys.argv[1:3]
base=Path(root)
assert str(base).startswith('/private/tmp/harbor-launch-responsibility-')
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
fn=lib.responsibility_get_pid_responsible_for_pid
fn.argtypes=[ctypes.c_int];fn.restype=ctypes.c_int
def report():
    return {'pid':os.getpid(),'ppid':os.getppid(),'sid':os.getsid(0),'rpid':fn(os.getpid())}
if mode=='backend':
    (base/(sys.argv[3]+'-backend.json')).write_text(json.dumps(report()))
elif mode=='request':
    subprocess.Popen([sys.executable,'-I','-B',str(base/'chain.py'),'worker',root,sys.argv[3]],start_new_session=True,close_fds=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
elif mode=='worker':
    (base/'worker.json').write_text(json.dumps(report()))
    child=subprocess.Popen([sys.executable,'-I','-B',str(base/'chain.py'),'desktop_child',root,sys.argv[3]],start_new_session=True,close_fds=True)
    try: child.wait(timeout=40)
    finally:
        if child.poll() is None: child.terminate();child.wait(timeout=5)
elif mode=='desktop_child':
    os.execv(sys.argv[3],[sys.argv[3],'target',root,'--user-data-dir='+root+'/direct/electron'])
else: raise RuntimeError('unknown fixture mode')
'''


def run(command, *, timeout, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Fixture command failed ({result.returncode}):\n'
                           + (result.stdout + result.stderr)[-5000:])
    return result


def executable(pid):
    # Only the fixture PIDs recorded below are queried; never enumerate processes.
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
    proc_pidpath = libproc.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    return buffer.value.decode() if proc_pidpath(pid, buffer, len(buffer)) > 0 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--red-direct', action='store_true')
    parser.add_argument('--keep-evidence', action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='harbor-launch-responsibility-', dir='/private/tmp'))
    try:
        (root / 'chain.py').write_text(CHAIN)
        for name in ('home', 'tmp'):
            (root / name).mkdir()
        for name in ('Driver', 'Target'):
            app = root / (name + '.app')
            binary = app / 'Contents/MacOS/Fixture'
            binary.parent.mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleExecutable': 'Fixture', 'CFBundleName': 'Synthetic ' + name,
                'CFBundleIdentifier': 'org.synthetic.harbor.responsibility.' + name.lower() + '.' + uuid.uuid4().hex,
                'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'LSUIElement': True,
            }))
            if name == 'Driver':
                run(['/usr/bin/swiftc', '-module-cache-path', str(root / 'module-cache'),
                     str(ROOT / 'tests/native_launch_responsibility_acceptance.swift'), '-o', str(binary)], timeout=90)
            else:
                shutil.copy2(root / 'Driver.app/Contents/MacOS/Fixture', binary)
            # Sign only these newly created synthetic bundles, never an installed app.
            run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)], timeout=10)
        environment = {'HOME': str(root / 'home'), 'CFFIXED_USER_HOME': str(root / 'home'),
                       'TMPDIR': str(root / 'tmp'), 'PATH': '/usr/bin:/bin',
                       'CODEX_HOME': str(root / 'driver/codex'), 'CODEX_SQLITE_HOME': str(root / 'history'),
                       'CODEX_CLI_PATH': str(root / 'driver/backend')}
        run(['/usr/bin/open', '-n', '-g', '-a', str(root / 'Driver.app'), '--args',
             'driver', str(root), sys.executable], timeout=10, env=environment)
        deadline = time.monotonic() + 30
        while not (root / 'driver-done.json').exists():
            errors = sorted(root.glob('error-*.json'))
            if errors:
                raise AssertionError(errors[0].read_text()[:2000])
            if time.monotonic() > deadline:
                raise AssertionError('Synthetic launch did not complete within 30 seconds')
            time.sleep(.05)

        def read(name):
            return json.loads((root / (name + '.json')).read_text())

        driver, worker, receipts = read('driver'), read('worker'), read('launched')
        driver_pid = driver['pid']
        target_path = str(root / 'Target.app/Contents/MacOS/Fixture')
        assert driver['rpid'] == driver_pid and driver['ppid'] == 1
        assert driver['responsible']['executable'] == str(root / 'Driver.app/Contents/MacOS/Fixture')
        assert worker['rpid'] == driver_pid and worker['sid'] == worker['pid']
        direct = read('direct')
        assert direct['rpid'] == driver_pid and direct['sid'] == direct['pid']
        assert direct['responsible']['executable'] == driver['responsible']['executable']
        assert direct['self']['executable'] == target_path
        assert read('direct-backend')['rpid'] == driver_pid
        assert receipts['direct_request_exit'] == receipts['open_request_exit'] == 0
        assert receipts['missing_app_callback_failed'] is True

        observations = []
        for slot in ('ls-one', 'ls-two', 'open'):
            value = read(slot)
            assert value['rpid'] == value['pid'] and value['ppid'] == 1, slot
            assert value['responsible'] == value['self'], slot
            assert value['responsible']['executable'] == target_path, slot
            assert read(slot + '-backend')['rpid'] == value['pid'], slot
            assert value['backend_exit'] == 0
            assert value['env'] == {'SYNTHETIC_SLOT': slot, 'CODEX_HOME': str(root / slot / 'codex'),
                                    'CODEX_SQLITE_HOME': str(root / 'history'),
                                    'CODEX_CLI_PATH': str(root / slot / 'backend')}, slot
            assert '--user-data-dir=' + str(root / slot / 'electron') in value['args'], slot
            assert executable(value['pid']) == target_path, slot
            if slot.startswith('ls-'):
                assert receipts[slot] == {'pid': value['pid'], 'bundle': str(root / 'Target.app'),
                                          'executable': target_path}, slot
            observations.append(value)
        assert len({value['pid'] for value in observations} | {direct['pid']}) == 4
        assert len({value['self']['cdhash'] for value in observations} | {direct['self']['cdhash']}) == 1
        summary = {'launchservices_instances': 3, 'workspace_callback_pids_verified': 2,
                   'missing_app_callback_error_verified': True,
                   'launchservices_and_backends_self_attributed': True,
                   'direct_and_backend_attributed_to_driver': True,
                   'distinct_private_environment_and_arguments': True,
                   'same_target_code_identity_all_instances': True,
                   'actual_tcc_prompt_tested': False, 'production_auth_or_desktop_used': False}
        (root / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, sort_keys=True))
        if args.red_direct:
            assert direct['rpid'] == direct['pid'], 'Direct Python launch attributes the native app to its launcher'
    finally:
        (root / 'stop').touch()
        # All fixture apps cooperate with this stop marker, including failures.
        recorded = []
        for name in ('driver', 'direct', 'ls-one', 'ls-two', 'open'):
            path = root / (name + '.json')
            if path.exists():
                value = json.loads(path.read_text())
                recorded.append((value['pid'], value['self']['executable']))
        deadline = time.monotonic() + 5
        while any(executable(pid) == path for pid, path in recorded) and time.monotonic() < deadline:
            time.sleep(.05)
        alive = [pid for pid, path in recorded if executable(pid) == path]
        if args.keep_evidence or alive:
            print('Synthetic evidence: ' + str(root))
        else:
            shutil.rmtree(root)
        if alive:
            raise RuntimeError('Synthetic fixture failed to exit after stop: ' + repr(alive))


if __name__ == '__main__':
    main()
