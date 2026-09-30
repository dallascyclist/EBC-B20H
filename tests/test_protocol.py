import unittest
from pathlib import Path

from ebctool import protocol as P
from tests.support import hexframe

FIX = Path(__file__).parent / "fixtures" / "frames"


def fixture(name):
    return [hexframe(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


class Base240Tests(unittest.TestCase):
    def test_round_trip_and_limits(self):
        self.assertEqual(P.encode240(5400), (22, 120))
        self.assertEqual(P.decode240(22, 120), 5400)
        self.assertEqual(P.encode240(0), (0, 0))
        with self.assertRaises(ValueError):
            P.encode240(57840)


class ChecksumTests(unittest.TestCase):
    def test_plain_xor(self):
        self.assertEqual(P.checksum(bytes([0x01, 0, 0x14, 0x01, 0x5A, 0, 0])), 0x4E)

    def test_high_nibble_quirk(self):
        self.assertEqual(P.checksum(bytes([0xF3, 0, 0, 0, 0, 0, 0])), 0x03)
        self.assertTrue(P.checksum_ok(bytes([0xF3, 0, 0, 0, 0, 0, 0]), 0x03))
        self.assertTrue(P.checksum_ok(bytes([0xF3, 0, 0, 0, 0, 0, 0]), 0xF3))
        self.assertFalse(P.checksum_ok(bytes([0xF3, 0, 0, 0, 0, 0, 0]), 0x04))


class CommandTests(unittest.TestCase):
    def test_connect_disconnect_stop(self):
        self.assertEqual(P.connect(), hexframe("fa 05 00 00 00 00 00 00 05 f8"))
        self.assertEqual(P.disconnect(), hexframe("fa 06 00 00 00 00 00 00 06 f8"))
        self.assertEqual(P.stop(), hexframe("fa 02 00 00 00 00 00 00 02 f8"))

    def test_start_cc_a20_reference_vector(self):
        self.assertEqual(P.start_cc(0.2, 3.3, 0), hexframe("fa 01 00 14 01 5a 00 00 4e f8"))

    def test_start_cc_b20h_captured_vector(self):
        self.assertEqual(P.start_cc(1.0, 50.0, 3), hexframe("fa 01 00 64 14 c8 00 03 ba f8"))

    def test_timer_sync(self):
        self.assertEqual(P.timer_sync(1), hexframe("fa 0a 00 01 00 00 00 00 0b f8"))
        self.assertEqual(P.timer_sync(2), hexframe("fa 0a 00 02 00 00 00 00 08 f8"))


class DecodeTests(unittest.TestCase):
    def test_voltage_ranges(self):
        self.assertAlmostEqual(P.decode_voltage(0x9F, 0x65), 54.93)
        self.assertAlmostEqual(P.decode_voltage(0x9F, 0x12), 54.10)
        self.assertAlmostEqual(P.decode_voltage(0x0F, 0x93), 3.747)
        self.assertAlmostEqual(P.decode_voltage(0, 0), 0.0)

    def test_capacity_ranges(self):
        self.assertAlmostEqual(P.decode_capacity(0x00, 0x53), 0.083)
        self.assertAlmostEqual(P.decode_capacity(0x29, 0x9E), 9.998)
        self.assertAlmostEqual(P.decode_capacity(0x8F, 0xC6), 17.50)
        self.assertAlmostEqual(P.decode_capacity(0xC4, 0x78), 143.92)
        self.assertAlmostEqual(P.decode_capacity(0xE7, 0x77), 231.1)

    def test_current(self):
        self.assertAlmostEqual(P.decode_current(0x00, 0x64), 1.0)


class ClassifyTests(unittest.TestCase):
    def test_states(self):
        self.assertEqual(P.classify(0x00), ("idle", 0))
        self.assertEqual(P.classify(0x01), ("idle", 1))
        self.assertEqual(P.classify(0x0A), ("active", 0))
        self.assertEqual(P.classify(0x14), ("finished", 0))
        self.assertEqual(P.classify(0x64), ("firmware_idle", 0))
        self.assertEqual(P.classify(0x65), ("firmware_idle", 1))
        self.assertEqual(P.classify(0x6E), ("firmware_active", 0))
        self.assertEqual(P.classify(0x33), ("unknown", None))


class ParseStatusTests(unittest.TestCase):
    def test_idle_frame_with_pack(self):
        s = P.parse_status(fixture("b20h-cc-run.hex")[0])
        self.assertEqual((s.state, s.mode, s.model, s.model_name), ("idle", 0, 0x1C, "EBC-B20H"))
        self.assertAlmostEqual(s.volts, 54.58)
        self.assertAlmostEqual(s.amps, 0.0)
        self.assertAlmostEqual(s.ah_device, 0.083)
        self.assertEqual((s.echo1, s.echo2, s.echo3), (100, 900, 0))
        self.assertIsNone(s.fw_version)
        self.assertTrue(s.is_measurement)

    def test_active_and_finished_frames(self):
        a, fw, fin, idle = [P.parse_status(f) for f in fixture("b20h-cc-run.hex")[1:]]
        self.assertEqual(a.state, "active"); self.assertAlmostEqual(a.amps, 1.0); self.assertAlmostEqual(a.volts, 54.33)
        self.assertEqual((a.echo1, a.echo2, a.echo3), (100, 5000, 3))
        self.assertEqual(fw.state, "firmware_active"); self.assertEqual(fw.fw_version, "3.08"); self.assertTrue(fw.is_active)
        self.assertFalse(fw.is_measurement)
        self.assertEqual(fin.state, "finished"); self.assertAlmostEqual(fin.ah_device, 0.050)
        self.assertEqual(idle.state, "idle"); self.assertAlmostEqual(idle.ah_device, 0.050)

    def test_firmware_idle_frame(self):
        s = P.parse_status(fixture("b20h-idle-no-pack.hex")[1])
        self.assertEqual((s.state, s.mode, s.fw_version), ("firmware_idle", 1, "3.08"))

    def test_bad_frames_raise(self):
        good = fixture("b20h-cc-run.hex")[0]
        with self.assertRaises(P.FrameError):
            P.parse_status(good[:-1])
        with self.assertRaises(P.FrameError):
            P.parse_status(b"\x00" + good[1:])
        bad = bytearray(good); bad[17] ^= 0x01
        with self.assertRaises(P.FrameError):
            P.parse_status(bytes(bad))


class ExtractFramesTests(unittest.TestCase):
    def test_split_garbage_and_partial(self):
        f1, f2 = fixture("b20h-cc-run.hex")[:2]
        stream = b"\x00\x11" + f1 + b"\xfa\x00zz" + f2[:10]
        frames, rest = P.extract_frames(stream)
        self.assertEqual(frames, [f1])
        self.assertEqual(rest, f2[:10])
        frames, rest = P.extract_frames(rest + f2[10:])
        self.assertEqual(frames, [f2])
        self.assertEqual(rest, b"")

    def test_false_sof_inside_frame_is_skipped(self):
        f1 = fixture("b20h-cc-run.hex")[0]
        junk = b"\xfa" + b"\x01" * 17 + b"\x00"  # 19 bytes, wrong trailer
        frames, rest = P.extract_frames(junk + f1)
        self.assertEqual(frames, [f1])
        self.assertEqual(rest, b"")


if __name__ == "__main__":
    unittest.main()
