import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('challenge_store', Path(__file__).parents[1] / 'challenge_store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ChallengeTests(unittest.TestCase):
    def test_database_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auth.db'
            path.touch(mode=0o666)
            path.chmod(0o666)
            module.ChallengeStore(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            link = Path(directory) / 'link.db'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                module.ChallengeStore(link)

    def test_one_use_and_purpose_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = module.ChallengeStore(Path(directory) / 'auth.db')
            identifier, challenge = store.issue('authenticate')
            self.assertEqual(store.consume(identifier, 'authenticate'), challenge)
            self.assertIsNone(store.consume(identifier, 'authenticate'))
            identifier, _ = store.issue('register')
            self.assertIsNone(store.consume(identifier, 'authenticate'))
            self.assertIsNone(store.consume(identifier, 'register'))

    def test_invalid_lifetimes_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = module.ChallengeStore(Path(directory) / 'auth.db')
            for ttl in (0, 301):
                with self.assertRaises(ValueError):
                    store.issue('register', ttl)

    def test_expired_challenge_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = module.ChallengeStore(Path(directory) / 'auth.db')
            identifier, _ = store.issue('register')
            with module.sqlite3.connect(store.path) as db:
                db.execute('UPDATE challenges SET expires = 0 WHERE id = ?', (identifier,))
            self.assertIsNone(store.consume(identifier, 'register'))
