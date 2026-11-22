"""One-use recovery codes. Callers enforce authorization and rate limits."""
import hashlib
import os
import secrets
import sqlite3


class RecoveryStore:
    def __init__(self, path):
        self.path = path
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS recovery_codes (digest BLOB PRIMARY KEY)')

    @staticmethod
    def _digest(code):
        if not isinstance(code, str) or len(code) != 32:
            return None
        try:
            value = bytes.fromhex(code)
        except ValueError:
            return None
        if len(value) != 16:
            return None
        return hashlib.sha256(b'arc-recovery-v1:' + value).digest()

    def replace(self, count=10):
        if not 1 <= count <= 20:
            raise ValueError('invalid recovery code count')
        codes = [secrets.token_hex(16) for _ in range(count)]
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM recovery_codes')
            db.executemany('INSERT INTO recovery_codes VALUES (?)', [(self._digest(code),) for code in codes])
        return codes

    def consume(self, code):
        digest = self._digest(code)
        if digest is None:
            return False
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            result = db.execute('DELETE FROM recovery_codes WHERE digest = ?', (digest,))
            return result.rowcount == 1
