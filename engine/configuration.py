"""Preserve native user preferences while refreshing Harbor-owned launch fields.

Uses TOML Kit for partial edits and Python's TOML parser for verification before
any write. The first existing file is backed up verbatim, including comments.
Only Harbor's private CODEX_HOME is written, never the selected history's config.
"""
import copy
import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tomllib

from core import UserError, atomic_write, directory

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
import tomlkit

MANAGED = '.harbor-managed-config.toml'
BACKUP = '.harbor-config-before-preservation.toml'
ABSENT = object()


def read(path):
    directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return '', {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise UserError('Codex 配置不是普通文件或过大，已保留原文件并停止启动。')
        with os.fdopen(fd, 'r', encoding='utf-8') as handle:
            fd = None
            text = handle.read()
        return text, tomllib.loads(text)
    except (ValueError, UnicodeError):
        raise UserError('Codex 配置格式无效，已保留原文件。请修正该实例的 config.toml 后再启动。') from None
    finally:
        if fd is not None:
            os.close(fd)


def key(value):
    return value if re.fullmatch(r'[A-Za-z0-9_-]+', value) else json.dumps(value, ensure_ascii=False)


def scalar(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return '[' + ', '.join(scalar(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(f'{key(k)} = {scalar(v)}' for k, v in value.items()) + '}'
    raise UserError('Codex 配置包含无法保留的值，未覆盖原文件。')


def dumps(value):
    lines = []
    def table(data, path=()):
        if path:
            lines.extend(['', '[' + '.'.join(key(p) for p in path) + ']'])
        for k, v in data.items():
            if not isinstance(v, dict):
                lines.append(f'{key(k)} = {scalar(v)}')
        for k, v in data.items():
            if isinstance(v, dict):
                table(v, (*path, k))
    table(value)
    return '\n'.join(lines).lstrip('\n') + '\n'


def leaves(value, path=()):
    for k, v in value.items():
        if isinstance(v, dict) and v:
            yield from leaves(v, (*path, k))
        else:
            yield (*path, k), v


def lookup(value, path):
    for k in path:
        if not isinstance(value, dict) or k not in value:
            return ABSENT
        value = value[k]
    return value


def remove(value, path):
    parent = value
    stack = []
    for k in path[:-1]:
        if not isinstance(parent.get(k), dict):
            return
        stack.append((parent, k))
        parent = parent[k]
    parent.pop(path[-1], None)
    for parent, k in reversed(stack):
        if not parent[k]:
            del parent[k]


def overlay(value, generated):
    for k, v in generated.items():
        if isinstance(v, dict) and isinstance(value.get(k), dict):
            overlay(value[k], v)
        else:
            value[k] = copy.deepcopy(v)


def put(value, path, item):
    for k in path[:-1]:
        if not isinstance(value.get(k), dict):
            value[k] = {}
        value = value[k]
    value[path[-1]] = copy.deepcopy(item)


def patch_document(original, value):
    """Use the established style-preserving parser, touching changed keys only."""
    document = tomlkit.parse(original)
    def update(target, wanted):
        for k in list(target):
            if k not in wanted:
                del target[k]
        for k, v in wanted.items():
            if k in target and target[k] == v:
                continue
            if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                update(target[k], v)
            else:
                target[k] = v
    update(document, value)
    return tomlkit.dumps(document)


def equivalent(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equivalent(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    return type(left) is type(right) and left == right


def save(prepared, generated_text):
    home = Path(prepared['home'])
    path = home / 'config.toml'
    original, value = read(path)
    _, ledger = read(home / MANAGED)
    if ledger and (ledger.get('version') != 1 or not isinstance(ledger.get('applied'), dict)
                   or not isinstance(ledger.get('before'), dict)):
        raise UserError('Harbor 配置记录格式无效，未覆盖 Codex 配置。')
    previous, before = ledger.get('applied', {}), ledger.get('before', {})
    mcp_key = 'mcp_oauth_credentials_store'
    deleted_mcp_default = mcp_key in previous and mcp_key not in value
    generated = tomllib.loads(generated_text)
    defaults = {k: generated.pop(k) for k in ('model', 'model_reasoning_effort') if k in generated}
    # Undo unchanged owned fields before applying the next overlay. Restore the
    # prior user value, not just absence. A user edit since launch wins here.
    for field, old in leaves(previous):
        if lookup(value, field) == old:
            original_value = lookup(before, field)
            if original_value is ABSENT:
                remove(value, field)
            else:
                put(value, field, original_value)
    for field, old in leaves(before):
        if old == {} and lookup(value, field) is ABSENT:
            put(value, field, {})
    from native_defaults import inherit_configuration
    inherited, protected = inherit_configuration(prepared, value, ledger)
    if prepared.get('access_mode', 'native') == 'native':
        if deleted_mcp_default:
            # A deliberate deletion chooses native storage semantics. Do not
            # refill it from either the source or our per-home safety default.
            value.pop(mcp_key, None)
            inherited.pop(mcp_key, None)
            if [mcp_key] not in protected: protected = sorted([*protected, [mcp_key]])
        if mcp_key in value or [mcp_key] in protected:
            generated.pop(mcp_key, None)
    if prepared['profile']['kind'] == 'official':
        for field in ('openai_base_url', 'model_catalog_json'):
            value.pop(field, None)
    before = {k: copy.deepcopy(value[k]) for k in generated if k in value}
    overlay(value, generated)
    explicit = prepared.get('explicit_model', False)
    for k, v in defaults.items():
        if explicit or k not in value:
            value[k] = v
    for k in ('model', 'model_reasoning_effort'):
        if k in value and (not isinstance(value[k], str) or not value[k]):
            raise UserError('Codex 的默认模型或推理程度格式无效，未覆盖原文件。')
    if prepared['profile']['kind'] == 'api':
        mappings = {m['alias']: m for m in prepared['profile']['models']}
        if value['model'] not in mappings:
            value.update(defaults)
        mapping = mappings[value['model']]
        from reasoning import validate
        rule = validate(mapping)
        if value['model_reasoning_effort'] not in rule['levels']:
            value['model_reasoning_effort'] = rule['effort']
    text = patch_document(original, value)
    # Fail closed instead of silently discarding an unfamiliar TOML value.
    try:
        if not equivalent(tomllib.loads(text), value):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise UserError('Codex 配置无法完整保留，未覆盖原文件。') from None
    backup = home / BACKUP
    if original and not backup.exists():
        atomic_write(backup, original)
    if text != original:
        atomic_write(path, text)
    record = dumps({'version': 1, 'applied': generated, 'before': before,
                    'inherited': inherited, 'protected': protected})
    record_path = home / MANAGED
    if not record_path.exists() or record_path.read_text() != record:
        atomic_write(record_path, record)
