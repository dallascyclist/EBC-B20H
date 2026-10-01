# ebctool

ebctool drives a ZKETECH EBC-B20H battery capacity tester from a Mac or a Linux computer to run
constant-current discharge tests on 48 V and 52 V e-bike packs. You choose a current and a cutoff
voltage; the tester holds that current until the pack reaches the cutoff (or an optional time
limit), and ebctool records voltage and current from each status frame the tester sends during
the discharge, then reports the runtime and the delivered Ah and Wh.

Every test is stored in a local SQLite database against a named pack, so the same pack can be
tested again months later and the results compared: a history table per pack, and a
self-contained HTML page with the voltage curves overlaid and delivered Ah by date. It is a small
Python program with nothing to install: the standard library plus a vendored copy of pyserial.

- Wire protocol as observed on the B20H: `docs/PROTOCOL.md`
- Design: `docs/superpowers/specs/2026-09-30-ebc-b20h-design.md`
- Implementation plan: `docs/superpowers/plans/2026-09-30-ebctool-v1.md`

## Quick reference

    bin/ebc.sh ports                                   # serial ports, and the one port = auto picks
    bin/ebc.sh discover [--seconds 30] [--raw]         # connect and print every frame for a while
    bin/ebc.sh monitor                                 # live voltage, current and state until Ctrl-C; starts nothing
    bin/ebc.sh pack add NAME --chemistry li-ion [--cells 14] [--rated-ah 17.5] [--nominal-v 52] [--notes NOTES]
    bin/ebc.sh pack list
    bin/ebc.sh pack show NAME                          # one pack and its tests
    bin/ebc.sh discharge --pack NAME --current 5.0 --cutoff 42.0 [--max-minutes 600] [--notes NOTES] [--yes]
    bin/ebc.sh tests [--pack NAME]                     # tests, newest first
    bin/ebc.sh export --test ID [--csv PATH]           # one test's samples as CSV
    bin/ebc.sh history --pack NAME [--html PATH]       # per-pack history table, optionally an HTML page
    bin/ebc.sh --version
    bin/test.sh                                        # unit tests; no tester needed

Every command also takes `--config PATH`, `--port PORT` and `--db PATH`, before or after the
command name. `bin/ebc.sh --help` and `bin/ebc.sh COMMAND --help` list the options.

## Requirements

- Python 3.12 or newer. Nothing is pip-installed: pyserial 3.5 is vendored under
  `python/lib/serial` (version, source and checksum in `python/lib/VENDOR.md`); everything else is
  the standard library.
- macOS or Linux. The launchers are POSIX `sh` scripts; Windows has no launcher and is untested.
- An EBC-B20H and the tester's own USB cable. The cable is a CH340 USB-serial adapter (USB ID
  1A86:7523).
- On macOS, the WCH CH34x driver:

      brew install --cask wch-ch34x-usb-serial-driver

  then approve it in System Settings → General → Login Items & Extensions → Driver Extensions.
  It names the port `/dev/cu.wchusbserial` followed by a number taken from the USB port the cable
  is in, so the name changes if you move the cable; `port = auto` finds it either way.
- On macOS, plug the tester into a port directly on the Mac, not through a dock or a hub chain.
  Four hubs deep in a dock chain, the driver's USB control transfers were dropped at random and
  configuring the port failed with "Invalid argument" about three times in four, under Apple's
  driver and WCH's alike. On a direct port it works first time.
- On Linux the kernel's `ch341` driver handles the cable and the port is `/dev/ttyUSB0` or
  similar. `port = auto` only searches the macOS device names, so set `port` in the config (or
  pass `--port`). The unit tests run on Linux, but the tool has only been used with the tester
  on macOS.

## Setup

    cp config/config.example.ini config/config.ini    # optional: without it the built-in defaults apply
    bin/test.sh                                        # unit tests, no tester needed
    bin/ebc.sh ports                                   # is the tester's port there?
    bin/ebc.sh discover --seconds 10                   # does the tester answer?

For `discover`, leave the tester on its testing screen (not the settings screen) with no
front-panel test running; it sends Connect, prints what comes back, and sends Disconnect.

