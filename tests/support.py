"""Builders shared by the test modules: config, log, store, and frame helpers."""
import io
from datetime import datetime, timezone
from pathlib import Path

from ebctool.config import Config, Limits, RunConfig, SerialConfig
from ebctool.log import RunLog, current, set_current

STARTED = datetime(2026, 9, 30, 20, 0, 0, tzinfo=timezone.utc)


def write_config(tmpdir, text):
    """Write text to <tmpdir>/config.ini and return that path."""
    path = Path(tmpdir) / "config.ini"
    path.write_text(text, encoding="utf-8")
    return path


def make_config(tmpdir, **overrides):
    tmp = Path(tmpdir)
    serial = SerialConfig(port=overrides.get("port", "/dev/fake"), parity=overrides.get("parity", "E"),
                          silence_timeout_s=overrides.get("silence_timeout_s", 10.0))
    run = RunConfig(log_dir=tmp / "logs", db_path=tmp / "db" / "t.db", log_frames=overrides.get("log_frames", False))
    return Config(path=tmp / "config.ini", serial=serial, run=run, limits=Limits())


def close_log():
    log = current()
    if log is not None:
        log.close()
    set_current(None)


def make_log(tmpdir):
    close_log()
    log = RunLog(Path(tmpdir) / "logs", now=STARTED, pid=1, stream=io.StringIO())
    set_current(log)
    return log


def make_store(tmpdir):
    from ebctool.store import Store
    store = Store(Path(tmpdir) / "db" / "t.db")
    return store


def hexframe(text):
    """'fa 05 00 ... f8' -> bytes."""
    return bytes.fromhex(text.replace(" ", ""))
