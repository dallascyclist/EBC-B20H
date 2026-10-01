"""Command line: argument parsing, config and log setup, subcommand dispatch, exit codes (spec section 8).

Exit codes: 0 finished; 1 aborted or unexpected exception; 2 stopped by the user; 3 configuration,
argument or preflight failure with nothing started. User-facing errors go to stdout as "error: ...";
stderr carries only the run log's WARNING-and-above mirror.

The device commands leave the tester disconnected on every path. discharge never sends Start
before preflight has passed and the user has confirmed (or given --yes); once Start is sent the
session owns Stop and Disconnect (spec section 9).
"""
from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import serial

from ebctool import __version__
from ebctool import protocol as P
from ebctool.config import Config, ConfigError, default_config, load_config
from ebctool.console import ConsoleSink, StoreSink, hms, limit_text, status_line, utc_and_local, utc_iso
from ebctool.device import DeviceError, SerialDevice, list_ports, resolve_port
from ebctool.log import RunLog, current, set_current
from ebctool.protocol import Status
from ebctool.report import format_table, history_rows, write_html
from ebctool.session import DischargePlan, DischargeSession, PreflightError
from ebctool.store import CHEMISTRIES, Pack, Store, StoreError, TestRow

EXIT_OK, EXIT_ABORTED, EXIT_USER_STOP, EXIT_CONFIG = 0, 1, 2, 3
EXIT_BY_END_REASON = {"cutoff": EXIT_OK, "max_time": EXIT_OK, "user_stop": EXIT_USER_STOP,
                      "comm_lost": EXIT_ABORTED, "device_error": EXIT_ABORTED, "pack_lost": EXIT_ABORTED}
NO_ANSWER = "no answer from tester: leave the settings screen, stop any panel-started test, and check the port"
MAX_MINUTES = 999
LOW_CUTOFF_V_PER_CELL = 2.5
READ_STEP_S = 1.0   # longest single read in discover and monitor
DEFAULT_CONFIG = Path("config/config.ini")
# Python 3.14 colours argparse help on a terminal; console output carries no colour escapes.
_NO_COLOR = {"color": False} if sys.version_info >= (3, 14) else {}


