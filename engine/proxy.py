"""Authenticated loopback Responses gateway; no protocol conversion or retries."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from core import UserError,map_request

MAX_BODY=64*1024*1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class Gateway:
    def __init__(self,profile,key,record=lambda _:None):
        if profile['protocol']!='responses': raise UserError('此版本仅支持 OpenAI Responses。')
        self.profile=profile
        self.key=key
        self.record=record
        self.log_failed=False
        self.token=secrets.token_urlsafe(40)
        self.limit=threading.BoundedSemaphore(8)
        # Do not inherit ambient HTTP proxies, cookies, auth or redirect handlers.
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        gateway=self

        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.settimeout(150)

            def log_message(self,*args): pass

            def error(self,status,message):
                data=json.dumps({'error':{'message':message,'type':'smart2_gateway_error'}}).encode()
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Connection','close')
                self.end_headers();self.wfile.write(data);self.close_connection=True

            def authorized(self):
                value=self.headers.get('Authorization','')
                return hmac.compare_digest(value,'Bearer '+gateway.token)

            def do_GET(self):
                if not self.authorized(): return self.error(401,'Harbor 本地请求身份不匹配。')
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
                try:
                    if self.headers.get('Transfer-Encoding'):
                        return self.error(400,'请求必须带 Content-Length。')
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<=MAX_BODY: return self.error(413,'请求大小无效或超过 64 MiB。')
                    if self.headers.get('Content-Encoding','identity')!='identity':
                        return self.error(415,'请关闭请求压缩。')
                    raw=self.rfile.read(size)
                    if len(raw)!=size: return self.error(400,'请求不完整。')
                    request=json.loads(raw)
                    if not isinstance(request,dict): raise UserError('请求必须是 JSON 对象。')
                    alias=request.get('model')
                    body=map_request(gateway.profile,request)
                    actual=body['model']
                    upstream=gateway.profile['url']+self.path.removeprefix('/v1')
                    req=urllib.request.Request(upstream,data=json.dumps(body).encode(),headers={
                        'Authorization':'Bearer '+gateway.key,'Content-Type':'application/json',
                        'Accept':'text/event-stream' if body.get('stream') else 'application/json',
                        'Accept-Encoding':'identity','User-Agent':'Codex-Harbor/0.4'})
                    with gateway.opener.open(req,timeout=120) as response:
                        status=response.status
                        self.send_response(status)
                        self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                        self.send_header('Connection','close')
                        self.send_header('Cache-Control','no-store')
                        self.end_headers();started=True;self.close_connection=True
                        while True:
                            chunk=response.read1(65536)
                            if not chunk: break
                            self.wfile.write(chunk);self.wfile.flush()
                except UserError as e:
                    status=422
                    if not started:self.error(status,str(e))
                except (ValueError,TypeError):
                    status=400
                    if not started:self.error(status,'请求 JSON 或参数无效。')
                except urllib.error.HTTPError as e:
                    status=e.code if e.code>=400 else 502
                    # No upstream error body is logged or echoed: it may contain secrets.
                    if not started:self.error(status,f'上游返回 HTTP {e.code}。请检查 Responses 兼容性、模型和权限。Harbor 转发层未重试；Codex 可能按自身规则重试。')
                    e.close()
                except (OSError,urllib.error.URLError):
                    if not started:
                        try:self.error(502,'无法连接上游或连接已中断。Harbor 转发层未重试；Codex 可能按自身规则重试。')
                        except OSError:pass
                    self.close_connection=True
                finally:
                    try:
                        gateway.record({'time':time.time(),'profile':gateway.profile['id'],
                                        'alias':alias,'upstream_model':actual,'status':status})
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
        self.server.shutdown();self.server.server_close()
