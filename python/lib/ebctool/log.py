"""Per-run log for ebctool: one file per run, UTC timestamps, PID, every call and decision.

Line format: 2026-09-24T01:06:30.123Z pid=12345 LEVEL module.function: message
"""
from __future__ import annotations

import functools
import inspect
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_SECRET_NAME = re.compile(r"pass|secret|key|token", re.IGNORECASE)
_MAX_REPR = 200
_SKIP_ARGS = ("self", "ctx", "db", "log", "conn")
_current: Optional["RunLog"] = None


def current() -> Optional["RunLog"]:
    return _current


def set_current(log: Optional["RunLog"]) -> None:
    global _current
    _current = log


def redact_value(name: str, value: Any) -> str:
    """repr() of an argument for the log; secrets by name become ***, long values truncated."""
    if _SECRET_NAME.search(name or ""):
        return "***"
    text = repr(value)
    if len(text) > _MAX_REPR:
        size = len(value) if isinstance(value, (str, bytes)) else len(text)
        text = text[:_MAX_REPR] + "...(%d chars)" % size
    return text


def summarize(value: Any) -> str:
    """Short description of a return value."""
    if value is None:
        return "None"
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return "%s[%d]" % (type(value).__name__, len(value))
    text = repr(value)
    return text if len(text) <= _MAX_REPR else text[:_MAX_REPR] + "..."


def _stamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


class RunLog:
    def __init__(self, log_dir, now: Optional[datetime] = None, pid: Optional[int] = None,
                 stream=None, mirror_level: str = "WARNING"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.started = now or datetime.now(timezone.utc)
        self.pid = os.getpid() if pid is None else pid
        self.stream = sys.stderr if stream is None else stream
        self._mirror_index = LEVELS.index(mirror_level)
        self.run_id = self.started.strftime("%Y%m%d%H%M")
        path = self.log_dir / (self.run_id + ".log")
        if path.exists():
            self.run_id = "%s-%d" % (self.run_id, self.pid)
            path = self.log_dir / (self.run_id + ".log")
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def _write(self, level: str, where: str, message: str) -> None:
        text = str(message).replace("\n", "\n    ")
        line = "%s pid=%d %s %s: %s" % (_stamp(), self.pid, level, where, text)
        self._fh.write(line + "\n")
        self._fh.flush()
        if LEVELS.index(level) >= self._mirror_index:
            self.stream.write(line + "\n")
            self.stream.flush()

    def debug(self, where: str, message: str) -> None:
        self._write("DEBUG", where, message)

    def info(self, where: str, message: str) -> None:
        self._write("INFO", where, message)

    def warning(self, where: str, message: str) -> None:
        self._write("WARNING", where, message)

    def error(self, where: str, message: str) -> None:
        self._write("ERROR", where, message)

    def decision(self, where: str, message: str) -> None:
        """A branch taken and why. Grep for DECISION to follow a run."""
        self._write("INFO", where, "DECISION " + message)

    def exception(self, where: str, exc: BaseException) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._write("ERROR", where, "%s: %s\n%s" % (type(exc).__name__, exc, tb.rstrip()))

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def traced(fn: Callable) -> Callable:
    """Log entry (arguments, secrets redacted) and exit (result summary, elapsed ms)."""
    where = "%s.%s" % (fn.__module__.rsplit(".", 1)[-1], fn.__name__)
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        log = _current
        if log is None:
            return fn(*args, **kwargs)
        try:
            bound = sig.bind_partial(*args, **kwargs).arguments
        except TypeError:
            bound = {}
        shown = ", ".join("%s=%s" % (k, redact_value(k, v)) for k, v in bound.items() if k not in _SKIP_ARGS)
        log.debug(where, "-> (%s)" % shown)
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            log.debug(where, "<- raised %s: %s (%.1f ms)" % (type(exc).__name__, exc, (time.perf_counter() - start) * 1000))
            raise
        log.debug(where, "<- %s (%.1f ms)" % (summarize(result), (time.perf_counter() - start) * 1000))
        return result

    return wrapper
