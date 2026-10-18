"""Authenticate a native stdio app-server without giving it a refresh token.

The desktop still speaks the native protocol. Only initialization, login and the
documented external-token refresh callback are handled here. Tool requests and
history are passed through unchanged by this layer.
"""
import json
import os
import selectors
import time
import uuid

MAX_LINE=64*1024*1024


def encode(value):
    return json.dumps(value,separators=(',',':')).encode()+b'\n'


class AuthBridge:
    def __init__(self,credentials,to_client,to_native,*,on_logout=lambda:None):
        self.credentials=credentials
        self.on_logout=on_logout
        self.to_client=to_client;self.to_native=to_native
        self.login_id='harbor-auth-'+uuid.uuid4().hex
        self.initialize_id=None;self.initialize_response=None
        self.ready=False;self.failed=False;self.detached=False
        self.access_token=None;self.account_id=None
        self.notifications=[];self.notification_bytes=0

    @staticmethod
    def error(ident,message):
        return encode({'id':ident,'error':{'code':-32000,'message':message}})

    def client(self,raw):
        value=json.loads(raw)
        method=value.get('method')
        if method=='initialize' and self.initialize_id is None and not self.ready:
            self.initialize_id=value['id']
            params=value.setdefault('params',{})
            params.setdefault('capabilities',{})['experimentalApi']=True
            self.to_native(encode(value));return
        if method=='initialized':return  # Sent once before the internal login.
        if method=='account/login/start':
            self.to_client(self.error(value.get('id'),'请在 Harbor 管理和重新登录此账号；当前窗口的账号来源保持不变。'))
            return
        if method=='account/logout':
            self.detached=True;self.access_token=None
            self.on_logout()
        if not self.ready:
            self.to_client(self.error(value.get('id'),'Harbor 尚未完成此窗口的官方账号连接。'))
            return
        self.to_native(raw)

    def server(self,raw):
        value=json.loads(raw)
        if value.get('method')=='account/chatgptAuthTokens/refresh':
            try:
                if self.detached:raise ValueError('Detached account')
                previous=(value.get('params') or {}).get('previousAccountId')
                payload=self.credentials(previous_account_id=previous or self.account_id,
                    rejected_access_token=self.access_token,force_refresh=True,timeout=8)
                if payload['chatgptAccountId']!=self.account_id:raise ValueError('Account changed')
                self.access_token=payload['accessToken']
                self.to_native(encode({'id':value['id'],'result':{k:v for k,v in payload.items() if k!='type'}}))
            except Exception:
                self.to_native(self.error(value.get('id'),'官方账号刷新未完成；请在 Harbor 查看登录状态或重新登录。'))
            return
        if self.initialize_id is not None and value.get('id')==self.initialize_id and not self.ready:
            if 'error' in value:
                self.failed=True;self.to_client(raw);return
            self.initialize_response=raw
            try:
                payload=self.credentials(timeout=20)
                self.access_token=payload['accessToken'];self.account_id=payload['chatgptAccountId']
                self.to_native(encode({'method':'initialized','params':{}}))
                self.to_native(encode({'id':self.login_id,'method':'account/login/start','params':payload}))
            except Exception:
                self.failed=True
                self.to_client(self.error(self.initialize_id,'Harbor 无法连接此官方账号，请退出该账号的旧版窗口，并在 Harbor 核对登录状态。'))
            return
        if value.get('id')==self.login_id:
            if 'error' in value:
                self.failed=True
                self.to_client(self.error(self.initialize_id,'当前 Codex 无法使用独立窗口登录；请核对 Codex 版本或在 Harbor 重新登录。'))
                return
            self.ready=True
            self.to_client(self.initialize_response)
            for notification in self.notifications:self.to_client(notification)
            self.notifications.clear();self.notification_bytes=0
            return
        if not self.ready:
            self.notification_bytes+=len(raw)
            if self.notification_bytes>2*1024*1024:raise ValueError('Initialization notification limit')
            # The desktop did not start this login, so it has no matching loginId.
            if value.get('method')!='account/login/completed':self.notifications.append(raw)
            return
        self.to_client(raw)


def run(proc,credentials,*,input_fd=0,output_fd=1,rewrite=lambda value:value,on_logout=lambda:None):
    """Bounded, nonblocking duplex transport; all tokens stay in private pipes."""
    native_in=proc.stdin.fileno();native_out=proc.stdout.fileno()
    queued={output_fd:bytearray(),native_in:bytearray()}
    received={input_fd:bytearray(),native_out:bytearray()}
    for fd in {input_fd,output_fd,native_in,native_out}:os.set_blocking(fd,False)
    bridge=AuthBridge(credentials,lambda raw:queued[output_fd].extend(raw),
                      lambda raw:queued[native_in].extend(rewrite(raw)),on_logout=on_logout)
    deadline=time.monotonic()+35
    input_closed=False
    try:
        with selectors.DefaultSelector() as selector:
            # A native process can exit with a final response still in its pipe.
            # Drain that pipe to EOF rather than treating process exit as EOF.
            while native_out in received or queued[output_fd]:
                if not bridge.ready and time.monotonic()>deadline:raise TimeoutError('Official initialization timed out')
                desired={fd:selectors.EVENT_READ for fd in received}
                desired.update({fd:selectors.EVENT_WRITE for fd,data in queued.items() if data})
                if any(len(data)>MAX_LINE for data in [*queued.values(),*received.values()]):
                    raise ValueError('RPC buffer limit exceeded')
                for key in list(selector.get_map().values()):
                    if key.fd not in desired:selector.unregister(key.fd)
                for fd,events in desired.items():
                    if fd not in selector.get_map():selector.register(fd,events)
                    else:selector.modify(fd,events)
                for key,events in selector.select(.2):
                    fd=key.fd
                    if events&selectors.EVENT_WRITE:
                        try:written=os.write(fd,queued[fd])
                        except BlockingIOError:continue
                        del queued[fd][:written]
                    else:
                        try:chunk=os.read(fd,65536)
                        except BlockingIOError:continue
                        if not chunk:
                            pending=received.pop(fd)
                            if pending:
                                (bridge.client if fd==input_fd else bridge.server)(bytes(pending))
                            if fd==input_fd:input_closed=True
                            continue
                        pending=received[fd];pending.extend(chunk)
                        while (end:=pending.find(b'\n'))>=0:
                            if end>MAX_LINE:raise ValueError('RPC line limit exceeded')
                            raw=bytes(pending[:end+1]);del pending[:end+1]
                            (bridge.client if fd==input_fd else bridge.server)(raw)
                if input_closed and not queued[native_in] and not proc.stdin.closed:
                    if native_in in selector.get_map():selector.unregister(native_in)
                    proc.stdin.close()
                if bridge.failed and not queued[output_fd]:
                    if proc.poll() is None:proc.terminate()
                    return False
                if not received and not any(queued.values()):break
        return not bridge.failed
    finally:
        # stdout belongs to the wrapper; never close desktop-owned descriptors.
        for fd in (input_fd,output_fd):
            try:os.set_blocking(fd,True)
            except OSError:pass
