import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from ebctool import run as R
from ebctool.store import SampleRow, Store
from tests.support import close_log


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(close_log)
        self.cfg = Path(self.tmp.name) / "config.ini"
        self.cfg.write_text("[run]\nlog_dir = %s/logs\ndb_path = %s/db/t.db\n" % (self.tmp.name, self.tmp.name))

    def main(self, *args):
        out = io.StringIO()
        code = R.main(["--config", str(self.cfg), *args], stdout=out, stdin=io.StringIO(""))
        return code, out.getvalue()

    def test_version(self):
        code, out = self.main("--version")
        self.assertEqual(code, 0)
        self.assertIn("0.1.0", out)

    def test_bad_argument_exits_3(self):
        code, _ = self.main("discharge", "--pack", "x")   # missing --current/--cutoff
        self.assertEqual(code, 3)

    def test_missing_config_file_exits_3(self):
        out = io.StringIO()
        code = R.main(["--config", str(Path(self.tmp.name) / "nope.ini"), "pack", "list"], stdout=out)
        self.assertEqual(code, 3)

    def test_pack_add_list_show(self):
        code, _ = self.main("pack", "add", "rad52", "--chemistry", "li-ion", "--cells", "14", "--rated-ah", "17.5", "--nominal-v", "52")
        self.assertEqual(code, 0)
        code, out = self.main("pack", "list")
        self.assertEqual(code, 0); self.assertIn("rad52", out); self.assertIn("17.5", out)
        code, out = self.main("pack", "show", "rad52")
        self.assertEqual(code, 0); self.assertIn("cells", out.lower())
        code, _ = self.main("pack", "add", "rad52", "--chemistry", "li-ion")
        self.assertEqual(code, 3)

    def test_tests_and_export_on_empty_db(self):
        code, out = self.main("tests")
        self.assertEqual(code, 0)
        code, _ = self.main("export", "--test", "99")
        self.assertEqual(code, 3)

    def test_log_file_is_written(self):
        self.main("pack", "list")
        logs = list((Path(self.tmp.name) / "logs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("ebctool 0.1.0 starting", logs[0].read_text())


class RulingTests(unittest.TestCase):
    """Controller rulings R7, R8, R9, R22, R23 and the --config/--db defaults."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(close_log)
        self.root = Path(self.tmp.name)
        self.logs = self.root / "logs"
        self.db = self.root / "db" / "t.db"
        self.cfg = self.root / "config.ini"
        self.cfg.write_text("[run]\nlog_dir = %s\ndb_path = %s\n" % (self.logs, self.db))

    def main(self, *args, config=True):
        out, err = io.StringIO(), io.StringIO()
        argv = (["--config", str(self.cfg)] if config else []) + list(args)
        with contextlib.redirect_stderr(err):
            code = R.main(argv, stdout=out, stdin=io.StringIO(""))
        return code, out.getvalue(), err.getvalue()

    def log_text(self):
        return "".join(p.read_text() for p in sorted(self.logs.glob("*.log")))

    def seed_test(self, samples=2):
        """One finished test with `samples` samples on pack rad52, written straight to the store."""
        with Store(self.db) as st:
            p = st.add_pack("rad52", "li-ion", cells_series=14, rated_ah=17.5)
            tid = st.begin_test(p.id, "2026-09-30T20:00:00Z", 5.0, 42.0, 600, "202609302000")
            for k in range(1, samples + 1):
                st.append_sample(tid, SampleRow(tid, 2.0 * k, 54.3, 5.0, 0.003 * k, 0.15 * k, 0.003 * k, "active"))
            st.finish_test(tid, "2026-09-30T21:02:03Z", "cutoff", 3723.0, 5.123, 250.0, 5.1, 54.3, 42.0,
                           "EBC-B20H", "3.08")
        return tid

    # R7

    def test_version_prints_exact_line_and_opens_no_log(self):
        code, out, err = self.main("--version")
        self.assertEqual((code, out, err), (0, "ebctool 0.1.0\n", ""))
        self.assertFalse(self.logs.exists())

    def test_no_subcommand_exits_3_with_error(self):
        code, out, err = self.main()
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: "), out)
        self.assertEqual(err, "")
        self.assertFalse(self.logs.exists())

    # R8

    def test_store_error_goes_to_stdout_prefixed_and_is_logged(self):
        code, out, err = self.main("pack", "show", "nope")
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: no pack 'nope'\n")
        self.assertEqual(err, "")
        self.assertIn("no pack 'nope'", self.log_text())

    def test_duplicate_pack_error_text(self):
        self.main("pack", "add", "a", "--chemistry", "other")
        code, out, _ = self.main("pack", "add", "a", "--chemistry", "other")
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: pack 'a' already exists\n")

    def test_tests_unknown_pack_exits_3(self):
        code, out, _ = self.main("tests", "--pack", "nope")
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: no pack 'nope'\n")

    def test_export_unknown_test_error_text(self):
        code, out, _ = self.main("export", "--test", "99")
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: no test 99\n")
        self.assertEqual(list(self.db.parent.glob("*.csv")), [])

    def test_unexpected_exception_exits_1_and_logs_traceback(self):
        def broken_store(path):
            raise RuntimeError("disk on fire")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = R.main(["--config", str(self.cfg), "pack", "list"], stdout=out, store_factory=broken_store)
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().startswith("error: "), out.getvalue())
        self.assertIn("disk on fire", out.getvalue())
        self.assertIn("Traceback", self.log_text())

    def test_keyboard_interrupt_exits_2(self):
        def interrupted_store(path):
            raise KeyboardInterrupt
        code = R.main(["--config", str(self.cfg), "pack", "list"], stdout=io.StringIO(),
                      store_factory=interrupted_store)
        self.assertEqual(code, 2)

    # R9

    def test_parse_error_prints_error_on_stdout_without_log(self):
        code, out, err = self.main("discharge", "--pack", "x")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: "), out)
        self.assertIn("--current", out)
        self.assertEqual(err, "")
        self.assertFalse(self.logs.exists())

    def test_bad_chemistry_is_a_parse_error(self):
        code, out, _ = self.main("pack", "add", "a", "--chemistry", "lead")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: "), out)
        self.assertIn("lead", out)

    def test_export_test_id_must_be_an_integer(self):
        code, out, _ = self.main("export", "--test", "abc")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: "), out)
        self.assertFalse(self.logs.exists())

    def test_pack_needs_an_action(self):
        code, out, _ = self.main("pack")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: "), out)

    # R22

    def test_missing_config_prints_error_without_log(self):
        code, out, err = self.main("--config", str(self.root / "nope.ini"), "pack", "list", config=False)
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: config file not found"), out)
        self.assertEqual(err, "")

    def test_bad_config_value_exits_3_without_log(self):
        self.cfg.write_text("[run]\nlog_dir = %s\n[serial]\nparity = N\n" % self.logs)
        code, out, err = self.main("pack", "list")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: [serial] parity"), out)
        self.assertEqual(err, "")
        self.assertFalse(self.logs.exists())

    # --config default and --db override

    def test_default_config_is_config_ini_else_built_in_defaults(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        code, _, _ = self.main("pack", "add", "a", "--chemistry", "other", config=False)
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "db" / "ebctool.db").is_file())   # built-in default db_path
        self.assertTrue(any((self.root / "logs").glob("*.log")))       # built-in default log_dir
        (self.root / "config").mkdir()
        (self.root / "config" / "config.ini").write_text("[run]\nlog_dir = logs2\ndb_path = other/x.db\n")
        code, out, _ = self.main("pack", "list", config=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)   # header only: a fresh database, not db/ebctool.db
        self.assertTrue((self.root / "other" / "x.db").is_file())
        self.assertTrue(any((self.root / "logs2").glob("*.log")))

    def test_db_overrides_config_db_path(self):
        other = self.root / "elsewhere" / "o.db"
        code, _, _ = self.main("--db", str(other), "pack", "add", "only-here", "--chemistry", "other")
        self.assertEqual(code, 0)
        self.assertTrue(other.is_file())
        self.assertFalse(self.db.exists())
        _, out, _ = self.main("--db", str(other), "pack", "list")
        self.assertIn("only-here", out)
        _, out, _ = self.main("pack", "list")
        self.assertNotIn("only-here", out)

    # listings

    def test_tests_prints_header_then_one_line_per_test(self):
        _, out, _ = self.main("tests")
        self.assertEqual(out.count("\n"), 1)
        self.seed_test()
        code, out, _ = self.main("tests")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        for text in ("rad52", "cutoff", "1:02:03", "5.123", "2026-09-30T20:00:00Z"):
            self.assertIn(text, lines[1])
        code, out, _ = self.main("tests", "--pack", "rad52")
        self.assertEqual((code, len(out.splitlines())), (0, 2))

    def test_pack_show_lists_its_tests(self):
        self.seed_test()
        code, out, _ = self.main("pack", "show", "rad52")
        self.assertEqual(code, 0)
        self.assertIn("li-ion", out)
        self.assertIn("17.50", out)
        self.assertIn("1:02:03", out)

    # R23

    def test_export_default_path_is_beside_db(self):
        tid = self.seed_test(samples=2)
        code, out, _ = self.main("export", "--test", str(tid))
        self.assertEqual(code, 0)
        path = self.db.parent / ("test-%d.csv" % tid)
        self.assertTrue(path.is_file())
        self.assertEqual(len(path.read_text().splitlines()), 3)   # header + 2 samples
        self.assertIn("2 rows", out)
        self.assertIn(str(path), out)

    def test_export_default_path_follows_db_override(self):
        other = self.root / "elsewhere" / "o.db"
        with Store(other) as st:
            p = st.add_pack("x", "other")
            tid = st.begin_test(p.id, "2026-09-30T20:00:00Z", 1.0, 42.0, 0, "r")
        code, out, _ = self.main("--db", str(other), "export", "--test", str(tid))
        self.assertEqual(code, 0)
        self.assertTrue((other.parent / ("test-%d.csv" % tid)).is_file())
        self.assertIn("0 rows", out)

    # R24: --config, --db and --port before or after the subcommand; after wins

    def test_db_after_subcommand_works(self):
        other = self.root / "elsewhere" / "o.db"
        code, _, _ = self.main("pack", "add", "only-here", "--chemistry", "other", "--db", str(other))
        self.assertEqual(code, 0)
        self.assertTrue(other.is_file())
        self.assertFalse(self.db.exists())
        code, out, _ = self.main("pack", "list", "--db", str(other))
        self.assertEqual(code, 0)
        self.assertIn("only-here", out)

    def test_config_before_and_after_subcommand(self):
        code, _, _ = self.main("--config", str(self.cfg), "pack", "list", config=False)
        self.assertEqual(code, 0)
        code, _, _ = self.main("pack", "list", "--config", str(self.cfg), config=False)
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.logs.glob("*.log"))), 2)

    def test_port_after_subcommand_parses(self):
        args = R.parse_args(["discharge", "--pack", "p", "--current", "1", "--cutoff", "50", "--port", "/dev/fake"])
        self.assertEqual(args.port, "/dev/fake")

    def test_value_after_subcommand_wins(self):
        args = R.parse_args(["--db", "before.db", "pack", "list", "--db", "after.db"])
        self.assertEqual(args.db, "after.db")
        args = R.parse_args(["--db", "a.db", "pack", "--db", "b.db", "list", "--db", "c.db"])
        self.assertEqual(args.db, "c.db")
        args = R.parse_args(["--db", "before.db", "--port", "/dev/x", "tests"])
        self.assertEqual((args.db, args.port), ("before.db", "/dev/x"))

    def test_globals_default_to_none_when_absent(self):
        args = R.parse_args(["pack", "list"])
        self.assertEqual((args.config, args.db, args.port), (None, None, None))

    def test_version_is_top_level_only(self):
        code, out, _ = self.main("pack", "list", "--version")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: unrecognized arguments"), out)

    def test_export_csv_creates_parent_directory(self):
        tid = self.seed_test(samples=3)
        path = self.root / "out" / "nested" / "x.csv"
        code, out, _ = self.main("export", "--test", str(tid), "--csv", str(path))
        self.assertEqual(code, 0)
        self.assertTrue(path.is_file())
        self.assertIn("3 rows", out)
        self.assertIn(str(path), out)


if __name__ == "__main__":
    unittest.main()
