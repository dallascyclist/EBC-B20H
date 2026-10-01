"""One constant-current discharge: Connect, preflight, Start, watch the run, Stop and Disconnect.

The tester's firmware is the safety authority: it ends the discharge itself at the cutoff
voltage or the time limit. The session never sends Start unless preflight passed, records
what the tester reports, and on every exit path after Start sends Stop then Disconnect.
Rules: spec section 9; device behaviour: spec section 2.

Callers either use run() or, to put a prompt between preflight and Start, call connect(),
preflight(first), then run_from(first) or abandon().

Voltages are compared in whole centivolts: the tester reports 10 mV steps, and float sums
such as 32.04 + 0.05 would misplace the exact boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Protocol, Sequence

from ebctool import protocol as P
from ebctool.log import current
from ebctool.protocol import Status

if TYPE_CHECKING:
    from ebctool.device import Device

WHERE = "session.run"
CONNECT_RESEND_S = 5.0     # resend Connect this often while the tester is silent
ACTIVE_WAIT_S = 10.0       # Start must produce an active frame within this, else device_error
TIMER_SYNC_S = 60.0        # vendor software sends elapsed minutes once a minute
MAX_READ_STEP_S = 1.0      # longest single read, so stop_requested() is polled at least this often
HEADROOM_CV = 50           # preflight: idle voltage >= cutoff + 0.50 V
CUTOFF_MARGIN_CV = 5       # end: lower of ending and last sampled voltage <= cutoff + 0.05 V counts as cutoff
MAX_TIME_MARGIN_S = 5.0    # end: runtime >= max minutes - 5 s counts as max_time


def _cv(volts: float) -> int:
    """Volts to whole centivolts."""
    return round(volts * 100)


@dataclass(frozen=True)
class DischargePlan:
    current_a: float
    cutoff_v: float
    max_minutes: int   # 0 = no limit


@dataclass(frozen=True)
class Sample:
    t_s: float          # host seconds since the first active frame; strictly increasing
    volts: float
    amps: float
    ah_host: float
    wh_host: float
    ah_device: float
    state: str


@dataclass(frozen=True)
class SessionResult:
    end_reason: str     # cutoff | max_time | user_stop | comm_lost | device_error
    runtime_s: float
    ah_host: float
    wh_host: float
    ah_device_final: float
    start_v: float
    end_v: float
    device_model: str
    firmware: str | None
    frames: int
    bad_frames: int


class PreflightError(Exception):
    """Refused before Start; nothing was started and Disconnect has been sent.
    status is the frame that failed the check, or None when the tester never answered."""

    REASONS = ("no_answer", "not_idle", "voltage_below_cutoff")

    def __init__(self, reason: str, status: Optional[Status] = None, detail: str = ""):
        if reason not in self.REASONS:
            raise ValueError("unknown preflight reason %r" % (reason,))
        super().__init__(detail or reason)
        self.reason = reason
        self.status = status


class CommLost(Exception):
    """No valid frame for silence_timeout_s after Start, or reading the device failed."""


class UserStop(Exception):
    """stop_requested() returned true while the discharge was starting or running."""


class Sink(Protocol):
    def on_start(self, plan: DischargePlan, first: Status) -> None: ...

    def on_sample(self, sample: Sample) -> None: ...

    def on_end(self, result: SessionResult) -> None: ...


class _Integrator:
    """Trapezoid integrals of current (Ah) and power (Wh) over host time."""

    def __init__(self):
        self.ah = 0.0
        self.wh = 0.0
        self._prev: Optional[tuple[float, float, float]] = None

    def push(self, t: float, amps: float, volts: float) -> None:
        if self._prev is not None:
            t0, a0, v0 = self._prev
            dt = t - t0
            self.ah += (a0 + amps) / 2 * dt / 3600
            self.wh += (a0 * v0 + amps * volts) / 2 * dt / 3600
        self._prev = (t, amps, volts)


class DischargeSession:
    def __init__(self, device: "Device", plan: DischargePlan, sinks: Sequence[Sink], clock,
                 silence_timeout_s: float = 10.0, stop_requested: Callable[[], bool] = lambda: False):
        if not silence_timeout_s > 0:
            raise ValueError("silence_timeout_s must be positive, got %r" % (silence_timeout_s,))
        self.device = device
        self.plan = plan
        self.sinks = list(sinks)
        self.clock = clock
        self.silence_timeout_s = silence_timeout_s
        self.stop_requested = stop_requested
        self.frames = 0                        # every Status read, connect phase included
        self.firmware: Optional[str] = None    # from the latest firmware report seen
        self._step = min(MAX_READ_STEP_S, silence_timeout_s / 4)
        self._model_warned = False
        self._last: Optional[Status] = None    # latest measurement frame
        self._integ = _Integrator()
        self._t_active0: Optional[float] = None
        self._last_t_s: Optional[float] = None
        self._last_sampled_v: Optional[float] = None   # volts of the last active frame that made a Sample
        self._minute = 0

    # --- before Start ----------------------------------------------------------------

    def run(self) -> SessionResult:
        first = self.connect()
        self.preflight(first)
        return self.run_from(first)

    def connect(self) -> Status:
        """Send Connect and return the first measurement frame, which must be idle.
        Does not poll stop_requested. Any failure sends Disconnect before propagating."""
        try:
            return self._connect()
        except BaseException as exc:
            self._best_effort(P.disconnect(), "Disconnect")
            self._log("decision", "connect failed (%s); sent Disconnect" % (type(exc).__name__,))
            raise

    def _connect(self) -> Status:
        t0 = self.clock.now()
        deadline = t0 + self.silence_timeout_s
        next_resend = t0 + CONNECT_RESEND_S
        self._log("decision", "sending Connect; waiting up to %.1f s for an idle status frame" % self.silence_timeout_s)
        self.device.send(P.connect())
        while True:
            now = self.clock.now()
            status = self._read(max(0.0, min(self._step, deadline - now, next_resend - now)))
            if status is not None and status.is_measurement:
                return self._accept_first(status)
            now = self.clock.now()
            if now >= deadline:
                seen = "no frame at all" if self.frames == 0 else "%d frames, none a measurement" % self.frames
                self._log("decision", "no idle status frame within %.1f s of Connect (%s); no_answer"
                          % (self.silence_timeout_s, seen))
                raise PreflightError("no_answer", None, "no answer from tester within %.1f s" % self.silence_timeout_s)
            if now >= next_resend:
                self._log("decision", "no status frame yet; resending Connect")
                self.device.send(P.connect())
                next_resend = now + CONNECT_RESEND_S

    def _accept_first(self, status: Status) -> Status:
        if status.model not in P.MODELS and not self._model_warned:
            self._model_warned = True
            self._log("warning", "unknown model byte 0x%02x; decoding as EBC-B20H" % status.model)
        if status.state != "idle":
            self._log("decision", "first status frame is %s, not idle; not_idle" % status.state)
            raise PreflightError("not_idle", status, "tester is %s, not idle" % status.state)
        self._last = status
        self._log("decision", "tester %s idle at %.2f V" % (status.model_name, status.volts))
        return status

    def preflight(self, idle: Status) -> None:
        """Refuse unless the idle voltage is at least cutoff + 0.5 V; Disconnect before refusing."""
        cutoff = self.plan.cutoff_v
        if _cv(idle.volts) < _cv(cutoff) + HEADROOM_CV:
            self._best_effort(P.disconnect(), "Disconnect")
            self._log("decision", "pack %.2f V is below cutoff %.2f V + 0.50 V; voltage_below_cutoff; sent Disconnect"
                      % (idle.volts, cutoff))
            raise PreflightError("voltage_below_cutoff", idle, "pack reads %.2f V, cutoff %.2f V needs at least"
                                 " 0.5 V headroom" % (idle.volts, cutoff))
        self._log("decision", "preflight passed: %.2f V >= cutoff %.2f V + 0.50 V" % (idle.volts, cutoff))

    def abandon(self) -> None:
        """Give up between preflight and Start (prompt declined, Ctrl-C): Disconnect only."""
        self._best_effort(P.disconnect(), "Disconnect")
        self._log("decision", "abandoned before Start; sent Disconnect")

    # --- Start to end ----------------------------------------------------------------

    def run_from(self, first: Status) -> SessionResult:
        """Start the discharge and follow it to its end. Every exit sends Stop then Disconnect
        and calls each sink's on_end once; an unexpected error is re-raised after that."""
        self._last = first
        error: Optional[BaseException] = None
        end_reason = "device_error"
        try:
            for sink in self.sinks:
                sink.on_start(self.plan, first)
            plan = self.plan
            self._log("decision", "sending Start: CC %.2f A, cutoff %.2f V, max %d min"
                      % (plan.current_a, plan.cutoff_v, plan.max_minutes))
            self.device.send(P.start_cc(plan.current_a, plan.cutoff_v, plan.max_minutes))
            if self._await_active():
                end_reason = self._watch()
            else:
                self._log("decision", "no active frame within %.0f s of Start; device_error" % ACTIVE_WAIT_S)
        except (UserStop, KeyboardInterrupt) as exc:
            why = "Ctrl-C" if isinstance(exc, KeyboardInterrupt) else "stop requested"
            self._log("decision", "%s; user_stop" % why)
            end_reason = "user_stop"
        except CommLost as exc:
            self._log("decision", "%s; comm_lost" % exc)
            end_reason = "comm_lost"
        except Exception as exc:
            self._log("exception", exc)
            self._log("decision", "unexpected %s; ending as device_error" % type(exc).__name__)
            end_reason, error = "device_error", exc
        finally:
            t_end = self.clock.now()
            self._best_effort(P.stop(), "Stop")
            self._best_effort(P.disconnect(), "Disconnect")
            self._log("decision", "sent Stop then Disconnect")
        result = self._result(end_reason, first, t_end)
        self._log("decision", "session ended: %s after %.1f s, %.4f Ah host, %.3f Ah device, %.2f V"
                  % (result.end_reason, result.runtime_s, result.ah_host, result.ah_device_final, result.end_v))
        for sink in self.sinks:
            try:
                sink.on_end(result)
            except Exception as exc:
                self._log("exception", exc)
                error = exc if error is None else error
        if error is not None:
            raise error
        return result

    def _await_active(self) -> bool:
        """Read until a frame with is_active (True) or ACTIVE_WAIT_S after Start (False)."""
        t_start = self.clock.now()
        deadline = t_start + ACTIVE_WAIT_S
        while True:
            self._check_stop()
            status = self._read_running(max(0.0, min(self._step, deadline - self.clock.now())))
            if status is not None:
                if status.is_measurement:
                    self._last = status
                if status.is_active:
                    now = self.clock.now()
                    self._log("decision", "first active frame (%s) %.1f s after Start; running"
                              % (status.state, now - t_start))
                    self._t_active0 = now
                    self._take(status)
                    return True
            self._check_silence()
            if self.clock.now() >= deadline:
                return False

    def _watch(self) -> str:
        """Follow the running discharge until the tester ends it; returns the end reason."""
        while True:
            self._check_stop()
            status = self._read_running(self._step)
            if status is not None:
                if status.state in ("finished", "idle"):
                    self._last = status
                    return self._device_end_reason(status)
                self._take(status)
            self._check_silence()
            self._timer_sync()

    def _take(self, status: Status) -> None:
        """Turn an active measurement frame into a Sample; firmware and unknown frames are only counted."""
        if status.state != "active":
            if status.state == "unknown":
                self._log("debug", "ignoring frame with unknown command byte 0x%02x" % status.cmd)
            return
        self._last = status
        t_s = self.clock.now() - self._t_active0
        if self._last_t_s is not None and t_s <= self._last_t_s:
            self._log("debug", "skipping sample at t=%.3f s: not after the previous sample at %.3f s"
                      % (t_s, self._last_t_s))
            return
        self._last_t_s = t_s
        self._last_sampled_v = status.volts
        self._integ.push(t_s, status.amps, status.volts)
        sample = Sample(t_s, status.volts, status.amps, self._integ.ah, self._integ.wh, status.ah_device, status.state)
        for sink in self.sinks:
            sink.on_sample(sample)

    def _device_end_reason(self, status: Status) -> str:
        """Once the load is off the voltage rebounds, so a frame read after a real cutoff can sit
        well above it: judge the cutoff on the lower of the ending frame and the last sampled
        active frame (the ending frame alone if nothing was sampled)."""
        runtime = self.clock.now() - self._t_active0
        plan = self.plan
        judged = status.volts if self._last_sampled_v is None else min(status.volts, self._last_sampled_v)
        if _cv(judged) <= _cv(plan.cutoff_v) + CUTOFF_MARGIN_CV:
            reason = "cutoff"
        elif plan.max_minutes > 0 and runtime >= plan.max_minutes * 60 - MAX_TIME_MARGIN_S:
            reason = "max_time"
        else:
            reason = "device_error"
        self._log("decision", "tester reported %s at %.2f V after %.1f s (judged on %.2f V); %s"
                  % (status.state, status.volts, runtime, judged, reason))
        return reason

    def _timer_sync(self) -> None:
        minute = int((self.clock.now() - self._t_active0) // TIMER_SYNC_S)
        if minute > self._minute:
            self._minute = minute
            self._log("info", "timer sync: minute %d" % minute)
            self._best_effort(P.timer_sync(minute), "timer sync")

    def _check_stop(self) -> None:
        if self.stop_requested():
            raise UserStop()

    def _check_silence(self) -> None:
        age = self.device.last_frame_age()
        if age > self.silence_timeout_s:
            raise CommLost("no valid frame for %.1f s" % age)

    def _result(self, end_reason: str, first: Status, t_end: float) -> SessionResult:
        last = self._last or first
        runtime = 0.0 if self._t_active0 is None else t_end - self._t_active0
        return SessionResult(end_reason=end_reason, runtime_s=runtime, ah_host=self._integ.ah, wh_host=self._integ.wh,
                             ah_device_final=last.ah_device, start_v=first.volts, end_v=last.volts,
                             device_model=first.model_name, firmware=self.firmware, frames=self.frames,
                             bad_frames=self.device.bad_frames)

    # --- device and log helpers ------------------------------------------------------

    def _read(self, timeout: float) -> Optional[Status]:
        status = self.device.read(timeout)
        if status is not None:
            self.frames += 1
            if status.fw_version is not None:
                self.firmware = status.fw_version
        return status

    def _read_running(self, timeout: float) -> Optional[Status]:
        """After Start a failing read (the port vanished) is a communication loss."""
        try:
            return self._read(timeout)
        except Exception as exc:
            raise CommLost("reading the tester failed: %s: %s" % (type(exc).__name__, exc)) from exc

    def _best_effort(self, frame: bytes, what: str) -> None:
        try:
            self.device.send(frame)
        except Exception as exc:
            self._log("warning", "%s not sent: %s: %s" % (what, type(exc).__name__, exc))

    def _log(self, kind: str, message) -> None:
        """Log through the current RunLog, if any. kind names a RunLog method (decision, debug,
        info, warning, exception). A failing log write is dropped: it must never keep Stop or
        Disconnect from being sent, and with the log itself broken there is nowhere to report it."""
        log = current()
        if log is None:
            return
        try:
            getattr(log, kind)(WHERE, message)
        except Exception:
            pass
