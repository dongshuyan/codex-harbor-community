"""Sidebar order is separate from private account/API snapshots."""
import fcntl
import hashlib
import json
from core import UserError, directory, write_json, ID


def ordered(root,profiles):
    path=root/'profile-order.json'
    if path.is_symlink():raise UserError('API 排序路径不安全。')
    if not path.exists():return profiles
    value=json.loads(path.read_text())
    ids=value.get('ids') if isinstance(value,dict) else None
    if not isinstance(ids,list) or any(not isinstance(x,str) or not ID.fullmatch(x) for x in ids) or len(set(ids))!=len(ids):
        raise UserError('API 排序文件无效，请从备份恢复排序文件。')
    ranks={ident:i for i,ident in enumerate(ids)}
    return sorted(profiles,key=lambda p:ranks.get(p['id'],len(ids)))


def revision(profiles):
    return hashlib.sha256('\n'.join(p['id'] for p in profiles).encode()).hexdigest()


def save(store,ids,expected):
    path=directory(store.root/'locks')/'profile-order.lock'
    if path.is_symlink():raise UserError('API 排序锁路径不安全。')
    with path.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        profiles=store.profiles()
        if revision(profiles)!=expected:raise UserError('API 列表已改变，请刷新后重新排序。')
        if not isinstance(ids,list) or any(not isinstance(x,str) for x in ids) or len(ids)!=len(profiles) or set(ids)!={p['id'] for p in profiles}:
            raise UserError('排序必须完整包含当前每个 API / 账号，且不能重复。')
        write_json(store.root/'profile-order.json',{'ids':ids})
        profiles=store.profiles()
        return {'profiles':profiles,'profile_order_revision':revision(profiles)}
