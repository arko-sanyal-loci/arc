"""One-use WebAuthn challenges; callers must still verify the ceremony."""
import os
import secrets
import sqlite3
import time


class ChallengeStore:
    def __init__(self, path):
        self.path = path
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS challenges (id TEXT PRIMARY KEY, purpose TEXT NOT NULL, challenge BLOB NOT NULL, expires REAL NOT NULL)")

    def issue(self, purpose, ttl=120):
        if purpose not in ("register", "authenticate") or not 1 <= ttl <= 300:
            raise ValueError("invalid challenge purpose or lifetime")
        identifier, challenge = secrets.token_urlsafe(32), secrets.token_bytes(32)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM challenges WHERE expires <= ?", (time.time(),))
            db.execute("INSERT INTO challenges VALUES (?, ?, ?, ?)", (identifier, purpose, challenge, time.time() + ttl))
        return identifier, challenge

    def consume(self, identifier, purpose):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT purpose, challenge, expires FROM challenges WHERE id = ?", (identifier,)).fetchone()
            db.execute("DELETE FROM challenges WHERE id = ?", (identifier,))
        if row is None or row[0] != purpose or row[2] <= time.time():
            return None
        return row[1]
