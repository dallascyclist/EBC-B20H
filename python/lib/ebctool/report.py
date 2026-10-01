"""Per-pack discharge history: a fixed-width text table and one self-contained HTML page.

history_rows() reads a pack's finished tests oldest first; format_table() lays them out for the
console; write_html() renders the same rows as one HTML file holding two inline SVG charts and the
table. The page is a <style> block plus markup: no script and no external URL.

Capacity and percent of rated use host-integrated Ah (ah_host); the tester's own capacity field is
unverified above 10 Ah. Colours are the dataviz reference palette, validated for light and dark with
validate_palette.js. Text always wears an ink colour; a series colour goes only on its line, its
markers and its legend or label key. The light palette has three slots under 3:1 against the
surface, so every coloured voltage curve carries a direct end label with its test date.

Start times are stored in UTC. The tables show each in UTC and in local time (the environment's
zone: TZ if set, else the system zone); chart labels use the local calendar day, and tooltips and
chart B's value label carry both forms.

A pack_lost test (pack unplugged, or its BMS cut off) is a finished test and is listed like any
other, but it is not a capacity result: the HTML table marks it in the end column and chart B
leaves it out, saying so below the chart, so a BMS trip does not look like capacity fade.
The summary line takes its latest capacity result from the newest test that is not pack_lost, and
chart A draws no sample under the pack-lost floor, which would stretch its voltage axis to 0.
"""
from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from ebctool.console import hms, utc_and_local
from ebctool.log import current, traced
from ebctool.session import PACK_LOST_FLOOR_CV
from ebctool.store import Pack, SampleRow, Store, TestRow

RECENT = 8                       # newest tests drawn in categorical colour; older ones fold into gray
WIDTH, HEIGHT = 800, 360
# The brief's margins are left 56, right 24, top 24, bottom 40; the right one is widened so the
# dated end labels of chart A and the last-point label of chart B sit beside the plot. That label's
# local start time (2026-09-30 21:42 CDT) measures about 127 px of 11 px system-ui, 139 px with a
# numeric zone (+0530); it starts 6 px left of X1, so 144 leaves it 150 px.
LEFT, RIGHT, TOP, BOTTOM = 56, 144, 24, 40
X0, X1, Y0, Y1 = LEFT, WIDTH - RIGHT, TOP, HEIGHT - BOTTOM
LABEL_X = X1 + 8                 # end-label column: 10 px colour key, then the date
LABEL_STEP = 14                  # end labels are 11 px text, at least this far apart
POINT_INSET = 16                 # chart B keeps its first and last markers off the plot edges
DATE_GAP = 80                    # minimum spacing of chart B's date tick labels (each about 60 px)
VAL_LINE = 13                    # line pitch of chart B's three-line value label (11 px text)
MAX_POINTS = 400                 # longer curves are thinned evenly; the last sample always stays

# role, light, dark: the dataviz reference palette (surface, ink, secondary ink, muted, gridline, 8 series)
PALETTE = (
    ("surface", "#fcfcfb", "#1a1a19"),
    ("ink", "#0b0b0b", "#ffffff"),
    ("ink2", "#52514e", "#c3c2b7"),
    ("muted", "#898781", "#898781"),
    ("grid", "#e1e0d9", "#2c2c2a"),
    ("s1", "#2a78d6", "#3987e5"),
    ("s2", "#eb6834", "#d95926"),
    ("s3", "#1baf7a", "#199e70"),
    ("s4", "#eda100", "#c98500"),
    ("s5", "#e87ba4", "#d55181"),
    ("s6", "#008300", "#008300"),
    ("s7", "#4a3aa7", "#9085e9"),
    ("s8", "#e34948", "#e66767"),
)

PACK_LOST = "pack_lost"
LOST_END_TEXT = "pack_lost: not a capacity result"   # the HTML table's end cell for a pack_lost test
LOST_NOTE = "pack_lost tests are not plotted here"     # under chart B when any were left out

COLUMNS = (("id", ">"), ("started UTC", "<"), ("started local", "<"), ("current A", ">"), ("cutoff V", ">"),
           ("Ah", ">"), ("% rated", ">"), ("Ah device", ">"), ("Wh", ">"), ("runtime", ">"), ("start V", ">"),
           ("end V", ">"), ("end", "<"))


