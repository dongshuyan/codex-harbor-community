"""Cancellable standard-library HTTP exchanges for the Router gateway."""
import http.client
import selectors
import socket
import ssl
import threading
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def loopback_environment(environment):
    """Keep local gateway credentials away from ambient HTTP proxies."""
    result = dict(environment)
    entries = [value.strip() for key in ('NO_PROXY', 'no_proxy')
               for value in result.get(key, '').split(',') if value.strip()]
    bypass = ','.join(dict.fromkeys([*entries, '127.0.0.1', 'localhost']))
    result.update(NO_PROXY=bypass, no_proxy=bypass)
    return result


def error_kind(error):
    """Bounded diagnostic categories; never serialize exception text or URLs."""
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, TimeoutError):
        return 'timeout'
    if isinstance(error, http.client.IncompleteRead):
        return 'incomplete_response'
    if isinstance(error, ssl.SSLError):
        return 'tls'
    if isinstance(error, socket.gaierror):
        return 'dns'
    if isinstance(error, ConnectionError):
        return 'disconnected'
    if isinstance(error, http.client.HTTPException):
        return 'protocol'
    return 'connection_error'


class Exchange:
    """Own duplicate transport descriptors so cancellation interrupts blocking IO.

    Reused from the previous official bridge. The watcher interrupts established
    sockets. Registration happens after TCP/TLS setup, so cancellation during
    setup waits for it to return or fail. Each socket operation uses urllib's
    connect timeout; multiple addresses and OS DNS resolution can exceed that
    wall-clock duration. Each request gets its own opener and no automatic replay.
    """
    def __init__(self, downstream, *, read_timeout=600):
        self.downstream = downstream
        self.read_timeout = read_timeout
        self.sockets = []
        self.lock = threading.Lock()
        self.done = threading.Event()

    def connected(self, sock):
        duplicate = socket.fromfd(sock.fileno(), sock.family, sock.type, sock.proto)
        with self.lock:
            if self.done.is_set():
                try:
                    duplicate.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                duplicate.close()
                raise OSError('Request cancelled')
            self.sockets.append(duplicate)
        sock.settimeout(self.read_timeout)

    def cancel(self, *, downstream=False):
        self.done.set()
        with self.lock:
            sockets, self.sockets = self.sockets, []
        for sock in sockets + ([self.downstream] if downstream else []):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if sock is not self.downstream:
                sock.close()

    def watch(self):
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.downstream, selectors.EVENT_READ)
                while not self.done.wait(.2):
                    if selector.select(0) and not self.downstream.recv(1, socket.MSG_PEEK):
                        self.cancel()
                        return
        except (OSError, ValueError):
            self.cancel()

    def opener(self, *, proxies=None):
        owner = self
        class Tracked:
            def connect(self):
                super().connect()
                owner.connected(self.sock)
        class HTTP(Tracked, http.client.HTTPConnection):
            pass
        class HTTPS(Tracked, http.client.HTTPSConnection):
            pass
        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(HTTP, req)
        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(HTTPS, req, context=self._context)
        return urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect(),
                                           HTTPHandler(), HTTPSHandler())
