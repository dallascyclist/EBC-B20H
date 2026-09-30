"""Command line: argument parsing, config and log setup, subcommand dispatch, exit codes (spec section 8).

Exit codes: 0 finished; 1 aborted or unexpected exception; 2 stopped by the user; 3 configuration,
argument or preflight failure with nothing started. User-facing errors go to stdout as "error: ...";
stderr carries only the run log's WARNING-and-above mirror.
"""
from __future__ import annotations

import argparse
import sys
import time
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ebctool import __version__
from ebctool.config import Config, ConfigError, default_config, load_config
from ebctool.device import SerialDevice, resolve_port
from ebctool.log import RunLog, set_current
from ebctool.store import CHEMISTRIES, Store, StoreError, TestRow

EXIT_OK, EXIT_ABORTED, EXIT_USER_STOP, EXIT_CONFIG = 0, 1, 2, 3
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
    return SerialDevice(resolve_port(port or config.serial.port), parity=config.serial.parity,
                        baud=config.serial.baud, log_frames=config.run.log_frames)


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


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = _Parser(prog="bin/ebc.sh", description="Drive a ZKETECH EBC-B20H battery tester and keep per-pack test history.",
                **_NO_COLOR)
    p.add_argument("--config", metavar="PATH",
                   help="INI file (default: config/config.ini if it exists, else built-in defaults)")
    p.add_argument("--port", help="serial port; overrides [serial] port")
    p.add_argument("--db", metavar="PATH", help="SQLite database; overrides [run] db_path")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("ports", help="list serial ports and the one 'auto' picks")
    s.set_defaults(func=cmd_ports)

    s = sub.add_parser("discover", help="connect and print every status frame for a while")
    s.add_argument("--seconds", type=float, default=30.0, help="how long to listen (default 30)")
    s.add_argument("--raw", action="store_true", help="also print each frame in hex")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("monitor", help="print decoded status frames until Ctrl-C")
    s.set_defaults(func=cmd_monitor)

    pack = sub.add_parser("pack", help="add, list or show packs")
    pack_sub = pack.add_subparsers(dest="pack_command", metavar="{add,list,show}", required=True)
    s = pack_sub.add_parser("add", help="register a pack")
    s.add_argument("name")
    s.add_argument("--chemistry", required=True, choices=CHEMISTRIES)
    s.add_argument("--cells", type=int, help="cells in series")
    s.add_argument("--rated-ah", type=float, help="rated capacity in Ah")
    s.add_argument("--nominal-v", type=float, help="nominal voltage")
    s.add_argument("--notes", default="")
    s.set_defaults(func=cmd_pack_add)
    s = pack_sub.add_parser("list", help="list packs")
    s.set_defaults(func=cmd_pack_list)
    s = pack_sub.add_parser("show", help="show one pack and its tests")
    s.add_argument("name")
    s.set_defaults(func=cmd_pack_show)

    s = sub.add_parser("discharge", help="run a constant-current discharge test on a pack")
    s.add_argument("--pack", required=True, metavar="NAME")
    s.add_argument("--current", type=float, required=True, metavar="A")
    s.add_argument("--cutoff", type=float, required=True, metavar="V")
    s.add_argument("--max-minutes", type=int, default=0, metavar="N", help="time limit (default 0: none)")
    s.add_argument("--notes", default="")
    s.add_argument("--yes", action="store_true", help="start without asking for confirmation")
    s.set_defaults(func=cmd_discharge)

    s = sub.add_parser("tests", help="list tests, newest first")
    s.add_argument("--pack", metavar="NAME")
    s.set_defaults(func=cmd_tests)

    s = sub.add_parser("export", help="write one test's samples as CSV")
    s.add_argument("--test", type=int, required=True, metavar="ID")
    s.add_argument("--csv", metavar="PATH", help="output file (default: test-ID.csv beside the database)")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("history", help="per-pack history table, optionally as HTML")
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
    return store_factory(config.run.db_path)


def _error(stdout, message) -> int:
    print("error: %s" % message, file=stdout)
    return EXIT_CONFIG


# output formatting

def _fmt(value, spec: str) -> str:
    return "-" if value is None else spec % value


def _hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


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
                ("created", "<"))
TEST_COLUMNS = (("id", ">"), ("started", "<"), ("current A", ">"), ("cutoff V", ">"), ("end", "<"), ("Ah", ">"),
                ("Wh", ">"), ("runtime", ">"))


def _tests_table(tests: Sequence[TestRow], pack_names: Optional[dict[int, str]] = None) -> list[str]:
    """TEST_COLUMNS, plus a pack column after id when pack_names (pack id -> name) is given."""
    columns = list(TEST_COLUMNS)
    if pack_names is not None:
        columns.insert(1, ("pack", "<"))
    rows = []
    for t in tests:
        row = [str(t.id), t.started_at, "%.2f" % t.set_current_a, "%.2f" % t.cutoff_v, t.end_reason or "-",
               _fmt(t.ah_host, "%.3f"), _fmt(t.wh_host, "%.1f"), _hms(t.runtime_s)]
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
                                       _fmt(p.rated_ah, "%.2f"), p.created_at) for p in packs]))
    return EXIT_OK


def cmd_pack_show(args, config: Config, ctx: RunContext) -> int:
    with closing(_open_store(config, ctx.store_factory)) as store:
        pack = store.get_pack(args.name)
        tests = store.list_tests(args.name)
    fields = (("name", pack.name), ("chemistry", pack.chemistry), ("cells", _fmt(pack.cells_series, "%d")),
              ("nominal V", _fmt(pack.nominal_v, "%.1f")), ("rated Ah", _fmt(pack.rated_ah, "%.2f")),
              ("notes", pack.notes or "-"), ("created", pack.created_at))
    _print(ctx, ["%-10s %s" % f for f in fields] + [""] + _tests_table(tests))
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


def cmd_ports(args, config: Config, ctx: RunContext) -> int:
    raise NotImplementedError("Task 9")


def cmd_discover(args, config: Config, ctx: RunContext) -> int:
    raise NotImplementedError("Task 9")


def cmd_monitor(args, config: Config, ctx: RunContext) -> int:
    raise NotImplementedError("Task 9")


def cmd_discharge(args, config: Config, ctx: RunContext) -> int:
    raise NotImplementedError("Task 9")


def cmd_history(args, config: Config, ctx: RunContext) -> int:
    raise NotImplementedError("Task 10")


def _run(args: argparse.Namespace, argv: list[str], source: str, ctx: RunContext) -> int:
    log = ctx.log
    try:
        log.info("run.main", "ebctool %s starting: %s" % (__version__, argv))
        log.decision("run.main", "config from %s; database %s%s"
                     % (source, ctx.config.run.db_path, " (--db)" if args.db is not None else ""))
        code = args.func(args, ctx.config, ctx)
    except (ConfigError, StoreError) as exc:
        # INFO, not ERROR: the message is already on stdout, and ERROR would repeat it on stderr.
        log.info("run.main", "error: %s" % exc)
        code = _error(ctx.stdout, exc)
    except KeyboardInterrupt:
        log.decision("run.main", "interrupted by Ctrl-C")
        code = EXIT_USER_STOP
    except Exception as exc:
        log.exception("run.main", exc)
        print("error: unexpected %s: %s (details in %s)" % (type(exc).__name__, exc, log.path), file=ctx.stdout)
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
