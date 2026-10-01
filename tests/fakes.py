"""Test doubles for the device layer: a scripted serial port, a manual clock, and a scripted Device;
and a stdout whose pipe closes."""
import io
import time
from collections import deque


class FakeSerial:
    """Stands in for serial.Serial. Each read(n) returns the next scripted chunk whole (n is
    ignored); a chunk that is an exception instance is raised instead, as a vanished port would.
    Once the script is exhausted read returns b"" after a short sleep, like a 0.1 s read timeout
    with nothing on the wire, so a reader thread does not spin."""

    def __init__(self, chunks):
        self._chunks = deque(chunks)
        self.written = []
        self.flushes = 0
        self.input_resets = 0
        self.closed = False

    def read(self, n):
        if not self._chunks:
            time.sleep(0.005)
            return b""
        chunk = self._chunks.popleft()
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def flush(self):
        self.flushes += 1

    def reset_input_buffer(self):
        """Counted only: scripted chunks model future arrivals, so nothing is discarded."""
        self.input_resets += 1

    def close(self):
        self.closed = True

    @property
    def in_waiting(self):
        return sum(len(c) for c in self._chunks if not isinstance(c, BaseException))


class FakeClock:
    """Manual clock: time moves only through sleep() and advance_to()."""

    def __init__(self, start=0.0):
        self._now = float(start)

    def now(self):
        return self._now

    monotonic = now

    def sleep(self, seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        self._now += seconds

    def advance_to(self, t):
        """Move to t, or stay put if t is already past; never goes backwards."""
        self._now = max(self._now, float(t))


class FakeDevice:
    """Scripted Device: script is [(t, Status), ...] in time order, t in clock seconds."""

    def __init__(self, script, clock):
        self._script = deque(script)
        self.clock = clock
        self.sent = []
        self.bad_frames = 0
        self.closed = False
        self._t_last = None

    def send(self, frame):
        self.sent.append(bytes(frame))

    def read(self, timeout):
        """Return the next Status if it is due by now + timeout, moving the clock to its time
        (never backwards); otherwise let timeout pass and return None. Negative timeout is 0."""
        timeout = max(0.0, timeout)
        if self._script and self._script[0][0] <= self.clock.now() + timeout:
            t, status = self._script.popleft()
            self.clock.advance_to(t)
            self._t_last = self.clock.now()
            return status
        self.clock.sleep(timeout)
        return None

    def last_frame_age(self):
        if self._t_last is None:
            return float("inf")
        return self.clock.now() - self._t_last

    def close(self):
        self.closed = True


def make_device_factory(script, device_class=FakeDevice):
    """A device_factory for run.main: factory(config, port) builds device_class(script, clock) over
    one shared FakeClock, exposed as factory.clock. factory.last is the latest device built, or
    None while the command has not opened one."""
    clock = FakeClock()

    def factory(config, port):
        factory.last = device_class(list(script), clock)
        return factory.last

    factory.clock = clock
    factory.last = None
    return factory


class ClosingPipe(io.StringIO):
    """A stdout whose reader goes away, like a pipe into a tee killed by the same Ctrl-C.

    The pipe starts closing on close_pipe(), once `after_writes` writes have gone through, or on a
    write containing `after_text`. Like a buffered pipe, it still takes writes until the next flush;
    that flush raises BrokenPipeError and from then on every write and flush does. failures counts
    the raises."""

    def __init__(self, after_writes=None, after_text=None):
        super().__init__()
        self.after_writes = after_writes
        self.after_text = after_text
        self.writes = 0
        self.closing = False
        self.broken = False
        self.failures = 0

    def close_pipe(self):
        self.closing = True

    def _fail(self):
        self.broken = True
        self.failures += 1
        raise BrokenPipeError(32, "Broken pipe")

    def write(self, text):
        if self.broken:
            self._fail()
        self.writes += 1
        if self.after_writes is not None and self.writes >= self.after_writes:
            self.closing = True
        if self.after_text is not None and self.after_text in text:
            self.closing = True
        return super().write(text)

    def flush(self):
        if self.broken or self.closing:
            self._fail()
