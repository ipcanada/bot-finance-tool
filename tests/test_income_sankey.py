"""Spec parsing and an optional render smoke test for the income-statement Sankey.

The render test skips when matplotlib is not installed. Parsing does not need it.
"""
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import fin
import income_sankey

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_JSON = ROOT / "examples" / "spacex-q3-2026-income-sankey.json"
EXAMPLE_CSV = ROOT / "examples" / "spacex-q3-2026-income-sankey.csv"

EXPECTED_LABELS = {
    "segments": [
        "Space\n$1.8B  +6.1% Y/Y",
        "Connectivity\n$5.2B  +69% Y/Y",
        "SI\n$7.6B  +830% Y/Y",
    ],
    "revenue": "Revenue\n$14.6B  +161% Y/Y",
    "gross_profit": "Gross profit  $9.1B\n62.4% margin  +10.9pp Y/Y",
    "cost_of_revenue": "Cost of revenue\n($5.5B)  +103% Y/Y",
    "operating_expenses": "Operating expenses  ($6.3B)\n43.3% of rev  +101% Y/Y",
    "children": [
        "Research & development\n($4.6B)  31.5% of rev",
        "SG&A\n($1.7B)  11.7% of rev",
        "Restructuring  ($3M)",
    ],
    "operating_profit": "Operating profit  $2.8B\n19.1% margin  +24.0pp Y/Y",
    "bridges": [
        "Other income, net\n$1.2B (see note)",
        "Tax benefit  $0.4B",
    ],
    "net_profit": "Net profit\n$4.4B  30.2% margin\n+49.1pp Y/Y",
}


