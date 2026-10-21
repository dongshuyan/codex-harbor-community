"""Read native defaults without sharing account credentials or writing sources.

The selected history directory is also its native Codex customization source.
Workspace discovery stays in Codex; it is never flattened into user defaults.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import tomllib

from core import UserError, atomic_write, directory

FILES = '.harbor-native-resources.json'
# These select identity, transport or persistence. Everything else stays native.
ROUTING = frozenset('''model_provider model_providers profile profiles openai_base_url
chatgpt_base_url model_catalog_json cli_auth_credentials_store
forced_login_method forced_chatgpt_workspace_id sqlite_home
api_key access_token refresh_token experimental_bearer_token auth_mode codex_home'''.split())
# Shell customizations may configure other tools' credentials and proxies. Only
# the selected Codex locations must survive subsequent user startup files.
SHELL_ROUTE_ENV = ('CODEX_HOME', 'CODEX_SQLITE_HOME')
ROUTE_ENV = ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN',
             'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION',
             'OPENAI_PROJECT_ID', 'CODEX_REFRESH_TOKEN_URL_OVERRIDE', 'CODEX_REVOKE_TOKEN_URL_OVERRIDE')


def bundled_registration(name, entry):
    """Recognize native per-home registrations without probing other directories."""
    if name not in ('openai-bundled', 'openai-bundled-alpha') or not isinstance(entry, dict):
        return False
    path = entry.get('source')
    if entry.get('source_type') != 'local' or not isinstance(path, str) or not path.startswith('/'):
        return False
    suffix = '/.tmp/bundled-marketplaces/' + name
    return path.endswith(suffix)


def read_source(path, limit=4 * 1024 * 1024, *, follow=False):
    """Read only; sources may use dotfile links, private destinations may not."""
    try:
        fd = os.open(path, os.O_RDONLY | (0 if follow else os.O_NOFOLLOW) | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise UserError('原 Codex 设置文件类型或大小异常，未修改原文件。')
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise UserError('原 Codex 设置文件过大，未修改原文件。')
        return data
    finally:
        if fd is not None:
            os.close(fd)


def source_configuration(prepared):
    if not prepared.get('native_user_home'):
        return {}
    source = Path(prepared['history'])
    raw = read_source(source / 'config.toml', follow=True)
    if raw is None:
        return {}
    try:
        value = tomllib.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise UserError('会话目录的 Codex 配置格式无效，未修改原文件。') from None
    value = {k: v for k, v in value.items() if k not in ROUTING}
    # A provider-specific catalog belongs to Harbor's API selection, not a source.
    if prepared['profile']['kind'] == 'api':
        for k in ('model', 'model_reasoning_effort', 'model_context_window', 'model_auto_compact_token_limit'):
            value.pop(k, None)
    env = value.get('shell_environment_policy', {}).get('set', {})
    # Do not replicate source routing credentials into every account's config
    # and ledger. User shell exports remain available in their original files.
    for k in ROUTE_ENV:
        env.pop(k, None)
    # Account-scoped remote plugin provisioning must be resolved by this login.
    for plugin in value.get('plugins', {}).values():
        if isinstance(plugin, dict):
            for k in ('remote_plugin_id', 'release_id', 'auto_provision'):
                plugin.pop(k, None)
    def absolute(table, key):
        item = table.get(key)
        if isinstance(item, str) and item and not item.startswith(('~', '/')):
            table[key] = str(source / item)
    for key in ('model_instructions_file', 'experimental_instructions_file',
                'experimental_compact_prompt_file', 'log_dir'):
        absolute(value, key)
    for item in value.get('skills', {}).get('config', []):
        absolute(item, 'path')
    for section, field in [('agents', 'config_file'), ('mcp_servers', 'cwd')]:
        for item in value.get(section, {}).values():
            if isinstance(item, dict):
                absolute(item, field)
    for item in value.get('marketplaces', {}).values():
        if isinstance(item, dict) and item.get('source_type') == 'local':
            absolute(item, 'source')
    # The signed desktop materializes and registers bundled plugins for its own
    # home. Copying another home's registration prevents native marketplace/add.
    markets = value.get('marketplaces', {})
    for name, item in list(markets.items()):
        if bundled_registration(name, item):
            del markets[name]
    if not markets:
        value.pop('marketplaces', None)
    return value


def inherit_configuration(prepared, value, ledger):
    """Three-way defaults: refresh unchanged inherited fields; keep local edits."""
    from configuration import ABSENT, leaves, lookup, put, remove
    source = source_configuration(prepared)
    previous = copy.deepcopy(ledger.get('inherited', {}))
    protected = ledger.get('protected', [])
    if not isinstance(previous, dict) or not isinstance(protected, list) or any(
            not isinstance(p, list) or not p or any(not isinstance(k, str) for k in p) for p in protected):
        raise UserError('Harbor 原生设置记录无效，未覆盖配置。')
    blocked = {tuple(p) for p in protected}
    # Withdraw only a proven Harbor copy. Preserve a changed/deleted table as a
    # whole so removing an unchanged source_type cannot corrupt a user's edit.
    markets = previous.get('marketplaces', {})
    for name, item in list(markets.items()) if isinstance(markets, dict) else []:
        if not bundled_registration(name, item):
            continue
        path = ('marketplaces', name)
        if lookup(value, path) == item and not any(path[:len(p)] == p or p[:len(path)] == path for p in blocked):
            remove(value, path)
        else:
            blocked.add(path)
        remove(previous, path)
    for path, old in leaves(previous):
        if lookup(value, path) == old:
            remove(value, path)
        else:
            blocked.add(path)
    inherited = {}
    for path, item in leaves(source):
        if any(path[:len(p)] == p or p[:len(path)] == path for p in blocked):
            continue
        if any(lookup(value, path[:i]) is not ABSENT and not isinstance(lookup(value, path[:i]), dict)
               for i in range(1, len(path))):
            continue
        if lookup(value, path) is ABSENT:
            put(value, path, item)
            put(inherited, path, item)
    return inherited, [list(p) for p in sorted(blocked)]


def refresh_resources(prepared):
    """Connect user skills; privately snapshot only instruction/rule text files.

    Keep Codex's .system skills and plugin/account caches independent. Never
    follow a destination symlink when writing. User edits take precedence.
    """
    if not prepared.get('native_user_home'):
        return
    home, source = Path(prepared['home']), Path(prepared['history'])
    record = home / FILES
    old_raw = read_source(record)
    try:
        old = json.loads(old_raw) if old_raw else {'links': {}, 'files': {}}
        if not isinstance(old, dict) or set(old) != {'links', 'files'} or not all(isinstance(old.get(k), dict) for k in ('links', 'files')):
            raise ValueError()
        if any(not isinstance(v, str) for d in old.values() for v in d.values()):
            raise ValueError()
        if any(not resource_path(k) for d in old.values() for k in d):
            raise ValueError()
    except (ValueError, TypeError):
        raise UserError('Harbor 原生扩展记录无效，未修改扩展。') from None
    wanted_links, wanted_files = {}, {}
    skills = source / 'skills'
    if skills.is_dir():
        wanted_links = {'skills/' + p.name: str(p) for p in skills.iterdir()
                        if not p.name.startswith('.') and p.is_dir()}
    for name in ('AGENTS.md', 'AGENTS.override.md'):
        data = read_source(source / name, follow=True)
        if data is not None:
            wanted_files[name] = data
    rules = source / 'rules'
    if rules.is_dir():
        for p in rules.glob('*.rules'):
            data = read_source(p, follow=True)
            if data is not None:
                wanted_files['rules/' + p.name] = data
    links, files = {}, {}
    for name in set(old['links']) | set(wanted_links):
        destination = home / name
        directory(destination.parent)
        target = wanted_links.get(name)
        owned = destination.is_symlink() and os.readlink(destination) == old['links'].get(name)
        if owned and os.readlink(destination) != target:
            destination.unlink()
        if target and not os.path.lexists(destination):
            destination.symlink_to(target, target_is_directory=True)
            owned = True
        if target and owned:
            links[name] = target
    for name in set(old['files']) | set(wanted_files):
        destination = home / name
        directory(destination.parent)
        if destination.is_symlink():
            continue
        current = read_source(destination)
        owned = current is not None and hashlib.sha256(current).hexdigest() == old['files'].get(name)
        data = wanted_files.get(name)
        if data is None:
            if owned:
                destination.unlink()
        elif owned or current is None:
            if current != data:
                try:
                    text = data.decode('utf-8')
                except UnicodeError:
                    raise UserError('原 Codex 指令或规则不是 UTF-8 文本，未覆盖私有副本。') from None
                atomic_write(destination, text)
            files[name] = hashlib.sha256(data).hexdigest()
    text = json.dumps({'links': links, 'files': files}, ensure_ascii=False, sort_keys=True) + '\n'
    if old_raw != text.encode():
        atomic_write(record, text)


def resource_path(name):
    if name in ('AGENTS.md', 'AGENTS.override.md'):
        return True
    p = Path(name)
    return (len(p.parts) == 2 and p.parts[0] in ('skills', 'rules')
            and not p.parts[1].startswith('.') and p.as_posix() == name)


def shell_environment(prepared):
    """Load normal zsh customizations, then pin this window's identity/location."""
    user = prepared.get('native_user_home')
    if not user:
        return
    osh = Path(prepared['os_home'])
    cleared = SHELL_ROUTE_ENV if prepared.get('access_mode', 'native') == 'native' else ROUTE_ENV
    pinned = {'HOME': user, 'ZDOTDIR': str(osh), 'CODEX_HOME': prepared['home'],
              'CODEX_SQLITE_HOME': prepared['history']}
    for name in ('.zshenv', '.zprofile', '.zshrc', '.zlogin'):
        # Keep the user's custom ZDOTDIR discovered by their own startup files.
        text = ('# Harbor routing guard; user startup files remain unchanged.\n'
                'export ZDOTDIR=${HARBOR_USER_ZDOTDIR:-' + shlex.quote(user) + '}\n'
                'if [[ -r "$ZDOTDIR/' + name + '" ]]; then source "$ZDOTDIR/' + name + '"; fi\n'
                'export HARBOR_USER_ZDOTDIR="$ZDOTDIR"\n'
                + 'unset ' + ' '.join(cleared) + '\n'
                + ''.join('export ' + k + '=' + shlex.quote(v) + '\n' for k, v in pinned.items()))
        path = osh / name
        current = read_source(path)
        if current != text.encode():
            atomic_write(path, text)
