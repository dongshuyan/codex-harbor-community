"""Official Responses replay compatibility, without changing stored history.

The search declaration follows yetone/magpie (MIT), codex_request.go at
c4aeaec740b98a3f0f772f60dcea517ed144354b. See LICENSE-MAGPIE.
Only the outgoing request is adapted. Native model catalogs, error bodies,
credentials refresh and the on-disk transcript remain owned by Codex.
"""
from __future__ import annotations

import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import selectors
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from proxy import MAX_BODY, NoRedirect
from native_cli import app_server_config_scope

OFFICIAL_BASE = 'https://chatgpt.com/backend-api/codex'
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade'}


def _declares_search(tools):
    return isinstance(tools, list) and any(isinstance(item, dict)
        and isinstance(item.get('type'), str) and item['type'].startswith('web_search') for item in tools)


def declare_search(raw, *, lite=False):
    """Supply the missing hosted-history schema; otherwise preserve exact bytes."""
    if b'"web_search_call"' not in raw:
        return raw
    def invalid_constant(_):
        raise ValueError('Non-JSON number')
    try:
        body = json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        return raw
    if not isinstance(body, dict) or not isinstance(body.get('input'), list):
        return raw
    items = body['input']
    if not any(isinstance(item, dict) and item.get('type') == 'web_search_call' for item in items):
        return raw
    tools = body.get('tools')
    if tools is None: tools = []
    if not isinstance(tools, list) or _declares_search(tools):
        return raw
    additional = [item for item in items if isinstance(item, dict) and item.get('type') == 'additional_tools']
    for item in additional:
        declared = item.get('tools', [])
        if not isinstance(declared, list) or _declares_search(declared):
            return raw
    offered = bool(tools) or any(item.get('tools') for item in additional)
    if not offered and body.get('tool_choice') not in (None, 'auto', 'none'):
        # Leave an inconsistent forced/unknown choice to native validation;
        # adding the history schema must not turn it into a forced search.
        return raw
    reasoning = body.get('reasoning')
    lite = lite or bool(additional) or isinstance(reasoning, dict) and reasoning.get('context') == 'all_turns'
    cached = {'type': 'web_search', 'external_web_access': False}
    if additional:
        additional[0].setdefault('tools', []).append(cached)
    elif lite:
        items.insert(0, {'type': 'additional_tools', 'role': 'developer', 'tools': [cached]})
    else:
        body['tools'] = tools + [cached]
    if not offered:
        body['tool_choice'] = 'none'
    try:
        return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    except (ValueError, UnicodeError, RecursionError):
        return raw


def native_arguments(args, url):
    """Override only this child's transport; do not write its config.toml."""
    values = {'openai_base_url': json.dumps(url), 'features.enable_request_compression': 'false',
              'cli_auth_credentials_store': '"ephemeral"'}
    result = list(args)
    if '--' in result:
        raise ValueError('Unsupported stdio app-server argument terminator')
    for i, arg in enumerate(result):
        if arg == '--': break
        if arg in ('-c', '--config') and i + 1 < len(result):
            key = result[i + 1].split('=', 1)[0].strip()
            if key in values: result[i + 1] = key + '=' + values[key]
        elif arg.startswith('--config=') or arg.startswith('-c') and len(arg) > 2:
            prefix = '--config=' if arg.startswith('--config=') else '-c'
            setting = arg[len(prefix):].removeprefix('=')
            key = setting.split('=', 1)[0].strip()
            if key in values: result[i] = prefix + key + '=' + values[key]
    pinned = [part for key, value in values.items() for part in ('-c', key + '=' + value)]
    # Last config assignments also win over table-form overrides. Native feature
    # switches outrank -c, so use its own --disable for this transport requirement.
    return app_server_config_scope(['--disable', 'enable_request_compression', *result, *pinned])


def loopback_environment(environment):
    """Keep OAuth/local gateway credentials away from ambient HTTP proxies."""
    result = dict(environment)
    entries = [value.strip() for key in ('NO_PROXY', 'no_proxy')
               for value in result.get(key, '').split(',') if value.strip()]
    bypass = ','.join(dict.fromkeys([*entries, '127.0.0.1', 'localhost']))
    result.update(NO_PROXY=bypass, no_proxy=bypass)
    return result


def _headers(headers, *, request=False):
    excluded = HOP_HEADERS | {part.strip().lower() for value in headers.get_all('Connection', [])
                             for part in value.split(',')}
    if request: excluded |= {'host', 'content-length'}
    return [(key, value) for key, value in headers.items() if key.lower() not in excluded]


