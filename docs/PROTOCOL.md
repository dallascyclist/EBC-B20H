# EBC-B20H serial protocol

These notes describe the serial protocol of the ZKETECH EBC-B20H as ebctool uses it. They combine
the published references for the EBC-A20 family (listed at the end) with what one B20H (model byte
`0x1C`, firmware 3.08) did on the wire on 2026-09-30. Where it matters, each section says whether
a point was confirmed on the B20H or comes from the references only. The codec is
`python/lib/ebctool/protocol.py`; its tests use frames captured from this tester
(`tests/fixtures/frames/`).

## Link

- The tester's own cable: a CH340 USB-serial adapter, USB ID 1A86:7523.
- 9600 baud, 8 data bits, 1 stop bit, even parity. From a silent state Connect gets a reply with
  even or odd parity, never with none. ebctool defaults to even (`[serial] parity = E`).
- Port names: on macOS the WCH CH34x driver names the port `/dev/cu.wchusbserial` plus a number
  (for example `/dev/cu.wchusbserial11140`), and Apple's built-in driver names the same cable
  `/dev/cu.usbserial-` plus a number. The number follows the USB port, so it changes when the
  cable moves. On Linux the kernel's `ch341` driver gives `/dev/ttyUSB0` or similar.
- The tester is silent until it receives Connect and stops sending after Disconnect.
- ebctool opens the port once per command and holds it until the command ends.

## Framing

Every frame starts with `FA` and ends with `F8`.

    host to tester, 10 bytes:  FA cmd p1h p1l p2h p2l p3h p3l chk F8
    tester to host, 19 bytes:  FA cmd f1h f1l f2h f2l f3h f3l f4h f4l f5h f5l f6h f6l f7h f7l model chk F8

`chk` covers every byte between `FA` and `chk`: the command byte and the three parameters, or the
command byte, the seven fields and the model byte. Confirmed on the B20H.

## Base-240 fields

Every two-byte value is base 240: `value = hi × 240 + lo`, each byte from `00` to `EF`. For
example 5000 is `14 c8` (20 × 240 + 200). Because no byte of a plain value reaches `F0`, the
markers `FA` and `F8` cannot occur inside a field. Live voltage and capacity add range flags to
the high byte (see below), which can lift it above `EF`, but within this tester's limits it never
reaches `FA`.

## Checksum

`chk` is the XOR of the covered bytes, with one quirk taken from the references: when the XOR's
high nibble is `F`, the vendor clears that nibble, so `FE` is sent as `0E`. That keeps the markers
out of the checksum position as well. ebctool sends the cleared form and accepts either form on
receive.

Example, as ebctool builds it: Start 1.50 A, cutoff 42.00 V, no limit has the XOR `FE`, so the
frame is `fa 01 00 96 11 78 00 00 0e f8`.

No frame in the 2026-09-30 capture needed the quirk, so it is not yet confirmed on the B20H.

## Commands

| cmd | Meaning | p1 | p2 | p3 | Frame | On the B20H |
|---|---|---|---|---|---|---|
| `05` | Connect; the LCD shows PC | 0 | 0 | 0 | `fa 05 00 00 00 00 00 00 05 f8` | Confirmed: starts the stream |
| `06` | Disconnect | 0 | 0 | 0 | `fa 06 00 00 00 00 00 00 06 f8` | Confirmed: stops the stream |
| `02` | Stop | 0 | 0 | 0 | `fa 02 00 00 00 00 00 00 02 f8` | Stop during a running discharge not yet observed |
| `01` | Start CC discharge | current, 10 mA | cutoff, 10 mV | max minutes, 0 = none | `fa 01 00 64 14 c8 00 03 ba f8` (1.00 A, 50.00 V, 3 min) | Confirmed |
| `07` | Adjust a running CC discharge | as `01` | as `01` | as `01` | `fa 07 00 64 14 c8 00 03 bc f8` | References only; ebctool v1 never sends it |
| `0A` | Timer sync | minutes elapsed | 0 | 0 | `fa 0a 00 01 00 00 00 00 0b f8` (minute 1) | Accepted; no visible effect |

Command parameters are plain base-240 with no range flags: current in 10 mA, cutoff in 10 mV,
time in minutes. The units were confirmed through the echo fields (see Behaviours): a PC Start
with 1.00 A, 9.00 V and no limit (`fa 01 00 64 03 b4 00 00 d2 f8`) made the idle report echo 100,
900 and 0.

The vendor's software sends the timer sync once a minute. Its purpose is unknown; on the B20H it
changed nothing visible. ebctool sends it once a minute while a discharge runs, to mirror the
vendor. Do not confuse the host command `0A` (timer sync) with the status byte `0A` (CC running),
which travels the other way.

## Status frame

