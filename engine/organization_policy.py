"""Automatic, causal reconciliation of allowlisted desktop presentation state.

The caller commits canonical values, revisions, writer bases and observations in
one transaction. Revisions (including absent-value tombstones) prevent an old
window from gaining write authority merely by saving its old snapshot again.
This module never reads or writes conversation files or running windows.
"""
import copy
import hashlib
import json

from organization_merge import _insert_missing

ABSENT = object()


def token(path):
    return json.dumps(path, ensure_ascii=False, separators=(',', ':'))


def sequence(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def evidence(value):
    return {'present': False} if value is ABSENT else {'present': True, 'value': copy.deepcopy(value)}


def initialize(scope):
    if 'versions' in scope:
        return
    scope['clock'] = 1
    scope['versions'] = {}
    scope.setdefault('alternatives', {})

    def visit(value, path=()):
        scope['versions'][token(path)] = 1
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, (*path, key))
        elif sequence(value):
            scope['versions'][token((*path, '@order'))] = 1
            for item in value:
                scope['versions'][token((*path, '@member', item))] = 1
    visit(scope['data'])


def reconcile(scope, writer, before, after, *, legacy=False):
    """Merge actual deltas; stale differing values keep the committed choice.

    Legacy imports may add only previously unseen positive values. Absence in a
    legacy snapshot is never a deletion. Each rejected alternative is retained
    once per writer/key, not appended on every observation.
    """
    initialize(scope)
    versions = scope['versions']
    bases = writer.setdefault('base_versions', {})
    alternatives = scope.setdefault('alternatives', {})
    start = scope['clock']

    def revision(path):
        return versions.get(token(path), 0)

    def fresh(path):
        return bases.get(token(path), 0) == revision(path)

    def stamp(path):
        scope['clock'] += 1
        versions[token(path)] = scope['clock']
        bases[token(path)] = scope['clock']

    def alternative(path, old, now, new):
        ident = hashlib.sha256(token((writer['registration'], *path)).encode()).hexdigest()
        alternatives[ident] = {'writer': writer['registration'], 'path': list(path),
            'baseline': evidence(old), 'current': evidence(now), 'incoming': evidence(new),
            'revision': revision(path), 'decision': 'keep_committed'}

    def acknowledge(path):
        bases[token(path)] = revision(path)
        ident = hashlib.sha256(token((writer['registration'], *path)).encode()).hexdigest()
        alternatives.pop(ident, None)

    def seed(value, path):
        """Register every new node so a subsequent removal retains its history."""
        stamp(path)
        if isinstance(value, dict):
            for key, item in value.items():
                seed(item, (*path, key))
        elif sequence(value):
            stamp((*path, '@order'))
            for item in value:
                stamp((*path, '@member', item))
        # Children change the clock, but the aggregate revision also describes
        # this exact subtree. It must not precede one of its own descendants.
        versions[token(path)] = scope['clock']
        bases[token(path)] = scope['clock']

    def ordered(now, old, new, path):
        now, old, new = [list(dict.fromkeys(v)) for v in (now, old, new)]
        wanted = set(now)
        for item in sorted(set(old) ^ set(new)):
            member = (*path, '@member', item)
            incoming = item in new
            current = item in wanted
            if incoming == current:
                acknowledge(member)
            elif fresh(member) and not (legacy and (not incoming or revision(member))):
                (wanted.add if incoming else wanted.discard)(item)
                stamp(member)
            else:
                alternative(member, item in old, current, incoming)
        common = set(old) & set(new)
        changed_order = [v for v in old if v in common] != [v for v in new if v in common]
        order_path = (*path, '@order')
        primary, secondary = now, new
        if changed_order:
            surviving = common & wanted
            current_order = [v for v in now if v in surviving]
            new_order = [v for v in new if v in surviving]
            if current_order == new_order:
                acknowledge(order_path)
            elif fresh(order_path) and not legacy:
                primary, secondary = new, now
                stamp(order_path)
            else:
                alternative(order_path, old, now, new)
        return _insert_missing(primary, secondary, wanted)

    def walk(now, old, new, path):
        if old == new:
            return now
        if isinstance(now, dict) and isinstance(new, dict) and (isinstance(old, dict) or old is ABSENT):
            prior = {} if old is ABSENT else old
            result = copy.deepcopy(now)
            for key in sorted(prior.keys() | new.keys()):
                child = walk(now.get(key, ABSENT), prior.get(key, ABSENT),
                             new.get(key, ABSENT), (*path, key))
                if child is ABSENT:
                    result.pop(key, None)
                else:
                    result[key] = child
            if result != now:
                # Invalidate old whole-object removals, without granting the
                # current writer authority over any unseen sibling changes.
                scope['clock'] += 1
                versions[token(path)] = scope['clock']
            if result == new:
                acknowledge(path)
            return result
        if sequence(now) and (sequence(new) or new is ABSENT) and (sequence(old) or old is ABSENT) and path[-1:] != ('rootPaths',):
            result = ordered(now, [] if old is ABSENT else old, [] if new is ABSENT else new, path)
            if result != now:
                scope['clock'] += 1
                versions[token(path)] = scope['clock']
            if result == new:
                acknowledge(path)
            return ABSENT if new is ABSENT and not result else result
        if now == new:
            acknowledge(path)
            return now
        if fresh(path) and not (legacy and (new is ABSENT or revision(path))):
            seed(new, path)
            return ABSENT if new is ABSENT else copy.deepcopy(new)
        alternative(path, old, now, new)
        return now

    result = walk(scope['data'], before, after, ())
    if result != scope['data']:
        scope['data'] = result
        scope['generation'] = scope.get('generation', 0) + 1
    # A convergent acknowledgement can change bases without changing canonical
    # values; the caller persists both even when the generation is unchanged.
    return scope['clock'] != start
