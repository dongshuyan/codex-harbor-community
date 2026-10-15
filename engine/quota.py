"""Read official quotas through bundled Codex, without owning token refresh.

Only the current access token and account ID enter an anonymous RPC pipe. A
fresh temporary CODEX_HOME prevents config/history/cookie cross-contamination.
The long-lived auth file is never written, copied or supplied to the child.
"""
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import select
import stat
import tempfile
import time

from core import CODEX, UserError, directory
from accounts import take_lock
from login import RPC

TIMEOUT = 20


def _regular_auth(path):
    if any(p.is_symlink() for p in [path, *path.parents]):
        # macOS's /var and /tmp are system aliases; Store roots are resolved.
        raise OSError('unsafe auth path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1024*1024:
        raise OSError('invalid auth file')
    return info


def _revision(info):
    return hashlib.sha256(f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'.encode()).hexdigest()


def credential_revision(path):
    """Opaque file-generation marker, not a token or account identifier."""
    try:return _revision(_regular_auth(path))
    except OSError:return None


def credentials(path):
    _regular_auth(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1024*1024:
            raise ValueError('invalid auth')
        raw = handle.read(1024*1024+1)
    if len(raw) > 1024*1024:raise ValueError('invalid auth')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('auth_mode') not in (None, 'chatgpt'):
        raise ValueError('not official auth')
    tokens = value.get('tokens')
    if not isinstance(tokens, dict):raise ValueError('missing auth')
    token, account = tokens.get('access_token'), tokens.get('account_id')
    for item, maximum in [(token,65536),(account,512)]:
        if not isinstance(item,str) or not item or len(item)>maximum or any(not c.isprintable() or c.isspace() for c in item):
            raise ValueError('invalid auth')
    return {'type':'chatgptAuthTokens','accessToken':token,'chatgptAccountId':account}, _revision(info)


class QuotaFailure(Exception):
    def __init__(self, state):self.state=state


class QuotaRPC(RPC):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        os.set_blocking(self.proc.stdin.fileno(),False)

    def send_before(self, method, params, ident, deadline):
        value={'method':method,'params':params}
        if ident is not None:value['id']=ident
        pending=memoryview(json.dumps(value).encode()+b'\n')
        descriptor=self.proc.stdin.fileno()
        while pending:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            _,ready,_=select.select([],[descriptor],[],min(remaining,.2))
            if not ready:
                if self.proc.poll() is not None:raise QuotaFailure('unavailable')
                continue
            try:written=os.write(descriptor,pending)
            except BlockingIOError:continue
            if written<=0:raise QuotaFailure('unavailable')
            pending=pending[written:]

    def request(self, method, params, ident, deadline):
        self.send_before(method, params, ident, deadline)
        for _ in range(256):
            message = self.read(deadline)
            if not isinstance(message, dict):raise QuotaFailure('unavailable')
            if message.get('method') == 'account/chatgptAuthTokens/refresh':
                # Never rotate another process's refresh token, even on 401.
                raise QuotaFailure('auth_required')
            if message.get('method') is not None:continue
            if message.get('id') != ident:continue
            error = message.get('error')
            if error is not None:
                detail = str(error).lower()  # Classified only; never logged/displayed.
                if any(x in detail for x in ['401', 'unauthorized', 'expired', 'authentication required']):
                    raise QuotaFailure('auth_required')
                if isinstance(error,dict) and error.get('code') in (-32601,-32602):
                    raise QuotaFailure('unsupported')
                raise QuotaFailure('unavailable')
            result=message.get('result')
            if not isinstance(result,dict):raise QuotaFailure('unavailable')
            return result
        raise QuotaFailure('unavailable')


def query(auth, *, command=None, timeout=TIMEOUT):
    with tempfile.TemporaryDirectory(prefix='harbor-quota-') as temporary:
        root=Path(temporary).resolve()
        home=directory(root/'codex')
        env={'HOME':str(root),'CODEX_HOME':str(home),'ZDOTDIR':str(root),
             'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'}
        args=command or [str(CODEX),'-c','analytics.enabled=false',
            '-c','features.remote_plugin=false','-c','features.apps=false',
            '-c','features.runtime_metrics=false','app-server']
        deadline=time.monotonic()+timeout
        rpc=QuotaRPC(args,env,root)
        try:
            rpc.request('initialize',{'clientInfo':{'name':'codex_harbor_quota','version':'0.7.0'},
                                     'capabilities':{'experimentalApi':True}},1,deadline)
            rpc.send_before('initialized',{},None,deadline)
            result=rpc.request('account/login/start',auth,2,deadline)
            if result.get('type')!='chatgptAuthTokens':raise QuotaFailure('unsupported')
            return rpc.request('account/rateLimits/read',{'excludeResetCreditDetails':True},3,deadline)
        finally:rpc.close()


def _number(value, *, positive=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return None
    if positive and (value<=0 or value!=int(value) or value>253402300799):return None
    return value


def _label(value, fallback):
    return value if isinstance(value,str) and 0<len(value)<=100 and all(c.isprintable() for c in value) else fallback


def normalize(value):
    buckets=value.get('rateLimitsByLimitId')
    if not isinstance(buckets,dict) or not buckets:
        legacy=value.get('rateLimits')
        buckets={'codex':legacy} if isinstance(legacy,dict) else {}
    result=[]
    for key, bucket in sorted(buckets.items(),key=lambda pair:(pair[0]!='codex',str(pair[0])))[:32]:
        if not isinstance(bucket,dict):continue
        windows=[]
        for slot in ('primary','secondary'):
            raw=bucket.get(slot)
            if not isinstance(raw,dict):continue
            used=_number(raw.get('usedPercent'))
            windows.append({'id':slot,'remaining_percent':None if used is None else max(0,min(100,100-used)),
                            'duration_minutes':_number(raw.get('windowDurationMins'),positive=True),
                            'resets_at':_number(raw.get('resetsAt'),positive=True)})
        if windows:
            ident=_label(key,'unknown')
            result.append({'id':ident,'name':_label(bucket.get('limitName'),'Codex' if ident=='codex' else ident),
                           'windows':windows})
    return result


def fetch(store, ident, *, command=None, timeout=TIMEOUT):
    """Shared read lease permits an active desktop; edits/login remain exclusive."""
    if store.profile(ident)['kind']!='official':raise UserError('额度查询仅用于 GPT 官方账号。')
    result={'state':'busy'}
    try:
        with ExitStack() as guards:
            guards.enter_context(store.operation_guard(ident,shared=True))
            guards.enter_context(take_lock(directory(store.root/'locks')/f'quota-{ident}.lock'))
            if store.profile(ident)['kind']!='official':raise UserError('账号类型已变化。')
            path=store.root/'accounts'/ident/'codex'/'auth.json'
            try:auth,revision=credentials(path)
            except (OSError,ValueError,RecursionError):return {'state':'auth_required'}
            result={'credential_revision':revision}
            try:
                buckets=normalize(query(auth,command=command,timeout=timeout))
                if credential_revision(path)!=revision:return {'state':'changed'}
                result.update(state='ready' if buckets else 'empty',buckets=buckets,checked_at=time.time())
            except QuotaFailure as error:result['state']=error.state
            except TimeoutError:result['state']='timeout'
            except (OSError,ValueError,UserError,RecursionError):result['state']='unavailable'
    except UserError:return {'state':'busy'}
    return result