Both launchers use `venv/bin/python` when it exists, otherwise `python3`. `bin/ebc.sh` puts
`/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin` and `/bin` ahead of your `PATH`, in that order,
so a Homebrew Python 3.12 or newer is found before Apple's `/usr/bin/python3` (3.9). If the
`python3` it finds is older, it stops with "ebctool requires Python 3.12 or newer". A venv makes
the choice explicit; install nothing into it:

    /opt/homebrew/bin/python3 -m venv venv             # any Python 3.12 or newer

`bin/ebc.sh` changes to the repository root before it starts, so every relative path, in the
config and on the command line (`--config`, `--db`, `--csv`, `--html`), is relative to the
repository root, not to the directory you ran it from.

## Configuration

`config/config.ini` is ignored by git. Every section and key is optional: a key that is absent
takes its default, and a value may carry a trailing `; comment`. With no
`config/config.ini` and no `--config`, the defaults below apply. A bad value stops the command
with exit 3 before anything is touched.

| Key | Default | Meaning |
|---|---|---|
| `[serial] port` | `auto` | `auto` picks the first `/dev/cu.wchusbserial*` (WCH driver), else the first `/dev/cu.usbserial*` (Apple's driver). Anything else is used as the device path, for example `/dev/ttyUSB0`. `--port` overrides it |
| `[serial] parity` | `E` | `E` or `O`. The B20H answers either, never no parity |
| `[serial] baud` | `9600` | Line speed |
| `[serial] silence_timeout_s` | `10` | Seconds to wait for the tester's first idle frame after Connect, and the longest a running test may go without a valid frame before it ends as `comm_lost` |
| `[run] log_dir` | `logs` | Where the per-run log files go |
| `[run] db_path` | `db/ebctool.db` | The SQLite database, created on first use. `--db` overrides it |
| `[run] log_frames` | `false` | `true` also writes every received frame to the run log in hex, at DEBUG. Sent frames are logged either way |
| `[limits] min_current_a` | `0.5` | Lowest `--current` accepted, in A |
| `[limits] max_current_a` | `20.0` | Highest `--current` accepted, in A |
| `[limits] min_cutoff_v` | `8.0` | Lowest `--cutoff` accepted, in V |
| `[limits] max_cutoff_v` | `88.0` | Highest `--cutoff` accepted, in V |

The limits default to the B20H's published ranges: current up to 20.00 A with a 0.50 A minimum on
2023 firmware, cutoff 8.00 to 88.00 V. Each minimum must not exceed its maximum.

## Commands

| Command | Option | Default | Meaning |
|---|---|---|---|
| every command | `--config PATH` | `config/config.ini` if it exists, else built-in defaults | The INI file to read |
| every command | `--port PORT` | `[serial] port` | Serial port for this run |
| every command | `--db PATH` | `[run] db_path` | Database for this run |
| `discover` | `--seconds SECONDS` | `30` | How long to listen |
| `discover` | `--raw` | off | Also print each frame in hex |
| `pack add` | `NAME` | required | The pack's name; must be unique |
| `pack add` | `--chemistry` | required | `li-ion`, `lifepo4` or `other` |
| `pack add` | `--cells CELLS` | none | Cells in series. Enables the low-cutoff check |
| `pack add` | `--rated-ah RATED_AH` | none | Rated capacity in Ah. Enables "% rated" in the history |
| `pack add` | `--nominal-v NOMINAL_V` | none | Nominal voltage, shown by `pack list` and `pack show` |
| `pack add` | `--notes NOTES` | empty | Free text |
| `discharge` | `--pack NAME` | required | A pack added with `pack add` |
| `discharge` | `--current A` | required | Discharge current in A |
| `discharge` | `--cutoff V` | required | Cutoff voltage in V |
| `discharge` | `--max-minutes N` | `0` (none) | Time limit, 0 to 999 minutes, enforced by the tester |
| `discharge` | `--notes NOTES` | empty | Stored with the test |
| `discharge` | `--yes` | off | Start without the confirmation prompt, and accept a cutoff below 2.5 V per cell |
| `tests` | `--pack NAME` | all packs | Only this pack's tests |
| `export` | `--test ID` | required | A test id from `tests` |
| `export` | `--csv PATH` | `test-ID.csv` beside the database | Where to write |
| `history` | `--pack NAME` | required | The pack to report on |
| `history` | `--html PATH` | none | Also write the HTML page here |

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | The device ended the test (cutoff or max time) |
| 1 | Aborted after Start (communication lost, device error, pack lost (`pack_lost`), unexpected failure); partial data saved |
| 2 | Stopped by the user (Ctrl-C or declining the prompt); partial data saved if the test had started |
| 3 | Nothing started: configuration, argument, database, serial-port, range or preflight failure |

The table is written for `discharge`; the other commands use the same codes. `pack`, `tests`,
`export` and `history` exit 0 or 3 (1 on an unexpected failure). `discover` exits 1 if the tester
sent nothing. `monitor` runs until Ctrl-C, so it exits 2, or 3 with `error: no answer from tester`
if nothing arrives within `silence_timeout_s` of Connect. `ports` exits 0. Errors are
printed as `error: ...` on stdout; stderr carries only the run log's warnings and errors (and the
Python version check, which runs first).

