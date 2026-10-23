import tempfile
import unittest
from datetime import date
from pathlib import Path

from primavera_sync import analytics, db, importer

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "saft-exemplo.xml"


class SaftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "t.db"
        self.counts = importer.load(SAMPLE, path)
        self.conn = db.connect(path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_cancelled_invoice_skipped(self):
        self.assertEqual(self.counts["invoices"], 3)

    def test_summary(self):
        s = analytics.summary(self.conn)
        self.assertAlmostEqual(s["invoiced_gross"], 2214.00)
        self.assertAlmostEqual(s["received"], 1530.00)
        self.assertAlmostEqual(s["open_total"], 684.00)

    def test_open_invoices(self):
        rows = analytics.open_invoices(self.conn, today=date(2026, 10, 1))
        self.assertEqual([r["number"] for r in rows], ["FT 2026/2", "FT 2026/3"])
        self.assertAlmostEqual(rows[0]["open_amount"], 315.00)

    def test_reimport_is_idempotent(self):
        importer.load(SAMPLE, Path(self.tmp.name) / "t.db")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
