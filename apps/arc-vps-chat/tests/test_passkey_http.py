"""Check that passkey endpoints reject unauthorized or cross-origin requests."""
import base64
import hashlib
import http.client
import json
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from test_passkey_ceremony import b64, client
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

    def test_signed_enrollment_login_and_session(self):
        def request(path, payload, auth=None, cookie=None):
            conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
            headers = {'Content-Type': 'application/json', 'Origin': 'https://billo.tech'}
            if auth:
                headers['Authorization'] = 'Basic ' + base64.b64encode(auth).decode()
            if cookie:
                headers['Cookie'] = cookie
            conn.request('POST', path, json.dumps(payload), headers)
            response = conn.getresponse()
            status, body, set_cookie = response.status, response.read(), response.getheader('Set-Cookie')
            conn.close()
            return status, json.loads(body), set_cookie

        prefix = '/arko/chat/api/passkeys/'
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
        credential_id = b'http-test-key'
        rp_hash = hashlib.sha256(b'billo.tech').digest()
        status, data, _ = request(prefix+'register/options', {}, b'arko:correct')
        self.assertEqual(status, 200)
        client_data = client('webauthn.create', data['options']['challenge'])
        auth_data = rp_hash + bytes([0x45]) + struct.pack('>I', 0) + bytes(16) + struct.pack('>H', len(credential_id)) + credential_id + cose
        registration = {'id': b64(credential_id), 'rawId': b64(credential_id), 'type': 'public-key', 'response': {'clientDataJSON': b64(client_data), 'attestationObject': b64(cbor2.dumps({'fmt': 'none', 'authData': auth_data, 'attStmt': {}}))}}
        status, _, _ = request(prefix+'register/verify', {'id': data['id'], 'credential': registration}, b'arko:correct')
        self.assertEqual(status, 201)
        status, data, _ = request(prefix+'login/options', {})
        self.assertEqual(status, 200)
        client_data = client('webauthn.get', data['options']['challenge'])
        auth_data = rp_hash + bytes([0x05]) + struct.pack('>I', 1)
        signature = key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        assertion = {'id': b64(credential_id), 'rawId': b64(credential_id), 'type': 'public-key', 'response': {'clientDataJSON': b64(client_data), 'authenticatorData': b64(auth_data), 'signature': b64(signature)}}
        status, _, cookie = request(prefix+'login/verify', {'id': data['id'], 'credential': assertion})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        connection.request('GET', '/arko/chat', headers={'Cookie': cookie.split(';')[0]})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()
        status, _, _ = request(prefix+'login/verify', {'id': data['id'], 'credential': assertion})
        self.assertEqual(status, 400)

    def test_recovery_code_is_single_use_and_cannot_create_codes(self):
        path = '/arko/chat/api/recovery/create'
        status, body = self.post(path, 'https://billo.tech', b'arko:correct')
        self.assertEqual(status, 200)
        code = json.loads(body)['codes'][0]
        def use():
            c = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
            c.request('POST', '/arko/chat/api/recovery/use', json.dumps({'code': code}), {'Content-Type':'application/json', 'Origin':'https://billo.tech'})
            r = c.getresponse()
            status, cookie = r.status, r.getheader('Set-Cookie')
            r.read(); c.close()
            return status, cookie
        status, cookie = use()
        self.assertEqual(status, 200)
        self.assertEqual(use()[0], 400)
        c = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        c.request('POST', path, '{}', {'Content-Type':'application/json', 'Origin':'https://billo.tech', 'Cookie':cookie.split(';')[0]})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        r.read(); c.close()

    def test_disabled_feature_returns_not_found(self):
        os.environ['ARC_CHAT_PASSKEYS_ENABLED'] = '0'
        try:
            self.assertEqual(self.post('/arko/chat/api/passkeys/login/options', 'https://billo.tech')[0], 404)
        finally:
            os.environ['ARC_CHAT_PASSKEYS_ENABLED'] = '1'
