"""Exercise the production HarborLaunch helper with synthetic apps and locks.

Run explicitly in a macOS GUI session:
    python3 -B tests/native_launch_helper_acceptance.py --keep-evidence

No installed desktop, account, protected folder, network request, or TCC access
is used. Every child app and every writable fixture is created in /private/tmp.
"""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
TARGET_SOURCE = r'''import AppKit
import Darwin
import Foundation
let environment = ProcessInfo.processInfo.environment
guard let directory = environment["SYNTHETIC_CASE_DIR"],
      directory.hasPrefix("/private/tmp/harbor-launch-helper-") else { exit(2) }
func save(_ value: [String:Any], _ name: String) throws {
    try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        .write(to: URL(fileURLWithPath: directory + "/" + name), options: .atomic)
}
typealias Query = @convention(c) (pid_t) -> pid_t
guard let handle = dlopen("/usr/lib/libSystem.B.dylib", RTLD_NOW),
      let symbol = dlsym(handle, "responsibility_get_pid_responsible_for_pid") else { exit(3) }
let responsible = unsafeBitCast(symbol, to: Query.self)(getpid())
let keys = ["CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_CLI_PATH", "SYNTHETIC_CASE_DIR"]
let value: [String:Any] = ["pid": getpid(), "ppid": getppid(), "rpid": responsible,
    "args": CommandLine.arguments,
    "env": Dictionary(uniqueKeysWithValues: keys.map { ($0, environment[$0] ?? "") })]
do {
    try save(value, "entered.json")
    if environment["SYNTHETIC_BEHAVIOR"] == "exit-before-checkin" { exit(7) }
    if environment["SYNTHETIC_BEHAVIOR"] == "delay-checkin" { Thread.sleep(forTimeInterval: 2) }
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.finishLaunching()
    try save(value, "ready.json")
    let deadline = Date().addingTimeInterval(directory.hasSuffix("/bystander") ? 90 : 35)
    _ = Timer.scheduledTimer(withTimeInterval: 0.02, repeats: true) { _ in
        if FileManager.default.fileExists(atPath: directory + "/stop") || Date() >= deadline {
            NSApplication.shared.terminate(nil)
        }
    }
    app.run()
} catch {
    try? save(["error": String(describing:error)], "target-error.json")
    exit(4)
}
'''

WORKER_SOURCE = r'''import fcntl,json,os,subprocess,sys,time
from pathlib import Path
case=Path(sys.argv[1])
assert str(case).startswith('/private/tmp/harbor-launch-helper-')
spec=json.loads((case/'spec.json').read_text())
def save(name,value):
    temporary=case/(name+'.tmp')
    temporary.write_text(json.dumps(value))
    os.replace(temporary,case/name)
def stamp(pid):
    result=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=3)
    if result.returncode: raise RuntimeError('fixture process disappeared')
    return result.stdout.strip()
handles=[]
try:
    for name in ('combo','lifecycle','account','slot'):
        handle=(case/(name+'.lock')).open('a')
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        handles.append(handle)
    state={'job':spec['job'],'id':'a'*32,'storage':'local','status':'starting',
           'created_at':time.time(),'worker_pid':os.getpid(),'worker_pid_start':stamp(os.getpid()),
           'desktop_spawn_attempted':True,'lifecycle_version':3}
    save('job.json',state)
    with (case/'helper.stdout').open('wb') as out,(case/'helper.stderr').open('wb') as err:
        child=subprocess.Popen([spec['helper'],str(case/'job.json')],stdin=subprocess.PIPE,
                               stdout=out,stderr=err,pass_fds=tuple(h.fileno() for h in handles))
        save('worker-ready.json',{'pid':os.getpid(),
            'helper_pid':child.pid,'helper_start':stamp(child.pid)})
        child.stdin.write(json.dumps(spec['request']).encode());child.stdin.close()
        code=child.wait(timeout=40)
        save('worker-done.json',{'helper_exit':code})
finally:
    for handle in handles: handle.close()
'''


def run(command, *, timeout, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Fixture command failed ({result.returncode}):\n'
                           + (result.stdout + result.stderr)[-5000:])
    return result


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def wait_for(predicate, label, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.03)
    raise AssertionError('Timed out: ' + label)


def executable(pid):
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
    query = libproc.proc_pidpath
    query.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    return buffer.value.decode() if query(pid, buffer, len(buffer)) > 0 else None


def held_locks(case):
    held = set()
    for name in ('combo', 'lifecycle', 'account', 'slot'):
        with (case / (name + '.lock')).open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.add(name)
    return held