@dataclass(frozen=True)
class HistoryRow:
    """One finished test as the report shows it. started_utc and started_local are the start time
    in both forms (console.utc_and_local); date is the local calendar day of the start, which the
    charts place and label tests by. ah_host is host-integrated."""
    test_id: int
    date: str
    started_utc: str
    started_local: str
    set_current_a: float
    cutoff_v: float
    ah_host: Optional[float]
    ah_device: Optional[float]
    wh_host: Optional[float]
    runtime_s: Optional[float]
    start_v: Optional[float]
    end_v: Optional[float]
    pct_of_rated: Optional[float]
    end_reason: str


def _row(pack: Pack, t: TestRow) -> HistoryRow:
    rated = pack.rated_ah
    pct = 100.0 * t.ah_host / rated if t.ah_host is not None and rated is not None and rated > 0 else None
    utc, local = utc_and_local(t.started_at)
    return HistoryRow(t.id, local[:10], utc, local, t.set_current_a, t.cutoff_v, t.ah_host,
                      t.ah_device_final, t.wh_host, t.runtime_s, t.start_v, t.end_v, pct, t.end_reason)


@traced
def history_rows(store: Store, pack_name: str) -> list[HistoryRow]:
    """The pack's finished tests, oldest first (StoreError if there is no such pack). Tests that were
    begun but never finished (no end_reason) are left out and counted in the run log."""
    pack = store.get_pack(pack_name)
    tests = store.list_tests(pack_name)[::-1]
    skipped = [t.id for t in tests if t.end_reason is None]
    log = current()
    if skipped and log is not None:
        log.decision("report.history_rows", "skipped %d unfinished %s (id %s)"
                     % (len(skipped), "test" if len(skipped) == 1 else "tests", ", ".join(map(str, skipped))))
    return [_row(pack, t) for t in tests if t.end_reason is not None]


# text table

def _fmt(value, spec: str) -> str:
    return "-" if value is None else spec % value


def _cells(r: HistoryRow) -> list[str]:
    return [str(r.test_id), r.started_utc, r.started_local, "%.2f" % r.set_current_a, "%.2f" % r.cutoff_v,
            _fmt(r.ah_host, "%.3f"), _fmt(r.pct_of_rated, "%.1f"), _fmt(r.ah_device, "%.3f"),
            _fmt(r.wh_host, "%.1f"), hms(r.runtime_s), _fmt(r.start_v, "%.2f"), _fmt(r.end_v, "%.2f"),
            r.end_reason]


def format_table(rows: Sequence[HistoryRow]) -> str:
    """Fixed-width text: a header line, then one line per row (no trailing newline)."""
    body = [_cells(r) for r in rows]
    widths = [max([len(h)] + [len(c[i]) for c in body]) for i, (h, _) in enumerate(COLUMNS)]
    lines = []
    for cells in [[h for h, _ in COLUMNS]] + body:
        lines.append("  ".join(format(c, a + str(w)) for c, (_, a), w in zip(cells, COLUMNS, widths)).rstrip())
    return "\n".join(lines)


# chart helpers

_esc = html.escape


def _scale(values: Sequence[float], lo: float, hi: float, px_lo: float, px_hi: float) -> list[float]:
    """Map values in lo..hi linearly onto px_lo..px_hi; all to the middle when lo == hi."""
    if hi == lo:
        return [(px_lo + px_hi) / 2.0 for _ in values]
    k = (px_hi - px_lo) / (hi - lo)
    return [px_lo + (v - lo) * k for v in values]


def _polyline(points: Sequence[tuple[float, float]]) -> str:
    return " ".join("%.1f,%.1f" % p for p in points)


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """Round tick values (1, 2, 2.5 or 5 times a power of ten) from at or below lo to at or above hi,
    at most about n intervals. The first and last ticks are the axis domain."""
    if not hi > lo:
        lo, hi = lo - 0.5, lo + 0.5
    mag = 10 ** math.floor(math.log10((hi - lo) / n))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        first, last = math.floor(lo / step + 1e-9), math.ceil(hi / step - 1e-9)
        if last - first <= n:
            break
    return [round(k * step, 9) for k in range(first, last + 1)]


def _tick_text(value: float) -> str:
    return "%g" % value


def _thin(curve: Sequence[SampleRow], limit: int = MAX_POINTS) -> list[SampleRow]:
    if len(curve) <= limit:
        return list(curve)
    kept = list(curve[::math.ceil(len(curve) / limit)])
    if kept[-1] is not curve[-1]:
        kept.append(curve[-1])
    return kept


