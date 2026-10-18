"""Edition identity and local dependency discovery. No user data is imported."""
import json
import os
from pathlib import Path
import plistlib
import pwd

DATA_DIRECTORIES = {False: 'Codex Harbor', True: 'Codex Harbor Community'}


def default_root(*, community=None):
    """Use the login user's home, independent of an isolated process HOME."""
    name = EDITION['data_directory'] if community is None else DATA_DIRECTORIES[community]
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / 'Library/Application Support' / name


def settings(engine=None):
    engine=Path(engine or Path(__file__).resolve().parent)
    public=(engine/'COMMUNITY').is_file()
    info=engine.parent.parent/'Info.plist'
    if info.is_file():
        value=plistlib.loads(info.read_bytes())
        public=public or 'codex-harbor-community' in value.get('CFBundleIdentifier','')
    manifest=engine/'edition.json'
    value=json.loads(manifest.read_text()) if manifest.is_file() else {}
    public=public or value.get('edition')=='community'
    name=DATA_DIRECTORIES[public]
    # Packaged markers/metadata select the edition. Never accept an arbitrary
    # data_directory from JSON; installed metadata is covered by bundle/runtime verification.
    machine={}
    if not public and (engine/'machine-defaults.json').is_file():
        machine=json.loads((engine/'machine-defaults.json').read_text())
    return dict(community=public,app_name=name,data_directory=name,
                version=str(value.get('version','0.9.1' if public else '0.8.1')),machine=machine)


def discover_app(candidates=None):
    if candidates is None:
        candidates=[base/name for base in [Path('/Applications'),Path.home()/'Applications']
                    for name in ['Codex.app','ChatGPT.app']]
    for app in map(Path,candidates):
        try:
            info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            name=info['CFBundleExecutable']
            if not isinstance(name,str) or Path(name).name!=name:continue
            executable=app/'Contents/MacOS'/name
            if not executable.is_file() or not os.access(executable,os.X_OK):continue
            for rel in ['codex-cli/CodexCLI.app/Contents/MacOS/codex','codex']:
                cli=app/'Contents/Resources'/rel
                if cli.is_file() and os.access(cli,os.X_OK):return app,cli,executable
        except (OSError,ValueError,KeyError,plistlib.InvalidFileException):continue
    # Configuration remains usable without Codex; attempts to launch explain it.
    app=Path('/Applications/Codex.app')
    return app,app/'Contents/Resources/codex',app/'Contents/MacOS/Codex'


EDITION=settings()
