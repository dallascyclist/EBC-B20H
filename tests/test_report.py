import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from ebctool import run as R
from ebctool.report import format_table, history_rows, write_html
from ebctool.store import SampleRow, Store
from tests.support import close_log, make_log, use_tz

SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S)
TEXT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
TAG_RE = re.compile(r"<[^>]+>")


def add_finished(store, pack_id, day, ah, samples=3, ah_device=None, run="r", reason="cutoff"):
    """One finished test started on 2026-09-<day> with a short falling voltage curve."""
    tid = store.begin_test(pack_id, "2026-09-%sT10:00:00Z" % day, 5.0, 42.0, 0, run)
    for k in range(samples):
        store.append_sample(tid, SampleRow(tid, 2.0 * k, 54.0 - k, 5.0, ah * k / 2, 50.0 * k, ah * k / 2, "active"))
    store.finish_test(tid, "2026-09-%sT12:00:00Z" % day, reason, 7200, ah, 400.0,
                      ah if ah_device is None else ah_device, 54.0, 42.1, "EBC-B20H", "3.08")
    return tid


class ReportTests(unittest.TestCase):
    def setUp(self):
        use_tz(self, "America/Chicago")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.store.close)
        p = self.store.add_pack("rad52", "li-ion", cells_series=14, rated_ah=10.0)
        self.ids = []
        for n, (day, ah) in enumerate([("01", 9.0), ("15", 8.5), ("30", 8.0)]):
            tid = self.store.begin_test(p.id, "2026-09-%sT10:00:00Z" % day, 5.0, 42.0, 0, "r%d" % n)
            for k in range(3):
                self.store.append_sample(tid, SampleRow(tid, 2.0 * k, 54.0 - k, 5.0, ah * k / 2, 50.0 * k, ah * k / 2, "active"))
            self.store.finish_test(tid, "2026-09-%sT12:00:00Z" % day, "cutoff", 7200, ah, 400.0, ah, 54.0, 42.1, "EBC-B20H", "3.08")
            self.ids.append(tid)
        unfinished = self.store.begin_test(p.id, "2026-10-01T10:00:00Z", 5.0, 42.0, 0, "r9")

    def html(self, pack="rad52", name="h.html"):
        out = Path(self.tmp.name) / name
        write_html(self.store, pack, out)
        return out.read_text(encoding="utf-8")

    def test_rows_oldest_first_with_percent(self):
        rows = history_rows(self.store, "rad52")
        self.assertEqual([r.date for r in rows], ["2026-09-01", "2026-09-15", "2026-09-30"])
        self.assertAlmostEqual(rows[0].pct_of_rated, 90.0)
        self.assertEqual(len(rows), 3)

    def test_table_has_header_and_rows(self):
        text = format_table(history_rows(self.store, "rad52"))
        self.assertIn("started UTC", text.splitlines()[0])
        self.assertIn("started local", text.splitlines()[0])
        self.assertEqual(len(text.splitlines()), 4)

    # R37: start times in UTC and in the environment's local zone; chart labels by local day

    def late_evening(self, *starts):
        """Pack 'eve' with one finished test per start time (stored UTC), oldest first."""
        p = self.store.add_pack("eve", "li-ion", rated_ah=10.0)
        for n, started in enumerate(starts):
            tid = self.store.begin_test(p.id, started, 5.0, 42.0, 0, "e%d" % n)
            for k in range(3):
                self.store.append_sample(tid, SampleRow(tid, 2.0 * k, 54.0 - k, 5.0, 4.0 * k, 50.0 * k, 4.0 * k,
                                                        "active"))
            self.store.finish_test(tid, "x", "cutoff", 7200, 8.0 + n, 400.0, 8.0, 54.0, 42.1, "EBC-B20H", "3.08")

    def test_row_carries_utc_local_and_local_day(self):
        self.late_evening("2026-10-01T02:42:14Z")
        (row,) = history_rows(self.store, "eve")
        self.assertEqual((row.started_utc, row.started_local, row.date),
                         ("2026-10-01 02:42Z", "2026-09-30 21:42 CDT", "2026-09-30"))

    def test_table_shows_start_in_utc_and_local_time(self):
        self.late_evening("2026-10-01T02:42:14Z")
        header, line = format_table(history_rows(self.store, "eve")).splitlines()
        utc, local = header.index("started UTC"), header.index("started local")
        self.assertLess(header.index("id"), utc)
        self.assertEqual(line[utc:local].strip(), "2026-10-01 02:42Z")
        self.assertEqual(line[local:local + len("2026-09-30 21:42 CDT")], "2026-09-30 21:42 CDT")
        self.assertNotIn(" date ", header)

    def test_tz_utc_shows_the_same_time_in_both_columns(self):
        use_tz(self, "UTC")
        self.late_evening("2026-10-01T02:42:14Z")
        (row,) = history_rows(self.store, "eve")
        self.assertEqual((row.started_utc, row.started_local, row.date),
                         ("2026-10-01 02:42Z", "2026-10-01 02:42 UTC", "2026-10-01"))
        self.assertEqual(row.started_utc[:-1], row.started_local[:-len(" UTC")])

    def test_html_table_has_both_columns_and_charts_use_local_days(self):
        self.late_evening("2026-10-01T02:42:14Z", "2026-10-01T15:00:00Z")   # local 30 Sep 21:42, 1 Oct 10:00
        html = self.html("eve")
        self.assertIn('<th scope="col">started UTC</th><th scope="col">started local</th>', html)
        self.assertIn("<td>2026-10-01 02:42Z</td><td>2026-09-30 21:42 CDT</td>", html)
        self.assertIn("<td>2026-10-01 15:00Z</td><td>2026-10-01 10:00 CDT</td>", html)
        chart_a, chart_b = SVG_RE.findall(html)
        labels_a = [body for attrs, body in TEXT_RE.findall(chart_a) if 'class="lbl"' in attrs]
        self.assertEqual(sorted(labels_a), ["2026-09-30", "2026-10-01"])
        ticks_b = [body for attrs, body in TEXT_RE.findall(chart_b) if 'class="tick"' in attrs and "-" in body]
        self.assertEqual(ticks_b, ["2026-09-30", "2026-10-01"])   # one local day apart, so both fit
        self.assertIn("<li><span class=\"key k1\"></span>2026-09-30</li>", html)   # legend

    def test_every_test_tooltip_shows_both_forms(self):
        self.late_evening("2026-10-01T02:42:14Z", "2026-10-01T15:00:00Z")
        titles = [t for svg in SVG_RE.findall(self.html("eve")) for t in TITLE_RE.findall(svg) if "test " in t]
        self.assertEqual(len(titles), 4)   # two curves in chart A, two points in chart B
        for title in titles:
            self.assertTrue(("2026-09-30 21:42 CDT" in title and "2026-10-01 02:42Z" in title)
                            or ("2026-10-01 10:00 CDT" in title and "2026-10-01 15:00Z" in title), title)

    def test_capacity_value_label_shows_both_forms_inside_the_chart(self):
        self.late_evening("2026-09-20T15:00:00Z", "2026-10-01T02:42:14Z")
        chart_b = SVG_RE.findall(self.html("eve"))[1]
        [body] = [b for a, b in TEXT_RE.findall(chart_b) if 'class="val"' in a]
        lines = [TAG_RE.sub("", part) for part in re.findall(r"<tspan\b[^>]*>.*?</tspan>", body)]
        self.assertEqual(lines, ["9.00 Ah (90%)", "2026-09-30 21:42 CDT", "2026-10-01 02:42Z"])
        ys = [float(y) for y in re.findall(r'\by="(-?[\d.]+)"', body)]
        self.assertEqual(len(ys), 3)
        self.assertTrue(all(24 <= y <= 320 for y in ys), ys)   # stays between the plot's top and bottom

    def test_capacity_value_label_near_the_bottom_puts_the_dates_above(self):
        p = self.store.add_pack("low", "li-ion", rated_ah=17.5)
        add_finished(self.store, p.id, "20", 0.5)
        add_finished(self.store, p.id, "29", 0.0327)   # the bench test: a sliver of a 17.5 Ah pack
        chart_b = SVG_RE.findall(self.html("low"))[1]
        [(_, body)] = [(a, b) for a, b in TEXT_RE.findall(chart_b) if 'class="val"' in a]
        spans = re.findall(r'<tspan\b[^>]*\by="([\d.]+)"[^>]*>(.*?)</tspan>', body)
        self.assertEqual([text for _, text in spans], ["2026-09-29 05:00 CDT", "2026-09-29 10:00Z", "0.03 Ah (0%)"])
        last_cy = float(re.findall(r'class="dot" cx="[\d.]+" cy="([\d.]+)"', chart_b)[-1])
        self.assertEqual(float(spans[-1][0]), last_cy)                  # the value stays level with its point
        self.assertTrue(all(float(y) <= 320 for y, _ in spans), spans)  # nothing reaches the date ticks

    def test_summary_names_the_latest_test_in_both_forms(self):
        self.late_evening("2026-10-01T02:42:14Z")
        self.assertIn("latest capacity result 2026-09-30 21:42 CDT (2026-10-01 02:42Z): 8.000 Ah", self.html("eve"))

    # R41: the summary line never presents a pack_lost test as the latest capacity result

    def summary(self, pack):
        [text] = re.findall(r'<p class="summary">(.*?)</p>', self.html(pack))
        return text

    def test_summary_without_pack_lost_tests(self):
        self.assertEqual(self.summary("rad52"),
                         "rated 10.00 Ah; 3 finished tests; latest capacity result 2026-09-30 05:00 CDT"
                         " (2026-09-30 10:00Z): 8.000 Ah, 80.0% of rated")

    def test_summary_counts_an_older_pack_lost_test(self):
        self.lost_pack()   # cutoff, pack_lost, cutoff
        self.assertEqual(self.summary("lost"),
                         "rated 10.00 Ah; 3 finished tests (1 pack_lost); latest capacity result 2026-09-20"
                         " 05:00 CDT (2026-09-20 10:00Z): 8.800 Ah, 88.0% of rated")

    def test_summary_skips_a_newest_pack_lost_test(self):
        p = self.store.add_pack("bench", "li-ion", rated_ah=17.5)
        add_finished(self.store, p.id, "15", 8.75)
        lost = add_finished(self.store, p.id, "30", 0.007, reason="pack_lost")   # the 22:06 CDT bench unplug
        self.assertEqual(self.summary("bench"),
                         "rated 17.50 Ah; 2 finished tests (1 pack_lost); latest capacity result 2026-09-15"
                         " 05:00 CDT (2026-09-15 10:00Z): 8.750 Ah, 50.0%% of rated;"
                         " newest test %d ended pack_lost: not a capacity result" % lost)

    def test_summary_with_only_pack_lost_tests_has_no_capacity_result(self):
        p = self.store.add_pack("gone", "li-ion", rated_ah=10.0)
        add_finished(self.store, p.id, "10", 0.5, reason="pack_lost")
        lost = add_finished(self.store, p.id, "12", 0.2, reason="pack_lost")
        self.assertEqual(self.summary("gone"),
                         "rated 10.00 Ah; 2 finished tests (2 pack_lost); no capacity result yet;"
                         " newest test %d ended pack_lost: not a capacity result" % lost)

    def test_html_is_self_contained_with_one_polyline_per_test(self):
        out = Path(self.tmp.name) / "h.html"
        write_html(self.store, "rad52", out)
        html = out.read_text(encoding="utf-8")
        self.assertNotIn("<script", html)
        self.assertNotIn("http://", html); self.assertNotIn("https://", html)
        self.assertEqual(html.count("<polyline"), 3 + 1)      # chart A per test + chart B
        self.assertIn("2026-09-30", html)
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn("<title>", html)

    def test_more_than_eight_tests_fold_into_older(self):
        p = self.store.add_pack("big", "li-ion")
        for n in range(10):
            tid = self.store.begin_test(p.id, "2026-08-%02dT10:00:00Z" % (n + 1), 5.0, 42.0, 0, "b%d" % n)
            self.store.append_sample(tid, SampleRow(tid, 0.0, 54.0, 5.0, 0, 0, 0, "active"))
            self.store.finish_test(tid, "x", "cutoff", 100, 1.0, 50.0, 1.0, 54, 42, "m", "f")
        out = Path(self.tmp.name) / "b.html"
        write_html(self.store, "big", out)
        html = out.read_text(encoding="utf-8")
        self.assertIn("older (2)", html)

    # R12: percent of rated comes from host-integrated Ah

    def test_percent_of_rated_uses_host_ah(self):
        p = self.store.add_pack("r12", "li-ion", rated_ah=20.0)
        add_finished(self.store, p.id, "05", 15.0, ah_device=19.0)
        (row,) = history_rows(self.store, "r12")
        self.assertAlmostEqual(row.pct_of_rated, 75.0)
        self.assertAlmostEqual(row.ah_host, 15.0)
        self.assertAlmostEqual(row.ah_device, 19.0)

    def test_pack_without_rated_ah_shows_dash_for_percent(self):
        p = self.store.add_pack("norate", "other")
        add_finished(self.store, p.id, "05", 3.0)
        rows = history_rows(self.store, "norate")
        self.assertIsNone(rows[0].pct_of_rated)
        header, line = format_table(rows).splitlines()
        start = header.index("% rated")
        self.assertEqual(line[start:start + len("% rated")].strip(), "-")
        self.assertIn("rated Ah not set", self.html("norate"))

    def test_no_finished_tests_gives_header_only(self):
        p = self.store.add_pack("empty", "li-ion", rated_ah=10.0)
        self.store.begin_test(p.id, "2026-09-02T10:00:00Z", 5.0, 42.0, 0, "e")
        rows = history_rows(self.store, "empty")
        self.assertEqual(rows, [])
        self.assertEqual(len(format_table(rows).splitlines()), 1)
        html = self.html("empty")
        self.assertNotIn("<polyline", html)
        self.assertIn("no finished tests", html)

    def test_unfinished_tests_are_skipped_and_logged(self):
        self.addCleanup(close_log)
        log = make_log(self.tmp.name)
        history_rows(self.store, "rad52")
        log.close()
        self.assertIn("skipped 1 unfinished", log.path.read_text(encoding="utf-8"))

    def test_user_strings_are_escaped(self):
        p = self.store.add_pack("<b>&pack", "other", rated_ah=5.0, notes="<i>spare</i>")
        add_finished(self.store, p.id, "05", 4.0)
        html = self.html("<b>&pack")
        self.assertIn("&lt;b&gt;&amp;pack discharge history", html)
        self.assertIn("&lt;i&gt;spare&lt;/i&gt;", html)
        self.assertNotIn("<b>", html)
        self.assertNotIn("<i>", html)

    # R39: a pack_lost test (pack unplugged, or its BMS cut off) is listed and marked, but it is
    # not a capacity result, so chart B leaves it out

    def lost_pack(self):
        """Pack 'lost' (rated 10 Ah): cutoff on the 1st, pack_lost on the 10th, cutoff on the 20th."""
        p = self.store.add_pack("lost", "li-ion", rated_ah=10.0)
        return [add_finished(self.store, p.id, "01", 9.0),
                add_finished(self.store, p.id, "10", 0.5, reason="pack_lost"),
                add_finished(self.store, p.id, "20", 8.8)]

    def test_pack_lost_tests_are_history_rows_with_percent_of_rated(self):
        self.lost_pack()
        rows = history_rows(self.store, "lost")
        self.assertEqual([r.end_reason for r in rows], ["cutoff", "pack_lost", "cutoff"])
        self.assertAlmostEqual(rows[1].pct_of_rated, 5.0)
        header, *lines = format_table(rows).splitlines()
        self.assertEqual([line.split()[-1] for line in lines], ["cutoff", "pack_lost", "cutoff"])

    def test_chart_b_leaves_out_pack_lost_tests_and_says_so_below_it(self):
        first, lost, last = self.lost_pack()
        html = self.html("lost")
        chart_a, chart_b = SVG_RE.findall(html)
        self.assertEqual(chart_a.count("<polyline"), 3)   # chart A still draws the pack_lost curve
        self.assertTrue([t for t in TITLE_RE.findall(chart_a) if "test %d:" % lost in t])
        self.assertEqual(chart_b.count('class="dot"'), 2)
        titles_b = [t for t in TITLE_RE.findall(chart_b) if "test " in t]
        self.assertEqual(len(titles_b), 2)
        self.assertFalse([t for t in titles_b if "test %d:" % lost in t], titles_b)
        ticks_b = [b for a, b in TEXT_RE.findall(chart_b) if 'class="tick"' in a and "-" in b]
        self.assertNotIn("2026-09-10", ticks_b)
        section_b = html[html.index("<h2>Delivered Ah by date</h2>"):]
        section_b = section_b[:section_b.index("</section>")]
        self.assertTrue(section_b.rstrip().endswith('</svg>\n<p class="chart-note">pack_lost tests are not plotted'
                                                    ' here</p>'), section_b[-200:])

    def test_no_chart_b_note_without_pack_lost_tests(self):
        self.assertNotIn("not plotted here", self.html())

    def test_only_pack_lost_tests_leave_chart_b_empty_with_the_note(self):
        p = self.store.add_pack("gone", "li-ion", rated_ah=10.0)
        add_finished(self.store, p.id, "10", 0.5, reason="pack_lost")
        html = self.html("gone")
        self.assertEqual(len(SVG_RE.findall(html)), 1)   # chart A only
        self.assertIn('<p class="empty">no capacity to plot</p>\n<p class="chart-note">pack_lost tests are not'
                      ' plotted here</p>', html)

    # R41: a running sample under the pack-lost floor (1.00 V) does not stretch chart A's shared axis

    def chart_a(self, pack):
        """Chart A's volt tick values and its polylines as {test id: [(x, y), ...]}."""
        chart = SVG_RE.findall(self.html(pack))[0]
        ticks = [float(b) for a, b in TEXT_RE.findall(chart)
                 if 'class="tick"' in a and 'text-anchor="end"' in a and b != "V"]
        lines = {}
        for points, title in re.findall(r'<polyline class="line[^"]*" points="([^"]+)"><title>([^<]*)</title>', chart):
            tid = int(re.search(r"test (\d+):", title).group(1))
            lines[tid] = [tuple(map(float, p.split(","))) for p in points.split()]
        return ticks, lines

    def test_chart_a_axis_ignores_samples_below_the_pack_lost_floor(self):
        first, lost, last = self.lost_pack()   # every curve runs 54 -> 52 V
        ticks_before, lines_before = self.chart_a("lost")
        self.store.append_sample(lost, SampleRow(lost, 6.0, 0.99, 0.0, 0.25, 50.0, 0.25, "active"))
        self.store.append_sample(lost, SampleRow(lost, 8.0, 0.00, 0.0, 0.25, 50.0, 0.25, "active"))   # pack gone
        ticks, lines = self.chart_a("lost")
        self.assertEqual(ticks, ticks_before)
        self.assertEqual(ticks[0], 52.0)
        self.assertEqual(len(lines[lost]), 3)                      # the two collapsed samples are not drawn
        self.assertEqual([y for _, y in lines[lost]], [y for _, y in lines_before[lost]])
        self.assertTrue(all(24 <= y <= 320 for pts in lines.values() for _, y in pts))
        self.assertEqual([s.volts for s in self.store.samples(lost)][-2:], [0.99, 0.00])   # still stored
        csv_path = Path(self.tmp.name) / "lost.csv"
        self.assertEqual(self.store.export_csv(lost, csv_path), 5)                        # and exported
        self.assertTrue(csv_path.read_text().splitlines()[-1].startswith("8.0,0.0,"))

    def test_html_table_marks_pack_lost_in_the_end_column(self):
        first, lost, last = self.lost_pack()
        html = self.html("lost")
        rows = re.findall(r"<tr><td class=\"num\">(\d+)</td>.*?</tr>", html)
        self.assertEqual(rows, [str(first), str(lost), str(last)])
        ends = re.findall(r"<td( class=\"lost\")?>([^<]*)</td></tr>", html)
        self.assertEqual(ends, [("", "cutoff"), (' class="lost"', "pack_lost: not a capacity result"),
                                ("", "cutoff")])

    def test_pack_lost_mark_and_note_wear_ink_colours_only(self):
        self.lost_pack()
        html = self.html("lost")
        for selector in (".lost", ".chart-note"):
            rules = [body for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", html)
                     if selector in [s.strip() for s in sels.split(",")]]
            self.assertTrue(rules, selector)
            for body in rules:
                for colour in re.findall(r"(?<![-\w])color:([^;]+)", body):
                    self.assertIn(colour.strip(), ("var(--ink)", "var(--ink2)"), (selector, body))

    def test_unknown_pack_raises_store_error(self):
        from ebctool.store import StoreError
        with self.assertRaises(StoreError):
            history_rows(self.store, "nope")
        with self.assertRaises(StoreError):
            write_html(self.store, "nope", Path(self.tmp.name) / "never" / "x.html")
        self.assertFalse((Path(self.tmp.name) / "never").exists())

    # chart anatomy: two inline 800x360 charts, text in ink colours, end labels, nothing outside the box

    def test_two_charts_with_fixed_viewbox(self):
        svgs = SVG_RE.findall(self.html())
        self.assertEqual(len(svgs), 2)
        for svg in svgs:
            self.assertIn('viewBox="0 0 800 360"', svg)
        self.assertIn("Voltage under load", self.html())
        self.assertIn("Delivered Ah by date", self.html())

    def test_text_never_wears_a_series_colour(self):
        html = self.big_html()
        for attrs, _ in TEXT_RE.findall(html):
            self.assertNotRegex(attrs, r'class="[^"]*\bs[1-8]\b|--s[1-8]|fill="#', attrs)

    def test_each_coloured_line_has_a_dated_end_label(self):
        chart_a = SVG_RE.findall(self.big_html())[0]
        labels = [body for _, body in TEXT_RE.findall(chart_a)]
        for day in range(3, 11):
            self.assertIn("2026-08-%02d" % day, labels)
        for day in (1, 2):   # the folded older tests carry no end label
            self.assertNotIn("2026-08-%02d" % day, labels)
        self.assertEqual(chart_a.count("<polyline"), 10)

    def test_all_svg_coordinates_inside_viewbox(self):
        for html in (self.html(), self.big_html()):
            for svg in SVG_RE.findall(html):
                for name, value in re.findall(r'\b(x|y|x1|y1|x2|y2|cx|cy)="(-?[\d.]+)"', svg):
                    limit = 800 if name.startswith("x") or name == "cx" else 360
                    self.assertTrue(0 <= float(value) <= limit, (name, value))
                for points in re.findall(r'points="([^"]+)"', svg):
                    for pair in points.split():
                        x, y = map(float, pair.split(","))
                        self.assertTrue(0 <= x <= 800 and 0 <= y <= 360, pair)

    def test_end_labels_fan_out_in_end_order_without_crossing(self):
        from ebctool.report import LABEL_STEP, _crosses, _place_labels
        ends = [(600.0 - 10 * i, 290.0 + (i % 3) * 0.7) for i in range(8)]   # bunched at a cutoff
        ys = _place_labels(ends, 696.0, 30.0, 320.0)
        self.assertEqual(sorted(range(8), key=lambda i: ys[i]), list(range(7, -1, -1)))   # leftmost on top
        self.assertTrue(all(b - a >= LABEL_STEP - 1e-9 for a, b in zip(sorted(ys), sorted(ys)[1:])))
        scattered = [(80.0 + 73 * i % 600, 40.0 + 151 * i % 280) for i in range(8)]
        for column in (ends, scattered):
            ys = _place_labels(column, 696.0, 30.0, 320.0)
            self.assertTrue(all(30.0 <= y <= 320.0 for y in ys))
            for a in range(8):
                for b in range(a + 1, 8):
                    self.assertFalse(_crosses(column[a], (696.0, ys[a]), column[b], (696.0, ys[b])))

    def big_html(self):
        """Ten tests on pack 'many' with curves of different length, so eight colour and two fold."""
        p = self.store.add_pack("many", "li-ion", cells_series=14, rated_ah=17.5)
        for n in range(10):
            tid = self.store.begin_test(p.id, "2026-08-%02dT10:00:00Z" % (n + 1), 5.0, 42.0, 0, "m%d" % n)
            steps = 30 - n
            for k in range(steps + 1):
                self.store.append_sample(tid, SampleRow(tid, 60.0 * k, 57.0 - 15.0 * k / steps, 5.0,
                                                        5.0 * k / 60, 0, 0, "active"))
            self.store.finish_test(tid, "x", "cutoff", 60.0 * steps, 5.0 * steps / 60, 100.0, 1.0, 57.0, 42.0,
                                   "m", "f")
        return self.html("many", "many.html")