def _spread(desired: Sequence[float], lo: float, hi: float, step: float) -> list[float]:
    """Positions for ascending desired values, each at least step above the one below it, kept in
    lo..hi. The stack grows upward from the lowest value: curves that stop at a cutoff end bunched
    low, and a stack on one side of the bunch lets the labels fan out in end order."""
    ys = [min(max(d, lo), hi) for d in desired]
    for i in range(len(ys) - 2, -1, -1):
        ys[i] = min(ys[i], ys[i + 1] - step)
    if ys and ys[0] < lo:
        ys[0] = lo
        for i in range(1, len(ys)):
            ys[i] = max(ys[i], ys[i - 1] + step)
    return ys


def _crosses(p1, p2, p3, p4) -> bool:
    """True when segment p1-p2 properly crosses segment p3-p4."""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return side(p1, p2, p3) * side(p1, p2, p4) < 0 and side(p3, p4, p1) * side(p3, p4, p2) < 0


def _place_labels(ends: Sequence[tuple[float, float]], column_x: float, lo: float, hi: float) -> list[float]:
    """y of each end's label in a column at column_x. Ends less than LABEL_STEP apart in height form a
    bunch; slots go to bunches top down and, within a bunch, to its ends left to right, so curves that
    stop together at a cutoff fan out in end order. Then any two leader lines that cross swap slots;
    each swap shortens the leaders' total length, so this ends, with no two leaders crossing."""
    by_y = sorted(range(len(ends)), key=lambda i: ends[i][1])
    bunch, n = {}, 0
    for prev, i in zip([None] + by_y, by_y):
        if prev is not None and ends[i][1] - ends[prev][1] >= LABEL_STEP:
            n += 1
        bunch[i] = n
    order = sorted(range(len(ends)), key=lambda i: (bunch[i], ends[i][0]))
    slot = dict(zip(order, _spread([ends[i][1] for i in by_y], lo, hi, LABEL_STEP)))
    swapped = True
    while swapped:
        swapped = False
        for a in range(len(ends)):
            for b in range(a + 1, len(ends)):
                if _crosses(ends[a], (column_x, slot[a]), ends[b], (column_x, slot[b])):
                    slot[a], slot[b] = slot[b], slot[a]
                    swapped = True
    return [slot[i] for i in range(len(ends))]


def _y_axis(ticks: Sequence[float], unit: str) -> list[str]:
    """Hairline gridline and muted label per tick, and the unit above the labels."""
    parts = ['<text class="tick" x="%d" y="%d" text-anchor="end">%s</text>' % (X0 - 8, Y0 - 10, unit)]
    for v, y in zip(ticks, _scale(ticks, ticks[0], ticks[-1], Y1, Y0)):
        parts.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>' % (X0, y, X1, y))
        parts.append('<text class="tick" x="%d" y="%.1f" dy="0.35em" text-anchor="end">%s</text>'
                     % (X0 - 8, y, _tick_text(v)))
    return parts


def _svg(label: str, parts: Sequence[str]) -> str:
    return ('<svg viewBox="0 0 %d %d" role="img" aria-label="%s">\n%s\n</svg>'
            % (WIDTH, HEIGHT, _esc(label), "\n".join(parts)))


def _started(r: HistoryRow) -> str:
    """Both forms of the start time in one phrase: 2026-09-30 21:42 CDT (2026-10-01 02:42Z)."""
    return "%s (%s)" % (r.started_local, r.started_utc)


def _test_title(r: HistoryRow) -> str:
    pct = "" if r.pct_of_rated is None else " (%.1f%% of rated)" % r.pct_of_rated
    return ("%s, test %d: %s Ah%s in %s, %s V to %s V, %s"
            % (_started(r), r.test_id, _fmt(r.ah_host, "%.3f"), pct, hms(r.runtime_s), _fmt(r.start_v, "%.2f"),
               _fmt(r.end_v, "%.2f"), r.end_reason))


# chart A: voltage under load

