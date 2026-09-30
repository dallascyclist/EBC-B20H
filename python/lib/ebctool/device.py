"""Serial transport for the tester: port choice, port listing, and a SerialDevice whose
reader thread turns the byte stream into Status reports.

The port is opened once, at 9600 baud 8 data bits 1 stop bit, and never reopened. Parity E is
the default; the tester also answers O, never N.
"""
from __future__ import annotations

import glob
import queue
import threading
import time
from typing import Optional, Protocol

import serial

from ebctool.log import current
from ebctool.protocol import FrameError, Status, extract_frames, parse_status

AUTO_PATTERNS = ("/dev/cu.wchusbserial*", "/dev/cu.usbserial*")   # WCH driver node first, then Apple's
_READ_CHUNK = 512
_JOIN_TIMEOUT_S = 2.0
_READER_DIED = object()   # queue marker put once when the reader thread stops on an error


class DeviceError(Exception):
    """No usable serial port, or the open port failed while running."""


def resolve_port(setting: str, glob_fn=glob.glob) -> str:
    """'auto' -> first sorted wchusbserial node, else first sorted usbserial node; else as given."""
    if setting != "auto":
        return setting
    for pattern in AUTO_PATTERNS:
        matches = sorted(glob_fn(pattern))
        if matches:
            return matches[0]
    raise DeviceError("no serial port found; is the tester on a port directly on the Mac?")


def list_ports() -> list[tuple[str, str]]:
    """(device, description) for every serial port the OS reports."""
    import serial.tools.list_ports

    return [(p.device, p.description) for p in serial.tools.list_ports.comports()]


class Device(Protocol):
    bad_frames: int

    def send(self, frame: bytes) -> None: ...

    def read(self, timeout: float) -> Optional[Status]: ...

    def last_frame_age(self) -> float: ...

    def close(self) -> None: ...


def _open_serial(port: str, baud: int, parity: str):
    return serial.Serial(port, baud, bytesize=8, parity=parity, stopbits=1, timeout=0.1)


class SerialDevice:
    """Device over a real serial port. A daemon reader thread parses frames into a queue;
    bad frames are dropped and counted in bad_frames."""

    def __init__(self, port: str, parity: str = "E", baud: int = 9600, serial_factory=None,
                 clock=time.monotonic, log_frames: bool = False):
        self.port = port
        self.log_frames = log_frames
        self.bad_frames = 0
        self._clock = clock
        self._last_ts: Optional[float] = None
        self._error: Optional[BaseException] = None
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._closed = False
        self._ser = (serial_factory or _open_serial)(port, baud, parity)
        self._thread = threading.Thread(target=self._reader, name="ebctool-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        try:
            self._pump()
        except Exception as exc:   # port vanished (or a bug): surface once through read(), then stop
            self._error = exc
            self._queue.put(_READER_DIED)

    def _pump(self) -> None:
        buf = b""
        while not self._stop.is_set():
            chunk = self._ser.read(_READ_CHUNK)
            if not chunk:
                continue
            buf += chunk
            frames, buf = extract_frames(buf)
            for raw in frames:
                log = current()
                try:
                    status = parse_status(raw)
                except FrameError as exc:
                    self.bad_frames += 1
                    if log is not None:
                        log.debug("device.frame", "bad frame dropped (%s): %s" % (exc, raw.hex(" ")))
                    continue
                if log is not None and self.log_frames:
                    log.debug("device.frame", raw.hex(" "))
                self._last_ts = self._clock()
                self._queue.put(status)   # last: whoever reads it sees its log line and last_ts

    def send(self, frame: bytes) -> None:
        log = current()
        if log is not None:
            log.debug("device.send", "tx " + frame.hex(" "))
        self._ser.write(frame)
        self._ser.flush()

    def read(self, timeout: float) -> Optional[Status]:
        """Next Status, or None if none arrives within timeout (negative counts as 0).
        DeviceError once the reader has stopped on a port error and the queue is drained."""
        try:
            item = self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if item is _READER_DIED:
            self._queue.put(item)   # every later read fails the same way
            raise DeviceError(str(self._error)) from self._error
        return item

    def last_frame_age(self) -> float:
        if self._last_ts is None:
            return float("inf")
        return self._clock() - self._last_ts

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._thread.join(_JOIN_TIMEOUT_S)
        self._ser.close()