class HistoryCommandTests(unittest.TestCase):
    """history --pack NAME [--html PATH] through run.main."""

    def setUp(self):
        use_tz(self, "America/Chicago")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(close_log)
        self.root = Path(self.tmp.name)
        self.db = self.root / "db" / "t.db"
        self.cfg = self.root / "config.ini"
        self.cfg.write_text("[run]\nlog_dir = %s\ndb_path = %s\n" % (self.root / "logs", self.db))
        with Store(self.db) as st:
            p = st.add_pack("rad52", "li-ion", cells_series=14, rated_ah=10.0)
            for day, ah in (("01", 9.0), ("15", 8.5)):
                add_finished(st, p.id, day, ah)
            st.begin_test(p.id, "2026-09-20T10:00:00Z", 5.0, 42.0, 0, "unfinished")
            st.add_pack("fresh", "li-ion")

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = R.main(["--config", str(self.cfg), *args], stdout=out, stdin=io.StringIO(""))
        return code, out.getvalue(), err.getvalue()

    def test_prints_table_oldest_first(self):
        code, out, err = self.main("history", "--pack", "rad52")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("% rated", lines[0])
        self.assertIn("started UTC", lines[0]); self.assertIn("started local", lines[0])
        self.assertIn("2026-09-01 10:00Z", lines[1]); self.assertIn("2026-09-01 05:00 CDT", lines[1])
        self.assertIn("90.0", lines[1])
        self.assertIn("2026-09-15 10:00Z", lines[2]); self.assertIn("2026-09-15 05:00 CDT", lines[2])
        self.assertIn("85.0", lines[2])

    def test_html_writes_file_in_new_directory_and_prints_path(self):
        path = self.root / "out" / "nested" / "rad52.html"
        code, out, _ = self.main("history", "--pack", "rad52", "--html", str(path))
        self.assertEqual(code, 0)
        self.assertTrue(path.is_file())
        self.assertIn("rad52 discharge history", path.read_text(encoding="utf-8"))
        self.assertEqual(out.splitlines()[-1], "wrote %s" % path)
        self.assertEqual(len(out.splitlines()), 4)   # table header + 2 rows + path line

    def test_unknown_pack_exits_3(self):
        path = self.root / "out" / "x.html"
        code, out, err = self.main("history", "--pack", "nope", "--html", str(path))
        self.assertEqual(code, 3)
        self.assertEqual(out, "error: no pack 'nope'\n")
        self.assertEqual(err, "")
        self.assertFalse(path.parent.exists())

    def test_no_finished_tests_prints_header_only(self):
        code, out, _ = self.main("history", "--pack", "fresh")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("started local", out)


if __name__ == "__main__":
    unittest.main()
