"""Authenticated loopback Responses gateway; no protocol conversion or retries."""
from __future__ import annotations

import hmac
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from core import UserError,map_request
from http_transport import Exchange, NoRedirect, error_kind

MAX_BODY=64*1024*1024
CONNECT_TIMEOUT=30
READ_TIMEOUT=120


class Gateway:
    def __init__(self,profile,key,record=lambda _:None):
        if profile['protocol']!='responses': raise UserError('此版本仅支持 OpenAI Responses。')
        self.profile=profile
        self.key=key
        self.record=record
        self.log_failed=False
        self.token=secrets.token_urlsafe(40)
        self.limit=threading.BoundedSemaphore(8)
        self.lock=threading.Lock()
        self.exchanges=set()
        self.closed=False
        self.thread=None
        gateway=self

        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.settimeout(150)

            def log_message(self,*args): pass

            def error(self,status,message):
                data=json.dumps({'error':{'message':message,'type':'smart2_gateway_error'}}).encode()
                self.close_connection=True
                try:
                    self.send_response(status)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(data)))
                    self.send_header('Connection','close')
                    self.end_headers();self.wfile.write(data);self.wfile.flush()
                except OSError:
                    pass  # Cancellation never causes a second upstream attempt.

            def authorized(self):
                value=self.headers.get('Authorization','')
                return value.isascii() and hmac.compare_digest(value,'Bearer '+gateway.token)

            def do_GET(self):
                if not self.authorized(): return self.error(401,'Harbor 本地请求身份不匹配。')
                if self.path=='/v1/responses' and self.headers.get('Upgrade','').lower()=='websocket':
                    # Current native Codex ignores prefer_websockets=False and
                    # uses 426 to select HTTP immediately (404 causes retries).
                    return self.error(426,'请使用 HTTP Responses 传输。')
                if self.path!='/v1/models': return self.error(404,'不支持这个接口。')
                data=json.dumps({'object':'list','data':[{'id':m['alias'],'object':'model'}
                                  for m in gateway.profile['models']]}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

            def do_POST(self):
                if not self.authorized(): return self.error(401,'Harbor 本地请求身份不匹配。')
                if self.path not in ('/v1/responses','/v1/responses/compact'):
                    return self.error(404,'仅允许 Responses 与 Responses compact 接口。')
                if not gateway.limit.acquire(blocking=False): return self.error(429,'本地并发请求过多。')
                started=False
                alias=None;actual=None;status=502
                outcome='rejected';failure=None;operation='upload'
                exchange=Exchange(self.connection,read_timeout=READ_TIMEOUT)
                with gateway.lock:
                    stopping=gateway.closed
                    if not stopping:gateway.exchanges.add(exchange)
                try:
                    if stopping:
                        status=503;failure='stopping'
                        return self.error(status,'本地转发器正在关闭。')
                    if self.headers.get('Transfer-Encoding'):
                        status=400;failure='invalid_request'
                        return self.error(status,'请求必须带 Content-Length。')
                    sizes=self.headers.get_all('Content-Length',[])
                    if len(sizes)!=1:
                        status=400;failure='invalid_request'
                        return self.error(status,'请求必须带唯一 Content-Length。')
                    size=int(sizes[0])
                    if not 0<size<=MAX_BODY:
                        status=413;failure='invalid_request'
                        return self.error(status,'请求大小无效或超过 64 MiB。')
                    if self.headers.get('Content-Encoding','identity')!='identity':
                        status=415;failure='invalid_request'
                        return self.error(status,'请关闭请求压缩。')
                    raw=self.rfile.read(size)
                    if len(raw)!=size:
                        status=400;failure='invalid_request'
                        return self.error(status,'请求不完整。')
                    request=json.loads(raw)
                    if not isinstance(request,dict): raise UserError('请求必须是 JSON 对象。')
                    body=map_request(gateway.profile,request)
                    # Only validated, configured model names enter metadata.
                    alias=request['model']
                    actual=body['model']
                    upstream=gateway.profile['url']+self.path.removeprefix('/v1')
                    req=urllib.request.Request(upstream,data=json.dumps(body).encode(),headers={
                        'Authorization':'Bearer '+gateway.key,'Content-Type':'application/json',
                        'Accept':'text/event-stream' if body.get('stream') else 'application/json',
                        'Accept-Encoding':'identity','User-Agent':'Codex-Harbor/0.4'})
                    threading.Thread(target=exchange.watch,daemon=True).start()
                    operation='upstream'
                    # Every request owns its sockets. Keep Router's existing
                    # explicit no-ambient-proxy policy and refuse redirects.
                    with exchange.opener(proxies={}).open(req,timeout=CONNECT_TIMEOUT) as response:
                        status=response.status
                        lengths=response.headers.get_all('Content-Length',[])
                        remaining=None
                        if not response.chunked and lengths:
                            if len(lengths)!=1 or not lengths[0].isascii() or not lengths[0].isdigit():
                                raise http.client.HTTPException('Ambiguous upstream response length')
                            remaining=int(lengths[0])
                        chunked=remaining is None
                        operation='client'
                        self.send_response(status)
                        self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                        if response.headers.get('Content-Encoding'):
                            self.send_header('Content-Encoding',response.headers['Content-Encoding'])
                        if chunked:self.send_header('Transfer-Encoding','chunked')
                        else:self.send_header('Content-Length',str(remaining))
                        self.send_header('Connection','close')
                        self.send_header('Cache-Control','no-store')
                        self.end_headers();started=True;self.close_connection=True
                        while True:
                            operation='upstream'
                            chunk=response.read1(65536)
                            if not chunk: break
                            if remaining is not None:remaining-=len(chunk)
                            operation='client'
                            if chunked:self.wfile.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n')
                            else:self.wfile.write(chunk)
                            self.wfile.flush()
                        if exchange.done.is_set():raise OSError('Request cancelled')
                        if remaining:raise http.client.IncompleteRead(b'',remaining)
                        operation='client'
                        if chunked:self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                        outcome='complete'  # HTTP transfer, not model success.
                except UserError as e:
                    status=422;failure='invalid_request'
                    if not started:self.error(status,str(e))
                except (ValueError,TypeError):
                    status=400;failure='invalid_request'
                    if not started:self.error(status,'请求 JSON 或参数无效。')
                except urllib.error.HTTPError as e:
                    status=e.code if e.code>=400 else 502
                    failure='upstream_http'
                    # No upstream error body is logged or echoed: it may contain secrets.
                    if not started:self.error(status,f'上游返回 HTTP {e.code}。请检查 Responses 兼容性、模型和权限。Harbor 转发层未重试；Codex 可能按自身规则重试。')
                    e.close()
                except (OSError,urllib.error.URLError,http.client.HTTPException) as error:
                    if operation=='upload':
                        timed_out=isinstance(error,TimeoutError)
                        status=408 if timed_out else 400
                        outcome='failed' if timed_out else 'cancelled'
                        failure='client_timeout' if timed_out else 'client_disconnected'
                        self.error(status,'本地请求上传超时。' if timed_out else '本地请求上传已中断。')
                        return
                    cancelled=exchange.done.is_set() or operation=='client'
                    outcome='cancelled' if cancelled else 'failed'
                    failure='client_cancelled' if cancelled else error_kind(error)
                    if not started and not cancelled:
                        status=502
                        self.error(status,'无法连接上游或连接已中断。Harbor 转发层未重试；Codex 可能按自身规则重试。')
                    self.close_connection=True
                finally:
                    exchange.cancel()
                    with gateway.lock:gateway.exchanges.discard(exchange)
                    try:
                        gateway.record({'time':time.time(),'profile':gateway.profile['id'],
                                        'alias':alias,'upstream_model':actual,'status':status,
                                        'outcome':outcome,'error_kind':failure})
                    except OSError:
                        gateway.log_failed=True
                    finally:
                        gateway.limit.release()

        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.url=f'http://127.0.0.1:{self.server.server_port}/v1'

    def start(self):
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
            exchanges=tuple(self.exchanges)
        for exchange in exchanges:exchange.cancel(downstream=True)
        if self.thread:self.server.shutdown()
        self.server.server_close()
