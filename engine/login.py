"""Native Codex browser login in one account's private home. No model calls.

The authorization URL exists only in an anonymous pipe and the browser. Status
files contain an allowlisted state and a masked label, never server payloads.
The local UI may display the current credential's email, never its tokens.
"""
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parent))
from core import CODEX, DEFAULT_ROOT, ID, Store, UserError, directory, process_matches, process_stamp, write_json
from accounts import account_home, take_lock


def check_url(value):
    if not isinstance(value,str): raise UserError('未取得有效授权地址。')
    url=urlsplit(value)
    if (url.scheme!='https' or url.hostname!='auth.openai.com' or url.username or url.password
            or url.port not in (None,443)):
        raise UserError('授权地址不是已验证的 OpenAI 登录地址，未打开。')
    return value


def masked_account(value):
    account=value.get('account') or {}
    email=account.get('email') if isinstance(account,dict) else None
    if not isinstance(email,str) or '@' not in email: return 'GPT 官方账号'
    local,domain=email.rsplit('@',1)
    return local[:2]+'***@'+domain[:100]


def state_path(store, ident):
    if not ID.fullmatch(ident): raise UserError('账号标识无效。')
    return directory(store.root/'login')/f'{ident}.json'


def local_account_email(auth):
    """Read display-only claims, as Codex does. This does not validate a login.

    Read only this Harbor account's regular file, without following symlinks or
    modifying/refreshing credentials. Do not expose any other claims or tokens.
    A concurrent credential replacement or malformed file simply hides the label.
    """
    try:
        if any(p.is_symlink() for p in [auth.parent, auth.parent.parent, auth.parent.parent.parent]):return None
        fd=os.open(auth,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024:return None
            raw=handle.read(1024*1024+1)
        if len(raw)>1024*1024:return None
        data=json.loads(raw)
        if not isinstance(data,dict) or data.get('auth_mode') not in (None,'chatgpt'):return None
        tokens=data.get('tokens')
        token=tokens.get('id_token') if isinstance(tokens,dict) else None
        if not isinstance(token,str) or len(token)>65536:return None
        parts=token.split('.')
        if len(parts)!=3:return None
        claims=json.loads(base64.b64decode(parts[1]+'='*(-len(parts[1])%4),altchars=b'-_',validate=True))
        if not isinstance(claims,dict):return None
        profile=claims.get('https://api.openai.com/profile')
        email=claims.get('email') or (profile.get('email') if isinstance(profile,dict) else None)
        if (not isinstance(email,str) or len(email)>320 or email.count('@')!=1
                or any(not c.isprintable() or c.isspace() for c in email)
                or not all(email.split('@'))):return None
        return email
    except (OSError,ValueError,UnicodeError,RecursionError):return None


def snapshot(store, ident):
    path=state_path(store,ident)
    if path.is_symlink(): raise UserError('登录状态路径不安全。')
    auth=store.root/'accounts'/ident/'codex'/'auth.json'
    present=auth.is_file() and not auth.is_symlink()
    from quota import credential_revision
    revision_before=credential_revision(auth)
    email=local_account_email(auth) if present else None
    revision_after=credential_revision(auth)
    # Native Codex may atomically replace credentials while this snapshot is read.
    # Never label a new credential generation with the previous account's email.
    revision=revision_after if revision_before==revision_after else None
    if revision is None:email=None
    identity={'account_email':email} if email else {}
    identity['credential_revision']=revision
    if path.is_file():
        value=json.loads(path.read_text())
        if value.get('state') in ['waiting','starting'] and not process_matches(value):
            value={'state':'error','message':'授权进程已退出，请重新发起登录。'}
        result={k:value[k] for k in ['state','message'] if k in value}
        if present and value.get('masked_account'):
            result['masked_account']=value['masked_account']
        return dict(result,credential_present=present,**identity)
    return dict(state='stored' if present else 'none',credential_present=present,**identity)


class RPC:
    def __init__(self, command, env, cwd, *, pass_fds=()):
        self.proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env=env,cwd=cwd,close_fds=True,pass_fds=pass_fds)
        self.buffer=b''
        self.pending=[]

    def send(self, method, params, ident=None):
        value=dict(method=method,params=params)
        if ident is not None:value['id']=ident
        self.proc.stdin.write(json.dumps(value).encode()+b'\n');self.proc.stdin.flush()

    def read(self, deadline):
        while b'\n' not in self.buffer:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            ready,_,_=select.select([self.proc.stdout],[],[],min(remaining,.2))
            if not ready:
                if self.proc.poll() is not None:raise UserError('Codex 授权组件已退出。')
                continue
            raw=os.read(self.proc.stdout.fileno(),65536)
            if not raw:raise UserError('Codex 授权组件已关闭连接。')
            self.buffer+=raw
            if len(self.buffer)>2*1024*1024:raise UserError('授权组件返回异常数据。')
        line,self.buffer=self.buffer.split(b'\n',1)
        return json.loads(line)

    def request(self, method, params, ident, deadline):
        self.send(method,params,ident)
        while True:
            message=self.read(deadline)
            if message.get('id')==ident:
                if 'error' in message:raise UserError('Codex 拒绝本次授权操作；请核对版本或取消其他登录流程。')
                return message.get('result') or {}
            self.pending.append(message)
            if len(self.pending)>200:raise UserError('授权通知过多，已停止等待。')

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait()
        self.proc.stdin.close();self.proc.stdout.close()


