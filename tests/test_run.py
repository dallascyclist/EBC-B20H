import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ebctool import protocol as P
from ebctool import run as R
from ebctool.console import ConsoleSink, hms
from ebctool.device import DeviceError
from ebctool.session import DischargePlan, Sample, SessionResult
from ebctool.store import SampleRow, Store
from tests.fakes import ClosingPipe, FakeClock, FakeDevice, make_device_factory
from tests.support import close_log, hexframe, make_log

EBC_PY = Path(__file__).resolve().parent.parent / "python" / "scripts" / "ebc.py"


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

    def test_non_positive_silence_timeout_exits_3(self):
        self.cfg.write_text(self.cfg.read_text() + "[serial]\nsilence_timeout_s = 0\n")
        code, out = self.main("pack", "list")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: [serial] silence_timeout_s"), out)

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

    def test_unopenable_database_exits_3(self):
        afile = self.root / "afile"
        afile.write_text("not a directory")
        code, out, err = self.main("--db", str(afile / "x.db"), "pack", "list")
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("error: cannot open database"), out)
        self.assertEqual(err, "")

    def test_keyboard_interrupt_exits_2(self):
        def interrupted_store(path):
            raise KeyboardInterrupt
        code = R.main(["--config", str(self.cfg), "pack", "list"], stdout=io.StringIO(),
                      store_factory=interrupted_store)
        self.assertEqual(code, 2)

    # R38: the error printer never raises on a closed stdout; the exit code is unchanged

    def closed_stdout(self):
        out = ClosingPipe()
        out.broken = True   # the reader is already gone: every write raises BrokenPipeError
        return out

    def test_error_printer_survives_closed_stdout(self):
        cases = ((["pack", "show", "nope"], 3),                 # StoreError after the log is open
                 (["discharge", "--pack", "x"], 3),             # parse error, before any log
                 (["--config", str(self.root / "nope.ini"), "pack", "list"], 3))
        for args, want in cases:
            with self.subTest(args=args):
                out, err = self.closed_stdout(), io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = R.main(["--config", str(self.cfg), *args], stdout=out, stdin=io.StringIO(""))
                self.assertEqual(code, want)
                self.assertEqual(out.failures, 1)
                self.assertEqual(err.getvalue(), "")

    def test_unexpected_error_printer_survives_closed_stdout(self):
        def broken_store(path):
            raise RuntimeError("disk on fire")
        out, err = self.closed_stdout(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = R.main(["--config", str(self.cfg), "pack", "list"], stdout=out, store_factory=broken_store)
        self.assertEqual(code, 1)
        self.assertEqual(out.failures, 1)
        self.assertIn("disk on fire", self.log_text())

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


IDLE_HEX = "fa 00 00 00 9f 42 00 53 00 00 00 64 03 b4 00 00 1c 41 f8"
ACTIVE_HEX = "fa 0a 00 64 9f 29 00 01 00 00 00 64 14 c8 00 03 1c 7e f8"
FIN_CUTOFF_HEX = "fa 14 00 64 9d 5b 00 32 00 00 00 64 14 c8 00 03 1c 23 f8"   # finished at 50.03 V
NO_ANSWER = "no answer from tester: leave the settings screen, stop any panel-started test, and check the port"


class DeviceCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(close_log)
        self.cfg = Path(self.tmp.name) / "config.ini"
        self.cfg.write_text("[serial]\nport = /dev/fake\n[run]\nlog_dir = %s/logs\ndb_path = %s/db/t.db\n" % (self.tmp.name, self.tmp.name))

    def main(self, script, *args, stdin="", device_class=FakeDevice):
        out = io.StringIO()
        factory = make_device_factory([(t, P.parse_status(hexframe(h))) for t, h in script], device_class)
        code = R.main(["--config", str(self.cfg), *args], stdout=out, stdin=io.StringIO(stdin), device_factory=factory, clock=factory.clock)
        return code, out.getvalue(), factory.last

    def add_pack(self):
        R.main(["--config", str(self.cfg), "pack", "add", "p", "--chemistry", "li-ion", "--cells", "14"], stdout=io.StringIO())

    def test_discover_prints_decoded_frames_and_disconnects(self):
        code, out, dev = self.main([(0.5, IDLE_HEX)], "discover", "--seconds", "2")
        self.assertEqual(code, 0)
        self.assertIn("54.58", out); self.assertIn("EBC-B20H", out)
        self.assertEqual(dev.sent[-1], P.disconnect())

    def test_discover_no_answer_exits_1_with_hint(self):
        code, out, _ = self.main([], "discover", "--seconds", "1")
        self.assertEqual(code, 1)
        self.assertIn("settings screen", out)

    def test_discharge_happy_path_exit_0_and_rows(self):
        self.add_pack()
        script = [(0.5, IDLE_HEX), (6.5, ACTIVE_HEX), (8.5, ACTIVE_HEX), (10.5, FIN_CUTOFF_HEX)]
        code, out, dev = self.main(script, "discharge", "--pack", "p", "--current", "1", "--cutoff", "50", "--max-minutes", "0", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("cutoff", out)
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
        code, out = self._plain("tests")
        self.assertIn("cutoff", out)
        self.assertEqual(out.count("\n"), 2)   # header + one row

    def _plain(self, *args):
        out = io.StringIO()
        code = R.main(["--config", str(self.cfg), *args], stdout=out, stdin=io.StringIO(""))
        return code, out.getvalue()

    def test_discharge_prompt_declined_exits_2_without_row(self):
        self.add_pack()
        code, _, dev = self.main([(0.5, IDLE_HEX)], "discharge", "--pack", "p", "--current", "1", "--cutoff", "50", stdin="n\n")
        self.assertEqual(code, 2)
        self.assertNotIn(P.start_cc(1.0, 50.0, 0), dev.sent)
        self.assertEqual(dev.sent[-1], P.disconnect())
        _, out = self._plain("tests")
        self.assertEqual(out.count("\n"), 1)   # header only

    def test_ctrl_c_during_preflight_exits_2_without_test_row(self):
        self.add_pack()
        class Boom(io.StringIO):
            def readline(self):
                raise KeyboardInterrupt
        out = io.StringIO()
        factory = make_device_factory([(0.5, P.parse_status(hexframe(IDLE_HEX)))])
        code = R.main(["--config", str(self.cfg), "discharge", "--pack", "p", "--current", "1", "--cutoff", "50"], stdout=out, stdin=Boom(), device_factory=factory, clock=factory.clock)
        self.assertEqual(code, 2)
        self.assertEqual(factory.last.sent[-1], P.disconnect())
        _, listing = self._plain("tests")
        self.assertEqual(listing.count("\n"), 1)  # header only

    def test_discharge_current_out_of_range_exits_3(self):
        self.add_pack()
        code, out, _ = self.main([], "discharge", "--pack", "p", "--current", "25", "--cutoff", "50", "--yes")
        self.assertEqual(code, 3); self.assertIn("current", out.lower())

    def test_discharge_cutoff_above_pack_exits_3(self):
        self.add_pack()
        code, out, dev = self.main([(0.5, IDLE_HEX)], "discharge", "--pack", "p", "--current", "1", "--cutoff", "54.3", "--yes")
        self.assertEqual(code, 3); self.assertIn("headroom", out)
        self.assertEqual(dev.sent[-1], P.disconnect())

    def test_low_cutoff_warning_needs_yes(self):
        self.add_pack()   # 14 cells -> warn below 35.0 V
        code, out, _ = self.main([(0.5, IDLE_HEX)], "discharge", "--pack", "p", "--current", "1", "--cutoff", "30", stdin="n\n")
        self.assertEqual(code, 2); self.assertIn("below", out)


class CtrlCWhenDrained(FakeDevice):
    """Ctrl-C arrives while waiting for the frame after the last scripted one."""

    def read(self, timeout):
        if not self._script:
            raise KeyboardInterrupt
        return super().read(timeout)


class CtrlCAfterAMinute(FakeDevice):
    """Ctrl-C once a minute has passed on the fake clock, so a command that waits silently forever
    ends (as exit 2) instead of hanging the test."""

    def read(self, timeout):
        if self.clock.now() >= 60.0:
            raise KeyboardInterrupt
        return super().read(timeout)


class CtrlCOnFirstRead(FakeDevice):
    def read(self, timeout):
        raise KeyboardInterrupt


class SigintAfterThirdFrame(FakeDevice):
    """Delivers a real SIGINT to this process right after returning the third frame, recording which
    handler was installed at that moment and whether the signal raised."""

    def __init__(self, script, clock):
        super().__init__(script, clock)
        self.count = 0
        self.handler = None
        self.raised = None

    def read(self, timeout):
        status = super().read(timeout)
        if status is not None:
            self.count += 1
            if self.count == 3:
                self.handler = signal.getsignal(signal.SIGINT)
                try:
                    signal.raise_signal(signal.SIGINT)
                    self.raised = False
                except KeyboardInterrupt:
                    self.raised = True
        return status


class SigintAndTeeDies(SigintAfterThirdFrame):
    """SigintAfterThirdFrame under `bin/ebc.sh discharge ... | tee`: the same Ctrl-C kills the tee,
    so stdout's pipe closes as the signal arrives."""

    def __init__(self, script, clock, pipe):
        super().__init__(script, clock)
        self.pipe = pipe

    def read(self, timeout):
        status = super().read(timeout)
        if self.handler is not None:
            self.pipe.close_pipe()
        return status


class DeviceCommandRulingTests(unittest.TestCase):
    """Rulings R5, R11, R20/R26, R27 and the exit codes, beyond the brief's tests."""

    main = DeviceCommandTests.main
    add_pack = DeviceCommandTests.add_pack
    _plain = DeviceCommandTests._plain

    def setUp(self):
        DeviceCommandTests.setUp(self)
        self.db = Path(self.tmp.name) / "db" / "t.db"
        self.logs = Path(self.tmp.name) / "logs"

    def discharge(self, script, *extra, stdin="", device_class=FakeDevice):
        return self.main(script, "discharge", "--pack", "p", *extra, stdin=stdin, device_class=device_class)

    def rows(self):
        with Store(self.db) as st:
            return [(t, st.samples(t.id)) for t in st.list_tests()]

    def assert_not_started(self, dev):
        self.assertFalse([f for f in dev.sent if f[1] == P.CMD_START_CC], dev.sent)
        self.assertEqual(dev.sent[-1], P.disconnect())
        self.assertTrue(dev.closed)
        self.assertEqual(self.rows(), [])

    # discover and monitor

    def test_discover_elapsed_time_comes_from_the_injected_clock(self):
        code, out, dev = self.main([(0.5, IDLE_HEX), (1.5, IDLE_HEX)], "discover", "--seconds", "2")
        self.assertEqual(code, 0)
        self.assertIn("0.5 s", out)
        self.assertIn("1.5 s", out)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])
        self.assertTrue(dev.closed)
        self.assertEqual(dev.clock.now(), 2.0)   # listened exactly --seconds on the fake clock

    def test_discover_raw_adds_hex(self):
        code, out, _ = self.main([(0.5, IDLE_HEX)], "discover", "--seconds", "1", "--raw")
        self.assertEqual(code, 0)
        self.assertIn(IDLE_HEX, out)
        self.assertIn("54.58", out)

    def test_discover_no_answer_message_is_exact(self):
        code, out, dev = self.main([], "discover", "--seconds", "1")
        self.assertEqual(code, 1)
        self.assertEqual(out, "error: %s\n" % NO_ANSWER)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])

    def test_discover_seconds_must_be_positive(self):
        for value in ("0", "-1", "nan"):
            with self.subTest(value=value):
                code, out, dev = self.main([(0.5, IDLE_HEX)], "discover", "--seconds", value)
                self.assertEqual(code, 3)
                self.assertTrue(out.startswith("error: --seconds"), out)
                self.assertIsNone(dev)

    def test_monitor_ctrl_c_disconnects_and_exits_2(self):
        script = [(0.5, IDLE_HEX), (2.5, IDLE_HEX), (4.5, IDLE_HEX)]
        code, out, dev = self.main(script, "monitor", device_class=CtrlCWhenDrained)
        self.assertEqual(code, 2)
        self.assertEqual(out.count("54.58"), 3)
        self.assertIn("4.5 s", out)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])
        self.assertTrue(dev.closed)

    # R32: monitor gives up when nothing at all answers Connect

    def test_monitor_no_frame_within_silence_timeout_exits_3(self):
        code, out, dev = self.main([], "monitor", device_class=CtrlCAfterAMinute)
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: %s\n" % NO_ANSWER)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])
        self.assertTrue(dev.closed)
        self.assertEqual(dev.clock.now(), 10.0)   # the default [serial] silence_timeout_s, on the fake clock

    def test_monitor_no_answer_follows_configured_silence_timeout(self):
        self.cfg.write_text(self.cfg.read_text().replace("port = /dev/fake\n", "port = /dev/fake\nsilence_timeout_s = 3\n"))
        code, out, dev = self.main([(5.0, IDLE_HEX)], "monitor", device_class=CtrlCAfterAMinute)
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: %s\n" % NO_ANSWER)
        self.assertEqual(dev.clock.now(), 3.0)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])

    def test_monitor_keeps_running_through_silence_once_frames_arrived(self):
        script = [(0.5, IDLE_HEX), (30.0, IDLE_HEX)]   # 29.5 s gap, well past silence_timeout_s
        code, out, dev = self.main(script, "monitor", device_class=CtrlCWhenDrained)
        self.assertEqual(code, 2)
        self.assertEqual(out.count("54.58"), 2)
        self.assertIn("30.0 s", out)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])

    def test_ports_lists_ports_and_the_chosen_one(self):
        with mock.patch.object(R, "list_ports", return_value=[("/dev/cu.a", "USB Serial"), ("/dev/cu.b", "other")]):
            code, out = self._plain("ports")
            self.assertEqual(code, 0)
            self.assertIn("/dev/cu.a", out); self.assertIn("USB Serial", out); self.assertIn("/dev/cu.b", out)
            self.assertIn("/dev/fake", out)   # [serial] port in the config
            with mock.patch.object(R, "resolve_port", return_value="/dev/cu.a") as resolve:
                code, out = self._plain("ports", "--port", "auto")
            self.assertEqual(code, 0)
            resolve.assert_called_once_with("auto")
            self.assertIn("auto", out.splitlines()[-1]); self.assertIn("/dev/cu.a", out.splitlines()[-1])
            with mock.patch.object(R, "resolve_port", side_effect=DeviceError("no serial port found; x")):
                code, out = self._plain("ports", "--port", "auto")
            self.assertEqual(code, 0)
            self.assertIn("no serial port found", out)

    # R20/R26: opening the port

    def test_port_that_cannot_open_exits_3(self):
        self.add_pack()
        missing = Path(self.tmp.name) / "no-such-port"
        self.cfg.write_text(self.cfg.read_text().replace("/dev/fake", str(missing)))
        for args in (["discover", "--seconds", "1"], ["monitor"],
                     ["discharge", "--pack", "p", "--current", "1", "--cutoff", "50", "--yes"]):
            with self.subTest(args=args):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = R.main(["--config", str(self.cfg), *args], stdout=out, stdin=io.StringIO(""),
                                  clock=FakeClock())
                self.assertEqual(code, 3)
                self.assertTrue(out.getvalue().startswith("error: cannot open %s: " % missing), out.getvalue())
                self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.rows(), [])

    def test_device_error_from_factory_exits_3(self):
        def no_port(config, port):
            raise DeviceError("no serial port found; is the tester on a port directly on the Mac?")
        out = io.StringIO()
        code = R.main(["--config", str(self.cfg), "monitor"], stdout=out, device_factory=no_port, clock=FakeClock())
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "error: no serial port found; is the tester on a port directly on the Mac?\n")

    # R27: range checks, before the device is opened

    def test_range_checks_exit_3_naming_value_and_range(self):
        self.add_pack()
        cases = [(["--current", "0.4", "--cutoff", "50"], "--current 0.4 A", "0.5..20 A"),
                 (["--current", "25", "--cutoff", "50"], "--current 25 A", "0.5..20 A"),
                 (["--current", "nan", "--cutoff", "50"], "--current nan A", "0.5..20 A"),
                 (["--current", "1", "--cutoff", "7.99"], "--cutoff 7.99 V", "8..88 V"),
                 (["--current", "1", "--cutoff", "88.01"], "--cutoff 88.01 V", "8..88 V"),
                 (["--current", "1", "--cutoff", "nan"], "--cutoff nan V", "8..88 V"),
                 (["--current", "1", "--cutoff", "50", "--max-minutes", "-1"], "--max-minutes -1", "0..999"),
                 (["--current", "1", "--cutoff", "50", "--max-minutes", "1000"], "--max-minutes 1000", "0..999")]
        for extra, value, allowed in cases:
            with self.subTest(extra=extra):
                code, out, dev = self.discharge([(0.5, IDLE_HEX)], *extra, "--yes")
                self.assertEqual(code, 3)
                self.assertTrue(out.startswith("error: %s " % value), out)
                self.assertIn(allowed, out)
                self.assertIsNone(dev)
        self.assertEqual(self.rows(), [])

    def test_range_bounds_are_inclusive(self):
        self.add_pack()
        code, _, dev = self.discharge([(0.5, IDLE_HEX)], "--current", "20", "--cutoff", "50", "--max-minutes", "999",
                                      stdin="n\n")
        self.assertEqual(code, 2)   # passed the checks, opened the device, declined at the prompt
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])

    def test_range_follows_config_limits(self):
        self.add_pack()
        self.cfg.write_text(self.cfg.read_text() + "[limits]\nmax_current_a = 5\n")
        code, out, dev = self.discharge([(0.5, IDLE_HEX)], "--current", "6", "--cutoff", "50", "--yes")
        self.assertEqual(code, 3)
        self.assertIn("0.5..5 A", out)
        self.assertIsNone(dev)

    def test_missing_pack_exits_3_before_device(self):
        code, out, dev = self.discharge([(0.5, IDLE_HEX)], "--current", "1", "--cutoff", "50", "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: no pack 'p'\n")
        self.assertIsNone(dev)

    # low-cutoff warning

    def test_low_cutoff_declined_never_opens_the_device(self):
        self.add_pack()
        code, out, dev = self.discharge([(0.5, IDLE_HEX)], "--current", "1", "--cutoff", "30", stdin="n\n")
        self.assertEqual(code, 2)
        self.assertIn("30.00 V is below 14 cells x 2.5 V = 35.00 V", out)
        self.assertIsNone(dev)

    def test_low_cutoff_typed_y_goes_on_to_the_start_prompt(self):
        self.add_pack()
        code, out, dev = self.discharge([(0.5, IDLE_HEX)], "--current", "1", "--cutoff", "30", stdin="y\nn\n")
        self.assertEqual(code, 2)
        self.assertIn("below", out)
        self.assertIn("Start discharge? [y/N] ", out)
        self.assert_not_started(dev)

    def test_low_cutoff_with_yes_warns_and_starts(self):
        self.add_pack()
        script = [(0.5, IDLE_HEX), (6.5, ACTIVE_HEX), (8.5, FIN_CUTOFF_HEX)]
        code, out, dev = self.discharge(script, "--current", "1", "--cutoff", "30", "--yes")
        self.assertIn("below", out)
        self.assertIn(P.start_cc(1.0, 30.0, 0), dev.sent)
        self.assertEqual(code, 1)   # finished far above the 30 V cutoff with no time limit: device_error
        self.assertEqual(self.rows()[0][0].end_reason, "device_error")

    # preflight refusals (exit 3, nothing started, no test row)

    def test_no_answer_through_discharge(self):
        self.add_pack()
        code, out, dev = self.discharge([], "--current", "1", "--cutoff", "50", "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[-1], "error: %s" % NO_ANSWER)
        self.assert_not_started(dev)

    def test_not_idle_through_discharge(self):
        self.add_pack()
        code, out, dev = self.discharge([(0.5, ACTIVE_HEX)], "--current", "1", "--cutoff", "50", "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[-1], "error: tester is busy (state active); stop it from the panel first")
        self.assert_not_started(dev)

    def test_voltage_below_cutoff_message_is_exact(self):
        self.add_pack()
        code, out, dev = self.discharge([(0.5, IDLE_HEX)], "--current", "1", "--cutoff", "54.3", "--yes")
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[-1], "error: pack reads 54.58 V, cutoff 54.30 V needs at least 0.5 V headroom")
        self.assert_not_started(dev)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])   # preflight's own Disconnect, no second one

    # Ctrl-C and the prompt (R5, R11)

    def test_ctrl_c_during_connect_exits_2_with_one_disconnect(self):
        self.add_pack()
        code, out, dev = self.discharge([], "--current", "1", "--cutoff", "50", "--yes", device_class=CtrlCOnFirstRead)
        self.assertEqual(code, 2)
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])
        self.assert_not_started(dev)

    def test_ctrl_c_at_prompt_abandons(self):
        self.add_pack()
        class Boom(io.StringIO):
            def readline(self):
                raise KeyboardInterrupt
        out = io.StringIO()
        factory = make_device_factory([(0.5, P.parse_status(hexframe(IDLE_HEX)))])
        code = R.main(["--config", str(self.cfg), "discharge", "--pack", "p", "--current", "1", "--cutoff", "50"],
                      stdout=out, stdin=Boom(), device_factory=factory, clock=factory.clock)
        self.assertEqual(code, 2)
        self.assertEqual(factory.last.sent, [P.connect(), P.disconnect()])
        self.assert_not_started(factory.last)

    def test_prompt_shows_reading_and_plan_and_declines_on_anything_but_y(self):
        self.add_pack()
        for answer in ("n\n", "\n", "", "yes\n", "q\n"):
            with self.subTest(answer=answer):
                code, out, dev = self.discharge([(0.5, IDLE_HEX)], "--current", "1", "--cutoff", "50", stdin=answer)
                self.assertEqual(code, 2)
                self.assertIn("54.58 V", out)
                self.assertIn("1.00 A", out)
                self.assertIn("50.00 V", out)
                self.assertIn("Start discharge? [y/N] ", out)
                self.assertEqual(dev.sent, [P.connect(), P.disconnect()])
        self.assertEqual(self.rows(), [])

    # runs that started

    def test_happy_path_rows_and_console(self):
        self.add_pack()
        script = [(0.5, IDLE_HEX), (6.5, ACTIVE_HEX), (8.5, ACTIVE_HEX), (10.5, FIN_CUTOFF_HEX)]
        code, out, dev = self.discharge(script, "--current", "1", "--cutoff", "50", "--notes", "first", stdin="Y\n")
        self.assertEqual(code, 0)
        self.assertEqual(dev.sent, [P.connect(), P.start_cc(1.0, 50.0, 0), P.stop(), P.disconnect()])
        self.assertTrue(dev.closed)
        [(test, samples)] = self.rows()
        self.assertEqual(test.end_reason, "cutoff")
        self.assertRegex(test.started_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(test.ended_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual((test.set_current_a, test.cutoff_v, test.max_minutes, test.notes), (1.0, 50.0, 0, "first"))
        self.assertEqual((test.device_model, test.firmware), ("EBC-B20H", None))
        self.assertAlmostEqual(test.start_v, 54.58); self.assertAlmostEqual(test.end_v, 50.03)
        self.assertAlmostEqual(test.runtime_s, 4.0)
        self.assertTrue((self.logs / (test.run_id + ".log")).is_file())
        self.assertEqual([s.t_s for s in samples], [0.0, 2.0])
        self.assertIn("\r  t=0:00:02  54.33 V  1.00 A", out)
        self.assertIn("saved as test %d" % test.id, out)
        self.assertNotIn("\x1b", out)

    def test_never_active_run_stores_and_shows_no_device_ah(self):
        self.add_pack()
        script = [(0.5, IDLE_HEX)] + [(2.5 + 2 * k, IDLE_HEX) for k in range(7)]   # Start never takes
        code, out, dev = self.discharge(script, "--current", "1", "--cutoff", "50", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
        self.assertIn("Ah device   -\n", out)
        [(test, samples)] = self.rows()
        self.assertEqual((test.end_reason, test.ah_device_final, samples), ("device_error", None, []))
        code, out = self._plain("history", "--pack", "p")
        self.assertEqual(code, 0, out)
        header, row = out.splitlines()
        self.assertIn("Ah device", header)
        self.assertEqual(row.split()[6], "-")   # columns: id date current cutoff Ah %rated Ah-device ...
        html = Path(self.tmp.name) / "out" / "p.html"
        code, out = self._plain("history", "--pack", "p", "--html", str(html))
        self.assertEqual(code, 0, out)
        self.assertTrue(html.is_file())
        for args in (("tests",), ("pack", "show", "p")):
            with self.subTest(args=args):
                code, out = self._plain(*args)
                self.assertEqual(code, 0, out)
                self.assertIn("device_error", out)

    def test_ctrl_c_during_run_is_user_stop_with_finished_row(self):
        self.add_pack()
        before = signal.getsignal(signal.SIGINT)
        script = [(0.5, IDLE_HEX)] + [(6.5 + 2 * k, ACTIVE_HEX) for k in range(10)]
        code, out, dev = self.discharge(script, "--current", "1", "--cutoff", "50", "--yes",
                                        device_class=SigintAfterThirdFrame)
        self.assertEqual(code, 2)
        self.assertFalse(dev.raised)                  # the flag handler took the signal, not KeyboardInterrupt
        self.assertIsNot(dev.handler, before)
        self.assertIs(signal.getsignal(signal.SIGINT), before)   # restored afterwards
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
        self.assertEqual(dev.sent.count(P.disconnect()), 1)
        [(test, samples)] = self.rows()
        self.assertEqual(test.end_reason, "user_stop")
        self.assertIsNotNone(test.ended_at)
        self.assertEqual(len(samples), 2)
        self.assertIn("user_stop", out)

    def test_ctrl_c_under_tee_is_user_stop_exit_2_despite_closed_stdout(self):
        """R38, the bench finding: Ctrl-C on `discharge ... | tee` also kills the tee. The pipe
        closes as the signal arrives, or earlier, just after the pre-Start lines."""
        self.add_pack()
        script = [(0.5, P.parse_status(hexframe(IDLE_HEX)))]
        script += [(6.5 + 2 * k, P.parse_status(hexframe(ACTIVE_HEX))) for k in range(10)]
        before = signal.getsignal(signal.SIGINT)
        for name, out in (("at Ctrl-C", ClosingPipe()), ("after the plan line", ClosingPipe(after_text="plan: "))):
            with self.subTest(pipe=name):
                factory = make_device_factory(script, lambda script, clock: SigintAndTeeDies(script, clock, out))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = R.main(["--config", str(self.cfg), "discharge", "--pack", "p", "--current", "1",
                                   "--cutoff", "50", "--yes"], stdout=out, stdin=io.StringIO(""),
                                  device_factory=factory, clock=factory.clock)
                dev = factory.last
                self.assertEqual(code, 2)
                self.assertFalse(dev.raised)   # the flag handler took the signal
                self.assertIs(signal.getsignal(signal.SIGINT), before)
                self.assertEqual(dev.sent.count(P.start_cc(1.0, 50.0, 0)), 1)
                self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
                self.assertEqual(dev.sent.count(P.disconnect()), 1)
                self.assertTrue(dev.closed)
                self.assertTrue(out.broken)
                self.assertIn("plan: CC 1.00 A", out.getvalue())
                self.assertEqual(err.getvalue(), "")   # nothing at WARNING or above
                test, samples = self.rows()[0]         # newest first
                self.assertEqual(test.end_reason, "user_stop")
                self.assertIsNotNone(test.ended_at)
                self.assertEqual(len(samples), 2)
                log = (self.logs / (test.run_id + ".log")).read_text(encoding="utf-8")
                self.assertNotIn(" ERROR ", log)
                self.assertIn("console output stopped", log)
                self.assertIn("exit 2", log)
        self.assertEqual(len(self.rows()), 2)

    def test_comm_lost_exits_1_and_keeps_partial_samples(self):
        self.add_pack()
        script = [(0.5, IDLE_HEX), (6.5, ACTIVE_HEX), (8.5, ACTIVE_HEX)]   # then silence
        code, out, dev = self.discharge(script, "--current", "1", "--cutoff", "50", stdin="y\n")
        self.assertEqual(code, 1)
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
        [(test, samples)] = self.rows()
        self.assertEqual(test.end_reason, "comm_lost")
        self.assertEqual([s.t_s for s in samples], [0.0, 2.0])
        self.assertIn("comm_lost", out)
        self.assertIn("panel", out)   # tells the user to check the tester itself

    def test_signal_handler_untouched_when_not_started(self):
        self.add_pack()
        before = signal.getsignal(signal.SIGINT)
        self.discharge([(0.5, IDLE_HEX)], "--current", "1", "--cutoff", "50", stdin="n\n")
        self.assertIs(signal.getsignal(signal.SIGINT), before)


class ConsoleSinkTests(unittest.TestCase):
    def test_live_line_rewrites_one_80_column_line(self):
        out = io.StringIO()
        sink = ConsoleSink(out, FakeClock())
        sink.on_sample(Sample(154.0, 54.21, 1.0, 0.043, 2.34, 0.04, "active"))
        self.assertEqual(out.getvalue(), "\r" + "  t=0:02:34  54.21 V  1.00 A  0.043 Ah  2.34 Wh  active".ljust(80))

    def test_end_prints_newline_then_six_line_summary(self):
        out = io.StringIO()
        ConsoleSink(out, FakeClock()).on_end(
            SessionResult("cutoff", 3723.0, 5.123, 250.0, 5.1, 54.58, 50.03, "EBC-B20H", "3.08", 100, 0))
        text = out.getvalue()
        self.assertTrue(text.startswith("\n"), repr(text))
        lines = text[1:].splitlines()
        self.assertEqual(len(lines), 6, lines)
        for want in ("cutoff", "runtime     1:02:03\n", "5.123", "5.100", "250.00", "54.58", "50.03"):
            self.assertIn(want, text)
        self.assertNotIn("01:02:03", text)
        self.assertNotIn("\x1b", text)

    # R38: the first OSError ends console output for the session, without raising

    def test_first_os_error_stops_all_console_output_and_is_logged_once_at_info(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(close_log)
        log = make_log(tmp.name)
        out = ClosingPipe(after_writes=1)
        sink = ConsoleSink(out, FakeClock())
        sink.on_start(DischargePlan(1.0, 50.0, 0), P.parse_status(hexframe(IDLE_HEX)))   # flush raises
        sink.on_sample(Sample(2.0, 54.21, 1.0, 0.001, 0.05, 0.0, "active"))
        sink.on_end(SessionResult("user_stop", 2.0, 0.001, 0.05, 0.0, 54.58, 54.21, "EBC-B20H", None, 3, 0))
        self.assertEqual((out.writes, out.failures), (1, 1))
        self.assertTrue(out.getvalue().startswith("starting CC 1.00 A"))
        lines = [l for l in log.path.read_text(encoding="utf-8").splitlines() if "console" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(" INFO console.ConsoleSink: console output stopped: BrokenPipeError", lines[0])
        self.assertEqual(log.stream.getvalue(), "")   # below the stderr mirror

    def test_any_os_error_on_write_counts_and_no_log_is_fine(self):
        class Gone(io.StringIO):
            calls = 0
            def write(self, text):
                Gone.calls += 1
                raise OSError(5, "Input/output error")
        close_log()
        sink = ConsoleSink(Gone(), FakeClock())
        sink.on_sample(Sample(2.0, 54.21, 1.0, 0.001, 0.05, 0.0, "active"))
        sink.on_end(SessionResult("cutoff", 2.0, 0.001, 0.05, 0.0, 54.58, 50.0, "EBC-B20H", None, 3, 0))
        self.assertEqual(Gone.calls, 1)

    def test_hms_is_one_format_with_unpadded_hours(self):
        cases = [(0, "0:00:00"), (154, "0:02:34"), (154.4, "0:02:34"), (3599.6, "1:00:00"), (3723, "1:02:03"),
                 (12 * 3600 + 300, "12:05:00"), (None, "-")]
        for seconds, want in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(hms(seconds), want)



class EntryScriptTests(unittest.TestCase):
    """R38: python/scripts/ebc.py when stdout's reader is gone before anything is written, as in
    `bin/ebc.sh ... | head -c 0`. The pipe's read end is closed before the child starts, so its
    first write always fails: no timing involved."""

    def run_with_closed_stdout(self, *args):
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            with tempfile.TemporaryDirectory() as cwd:
                return subprocess.run([sys.executable, str(EBC_PY), *args], stdout=write_end,
                                      stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=cwd, timeout=60)
        finally:
            os.close(write_end)

    def test_closed_stdout_keeps_exit_code_and_prints_nothing_on_stderr(self):
        for args in (["--version"], ["--help"]):
            with self.subTest(args=args):
                proc = self.run_with_closed_stdout(*args)
                self.assertEqual(proc.returncode, 0)
                self.assertNotIn(b"Exception ignored", proc.stderr)
                self.assertEqual(proc.stderr, b"")

    def test_closed_stdout_keeps_a_non_zero_exit_code(self):
        proc = self.run_with_closed_stdout()   # no command: error on stdout, exit 3
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stderr, b"")


if __name__ == "__main__":
    unittest.main()
