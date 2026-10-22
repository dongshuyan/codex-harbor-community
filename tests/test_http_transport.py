"""Socket ownership and bounded diagnostics; no network services or credentials."""
import http.client
from pathlib import Path
import socket
import ssl
import sys
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from http_transport import Exchange, error_kind


class HttpTransportTests(unittest.TestCase):
    def test_errors_are_classified_without_private_exception_text(self):
        cases = [(TimeoutError('PRIVATE'), 'timeout'),
                 (urllib.error.URLError(TimeoutError('PRIVATE')), 'timeout'),
                 (http.client.IncompleteRead(b'PRIVATE', 7), 'incomplete_response'),
                 (http.client.RemoteDisconnected('PRIVATE'), 'disconnected'),
                 (http.client.BadStatusLine('PRIVATE'), 'protocol'),
                 (ssl.SSLError('PRIVATE'), 'tls'),
                 (socket.gaierror('PRIVATE'), 'dns'),
                 (urllib.error.URLError('https://PRIVATE.invalid'), 'connection_error')]
        for error, expected in cases:
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(error_kind(error), expected)

    def test_cancel_before_connection_registration_interrupts_late_socket(self):
        downstream, peer = socket.socketpair()
        upstream, remote = socket.socketpair()
        try:
            exchange = Exchange(downstream, read_timeout=120)
            exchange.cancel()
            with self.assertRaises(OSError):
                exchange.connected(upstream)
            remote.settimeout(1)
            self.assertEqual(remote.recv(1), b'')
            self.assertEqual(exchange.sockets, [])
        finally:
            for sock in (downstream, peer, upstream, remote):
                sock.close()

    def test_registered_socket_timeout_and_idempotent_cleanup(self):
        downstream, peer = socket.socketpair()
        upstream, remote = socket.socketpair()
        try:
            exchange = Exchange(downstream, read_timeout=120)
            exchange.connected(upstream)
            self.assertEqual(upstream.gettimeout(), 120)
            exchange.cancel(downstream=True)
            exchange.cancel(downstream=True)
            for sock in (peer, remote):
                sock.settimeout(1)
                self.assertEqual(sock.recv(1), b'')
            self.assertEqual(exchange.sockets, [])
        finally:
            for sock in (downstream, peer, upstream, remote):
                sock.close()


if __name__ == '__main__':
    unittest.main()
