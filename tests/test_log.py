import io
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ebctool.log import RunLog, set_current, traced

LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z pid=(\d+) (DEBUG|INFO|WARNING|ERROR) ([\w.]+): (.*)$")


class RunLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(set_current, None)
        self.now = datetime(2026, 9, 30, 20, 15, 0, tzinfo=timezone.utc)

    def make(self):
        stream = io.StringIO()
        log = RunLog(self.tmp.name, now=self.now, pid=4242, stream=stream)
        self.addCleanup(log.close)
        return log, stream

    def test_file_name_and_run_id_from_utc_start(self):
        log, _ = self.make()
        self.assertEqual(log.run_id, "202609302015")
        self.assertEqual(log.path, Path(self.tmp.name) / "202609302015.log")

    def test_collision_appends_pid(self):
        self.make()
        second, _ = self.make()
        self.assertEqual(second.run_id, "202609302015-4242")

    def test_line_format_decision_marker_and_mirroring(self):
        log, stream = self.make()
        log.debug("m.f", "d"); log.info("m.f", "i"); log.decision("m.f", "chose x")
        log.warning("m.f", "w"); log.error("m.f", "e")
        lines = log.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertIsNotNone(LINE.match(line), line)
        self.assertIn(" INFO m.f: DECISION chose x", lines[2])
        self.assertEqual(len(stream.getvalue().splitlines()), 2)

    def test_traced_logs_entry_and_exit_when_current(self):
        log, _ = self.make()
        set_current(log)

        @traced
        def add(a, b):
            return a + b
        self.assertEqual(add(2, 3), 5)
        text = log.path.read_text(encoding="utf-8")
        self.assertIn("-> (a=2, b=3)", text)
        self.assertIn("<- 5 (", text)


if __name__ == "__main__":
    unittest.main()
