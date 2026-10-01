import tempfile
import unittest

from ebctool import protocol as P
from ebctool.log import set_current
from ebctool.session import CommLost, DischargePlan, DischargeSession, PreflightError
from tests.fakes import FakeClock, FakeDevice
from tests.support import close_log, hexframe, make_log

IDLE = P.parse_status(hexframe("fa 00 00 00 9f 42 00 53 00 00 00 64 03 b4 00 00 1c 41 f8"))       # 54.58 V
ACTIVE = P.parse_status(hexframe("fa 0a 00 64 9f 29 00 01 00 00 00 64 14 c8 00 03 1c 7e f8"))     # 1.00 A 54.33 V
FW_ACTIVE = P.parse_status(hexframe("fa 6e 00 64 9f 2b 00 00 00 00 01 44 32 ea 08 e4 1c d3 f8"))
FINISHED = P.parse_status(hexframe("fa 14 00 64 9f 12 00 32 00 00 00 64 14 c8 00 03 1c 68 f8"))   # 54.10 V 0.050 Ah
IDLE_AFTER = P.parse_status(hexframe("fa 00 00 64 9f 12 00 32 00 00 00 64 14 c8 00 03 1c 7c f8"))
PLAN = DischargePlan(current_a=1.0, cutoff_v=50.0, max_minutes=3)


class RecordingSink:
    def __init__(self):
        self.started = None; self.samples = []; self.ended = None
    def on_start(self, plan, first): self.started = (plan, first)
    def on_sample(self, s): self.samples.append(s)
    def on_end(self, r): self.ended = r


def _fix(frame):
    """Recompute the checksum byte of a hand-built frame."""
    payload = frame[1:-2]
    return frame[:-2] + bytes([P.checksum(payload), P.EOF])


def frame(cmd_hex, amps_hex="00 64", volts_hex="9f 29", cap_hex="00 01"):
    return P.parse_status(_fix(hexframe("fa %s %s %s %s 00 00 00 64 14 c8 00 03 1c 00 f8" % (cmd_hex, amps_hex, volts_hex, cap_hex))))


def volts_hex(volts):
    """Live-voltage field for volts: 10 mV units offset 0x800, base 240, bit 7 set on the high byte."""
    hi, lo = divmod(round(volts * 100) + 0x800, 240)
    return "%02x %02x" % (hi | 0x80, lo)


