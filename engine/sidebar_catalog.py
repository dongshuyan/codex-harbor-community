"""Request native sidebar reconciliation before starting an idle private window.

The caller must own that window's desktop/backend slot leases. Shared history
is read-only. Never copy a desktop database: it also holds account/cloud state.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import stat

from core import UserError, write_json
from storage import safe_path


@contextmanager
def _connect(path, *, readonly):
    safe_path(path.parent)
    if path.is_symlink():
        raise UserError('会话索引路径不能是符号链接；未修改索引。')
    db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                         uri=True, timeout=0.25)
    try:
        db.row_factory = sqlite3.Row
        if readonly:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
        with db:
            yield db
    finally:
        db.close()


def _require(db, table, columns):
    actual = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
    if not set(columns).issubset(actual):
        raise UserError('Codex 的会话索引格式尚未验证；未修改数据，请先检查版本兼容性。')


def _rollout_exists(path):
    for candidate in (Path(path), Path(str(path) + '.zst')):
        try:
            if stat.S_ISREG(candidate.stat().st_mode):
                return True
        except FileNotFoundError:
            pass
    return False


def binding(history):
    from storage import volume_identity
    return {'version':2,'history':str(Path(history).resolve()),'volume_uuid':volume_identity(Path(history))}


def prepare_catalog(home, history):
    """Detect missing/grouped-wrong entries; let native Codex populate its cache.

    The predicate follows Codex's native local sidebar list (including nonempty
    preview, not the obsolete has_user_event flag). We read no chat bodies.
    An interrupted flag/marker write is safe to repeat. Existing catalog rows,
    observation sequences, other hosts and original history are never rewritten.
    """
    home, history = Path(home), Path(history)
    cache = home / 'sqlite/codex-dev.db'
    result = {'rescan': False, 'missing': 0, 'misgrouped': 0, 'extra': 0, 'unavailable': 0}
    if not cache.exists() and not cache.is_symlink():
        return result  # A new native profile builds its own catalog.
    marker = home / '.harbor-catalog-binding.json'
    try:
        safe_path(home)
        if marker.is_symlink():
            raise UserError('会话索引归属标记不能是符号链接。')
        previous = json.loads(marker.read_text()) if marker.exists() else None
        selected_binding = binding(history)
        changed_history = previous != selected_binding
        with _connect(cache, readonly=True) as db:
            _require(db, 'local_thread_catalog',
                     ['host_id', 'thread_id', 'cwd', 'project_id', 'missing_candidate'])
            _require(db, 'local_thread_catalog_sync_state',
                     ['host_id', 'initial_build_complete', 'last_full_reconciled_at',
                      'watermark_updated_at', 'observation_sequence'])
            _require(db, 'local_thread_catalog_metadata', ['id', 'catalog_revision'])
            _require(db, 'local_thread_catalog_scan_checkpoints', ['host_id'])
            existing = {r['thread_id']: dict(r) for r in db.execute(
                "SELECT thread_id,cwd,project_id,missing_candidate FROM local_thread_catalog WHERE host_id='local'")}
        source = history / 'state_5.sqlite'
        if source.exists() or source.is_symlink():
            with _connect(source, readonly=True) as db:
                _require(db, 'threads', ['id', 'cwd', 'project_id', 'source', 'archived',
                                          'preview', 'rollout_path'])
                rows = db.execute("SELECT id,cwd,project_id,rollout_path FROM threads "
                                  "WHERE archived=0 AND preview<>'' AND source IN ('cli','vscode')").fetchall()
            for row in rows:
                cached = existing.get(row['id'])
                missing = cached is None or cached['missing_candidate'] != 0
                # Installed desktop intentionally stores project_id=NULL for
                # local entries. Membership lives in the native project view,
                # not in this cache's ChatGPT-project column.
                misgrouped = cached is not None and (cached['cwd'] != row['cwd'] or
                                                      cached['project_id'] is not None)
                if not (missing or misgrouped):
                    continue
                if not _rollout_exists(row['rollout_path']):
                    result['unavailable'] += 1
                    continue
                result['missing'] += int(missing)
                result['misgrouped'] += int(misgrouped)
            expected = {row['id'] for row in rows}
            result['extra'] = sum(ident not in expected and row['missing_candidate'] == 0
                                  for ident, row in existing.items())
        else:
            changed_history = True  # Native initialization has not completed.
        result['rescan'] = bool(changed_history or result['missing'] or result['misgrouped'] or result['extra'])
        from organization import read_object
        from catalog_verification import snapshot,titles_consistent
        captured=snapshot(home,history)
        receipt_path=home/'.harbor-catalog-receipt.json'
        previous_receipt=read_object(receipt_path)
        if captured:
            result['metadata']=captured['metadata']
            # Unavailable original files cannot be restored by rescanning a UI
            # cache; report them separately, never invent replacement history.
            if captured['metadata'] or (not result['unavailable'] and not titles_consistent(previous_receipt,captured)):
                result['rescan']=True
        if result['rescan']:
            with _connect(cache, readonly=False) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE local_thread_catalog_sync_state SET initial_build_complete=0, "
                           "last_full_reconciled_at=NULL,watermark_updated_at=NULL WHERE host_id='local'")
                db.execute("DELETE FROM local_thread_catalog_scan_checkpoints WHERE host_id='local'")
                db.execute('UPDATE local_thread_catalog_metadata SET catalog_revision=catalog_revision+1 WHERE id=1')
        if previous != selected_binding:
            write_json(marker, selected_binding)
        return result
    except (OSError, sqlite3.Error, ValueError) as error:
        if isinstance(error, UserError):
            raise
        raise UserError('无法核对当前窗口的会话索引；未改动原始历史，请检查目录可用性或稍后重试。') from error


def verify_catalog(home,history,*,write_receipt=True):
    """Called after native launch; never confuse a running PID with complete UI."""
    from catalog_verification import snapshot,audit_rollouts,receipt
    home,history=Path(home),Path(history)
    try:
        captured=snapshot(home,history)
        if captured is None:return {'status':'loading','message':'正在建立原生会话目录。'}
        result={k:captured[k] for k in ('missing','extra','metadata')}
        result.update(status='loading',message='原生会话目录仍在加载。')
        if not captured['native_complete']:return result
        audit=audit_rollouts(history,captured['source'],
                             missing_cache_ids=captured['expected'].keys()-captured['active'].keys())
        # SQLite snapshots are short and independent. Re-read only after the
        # filesystem audit, so an ordinary concurrent edit cannot certify a
        # mixture of two different source/cache generations.
        later=snapshot(home,history)
        if later is None or not later['native_complete'] or any(
                later[key]!=captured[key] for key in ('source_digest','cache_digest')):
            result['message']='检查期间有新会话变化，正在重新核对。'
            return result
        result['audit']=audit
        if any(audit[k] for k in ('missing_rollouts','unindexed_rollouts','uncertain_paths')):
            result.update(status='unverified',message=(f"历史清单尚未对齐：{audit['missing_rollouts']} 条索引引用暂不可用，"
                          f"{audit['unindexed_rollouts']} 个历史文件尚未入索引，{audit['uncertain_paths']} 个路径待核对。原始文件未修改。"))
            return result
        if any(result[k] for k in ('missing','extra','metadata')):return result
        # Observed native output, not a reimplementation of native markdown
        # title normalization. Subsequent title-only damage invalidates this
        # receipt and requires another native full reconciliation.
        if write_receipt:write_json(home/'.harbor-catalog-receipt.json',receipt(captured))
        else:result['_receipt']=receipt(captured)
        result.update(status='verified',message='已核对当前会话目录。')
        return result
    except (OSError,sqlite3.Error,ValueError,UserError) as exc:
        return {'status':'unverified','message':str(exc) if isinstance(exc,UserError) else '会话完整性检查暂未完成；未改动原始历史。'}
