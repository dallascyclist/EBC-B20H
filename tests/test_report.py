import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from ebctool import run as R
from ebctool.report import format_table, history_rows, write_html
from ebctool.store import SampleRow, Store
from tests.support import close_log, make_log

SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S)
TEXT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S)


def add_finished(store, pack_id, day, ah, samples=3, ah_device=None, run="r"):
    """One finished test started on 2026-09-<day> with a short falling voltage curve."""
    tid = store.begin_test(pack_id, "2026-09-%sT10:00:00Z" % day, 5.0, 42.0, 0, run)
    for k in range(samples):
        store.append_sample(tid, SampleRow(tid, 2.0 * k, 54.0 - k, 5.0, ah * k / 2, 50.0 * k, ah * k / 2, "active"))
    store.finish_test(tid, "2026-09-%sT12:00:00Z" % day, "cutoff", 7200, ah, 400.0,
                      ah if ah_device is None else ah_device, 54.0, 42.1, "EBC-B20H", "3.08")
    return tid


class ReportTests(unittest.TestCase):
    def setUp(self):
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
        self.assertIn("date", text.splitlines()[0])
        self.assertEqual(len(text.splitlines()), 4)

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
        self.assertIn("2026-09-01", lines[1]); self.assertIn("90.0", lines[1])
        self.assertIn("2026-09-15", lines[2]); self.assertIn("85.0", lines[2])

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
        self.assertIn("date", out)


if __name__ == "__main__":
    unittest.main()
