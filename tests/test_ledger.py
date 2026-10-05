import json
import tempfile
import unittest
from pathlib import Path

from swarm.ledger import Ledger


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "logs" / "swarm.jsonl"
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_increments_seq_and_persists(self):
        e1 = self.ledger.append("dispense", {"x": 1}, actor="vending")
        e2 = self.ledger.append("complete", {"x": 2}, actor="worker")
        self.assertEqual(e1.seq, 1)
        self.assertEqual(e2.seq, 2)
        self.assertTrue(self.path.exists())
        lines = self.path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_seq_recovers_across_reopen(self):
        self.ledger.append("a")
        self.ledger.append("b")
        reopened = Ledger(self.path)
        self.assertEqual(reopened.last_seq, 2)
        self.assertEqual(reopened.append("c").seq, 3)

    def test_query_filters(self):
        self.ledger.append("dispense", actor="vending")
        self.ledger.append("health", actor="supervisor")
        self.ledger.append("dispense", actor="vending")
        self.assertEqual(len(self.ledger.query(type="dispense")), 2)
        self.assertEqual(len(self.ledger.query(actor="supervisor")), 1)
        self.assertEqual(len(self.ledger.query(type="dispense", since_seq=1)), 1)

    def test_torn_line_is_skipped(self):
        self.ledger.append("good")
        with self.path.open("a") as fh:
            fh.write('{"seq": 99, "ts": "x", "type": "broken"')  # no closing brace
        events = list(self.ledger.read_all())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "good")

    def test_rotation_archives_old_file(self):
        ledger = Ledger(self.path, max_bytes=10)
        ledger.append("one", {"pad": "x" * 200})
        ledger.append("two", {"pad": "y" * 200})
        archives = list(self.path.parent.glob("swarm.jsonl.*.bak"))
        self.assertTrue(archives)

    def test_types_histogram(self):
        self.ledger.append("a")
        self.ledger.append("a")
        self.ledger.append("b")
        self.assertEqual(self.ledger.types(), {"a": 2, "b": 1})


if __name__ == "__main__":
    unittest.main()
