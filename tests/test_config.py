import tempfile
import unittest
from pathlib import Path

from ebctool.config import ConfigError, default_config, load_config

FULL = """
[serial]
port = /dev/cu.wchusbserial11140
parity = O
baud = 9600
silence_timeout_s = 7.5

[run]
log_dir = mylogs
db_path = data/t.db
log_frames = true

[limits]
min_current_a = 0.1
max_current_a = 20
min_cutoff_v = 8
max_cutoff_v = 88
"""


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        p = Path(self.tmp.name) / "config.ini"
        p.write_text(text, encoding="utf-8")
        return p

    def test_full_file_round_trips(self):
        c = load_config(self.write(FULL))
        self.assertEqual(c.serial.port, "/dev/cu.wchusbserial11140")
        self.assertEqual(c.serial.parity, "O")
        self.assertAlmostEqual(c.serial.silence_timeout_s, 7.5)
        self.assertEqual(c.run.log_dir, Path("mylogs"))
        self.assertEqual(c.run.db_path, Path("data/t.db"))
        self.assertTrue(c.run.log_frames)
        self.assertAlmostEqual(c.limits.min_current_a, 0.1)

    def test_empty_file_gives_defaults(self):
        c = load_config(self.write("[serial]\n"))
        self.assertEqual(c.serial.port, "auto")
        self.assertEqual(c.serial.parity, "E")
        self.assertEqual(c.run.db_path, Path("db/ebctool.db"))
        self.assertAlmostEqual(c.limits.max_cutoff_v, 88.0)

    def test_missing_file_is_error(self):
        with self.assertRaises(ConfigError):
            load_config(Path(self.tmp.name) / "nope.ini")

    def test_bad_parity_is_error(self):
        with self.assertRaisesRegex(ConfigError, "parity"):
            load_config(self.write("[serial]\nparity = N\n"))

    def test_non_numeric_limit_is_error(self):
        with self.assertRaisesRegex(ConfigError, "max_current_a"):
            load_config(self.write("[limits]\nmax_current_a = lots\n"))

    def test_inverted_limits_is_error(self):
        with self.assertRaisesRegex(ConfigError, "min_current_a"):
            load_config(self.write("[limits]\nmin_current_a = 5\nmax_current_a = 1\n"))

    def test_default_config(self):
        c = default_config()
        self.assertEqual(c.serial.parity, "E")
        self.assertEqual(c.run.log_dir, Path("logs"))

    def test_committed_example_config_loads(self):
        root = Path(__file__).resolve().parents[1]
        c = load_config(root / "config" / "config.example.ini")
        self.assertEqual(c.serial.port, "auto")
        self.assertEqual(c.serial.parity, "E")
        self.assertAlmostEqual(c.serial.silence_timeout_s, 10.0)
        self.assertEqual(c.run.db_path, Path("db/ebctool.db"))


if __name__ == "__main__":
    unittest.main()
