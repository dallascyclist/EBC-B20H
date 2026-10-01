"""Console output for the device commands, and the sink that writes a discharge into the store.

ConsoleSink and StoreSink implement the session's Sink protocol (on_start, on_sample, on_end).
Everything goes to the stdout the caller passes in. No colour escapes: the live line is
redrawn with a carriage return and padded so a shorter line covers a longer one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ebctool.log import current
from ebctool.protocol import MODE_NAMES, Status
from ebctool.session import DischargePlan, Sample, SessionResult, pack_lost_text
from ebctool.store import SampleRow, Store

LIVE_WIDTH = 80


def utc_iso() -> str:
    """Now as UTC ISO-8601 to the second, e.g. 2026-09-30T20:00:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_and_local(stamp: str) -> tuple[str, str]:
    """A stored UTC time (2026-10-01T02:42:14Z) as the two forms the listings and the report
    show, to the minute: ("2026-10-01 02:42Z", "2026-09-30 21:42 CDT"). Local time is the
    environment's: TZ if set, else the system zone. Its first ten characters are the local
    calendar day. Text that is not such a time comes back unchanged in both places."""
    try:
        utc = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return stamp, stamp
    return utc.strftime("%Y-%m-%d %H:%MZ"), _local_text(utc.astimezone())


def _local_text(local: datetime) -> str:
    """2026-09-30 21:42 CDT: the zone's abbreviation, or its numeric offset when it has none."""
    return local.strftime("%Y-%m-%d %H:%M ") + (local.strftime("%Z") or local.strftime("%z"))


def hms(seconds: Optional[float]) -> str:
    """Elapsed time as H:MM:SS, hours not zero-padded (0:02:34, 12:05:00); "-" for None.
    The one runtime format: the live line, the end summary, the test tables and the report."""
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def limit_text(max_minutes: int) -> str:
    return "max %d min" % max_minutes if max_minutes else "no time limit"


def status_line(status: Status, elapsed: float, raw: bool = False) -> str:
    """One decoded status frame for discover and monitor, prefixed by its hex when raw."""
    mode = "-" if status.mode is None else MODE_NAMES.get(status.mode, str(status.mode))
    text = ("%7.1f s  %-15s %-12s %6.2f V %6.2f A %8.3f Ah  echo %d %d %d  %s  fw %s"
            % (elapsed, status.state, mode, status.volts, status.amps, status.ah_device,
               status.echo1, status.echo2, status.echo3, status.model_name, status.fw_version or "-"))
    return status.raw.hex(" ") + "  " + text if raw else text


class ConsoleSink:
    """Progress of one discharge on the console. The clock is the session's; the live line
    shows each sample's own t_s (seconds since the first active frame).

    Console output is best effort: the first OSError from a write or flush (BrokenPipeError
    when the reader of a `| tee` died with the same Ctrl-C) ends output for the rest of the session,
    with one INFO line in the run log, and is never raised into the session."""

    def __init__(self, stdout, clock):
        self.stdout = stdout
        self.clock = clock
        self.lost = False

    def _write(self, text: str) -> None:
        if self.lost:
            return
        try:
            self.stdout.write(text)
            self.stdout.flush()
        except OSError as exc:
            self.lost = True
            log = current()
            if log is not None:
                try:
                    log.info("console.ConsoleSink", "console output stopped: %s: %s; the run goes on"
                             % (type(exc).__name__, exc))
                except Exception:   # a broken log must not end the run either
                    pass

    def on_start(self, plan: DischargePlan, first: Status) -> None:
        self._write("starting CC %.2f A, cutoff %.2f V, %s, from %.2f V; Ctrl-C stops the discharge\n"
                    % (plan.current_a, plan.cutoff_v, limit_text(plan.max_minutes), first.volts))

    def on_sample(self, sample: Sample) -> None:
        line = ("  t=%s  %.2f V  %.2f A  %.3f Ah  %.2f Wh  %s"
                % (hms(sample.t_s), sample.volts, sample.amps, sample.ah_host, sample.wh_host, sample.state))
        self._write("\r" + line.ljust(LIVE_WIDTH))

    def on_end(self, result: SessionResult) -> None:
        lines = ("end reason  %s" % result.end_reason,
                 "runtime     %s" % hms(result.runtime_s),
                 "Ah host     %.3f" % result.ah_host,
                 "Ah device   %s" % ("-" if result.ah_device_final is None else "%.3f" % result.ah_device_final),
                 "Wh host     %.2f" % result.wh_host,
                 "voltage     %.2f V at start, %.2f V at end" % (result.start_v, result.end_v))
        if result.collapsed_v is not None:
            lines += (pack_lost_text(result.collapsed_v),)
        self._write("\n" + "".join(line + "\n" for line in lines))


class StoreSink:
    """Writes each sample as it arrives (the store commits every write) and the result at the end."""

    def __init__(self, store: Store, test_id: int):
        self.store = store
        self.test_id = test_id

    def on_start(self, plan: DischargePlan, first: Status) -> None:
        pass

    def on_sample(self, sample: Sample) -> None:
        self.store.append_sample(self.test_id, SampleRow(self.test_id, sample.t_s, sample.volts, sample.amps,
                                                         sample.ah_host, sample.wh_host, sample.ah_device,
                                                         sample.state))

    def on_end(self, result: SessionResult) -> None:
        self.store.finish_test(self.test_id, ended_at=utc_iso(), end_reason=result.end_reason,
                               runtime_s=result.runtime_s, ah_host=result.ah_host, wh_host=result.wh_host,
                               ah_device_final=result.ah_device_final, start_v=result.start_v, end_v=result.end_v,
                               device_model=result.device_model, firmware=result.firmware)