| Bytes | Field | In a CC status frame | Encoding |
|---|---|---|---|
| 0 | `FA` | start marker | |
| 1 | cmd | state and mode (see State bytes) | |
| 2–3 | f1 | live current | plain base-240, 10 mA |
| 4–5 | f2 | live voltage | range-encoded |
| 6–7 | f3 | capacity discharged | range-encoded |
| 8–9 | f4 | unknown; always 0 so far | |
| 10–11 | f5 | echo of the set current | plain base-240, 10 mA |
| 12–13 | f6 | echo of the cutoff | plain base-240, 10 mV |
| 14–15 | f7 | echo of the time limit | plain base-240, minutes |
| 16 | model | `1C` is the EBC-B20H | |
| 17 | chk | checksum | |
| 18 | `F8` | end marker | |

Live current (f1) is confirmed as plain base-240 in 10 mA: 1.00 A reads `00 64` (100). In a
firmware report (cmd `64` or `6E`) f1 to f3 still carry the live current, voltage and capacity,
but f5 holds the firmware version and f6 and f7 hold constants: on this unit f5 = 308 (firmware
3.08), f6 = 12234 and f7 = 2148.

Model bytes known to ebctool, all from the references except the B20H's: `05` EBC-A05, `06`
EBC-A10H, `09` EBC-A20, `10` EBC-A40, `1C` EBC-B20H. An unknown model byte is decoded as a B20H
and logged as a warning.

A running frame from the capture (10.37 s), byte by byte:

| Bytes | Hex | Decoded |
|---|---|---|
| cmd | `0a` | CC discharge running |
| f1 | `00 64` | 100, so 1.00 A |
| f2 | `9f 29` | bit 7 set: ((0x1F × 240) + 0x29 − 0x800) / 100 = 54.33 V |
| f3 | `00 01` | 1, so 0.001 Ah |
| f4 | `00 00` | 0 |
| f5 | `00 64` | set current 1.00 A |
| f6 | `14 c8` | 5000, so cutoff 50.00 V |
| f7 | `00 03` | time limit 3 minutes |
| model | `1c` | EBC-B20H |
| chk | `7e` | XOR of the 16 bytes from cmd to model |

## State bytes

The status command byte is a base plus the mode: +0 constant-current discharge, +1
constant-power discharge, +2 (labelled `cv_charge` in `protocol.py`; never seen on the B20H). The
numbering matches the A20.

| Base | State | Seen on the B20H |
|---|---|---|
| `00` | idle | `00` after a CC start or run; `01` on arrival, because the unit's last PC-commanded mode was constant power |
| `0A` | discharge running | `0A` |
| `14` | discharge finished | `14`, in exactly one frame |
| `64` | firmware report, idle | `64`, and `65` on arrival |
| `6E` | firmware report, running | `6E` |

## Voltage and capacity encoding

The live voltage (f2) and the capacity (f3) carry range flags in the high byte; the command
parameters and the echo fields do not.

Live voltage, confirmed on the B20H in the bit-7 form:

| High byte | Value | Example |
|---|---|---|
| bit 7 clear | `(hi × 240 + lo) / 1000` V, 1 mV steps | `00 00` is 0.00 V (no pack) |
| bit 7 set | `((hi & 0x7F) × 240 + lo − 0x800) / 100` V, 10 mV steps, offset 2048 | `9f 65` is 54.93 V, `9f 12` is 54.10 V |

A pack measured at 54.9 V read `9f 65`: nonsense as plain base-240, 54.93 V in the bit-7 form. The
switch to the bit-7 form is expected at 10 V; below that the plain form has only been seen reading
0.00 V with no pack attached.

Capacity, from the references; only the plain form is confirmed on the B20H:

| High byte | Value | Example (decoded by ebctool) |
|---|---|---|
| bit 7 clear | `(hi × 240 + lo) / 1000` Ah, 1 mAh steps | `00 32` is 0.050 Ah (seen), `29 9e` is 9.998 Ah |
| bit 7 set, bits 6 and 5 not both set | `((hi & 0x7F) × 240 + lo − 0x800) / 100` Ah, 10 mAh steps | `8f c6` is 17.50 Ah, `91 24` is 20.68 Ah, `c4 78` is 143.92 Ah |
| bits 7, 6 and 5 set | `((hi & 0x3F) × 240 + lo − 0x1C00) / 10` Ah, 0.1 Ah steps | `e7 77` is 231.1 Ah |

The two upper forms are unverified on the B20H, and one reference decoder flags the 10–200 Ah
branch as wrong. That is why ebctool reports host-integrated Ah and stores the tester's count
beside it. A full discharge of a 10 to 20 Ah e-bike pack takes the count past 10 Ah into the
10 mAh form, so the first full pack test will check it.

## Behaviours

Confirmed on the B20H.

- **Streams only in PC mode.** The tester is silent before Connect and after Disconnect. In
  between it sends one frame about every 2 s, idle or running. For the first 10 to 15 s after
  Connect, and again after Start, every other frame is a firmware report; after that every frame
  is a status frame. In the capture 3 of the 92 frames from Start to the finished frame were
  firmware reports.
