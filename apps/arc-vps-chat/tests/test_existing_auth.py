"""Regression checks for the existing password gate during passkey rollout."""
import base64
import hashlib
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("arc_vps_chat", Path(__file__).parents[1] / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class PasswordGateTests(unittest.TestCase):
    def test_password_hash_accepts_only_correct_password(self):
        salt = b"test-only-salt"
        expected = hashlib.scrypt(b"correct horse", salt=salt, n=16384, r=8, p=1, dklen=32)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        old = server.CHAT_PASSWORD_HASH
        try:
            server.CHAT_PASSWORD_HASH = f"scrypt$16384$8$1${encode(salt)}${encode(expected)}"
            self.assertTrue(server._scrypt_matches(b"correct horse"))
            self.assertFalse(server._scrypt_matches(b"wrong horse"))
            server.CHAT_PASSWORD_HASH = "malformed"
            self.assertFalse(server._scrypt_matches(b"correct horse"))
        finally:
            server.CHAT_PASSWORD_HASH = old

    def test_basic_parser_rejects_malformed_credentials(self):
        encoded = base64.b64encode(b"arko:password:with:colons").decode()
        self.assertEqual(server._basic_user("Basic " + encoded), ("arko", b"password:with:colons"))
        for value in ("", "Bearer token", "Basic !!!", "Basic YWJj"):
            with self.subTest(value=value):
                self.assertIsNone(server._basic_user(value))


if __name__ == "__main__":
    unittest.main()
