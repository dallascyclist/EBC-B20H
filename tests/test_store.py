import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ebctool.store import SampleRow, Store, StoreError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "db" / "t.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_creates_parent_dir_and_schema(self):
        self.assertTrue(self.path.exists())
        self.assertEqual(self.store.list_packs(), [])

    def test_pack_add_get_list_and_duplicate(self):
        p = self.store.add_pack("rad52", "li-ion", cells_series=14, nominal_v=52.0, rated_ah=17.5)
        self.assertEqual(self.store.get_pack("rad52").id, p.id)
        self.assertEqual([x.name for x in self.store.list_packs()], ["rad52"])
        with self.assertRaisesRegex(StoreError, "exists"):
            self.store.add_pack("rad52", "li-ion")
        with self.assertRaisesRegex(StoreError, "chemistry"):
            self.store.add_pack("x", "lead")
        with self.assertRaisesRegex(StoreError, "no pack"):
            self.store.get_pack("nope")

    def test_test_lifecycle_and_samples(self):
        p = self.store.add_pack("rad52", "li-ion")
        tid = self.store.begin_test(p.id, "2026-09-30T20:00:00Z", 5.0, 42.0, 600, "202609302000")
        self.store.append_sample(tid, SampleRow(tid, 2.0, 54.3, 5.0, 0.0028, 0.15, 0.003, "active"))
        self.store.append_sample(tid, SampleRow(tid, 4.0, 54.2, 5.0, 0.0056, 0.30, 0.006, "active"))
        self.store.finish_test(tid, "2026-09-30T20:00:04Z", "max_time", 4.0, 0.0056, 0.30, 0.006, 54.3, 54.2, "EBC-B20H", "3.08")
        t = self.store.get_test(tid)
        self.assertEqual((t.end_reason, t.device_model, t.firmware), ("max_time", "EBC-B20H", "3.08"))
        self.assertAlmostEqual(t.ah_host, 0.0056)
        self.assertEqual([s.t_s for s in self.store.samples(tid)], [2.0, 4.0])
        self.assertEqual([x.id for x in self.store.list_tests("rad52")], [tid])

    def test_list_tests_newest_first_and_filter(self):
        a = self.store.add_pack("a", "li-ion"); b = self.store.add_pack("b", "lifepo4")
        t1 = self.store.begin_test(a.id, "2026-09-01T00:00:00Z", 1, 40, 0, "r1")
        t2 = self.store.begin_test(a.id, "2026-09-02T00:00:00Z", 1, 40, 0, "r2")
        t3 = self.store.begin_test(b.id, "2026-09-03T00:00:00Z", 1, 40, 0, "r3")
        self.assertEqual([t.id for t in self.store.list_tests()], [t3, t2, t1])
        self.assertEqual([t.id for t in self.store.list_tests("a")], [t2, t1])

    def test_bad_end_reason_rejected(self):
        p = self.store.add_pack("a", "li-ion")
        tid = self.store.begin_test(p.id, "2026-09-01T00:00:00Z", 1, 40, 0, "r1")
        with self.assertRaisesRegex(StoreError, "end_reason"):
            self.store.finish_test(tid, "x", "exploded", 1, 0, 0, 0, 1, 1, "m", "f")

    def test_export_csv(self):
        p = self.store.add_pack("a", "li-ion")
        tid = self.store.begin_test(p.id, "2026-09-01T00:00:00Z", 1, 40, 0, "r1")
        self.store.append_sample(tid, SampleRow(tid, 2.0, 54.3, 1.0, 0.0006, 0.03, 0.001, "active"))
        out = Path(self.tmp.name) / "t.csv"
        self.assertEqual(self.store.export_csv(tid, out), 1)
        lines = out.read_text().splitlines()
        self.assertEqual(lines[0], "t_s,volts,amps,ah_host,wh_host,ah_device,state")
        self.assertTrue(lines[1].startswith("2.0,54.3,1.0,"))

    def test_missing_test_raises(self):
        with self.assertRaisesRegex(StoreError, "^no test 99$"):
            self.store.get_test(99)
        out = Path(self.tmp.name) / "none.csv"
        with self.assertRaisesRegex(StoreError, "^no test 99$"):
            self.store.export_csv(99, out)
        self.assertFalse(out.exists())

    def test_schema_version_created_at_and_reopen(self):
        p = self.store.add_pack("a", "li-ion")
        self.assertRegex(p.created_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.store.close()
        with Store(self.path) as again:
            self.assertEqual([x.name for x in again.list_packs()], ["a"])
        with closing(sqlite3.connect(str(self.path))) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
