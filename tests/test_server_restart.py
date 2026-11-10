import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler

from web.server import Server


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *args):
        pass


class RestartTests(unittest.TestCase):
    def test_live_listener_stays_exclusive(self):
        with Server(('127.0.0.1', 0), Handler) as server:
            with self.assertRaises(OSError):
                other = Server(server.server_address, Handler)
                other.server_close()

    @unittest.skipIf(os.name == 'nt', 'Linux/Unix TIME_WAIT reuse regression')
    def test_restart_after_completed_http_request(self):
        address = ('127.0.0.1', 0)
        for _ in range(3):
            with Server(address, Handler) as server:
                address = server.server_address
                worker = threading.Thread(target=server.handle_request)
                worker.start()
                with urllib.request.urlopen('http://127.0.0.1:%d/' % address[1], timeout=5) as response:
                    self.assertEqual(response.read(), b'ok')
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
