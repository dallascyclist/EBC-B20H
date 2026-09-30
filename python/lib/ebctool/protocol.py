"""ZKETECH EBC frame codec. Pure functions; no I/O. Wire facts: spec sections 2 and 6.

Host -> device commands are 10 bytes: FA cmd p1 p1 p2 p2 p3 p3 xor F8.
Device -> host status reports are 19 bytes: FA cmd f1 f1 ... f7 f7 model xor F8.
Two-byte fields are base-240 (hi * 240 + lo). The checksum is the XOR of the bytes
between FA and the checksum; when its high nibble is 0xF the vendor clears that nibble.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
from operator import xor

SOF, EOF, COMMAND_LEN, STATUS_LEN = 0xFA, 0xF8, 10, 19
CMD_START_CC, CMD_STOP, CMD_CONNECT, CMD_DISCONNECT, CMD_ADJUST_CC, CMD_TIMER_SYNC = 0x01, 0x02, 0x05, 0x06, 0x07, 0x0A

MODELS = {0x05: "EBC-A05", 0x06: "EBC-A10H", 0x09: "EBC-A20", 0x10: "EBC-A40", 0x1C: "EBC-B20H"}
MODE_NAMES = {0: "cc_discharge", 1: "cp_discharge", 2: "cv_charge"}
STATES = ("idle", "active", "finished", "firmware_idle", "firmware_active", "unknown")
_STATE_BASES = ((0x00, "idle"), (0x0A, "active"), (0x14, "finished"), (0x64, "firmware_idle"), (0x6E, "firmware_active"))
_BASE240_MAX = 240 * 240 + 239
_SOF_BYTE = bytes([SOF])


class FrameError(ValueError):
    """A byte string that is not a valid frame."""


@dataclass(frozen=True)
class Model:
    ident: int
    name: str


def model_name(ident: int) -> str:
    return MODELS.get(ident, "unknown(0x%02x)" % ident)


# --- base-240 and checksum -------------------------------------------------------

def encode240(value: int) -> tuple[int, int]:
    """Split value into (hi, lo) with value == hi * 240 + lo."""
    if not 0 <= value <= _BASE240_MAX:
        raise ValueError("value out of base-240 range: %r" % value)
    return divmod(value, 240)


def decode240(hi: int, lo: int) -> int:
    return hi * 240 + lo


def checksum(payload: bytes) -> int:
    """XOR of payload; the vendor clears the high nibble when it is 0xF."""
    value = reduce(xor, payload, 0)
    return value & 0x0F if value & 0xF0 == 0xF0 else value


def checksum_ok(payload: bytes, given: int) -> bool:
    """Accept either the plain XOR or its nibble-cleared form."""
    plain = reduce(xor, payload, 0)
    return given == plain or given == (plain & 0x0F)


# --- commands (host -> device) ----------------------------------------------------

def build(cmd: int, p1: int = 0, p2: int = 0, p3: int = 0) -> bytes:
    """10-byte command frame; p1..p3 are base-240 encoded."""
    payload = bytes([cmd, *encode240(p1), *encode240(p2), *encode240(p3)])
    return _SOF_BYTE + payload + bytes([checksum(payload), EOF])


def connect() -> bytes:
    return build(CMD_CONNECT)


def disconnect() -> bytes:
    return build(CMD_DISCONNECT)


def stop() -> bytes:
    return build(CMD_STOP)


def start_cc(amps: float, cutoff_v: float, minutes: int) -> bytes:
    """Start constant-current discharge: current in 10 mA, cutoff in 10 mV, max minutes (0 = none)."""
    return build(CMD_START_CC, round(amps * 100), round(cutoff_v * 100), minutes)


def adjust_cc(amps: float, cutoff_v: float, minutes: int) -> bytes:
    """Change a running CC discharge; same fields and units as start_cc."""
    return build(CMD_ADJUST_CC, round(amps * 100), round(cutoff_v * 100), minutes)


def timer_sync(minutes: int) -> bytes:
    """Elapsed-minutes sync the vendor software sends once a minute."""
    return build(CMD_TIMER_SYNC, minutes)


# --- field decoders (device -> host) -----------------------------------------------

def decode_voltage(hi: int, lo: int) -> float:
    """Live voltage: bit 7 set -> 10 mV units offset 0x800; else plain mV."""
    if hi & 0x80:
        return ((hi & 0x7F) * 240 + lo - 0x800) / 100
    return (hi * 240 + lo) / 1000


def decode_capacity(hi: int, lo: int) -> float:
    """Capacity in Ah: bits 7..5 set -> 0.1 Ah offset 0x1C00; bit 7 -> 10 mAh offset 0x800; else mAh."""
    if hi & 0xE0 == 0xE0:
        return ((hi & 0x3F) * 240 + lo - 0x1C00) / 10
    if hi & 0x80:
        return ((hi & 0x7F) * 240 + lo - 0x800) / 100
    return (hi * 240 + lo) / 1000


def decode_current(hi: int, lo: int) -> float:
    """Live current: plain base-240 in 10 mA units."""
    return decode240(hi, lo) / 100


def classify(cmd: int) -> tuple[str, int | None]:
    """Status command byte -> (state, mode); each state spans base..base+2, one per mode."""
    for base, state in _STATE_BASES:
        if base <= cmd <= base + 2:
            return state, cmd - base
    return "unknown", None


# --- status frames -----------------------------------------------------------------

@dataclass(frozen=True)
class Status:
    raw: bytes
    cmd: int
    state: str
    mode: int | None
    amps: float
    volts: float
    ah_device: float
    fields: tuple[int, ...]
    echo1: int
    echo2: int
    echo3: int
    model: int
    model_name: str
    fw_version: str | None

    @property
    def is_measurement(self) -> bool:
        return self.state in ("idle", "active", "finished")

    @property
    def is_active(self) -> bool:
        return self.state in ("active", "firmware_active")


def parse_status(frame: bytes) -> Status:
    """Decode one 19-byte status report; FrameError on bad length, markers or checksum."""
    frame = bytes(frame)
    if len(frame) != STATUS_LEN:
        raise FrameError("status frame is %d bytes, expected %d" % (len(frame), STATUS_LEN))
    if frame[0] != SOF or frame[-1] != EOF:
        raise FrameError("bad frame markers %02x..%02x" % (frame[0], frame[-1]))
    payload = frame[1:-2]
    if not checksum_ok(payload, frame[-2]):
        raise FrameError("bad checksum %02x, computed %02x" % (frame[-2], checksum(payload)))
    cmd = payload[0]
    state, mode = classify(cmd)
    fields = tuple(decode240(payload[1 + 2 * k], payload[2 + 2 * k]) for k in range(7))
    firmware = state in ("firmware_idle", "firmware_active")
    model = payload[15]
    return Status(
        raw=frame,
        cmd=cmd,
        state=state,
        mode=mode,
        amps=decode_current(payload[1], payload[2]),
        volts=decode_voltage(payload[3], payload[4]),
        ah_device=decode_capacity(payload[5], payload[6]),
        fields=fields,
        echo1=fields[4],
        echo2=fields[5],
        echo3=fields[6],
        model=model,
        model_name=model_name(model),
        fw_version="%d.%02d" % divmod(fields[4], 100) if firmware else None,
    )


def extract_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a byte stream into 19-byte FA..F8 candidates; return (frames, unconsumed tail).

    Bytes before the first SOF are dropped. A candidate window that contains a later SOF
    is abandoned for that SOF: FA cannot occur inside a real frame (data bytes are
    base-240, cmd <= 0x70, the checksum's 0xF high nibble is cleared). A full window with
    a bad trailer advances one byte. A partial candidate stays in the tail. Checksums are
    not checked here.
    """
    frames, i = [], 0
    while True:
        i = buffer.find(_SOF_BYTE, i)
        if i < 0:
            return frames, b""
        later = buffer.find(_SOF_BYTE, i + 1, i + STATUS_LEN)
        if later >= 0:
            i = later
            continue
        if len(buffer) - i < STATUS_LEN:
            return frames, buffer[i:]
        if buffer[i + STATUS_LEN - 1] == EOF:
            frames.append(buffer[i:i + STATUS_LEN])
            i += STATUS_LEN
        else:
            i += 1