def run_flow(store, ident, publish, *, command=None, timeout=300):
    state=state_path(store,ident)
    cancel=state.with_suffix('.cancel')
    rpc=None;login_id=None;final_state=False
    failure={'state':'error','message':'授权未完成。请核对 Codex 是否可用，以及是否已有其他浏览器登录流程。'}
    metadata={'pid':os.getpid(),'pid_start':process_stamp(os.getpid())}
    def update(value):
        nonlocal final_state
        write_json(state,dict(value,**metadata))
        final_state=value['state'] not in ['starting','waiting']
        return value
    def finish_state():
        if not final_state:update(failure)
    try:
        with ExitStack() as guards:
            global_lease=guards.enter_context(take_lock(directory(store.root/'locks')/'browser-login.lock'))
            lease_fds=guards.enter_context(store.edit_guard(ident))
            if store.profile(ident)['kind']!='official':raise UserError('浏览器登录仅用于官方账号。')
            # Registered after the leases but before RPC.close: on failure the
            # child exits, the final state is published, then locks are released.
            guards.callback(finish_state)
            if cancel.is_symlink():raise UserError('取消标记不安全。')
            cancel.unlink(missing_ok=True)
            home=account_home(store,ident)
            osh=directory(store.root/'accounts'/ident/'login-os-home')
            env={'HOME':str(osh),'ZDOTDIR':str(osh),'CODEX_HOME':str(home),
                 'PATH':'/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin','LANG':'en_US.UTF-8'}
            args=command or [str(CODEX),'-c','cli_auth_credentials_store="file"',
                '-c','analytics.enabled=false','-c','features.remote_plugin=false','-c','features.apps=false',
                '-c','features.runtime_metrics=false','app-server']
            update({'state':'starting','message':'正在打开 Codex 官方授权流程。'})
            rpc=RPC(args,env,osh,pass_fds=(global_lease.fileno(),*lease_fds))
            # Reap the app-server before releasing the account/global leases.
            guards.callback(rpc.close)
            deadline=time.monotonic()+timeout
            rpc.request('initialize',{'clientInfo':{'name':'codex_harbor','title':'Codex Harbor','version':'0.4.0'}},1,min(deadline,time.monotonic()+10))
            rpc.send('initialized',{})
            response=rpc.request('account/login/start',{'type':'chatgpt'},2,min(deadline,time.monotonic()+10))
            url=check_url(response.get('authUrl'));login_id=response.get('loginId')
            if not isinstance(login_id,str):raise UserError('未取得登录流程标识。')
            update({'state':'waiting','message':'请在浏览器完成授权；最多等待 5 分钟。'})
            publish({'auth_url':url})
            while time.monotonic()<deadline:
                if cancel.exists():
                    rpc.send('account/login/cancel',{'loginId':login_id},4)
                    return update({'state':'cancelled','message':'已取消登录。'})
                try:message=rpc.pending.pop(0) if rpc.pending else rpc.read(min(deadline,time.monotonic()+.25))
                except TimeoutError:continue
                if message.get('method')=='account/login/completed':
                    params=message.get('params') or {}
                    if params.get('loginId')!=login_id:continue
                    if params.get('success') is not True:raise UserError('OpenAI 授权未完成，请重新发起登录。')
                    account=rpc.request('account/read',{'refreshToken':False},3,min(deadline,time.monotonic()+8))
                    return update({'state':'success','masked_account':masked_account(account),'message':'登录已完成；尚未发送模型请求。'})
            if login_id:rpc.send('account/login/cancel',{'loginId':login_id},4)
            return update({'state':'timeout','message':'等待授权超时；未发送模型请求。'})
    except (UserError,OSError,ValueError,TimeoutError):
        # A rejected second worker must not replace the active login's status.
        return failure


def start(store,ident):
    if store.running_profile(ident):raise UserError('请先退出此账号的窗口或后台任务，再重新授权。')
    store.profile(ident)
    proc=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),str(store.root),str(store.external),ident],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,
        env={'HOME':str(Path.home()),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin','LANG':'en_US.UTF-8'})
    try:
        ready,_,_=select.select([proc.stdout],[],[],24)
        if not ready:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            raise UserError('登录组件启动超时，没有打开授权网页。')
        line=proc.stdout.readline(32768)
        value=json.loads(line)
        if 'auth_url' not in value:raise UserError('登录组件未就绪，请关闭其他授权流程后再试。')
        check_url(value['auth_url'])
        return value
    finally:proc.stdout.close()


def cancel(store,ident):
    from core import atomic_write
    path=state_path(store,ident)
    atomic_write(path.with_suffix('.cancel'),'cancel\n')
    return {'message':'已请求取消登录。'}


if __name__=='__main__':
    def terminate(_sig,_frame):raise UserError('登录进程收到退出请求。')
    signal.signal(signal.SIGTERM,terminate)
    signal.signal(signal.SIGINT,terminate)
    published=False
    def publish(value):
        global published
        print(json.dumps(value),flush=True);published=True
        sys.stdout.close()
    try:
        root,external,ident=sys.argv[1:]
        store=Store(Path(root),Path(external))
        result=run_flow(store,ident,publish)
        if not published:print(json.dumps({'error':'login_unavailable'}),flush=True)
    except Exception:
        if not published:print(json.dumps({'error':'login_unavailable'}),flush=True)