def all_locks_free(case):
    return not held_locks(case)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep-evidence', action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='harbor-launch-helper-', dir='/private/tmp'))
    cases = []
    completed = False
    try:
        source = ROOT / 'native/Sources/HarborLaunch/main.swift'
        assert source.is_file(), 'Production HarborLaunch source has not been implemented'
        source_snapshot = root / 'production-source/main.swift'
        source_snapshot.parent.mkdir()
        source_bytes = source.read_bytes()
        source_snapshot.write_bytes(source_bytes)
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        (root / 'helper-source.sha256').write_text(source_hash + '\n')
        helper = root / 'HarborLaunch'
        app = root / 'Target.app'
        target = app / 'Contents/MacOS/Fixture'
        target.parent.mkdir(parents=True)
        (root / 'Target.swift').write_text(TARGET_SOURCE)
        (root / 'worker.py').write_text(WORKER_SOURCE)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleExecutable': 'Fixture', 'CFBundleName': 'Synthetic Harbor Launch Target',
            'CFBundleIdentifier': 'org.synthetic.harbor.helper.' + uuid.uuid4().hex,
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'LSUIElement': True,
        }))
        for input_path, output_path in ((source_snapshot, helper), (root / 'Target.swift', target)):
            run(['/usr/bin/swiftc', '-module-cache-path', str(root / 'module-cache'),
                 str(input_path), '-o', str(output_path)], timeout=90)
        run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)], timeout=10)
        for name in ('home', 'tmp'):
            (root / name).mkdir()
        environment = {'HOME': str(root / 'home'), 'CFFIXED_USER_HOME': str(root / 'home'),
                       'TMPDIR': str(root / 'tmp'), 'PATH': '/usr/bin:/bin'}

        def prepare(name, behavior='wait', application=None):
            case = root / name
            case.mkdir()
            app_environment = dict(environment, SYNTHETIC_CASE_DIR=str(case), SYNTHETIC_BEHAVIOR=behavior,
                                   CODEX_HOME=str(case / 'codex'), CODEX_SQLITE_HOME=str(root / 'history'),
                                   CODEX_CLI_PATH=str(case / 'backend'))
            request = {'app': str(application or app), 'environment': app_environment,
                       'arguments': ['--user-data-dir=' + str(case / 'electron')], 'activates': False}
            (case / 'spec.json').write_text(json.dumps({'helper': str(helper), 'job': 'job',
                                                       'request': request}))
            return case, request

        def launch(name, behavior='wait', application=None):
            case, request = prepare(name, behavior, application)
            log = (case / 'worker.stderr').open('wb')
            worker = subprocess.Popen([sys.executable, '-I', '-B', str(root / 'worker.py'), str(case)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                      env=environment, start_new_session=True)
            cases.append((case, worker, log))
            wait_for(lambda: read(case / 'worker-ready.json'), name + ' worker ready')
            return case, worker, request

        def running(case, request):
            entered = wait_for(lambda: read(case / 'ready.json'), case.name + ' target ready')
            state = wait_for(lambda: (s if (s := read(case / 'job.json')) and s.get('pid') else None),
                             case.name + ' ownership receipt')
            assert state['pid'] == entered['pid'] and state.get('pid_start'), state
            assert entered['rpid'] == entered['pid'] and entered['ppid'] == 1, entered
            assert executable(entered['pid']) == str(target)
            assert entered['env'] == {key: request['environment'][key] for key in entered['env']}
            assert request['arguments'][0] in entered['args']
            assert held_locks(case) == {'combo', 'lifecycle', 'account', 'slot'}, 'A running app lost leases'
            return entered, state

        def finished(case, worker, target_pid=None, *, read_state=True):
            worker.wait(timeout=12)
            if target_pid:
                wait_for(lambda: executable(target_pid) != str(target), case.name + ' target exit')
            wait_for(lambda: all_locks_free(case), case.name + ' lease release')
            metadata = read(case / 'worker-ready.json')
            wait_for(lambda: executable(metadata['helper_pid']) != str(helper), case.name + ' helper exit')
            return read(case / 'job.json') if read_state else None

        # This third instance remains alive while other workers are killed.
        bystander, bystander_request = prepare('bystander')
        run(['/usr/bin/open', '-n', '-g', '-a', str(app), '--args', *bystander_request['arguments']],
            timeout=10, env=bystander_request['environment'])
        bystander_pid = wait_for(lambda: read(bystander / 'ready.json'), 'bystander ready')['pid']

        first, worker_one, request_one = launch('first')
        first_value, _ = running(first, request_one)
        second, worker_two, request_two = launch('second')
        second_value, _ = running(second, request_two)
        assert len({first_value['pid'], second_value['pid'], bystander_pid}) == 3
        (first / 'stop').touch()
        finished(first, worker_one, first_value['pid'])
        assert executable(second_value['pid']) == executable(bystander_pid) == str(target)
        (second / 'stop').touch()
        finished(second, worker_two, second_value['pid'])

        before, before_worker, _ = launch('parent-death-before-callback', 'delay-checkin')
        before_pid = wait_for(lambda: read(before / 'entered.json'), 'delayed target entered')['pid']
        pending = read(before / 'job.json')
        assert not pending.get('pid'), 'Delayed check-in did not exercise the pending-callback window'
        before_worker.terminate()
        before_worker.wait(timeout=3)
        assert held_locks(before) == {'combo', 'lifecycle', 'account', 'slot'}, 'Helper dropped pending-launch leases'
        finished(before, before_worker, before_pid)
        assert executable(bystander_pid) == str(target)

        after, after_worker, after_request = launch('parent-death-after-callback')
        after_value, _ = running(after, after_request)
        after_worker.terminate()
        finished(after, after_worker, after_value['pid'])
        assert executable(bystander_pid) == str(target)

        cancelled, cancelled_worker, _ = launch('signal-before-callback', 'delay-checkin')
        cancelled_pid = wait_for(lambda: read(cancelled / 'entered.json'), 'cancelled target entered')['pid']
        helper_pid = read(cancelled / 'worker-ready.json')['helper_pid']
        assert not read(cancelled / 'job.json').get('pid') and executable(helper_pid) == str(helper)
        os.kill(helper_pid, signal.SIGTERM)
        assert not all_locks_free(cancelled)
        finished(cancelled, cancelled_worker, cancelled_pid)
        assert executable(bystander_pid) == str(target)

        unwritable, unwritable_worker, _ = launch('receipt-write-failure', 'delay-checkin')
        unwritable_pid = wait_for(lambda: read(unwritable / 'entered.json'), 'write-failure target entered')['pid']
        assert not read(unwritable / 'job.json').get('pid')
        (unwritable / 'job.json').rename(unwritable / 'original-intent.json')
        (unwritable / 'job.json').mkdir()
        finished(unwritable, unwritable_worker, unwritable_pid, read_state=False)
        assert read(unwritable / 'worker-done.json')['helper_exit'] != 0
        assert executable(bystander_pid) == str(target)

        missing, missing_worker, _ = launch('missing-app', application=root / 'Missing.app')
        missing_state = finished(missing, missing_worker)
        assert not missing_state.get('pid'), 'A missing app acquired a running receipt'
        assert read(missing / 'worker-done.json')['helper_exit'] != 0

        early, early_worker, _ = launch('exit-before-checkin', 'exit-before-checkin')
        early_pid = wait_for(lambda: read(early / 'entered.json'), 'early-exit target entered')['pid']
        early_state = finished(early, early_worker, early_pid)
        assert read(early / 'worker-done.json')['helper_exit'] != 0
        assert early_state.get('desktop_launch_phase') != 'requested', early_state
        assert executable(bystander_pid) == str(target)
        (bystander / 'stop').touch()
        wait_for(lambda: executable(bystander_pid) != str(target), 'bystander cooperative exit')
        summary = {'production_helper_used': True, 'two_private_instances_verified': True,
                   'production_helper_sha256': source_hash,
                   'parent_death_before_and_after_callback': True, 'missing_and_early_exit_failures': True,
                   'signal_before_callback_and_receipt_write_failure': True,
                   'all_inherited_leases_released': True, 'unrelated_fixture_survived': True,
                   'actual_tcc_prompt_tested': False, 'real_accounts_or_desktop_used': False}
        (root / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, sort_keys=True))
        completed = True
    finally:
        # Only cooperate with and reap processes created by this exact fixture.
        for case in root.iterdir():
            if case.is_dir() and (case / 'spec.json').exists():
                (case / 'stop').touch()
        for case, worker, log in cases:
            if worker.poll() is None:
                worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=3)
            log.close()
        # On an assertion failure, bound cleanup to the exact synthetic executable
        # paths. Never enumerate processes or signal an unrelated PID/group.
        fixture_processes = []
        for case in root.iterdir():
            if not case.is_dir() or not (case / 'spec.json').exists():
                continue
            metadata = read(case / 'worker-ready.json')
            if metadata:
                fixture_processes.append((metadata['helper_pid'], str(root / 'HarborLaunch')))
            metadata = read(case / 'entered.json')
            if metadata:
                fixture_processes.append((metadata['pid'], str(root / 'Target.app/Contents/MacOS/Fixture')))
        deadline = time.monotonic() + 3
        while any(executable(pid) == path for pid, path in fixture_processes) and time.monotonic() < deadline:
            time.sleep(.05)
        forced_cleanup = []
        for pid, path in fixture_processes:
            if executable(pid) == path:
                os.kill(pid, signal.SIGKILL)
                forced_cleanup.append(pid)
        if forced_cleanup:
            completed = False
            print('Fixture cleanup required SIGKILL for exact synthetic PIDs: ' + repr(forced_cleanup))
        if args.keep_evidence or not completed:
            print('Synthetic evidence: ' + str(root))
        else:
            shutil.rmtree(root)
        if forced_cleanup:
            raise AssertionError('Synthetic helper or app did not exit during bounded cleanup')


if __name__ == '__main__':
    main()