def _voltage_chart(pack: Pack, rows: Sequence[HistoryRow], curves: dict) -> str:
    """Legend, then the SVG: older tests in gridline gray underneath, the newest RECENT in slot
    colours by date, each coloured curve with a dated end label in a column right of the plot.
    Samples under the pack-lost floor (a pack_lost run's last frame, 0.00 V) are not drawn: they
    would stretch the shared voltage axis to 0. The database and CSV keep them."""
    curves = {r.test_id: [s for s in curves[r.test_id] if round(s.volts * 100) >= PACK_LOST_FLOOR_CV]
              for r in rows}   # compared in centivolts, as the session judges it
    samples = [s for r in rows for s in curves[r.test_id]]
    if not samples:
        return '<p class="empty">no samples to plot</p>'
    older, recent = rows[:-RECENT], rows[-RECENT:]
    # x runs to the longest curve's end, not a rounded tick, so the end labels sit close to the lines
    xmax = max(max(s.t_s for s in samples) / 60.0, 1.0)
    xt = [v for v in _ticks(0.0, xmax) if v <= xmax + 1e-9]
    yt = _ticks(min(s.volts for s in samples), max(s.volts for s in samples))

    def points(r: HistoryRow) -> list[tuple[float, float]]:
        curve = _thin(curves[r.test_id])
        return list(zip(_scale([s.t_s / 60.0 for s in curve], 0.0, xmax, X0, X1),
                        _scale([s.volts for s in curve], yt[0], yt[-1], Y1, Y0)))

    parts = _y_axis(yt, "V")
    for v, x in zip(xt, _scale(xt, 0.0, xmax, X0, X1)):
        parts.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (x, Y1 + 18, _tick_text(v)))
    parts.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle">elapsed minutes</text>'
                 % ((X0 + X1) / 2.0, Y1 + 34))
    for r in older:
        if curves[r.test_id]:
            parts.append('<polyline class="line older" points="%s"><title>%s</title></polyline>'
                         % (_polyline(points(r)), _esc(_test_title(r))))
    ends = []
    for slot, r in enumerate(recent, 1):
        pts = points(r)
        if pts:
            parts.append('<polyline class="line s%d" points="%s"><title>%s</title></polyline>'
                         % (slot, _polyline(pts), _esc(_test_title(r))))
            ends.append((pts[-1], slot, r.date))
    for ((xe, ye), slot, date), ly in zip(ends, _place_labels([e[0] for e in ends], LABEL_X, Y0 + 6, Y1)):
        if LABEL_X - xe > 8 or abs(ly - ye) > 1:
            parts.append('<line class="lead" x1="%.1f" y1="%.1f" x2="%d" y2="%.1f"/>' % (xe + 3, ye, LABEL_X - 3, ly))
        parts.append('<line class="key s%d" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>' % (slot, LABEL_X, ly, LABEL_X + 10, ly))
        parts.append('<text class="lbl" x="%d" y="%.1f" dy="0.35em">%s</text>' % (LABEL_X + 14, ly, _esc(date)))

    entries = ([('<span class="key"></span>', "older (%d)" % len(older))] if older else [])
    entries += [('<span class="key k%d"></span>' % slot, r.date) for slot, r in enumerate(recent, 1)]
    legend = ""
    if len(entries) >= 2:
        legend = '<ul class="legend">%s</ul>\n' % "".join("<li>%s%s</li>" % (key, _esc(text)) for key, text in entries)
    return legend + _svg("Voltage under load for %s: volts against elapsed minutes, one line per test" % pack.name,
                         parts)


# chart B: delivered Ah by date

def _day_numbers(dates: Sequence[str]) -> list[float]:
    """Days since year 1 for each YYYY-MM-DD date, so spacing follows the calendar; evenly spaced
    positions if any date does not parse."""
    try:
        return [float(datetime.strptime(d, "%Y-%m-%d").toordinal()) for d in dates]
    except ValueError:
        return [float(i) for i in range(len(dates))]