def script_full_run():
    s = [(0.5, IDLE), (2.5, IDLE), (6.5, ACTIVE), (8.5, FW_ACTIVE)]
    t = 10.5
    for k in range(2, 90):   # last active frame at 186.5 s, i.e. 180 s after the first active frame
        s.append((t, frame("0a", cap_hex="00 %02x" % min(k // 2, 0x32)))); t += 2.0
    s.append((t, FINISHED)); s.append((t + 0.1, IDLE_AFTER))
    return s


class SessionTests(unittest.TestCase):
    def make(self, script, plan=PLAN, silence=10.0, stop=lambda: False):
        clock = FakeClock()
        dev = FakeDevice(script, clock)
        sink = RecordingSink()
        sess = DischargeSession(dev, plan, [sink], clock, silence_timeout_s=silence, stop_requested=stop)
        return sess, dev, sink, clock

    def test_full_run_ends_max_time_with_samples_and_integrals(self):
        sess, dev, sink, _ = self.make(script_full_run())
        r = sess.run()
        self.assertEqual(r.end_reason, "max_time")
        self.assertAlmostEqual(r.ah_device_final, 0.050)
        self.assertGreater(r.ah_host, 0.045); self.assertLess(r.ah_host, 0.052)
        self.assertGreater(r.wh_host, 2.4); self.assertLess(r.wh_host, 2.9)
        self.assertAlmostEqual(r.start_v, 54.58); self.assertAlmostEqual(r.end_v, 54.10)
        self.assertEqual((r.device_model, r.firmware), ("EBC-B20H", "3.08"))
        self.assertGreater(len(sink.samples), 80)
        self.assertEqual(sink.ended, r)
        self.assertEqual(dev.sent[0], P.connect())
        self.assertEqual(dev.sent[1], P.start_cc(1.0, 50.0, 3))
        self.assertIn(P.timer_sync(1), dev.sent); self.assertIn(P.timer_sync(2), dev.sent)
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])

    def test_ends_on_active_to_idle_without_finished_frame(self):
        script = [(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("0a")), (10.5, IDLE_AFTER)]
        sess, dev, sink, _ = self.make(script)
        r = sess.run()
        self.assertEqual(r.end_reason, "device_error")   # neither cutoff nor max time reached
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])

    def test_cutoff_reason_when_voltage_at_cutoff(self):
        # 50.03 V -> (50.03*100 + 2048) = 7051 = 29*240 + 91 -> hi 0x1d|0x80 = 0x9d, lo 0x5b
        script = [(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("0a", volts_hex="9d 5b")), (10.5, frame("14", volts_hex="9d 5b"))]
        sess, _, _, _ = self.make(script)
        self.assertEqual(sess.run().end_reason, "cutoff")

    def test_no_idle_within_timeout_is_preflight_error(self):
        sess, dev, sink, _ = self.make([])
        with self.assertRaises(PreflightError) as cm:
            sess.run()
        self.assertEqual(cm.exception.reason, "no_answer")
        self.assertEqual(dev.sent[-1], P.disconnect())
        self.assertNotIn(P.start_cc(1.0, 50.0, 3), dev.sent)
        self.assertIsNone(sink.started)

    def test_connect_is_resent_while_waiting(self):
        sess, dev, _, _ = self.make([(7.0, IDLE), (8.0, ACTIVE), (10.0, FINISHED)])
        sess.run()
        self.assertGreaterEqual(dev.sent.count(P.connect()), 2)

    def test_voltage_below_cutoff_is_preflight_error(self):
        sess, dev, _, _ = self.make([(0.5, IDLE)], plan=DischargePlan(1.0, 54.5, 0))
        with self.assertRaises(PreflightError) as cm:
            sess.run()
        self.assertEqual(cm.exception.reason, "voltage_below_cutoff")

    def test_not_idle_is_preflight_error(self):
        sess, _, _, _ = self.make([(0.5, ACTIVE)])
        with self.assertRaises(PreflightError) as cm:
            sess.run()
        self.assertEqual(cm.exception.reason, "not_idle")

    def test_never_active_is_device_error(self):
        script = [(0.5, IDLE), (2.5, IDLE), (4.5, IDLE), (6.5, IDLE), (8.5, IDLE), (10.5, IDLE), (12.5, IDLE), (14.5, IDLE), (16.5, IDLE), (18.5, IDLE)]
        sess, dev, sink, _ = self.make(script)
        r = sess.run()
        self.assertEqual(r.end_reason, "device_error")
        self.assertEqual(sink.ended.end_reason, "device_error")
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])

    def test_silence_during_run_is_comm_lost(self):
        script = [(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("0a"))]
        sess, dev, sink, _ = self.make(script, silence=10.0)
        r = sess.run()
        self.assertEqual(r.end_reason, "comm_lost")
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])

    def test_user_stop(self):
        clock = FakeClock()
        script = [(0.5, IDLE), (6.5, ACTIVE)] + [(8.5 + 2 * k, frame("0a")) for k in range(20)]
        dev = FakeDevice(script, clock)
        sink = RecordingSink()
        sess = DischargeSession(dev, PLAN, [sink], clock, stop_requested=lambda: clock.now() > 12.0)
        r = sess.run()
        self.assertEqual(r.end_reason, "user_stop")
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])

    def test_firmware_frames_are_counted_but_not_sampled(self):
        script = [(0.5, IDLE), (6.5, FW_ACTIVE), (8.5, frame("0a")), (10.5, FINISHED)]
        sess, _, sink, _ = self.make(script)
        r = sess.run()
        self.assertEqual(len(sink.samples), 1)
        self.assertGreaterEqual(r.frames, 4)



# --- beyond the brief: rulings R4-R6, R21 and every exit path ------------------------

FW_IDLE = P.parse_status(hexframe("fa 64 00 00 9f 41 00 53 00 00 01 44 32 ea 08 e4 1c 84 f8"))
START_CMD = P.CMD_START_CC


def idle_with_model(model_byte):
    return P.parse_status(_fix(hexframe("fa 00 00 00 9f 42 00 53 00 00 00 64 03 b4 00 00 %02x 00 f8" % model_byte)))


