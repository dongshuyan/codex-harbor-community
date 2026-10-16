#!/usr/bin/env python3
"""Build a clean, content-addressed app. Does not install or read user settings."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT=Path(__file__).resolve().parent


def build(community=False,universal=False):
    spec=json.loads((ROOT/'release_manifest.json').read_text())
    community=community or (ROOT/'engine/COMMUNITY').is_file()
    name='Codex Harbor Community' if community else 'Codex Harbor'
    version=spec['version'] if community else '0.9.0'
    ident='io.github.dongshuyan.codex-harbor-community' if community else 'local.dongshuyan.codex-harbor'
    inputs=sorted(set(spec['runtime']+spec['native']+['native/Package.swift','build.py','release_manifest.json',
        'assets/CodexHarborIcon.icns','assets/restore-macos.command','LICENSE-Codex','LICENSE-CHERRY-PROVIDER-REGISTRY','LICENSE-TOMLKIT']))
    inputs += [p for p in ['LICENSE','THIRD-PARTY-NOTICES.md','CREDITS.md'] if (ROOT/p).is_file()]
    if not community and (ROOT/'engine/machine-defaults.json').is_file():inputs.append('engine/machine-defaults.json')
    digest=hashlib.sha256((name+str(universal)).encode()+b''.join(p.encode()+b'\0'+(ROOT/p).read_bytes() for p in inputs)).hexdigest()
    output=ROOT/'build'/digest[:16]/(name+'.app')
    if output.exists():
        subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(output)],check=True)
        return output
    # Direct compilation uses the reviewed source inventory, and works with
    # Apple's Command Line Tools without Xcode's multi-architecture build engine.
    architectures=['arm64','x86_64'] if universal else [subprocess.check_output(['/usr/bin/uname','-m'],text=True).strip()]
    compiled=ROOT/'build'/digest[:16]/'compiled';compiled.mkdir(parents=True,exist_ok=True)
    sdk=subprocess.check_output(['/usr/bin/xcrun','--sdk','macosx','--show-sdk-path'],text=True).strip()
    for architecture in architectures:
        target=compiled/architecture;target.mkdir(exist_ok=True)
        common=['/usr/bin/swiftc','-swift-version','5','-O','-whole-module-optimization','-sdk',sdk,
                '-target',architecture+'-apple-macosx14.0','-debug-prefix-map',str(ROOT)+'=/src/harbor',
                '-file-prefix-map',str(ROOT)+'=/src/harbor']
        for product in ['CodexSmart2','HarborKeychain']:
            sources=[str(ROOT/p) for p in spec['native'] if Path(p).parts[2]==product]
            if not sources:raise ValueError('Release source inventory is incomplete')
            subprocess.run(common+(['-parse-as-library'] if product=='CodexSmart2' else [])+
                           ['-module-name',product,*sources,'-o',str(target/product)],check=True)
    mac=output/'Contents/MacOS';res=output/'Contents/Resources'
    mac.mkdir(parents=True);res.mkdir()
    for product,dest in [('CodexSmart2',mac/'CodexSmart2'),('HarborKeychain',res/'HarborKeychain')]:
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
          'CFBundleVersion':'23' if community else '22','LSMinimumSystemVersion':'14.0',
          'NSHighResolutionCapable':True,'NSPrincipalClass':'NSApplication','CFBundleDevelopmentRegion':'zh_CN'}
    (output/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    (res/'build-manifest.json').write_text(json.dumps({'source_sha256':digest,'app_name':name,'version':version,
        'edition':'community' if community else 'private','existing_configuration_imported':False,
        'requires_python':'>=3.11','codex_catalog_source_commit':'740e5af33c71225640e0c1c1555c514c2c93ab74',
        'architectures':['arm64','x86_64'] if universal else [subprocess.check_output(['/usr/bin/uname','-m'],text=True).strip()]},indent=2)+'\n')
    for p in ['LICENSE','LICENSE-Codex','LICENSE-CHERRY-PROVIDER-REGISTRY','LICENSE-TOMLKIT','THIRD-PARTY-NOTICES.md','CREDITS.md']:
        if (ROOT/p).is_file():shutil.copyfile(ROOT/p,res/p)
    subprocess.run(['/usr/bin/strip','-S','-x',str(mac/'CodexSmart2'),str(res/'HarborKeychain')],check=True)
    subprocess.run(['/usr/bin/xattr','-cr',str(output)],check=True)
    subprocess.run(['/usr/bin/codesign','--force','--sign','-','--identifier',ident+'.keychain','--timestamp=none',str(res/'HarborKeychain')],check=True)
    subprocess.run(['/usr/bin/codesign','--force','--sign','-','--timestamp=none',str(output)],check=True)
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(output)],check=True)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--community',action='store_true')
    parser.add_argument('--universal',action='store_true');args=parser.parse_args()
    print(build(args.community,args.universal))