## How a test runs

The tester's firmware is the safety authority. ebctool never sends Start until its checks have
passed and you have confirmed, and it relies on the tester to end the discharge at the cutoff or
the time limit. It does not watch the pack's temperature, so stay with a pack under test.

`bin/ebc.sh discharge --pack NAME --current A --cutoff V` goes through these steps, and the run log
records each decision as a `DECISION` line.

1. **Checks before the port is opened.** The pack must exist, `--current` and `--cutoff` must lie
   within the `[limits]` ranges, and `--max-minutes` within 0 to 999. If the pack has a cell count
   and the cutoff is below 2.5 V per cell (35.0 V for 14 cells), ebctool warns and asks
   `Discharge below 2.5 V per cell? [y/N]` unless `--yes` was given. A failed check exits 3;
   declining exits 2.
2. **Connect.** ebctool opens the port, sends Connect and waits up to `silence_timeout_s` (10 s)
   for an idle status frame, resending Connect every 5 s. If nothing comes back it stops with
   `no answer from tester: leave the settings screen, stop any panel-started test, and check the
   port`: the B20H ignores Connect while its settings screen is open and while a test started from
   its front panel is running. If the tester answers but is not idle, ebctool stops with
   `tester is busy`. Both exit 3.
3. **Preflight.** The pack must read at least 0.5 V above the cutoff, or the command exits 3.
4. **Confirm.** ebctool prints the pack, the tester's model and firmware, the pack voltage and the
   plan, and asks `Start discharge? [y/N]` unless `--yes` was given. Any answer other than `y` or
   `Y` sends Disconnect and exits 2 with no test recorded; so does Ctrl-C at this point.
5. **Start.** The test is recorded with its start time in UTC, then Start goes to the tester with
   the current, the cutoff and the time limit. The tester must report a running discharge within
   10 s; if it does not (typically no pack, or a voltage already below the cutoff), the test ends
   as `device_error` and the command exits 1.
6. **Running.** Elapsed time counts from the first running frame. Every running status frame, one
   about every 2 s, becomes a sample: voltage, current, host Ah, host Wh and the tester's own Ah.
   Each sample is written to the database as it arrives, so a crash keeps everything up to that
   point. Host Ah and Wh are trapezoid integrals of current and of voltage times current over the
   host clock; the tester's own Ah count is stored beside them for comparison. A live line shows
   elapsed time, voltage, current, Ah and Wh. Once a minute ebctool sends a timer sync, as the
   vendor's software does. A running frame below 1.00 V means the pack is no longer on the leads:
   ebctool ends the test at once as `pack_lost` (exit 1) without waiting for the tester.
