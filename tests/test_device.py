import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ebctool.device import DeviceError, SerialDevice, list_ports, resolve_port
from ebctool.protocol import parse_status
from tests.fakes import FakeClock, FakeDevice, FakeSerial
from tests.support import close_log, hexframe, make_log

IDLE = hexframe("fa 00 00 00 9f 42 00 53 00 00 00 64 03 b4 00 00 1c 41 f8")
ACTIVE = hexframe("fa 0a 00 64 9f 29 00 01 00 00 00 64 14 c8 00 03 1c 7e f8")


class ResolvePortTests(unittest.TestCase):
    def test_prefers_wch_over_apple_node(self):
        def fake_glob(pattern):
            return {"/dev/cu.wchusbserial*": ["/dev/cu.wchusbserial11140"],
                    "/dev/cu.usbserial*": ["/dev/cu.usbserial-2141440"]}[pattern]
        self.assertEqual(resolve_port("auto", glob_fn=fake_glob), "/dev/cu.wchusbserial11140")

    def test_falls_back_to_apple_node(self):
        def fake_glob(pattern):
            return {"/dev/cu.wchusbserial*": [], "/dev/cu.usbserial*": ["/dev/cu.usbserial-2141440"]}[pattern]
        self.assertEqual(resolve_port("auto", glob_fn=fake_glob), "/dev/cu.usbserial-2141440")

    def test_none_found_is_error(self):
        with self.assertRaisesRegex(DeviceError, "no serial port"):
            resolve_port("auto", glob_fn=lambda p: [])

    def test_explicit_port_passes_through(self):
        self.assertEqual(resolve_port("/dev/ttyUSB0", glob_fn=lambda p: []), "/dev/ttyUSB0")

    def test_first_sorted_match_wins(self):
        def fake_glob(pattern):
            return {"/dev/cu.wchusbserial*": ["/dev/cu.wchusbserial2", "/dev/cu.wchusbserial1"]}[pattern]
        self.assertEqual(resolve_port("auto", glob_fn=fake_glob), "/dev/cu.wchusbserial1")


class ListPortsTests(unittest.TestCase):
    def test_pairs_device_with_description(self):
        port = SimpleNamespace(device="/dev/cu.wchusbserial11140", description="USB Serial")
        with mock.patch("serial.tools.list_ports.comports", return_value=[port]):
            self.assertEqual(list_ports(), [("/dev/cu.wchusbserial11140", "USB Serial")])