class ParseTests(unittest.TestCase):
    def test_example_json_labels_and_ties(self):
        spec = income_sankey.load_spec(EXAMPLE_JSON)
        self.assertEqual(spec["currency"], "USD")
        self.assertEqual(spec["unit"], "millions")
        self.assertFalse(spec["draft"])
        self.assertEqual(len(spec["footer"]), 4)
        labels = income_sankey.build_labels(spec)
        self.assertEqual(labels, EXPECTED_LABELS)
        self.assertEqual(sum(item["value"] for item in spec["segments"]), spec["revenue"]["value"])
        self.assertAlmostEqual(
            spec["revenue"]["value"] - spec["cost_of_revenue"]["value"],
            spec["gross_profit"]["value"],
        )
        self.assertAlmostEqual(
            spec["gross_profit"]["value"] - spec["operating_expenses"]["value"],
            spec["operating_profit"]["value"],
        )
        self.assertAlmostEqual(
            sum(item["value"] for item in spec["operating_expenses"]["children"]),
            spec["operating_expenses"]["value"],
        )
        self.assertAlmostEqual(
            spec["operating_profit"]["value"] + sum(item["value"] for item in spec["bridges"]),
            spec["net_profit"]["value"],
        )
        self.assertEqual(spec["notes"], [])
        self.assertTrue(income_sankey.positive_topology(spec))

    def test_example_csv_matches_json(self):
        json_spec = income_sankey.load_spec(EXAMPLE_JSON)
        csv_spec = income_sankey.load_spec(EXAMPLE_CSV)
        self.assertEqual(income_sankey.build_labels(csv_spec), income_sankey.build_labels(json_spec))
        self.assertEqual(csv_spec["footer"], json_spec["footer"])
        self.assertEqual(csv_spec["byline_color"], "#A97EFE")
        self.assertEqual(csv_spec["revenue"]["value"], json_spec["revenue"]["value"])

    def test_derives_missing_profit_lines(self):
        spec = income_sankey.normalize({
            "title": "Derived",
            "revenue": {"label": "Revenue", "value": 100, "prior": 80},
            "cost_of_revenue": {"value": 40, "prior": 30},
            "operating_expenses": {"value": 25, "children": [{"label": "R&D", "value": 25}]},
        })
        self.assertAlmostEqual(spec["gross_profit"]["value"], 60)
        self.assertAlmostEqual(spec["operating_profit"]["value"], 35)
        self.assertAlmostEqual(spec["net_profit"]["value"], 35)
        self.assertTrue(any("Gross profit derived" in note for note in spec["notes"]))

    def test_yy_suppressed_when_prior_is_not_positive(self):
        spec = income_sankey.normalize({
            "revenue": {"label": "Revenue", "value": 10, "prior": 0},
            "cost_of_revenue": {"value": 4, "prior": 4},
            "gross_profit": {"value": 6, "prior": -1},
            "operating_expenses": {"value": 1, "prior": 1},
            "operating_profit": {"value": 5, "prior": -2},
            "net_profit": {"value": 5, "prior": -2},
        })
        labels = income_sankey.build_labels(spec)
        self.assertEqual(labels["revenue"], "Revenue\n$10M")
        self.assertNotIn("n/m", labels["operating_profit"])

    def test_loss_topology_is_flagged(self):
        spec = income_sankey.normalize({
            "revenue": {"value": 1000},
            "cost_of_revenue": {"value": 400},
            "gross_profit": {"value": 600},
            "operating_expenses": {"value": 800},
            "operating_profit": {"label": "Operating profit", "value": -200},
            "net_profit": {"label": "Net profit", "value": -180},
            "bridges": [{"label": "Tax expense", "value": -30}, {"label": "Other income, net", "value": 50}],
        })
        self.assertFalse(income_sankey.positive_topology(spec))
        labels = income_sankey.build_labels(spec)
        self.assertIn("($", labels["operating_profit"])
        self.assertIn("($", labels["net_profit"])

    def test_bad_spec_errors(self):
        with self.assertRaises(income_sankey.SpecError):
            income_sankey.normalize({"title": "empty"})
        with self.assertRaises(income_sankey.SpecError):
            income_sankey.load_spec(ROOT / "does-not-exist.json")

    def test_reference_layout_anchors(self):
        spec = income_sankey.load_spec(EXAMPLE_JSON)
        lay = income_sankey.layout_geometry(spec, 1280, 720)
        self.assertEqual(lay["x"], [232.0, 392.0, 598.0, 806.0, 1040.0])
        self.assertEqual(lay["revenue_y"], 240.0)
        self.assertEqual(lay["op_y"], 270.0)
        self.assertEqual(lay["np_y"], 108.0)
        self.assertFalse(lay["fitted"])
        self.assertTrue(lay["positive"])
        self.assertEqual(lay["child_gaps"], [10.0, 10.0, 10.0])
        self.assertAlmostEqual(lay["S"] * spec["revenue"]["value"], 371.6625)

    def test_short_child_labels_get_extra_gap(self):
        spec = income_sankey.normalize({
            "revenue": {"value": 10000},
            "cost_of_revenue": {"value": 4000},
            "gross_profit": {"value": 6000},
            "operating_expenses": {
                "value": 1800,
                "children": [
                    {"label": "Research & development", "value": 1000},
                    {"label": "SG&A", "value": 500},
                    {"label": "Other operating", "value": 300},
                ],
            },
            "operating_profit": {"value": 4200},
            "net_profit": {"value": 4200},
        })
        lay = income_sankey.layout_geometry(spec, 1280, 720)
        self.assertGreater(lay["child_gaps"][0], 10)
        self.assertGreater(lay["child_gaps"][1], 10)

    def test_no_segments_shifts_columns(self):
        spec = income_sankey.normalize({
            "revenue": {"value": 100},
            "cost_of_revenue": {"value": 40},
            "operating_expenses": {"value": 20},
        })
        lay = income_sankey.layout_geometry(spec, 1280, 720)
        self.assertEqual(lay["x"][1], 232.0)

    def test_new_files_have_no_sec_contact(self):
        for rel in (
            "income_sankey.py",
            "examples/spacex-q3-2026-income-sankey.json",
            "examples/spacex-q3-2026-income-sankey.csv",
            "fonts/OFL.txt",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("SEC_USER_AGENT", text, rel)
            self.assertIsNone(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text), rel)