7. **End.** The tester ends the discharge itself, at the cutoff or at the time limit. ebctool sees
   either the tester's single "finished" frame or the switch from running back to idle, and names
   the end reason from the lower of that frame's voltage and the last running voltage:
   `pack_lost` (exit 1) if the ending frame reads below 1.00 V, or if that voltage is more than
   2.00 V or 10% of the cutoff (whichever is larger) below the cutoff; then `cutoff` if it is at
   most 0.05 V above the cutoff, `max_time` if a time limit was set and the run lasted at least
   that long less 5 s, otherwise `device_error`. An unplugged pack and an e-bike pack whose BMS
   cut off on low voltage look the same to the tester: the voltage collapses and the tester ends
   the run as if at the cutoff. `pack_lost` keeps that from being recorded as a capacity result.
   There is no host-side time backstop: the tester enforces `--max-minutes`; ebctool records it
   but does not stop the run itself.
8. **Ctrl-C and lost contact.** Ctrl-C during the run ends the test as `user_stop` (exit 2). If no
   valid frame arrives for `silence_timeout_s`, or reading the port fails, the test ends as
   `comm_lost` (exit 1); ebctool still sends Stop, but the tester may not hear it, so check its
   display and stop it from the panel if it is still running. An unexpected error ends the test as
   `device_error` (exit 1) with the traceback in the run log. The samples so far are kept in every
   case.
9. **Always.** However the run ends, ebctool sends Stop then Disconnect and prints a summary: end
   reason, runtime, host Ah, tester Ah, host Wh, start and end voltage, and for `pack_lost` the line
   `pack voltage collapsed to X V: pack disconnected or its BMS cut off; this test is not a capacity
   result`, which also goes to the run log as a `DECISION` line. Then it prints
   `saved as test N`, or after an unexpected error the error and the path of the run log.

## History and reports

- `tests [--pack NAME]` lists tests newest first: id, pack, start time in UTC and in local time,
  current, cutoff, end reason, Ah, Wh and runtime. A test whose process was killed before it
  finished shows `-` as its end reason.
- `pack show NAME` prints the pack's details and its tests.
- `export --test ID [--csv PATH]` writes the test's samples as CSV with the columns
  `t_s, volts, amps, ah_host, wh_host, ah_device, state`, where `t_s` is seconds since the first
  running frame. By default the file is `test-ID.csv` beside the database (`db/test-ID.csv`).
- `history --pack NAME` prints one line per finished test, oldest first: id, start time in UTC and
  in local time, current, cutoff, Ah, % of rated, tester Ah, Wh, runtime, start and end voltage,
  end reason. With `--html PATH` it also writes one self-contained HTML file: voltage under load
  against elapsed minutes with every test overlaid (the newest eight in colour and labelled by
  date, older ones in gray), delivered Ah by date, and the same table. The page has no script and
  loads nothing external; it follows the browser's light or dark setting. A `pack_lost` test is
  listed like any other and its curve is drawn, but the HTML table marks its end column
  `pack_lost: not a capacity result` and the delivered-Ah chart leaves it out, with the note
  `pack_lost tests are not plotted here`, so a BMS trip does not look like capacity fade.

Start times are stored in UTC. The `tests`, `pack show` and `history` tables and the HTML table
show each one twice, in columns `started UTC` (`2026-10-01 02:42Z`) and `started local`
(`2026-09-30 21:42 CDT`). Local time is the environment's: the `TZ` variable if it is set,
otherwise the system time zone, so `TZ=UTC bin/ebc.sh history --pack NAME` shows UTC in both
columns. The charts label and place each test by its local calendar day, so that test is dated
2026-09-30; the chart tooltips and the latest-value label give both forms. `pack list` and
`pack show` give the time each pack was added the same way, as `created UTC` and `created local`.

Ah and "% rated" use the host-integrated Ah. The tester's own count is shown beside it, but its
encoding above 10 Ah has not yet been verified on the B20H. "% rated" needs `--rated-ah` on the
pack. Tests that never finished are left out of the history.

## When something goes wrong