class _Exchange:
    """Own duplicate transport descriptors so cancellation interrupts blocking IO."""
    def __init__(self, downstream):
        self.downstream = downstream
        self.sockets = []
        self.lock = threading.Lock()
        self.done = threading.Event()

    def connected(self, sock):
        # SSL sockets cannot be dup()ed, but duplicating their underlying fd for
        # shutdown is safe; TLS bytes are read/written only by urllib itself.
        duplicate = socket.fromfd(sock.fileno(), sock.family, sock.type, sock.proto)
        with self.lock:
            if self.done.is_set():
                duplicate.close()
                raise OSError('Request cancelled')
            self.sockets.append(duplicate)
        sock.settimeout(600)  # Native long-response inactivity budget, not a turn deadline.

    def cancel(self, *, downstream=False):
        self.done.set()
        with self.lock:
            sockets, self.sockets = self.sockets, []
        for sock in sockets + ([self.downstream] if downstream else []):
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            if sock is not self.downstream: sock.close()

    def watch(self):
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.downstream, selectors.EVENT_READ)
                while not self.done.wait(.2):
                    if selector.select(0) and not self.downstream.recv(1, socket.MSG_PEEK):
                        self.cancel(); return
        except (OSError, ValueError):
            self.cancel()

    def opener(self):
        owner = self
        class Tracked:
            def connect(self):
                super().connect()
                owner.connected(self.sock)
        class HTTP(Tracked, http.client.HTTPConnection): pass
        class HTTPS(Tracked, http.client.HTTPSConnection): pass
        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req): return self.do_open(HTTP, req)
        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req): return self.do_open(HTTPS, req, context=self._context)
        # Match the user's existing proxy environment for the official HTTPS
        # destination; the native child's loopback hop separately bypasses it.
        return urllib.request.build_opener(urllib.request.ProxyHandler(), NoRedirect(), HTTPHandler(), HTTPSHandler())