class SystemClock:
    """Real time for the device commands; tests pass a FakeClock with the same two methods."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def default_device_factory(config: Config, port: Optional[str]):
    """SerialDevice on --port, else [serial] port ('auto' resolved). DeviceError when no port is
    found or the port cannot be opened."""
    path = resolve_port(port or config.serial.port)
    try:
        return SerialDevice(path, parity=config.serial.parity, baud=config.serial.baud,
                            log_frames=config.run.log_frames)
    except (serial.SerialException, OSError) as exc:
        raise DeviceError("cannot open %s: %s" % (path, exc)) from exc


@dataclass(frozen=True)
class RunContext:
    config: Config
    log: RunLog
    stdout: Any
    stdin: Any
    device_factory: Callable
    store_factory: Callable
    clock: Any


class _Parser(argparse.ArgumentParser):
    """Raises ConfigError (exit 3) where argparse would print usage and exit 2."""

    def error(self, message):
        raise ConfigError(message)


def _add_globals(parser: argparse.ArgumentParser, default) -> None:
    parser.add_argument("--config", metavar="PATH", default=default,
                        help="INI file (default: config/config.ini if it exists, else built-in defaults)")
    parser.add_argument("--port", default=default, help="serial port; overrides [serial] port")
    parser.add_argument("--db", metavar="PATH", default=default, help="SQLite database; overrides [run] db_path")


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    """Global options go before or after the subcommand; a value after the subcommand wins."""
    p = _Parser(prog="bin/ebc.sh", description="Drive a ZKETECH EBC-B20H battery tester and keep per-pack test history.",
                **_NO_COLOR)
    _add_globals(p, None)
    p.add_argument("--version", action="store_true", help="print the version and exit")
    # Each subparser repeats the globals with SUPPRESS defaults, so it sets them only when they are
    # given after the subcommand; argparse copies its values over the top level's afterwards.
    common = argparse.ArgumentParser(add_help=False)
    _add_globals(common, argparse.SUPPRESS)

    def add(subparsers, name, summary):
        return subparsers.add_parser(name, help=summary, parents=[common])

    sub = p.add_subparsers(dest="command")

    s = add(sub, "ports", "list serial ports and the one 'auto' picks")
    s.set_defaults(func=cmd_ports)

    s = add(sub, "discover", "connect and print every status frame for a while")
    s.add_argument("--seconds", type=float, default=30.0, help="how long to listen (default 30)")
    s.add_argument("--raw", action="store_true", help="also print each frame in hex")
    s.set_defaults(func=cmd_discover)

    s = add(sub, "monitor", "print decoded status frames until Ctrl-C")
    s.set_defaults(func=cmd_monitor)

    pack = add(sub, "pack", "add, list or show packs")
    pack_sub = pack.add_subparsers(dest="pack_command", metavar="{add,list,show}", required=True)
    s = add(pack_sub, "add", "register a pack")
    s.add_argument("name")
    s.add_argument("--chemistry", required=True, choices=CHEMISTRIES)
    s.add_argument("--cells", type=int, help="cells in series")
    s.add_argument("--rated-ah", type=float, help="rated capacity in Ah")
    s.add_argument("--nominal-v", type=float, help="nominal voltage")
    s.add_argument("--notes", default="")
    s.set_defaults(func=cmd_pack_add)
    s = add(pack_sub, "list", "list packs")
    s.set_defaults(func=cmd_pack_list)
    s = add(pack_sub, "show", "show one pack and its tests")
    s.add_argument("name")
    s.set_defaults(func=cmd_pack_show)

    s = add(sub, "discharge", "run a constant-current discharge test on a pack")
    s.add_argument("--pack", required=True, metavar="NAME")
    s.add_argument("--current", type=float, required=True, metavar="A")
    s.add_argument("--cutoff", type=float, required=True, metavar="V")
    s.add_argument("--max-minutes", type=int, default=0, metavar="N", help="time limit (default 0: none)")
    s.add_argument("--notes", default="")
    s.add_argument("--yes", action="store_true", help="start without asking for confirmation")
    s.set_defaults(func=cmd_discharge)

    s = add(sub, "tests", "list tests, newest first")
    s.add_argument("--pack", metavar="NAME")
    s.set_defaults(func=cmd_tests)

    s = add(sub, "export", "write one test's samples as CSV")
    s.add_argument("--test", type=int, required=True, metavar="ID")
    s.add_argument("--csv", metavar="PATH", help="output file (default: test-ID.csv beside the database)")
    s.set_defaults(func=cmd_export)

    s = add(sub, "history", "per-pack history table, optionally as HTML")
    s.add_argument("--pack", required=True, metavar="NAME")
    s.add_argument("--html", metavar="PATH")
    s.set_defaults(func=cmd_history)

    return p.parse_args(argv)


def _load(args: argparse.Namespace) -> tuple[Config, str]:
    """The config for this run with --db applied, and where it came from (for the log)."""
    if args.config is not None:
        config, source = load_config(args.config), str(args.config)
    elif DEFAULT_CONFIG.is_file():
        config, source = load_config(DEFAULT_CONFIG), str(DEFAULT_CONFIG)
    else:
        config, source = default_config(), "built-in defaults (no %s)" % DEFAULT_CONFIG
    if args.db is not None:
        config = replace(config, run=replace(config.run, db_path=Path(args.db)))
    return config, source


def _open_store(config: Config, store_factory: Callable):
    path = config.run.db_path
    try:
        return store_factory(path)
    except (OSError, sqlite3.Error) as exc:
        raise StoreError("cannot open database %s: %s" % (path, exc)) from exc


def _say(stdout, text: str) -> None:
    """print() for a line whose loss must not change the outcome (R38): the discharge's lines once
    the device is open, and the error lines. An OSError (BrokenPipeError when the reader of a
    `| tee` died with the same Ctrl-C) is logged at INFO and dropped; the exit code is unchanged."""
    try:
        print(text, file=stdout)
    except OSError as exc:
        log = current()
        if log is not None:
            log.info("run.output", "not printed (%s: %s): %s" % (type(exc).__name__, exc, text.strip()))


def _error(stdout, message) -> int:
    _say(stdout, "error: %s" % message)
    return EXIT_CONFIG


# output formatting

def _fmt(value, spec: str) -> str:
    return "-" if value is None else spec % value


def _table(columns: Sequence[tuple[str, str]], rows: Sequence[Sequence[str]]) -> list[str]:
    """Fixed-width lines: the header, then one per row. columns are (header, '<' or '>' alignment)."""
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, (h, _) in enumerate(columns)]
    lines = []
    for cells in [[h for h, _ in columns]] + [list(r) for r in rows]:
        lines.append("  ".join(format(c, a + str(w)) for c, (_, a), w in zip(cells, columns, widths)).rstrip())
    return lines


def _print(ctx: RunContext, lines: Sequence[str]) -> None:
    for line in lines:
        print(line, file=ctx.stdout)


PACK_COLUMNS = (("name", "<"), ("chemistry", "<"), ("cells", ">"), ("nominal V", ">"), ("rated Ah", ">"),
                ("created UTC", "<"), ("created local", "<"))
TEST_COLUMNS = (("id", ">"), ("started UTC", "<"), ("started local", "<"), ("current A", ">"), ("cutoff V", ">"),
                ("end", "<"), ("Ah", ">"), ("Wh", ">"), ("runtime", ">"))


def _tests_table(tests: Sequence[TestRow], pack_names: Optional[dict[int, str]] = None) -> list[str]:
    """TEST_COLUMNS, plus a pack column after id when pack_names (pack id -> name) is given."""
    columns = list(TEST_COLUMNS)
    if pack_names is not None:
        columns.insert(1, ("pack", "<"))
    rows = []
    for t in tests:
        row = [str(t.id), *utc_and_local(t.started_at), "%.2f" % t.set_current_a, "%.2f" % t.cutoff_v,
               t.end_reason or "-", _fmt(t.ah_host, "%.3f"), _fmt(t.wh_host, "%.1f"), hms(t.runtime_s)]
        if pack_names is not None:
            row.insert(1, pack_names[t.pack_id])
        rows.append(row)
    return _table(columns, rows)


# commands: each takes (args, config, ctx) and returns an exit code

def cmd_pack_add(args, config: Config, ctx: RunContext) -> int:
    with closing(_open_store(config, ctx.store_factory)) as store:
        pack = store.add_pack(args.name, args.chemistry, cells_series=args.cells, nominal_v=args.nominal_v,
                              rated_ah=args.rated_ah, notes=args.notes)
    print("added pack %s (id %d)" % (pack.name, pack.id), file=ctx.stdout)
    return EXIT_OK


def cmd_pack_list(args, config: Config, ctx: RunContext) -> int:
    with closing(_open_store(config, ctx.store_factory)) as store:
        packs = store.list_packs()
    _print(ctx, _table(PACK_COLUMNS, [(p.name, p.chemistry, _fmt(p.cells_series, "%d"), _fmt(p.nominal_v, "%.1f"),
                                       _fmt(p.rated_ah, "%.2f"), *utc_and_local(p.created_at)) for p in packs]))
    return EXIT_OK


def cmd_pack_show(args, config: Config, ctx: RunContext) -> int:
    with closing(_open_store(config, ctx.store_factory)) as store:
        pack = store.get_pack(args.name)
        tests = store.list_tests(args.name)
    created_utc, created_local = utc_and_local(pack.created_at)
    fields = (("name", pack.name), ("chemistry", pack.chemistry), ("cells", _fmt(pack.cells_series, "%d")),
              ("nominal V", _fmt(pack.nominal_v, "%.1f")), ("rated Ah", _fmt(pack.rated_ah, "%.2f")),
              ("notes", pack.notes or "-"), ("created UTC", created_utc), ("created local", created_local))
    _print(ctx, ["%-13s %s" % f for f in fields] + [""] + _tests_table(tests))
    return EXIT_OK


def cmd_tests(args, config: Config, ctx: RunContext) -> int:
    with closing(_open_store(config, ctx.store_factory)) as store:
        tests = store.list_tests(args.pack)
        names = {p.id: p.name for p in store.list_packs()}
    _print(ctx, _tests_table(tests, names))
    return EXIT_OK


def cmd_export(args, config: Config, ctx: RunContext) -> int:
    path = Path(args.csv) if args.csv else config.run.db_path.parent / ("test-%d.csv" % args.test)
    with closing(_open_store(config, ctx.store_factory)) as store:
        store.get_test(args.test)   # "no test N" before any directory or file is made
        path.parent.mkdir(parents=True, exist_ok=True)
        count = store.export_csv(args.test, path)
    print("wrote %d %s to %s" % (count, "row" if count == 1 else "rows", path), file=ctx.stdout)
    return EXIT_OK


# device commands

def _close(device, log: RunLog) -> None:
    try:
        device.close()
    except Exception as exc:
        log.warning("run.device", "closing the port failed: %s: %s" % (type(exc).__name__, exc))


def _disconnect_and_close(device, log: RunLog) -> None:
    """For discover and monitor, which send Connect themselves: best-effort Disconnect, then close."""
    try:
        device.send(P.disconnect())
        log.decision("run.device", "sent Disconnect")
    except Exception as exc:
        log.warning("run.device", "Disconnect not sent: %s: %s" % (type(exc).__name__, exc))
    _close(device, log)


def _ask(ctx: RunContext, prompt: str) -> bool:
    """True only for a typed y or Y; end of input counts as no."""
    ctx.stdout.write(prompt)
    ctx.stdout.flush()
    return ctx.stdin.readline().strip() in ("y", "Y")


def cmd_ports(args, config: Config, ctx: RunContext) -> int:
    ports = list_ports()
    for device, description in ports:
        print("%s  %s" % (device, description), file=ctx.stdout)
    if not ports:
        print("no serial ports", file=ctx.stdout)
    setting = args.port or config.serial.port
    try:
        chosen = resolve_port(setting)
    except DeviceError as exc:   # only 'auto' can fail to resolve
        print("%s: %s" % (setting, exc), file=ctx.stdout)
    else:
        if setting == "auto":
            print("auto picks %s" % chosen, file=ctx.stdout)
        else:
            print("port %s (from %s)" % (chosen, "--port" if args.port else "[serial] port"), file=ctx.stdout)
    return EXIT_OK


def cmd_discover(args, config: Config, ctx: RunContext) -> int:
    if not args.seconds > 0:
        raise ConfigError("--seconds must be a positive number, got %g" % args.seconds)
    device = ctx.device_factory(config, args.port)
    frames = 0
    try:
        ctx.log.decision("run.discover", "sending Connect; listening for %g s" % args.seconds)
        device.send(P.connect())
        t0 = ctx.clock.now()
        deadline = t0 + args.seconds
        while True:
            remaining = deadline - ctx.clock.now()
            if remaining <= 0:
                break
            status = device.read(min(remaining, READ_STEP_S))
            if status is not None:
                frames += 1
                print(status_line(status, ctx.clock.now() - t0, raw=args.raw), file=ctx.stdout, flush=True)
        bad = device.bad_frames
    finally:
        _disconnect_and_close(device, ctx.log)
    if frames == 0:
        ctx.log.info("run.discover", "error: %s" % NO_ANSWER)
        print("error: %s" % NO_ANSWER, file=ctx.stdout)
        return EXIT_ABORTED
    print("%d %s in %g s, %d bad" % (frames, "frame" if frames == 1 else "frames", args.seconds, bad),
          file=ctx.stdout)
    return EXIT_OK


def cmd_monitor(args, config: Config, ctx: RunContext) -> int:
    """Print frames until Ctrl-C (exit 2); exit 3 if no frame at all arrives within
    silence_timeout_s of Connect. Once a frame has arrived, silence never ends it."""
    timeout = config.serial.silence_timeout_s
    device = ctx.device_factory(config, args.port)
    answered = True
    try:
        try:
            ctx.log.decision("run.monitor", "sending Connect; printing frames until Ctrl-C")
            device.send(P.connect())
            t0 = ctx.clock.now()
            frames = 0
            while True:
                step = READ_STEP_S
                if frames == 0:
                    remaining = t0 + timeout - ctx.clock.now()
                    if remaining <= 0:
                        ctx.log.decision("run.monitor", "no frame within %.1f s of Connect; no_answer" % timeout)
                        answered = False
                        break
                    step = min(step, remaining)
                status = device.read(step)
                if status is not None:
                    frames += 1
                    print(status_line(status, ctx.clock.now() - t0), file=ctx.stdout, flush=True)
        except KeyboardInterrupt:
            ctx.log.decision("run.monitor", "Ctrl-C; stopping")
    finally:
        _disconnect_and_close(device, ctx.log)
    if not answered:
        ctx.log.info("run.monitor", "error: %s" % NO_ANSWER)
        return _error(ctx.stdout, NO_ANSWER)
    print("\nstopped by Ctrl-C; tester disconnected", file=ctx.stdout)
    return EXIT_USER_STOP


def _check_range(what: str, value, low, high, unit: str = "", source: str = "") -> None:
    """ConfigError naming the value and the allowed range; written so NaN fails too."""
    if not (low <= value <= high):
        raise ConfigError("%s %g%s is outside %g..%g%s%s" % (what, value, unit, low, high, unit, source))


def _check_plan(args, config: Config) -> None:
    lim = config.limits
    _check_range("--current", args.current, lim.min_current_a, lim.max_current_a, " A",
                 " ([limits] min_current_a..max_current_a)")
    _check_range("--cutoff", args.cutoff, lim.min_cutoff_v, lim.max_cutoff_v, " V",
                 " ([limits] min_cutoff_v..max_cutoff_v)")
    _check_range("--max-minutes", args.max_minutes, 0, MAX_MINUTES)


def _low_cutoff_ok(args, pack: Pack, ctx: RunContext) -> bool:
    """Below 2.5 V per cell needs --yes or a typed y. False: the user declined."""
    cells = pack.cells_series
    if not cells or not args.cutoff < cells * LOW_CUTOFF_V_PER_CELL:
        return True
    print("warning: cutoff %.2f V is below %d cells x %.1f V = %.2f V"
          % (args.cutoff, cells, LOW_CUTOFF_V_PER_CELL, cells * LOW_CUTOFF_V_PER_CELL), file=ctx.stdout)
    if args.yes:
        ctx.log.decision("run.discharge", "cutoff below %.1f V per cell accepted by --yes" % LOW_CUTOFF_V_PER_CELL)
        return True
    if _ask(ctx, "Discharge below %.1f V per cell? [y/N] " % LOW_CUTOFF_V_PER_CELL):
        ctx.log.decision("run.discharge", "cutoff below %.1f V per cell confirmed" % LOW_CUTOFF_V_PER_CELL)
        return True
    ctx.log.decision("run.discharge", "cutoff below %.1f V per cell not confirmed; not started" % LOW_CUTOFF_V_PER_CELL)
    print("not started", file=ctx.stdout)
    return False


def _preflight_message(exc: PreflightError, cutoff_v: float) -> str:
    if exc.reason == "no_answer":
        return NO_ANSWER
    if exc.reason == "not_idle":
        return "tester is busy (state %s); stop it from the panel first" % exc.status.state
    return "pack reads %.2f V, cutoff %.2f V needs at least 0.5 V headroom" % (exc.status.volts, cutoff_v)


def _connect_and_confirm(session: DischargeSession, pack: Pack, args, ctx: RunContext) -> Optional[Status]:
    """Connect, preflight, show the reading and ask. Returns the idle frame to start from, or None
    when the user declined or pressed Ctrl-C. Every way out but a returned frame leaves the tester
    disconnected: connect() and preflight() send Disconnect themselves before raising, and
    abandon() covers everything after connect() has returned."""
    plan = session.plan
    connected = False
    try:
        first = session.connect()
        connected = True
        session.preflight(first)
        cells = ", %d cells" % pack.cells_series if pack.cells_series else ""
        firmware = " firmware %s" % session.firmware if session.firmware else ""
        _say(ctx.stdout, "pack %s: %s%s" % (pack.name, pack.chemistry, cells))
        _say(ctx.stdout, "tester %s%s idle; pack reads %.2f V" % (first.model_name, firmware, first.volts))
        _say(ctx.stdout, "plan: CC %.2f A, cutoff %.2f V, %s"
             % (plan.current_a, plan.cutoff_v, limit_text(plan.max_minutes)))
        if args.yes or _ask(ctx, "Start discharge? [y/N] "):
            return first
        session.abandon()
        ctx.log.decision("run.discharge", "start declined at the prompt; not started")
        _say(ctx.stdout, "not started; tester disconnected")
        return None
    except PreflightError as exc:   # the session has sent Disconnect
        raise PreflightError(exc.reason, exc.status, _preflight_message(exc, plan.cutoff_v)) from None
    except KeyboardInterrupt:
        if connected:
            session.abandon()
        ctx.log.decision("run.discharge", "Ctrl-C before Start; not started")
        _say(ctx.stdout, "\nstopped before Start; tester disconnected")
        return None
    except BaseException:
        if connected:
            session.abandon()
        raise


def cmd_discharge(args, config: Config, ctx: RunContext) -> int:
    with closing(_open_store(config, ctx.store_factory)) as store:
        pack = store.get_pack(args.pack)
        _check_plan(args, config)
        if not _low_cutoff_ok(args, pack, ctx):
            return EXIT_USER_STOP
        plan = DischargePlan(current_a=args.current, cutoff_v=args.cutoff, max_minutes=args.max_minutes)
        device = ctx.device_factory(config, args.port)
        try:
            return _discharge(device, plan, pack, store, args, config, ctx)
        finally:
            _close(device, ctx.log)


def _discharge(device, plan: DischargePlan, pack: Pack, store: Store, args, config: Config, ctx: RunContext) -> int:
    stop = {"requested": False}

    def on_sigint(signum, frame):
        stop["requested"] = True

    session = DischargeSession(device, plan, [], ctx.clock, silence_timeout_s=config.serial.silence_timeout_s,
                               stop_requested=lambda: stop["requested"])
    first = _connect_and_confirm(session, pack, args, ctx)
    if first is None:
        return EXIT_USER_STOP
    try:   # nothing started yet: any failure here only needs Disconnect
        test_id = store.begin_test(pack.id, utc_iso(), plan.current_a, plan.cutoff_v, plan.max_minutes,
                                   ctx.log.run_id, args.notes)
        session.sinks = [ConsoleSink(ctx.stdout, ctx.clock), StoreSink(store, test_id)]
        previous = signal.signal(signal.SIGINT, on_sigint)
    except BaseException:
        session.abandon()
        raise
    try:   # from here the session sends Stop then Disconnect on every exit
        result = session.run_from(first)
    finally:
        signal.signal(signal.SIGINT, previous)
    _say(ctx.stdout, "saved as test %d" % test_id)
    if result.end_reason == "comm_lost":
        _say(ctx.stdout, "lost contact with the tester: check its display and stop it from the panel"
                         " if it is still running")
    return EXIT_BY_END_REASON[result.end_reason]


def cmd_history(args, config: Config, ctx: RunContext) -> int:
    """The pack's finished tests as a table; with --html also the HTML page (its directory created)."""
    with closing(_open_store(config, ctx.store_factory)) as store:
        rows = history_rows(store, args.pack)
        print(format_table(rows), file=ctx.stdout)
        if args.html:
            path = Path(args.html)
            write_html(store, args.pack, path)
            print("wrote %s" % path, file=ctx.stdout)
    return EXIT_OK