class DraftTests(unittest.TestCase):
    def test_yahoo_shaped_payload_becomes_a_labeled_draft(self):
        data = {
            "currency": "USD",
            "income": {
                "2026-09-30": {
                    "Total Revenue": 10_000_000_000,
                    "Cost Of Revenue": 4_000_000_000,
                    "Gross Profit": 6_000_000_000,
                    "Research And Development": 1_000_000_000,
                    "Selling General And Administration": 500_000_000,
                    "Operating Expense": 1_800_000_000,
                    "Operating Income": 4_200_000_000,
                    "Tax Provision": 800_000_000,
                    "Interest Expense": 100_000_000,
                    "Net Income": 3_500_000_000,
                },
                "2025-09-30": {
                    "Total Revenue": 8_000_000_000,
                    "Cost Of Revenue": 3_500_000_000,
                    "Gross Profit": 4_500_000_000,
                    "Research And Development": 900_000_000,
                    "Selling General And Administration": 400_000_000,
                    "Operating Expense": 1_500_000_000,
                    "Operating Income": 3_000_000_000,
                    "Tax Provision": 600_000_000,
                    "Net Income": 2_500_000_000,
                },
            },
        }
        spec = income_sankey.spec_from_financials(
            data,
            symbol="ACME",
            period="quarterly",
            source="yahoo",
            source_url="https://finance.yahoo.com/quote/ACME/financials",
            retrieved_at="2026-11-28T00:00:00+00:00",
        )
        self.assertTrue(spec["draft"])
        self.assertEqual(spec["revenue"]["value"], 10000)
        self.assertEqual(spec["revenue"]["prior"], 8000)
        self.assertEqual(spec["net_profit"]["value"], 3500)
        footer = "\n".join(spec["footer"])
        self.assertIn("Source: yahoo https://finance.yahoo.com/quote/ACME/financials", footer)
        self.assertIn("retrieved 2026-11-28T00:00:00+00:00", footer)
        self.assertIn("Aggregator statements are normalized", footer)
        self.assertIn("Draft for review; not investment advice.", footer)
        self.assertIn("interest expense", footer.lower())
        self.assertTrue(any(item["label"] == "Other operating" for item in spec["operating_expenses"]["children"]))
        self.assertAlmostEqual(
            spec["operating_profit"]["value"] + sum(item["value"] for item in spec["bridges"]),
            spec["net_profit"]["value"],
        )

    def test_alphavantage_shaped_rows(self):
        data = {
            "currency": "USD",
            "income": [
                {
                    "fiscalDateEnding": "2025-12-31",
                    "totalRevenue": "5000000000",
                    "costOfRevenue": "2000000000",
                    "grossProfit": "3000000000",
                    "operatingExpenses": "1000000000",
                    "operatingIncome": "2000000000",
                    "incomeTaxExpense": "400000000",
                    "netIncome": "1700000000",
                },
                {
                    "fiscalDateEnding": "2024-12-31",
                    "totalRevenue": "4000000000",
                    "costOfRevenue": "1800000000",
                    "grossProfit": "2200000000",
                    "operatingExpenses": "900000000",
                    "operatingIncome": "1300000000",
                    "incomeTaxExpense": "300000000",
                    "netIncome": "1100000000",
                },
            ],
        }
        spec = income_sankey.spec_from_financials(
            data, symbol="ACME", period="annual", source="alphavantage",
            source_url="https://www.alphavantage.co/query?function=INCOME_STATEMENT&symbol=ACME",
            retrieved_at="2026-11-28T00:00:00+00:00",
        )
        self.assertEqual(spec["revenue"]["value"], 5000)
        self.assertEqual(spec["revenue"]["prior"], 4000)
        self.assertTrue(spec["draft"])
        self.assertIn("alphavantage", spec["footer"][1])
        self.assertNotIn("apikey", spec["footer"][1].lower())


