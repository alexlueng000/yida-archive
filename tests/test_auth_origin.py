import unittest
from types import SimpleNamespace

from web.auth import same_origin


class AuthOriginTest(unittest.TestCase):
    def test_https_behind_proxy_and_local_http(self):
        for scheme in ('http', 'https'):
            self.assertTrue(same_origin(SimpleNamespace(headers={
                'Host': 'archive.example.com', 'Origin': scheme + '://archive.example.com'})))
        self.assertTrue(same_origin(SimpleNamespace(headers={
            'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765'})))

    def test_foreign_origins_and_ports_are_rejected(self):
        for origin in ('https://evil.example', 'https://archive.example.com.evil.example',
                       'https://archive.example.com:9999', 'null'):
            self.assertFalse(same_origin(SimpleNamespace(headers={
                'Host': 'archive.example.com', 'Origin': origin})))

    def test_forwarded_headers_do_not_override_host(self):
        self.assertFalse(same_origin(SimpleNamespace(headers={
            'Host': 'archive.example.com', 'Origin': 'https://evil.example',
            'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Proto': 'https'})))
