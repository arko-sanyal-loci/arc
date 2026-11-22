import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('challenge_store', Path(__file__).parents[1] / 'challenge_store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ChallengeTests(unittest.TestCase):
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