- **Ignores Connect during panel use.** The tester does not answer Connect while its settings
  screen is open, nor while a test started from its front panel is running; it answers only when
  idle on its testing screen. A panel-started test cannot be attached to, so every test ebctool
  logs is started from the PC.
- **Echoes the last PC command.** In the idle report f5 to f7 echo the last PC-commanded
  parameters, not the front-panel step program. On arrival the unit echoed 50, 5400 and 200.
  Saving panel step 1 as 1.00 A / 9.00 V / 000 left the echo unchanged; a PC Start with 1.00 A,
  9.00 V and no limit changed it to 100 / 900 / 0. The panel program is invisible on the wire.
- **Stays idle without a pack.** A PC Start with no pack attached (0.00 V) is accepted, but the
  tester stays idle and never reports running.
- **Resets capacity on Start.** The capacity count goes to 0 at a PC Start. It counted 0.050 Ah
  for 1.00 A over 3 minutes and 0.083 Ah for an earlier 5 minute panel test, both exact. Host
  trapezoid integration of the 3 minute run gave 0.0489 Ah.
- **Current starts one frame late.** In the capture the first running frame after Start still read
  0.00 A; the next read the set current.
- **Enforces the time limit itself.** With a 3 minute limit the run ended 179.9 s after the first
  running frame. The timer syncs sent at minutes 1 and 2 changed nothing visible. The tester does
  not report elapsed time, so the host clock is the only time source.
- **Sends the finished frame once.** `14` comes in exactly one frame, immediately followed by idle
  `00` frames that keep the final capacity. In the capture the finished frame and the first idle
  frame arrived together, both still showing 1.00 A; the next frame read 0 A and the voltage had
  rebounded from 54.10 to 54.34 V. ebctool ends a run on either `14` or a switch from running to
  idle, so one lost frame cannot leave it waiting.

## macOS: keep the tester on a direct port

On 2026-09-30, with the cable four hubs deep in a dock chain, configuring the port (`tcsetattr`)
failed with `EINVAL` ("Invalid argument") about 75% of the time, at random, under both Apple's
`AppleUSBCHCOM` driver and WCH's driver, even for a call that changed nothing. Random success
points at the hub chain dropping the driver's USB control transfers to the chip, not at a rejected
setting. On a port directly on the Mac the port opened first time and Connect got valid frames.
If "Invalid argument" comes back, check how deep the tester sits in the USB tree (System
Information, USB; or `ioreg -p IOUSB`) before suspecting the driver or the code.

## A full run, annotated

`docs/captures/2026-09-30-pc-cc-1A-50V-3min.log` holds every frame received during one
PC-started constant-current discharge: 1.00 A to a 50.00 V cutoff with a 3 minute limit, on a pack
resting at 54.57 V. Each line gives a host timestamp in seconds, the 19 raw bytes, and
the capture script's decode: `cmd`, `ok` (checksum valid), current `I`, voltage `V`, capacity
`cap`, and the raw values of f4 to f7. Only received frames were logged; the lines starting with
`#` are notes added here, including where commands went out.