def sent_start(dev):
    return [f for f in dev.sent if f[1] == START_CMD]


class CountingSink(RecordingSink):
    def __init__(self):
        super().__init__(); self.ends = 0
    def on_end(self, r):
        super().on_end(r); self.ends += 1


class FailingSink(CountingSink):
    def on_sample(self, s):
        raise RuntimeError("sink broke")


class DyingDevice(FakeDevice):
    """Raises on read once its script is used up, as SerialDevice does when the port vanishes."""
    def read(self, timeout):
        if not self._script:
            raise OSError("port vanished")
        return super().read(timeout)


CUTOFF_RUN = [(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("0a", volts_hex="9d 5b")), (10.5, frame("14", volts_hex="9d 5b"))]


class SessionExitPathTests(unittest.TestCase):
    def make(self, script, plan=PLAN, silence=10.0, stop=lambda: False, sinks=None, device=FakeDevice):
        clock = FakeClock()
        dev = device(script, clock)
        sinks = [CountingSink()] if sinks is None else sinks
        sess = DischargeSession(dev, plan, sinks, clock, silence_timeout_s=silence, stop_requested=stop)
        return sess, dev, sinks, clock

    def test_abandon_sends_disconnect(self):
        sess, dev, sinks, _ = self.make([(0.5, IDLE)])
        first = sess.connect()
        sess.preflight(first)
        sess.abandon()
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])
        self.assertIsNone(sinks[0].started)

    def test_connect_preflight_run_from_split(self):
        sess, dev, sinks, _ = self.make(CUTOFF_RUN)
        first = sess.connect()
        self.assertEqual(first.state, "idle")
        self.assertEqual(dev.sent, [P.connect()])
        sess.preflight(first)
        self.assertEqual(dev.sent, [P.connect()])
        r = sess.run_from(first)
        self.assertEqual(r.end_reason, "cutoff")
        self.assertEqual(dev.sent, [P.connect(), P.start_cc(1.0, 50.0, 3), P.stop(), P.disconnect()])
        self.assertEqual(r.frames, 4)   # R6: the connect-phase idle frame counts
        self.assertEqual(sinks[0].ends, 1)
        self.assertEqual(sinks[0].started, (PLAN, first))

    def test_preflight_failures_disconnect_and_never_start(self):
        cases = [("not_idle", [(0.5, ACTIVE)], PLAN),
                 ("not_idle", [(0.5, FINISHED)], PLAN),
                 ("voltage_below_cutoff", [(0.5, IDLE)], DischargePlan(1.0, 54.5, 0)),
                 ("no_answer", [], PLAN)]
        for reason, script, plan in cases:
            with self.subTest(reason=reason, script=script):
                sess, dev, sinks, _ = self.make(script, plan=plan)
                with self.assertRaises(PreflightError) as cm:
                    sess.run()
                self.assertEqual(cm.exception.reason, reason)
                self.assertEqual(dev.sent[-1], P.disconnect())
                self.assertEqual(sent_start(dev), [])
                self.assertIsNone(sinks[0].started)
                self.assertEqual(sinks[0].ends, 0)

    def test_preflight_error_carries_the_offending_status(self):
        sess, _, _, _ = self.make([(0.5, ACTIVE)])
        with self.assertRaises(PreflightError) as cm:
            sess.connect()
        self.assertEqual(cm.exception.status.state, "active")

    def test_only_firmware_frames_is_no_answer(self):
        script = [(0.5 + 2 * k, FW_IDLE) for k in range(10)]
        sess, dev, _, clock = self.make(script)
        with self.assertRaises(PreflightError) as cm:
            sess.connect()
        self.assertEqual(cm.exception.reason, "no_answer")
        self.assertLessEqual(clock.now(), 10.0)
        self.assertEqual(dev.sent[-1], P.disconnect())

    def test_read_error_during_connect_disconnects_and_propagates(self):
        sess, dev, _, _ = self.make([], device=DyingDevice)
        with self.assertRaises(OSError):
            sess.connect()
        self.assertEqual(dev.sent, [P.connect(), P.disconnect()])

    def test_read_error_after_start_is_comm_lost(self):
        sess, dev, sinks, _ = self.make([(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("0a"))], device=DyingDevice)
        r = sess.run()
        self.assertEqual(r.end_reason, "comm_lost")
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
        self.assertEqual((sinks[0].ends, sinks[0].ended), (1, r))

    def test_sink_failure_stops_disconnects_ends_once_and_reraises(self):
        failing, good = FailingSink(), CountingSink()
        sess, dev, _, _ = self.make(CUTOFF_RUN, sinks=[failing, good])
        with self.assertRaises(RuntimeError):
            sess.run()
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
        self.assertEqual((failing.ends, good.ends), (1, 1))
        self.assertEqual(good.ended.end_reason, "device_error")

    def test_keyboard_interrupt_during_run_is_user_stop(self):
        clock = FakeClock()
        def stop():
            if clock.now() > 12.0:
                raise KeyboardInterrupt
            return False
        script = [(0.5, IDLE), (6.5, ACTIVE)] + [(8.5 + 2 * k, frame("0a")) for k in range(20)]
        dev = FakeDevice(script, clock)
        sink = CountingSink()
        r = DischargeSession(dev, PLAN, [sink], clock, stop_requested=stop).run()
        self.assertEqual(r.end_reason, "user_stop")
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])
        self.assertEqual(sink.ends, 1)

    def test_stop_requested_while_awaiting_active(self):
        clock = FakeClock()
        script = [(0.5 + 2 * k, IDLE) for k in range(10)]
        dev = FakeDevice(script, clock)
        r = DischargeSession(dev, PLAN, [], clock, stop_requested=lambda: clock.now() > 3.0).run()
        self.assertEqual(r.end_reason, "user_stop")
        self.assertEqual(r.runtime_s, 0.0)
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])

    def test_samples_have_strictly_increasing_t_s(self):
        script = [(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("0a")), (8.5, frame("0a")), (10.5, FINISHED)]
        sess, _, sinks, _ = self.make(script)
        r = sess.run()
        self.assertEqual([s.t_s for s in sinks[0].samples], [0.0, 2.0])
        self.assertAlmostEqual(r.ah_host, 2.0 / 3600)
        self.assertEqual(r.frames, 5)

    def test_cutoff_boundary_compares_in_centivolts(self):
        # 32.09 V -> 3209 + 2048 = 5257 = 21*240 + 217 -> hi 0x95, lo 0xd9; float 32.04 + 0.05 < 32.09
        script = [(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("14", volts_hex="95 d9"))]
        sess, _, _, _ = self.make(script, plan=DischargePlan(1.0, 32.04, 0))
        self.assertEqual(sess.run().end_reason, "cutoff")

    # R30: after a real cutoff the load is off and the voltage rebounds, so the end reason is
    # judged on the lower of the ending frame and the last sampled active frame.

    def test_lost_finished_frame_with_rebounded_idle_is_cutoff(self):
        idle = frame("00", amps_hex="00 00", volts_hex=volts_hex(51.20))
        sag = frame("0a", volts_hex=volts_hex(50.02))
        self.assertEqual((idle.volts, sag.volts), (51.20, 50.02))
        sess, _, _, _ = self.make([(0.5, IDLE), (6.5, ACTIVE), (8.5, sag), (10.5, idle)])
        r = sess.run()
        self.assertEqual(r.end_reason, "cutoff")
        self.assertEqual(r.end_v, 51.20)   # what the pack read at the end; only the classification changed

    def test_rebounded_finished_frame_is_cutoff(self):
        finished = frame("14", volts_hex=volts_hex(51.20))
        sag = frame("0a", volts_hex=volts_hex(50.03))
        self.assertEqual((finished.volts, sag.volts), (51.20, 50.03))
        sess, _, _, _ = self.make([(0.5, IDLE), (6.5, ACTIVE), (8.5, sag), (10.5, finished)])
        r = sess.run()
        self.assertEqual(r.end_reason, "cutoff")
        self.assertEqual(r.end_v, 51.20)

    def test_idle_end_above_cutoff_is_still_device_error(self):
        high = frame("0a", volts_hex=volts_hex(53.10))
        idle = frame("00", amps_hex="00 00", volts_hex=volts_hex(53.00))
        self.assertEqual((high.volts, idle.volts), (53.10, 53.00))
        sess, _, _, _ = self.make([(0.5, IDLE), (6.5, high), (8.5, high), (10.5, idle)],
                                  plan=DischargePlan(1.0, 50.0, 0))
        r = sess.run()
        self.assertEqual(r.end_reason, "device_error")
        self.assertEqual(r.end_v, 53.00)

    def test_preflight_boundary_compares_in_centivolts(self):
        # 16.06 V -> 1606 + 2048 = 3654 = 15*240 + 54 -> hi 0x8f, lo 0x36; float 15.56 + 0.5 > 16.06
        idle = frame("00", amps_hex="00 00", volts_hex="8f 36")
        sess, _, _, _ = self.make([(0.5, idle)], plan=DischargePlan(1.0, 15.56, 0))
        sess.preflight(sess.connect())   # must not raise

    def test_rejects_non_positive_silence_timeout(self):
        clock = FakeClock()
        for bad in (0.0, -1.0):
            with self.subTest(silence=bad), self.assertRaises(ValueError):
                DischargeSession(FakeDevice([], clock), PLAN, [], clock, silence_timeout_s=bad)

    def test_no_firmware_frame_means_firmware_none(self):
        sess, _, _, _ = self.make(CUTOFF_RUN)
        r = sess.run()
        self.assertEqual((r.device_model, r.firmware), ("EBC-B20H", None))


class SessionLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(close_log)
        self.log = make_log(self.tmp.name)

    def lines(self):
        return self.log.path.read_text(encoding="utf-8").splitlines()

    def test_unknown_model_logs_one_warning(self):
        odd = idle_with_model(0x2A)
        clock = FakeClock()
        dev = FakeDevice([(0.5, odd), (6.5, ACTIVE), (8.5, frame("0a")), (10.5, FINISHED)], clock)
        r = DischargeSession(dev, PLAN, [], clock).run()
        warnings = [l for l in self.lines() if "WARNING" in l and "unknown model byte 0x2a; decoding as EBC-B20H" in l]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(r.device_model, "unknown(0x2a)")

    def test_known_model_logs_no_model_warning(self):
        clock = FakeClock()
        DischargeSession(FakeDevice(CUTOFF_RUN, clock), PLAN, [], clock).run()
        self.assertFalse([l for l in self.lines() if "unknown model" in l])

    def test_transitions_are_decision_lines(self):
        clock = FakeClock()
        DischargeSession(FakeDevice(CUTOFF_RUN, clock), PLAN, [], clock).run()
        decisions = [l for l in self.lines() if " DECISION " in l]
        self.assertGreaterEqual(len(decisions), 6)   # connect, idle, preflight, start, active, end, stop+disconnect
        self.assertTrue(any("cutoff" in l for l in decisions[-3:]))

    def test_samples_skipped_for_same_time_are_logged_at_debug(self):
        clock = FakeClock()
        script = [(0.5, IDLE), (6.5, ACTIVE), (8.5, frame("0a")), (8.5, frame("0a")), (10.5, FINISHED)]
        DischargeSession(FakeDevice(script, clock), PLAN, [], clock).run()
        self.assertTrue([l for l in self.lines() if " DEBUG " in l and "skip" in l.lower()])



class BrokenLog:
    """A RunLog whose every write fails, as on a full disk."""
    def __getattr__(self, name):
        def fail(*args):
            raise OSError("disk full")
        return fail


class BrokenLogTests(unittest.TestCase):
    def setUp(self):
        set_current(BrokenLog())
        self.addCleanup(set_current, None)

    def test_failing_log_does_not_block_stop_and_disconnect(self):
        clock = FakeClock()
        dev = FakeDevice(CUTOFF_RUN, clock)
        r = DischargeSession(dev, PLAN, [], clock).run()
        self.assertEqual(r.end_reason, "cutoff")
        self.assertEqual(dev.sent[-2:], [P.stop(), P.disconnect()])

    def test_failing_log_keeps_preflight_error_and_disconnect(self):
        clock = FakeClock()
        dev = FakeDevice([(0.5, ACTIVE)], clock)
        with self.assertRaises(PreflightError):
            DischargeSession(dev, PLAN, [], clock).run()
        self.assertEqual(dev.sent[-1], P.disconnect())


if __name__ == "__main__":
    unittest.main()
