"""Three-way merge of native local presentation snapshots, without guessing intent.

Lists merge membership and independent insertions. A list's reordering is one
semantic operation: two distinct concurrent reorders remain an explicit conflict.
Inputs and conflict evidence are immutable to the caller; no filesystem access.
"""
import copy

ABSENT = object()


def _evidence(value):
    return {'present': False} if value is ABSENT else {'present': True, 'value': copy.deepcopy(value)}


def _insert_missing(primary, other, wanted):
    """Keep surviving anchors; independent inserted items precede their next anchor.

No surviving next anchor means append. Ordering between independent unseen
insertions is a commit tie-break, not a claim about their wall-clock order.
This is linear even for large projectless-thread membership lists.
"""
    result = [item for item in primary if item in wanted]
    known = set(result)
    buckets = {}
    anchor = None
    for item in reversed(other):
        if item in known:
            anchor = item
        elif item in wanted:
            buckets.setdefault(anchor, []).append(item)
    merged = []
    for item in result:
        merged.extend(reversed(buckets.get(item, [])))
        merged.append(item)
    merged.extend(reversed(buckets.get(None, [])))
    return list(dict.fromkeys(merged))


def _ordered(current, before, after):
    current, before, after = [list(dict.fromkeys(items)) for items in (current, before, after)]
    now, old, new = set(current), set(before), set(after)
    common = now & old & new
    a = [item for item in before if item in common]
    b = [item for item in current if item in common]
    c = [item for item in after if item in common]
    if b != a and c != a and b != c:
        return None
    wanted = (now - (old - new)) | (new - old)
    return (_insert_missing(after, current, wanted) if c != a
            else _insert_missing(current, after, wanted))


def merge(current, before, after):
    """Return (merged, conflicts); a conflict preserves current and all alternatives."""
    conflicts = []

    def walk(now, old, new, path):
        if old == new or now == new:
            return now
        if now == old:
            return new
        if (len(path) == 3 and path[0] == 'local-projects' and path[-1] == 'updatedAt'
                and all(type(v) in (int, float) for v in (now, old, new))):
            return max(now, new)  # Native metadata, never an intent/merge priority.
        if isinstance(now, dict) and isinstance(new, dict) and (isinstance(old, dict) or old is ABSENT):
            previous = {} if old is ABSENT else old
            result = copy.deepcopy(now)
            for key in sorted(previous.keys() | new.keys()):
                value = walk(now.get(key, ABSENT), previous.get(key, ABSENT),
                             new.get(key, ABSENT), [*path, key])
                if value is ABSENT:
                    result.pop(key, None)
                else:
                    result[key] = copy.deepcopy(value)
            return result
        if (isinstance(now, list) and isinstance(new, list)
                and (isinstance(old, list) or old is ABSENT)
                and all(isinstance(v, str) for seq in (now, [] if old is ABSENT else old, new) for v in seq)):
            value = _ordered(now, [] if old is ABSENT else old, new)
            if value is not None:
                return value
        conflicts.append({'path': path, 'baseline': _evidence(old),
                          'current': _evidence(now), 'incoming': _evidence(new)})
        return now

    result = walk(current, before, after, [])
    return copy.deepcopy(result), conflicts


def changes(current,proposed,path=()):
    """Field alternatives for a legacy writer with no trustworthy last ack."""
    if current==proposed:return []
    if isinstance(current,dict) and isinstance(proposed,dict):
        result=[]
        for key in sorted(current.keys()|proposed.keys()):
            result.extend(changes(current.get(key,ABSENT),proposed.get(key,ABSENT),(*path,key)))
        return result
    return [{'path':list(path),'current':_evidence(current),'incoming':_evidence(proposed)}]
