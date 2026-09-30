"""SQLite history of packs, discharge tests and their samples, with CSV export of one test.

The database file and its parent directory are created on first use. Time stamps are UTC
ISO-8601 strings; callers supply them for tests, and the store stamps a pack's created_at.
Every write commits at once, so a crash mid-discharge keeps every sample written so far.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ebctool.log import traced

CHEMISTRIES = ("li-ion", "lifepo4", "other")
END_REASONS = ("cutoff", "max_time", "user_stop", "comm_lost", "device_error")
CSV_HEADER = ("t_s", "volts", "amps", "ah_host", "wh_host", "ah_device", "state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS packs (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, chemistry TEXT NOT NULL,
  cells_series INTEGER, nominal_v REAL, rated_ah REAL, notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tests (
  id INTEGER PRIMARY KEY, pack_id INTEGER NOT NULL REFERENCES packs(id),
  started_at TEXT NOT NULL, ended_at TEXT, mode TEXT NOT NULL DEFAULT 'cc',
  set_current_a REAL NOT NULL, cutoff_v REAL NOT NULL, max_minutes INTEGER NOT NULL,
  end_reason TEXT, runtime_s REAL, ah_host REAL, wh_host REAL, ah_device_final REAL,
  start_v REAL, end_v REAL, device_model TEXT, firmware TEXT, run_id TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS samples (
  test_id INTEGER NOT NULL REFERENCES tests(id), t_s REAL NOT NULL, volts REAL NOT NULL,
  amps REAL NOT NULL, ah_host REAL NOT NULL, wh_host REAL NOT NULL, ah_device REAL,
  state TEXT NOT NULL, PRIMARY KEY (test_id, t_s));
"""


class StoreError(Exception):
    """A missing pack or test, a duplicate pack name, or a value outside its allowed set."""


@dataclass(frozen=True)
class Pack:
    id: int
    name: str
    chemistry: str
    cells_series: Optional[int]
    nominal_v: Optional[float]
    rated_ah: Optional[float]
    notes: str
    created_at: str


@dataclass(frozen=True)
class TestRow:
    """One discharge test; ended_at, end_reason and the result fields stay None until finish_test."""
    id: int
    pack_id: int
    started_at: str
    ended_at: Optional[str]
    mode: str
    set_current_a: float
    cutoff_v: float
    max_minutes: int
    end_reason: Optional[str]
    runtime_s: Optional[float]
    ah_host: Optional[float]
    wh_host: Optional[float]
    ah_device_final: Optional[float]
    start_v: Optional[float]
    end_v: Optional[float]
    device_model: Optional[str]
    firmware: Optional[str]
    run_id: str
    notes: str


@dataclass(frozen=True)
class SampleRow:
    test_id: int
    t_s: float
    volts: float
    amps: float
    ah_host: float
    wh_host: float
    ah_device: Optional[float]
    state: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """One open connection to the history database; close it, or use it as a context manager."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            if self._conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                self._conn.execute("PRAGMA user_version = 1")
            self._conn.commit()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # packs

    @traced
    def add_pack(self, name, chemistry, cells_series=None, nominal_v=None, rated_ah=None, notes="") -> Pack:
        if chemistry not in CHEMISTRIES:
            raise StoreError("chemistry %r not one of %s" % (chemistry, ", ".join(CHEMISTRIES)))
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO packs (name, chemistry, cells_series, nominal_v, rated_ah, notes, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, chemistry, cells_series, nominal_v, rated_ah, notes, _utc_now()))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" not in str(exc):
                raise
            raise StoreError("pack %r already exists" % name) from None
        return Pack(**dict(self._conn.execute("SELECT * FROM packs WHERE id = ?", (cur.lastrowid,)).fetchone()))

    def get_pack(self, name) -> Pack:
        row = self._conn.execute("SELECT * FROM packs WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise StoreError("no pack %r" % name)
        return Pack(**dict(row))

    def list_packs(self) -> list[Pack]:
        return [Pack(**dict(r)) for r in self._conn.execute("SELECT * FROM packs ORDER BY name")]

    # tests

    @traced
    def begin_test(self, pack_id, started_at, set_current_a, cutoff_v, max_minutes, run_id, notes="") -> int:
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO tests (pack_id, started_at, set_current_a, cutoff_v, max_minutes, run_id, notes)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pack_id, started_at, set_current_a, cutoff_v, max_minutes, run_id, notes))
        except sqlite3.IntegrityError as exc:
            if "FOREIGN KEY" not in str(exc):
                raise
            raise StoreError("no pack with id %r" % (pack_id,)) from None
        return cur.lastrowid

    def append_sample(self, test_id, sample: SampleRow) -> None:
        """Insert and commit one sample under test_id (the argument wins over sample.test_id)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO samples (test_id, t_s, volts, amps, ah_host, wh_host, ah_device, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (test_id, sample.t_s, sample.volts, sample.amps, sample.ah_host, sample.wh_host,
                 sample.ah_device, sample.state))

    @traced
    def finish_test(self, test_id, ended_at, end_reason, runtime_s, ah_host, wh_host, ah_device_final,
                    start_v, end_v, device_model, firmware) -> None:
        if end_reason not in END_REASONS:
            raise StoreError("end_reason %r not one of %s" % (end_reason, ", ".join(END_REASONS)))
        with self._conn:
            cur = self._conn.execute(
                "UPDATE tests SET ended_at = ?, end_reason = ?, runtime_s = ?, ah_host = ?, wh_host = ?,"
                " ah_device_final = ?, start_v = ?, end_v = ?, device_model = ?, firmware = ? WHERE id = ?",
                (ended_at, end_reason, runtime_s, ah_host, wh_host, ah_device_final, start_v, end_v,
                 device_model, firmware, test_id))
        if cur.rowcount == 0:
            raise StoreError("no test %d" % test_id)

    def list_tests(self, pack_name=None) -> list[TestRow]:
        """All tests, or one pack's (StoreError if that pack is missing), newest start first."""
        if pack_name is None:
            rows = self._conn.execute("SELECT * FROM tests ORDER BY started_at DESC, id DESC")
        else:
            pack_id = self.get_pack(pack_name).id
            rows = self._conn.execute("SELECT * FROM tests WHERE pack_id = ? ORDER BY started_at DESC, id DESC",
                                      (pack_id,))
        return [TestRow(**dict(r)) for r in rows]

    def get_test(self, test_id) -> TestRow:
        row = self._conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
        if row is None:
            raise StoreError("no test %d" % test_id)
        return TestRow(**dict(row))

    def samples(self, test_id) -> list[SampleRow]:
        rows = self._conn.execute("SELECT * FROM samples WHERE test_id = ? ORDER BY t_s", (test_id,))
        return [SampleRow(**dict(r)) for r in rows]

    @traced
    def export_csv(self, test_id, path) -> int:
        """Write one test's samples to path as CSV; returns the number of data rows."""
        self.get_test(test_id)
        rows = self.samples(test_id)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            for s in rows:
                writer.writerow(("" if v is None else v for v in
                                 (s.t_s, s.volts, s.amps, s.ah_host, s.wh_host, s.ah_device, s.state)))
        return len(rows)