class RevenueColorTests(unittest.TestCase):
    def test_ciede2000_reference_pair(self):
        # Sharma, Wu, Dalal: the published CIEDE2000 pair is about 2.0425.
        delta = income_sankey.ciede2000((50, 2.6772, -79.7751), (50, 0, -82.7485))
        self.assertAlmostEqual(delta, 2.0425, places=3)
        self.assertEqual(income_sankey.ciede2000_rgb((1, 2, 3), (1, 2, 3)), 0)

    def test_no_logo_and_white_logo_stay_navy(self):
        none = income_sankey.resolve_revenue_color(None)
        self.assertEqual(none["final"], "#14213D")
        self.assertIsNone(none["detected"])
        self.assertEqual(none["source"], "navy")
        for rgb in ((255, 255, 255), (248, 248, 248), (250, 250, 252)):
            report = income_sankey.resolve_revenue_color(rgb)
            self.assertEqual(report["final"], "#14213D", rgb)
            self.assertEqual(report["detected"], income_sankey.rgb_to_hex(rgb))
            self.assertEqual(report["source"], "navy")

    def test_near_black_is_kept_when_contrast_holds(self):
        black = income_sankey.resolve_revenue_color((0, 0, 0))
        self.assertEqual(black["final"], "#000000")
        self.assertEqual(black["source"], "logo")
        dark = income_sankey.resolve_revenue_color((30, 30, 30))
        self.assertEqual(dark["final"], "#1E1E1E")
        self.assertGreaterEqual(income_sankey.contrast_ratio((30, 30, 30)), income_sankey.MIN_CONTRAST)

    def test_cost_red_is_shifted_clear_of_cost_and_profit(self):
        report = income_sankey.resolve_revenue_color((0xD9, 0x53, 0x4F))
        final = income_sankey.parse_hex_color(report["final"])
        self.assertEqual(report["source"], "shifted")
        self.assertNotEqual(report["final"], "#D9534F")
        self.assertGreaterEqual(
            income_sankey.ciede2000_rgb(final, (0xD9, 0x53, 0x4F)),
            income_sankey.MIN_DELTA_E,
        )
        self.assertGreaterEqual(
            income_sankey.ciede2000_rgb(final, (0x2E, 0x9E, 0x5B)),
            income_sankey.MIN_DELTA_E,
        )
        self.assertGreaterEqual(income_sankey.contrast_ratio(final), income_sankey.MIN_CONTRAST)

    def test_explicit_color_wins_even_when_it_matches_cost_red(self):
        report = income_sankey.resolve_revenue_color((0xD9, 0x53, 0x4F), explicit="#D9534F")
        self.assertEqual(report["final"], "#D9534F")
        self.assertEqual(report["source"], "explicit")
        self.assertEqual(report["detected"], "#D9534F")
        with self.assertRaises(income_sankey.SpecError):
            income_sankey.resolve_revenue_color(None, explicit="navy")

    def test_dominant_color_ignores_transparency_and_background(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        image = np.zeros((12, 12, 4), dtype=np.uint8)
        image[:, :] = (0xEC, 0xEC, 0xEC, 255)
        image[2:10, 2:6] = (0, 0, 0, 255)
        image[2:10, 6:10] = (0, 0, 0, 0)
        self.assertEqual(income_sankey.dominant_opaque_color(image), (0, 0, 0))
        white = np.full((4, 4, 4), 255, dtype=np.uint8)
        self.assertEqual(income_sankey.dominant_opaque_color(white), (255, 255, 255))
        self.assertEqual(income_sankey.resolve_revenue_color((255, 255, 255))["final"], "#14213D")


class CommandTests(unittest.TestCase):
    def test_version_and_doctor_report_charting(self):
        self.assertEqual(fin.__version__, "1.3.0")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fin.main(["doctor"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        charting = payload["data"]["charting"]
        self.assertIn("matplotlib", charting)
        self.assertIn("svg_logos", charting)
        self.assertIn("Open Sans", charting["font"])

    def test_missing_matplotlib_prints_install_hint(self):
        original = income_sankey.matplotlib_available
        income_sankey.matplotlib_available = lambda: False
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = fin.main([
                    "chart", "income-sankey",
                    "--spec", str(EXAMPLE_JSON),
                    "--out", str(ROOT / "should-not-render.png"),
                ])
            payload = json.loads(buf.getvalue())
        finally:
            income_sankey.matplotlib_available = original
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("pip install matplotlib", json.dumps(payload))
        self.assertFalse((ROOT / "should-not-render.png").exists())

    def test_spec_out_without_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "draft.json"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = fin.main([
                    "chart", "income-sankey",
                    "--spec", str(EXAMPLE_JSON),
                    "--spec-out", str(dest),
                    "--no-render",
                ])
            payload = json.loads(buf.getvalue())
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["data"]["rendered"])
            saved = json.loads(dest.read_text())
            self.assertEqual(saved["revenue"]["value"], 14575)


@unittest.skipUnless(income_sankey.matplotlib_available(), "matplotlib not installed")
class RenderTests(unittest.TestCase):
    def test_render_smoke_and_lockup(self):
        import numpy as np
        from PIL import Image

        spec = income_sankey.load_spec(EXAMPLE_JSON)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            icon = tmp / "icon.png"
            logo = tmp / "logo.png"
            out = tmp / "chart.png"
            svg = tmp / "chart.svg"
            Image.new("RGBA", (64, 64), (255, 0, 255, 255)).save(icon)
            Image.new("RGBA", (400, 50), (255, 136, 0, 255)).save(logo)
            result = income_sankey.render_chart(
                spec, out, svg_path=svg, byline_icon=icon, logo=logo, logo_width=320,
            )
            self.assertEqual(result["font"], "Open Sans")
            self.assertTrue(result["positive_topology"])
            self.assertTrue(svg.is_file() and svg.stat().st_size > 1000)
            image = Image.open(out).convert("RGBA")
            self.assertEqual(image.size, (1280, 720))
            pixels = np.array(image)
            background = np.array([0xEC, 0xEC, 0xEC], dtype=np.int16)
            ink = np.max(np.abs(pixels[..., :3].astype(np.int16) - background), axis=2) > 12
            self.assertGreater(ink.mean(), 0.02)

            # Title descenders end well above the byline icon. The reference gap is 23px.
            magenta = (np.abs(pixels[..., 0].astype(int) - 255) < 8) & (pixels[..., 1] < 30) & (np.abs(pixels[..., 2].astype(int) - 255) < 8)
            icon_rows = np.where(magenta.any(axis=1))[0]
            self.assertGreater(len(icon_rows), 10)
            icon_top = int(icon_rows.min())
            title_band = ink[:icon_top, :800]
            title_rows = np.where(title_band.any(axis=1))[0]
            title_bottom = int(title_rows.max())
            gap = icon_top - title_bottom - 1
            self.assertGreaterEqual(gap, 20, f"title-to-byline gap {gap}px")
            self.assertLessEqual(gap, 26, f"title-to-byline gap {gap}px")

            orange = (np.abs(pixels[..., 0].astype(int) - 255) < 8) & (np.abs(pixels[..., 1].astype(int) - 136) < 12) & (pixels[..., 2] < 30)
            # Revenue ink now follows the logo, so measure the wordmark in the header only.
            header_orange = orange.copy()
            header_orange[80:] = False
            logo_ys, logo_xs = np.where(header_orange)
            self.assertGreater(len(logo_xs), 100)
            logo_right = int(logo_xs.max())
            logo_cy = (int(logo_ys.min()) + int(logo_ys.max())) / 2
            title_cols = np.where(title_band.any(axis=0))[0]
            title_cy = (int(title_rows.min()) + title_bottom) / 2
            body = ink[90:660].copy()
            # Ignore the orange logo if it falls in the body band.
            body_orange = orange[90:660]
            body[body_orange] = False
            body_xs = np.where(body.any(axis=0))[0]
            chart_right = int(body_xs.max())
            self.assertLessEqual(abs(logo_right - chart_right), 2, f"logo right {logo_right} chart {chart_right}")
            self.assertLessEqual(abs(logo_cy - title_cy), 2.5, f"logo cy {logo_cy} title cy {title_cy}")

            # Green profit and red expense both show up.
            green = (pixels[..., 1] > 140) & (pixels[..., 0] < 80) & (pixels[..., 2] < 120)
            red = (pixels[..., 0] > 180) & (pixels[..., 1] < 120) & (pixels[..., 2] < 120)
            self.assertGreater(int(green.sum()), 500)
            self.assertGreater(int(red.sum()), 500)

    def test_loss_render_does_not_crash_and_uses_red(self):
        import numpy as np
        from PIL import Image

        spec = income_sankey.normalize({
            "title": "Loss Co Q1",
            "byline": "Reviewer",
            "revenue": {"label": "Revenue", "value": 1000, "prior": 900},
            "cost_of_revenue": {"label": "Cost of revenue", "value": 400, "prior": 350},
            "gross_profit": {"label": "Gross profit", "value": 600, "prior": 550},
            "operating_expenses": {
                "label": "Operating expenses",
                "value": 800,
                "prior": 500,
                "children": [
                    {"label": "Research & development", "value": 500},
                    {"label": "SG&A", "value": 300},
                ],
            },
            "operating_profit": {"label": "Operating profit", "value": -200, "prior": 50},
            "bridges": [{"label": "Tax expense", "value": -40}],
            "net_profit": {"label": "Net profit", "value": -240, "prior": 40},
            "footer": ["Source: fixture, not a filing.", "Draft for review; not investment advice."],
        })
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "loss.png"
            result = income_sankey.render_chart(spec, out)
            self.assertFalse(result["positive_topology"])
            pixels = np.array(Image.open(out).convert("RGBA"))
            red = (pixels[..., 0] > 180) & (pixels[..., 1] < 120) & (pixels[..., 2] < 120)
            self.assertGreater(int(red.sum()), 200)

    def test_explicit_flag_wins_and_logo_pixels_stay(self):
        import numpy as np
        from PIL import Image

        spec = income_sankey.load_spec(EXAMPLE_JSON)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            logo = tmp / "logo.png"
            out = tmp / "chart.png"
            image = Image.new("RGBA", (200, 40), (0, 0, 0, 255))
            draw = np.array(image)
            draw[:, :100] = (180, 20, 40, 255)
            draw[:, 100:] = (10, 40, 160, 255)
            Image.fromarray(draw).save(logo)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = fin.main([
                    "chart", "income-sankey",
                    "--spec", str(EXAMPLE_JSON),
                    "--logo", str(logo),
                    "--logo-width", "200",
                    "--revenue-color", "#112233",
                    "--out", str(out),
                ])
            payload = json.loads(buf.getvalue())
            self.assertEqual(rc, 0, buf.getvalue())
            colors = payload["data"]["revenue_color"]
            self.assertEqual(colors["final"], "#112233")
            self.assertEqual(colors["source"], "explicit")
            self.assertIn("detected", colors)
            auto = income_sankey.render_chart(spec, tmp / "auto.png", logo=logo, logo_width=200)
            final = income_sankey.parse_hex_color(auto["revenue_color"]["final"])
            detected = income_sankey.parse_hex_color(auto["revenue_color"]["detected"])
            self.assertGreaterEqual(income_sankey.ciede2000_rgb(final, (0xD9, 0x53, 0x4F)), income_sankey.MIN_DELTA_E)
            self.assertGreaterEqual(income_sankey.ciede2000_rgb(final, (0x2E, 0x9E, 0x5B)), income_sankey.MIN_DELTA_E)
            pixels = np.array(Image.open(tmp / "auto.png").convert("RGBA"))
            header = np.zeros(pixels.shape[:2], dtype=bool)
            header[:80] = True
            left = header & (pixels[..., 0] == 180) & (pixels[..., 1] == 20) & (pixels[..., 2] == 40)
            right = header & (pixels[..., 0] == 10) & (pixels[..., 1] == 40) & (pixels[..., 2] == 160)
            self.assertGreater(int(left.sum()), 400, "left logo color was recolored")
            self.assertGreater(int(right.sum()), 400, "right logo color was recolored")
            self.assertIn(detected, ((180, 20, 40), (10, 40, 160)))


if __name__ == "__main__":
    unittest.main()
