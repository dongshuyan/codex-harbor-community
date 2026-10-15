"""Backup/restore the explicitly shared macOS preference domain. Never auto-restore."""
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import uuid
from core import UserError,directory,write_json

DOMAIN='com.openai.codex'


def lifecycle_lock(root,exclusive=False):
    path=directory(Path(root)/'recovery')/'lifecycle.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    handle=os.fdopen(fd,'r+')
    try:fcntl.flock(handle,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close();raise UserError('Harbor 正在启动、运行或恢复；请先退出测试实例后再操作。') from None
    return handle


def verified_snapshot(root):
    dest=directory(Path(root)/'recovery');manifest=dest/'macos-before-first-launch.json'
    try:
        if manifest.is_symlink():raise ValueError()
        data=json.loads(manifest.read_text())
        name=data.get('file','com.openai.codex.plist')
        if not re.fullmatch(r'com\.openai\.codex(?:-[a-f0-9]{32})?\.plist',name):raise ValueError()
        file=dest/name
        if data.get('domain')!=DOMAIN or data.get('existed') is not True:raise ValueError()
        if not isinstance(data.get('handler'),str) or not re.fullmatch(r'[\w.-]+',data['handler']):raise ValueError()
        if file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest()!=data.get('sha256'):raise ValueError()
        return manifest,file,data
    except (ValueError,OSError,TypeError,KeyError):
        raise UserError('macOS 偏好备份缺失或校验失败，已停止操作；请保留 recovery 目录检查。') from None


def url_handler(value=None):
    cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    ls=ctypes.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
    cf.CFStringCreateWithCString.restype=ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32]
    cf.CFStringGetCString.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_long,ctypes.c_uint32]
    cf.CFRelease.argtypes=[ctypes.c_void_p]
    scheme=cf.CFStringCreateWithCString(None,b'codex',0x08000100)
    try:
        if value is not None:
            ls.LSSetDefaultHandlerForURLScheme.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
            handler=cf.CFStringCreateWithCString(None,value.encode(),0x08000100)
            try:
                if ls.LSSetDefaultHandlerForURLScheme(scheme,handler)!=0:raise UserError('链接打开方式恢复失败。')
            finally:cf.CFRelease(handler)
            return value
        ls.LSCopyDefaultHandlerForURLScheme.argtypes=[ctypes.c_void_p]
        ls.LSCopyDefaultHandlerForURLScheme.restype=ctypes.c_void_p
        handler=ls.LSCopyDefaultHandlerForURLScheme(scheme)
        if not handler:return None
        try:
            buf=ctypes.create_string_buffer(4096)
            if not cf.CFStringGetCString(handler,buf,len(buf),0x08000100):raise UserError('无法读取链接打开方式。')
            return buf.value.decode()
        finally:cf.CFRelease(handler)
    finally:cf.CFRelease(scheme)


def snapshot(root):
    dest=directory(Path(root)/'recovery')
    lock=dest/'backup.lock'
    if lock.is_symlink():raise UserError('备份锁路径不安全。')
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        manifest=dest/'macos-before-first-launch.json'
        if manifest.exists():return str(verified_snapshot(root)[0])
        handler=url_handler()
        if not handler:raise UserError('当前没有 codex:// 默认处理器，无法保证恢复，已停止启动。')
        file=dest/f'com.openai.codex-{uuid.uuid4().hex}.plist'
        result=subprocess.run(['/usr/bin/defaults','export',DOMAIN,'-'],capture_output=True,timeout=10)
        if result.returncode:raise UserError('无法完整导出 macOS 界面偏好，已停止启动。请先确认原版 Codex 可以正常运行。')
        # A failed manifest commit leaves an unreferenced immutable file. Retry
        # writes a new file; the first complete committed snapshot is never replaced.
        fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(result.stdout);f.flush();os.fsync(f.fileno())
        write_json(manifest,{'domain':DOMAIN,'file':file.name,'existed':True,'handler':handler,
                            'sha256':hashlib.sha256(result.stdout).hexdigest()})
        return str(manifest)


def restore(root):
    with lifecycle_lock(root,exclusive=True):return _restore(root)


def _restore(root):
    # Restoring shared UI state while any Codex is running would race its cache.
    result=subprocess.run(['/usr/bin/pgrep','-x','ChatGPT|Codex'],capture_output=True,timeout=3)
    if result.returncode==0:raise UserError('请先退出所有 Codex 窗口（包括旧版），再在系统终端执行恢复。')
    if result.returncode!=1:raise UserError('无法检查 Codex 是否已退出，未执行恢复。')
    _,file,data=verified_snapshot(root)
    cmd=['/usr/bin/defaults','import',DOMAIN,str(file)]
    result=subprocess.run(cmd,capture_output=True,timeout=10)
    if result.returncode:raise UserError('偏好恢复未成功，请保留备份。')
    verify=subprocess.run(['/usr/bin/defaults','export',DOMAIN,'-'],capture_output=True,timeout=10)
    if verify.returncode or plistlib.loads(verify.stdout)!=plistlib.loads(file.read_bytes()):
        raise UserError('偏好恢复后的读回校验未通过，请保留备份。')
    url_handler(data['handler'])
    if url_handler()!=data['handler']:raise UserError('链接打开方式的读回校验未通过，请保留备份。')
    return {'restored':True,'message':'已恢复首次启动 Harbor 前的 macOS 界面偏好及 codex:// 打开方式。账号配置与历史未改动。'}