class OfficialResponsesBridge:
    """A per-native-process, authenticated, fixed-destination HTTP boundary.

    upstream_base is a constructor seam for isolated tests, never an environment
    variable, CLI option or user profile field. Production always uses OFFICIAL_BASE.
    Authentication reuses the selected OAuth access token + account ID. They are
    supplied in memory by auth_bridge, never embedded in the URL, argv or logs.
    """
    def __init__(self, *, upstream_base=OFFICIAL_BASE):
        self.upstream_base = upstream_base.rstrip('/')
        self.lock = threading.Lock()
        self.credentials = []
        self.exchanges = set()
        self.closed = False
        self.revoked = False
        self.thread = None
        self.limit = threading.BoundedSemaphore(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def setup(self):
                super().setup(); self.connection.settimeout(30)
            def log_message(self, *args): pass
            def error(self, status, message):
                data = json.dumps({'error': {'type': 'harbor_official_bridge_error', 'message': message}}).encode()
                self.close_connection = True
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Connection', 'close')
                    self.end_headers(); self.wfile.write(data); self.wfile.flush()
                    # Closing with an unread upload can reset the connection and
                    # hide this error. Half-close, then discard a bounded tail.
                    self.connection.shutdown(socket.SHUT_WR)
                    remaining = getattr(self, '_body_remaining', None)
                    if remaining is None:
                        sizes = self.headers.get_all('Content-Length', [])
                        try:
                            remaining = int(sizes[0]) if len(sizes) == 1 else (MAX_BODY if self.command == 'POST' else 0)
                        except ValueError: remaining = MAX_BODY
                    remaining = min(MAX_BODY, max(0, remaining))
                    deadline = time.monotonic() + 2
                    while remaining and time.monotonic() < deadline:
                        self.connection.settimeout(max(.001, deadline - time.monotonic()))
                        chunk = self.rfile.read1(min(65536, remaining))
                        if not chunk: break
                        remaining -= len(chunk)
                except OSError:
                    pass  # The peer may already have closed; never retry.
            def do_GET(self): self.forward()
            def do_POST(self): self.forward()
            def forward(self):
                if not owner.authorized(self.headers):
                    return self.error(401, 'Harbor official connection is not authenticated.')
                parsed = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(parsed.path)
                prefix = '/backend-api/codex/'
                if (parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith(prefix)
                    or any(part in ('.', '..', '') for part in path[len(prefix):].split('/'))
                    or '%' in path or '\\' in path or any(ord(c) < 32 for c in path)):
                    return self.error(404, 'Unsupported official request path.')
                relative = path[len(prefix):]
                if self.command == 'GET' and relative == 'responses' and self.headers.get('Upgrade', '').lower() == 'websocket':
                    # Codex explicitly treats 426 as immediate HTTP fallback.
                    return self.error(426, 'Use the native HTTP Responses transport.')
                if not owner.limit.acquire(blocking=False):
                    return self.error(503, 'Harbor official connection is busy; retry shortly.')
                exchange = _Exchange(self.connection)
                started = False
                with owner.lock:
                    stopping = owner.closed or owner.revoked
                    if not stopping: owner.exchanges.add(exchange)
                forwarding = False
                try:
                    if stopping: return self.error(503, 'Harbor official connection is closing.')
                    if self.headers.get('Transfer-Encoding'):
                        return self.error(411, 'Content-Length is required.')
                    sizes = self.headers.get_all('Content-Length', [])
                    if len(sizes) > 1: return self.error(400, 'Ambiguous request length.')
                    size = int(sizes[0]) if sizes else 0
                    if size < 0 or size > MAX_BODY: return self.error(413, 'Harbor request limit is 64 MiB.')
                    if self.command == 'POST' and not sizes: return self.error(411, 'Content-Length is required.')
                    if self.command == 'GET' and size: return self.error(400, 'GET request body is unsupported.')
                    self._body_remaining = size
                    body = self.rfile.read(size) if size else None
                    self._body_remaining = size - len(body or b'')
                    if size and len(body) != size: return self.error(400, 'Incomplete request body.')
                    if self.command == 'POST' and relative in ('responses', 'responses/compact'):
                        if self.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                            return self.error(415, 'Native request compression must be disabled for history compatibility.')
                        body = declare_search(body or b'', lite=self.headers.get('x-openai-internal-codex-responses-lite', '').lower() == 'true')
                    upstream = owner.upstream_base + '/' + relative + ('?' + parsed.query if parsed.query else '')
                    request = urllib.request.Request(upstream, data=body, headers=dict(_headers(self.headers, request=True)), method=self.command)
                    threading.Thread(target=exchange.watch, daemon=True).start()
                    forwarding = True
                    try:
                        response = exchange.opener().open(request, timeout=30)
                    except urllib.error.HTTPError as error:
                        response = error  # Native refresh, quota and compaction need the original error.
                    with response:
                        if 300 <= response.status < 400:
                            return self.error(502, 'Official redirect was refused; credentials were not forwarded.')
                        self.send_response(response.status)
                        for key, value in _headers(response.headers): self.send_header(key, value)
                        self.send_header('Connection', 'close')
                        self.end_headers(); started = True; self.close_connection = True
                        while chunk := response.read1(65536):
                            self.wfile.write(chunk); self.wfile.flush()
                except (ValueError, UnicodeError, RecursionError):
                    if not started: self.error(400, 'Invalid official request framing.')
                except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
                    if not started:
                        if forwarding:
                            self.error(502, 'Official connection closed; Harbor did not retry the request.')
                        elif isinstance(error, TimeoutError):
                            self.error(408, 'Local request upload timed out; no official request was sent.')
                        else:
                            self.error(400, 'Local request upload closed; no official request was sent.')
                    self.close_connection = True
                finally:
                    exchange.cancel()
                    with owner.lock: owner.exchanges.discard(exchange)
                    owner.limit.release()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/backend-api/codex'

    def set_credentials(self, access_token, account_id):
        if not isinstance(access_token, str) or not access_token or not isinstance(account_id, str) or not account_id:
            raise ValueError('Missing official access credentials')
        pair = ('Bearer ' + access_token, account_id)
        with self.lock:
            if self.closed or self.revoked or self.credentials and self.credentials[0][1] != account_id:
                raise ValueError('Official bridge account cannot change')
            # The prior token may still be in flight; return its genuine upstream
            # 401 so Codex can request refreshed credentials through auth_bridge.
            self.credentials = [pair] + [entry for entry in self.credentials if entry != pair][:1]

    def authorized(self, headers):
        if any(len(headers.get_all(name, [])) != 1 for name in ('Authorization', 'ChatGPT-Account-Id')):
            return False
        auth, account = headers.get('Authorization', ''), headers.get('ChatGPT-Account-Id', '')
        with self.lock: credentials = tuple(self.credentials)
        return any(hmac.compare_digest(auth.encode(), token.encode()) and hmac.compare_digest(account.encode(), ident.encode())
                   for token, ident in credentials)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def revoke(self):
        """Detach this window only; persistent account credentials are untouched."""
        with self.lock:
            self.revoked = True
            self.credentials.clear()
            exchanges = tuple(self.exchanges)
        for exchange in exchanges: exchange.cancel(downstream=True)

    def close(self):
        self.revoke()
        with self.lock:
            if self.closed: return
            self.closed = True
        if self.thread: self.server.shutdown()
        self.server.server_close()
