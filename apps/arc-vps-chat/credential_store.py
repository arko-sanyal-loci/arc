"""Private credential storage for the single Arc account."""
import os
import sqlite3


class CredentialStore:
    def __init__(self, path):
        self.path = path
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS credentials (id BLOB PRIMARY KEY, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL CHECK(sign_count >= 0))')

    def add(self, credential_id, public_key, sign_count):
        if not credential_id or not public_key or sign_count < 0:
            raise ValueError('invalid credential')
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO credentials VALUES (?, ?, ?)', (credential_id, public_key, sign_count))

    def get(self, credential_id):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT public_key, sign_count FROM credentials WHERE id = ?', (credential_id,)).fetchone()

    def ids(self):
        with sqlite3.connect(self.path) as db:
            return [row[0] for row in db.execute('SELECT id FROM credentials')]

    def update_counter(self, credential_id, expected_count, new_count):
        if new_count < 0 or (expected_count and new_count <= expected_count):
            raise ValueError('counter did not advance')
        with sqlite3.connect(self.path) as db:
            cursor = db.execute('UPDATE credentials SET sign_count = ? WHERE id = ? AND sign_count = ?',
                                (new_count, credential_id, expected_count))
            return cursor.rowcount == 1
