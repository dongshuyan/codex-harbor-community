#!/usr/bin/env python3
"""Build a clean ZIP in an owned temporary workspace. Never installs an app."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess

import build_artifacts as artifacts

ROOT=Path(__file__).resolve().parent


def _build(community, universal, output_root, identity, keychain, certificate_sha1):
    spec=json.loads((ROOT/'release_manifest.json').read_text())
    community=community or (ROOT/'engine/COMMUNITY').is_file()
    name='Codex Harbor Community' if community else 'Codex Harbor'
    version=spec['version'] if community else spec['private_version']
    ident='io.github.dongshuyan.codex-harbor-community' if community else 'local.dongshuyan.codex-harbor'
    inputs=sorted(set(spec['runtime']+spec['native']+['native/Package.swift','build.py','build_artifacts.py','release_manifest.json',
        'assets/CodexHarborIcon.icns','assets/restore-macos.command','LICENSE-Codex','LICENSE-CHERRY-PROVIDER-REGISTRY','LICENSE-TOMLKIT','LICENSE-MAGPIE']))
    inputs += [p for p in ['LICENSE','THIRD-PARTY-NOTICES.md','CREDITS.md'] if (ROOT/p).is_file()]
    if not community and (ROOT/'engine/machine-defaults.json').is_file():inputs.append('engine/machine-defaults.json')
    digest=hashlib.sha256((name+str(universal)).encode()+b''.join(p.encode()+b'\0'+(ROOT/p).read_bytes() for p in inputs)).hexdigest()
    output=output_root/(name+'.app')
    # Direct compilation uses the reviewed source inventory, and works with
    # Apple's Command Line Tools without Xcode's multi-architecture build engine.
    architectures=['arm64','x86_64'] if universal else [subprocess.check_output(['/usr/bin/uname','-m'],text=True).strip()]
    compiled=output_root/'compiled';compiled.mkdir(parents=True,exist_ok=True)
    sdk=subprocess.check_output(['/usr/bin/xcrun','--sdk','macosx','--show-sdk-path'],text=True).strip()
    for architecture in architectures:
        target=compiled/architecture;target.mkdir(exist_ok=True)
        common=['/usr/bin/swiftc','-swift-version','5','-O','-whole-module-optimization','-sdk',sdk,
                '-target',architecture+'-apple-macosx14.0','-debug-prefix-map',str(ROOT)+'=/src/harbor',
                '-file-prefix-map',str(ROOT)+'=/src/harbor']
        for product in ['CodexSmart2','HarborKeychain','HarborLaunch']:
            sources=[str(ROOT/p) for p in spec['native'] if Path(p).parts[2]==product]
            if not sources:raise ValueError('Release source inventory is incomplete')
            subprocess.run(common+(['-parse-as-library'] if product=='CodexSmart2' else [])+
                           ['-module-name',product,*sources,'-o',str(target/product)],check=True)
    mac=output/'Contents/MacOS';res=output/'Contents/Resources'
    mac.mkdir(parents=True);res.mkdir()
    for product,dest in [('CodexSmart2',mac/'CodexSmart2'),('HarborKeychain',res/'HarborKeychain'),('HarborLaunch',res/'HarborLaunch')]:
        slices=[str(compiled/a/product) for a in architectures]
        if universal:subprocess.run(['/usr/bin/lipo','-create',*slices,'-output',str(dest)],check=True)
        else:shutil.copyfile(slices[0],dest)
        dest.chmod(0o755)
    for p in spec['runtime']:
        path=ROOT/p
        if path.is_symlink():raise ValueError('Symlink in release inputs')
        target=res/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    if not community and (ROOT/'engine/machine-defaults.json').is_file():shutil.copyfile(ROOT/'engine/machine-defaults.json',res/'engine/machine-defaults.json')
    (res/'engine/edition.json').write_text(json.dumps({'edition':'community' if community else 'private',
        'app_name':name,'data_directory':name,'version':version},indent=2)+'\n')
    if community:(res/'engine/COMMUNITY').write_text('community\n')
    shutil.copyfile(ROOT/'assets/CodexHarborIcon.icns',res/'AppIcon.icns')
    shutil.copyfile(ROOT/'assets/restore-macos.command',res/'restore-macos.command');(res/'restore-macos.command').chmod(0o700)
    info={'CFBundleName':name,'CFBundleDisplayName':name,'CFBundleIdentifier':ident,'CFBundleExecutable':'CodexSmart2',
          'CFBundlePackageType':'APPL','CFBundleIconFile':'AppIcon','CFBundleShortVersionString':version,
          'CFBundleVersion':'33','LSMinimumSystemVersion':'14.0',
          'NSHighResolutionCapable':True,'NSPrincipalClass':'NSApplication','CFBundleDevelopmentRegion':'zh_CN'}
    (output/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    (res/'build-manifest.json').write_text(json.dumps({'source_sha256':digest,'app_name':name,'version':version,
        'edition':'community' if community else 'private','existing_configuration_imported':False,
        'requires_python':'>=3.11','codex_catalog_source_commit':'740e5af33c71225640e0c1c1555c514c2c93ab74',
        'architectures':['arm64','x86_64'] if universal else [subprocess.check_output(['/usr/bin/uname','-m'],text=True).strip()]},indent=2)+'\n')
    for p in ['LICENSE','LICENSE-Codex','LICENSE-CHERRY-PROVIDER-REGISTRY','LICENSE-TOMLKIT','LICENSE-MAGPIE','THIRD-PARTY-NOTICES.md','CREDITS.md']:
        if (ROOT/p).is_file():shutil.copyfile(ROOT/p,res/p)
    subprocess.run(['/usr/bin/strip','-S','-x',str(mac/'CodexSmart2'),str(res/'HarborKeychain'),str(res/'HarborLaunch')],check=True)
    subprocess.run(['/usr/bin/xattr','-cr',str(output)],check=True)
    signing=['--sign',identity]+(['--keychain',str(keychain)] if keychain else [])
    for product, identifier in [(res/'HarborKeychain', ident+'.keychain'), (res/'HarborLaunch', ident+'.launch'), (output, ident)]:
        if product==output:
            from engine.runtime_payload import NOTICES, write_manifest
            runtime_paths=[*spec['runtime'],'engine/edition.json','HarborLaunch','HarborKeychain']
            runtime_paths.extend(name for name in NOTICES if (res/name).is_file())
            if (res/'engine/COMMUNITY').exists():runtime_paths.append('engine/COMMUNITY')
            if (res/'engine/machine-defaults.json').exists():runtime_paths.append('engine/machine-defaults.json')
            write_manifest(res,runtime_paths)
        requirement = f'identifier "{identifier}" and certificate leaf = H"{certificate_sha1}"' if certificate_sha1 else None
        flags=['--requirements','=designated => '+requirement] if requirement else []
        subprocess.run(['/usr/bin/codesign','--force',*signing,'--identifier',identifier,*flags,'--timestamp=none',str(product)],check=True,timeout=30)
        if requirement:subprocess.run(['/usr/bin/codesign','--verify','--strict','-R='+requirement,str(product)],check=True,timeout=30)
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(output)],check=True,timeout=30)
    return output


def build(community=False, universal=False, *, output_root, identity='-', keychain=None, certificate_sha1=None):
    """The caller owns an empty output directory and its app's entire lifetime."""
    output_root = artifacts.safe_components(output_root)
    if certificate_sha1 is not None and not re.fullmatch('[0-9a-f]{40}', certificate_sha1):
        raise ValueError('Certificate pin must be a lowercase SHA-1 fingerprint')
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError('Build output directory must be empty')
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        return _build(community, universal, output_root, identity, keychain, certificate_sha1)
    except BaseException:
        for app in output_root.glob('*.app'):
            artifacts.unregister(app)
            shutil.rmtree(app)
        shutil.rmtree(output_root/'compiled', ignore_errors=True)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--community',action='store_true')
    parser.add_argument('--universal',action='store_true')
    parser.add_argument('--identity',default='-',help='Signing certificate identity; default is ad-hoc')
    parser.add_argument('--keychain',type=Path)
    parser.add_argument('--output',type=Path,help='ZIP destination; no app is installed')
    args=parser.parse_args()
    with artifacts.Workspace() as workspace:
        app=build(args.community,args.universal,output_root=workspace.path/'build',
                  identity=args.identity,keychain=args.keychain)
        manifest=json.loads((app/'Contents/Resources/build-manifest.json').read_text())
        destination=args.output or ROOT/'release-artifacts'/(app.stem.replace(' ','-')+'-'+manifest['version']+'.zip')
        destination=artifacts.safe_components(destination)
        if destination.suffix.lower()!='.zip':parser.error('Output must be a ZIP archive')
        destination.parent.mkdir(parents=True,exist_ok=True)
        candidate=workspace.path/'release.zip'
        artifacts.make_archive(app,candidate)
        verify=workspace.path/'archive-check'
        artifacts.extract_archive(candidate,verify)
        extracted=verify/app.name
        if artifacts.inventory(extracted)!=artifacts.inventory(app):raise ValueError('Release archive differs from build')
        subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(extracted)],check=True)
        # Destination may be another volume; stage alongside it for atomic publication.
        import os, tempfile
        fd, name=tempfile.mkstemp(prefix='.harbor-release-',suffix='.zip',dir=destination.parent)
        os.close(fd)
        staged=Path(name)
        try:
            shutil.copyfile(candidate,staged);staged.chmod(0o600)
            if artifacts.sha256(staged)!=artifacts.sha256(candidate):raise ValueError('Archive copy failed')
            artifacts.sync_file(staged)
            os.replace(staged,destination)
            artifacts.sync_directory(destination.parent)
        finally:staged.unlink(missing_ok=True)
    print(destination)


if __name__=='__main__': main()
