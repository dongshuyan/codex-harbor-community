"""One short startup gate for all controlled native/Harbor entry points.

It protects preparation through macOS callback, not the desktop lifetime.
Unresolved OS intents are checked under the gate, including after worker death.
"""
import fcntl
import os
from pathlib import Path
from core import UserError,launch_uncertain
from organization import Organization,read_object


def acquire(store,job):
    org=Organization(store)
    fd=os.open(org.root/'startup.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    handle=os.fdopen(fd,'a')
    try:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError('另一个窗口正在核对启动状态，请稍后重试。') from None
        with org.transaction() as value:roots=list(value['control_roots'])
        for root in roots:
            for path in (Path(root)/'jobs').glob('*.json'):
                item=read_object(path)
                if item and item.get('job')!=job and launch_uncertain(item):
                    raise UserError('上一次系统启动结果尚未确认；已保留记录，请先核对对应窗口。')
        return handle
    except BaseException:
        handle.close();raise