def _run(args: argparse.Namespace, argv: list[str], source: str, ctx: RunContext) -> int:
    log = ctx.log
    try:
        log.info("run.main", "ebctool %s starting: %s" % (__version__, argv))
        log.decision("run.main", "config from %s; database %s%s"
                     % (source, ctx.config.run.db_path, " (--db)" if args.db is not None else ""))
        code = args.func(args, ctx.config, ctx)
    except (ConfigError, StoreError, DeviceError, PreflightError) as exc:
        # Nothing was started on any of these. INFO, not ERROR: the message is already on stdout,
        # and ERROR would repeat it on stderr.
        log.info("run.main", "error: %s" % exc)
        code = _error(ctx.stdout, exc)
    except KeyboardInterrupt:
        log.decision("run.main", "interrupted by Ctrl-C")
        code = EXIT_USER_STOP
    except Exception as exc:
        log.exception("run.main", exc)
        _say(ctx.stdout, "error: unexpected %s: %s (details in %s)" % (type(exc).__name__, exc, log.path))
        code = EXIT_ABORTED
    log.info("run.main", "exit %d" % code)
    return code


def main(argv: Optional[Sequence[str]] = None, stdout=None, stdin=None, device_factory=None, store_factory=None,
         clock=None) -> int:
    """Run one command and return its exit code. stdout and stdin default to sys.stdout and sys.stdin."""
    stdout = sys.stdout if stdout is None else stdout
    stdin = sys.stdin if stdin is None else stdin
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse_args(argv)
    except ConfigError as exc:
        return _error(stdout, exc)
    if args.version:
        print("ebctool %s" % __version__, file=stdout)
        return EXIT_OK
    if args.command is None:
        return _error(stdout, "no command given; bin/ebc.sh --help lists them")
    try:
        config, source = _load(args)
    except ConfigError as exc:
        return _error(stdout, exc)
    try:
        log = RunLog(config.run.log_dir)
    except OSError as exc:
        return _error(stdout, "cannot open a run log in %s: %s" % (config.run.log_dir, exc))

    set_current(log)
    ctx = RunContext(config=config, log=log, stdout=stdout, stdin=stdin,
                     device_factory=device_factory or default_device_factory,
                     store_factory=store_factory or Store, clock=clock or SystemClock())
    try:
        return _run(args, argv, source, ctx)
    finally:
        log.close()
        set_current(None)