```
# Connected and idle. Idle reports (00, CC mode) alternate with firmware reports (64, f5 = 308: firmware 3.08).
# The echo 100 / 900 / 0 is the previous PC command (1.00 A, 9.00 V, no limit); cap 0.083 Ah is left from an earlier panel test.
    0.12 fa 00 00 00 9f 41 00 53 00 00 00 64 03 b4 00 00 1c 42 f8 cmd=00 ok=1 I=0.00 V=54.57 cap=0.083 f4=0 f5=100 f6=900 f7=0
    1.46 fa 64 00 00 9f 41 00 53 00 00 01 44 32 ea 08 e4 1c 84 f8 cmd=64 ok=1 I=0.00 V=54.57 cap=0.083 f4=0 f5=308 f6=12234 f7=2148
    3.43 fa 00 00 00 9f 42 00 53 00 00 00 64 03 b4 00 00 1c 41 f8 cmd=00 ok=1 I=0.00 V=54.58 cap=0.083 f4=0 f5=100 f6=900 f7=0
    5.50 fa 64 00 00 9f 42 00 53 00 00 01 44 32 ea 08 e4 1c 87 f8 cmd=64 ok=1 I=0.00 V=54.58 cap=0.083 f4=0 f5=308 f6=12234 f7=2148
# Start went out before the next frame: fa 01 00 64 14 c8 00 03 ba f8 (CC 1.00 A, cutoff 50.00 V, 3 minutes).
# First running frame (0a): capacity reset to 0, echo now 100 / 5000 / 3, current still 0.00 A.
    6.65 fa 0a 00 00 9f 42 00 00 00 00 00 64 14 c8 00 03 1c 70 f8 cmd=0a ok=1 I=0.00 V=54.58 cap=0.000 f4=0 f5=100 f6=5000 f7=3
# Firmware report, running variant (6e). From here the current reads 1.00 A and the voltage sags under load.
    8.40 fa 6e 00 64 9f 2b 00 00 00 00 01 44 32 ea 08 e4 1c d3 f8 cmd=6e ok=1 I=1.00 V=54.35 cap=0.000 f4=0 f5=308 f6=12234 f7=2148
   10.37 fa 0a 00 64 9f 29 00 01 00 00 00 64 14 c8 00 03 1c 7e f8 cmd=0a ok=1 I=1.00 V=54.33 cap=0.001 f4=0 f5=100 f6=5000 f7=3
   12.43 fa 6e 00 64 9f 28 00 02 00 00 01 44 32 ea 08 e4 1c d2 f8 cmd=6e ok=1 I=1.00 V=54.32 cap=0.002 f4=0 f5=308 f6=12234 f7=2148
   14.39 fa 0a 00 64 9f 27 00 02 00 00 00 64 14 c8 00 03 1c 73 f8 cmd=0a ok=1 I=1.00 V=54.31 cap=0.002 f4=0 f5=100 f6=5000 f7=3
   16.36 fa 6e 00 64 9f 27 00 03 00 00 01 44 32 ea 08 e4 1c dc f8 cmd=6e ok=1 I=1.00 V=54.31 cap=0.003 f4=0 f5=308 f6=12234 f7=2148
# The firmware reports have stopped: every frame from here is a running status frame, one about every 2 s.
   18.43 fa 0a 00 64 9f 26 00 03 00 00 00 64 14 c8 00 03 1c 73 f8 cmd=0a ok=1 I=1.00 V=54.30 cap=0.003 f4=0 f5=100 f6=5000 f7=3
   20.40 fa 0a 00 64 9f 25 00 04 00 00 00 64 14 c8 00 03 1c 77 f8 cmd=0a ok=1 I=1.00 V=54.29 cap=0.004 f4=0 f5=100 f6=5000 f7=3
   22.38 fa 0a 00 64 9f 25 00 04 00 00 00 64 14 c8 00 03 1c 77 f8 cmd=0a ok=1 I=1.00 V=54.29 cap=0.004 f4=0 f5=100 f6=5000 f7=3
   24.45 fa 0a 00 64 9f 24 00 05 00 00 00 64 14 c8 00 03 1c 77 f8 cmd=0a ok=1 I=1.00 V=54.28 cap=0.005 f4=0 f5=100 f6=5000 f7=3
   26.42 fa 0a 00 64 9f 24 00 05 00 00 00 64 14 c8 00 03 1c 77 f8 cmd=0a ok=1 I=1.00 V=54.28 cap=0.005 f4=0 f5=100 f6=5000 f7=3
   28.39 fa 0a 00 64 9f 23 00 06 00 00 00 64 14 c8 00 03 1c 73 f8 cmd=0a ok=1 I=1.00 V=54.27 cap=0.006 f4=0 f5=100 f6=5000 f7=3
   30.46 fa 0a 00 64 9f 23 00 07 00 00 00 64 14 c8 00 03 1c 72 f8 cmd=0a ok=1 I=1.00 V=54.27 cap=0.007 f4=0 f5=100 f6=5000 f7=3
   32.43 fa 0a 00 64 9f 22 00 07 00 00 00 64 14 c8 00 03 1c 73 f8 cmd=0a ok=1 I=1.00 V=54.26 cap=0.007 f4=0 f5=100 f6=5000 f7=3
   34.41 fa 0a 00 64 9f 22 00 08 00 00 00 64 14 c8 00 03 1c 7c f8 cmd=0a ok=1 I=1.00 V=54.26 cap=0.008 f4=0 f5=100 f6=5000 f7=3
   36.37 fa 0a 00 64 9f 22 00 08 00 00 00 64 14 c8 00 03 1c 7c f8 cmd=0a ok=1 I=1.00 V=54.26 cap=0.008 f4=0 f5=100 f6=5000 f7=3
   38.43 fa 0a 00 64 9f 21 00 09 00 00 00 64 14 c8 00 03 1c 7e f8 cmd=0a ok=1 I=1.00 V=54.25 cap=0.009 f4=0 f5=100 f6=5000 f7=3
   40.38 fa 0a 00 64 9f 21 00 09 00 00 00 64 14 c8 00 03 1c 7e f8 cmd=0a ok=1 I=1.00 V=54.25 cap=0.009 f4=0 f5=100 f6=5000 f7=3
   42.46 fa 0a 00 64 9f 20 00 0a 00 00 00 64 14 c8 00 03 1c 7c f8 cmd=0a ok=1 I=1.00 V=54.24 cap=0.010 f4=0 f5=100 f6=5000 f7=3
   44.42 fa 0a 00 64 9f 20 00 0a 00 00 00 64 14 c8 00 03 1c 7c f8 cmd=0a ok=1 I=1.00 V=54.24 cap=0.010 f4=0 f5=100 f6=5000 f7=3
   46.39 fa 0a 00 64 9f 20 00 0b 00 00 00 64 14 c8 00 03 1c 7d f8 cmd=0a ok=1 I=1.00 V=54.24 cap=0.011 f4=0 f5=100 f6=5000 f7=3
   48.35 fa 0a 00 64 9f 1f 00 0c 00 00 00 64 14 c8 00 03 1c 45 f8 cmd=0a ok=1 I=1.00 V=54.23 cap=0.012 f4=0 f5=100 f6=5000 f7=3
   50.42 fa 0a 00 64 9f 1f 00 0c 00 00 00 64 14 c8 00 03 1c 45 f8 cmd=0a ok=1 I=1.00 V=54.23 cap=0.012 f4=0 f5=100 f6=5000 f7=3
   52.38 fa 0a 00 64 9f 1f 00 0d 00 00 00 64 14 c8 00 03 1c 44 f8 cmd=0a ok=1 I=1.00 V=54.23 cap=0.013 f4=0 f5=100 f6=5000 f7=3
   54.44 fa 0a 00 64 9f 1e 00 0d 00 00 00 64 14 c8 00 03 1c 45 f8 cmd=0a ok=1 I=1.00 V=54.22 cap=0.013 f4=0 f5=100 f6=5000 f7=3
   56.41 fa 0a 00 64 9f 1e 00 0e 00 00 00 64 14 c8 00 03 1c 46 f8 cmd=0a ok=1 I=1.00 V=54.22 cap=0.014 f4=0 f5=100 f6=5000 f7=3
   58.37 fa 0a 00 64 9f 1e 00 0e 00 00 00 64 14 c8 00 03 1c 46 f8 cmd=0a ok=1 I=1.00 V=54.22 cap=0.014 f4=0 f5=100 f6=5000 f7=3
   60.43 fa 0a 00 64 9f 1e 00 0f 00 00 00 64 14 c8 00 03 1c 47 f8 cmd=0a ok=1 I=1.00 V=54.22 cap=0.015 f4=0 f5=100 f6=5000 f7=3
   62.39 fa 0a 00 64 9f 1d 00 0f 00 00 00 64 14 c8 00 03 1c 44 f8 cmd=0a ok=1 I=1.00 V=54.21 cap=0.015 f4=0 f5=100 f6=5000 f7=3
   64.36 fa 0a 00 64 9f 1d 00 10 00 00 00 64 14 c8 00 03 1c 5b f8 cmd=0a ok=1 I=1.00 V=54.21 cap=0.016 f4=0 f5=100 f6=5000 f7=3
   66.43 fa 0a 00 64 9f 1d 00 11 00 00 00 64 14 c8 00 03 1c 5a f8 cmd=0a ok=1 I=1.00 V=54.21 cap=0.017 f4=0 f5=100 f6=5000 f7=3
# Around here, a minute into the run, the minute-1 timer sync went out: fa 0a 00 01 00 00 00 00 0b f8 (send times were not logged). Nothing changes.
   68.40 fa 0a 00 64 9f 1c 00 11 00 00 00 64 14 c8 00 03 1c 5b f8 cmd=0a ok=1 I=1.00 V=54.20 cap=0.017 f4=0 f5=100 f6=5000 f7=3
   70.36 fa 0a 00 64 9f 1c 00 12 00 00 00 64 14 c8 00 03 1c 58 f8 cmd=0a ok=1 I=1.00 V=54.20 cap=0.018 f4=0 f5=100 f6=5000 f7=3
   72.44 fa 0a 00 64 9f 1c 00 12 00 00 00 64 14 c8 00 03 1c 58 f8 cmd=0a ok=1 I=1.00 V=54.20 cap=0.018 f4=0 f5=100 f6=5000 f7=3
   74.40 fa 0a 00 64 9f 1c 00 13 00 00 00 64 14 c8 00 03 1c 59 f8 cmd=0a ok=1 I=1.00 V=54.20 cap=0.019 f4=0 f5=100 f6=5000 f7=3
   76.37 fa 0a 00 64 9f 1c 00 13 00 00 00 64 14 c8 00 03 1c 59 f8 cmd=0a ok=1 I=1.00 V=54.20 cap=0.019 f4=0 f5=100 f6=5000 f7=3
   78.43 fa 0a 00 64 9f 1b 00 14 00 00 00 64 14 c8 00 03 1c 59 f8 cmd=0a ok=1 I=1.00 V=54.19 cap=0.020 f4=0 f5=100 f6=5000 f7=3
   80.39 fa 0a 00 64 9f 1b 00 14 00 00 00 64 14 c8 00 03 1c 59 f8 cmd=0a ok=1 I=1.00 V=54.19 cap=0.020 f4=0 f5=100 f6=5000 f7=3
   82.45 fa 0a 00 64 9f 1b 00 15 00 00 00 64 14 c8 00 03 1c 58 f8 cmd=0a ok=1 I=1.00 V=54.19 cap=0.021 f4=0 f5=100 f6=5000 f7=3
   84.41 fa 0a 00 64 9f 1b 00 16 00 00 00 64 14 c8 00 03 1c 5b f8 cmd=0a ok=1 I=1.00 V=54.19 cap=0.022 f4=0 f5=100 f6=5000 f7=3
   86.36 fa 0a 00 64 9f 1b 00 16 00 00 00 64 14 c8 00 03 1c 5b f8 cmd=0a ok=1 I=1.00 V=54.19 cap=0.022 f4=0 f5=100 f6=5000 f7=3
   88.44 fa 0a 00 64 9f 1a 00 17 00 00 00 64 14 c8 00 03 1c 5b f8 cmd=0a ok=1 I=1.00 V=54.18 cap=0.023 f4=0 f5=100 f6=5000 f7=3
   90.40 fa 0a 00 64 9f 1a 00 17 00 00 00 64 14 c8 00 03 1c 5b f8 cmd=0a ok=1 I=1.00 V=54.18 cap=0.023 f4=0 f5=100 f6=5000 f7=3
   92.36 fa 0a 00 64 9f 1a 00 18 00 00 00 64 14 c8 00 03 1c 54 f8 cmd=0a ok=1 I=1.00 V=54.18 cap=0.024 f4=0 f5=100 f6=5000 f7=3
   94.43 fa 0a 00 64 9f 1a 00 18 00 00 00 64 14 c8 00 03 1c 54 f8 cmd=0a ok=1 I=1.00 V=54.18 cap=0.024 f4=0 f5=100 f6=5000 f7=3
   96.38 fa 0a 00 64 9f 19 00 19 00 00 00 64 14 c8 00 03 1c 56 f8 cmd=0a ok=1 I=1.00 V=54.17 cap=0.025 f4=0 f5=100 f6=5000 f7=3
   98.36 fa 0a 00 64 9f 19 00 19 00 00 00 64 14 c8 00 03 1c 56 f8 cmd=0a ok=1 I=1.00 V=54.17 cap=0.025 f4=0 f5=100 f6=5000 f7=3
  100.43 fa 0a 00 64 9f 19 00 1a 00 00 00 64 14 c8 00 03 1c 55 f8 cmd=0a ok=1 I=1.00 V=54.17 cap=0.026 f4=0 f5=100 f6=5000 f7=3
  102.39 fa 0a 00 64 9f 19 00 1b 00 00 00 64 14 c8 00 03 1c 54 f8 cmd=0a ok=1 I=1.00 V=54.17 cap=0.027 f4=0 f5=100 f6=5000 f7=3
  104.45 fa 0a 00 64 9f 19 00 1b 00 00 00 64 14 c8 00 03 1c 54 f8 cmd=0a ok=1 I=1.00 V=54.17 cap=0.027 f4=0 f5=100 f6=5000 f7=3
  106.42 fa 0a 00 64 9f 19 00 1c 00 00 00 64 14 c8 00 03 1c 53 f8 cmd=0a ok=1 I=1.00 V=54.17 cap=0.028 f4=0 f5=100 f6=5000 f7=3
  108.38 fa 0a 00 64 9f 18 00 1c 00 00 00 64 14 c8 00 03 1c 52 f8 cmd=0a ok=1 I=1.00 V=54.16 cap=0.028 f4=0 f5=100 f6=5000 f7=3
  110.45 fa 0a 00 64 9f 18 00 1d 00 00 00 64 14 c8 00 03 1c 53 f8 cmd=0a ok=1 I=1.00 V=54.16 cap=0.029 f4=0 f5=100 f6=5000 f7=3
  112.42 fa 0a 00 64 9f 18 00 1d 00 00 00 64 14 c8 00 03 1c 53 f8 cmd=0a ok=1 I=1.00 V=54.16 cap=0.029 f4=0 f5=100 f6=5000 f7=3
  114.37 fa 0a 00 64 9f 18 00 1e 00 00 00 64 14 c8 00 03 1c 50 f8 cmd=0a ok=1 I=1.00 V=54.16 cap=0.030 f4=0 f5=100 f6=5000 f7=3
  116.43 fa 0a 00 64 9f 18 00 1e 00 00 00 64 14 c8 00 03 1c 50 f8 cmd=0a ok=1 I=1.00 V=54.16 cap=0.030 f4=0 f5=100 f6=5000 f7=3
  118.40 fa 0a 00 64 9f 17 00 1f 00 00 00 64 14 c8 00 03 1c 5e f8 cmd=0a ok=1 I=1.00 V=54.15 cap=0.031 f4=0 f5=100 f6=5000 f7=3
  120.36 fa 0a 00 64 9f 17 00 20 00 00 00 64 14 c8 00 03 1c 61 f8 cmd=0a ok=1 I=1.00 V=54.15 cap=0.032 f4=0 f5=100 f6=5000 f7=3
  122.43 fa 0a 00 64 9f 17 00 20 00 00 00 64 14 c8 00 03 1c 61 f8 cmd=0a ok=1 I=1.00 V=54.15 cap=0.032 f4=0 f5=100 f6=5000 f7=3
  124.42 fa 0a 00 64 9f 17 00 21 00 00 00 64 14 c8 00 03 1c 60 f8 cmd=0a ok=1 I=1.00 V=54.15 cap=0.033 f4=0 f5=100 f6=5000 f7=3
  126.39 fa 0a 00 64 9f 17 00 21 00 00 00 64 14 c8 00 03 1c 60 f8 cmd=0a ok=1 I=1.00 V=54.15 cap=0.033 f4=0 f5=100 f6=5000 f7=3
# Around here, two minutes in, the minute-2 timer sync went out: fa 0a 00 02 00 00 00 00 08 f8. Again nothing changes.
  128.38 fa 0a 00 64 9f 17 00 22 00 00 00 64 14 c8 00 03 1c 63 f8 cmd=0a ok=1 I=1.00 V=54.15 cap=0.034 f4=0 f5=100 f6=5000 f7=3
  130.44 fa 0a 00 64 9f 16 00 22 00 00 00 64 14 c8 00 03 1c 62 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.034 f4=0 f5=100 f6=5000 f7=3
  132.40 fa 0a 00 64 9f 16 00 23 00 00 00 64 14 c8 00 03 1c 63 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.035 f4=0 f5=100 f6=5000 f7=3
  134.36 fa 0a 00 64 9f 16 00 23 00 00 00 64 14 c8 00 03 1c 63 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.035 f4=0 f5=100 f6=5000 f7=3
  136.43 fa 0a 00 64 9f 16 00 24 00 00 00 64 14 c8 00 03 1c 64 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.036 f4=0 f5=100 f6=5000 f7=3
  138.40 fa 0a 00 64 9f 16 00 25 00 00 00 64 14 c8 00 03 1c 65 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.037 f4=0 f5=100 f6=5000 f7=3
  140.37 fa 0a 00 64 9f 16 00 25 00 00 00 64 14 c8 00 03 1c 65 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.037 f4=0 f5=100 f6=5000 f7=3
  142.45 fa 0a 00 64 9f 16 00 26 00 00 00 64 14 c8 00 03 1c 66 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.038 f4=0 f5=100 f6=5000 f7=3
  144.41 fa 0a 00 64 9f 16 00 26 00 00 00 64 14 c8 00 03 1c 66 f8 cmd=0a ok=1 I=1.00 V=54.14 cap=0.038 f4=0 f5=100 f6=5000 f7=3
  146.36 fa 0a 00 64 9f 15 00 27 00 00 00 64 14 c8 00 03 1c 64 f8 cmd=0a ok=1 I=1.00 V=54.13 cap=0.039 f4=0 f5=100 f6=5000 f7=3
  148.42 fa 0a 00 64 9f 15 00 27 00 00 00 64 14 c8 00 03 1c 64 f8 cmd=0a ok=1 I=1.00 V=54.13 cap=0.039 f4=0 f5=100 f6=5000 f7=3
  150.38 fa 0a 00 64 9f 15 00 28 00 00 00 64 14 c8 00 03 1c 6b f8 cmd=0a ok=1 I=1.00 V=54.13 cap=0.040 f4=0 f5=100 f6=5000 f7=3
  152.35 fa 0a 00 64 9f 15 00 28 00 00 00 64 14 c8 00 03 1c 6b f8 cmd=0a ok=1 I=1.00 V=54.13 cap=0.040 f4=0 f5=100 f6=5000 f7=3
  154.42 fa 0a 00 64 9f 15 00 29 00 00 00 64 14 c8 00 03 1c 6a f8 cmd=0a ok=1 I=1.00 V=54.13 cap=0.041 f4=0 f5=100 f6=5000 f7=3
  156.38 fa 0a 00 64 9f 14 00 2a 00 00 00 64 14 c8 00 03 1c 68 f8 cmd=0a ok=1 I=1.00 V=54.12 cap=0.042 f4=0 f5=100 f6=5000 f7=3
  158.44 fa 0a 00 64 9f 14 00 2a 00 00 00 64 14 c8 00 03 1c 68 f8 cmd=0a ok=1 I=1.00 V=54.12 cap=0.042 f4=0 f5=100 f6=5000 f7=3
  160.40 fa 0a 00 64 9f 14 00 2b 00 00 00 64 14 c8 00 03 1c 69 f8 cmd=0a ok=1 I=1.00 V=54.12 cap=0.043 f4=0 f5=100 f6=5000 f7=3
  162.37 fa 0a 00 64 9f 14 00 2b 00 00 00 64 14 c8 00 03 1c 69 f8 cmd=0a ok=1 I=1.00 V=54.12 cap=0.043 f4=0 f5=100 f6=5000 f7=3
  164.44 fa 0a 00 64 9f 14 00 2c 00 00 00 64 14 c8 00 03 1c 6e f8 cmd=0a ok=1 I=1.00 V=54.12 cap=0.044 f4=0 f5=100 f6=5000 f7=3
  166.39 fa 0a 00 64 9f 14 00 2c 00 00 00 64 14 c8 00 03 1c 6e f8 cmd=0a ok=1 I=1.00 V=54.12 cap=0.044 f4=0 f5=100 f6=5000 f7=3
  168.36 fa 0a 00 64 9f 14 00 2d 00 00 00 64 14 c8 00 03 1c 6f f8 cmd=0a ok=1 I=1.00 V=54.12 cap=0.045 f4=0 f5=100 f6=5000 f7=3
  170.42 fa 0a 00 64 9f 13 00 2d 00 00 00 64 14 c8 00 03 1c 68 f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.045 f4=0 f5=100 f6=5000 f7=3
  172.38 fa 0a 00 64 9f 13 00 2e 00 00 00 64 14 c8 00 03 1c 6b f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.046 f4=0 f5=100 f6=5000 f7=3
  174.46 fa 0a 00 64 9f 13 00 2f 00 00 00 64 14 c8 00 03 1c 6a f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.047 f4=0 f5=100 f6=5000 f7=3
  176.42 fa 0a 00 64 9f 13 00 2f 00 00 00 64 14 c8 00 03 1c 6a f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.047 f4=0 f5=100 f6=5000 f7=3
  178.38 fa 0a 00 64 9f 13 00 30 00 00 00 64 14 c8 00 03 1c 75 f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.048 f4=0 f5=100 f6=5000 f7=3
  180.45 fa 0a 00 64 9f 13 00 30 00 00 00 64 14 c8 00 03 1c 75 f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.048 f4=0 f5=100 f6=5000 f7=3
  182.41 fa 0a 00 64 9f 13 00 31 00 00 00 64 14 c8 00 03 1c 74 f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.049 f4=0 f5=100 f6=5000 f7=3
  184.36 fa 0a 00 64 9f 13 00 31 00 00 00 64 14 c8 00 03 1c 74 f8 cmd=0a ok=1 I=1.00 V=54.11 cap=0.049 f4=0 f5=100 f6=5000 f7=3
# Last running frame: 0.050 Ah, exactly 1.00 A for 3 minutes, at 54.10 V.
  186.43 fa 0a 00 64 9f 12 00 32 00 00 00 64 14 c8 00 03 1c 76 f8 cmd=0a ok=1 I=1.00 V=54.10 cap=0.050 f4=0 f5=100 f6=5000 f7=3
# Finished (14), in this one frame only, 179.9 s after the first running frame: the tester enforced the 3 minute limit.
# The idle frame (00) after it has the same timestamp; both still show 1.00 A.
  186.54 fa 14 00 64 9f 12 00 32 00 00 00 64 14 c8 00 03 1c 68 f8 cmd=14 ok=1 I=1.00 V=54.10 cap=0.050 f4=0 f5=100 f6=5000 f7=3
  186.54 fa 00 00 64 9f 12 00 32 00 00 00 64 14 c8 00 03 1c 7c f8 cmd=00 ok=1 I=1.00 V=54.10 cap=0.050 f4=0 f5=100 f6=5000 f7=3
# Idle: 0 A, the voltage rebounding to 54.34 V, the capacity kept at 0.050 Ah.
  188.42 fa 00 00 00 9f 2a 00 32 00 00 00 64 14 c8 00 03 1c 20 f8 cmd=00 ok=1 I=0.00 V=54.34 cap=0.050 f4=0 f5=100 f6=5000 f7=3
  190.39 fa 00 00 00 9f 2b 00 32 00 00 00 64 14 c8 00 03 1c 21 f8 cmd=00 ok=1 I=0.00 V=54.35 cap=0.050 f4=0 f5=100 f6=5000 f7=3
# End of the capture.
```

## References

Used as protocol references only; no code from them is used in ebctool.

- [Kazhuu/ebc-battery-tester](https://github.com/Kazhuu/ebc-battery-tester), in particular its
  [FRAMES.md](https://github.com/Kazhuu/ebc-battery-tester/blob/main/FRAMES.md)
- enkiusz's [zketech-ebc-a20.py gist](https://gist.github.com/enkiusz/6408645efd622b8a638a14957cd37f47)
- [JOGAsoft/EBC-controller](https://github.com/JOGAsoft/EBC-controller) and the
  [ardiehl fork](https://github.com/ardiehl/EBC-controller)
- OpenTraceLab's ZKETECH EBD-USB notes, published at
  `opentracelab.github.io/website/hardware/digital-load/zketech-ebd-usb/`. That page was offline
  on 2026-09-30; its Markdown source is in
  [duyongze/website](https://github.com/duyongze/website/blob/main/docs/hardware/digital-load/zketech-ebd-usb/index.md)