class SerialDeviceTests(unittest.TestCase):
    def make(self, chunks, **kwargs):
        fake = FakeSerial(chunks)
        dev = SerialDevice("/dev/fake", serial_factory=lambda port, baud, parity: fake, **kwargs)
        self.addCleanup(dev.close)
        return dev, fake

    def drain(self, dev, n, timeout=2.0):
        out = []
        for _ in range(n):
            s = dev.read(timeout)
            if s is None:
                break
            out.append(s)
        return out

    def start_log(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = make_log(tmp.name)
        self.addCleanup(close_log)
        return log

    def test_frames_split_across_reads_are_reassembled(self):
        dev, _ = self.make([IDLE[:7], IDLE[7:] + ACTIVE[:3], ACTIVE[3:]])
        got = self.drain(dev, 2)
        self.assertEqual([s.state for s in got], ["idle", "active"])
        self.assertEqual(dev.bad_frames, 0)

    def test_bad_checksum_is_dropped_and_counted(self):
        bad = bytearray(IDLE); bad[17] ^= 0x01
        dev, _ = self.make([bytes(bad) + ACTIVE])
        got = self.drain(dev, 2)
        self.assertEqual([s.state for s in got], ["active"])
        self.assertEqual(dev.bad_frames, 1)

    def test_send_writes_and_flushes(self):
        dev, fake = self.make([])
        dev.send(hexframe("fa 05 00 00 00 00 00 00 05 f8"))
        self.assertEqual(fake.written, [hexframe("fa 05 00 00 00 00 00 00 05 f8")])
        self.assertEqual(fake.flushes, 1)

    def test_last_frame_age_is_inf_before_first_frame(self):
        dev, _ = self.make([])
        self.assertEqual(dev.last_frame_age(), float("inf"))

    def test_close_closes_port(self):
        dev, fake = self.make([])
        dev.close()
        self.assertTrue(fake.closed)

    def test_last_frame_age_uses_injected_clock(self):
        clock = FakeClock(start=100.0)
        dev, _ = self.make([IDLE], clock=clock.monotonic)
        self.assertIsNotNone(dev.read(2.0))
        clock.sleep(3.0)
        self.assertEqual(dev.last_frame_age(), 3.0)

    def test_negative_timeout_counts_as_zero(self):
        dev, _ = self.make([])
        self.assertIsNone(dev.read(-1.0))

    def test_close_stops_daemon_reader_and_is_idempotent(self):
        dev, fake = self.make([])
        reader = dev._thread
        self.assertTrue(reader.daemon)
        dev.close()
        dev.close()
        self.assertFalse(reader.is_alive())
        self.assertTrue(fake.closed)

    def test_serial_failure_is_raised_by_read_after_queued_frames(self):
        dev, _ = self.make([IDLE, OSError("device not configured")])
        self.assertEqual(dev.read(2.0).state, "idle")
        started = time.monotonic()
        with self.assertRaisesRegex(DeviceError, "device not configured"):
            dev.read(5.0)
        self.assertLess(time.monotonic() - started, 2.0)   # raised promptly, not after the timeout
        with self.assertRaisesRegex(DeviceError, "device not configured"):
            dev.read(0.1)

    def test_default_factory_opens_9600_8e1_with_short_timeout(self):
        with mock.patch("serial.Serial", return_value=FakeSerial([])) as opened:
            SerialDevice("/nonexistent/ebctool-test-port").close()
        opened.assert_called_once_with("/nonexistent/ebctool-test-port", 9600, bytesize=8, parity="E",
                                       stopbits=1, timeout=0.1)

    def test_send_logs_tx_hex_at_debug(self):
        log = self.start_log()
        dev, _ = self.make([])
        dev.send(hexframe("fa 05 00 00 00 00 00 00 05 f8"))
        self.assertIn(" DEBUG device.send: tx fa 05 00 00 00 00 00 00 05 f8", log.path.read_text(encoding="utf-8"))

    def test_log_frames_logs_each_valid_frame_as_hex(self):
        log = self.start_log()
        dev, _ = self.make([IDLE], log_frames=True)
        self.assertIsNotNone(dev.read(2.0))
        self.assertIn(" DEBUG device.frame: " + IDLE.hex(" "), log.path.read_text(encoding="utf-8"))

    def test_valid_frames_are_not_logged_by_default(self):
        log = self.start_log()
        dev, _ = self.make([IDLE])
        self.assertIsNotNone(dev.read(2.0))
        self.assertNotIn(IDLE.hex(" "), log.path.read_text(encoding="utf-8"))

    def test_bad_frame_is_logged_with_reason(self):
        log = self.start_log()
        bad = bytearray(IDLE); bad[17] ^= 0x01
        dev, _ = self.make([bytes(bad) + ACTIVE])
        self.assertEqual(dev.read(2.0).state, "active")
        self.assertIn("bad frame dropped (bad checksum 40", log.path.read_text(encoding="utf-8"))


class FakeClockTests(unittest.TestCase):
    def test_now_sleep_and_monotonic_alias(self):
        clock = FakeClock(start=10.0)
        clock.sleep(2.5)
        self.assertEqual(clock.now(), 12.5)
        self.assertEqual(clock.monotonic(), 12.5)

    def test_negative_sleep_is_rejected_like_time_sleep(self):
        with self.assertRaises(ValueError):
            FakeClock().sleep(-1.0)

    def test_advance_to_never_moves_backwards(self):
        clock = FakeClock(start=5.0)
        clock.advance_to(3.0)
        self.assertEqual(clock.now(), 5.0)
        clock.advance_to(7.5)
        self.assertEqual(clock.now(), 7.5)


class FakeDeviceTests(unittest.TestCase):
    def setUp(self):
        self.idle = parse_status(IDLE)
        self.active = parse_status(ACTIVE)

    def test_frame_due_within_timeout_is_returned_at_its_time(self):
        clock = FakeClock()
        dev = FakeDevice([(0.5, self.idle)], clock)
        self.assertEqual(dev.last_frame_age(), float("inf"))
        self.assertIs(dev.read(1.0), self.idle)
        self.assertEqual(clock.now(), 0.5)
        self.assertEqual(dev.last_frame_age(), 0.0)

    def test_no_frame_within_timeout_advances_clock_by_timeout(self):
        clock = FakeClock()
        dev = FakeDevice([(5.0, self.idle)], clock)
        self.assertIsNone(dev.read(1.0))
        self.assertEqual(clock.now(), 1.0)
        self.assertIs(dev.read(4.0), self.idle)   # t == now + timeout is due
        self.assertEqual(clock.now(), 5.0)
        self.assertIsNone(dev.read(1.0))          # script exhausted
        self.assertEqual(clock.now(), 6.0)
        self.assertEqual(dev.last_frame_age(), 1.0)

    def test_overdue_frame_never_moves_clock_backwards(self):
        clock = FakeClock(start=3.0)
        dev = FakeDevice([(1.0, self.idle), (2.0, self.active)], clock)
        self.assertIs(dev.read(0.5), self.idle)
        self.assertEqual(clock.now(), 3.0)
        self.assertIs(dev.read(0.0), self.active)
        self.assertEqual(clock.now(), 3.0)
        self.assertEqual(dev.last_frame_age(), 0.0)

    def test_negative_timeout_counts_as_zero(self):
        clock = FakeClock(start=1.0)
        dev = FakeDevice([(1.0, self.idle), (9.0, self.active)], clock)
        self.assertIs(dev.read(-1.0), self.idle)
        self.assertIsNone(dev.read(-1.0))
        self.assertEqual(clock.now(), 1.0)

    def test_send_close_and_bad_frames(self):
        dev = FakeDevice([], FakeClock())
        dev.send(hexframe("fa 05 00 00 00 00 00 00 05 f8"))
        dev.close()
        self.assertEqual(dev.sent, [hexframe("fa 05 00 00 00 00 00 00 05 f8")])
        self.assertTrue(dev.closed)
        self.assertEqual(dev.bad_frames, 0)


if __name__ == "__main__":
    unittest.main()