| You see | What happened | What to do |
|---|---|---|
| `error: no serial port found; is the tester on a port directly on the Mac?` | `port = auto` found neither a `/dev/cu.wchusbserial*` nor a `/dev/cu.usbserial*` node | Check the cable and the driver approval; `bin/ebc.sh ports` lists what the system sees. On Linux, set `port` |
| `error: cannot open ...` with `Invalid argument` | The port could not be configured. On macOS this is the dock or hub-chain problem | Move the tester to a port directly on the Mac |
| `error: cannot open ...` with `Permission denied` | Your account may not open the device node | On Linux, `ls -l /dev/ttyUSB0` shows the group that owns it; add yourself to that group and log in again |
| `error: no answer from tester: ...` | Nothing came back after Connect | Leave the settings screen, stop any panel-started test, and check that `port` names the tester |
| `error: tester is busy (state ...); stop it from the panel first` | The tester was not idle when ebctool connected | Stop it from the front panel and try again |
| `error: pack reads ... V, cutoff ... V needs at least 0.5 V headroom` | The pack is already close to or below the cutoff | Charge the pack, or choose a lower cutoff |
| `error: --current ... is outside ...` (or `--cutoff`, `--max-minutes`) | The value is outside the `[limits]` range or 0 to 999 minutes | Choose a value inside the range |
| `lost contact with the tester: check its display and stop it from the panel if it is still running` | No valid frame for `silence_timeout_s` during the run | Look at the tester. The samples so far are saved; `grep DECISION` the run log for the sequence |
| `pack voltage collapsed to ... V: pack disconnected or its BMS cut off; this test is not a capacity result` (end reason `pack_lost`, exit 1) | The pack voltage fell below 1.00 V, or far below the cutoff: a lead came off, or the pack's BMS cut the output (an e-bike BMS does this on low voltage, on a weak cell group, or on a current it will not supply) | Check the leads and the pack's own switch or fuse. If the BMS tripped above the cutoff, raise the cutoff or lower the current and test again. The test stays in the history but out of the delivered-Ah chart |
| `ebctool requires Python 3.12 or newer; found ...` | The launcher found an older `python3` | Create `venv` from a Python 3.12 or newer (see Setup) |

## Where things go

| Path | Contents |
|---|---|
| `config/config.ini` | Your settings. Ignored by git |
| `db/ebctool.db` | Packs, tests and samples (SQLite). Ignored by git |
| `logs/YYYYMMDDhhmm.log` | One log per command run, named by its UTC start minute (a `-PID` suffix if two start in the same minute). Lines carry a UTC millisecond timestamp, the PID, the level and the function. Each test records the name of its log. Ignored by git |
| `tests/fixtures/frames/` | Frames captured from the B20H, used by the unit tests |

## Protocol

`docs/PROTOCOL.md` describes the wire protocol as observed on one EBC-B20H (model byte `0x1C`,
firmware 3.08): the link settings, framing, base-240 fields, the checksum quirk, the commands, the
status frame and its range-encoded fields, the state bytes, the tester's behaviours, and an
annotated capture of a full 3 minute discharge.

## Credits

The EBC protocol was worked out by others for the EBC-A20 family. These were used as protocol
references only; no code from them is used here.

- [Kazhuu/ebc-battery-tester](https://github.com/Kazhuu/ebc-battery-tester), in particular its
  [FRAMES.md](https://github.com/Kazhuu/ebc-battery-tester/blob/main/FRAMES.md)
- enkiusz's [zketech-ebc-a20.py gist](https://gist.github.com/enkiusz/6408645efd622b8a638a14957cd37f47)
- [JOGAsoft/EBC-controller](https://github.com/JOGAsoft/EBC-controller) and the
  [ardiehl fork](https://github.com/ardiehl/EBC-controller)
- OpenTraceLab's ZKETECH EBD-USB notes, published at
  `opentracelab.github.io/website/hardware/digital-load/zketech-ebd-usb/`. That page was offline
  on 2026-09-30; its Markdown source is in
  [duyongze/website](https://github.com/duyongze/website/blob/main/docs/hardware/digital-load/zketech-ebd-usb/index.md)

pyserial 3.5 by Chris Liechti is vendored under its BSD-3 licence.

## Licence

MIT. Copyright (c) 2026 Doug Davis; see `LICENSE`. The vendored pyserial keeps its own BSD-3
licence in `python/lib/serial/LICENSE.txt`.
