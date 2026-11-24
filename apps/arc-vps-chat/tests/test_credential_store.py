import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('credential_store', Path(__file__).parents[1] / 'credential_store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CredentialTests(unittest.TestCase):
    def test_persistence_duplicates_and_counter_race(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'credentials.db'
            store = module.CredentialStore(path)
            store.add(b'id', b'public-key', 0)
            with self.assertRaises(sqlite3.IntegrityError):
                store.add(b'id', b'other-key', 0)
            self.assertEqual(module.CredentialStore(path).get(b'id'), (b'public-key', 0))
            self.assertTrue(store.update_counter(b'id', 0, 1))
            self.assertFalse(store.update_counter(b'id', 0, 2))
            with self.assertRaises(ValueError):
                store.update_counter(b'id', 1, 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
