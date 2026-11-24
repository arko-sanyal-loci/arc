import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('recovery_store', Path(__file__).parents[1] / 'recovery_store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RecoveryTests(unittest.TestCase):
    def test_one_use_replacement_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recovery.db'
            store = module.RecoveryStore(path)
            first, second = store.replace(2)
            self.assertNotIn(first.encode(), path.read_bytes())
            self.assertTrue(store.consume(first))
            self.assertFalse(store.consume(first))
            self.assertFalse(store.consume('garbage'))
            store.replace(2)
            self.assertFalse(store.consume(second))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
