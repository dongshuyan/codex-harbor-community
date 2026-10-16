"""macOS icon permission regression; no real app, account, window, or TCC grant.

Run: python3 -B tests/native_icon_readonly_acceptance.py
Add --red-url to apply the read-only requirement to the old NSImage URL loader.
Add --keep-evidence to retain synthetic traces under /private/tmp.
Requires only installed Apple command-line tools, AppKit and sandbox-exec.
"""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, timeout, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Fixture command failed ({result.returncode}):\n'
                           + (result.stdout + result.stderr)[-6000:])
    return result


def png(pixel):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!IIBBBBB', 2, 2, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes(pixel) * 2) * 2)) + chunk(b'IEND', b''))


def application(root, name, *, light, dark=None):
    app = root / (name + '.app')
    resources = app / 'Contents/Resources'
    resources.mkdir(parents=True)
    (app / 'Contents/MacOS').mkdir()
    (app / 'Contents/MacOS/SyntheticCodex').write_bytes(b'Not an executable; never launched')
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'SyntheticCodex'}))
    (resources / 'codex').write_bytes(b'Not an executable; never launched')
    (resources / 'icon-codex-light.png').write_bytes(light)
    if dark is not None:
        (resources / 'icon-codex-dark-color.png').write_bytes(dark)
    return app


def manifest(apps):
    result = {}
    for app in apps:
        for path in [app, *app.rglob('*')]:
            info = path.stat()
            result[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                                 hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--red-url', action='store_true')
    parser.add_argument('--keep-evidence', action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='harbor-icon-readonly-', dir='/private/tmp'))
    try:
        binary, library = root / 'icon-test', root / 'observe.dylib'
        run(['/usr/bin/clang', '-dynamiclib', '-Wall', '-Wextra', '-Werror',
             str(ROOT / 'tests/native_icon_readonly_interpose.c'), '-o', str(library)], timeout=30)
        run(['/usr/bin/swiftc', '-parse-as-library', '-module-cache-path', str(root / 'module-cache'),
             str(ROOT / 'native/Sources/CodexSmart2/Distribution.swift'),
             str(ROOT / 'tests/native_icon_readonly_acceptance.swift'), '-o', str(binary)], timeout=60)
        for directory in ('home', 'tmp'):
            (root / directory).mkdir()
        red, blue = [255, 0, 0, 255], [0, 0, 255, 255]
        good = application(root, 'SyntheticCodex', light=png(red), dark=png(blue))
        missing = application(root, 'MissingDark', light=png(red))
        corrupt = application(root, 'Corrupt', light=b'SYNTHETIC_INVALID_PNG')
        apps = (good, missing, corrupt)
        original = manifest(apps)
        environment = {'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'),
                       'PATH': '/usr/bin:/bin', 'ICON_FIXTURE_ROOT': str(root), 'ICON_TRACE_STACK': '1'}
        policy = '(version 1)(allow default)(deny network*)(deny file-write*)'

        def probe(name, mode, app=good, appearance='light'):
            # env sets DYLD after the Apple-signed sandbox-exec has started.
            result = run(['/usr/bin/sandbox-exec', '-p', policy, '/usr/bin/env',
                          'DYLD_INSERT_LIBRARIES=' + str(library), str(binary), mode, str(app), appearance],
                         timeout=10, cwd=root, env=environment)
            (root / (name + '.stdout')).write_text(result.stdout)
            (root / (name + '.stderr')).write_text(result.stderr)
            assert 'TRACE interpose-loaded' in result.stderr, 'Observation library was not loaded'
            assert 'TRACE_LIMIT' not in result.stderr, 'Observation output was truncated'
            return json.loads(result.stdout), result.stderr

        canary, trace = probe('canary', 'canary')
        assert canary['canary_blocked'] and 'BLOCK_USERACCESS' in trace, 'Unsafe probe was not intercepted'
        baseline, baseline_trace = probe('legacy-url', 'legacy-url')
        assert baseline['rendered'] and baseline['pixels'] == red * 4, 'Legacy fixture must actually render'
        baseline_probes = baseline_trace.count('TRACE BLOCK_USERACCESS')
        assert baseline_probes > 0, 'Legacy AppKit path no longer reproduces; review observation coverage'
        if args.red_url:
            assert 'TRACE BLOCK_' not in baseline_trace, 'Legacy URL decoding asks for write-capable access rights'

        checks = 0
        for name, app, appearance, pixel in [('light', good, 'light', red), ('dark', good, 'dark', blue),
                                            ('missing', missing, 'dark', None), ('corrupt', corrupt, 'light', None)]:
            result, trace = probe(name, 'production', app, appearance)
            app_trace = '\n'.join(line for line in trace.splitlines() if 'path=' + str(app) in line)
            assert 'TRACE BLOCK_' not in app_trace, f'{name}: production requested write-capable access'
            if pixel is None:
                assert not result['loaded'] and not result['rendered'], f'{name}: fallback must remain available'
            else:
                assert result['loaded'] and result['rendered'], f'{name}: production failed to render'
                assert result['width'] == 2 and result['height'] == 2 and result['pixels'] == pixel * 4
                assert 'TRACE open' in app_trace, f'{name}: source file read was not observed'
            checks += 1
        assert manifest(apps) == original, 'Icon loading changed fixture content, names, or metadata'
        summary = {'production_cases_passed': checks, 'legacy_useraccess_queries': baseline_probes,
                   'canary_blocked_before_kernel': True, 'fixture_unchanged': True,
                   'network_and_file_writes_denied': True, 'actual_tcc_prompt_tested': False}
        (root / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps(summary, sort_keys=True))
    finally:
        if args.keep_evidence:
            print('Synthetic evidence: ' + str(root))
        else:
            shutil.rmtree(root)


if __name__ == '__main__':
    main()
