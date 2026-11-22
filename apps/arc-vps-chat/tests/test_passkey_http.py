"""Check that passkey endpoints reject unauthorized or cross-origin requests."""
import base64
import hashlib
import http.client
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

root = Path(__file__).parents[1]
sys.path.insert(0, str(root))
os.environ['ARC_CHAT_PASSKEYS_ENABLED'] = '1'
spec = importlib.util.spec_from_file_location('arc_http_test', root / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class PasskeyHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(cls.directory.name)
        server._passkeys = server._credentials = None
        salt = b'http-test-salt'
        hashed = hashlib.scrypt(b'correct', salt=salt, n=16384, r=8, p=1, dklen=32)
        enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
        server.CHAT_PASSWORD_HASH = f'scrypt$16384$8$1${enc(salt)}${enc(hashed)}'
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def post(self, path, origin, auth=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        headers = {'Content-Type': 'application/json', 'Origin': origin}
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(auth).decode()
        connection.request('POST', path, '{}', headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_enrollment_requires_password_and_correct_origin(self):
        path = '/arko/chat/api/passkeys/register/options'
        self.assertEqual(self.post(path, 'https://billo.tech')[0], 401)
        self.assertEqual(self.post(path, 'https://evil.example', b'arko:correct')[0], 403)
        self.assertEqual(self.post(path, 'https://billo.tech', b'arko:wrong')[0], 401)
        self.assertEqual(self.post(path, 'https://billo.tech', b'arko:correct')[0], 200)

    def test_login_rejects_cross_origin_and_no_credentials(self):
        path = '/arko/chat/api/passkeys/login/options'
        self.assertEqual(self.post(path, 'https://evil.example')[0], 403)
        self.assertEqual(self.post(path, 'https://billo.tech')[0], 400)

    def test_disabled_feature_returns_not_found(self):
        os.environ['ARC_CHAT_PASSKEYS_ENABLED'] = '0'
        try:
            self.assertEqual(self.post('/arko/chat/api/passkeys/login/options', 'https://billo.tech')[0], 404)
        finally:
            os.environ['ARC_CHAT_PASSKEYS_ENABLED'] = '1'