def _capacity_chart(pack: Pack, rows: Sequence[HistoryRow]) -> str:
    """One series in slot 1 on a zero-based Ah axis (up to the rated Ah if that is higher), 8 px
    markers with a surface ring, and the last point's value labelled beside it. pack_lost tests
    are left out: they are not capacity results."""
    rows = [r for r in rows if r.ah_host is not None and r.end_reason != PACK_LOST]
    if not rows:
        return '<p class="empty">no capacity to plot</p>'
    ahs = [r.ah_host for r in rows]
    days = _day_numbers([r.date for r in rows])
    rated = [pack.rated_ah] if pack.rated_ah is not None and pack.rated_ah > 0 else []
    top = max(ahs + rated)
    yt = _ticks(min(0.0, min(ahs)), top if top > 0 else 1.0)
    xs = _scale(days, min(days), max(days), X0 + POINT_INSET, X1 - POINT_INSET)
    ys = _scale(ahs, yt[0], yt[-1], Y1, Y0)

    parts = _y_axis(yt, "Ah")
    ticks: list[tuple[float, str]] = []
    for x, r in zip(xs, rows):
        if not ticks or x - ticks[-1][0] >= DATE_GAP:
            ticks.append((x, r.date))
    if ticks[-1][1] != rows[-1].date:
        if xs[-1] - ticks[-1][0] < DATE_GAP:
            ticks.pop()
        ticks.append((xs[-1], rows[-1].date))
    for x, date in ticks:
        parts.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (x, Y1 + 18, _esc(date)))
    parts.append('<polyline class="line s1" points="%s"><title>Delivered Ah by date</title></polyline>'
                 % _polyline(list(zip(xs, ys))))
    for x, y, r in zip(xs, ys, rows):
        parts.append('<g><title>%s</title><circle class="hit" cx="%.1f" cy="%.1f" r="12"/>'
                     '<circle class="dot" cx="%.1f" cy="%.1f" r="4"/></g>' % (_esc(_test_title(r)), x, y, x, y))
    last = rows[-1]
    value = "%.2f Ah" % last.ah_host + ("" if last.pct_of_rated is None else " (%.0f%%)" % last.pct_of_rated)
    # The value sits level with the last point, its start time in both forms on two lines under
    # it; near the bottom of the plot the two lines go above it instead, clear of the date ticks.
    lines = [("", value), ("lbl", last.started_local), ("lbl", last.started_utc)]
    top = ys[-1]
    if ys[-1] + 2 * VAL_LINE > Y1:
        lines, top = lines[1:] + lines[:1], ys[-1] - 2 * VAL_LINE
    spans = "".join('<tspan%s x="%.1f" y="%.1f" dy="0.35em">%s</tspan>'
                    % (' class="%s"' % cls if cls else "", xs[-1] + 10, top + i * VAL_LINE, _esc(text))
                    for i, (cls, text) in enumerate(lines))
    parts.append('<text class="val">%s</text>' % spans)
    return _svg("Delivered Ah by date for %s" % pack.name, parts)


# page

CSS = """:root{%s color-scheme:light dark}
@media (prefers-color-scheme: dark){:root{%s}}
body{margin:32px auto;max-width:848px;padding:0 24px;background:var(--surface);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
h1{font-size:20px;font-weight:600;margin:0 0 4px}
h2{font-size:15px;font-weight:600;margin:28px 0 8px}
.summary,.notes,.empty,.chart-note{color:var(--ink2);margin:0 0 4px}
svg{display:block;width:100%%;max-width:800px;height:auto}
svg text{font-size:11px;fill:var(--muted);font-variant-numeric:tabular-nums}
svg .lbl{fill:var(--ink2)}
svg .val{fill:var(--ink)}
.grid{stroke:var(--grid);stroke-width:1;vector-effect:non-scaling-stroke;shape-rendering:crispEdges}
.lead{stroke:var(--muted);stroke-width:1;vector-effect:non-scaling-stroke}
.line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.line:hover{stroke-width:3}
.key{stroke-width:2;stroke-linecap:round}
.older{stroke:var(--grid)}
.hit{fill:transparent}
.dot{fill:var(--s1);stroke:var(--surface);stroke-width:2}
.legend{list-style:none;display:flex;flex-wrap:wrap;gap:4px 16px;margin:0 0 8px;padding:0;color:var(--ink2);font-size:13px}
.legend .key{display:inline-block;width:14px;border-top:2px solid var(--grid);vertical-align:middle;margin-right:6px}
%s
.scroll{overflow-x:auto;margin-top:28px}
table{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
caption{caption-side:top;text-align:left;font-weight:600;font-size:15px;padding-bottom:8px}
th,td{padding:4px 10px;border-bottom:1px solid var(--grid);text-align:left;white-space:nowrap}
th{color:var(--ink2);font-weight:600}
.num{text-align:right}
.lost{font-weight:600}
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
%s</style>
</head>
<body>
%s
</body>
</html>
"""


