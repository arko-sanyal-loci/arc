import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import offline_ledger

class OfflineLedgerTest(unittest.TestCase):
    def test_work_and_liability_append_and_latest_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(offline_ledger, "DB_PATH", Path(tmp) / "offline.sqlite"):
            work = offline_ledger.record_work(actor="Arc", event_type="inference", title="Test", metadata={"model":"mock"})
            self.assertEqual(len(offline_ledger.recent_work()), 1)
            self.assertTrue(offline_ledger.cache_status()["offline"])
            offline_ledger.record_liability(liability_ref="claim-1", creditor="A", debtor="B", description="Claim", state="proposed", source_note="synthetic test")
            offline_ledger.record_liability(liability_ref="claim-1", creditor="A", debtor="B", description="Claim", state="disputed", source_note="status update")
            self.assertEqual(offline_ledger.current_liabilities()["items"][0]["state"], "disputed")
            with self.assertRaisesRegex(Exception, "append-only"):
                import sqlite3
                with sqlite3.connect(offline_ledger.DB_PATH) as conn:
                    conn.execute("DELETE FROM work_events")

    def test_liability_requires_valid_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(offline_ledger, "DB_PATH", Path(tmp) / "offline.sqlite"):
            with self.assertRaises(ValueError):
                offline_ledger.record_liability(liability_ref="x", creditor="", debtor="", description="x", state="invented")

if __name__ == "__main__":
    unittest.main()