def _css() -> str:
    light = "".join("--%s:%s;" % (role, hexa) for role, hexa, _ in PALETTE)
    dark = "".join("--%s:%s;" % (role, hexa) for role, _, hexa in PALETTE)
    series = "\n".join(".s%d{stroke:var(--s%d)} .legend .k%d{border-top-color:var(--s%d)}" % ((n,) * 4)
                       for n in range(1, RECENT + 1))
    return CSS % (light, dark, series)


def _summary(pack: Pack, rows: Sequence[HistoryRow]) -> str:
    """Rated Ah; finished tests, with how many were pack_lost; the latest capacity result, from the
    newest test that is not pack_lost; and a pointer to the newest test when it was pack_lost."""
    parts = ["rated %.2f Ah" % pack.rated_ah if pack.rated_ah is not None else "rated Ah not set"]
    if not rows:
        return "; ".join(parts + ["no finished tests yet: bin/ebc.sh discharge --pack %s adds one" % pack.name])
    lost = sum(r.end_reason == PACK_LOST for r in rows)
    parts.append("%d finished %s" % (len(rows), "test" if len(rows) == 1 else "tests")
                 + (" (%d %s)" % (lost, PACK_LOST) if lost else ""))
    results = [r for r in rows if r.end_reason != PACK_LOST]
    if results:
        last = results[-1]
        latest = "latest capacity result %s: %s Ah" % (_started(last), _fmt(last.ah_host, "%.3f"))
        if last.pct_of_rated is not None:
            latest += ", %.1f%% of rated" % last.pct_of_rated
        parts.append(latest)
    else:
        parts.append("no capacity result yet")
    if rows[-1].end_reason == PACK_LOST:
        parts.append("newest test %d ended %s" % (rows[-1].test_id, LOST_END_TEXT))
    return "; ".join(parts)


def _html_table(pack: Pack, rows: Sequence[HistoryRow]) -> str:
    def cls(align: str) -> str:
        return ' class="num"' if align == ">" else ""

    def row(r: HistoryRow) -> str:
        cells = ["<td%s>%s</td>" % (cls(a), _esc(c)) for c, (_, a) in zip(_cells(r), COLUMNS)]
        if r.end_reason == PACK_LOST:   # the end column is the last
            cells[-1] = '<td class="lost">%s</td>' % _esc(LOST_END_TEXT)
        return "<tr>%s</tr>" % "".join(cells)

    head = "".join('<th scope="col"%s>%s</th>' % (cls(a), _esc(h)) for h, a in COLUMNS)
    body = "\n".join(row(r) for r in rows)
    return ('<div class="scroll"><table>\n<caption>%s tests, oldest first</caption>\n<thead><tr>%s</tr></thead>\n'
            '<tbody>\n%s\n</tbody>\n</table></div>'
            % (_esc(pack.name), head, body))


def _page(pack: Pack, rows: Sequence[HistoryRow], curves: dict) -> str:
    title = "%s discharge history" % _esc(pack.name)
    body = ["<h1>%s</h1>" % title, '<p class="summary">%s</p>' % _esc(_summary(pack, rows))]
    if pack.notes:
        body.append('<p class="notes">notes: %s</p>' % _esc(pack.notes))
    if rows:
        body.append("<section>\n<h2>Voltage under load</h2>\n%s\n</section>" % _voltage_chart(pack, rows, curves))
        note = ""
        if any(r.end_reason == PACK_LOST for r in rows):
            note = '\n<p class="chart-note">%s</p>' % _esc(LOST_NOTE)
        body.append("<section>\n<h2>Delivered Ah by date</h2>\n%s%s\n</section>" % (_capacity_chart(pack, rows), note))
    body.append(_html_table(pack, rows))
    return PAGE % (title, _css(), "\n".join(body))


@traced
def write_html(store: Store, pack_name: str, path) -> None:
    """Write the pack's history page to path, creating its directory (StoreError, and nothing
    written, if there is no such pack)."""
    pack = store.get_pack(pack_name)
    rows = history_rows(store, pack_name)
    curves = {r.test_id: store.samples(r.test_id) for r in rows}
    log = current()
    if log is not None and len(rows) > RECENT:
        log.decision("report.write_html", "%d tests: newest %d in colour, %d older drawn gray"
                     % (len(rows), RECENT, len(rows) - RECENT))
    page = _page(pack, rows, curves)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
