#!/usr/bin/env python3
"""Income-statement Sankey chart.

Reads a CSV or JSON spec (segments, statement lines, labels, optional Y/Y and
margin annotations, footer) and draws an App-Economy-Insights-style flow:

    segments → revenue → gross profit / cost of revenue
             → operating profit / operating expenses → R&D, SG&A, …
             → other income and tax → net profit

Nothing in here is specific to one company. matplotlib, Pillow, and cairosvg
are imported only when a chart or a logo is actually drawn, so the rest of
fin.py runs without them.

The 1280×720 layout (column anchors, gaps, revenue-bar height, type sizes)
is the reference design. Other canvases scale it. A fitter shrinks the flow
when a taller stack would collide with the header or the footer.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"

MATPLOTLIB_HINT = (
    "Charting needs matplotlib (optional). Install it with: pip install matplotlib"
)
SVG_HINT = (
    "SVG logos need cairosvg (optional). Install it with: pip install cairosvg "
    "— or pass a PNG logo."
)
PILLOW_HINT = (
    "Byline icons and logos need Pillow (optional; matplotlib normally installs it). "
    "Install it with: pip install pillow"
)

# Reference canvas. REF_REVENUE_PX is the revenue-node height the gaps were
# designed around; every other magnitude uses the same pixels-per-unit scale.
REF_W = 1280.0
REF_H = 720.0
REF_REVENUE_PX = 371.6625

NAVY = "#14213D"
GREEN = "#2E9E5B"
RED = "#D9534F"
SEG = "#3B5B8C"
BG = "#ECECEC"
MUTED = "#555555"

_CURRENCY_SYMBOL = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CAD": "C$",
    "AUD": "A$",
    "CHF": "CHF ",
}
_TO_MILLIONS = {
    "millions": 1.0,
    "million": 1.0,
    "m": 1.0,
    "thousands": 0.001,
    "thousand": 0.001,
    "k": 0.001,
    "billions": 1000.0,
    "billion": 1000.0,
    "b": 1000.0,
    "ones": 1e-6,
    "units": 1e-6,
    "dollars": 1e-6,
}

# Statement-line names we recognize on Yahoo, Alpha Vantage, and Finnhub labels.
# Matching ignores case, spaces, and punctuation.
_LINE_KEYS = {
    "revenue": ["totalrevenue", "operatingrevenue", "revenue"],
    "cor": ["costofrevenue", "reconciledcostofrevenue", "costofgoodsandservicessold"],
    "gp": ["grossprofit"],
    "opex": ["operatingexpense", "totaloperatingexpenses"],
    "rd": ["researchanddevelopment", "researchdevelopment"],
    "sga": ["sellinggeneralandadministration", "sellinggeneralandadministrative"],
    "op": ["operatingincome", "totaloperatingincomeasreported", "ebit"],
    "ni": ["netincome", "netincomecommonstockholders", "netincomeincludingnoncontrollinginterests"],
    "tax": ["taxprovision", "incometaxexpense", "provisionforincometaxes"],
    "other": ["otherincomeexpense", "totalotherincomeexpensenet", "othernonoperatingincomeexpenses", "othernonoperatingincome"],
    "intinc": ["interestincome", "interestincomenonoperating"],
    "intexp": ["interestexpense", "interestexpensenonoperating"],
}


class SpecError(ValueError):
    """The spec is missing something the chart cannot invent."""


class ChartDependencyError(RuntimeError):
    """matplotlib, Pillow, or an SVG rasterizer is not installed."""


def matplotlib_available():
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def charting_status():
    """What `fin.py doctor` reports. Does not require matplotlib to be installed."""
    out = {}
    if matplotlib_available():
        import matplotlib
        out["matplotlib"] = f"optional, installed {matplotlib.__version__}"
    else:
        out["matplotlib"] = "optional, not installed (income-sankey needs it: pip install matplotlib)"
    try:
        import PIL
        out["pillow"] = f"optional, installed {PIL.__version__}"
    except ImportError:
        out["pillow"] = "optional, not installed (byline icons and logos need it: pip install pillow)"
    try:
        import cairosvg
        ver = getattr(cairosvg, "__version__", "")
        out["svg_logos"] = f"optional, cairosvg {ver}".strip()
    except ImportError:
        out["svg_logos"] = "optional, cairosvg not installed (SVG logos need it: pip install cairosvg; or pass a PNG)"
    regular = FONT_DIR / "OpenSans-400.ttf"
    license_file = FONT_DIR / "OFL.txt"
    if regular.is_file() and license_file.is_file():
        out["font"] = "Open Sans bundled (fonts/OFL.txt); DejaVu Sans is the fallback"
    elif regular.is_file():
        out["font"] = "Open Sans files present but fonts/OFL.txt is missing; DejaVu Sans is the fallback"
    else:
        out["font"] = "Open Sans files missing; charts fall back to DejaVu Sans"
    return out


# ---------------------------------------------------------------- formatting
def fmt_usd(millions, currency="USD"):
    """Short dollar form used on the reference chart. `millions` is in millions."""
    sym = _CURRENCY_SYMBOL.get((currency or "USD").upper(), f"{currency} ")
    magnitude = abs(millions)
    if magnitude >= 100:
        body = f"{magnitude / 1000:.1f}B"
    else:
        body = f"{magnitude:,.0f}M"
    return f"{sym}{body}"


def fmt_pct(x):
    """Signed percent, integer when |x| >= 10, else one decimal, half-up."""
    q = Decimal(str(abs(x))).quantize(
        Decimal("1") if abs(x) >= 10 else Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return ("+" if x >= 0 else "-") + f"{q}%"


def fmt_ppt(x):
    return ("+" if x >= 0 else "-") + f"{abs(x):.1f}pp"


def _yoy(current, prior):
    return (current / prior - 1.0) * 100.0


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        if text == "" or text.lower() in {"nan", "none", "null", "n/a", "na"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _norm_key(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _yy_text(item):
    """Annotation override, else computed growth. Blank when prior is missing or not positive."""
    if "yy" in item:
        return item["yy"] or None
    prior = item.get("prior")
    if prior is None or prior <= 0:
        return None
    return fmt_pct(_yoy(item["value"], prior))


def _margin_value(item, revenue):
    if "margin" in item:
        return _num(item["margin"])
    if revenue is None or revenue == 0:
        return None
    return item["value"] / revenue * 100.0


def _margin_pp(item, revenue, prior_revenue):
    if "margin_yy" in item:
        return _num(item["margin_yy"])
    prior = item.get("prior")
    if prior is None or revenue in (None, 0) or prior_revenue in (None, 0):
        return None
    return (item["value"] / revenue - prior / prior_revenue) * 100.0


def _amount(item, currency, parens):
    text = fmt_usd(item["value"], currency)
    if parens:
        return f"({text})"
    return text


def build_labels(spec):
    """Label strings for every node. Role decides the template; the spec supplies names and numbers."""
    currency = spec.get("currency") or "USD"
    revenue = spec["revenue"]["value"]
    prior_revenue = spec["revenue"].get("prior")

    def growth_line(item, parens=False):
        if item.get("text"):
            return item["text"]
        amount = _amount(item, currency, parens or item["value"] < 0)
        yy = _yy_text(item)
        if yy:
            return f"{item['label']}\n{amount}  {yy} Y/Y"
        return f"{item['label']}\n{amount}"

    def profit_line(item, kind):
        if item.get("text"):
            return item["text"]
        negative = item["value"] < 0
        amount = _amount(item, currency, negative)
        margin = _margin_value(item, revenue)
        pp = _margin_pp(item, revenue, prior_revenue)
        if kind == "net" and margin is not None and pp is not None and not negative:
            return f"{item['label']}\n{amount}  {margin:.1f}% margin\n{fmt_ppt(pp)} Y/Y"
        if kind != "net" and margin is not None and pp is not None and not negative:
            return f"{item['label']}  {amount}\n{margin:.1f}% margin  {fmt_ppt(pp)} Y/Y"
        parts_head = f"{item['label']}  {amount}" if kind != "net" else f"{item['label']}\n{amount}"
        extra = []
        if margin is not None:
            extra.append(f"{margin:.1f}% margin")
        if pp is not None:
            extra.append(f"{fmt_ppt(pp)} Y/Y")
        if not extra:
            return parts_head
        if kind == "net":
            return parts_head + "\n" + "  ".join(extra)
        return parts_head + "\n" + "  ".join(extra)

    def expense_line(item, of_revenue=False, two_spaces_before_amount=False):
        if item.get("text"):
            return item["text"]
        amount = _amount(item, currency, item["value"] >= 0)
        if item["value"] < 0:
            amount = f"-{fmt_usd(item['value'], currency)}"
        yy = _yy_text(item)
        share = None
        if of_revenue and revenue not in (None, 0):
            share = abs(item["value"]) / revenue * 100.0
        if of_revenue and share is not None and yy:
            gap = "  " if two_spaces_before_amount else " "
            return f"{item['label']}{gap}{amount}\n{share:.1f}% of rev  {yy} Y/Y"
        if yy:
            return f"{item['label']}\n{amount}  {yy} Y/Y"
        return f"{item['label']}\n{amount}"

    def child_line(item):
        if item.get("text"):
            return item["text"]
        if item["value"] < 0:
            amount = f"-{fmt_usd(item['value'], currency)}"
        else:
            amount = f"({fmt_usd(item['value'], currency)})"
        if abs(item["value"]) >= 100 and revenue not in (None, 0):
            share = abs(item["value"]) / revenue * 100.0
            return f"{item['label']}\n{amount}  {share:.1f}% of rev"
        return f"{item['label']}  {amount}"

    def bridge_line(item):
        if item.get("text"):
            return item["text"]
        if item["value"] < 0:
            amount = f"({fmt_usd(item['value'], currency)})"
        else:
            amount = fmt_usd(item["value"], currency)
        note = item.get("note")
        if note:
            return f"{item['label']}\n{amount} {note}"
        return f"{item['label']}  {amount}"

    return {
        "segments": [growth_line(item) for item in spec["segments"]],
        "revenue": growth_line(spec["revenue"]),
        "gross_profit": profit_line(spec["gross_profit"], "gross"),
        "cost_of_revenue": expense_line(spec["cost_of_revenue"]),
        "operating_expenses": expense_line(spec["operating_expenses"], of_revenue=True, two_spaces_before_amount=True),
        "children": [child_line(item) for item in spec["operating_expenses"]["children"]],
        "operating_profit": profit_line(spec["operating_profit"], "operating"),
        "bridges": [bridge_line(item) for item in spec["bridges"]],
        "net_profit": profit_line(spec["net_profit"], "net"),
    }


# ---------------------------------------------------------------- spec parsing
def _item(raw, default_label, scale):
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = {"value": raw}
    if not isinstance(raw, dict):
        raise SpecError(f"{default_label} must be an object with label and value")
    value = _num(raw.get("value"))
    if value is None:
        return None
    item = {"label": str(raw.get("label") or default_label), "value": value * scale}
    prior = _num(raw.get("prior")) if "prior" in raw else None
    if prior is not None:
        item["prior"] = prior * scale
    if raw.get("note"):
        item["note"] = str(raw["note"])
    if "yy" in raw and raw.get("yy") not in ("",):
        item["yy"] = None if raw.get("yy") is None else str(raw["yy"])
    if raw.get("text"):
        item["text"] = str(raw["text"])
    for key in ("margin", "margin_yy"):
        if key in raw:
            item[key] = raw[key]
    return item


def _off(left, right):
    return abs(left - right) > max(0.5, 0.005 * max(abs(left), abs(right), 1.0))


def normalize(raw):
    """Validate a spec dict and fill derived lines (gross profit, operating profit, net)."""
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object or a CSV table")
    unit = str(raw.get("unit") or "millions").strip().lower()
    if unit not in _TO_MILLIONS:
        raise SpecError(
            f"unknown unit {unit!r}; use millions, thousands, billions, or ones"
        )
    scale = _TO_MILLIONS[unit]
    notes = [str(n) for n in raw.get("notes") or []]

    segments = []
    for entry in raw.get("segments") or []:
        item = _item(entry, "Segment", scale)
        if item is not None and item["value"] != 0:
            segments.append(item)

    revenue = _item(raw.get("revenue"), "Revenue", scale)
    cor = _item(raw.get("cost_of_revenue"), "Cost of revenue", scale)
    gp = _item(raw.get("gross_profit"), "Gross profit", scale)
    op = _item(raw.get("operating_profit"), "Operating profit", scale)
    ni = _item(raw.get("net_profit"), "Net profit", scale)

    opex_raw = raw.get("operating_expenses") or {}
    if isinstance(opex_raw, (int, float)) and not isinstance(opex_raw, bool):
        opex_raw = {"value": opex_raw}
    children = []
    for entry in (opex_raw.get("children") or []) if isinstance(opex_raw, dict) else []:
        item = _item(entry, "Operating expense", scale)
        if item is not None and item["value"] != 0:
            children.append(item)
    opex = _item(opex_raw if isinstance(opex_raw, dict) else None, "Operating expenses", scale)

    bridges = []
    for entry in raw.get("bridges") or []:
        item = _item(entry, "Below operating profit", scale)
        if item is not None and item["value"] != 0:
            bridges.append(item)

    if revenue is None and segments:
        revenue = {"label": "Revenue", "value": sum(item["value"] for item in segments)}
        notes.append("Revenue derived as the sum of the segments.")
    if gp is None and revenue is not None and cor is not None:
        value = revenue["value"] - cor["value"]
        gp = {"label": "Gross profit" if value >= 0 else "Gross loss", "value": value}
        notes.append("Gross profit derived as revenue minus cost of revenue.")
    if cor is None and revenue is not None and gp is not None:
        value = revenue["value"] - gp["value"]
        cor = {"label": "Cost of revenue", "value": value}
        notes.append("Cost of revenue derived as revenue minus gross profit.")
    if opex is None and children:
        opex = {"label": "Operating expenses", "value": sum(item["value"] for item in children), "children": children}
        notes.append("Operating expenses derived as the sum of the breakdown lines.")
    if opex is None and gp is not None and op is not None:
        opex = {"label": "Operating expenses", "value": gp["value"] - op["value"], "children": children}
        notes.append("Operating expenses derived as gross profit minus operating profit.")
    if op is None and gp is not None and opex is not None:
        value = gp["value"] - opex["value"]
        op = {"label": "Operating profit" if value >= 0 else "Operating loss", "value": value}
        notes.append("Operating profit derived as gross profit minus operating expenses.")
    if ni is None and op is not None:
        value = op["value"] + sum(item["value"] for item in bridges)
        ni = {"label": "Net profit" if value >= 0 else "Net loss", "value": value}
        notes.append("Net profit derived as operating profit plus the bridge lines.")

    missing = [
        name for name, item in (
            ("revenue", revenue),
            ("cost_of_revenue", cor),
            ("gross_profit", gp),
            ("operating_expenses", opex),
            ("operating_profit", op),
            ("net_profit", ni),
        ) if item is None
    ]
    if missing:
        raise SpecError(
            "spec is missing " + ", ".join(missing)
            + "; provide those lines or the inputs they are derived from"
        )
    if opex is not None and "children" not in opex:
        opex["children"] = children
    else:
        opex["children"] = children

    if segments and _off(sum(item["value"] for item in segments), revenue["value"]):
        notes.append("Segment values do not sum to revenue; both are drawn as stated.")
    if _off(revenue["value"] - cor["value"], gp["value"]):
        notes.append("Gross profit does not equal revenue minus cost of revenue; both are drawn as stated.")
    if _off(gp["value"] - opex["value"], op["value"]):
        notes.append("Operating profit does not equal gross profit minus operating expenses; both are drawn as stated.")
    if children and _off(sum(item["value"] for item in children), opex["value"]):
        notes.append("Operating-expense breakdown does not sum to operating expenses; both are drawn as stated.")
    if _off(op["value"] + sum(item["value"] for item in bridges), ni["value"]):
        notes.append("Net profit does not equal operating profit plus the bridge lines; both are drawn as stated.")

    footer = [str(line) for line in (raw.get("footer") or []) if str(line).strip()]
    draft = bool(raw.get("draft"))
    if draft and not any("not investment advice" in line.lower() for line in footer):
        footer.append("Draft for review; not investment advice.")

    byline = raw.get("byline")
    byline = str(byline).strip() if byline else None
    spec = {
        "title": str(raw.get("title") or "Income statement"),
        "currency": str(raw.get("currency") or "USD").upper(),
        "unit": "millions",
        "byline": byline,
        "byline_color": str(raw.get("byline_color") or NAVY),
        "draft": draft,
        "footer": footer,
        "segments": segments,
        "revenue": revenue,
        "cost_of_revenue": cor,
        "gross_profit": gp,
        "operating_expenses": opex,
        "operating_profit": op,
        "bridges": bridges,
        "net_profit": ni,
        "notes": notes,
    }
    for key in ("byline_icon", "logo", "logo_width"):
        if raw.get(key) not in (None, ""):
            spec[key] = raw[key]
    return spec


def parse_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "section" not in [name.strip() for name in reader.fieldnames if name]:
        raise SpecError("CSV spec needs a header row with a section column")
    raw = {
        "segments": [],
        "bridges": [],
        "footer": [],
        "operating_expenses": {"children": []},
    }
    opex_seen = False
    for row in reader:
        if not row or all(not (value or "").strip() for value in row.values()):
            continue
        section = (row.get("section") or "").strip().lower()
        label = (row.get("label") or "").strip()
        if not section or section.startswith("#"):
            continue
        if section == "title":
            raw["title"] = label
        elif section == "byline":
            raw["byline"] = label
        elif section == "byline_color":
            raw["byline_color"] = label
        elif section == "byline_icon":
            raw["byline_icon"] = label
        elif section == "logo":
            raw["logo"] = label
        elif section == "currency":
            raw["currency"] = label
        elif section == "unit":
            raw["unit"] = label
        elif section == "draft":
            raw["draft"] = label.lower() in {"1", "true", "yes", "y"}
        elif section == "footer":
            raw["footer"].append(label)
        elif section == "segment":
            raw["segments"].append(_csv_item(row))
        elif section in {"revenue", "cost_of_revenue", "gross_profit", "operating_profit", "net_profit"}:
            raw[section] = _csv_item(row)
        elif section == "operating_expenses":
            item = _csv_item(row)
            item["children"] = raw["operating_expenses"]["children"]
            raw["operating_expenses"] = item
            opex_seen = True
        elif section in {"opex_child", "child"}:
            raw["operating_expenses"]["children"].append(_csv_item(row))
        elif section == "bridge":
            raw["bridges"].append(_csv_item(row))
        else:
            raise SpecError(f"unknown CSV section {section!r}")
    if not opex_seen and raw["operating_expenses"]["children"]:
        raw["operating_expenses"]["value"] = sum(_num(item.get("value")) or 0 for item in raw["operating_expenses"]["children"])
        raw["operating_expenses"]["label"] = "Operating expenses"
    return raw


def _csv_item(row):
    item = {"label": (row.get("label") or "").strip(), "value": row.get("value")}
    if (row.get("prior") or "").strip():
        item["prior"] = row.get("prior")
    if (row.get("yy") or "").strip():
        item["yy"] = row.get("yy").strip()
    if (row.get("note") or "").strip():
        item["note"] = row.get("note").strip()
    return item


def load_spec(path):
    path = Path(path)
    if not path.is_file():
        raise SpecError(f"spec not found: {path}")
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"spec is not valid JSON: {exc}") from exc
    else:
        raw = parse_csv(text)
    return normalize(raw)


def dump_spec(spec):
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def apply_overrides(spec, *, title=None, byline=None, byline_color=None, footer=None):
    """CLI flags win over the file. `footer` replaces the spec footer when non-empty."""
    out = json.loads(json.dumps(spec))
    if title:
        out["title"] = title
    if byline is not None:
        out["byline"] = byline.strip() or None
    if byline_color:
        out["byline_color"] = byline_color
    if footer:
        out["footer"] = [str(line) for line in footer if str(line).strip()]
        if out.get("draft") and not any("not investment advice" in line.lower() for line in out["footer"]):
            out["footer"].append("Draft for review; not investment advice.")
    return out


# ---------------------------------------------------------------- draft from statements
def _pick(row, canonical):
    folded = {_norm_key(key): value for key, value in row.items()}
    for candidate in _LINE_KEYS[canonical]:
        if candidate in folded:
            number = _num(folded[candidate])
            if number is not None:
                return number
    return None


def _row_from_mapping(mapping):
    return {str(key): value for key, value in mapping.items() if _num(value) is not None}


def _extract_periods(data, period):
    """Return (current_row, prior_row, currency, current_end, prior_end, notes).

    Accepts the cleaned `financials` payload: a Yahoo dict of date → lines, an
    Alpha Vantage list of reports, or a Finnhub list of as-reported periods.
    """
    notes = []
    currency = "USD"
    payload = data
    if isinstance(data, dict) and "income" in data:
        currency = str(data.get("currency") or "USD")
        payload = data["income"]

    periods = []
    if isinstance(payload, dict) and any(_date_prefix(key) for key in payload):
        for key, row in payload.items():
            if not _date_prefix(key) or not isinstance(row, dict):
                continue
            periods.append((_date_prefix(key), _row_from_mapping(row)))
    elif isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if "fiscalDateEnding" in entry or "totalRevenue" in entry:
                end = _date_prefix(entry.get("fiscalDateEnding") or "") or "9999-99-99"
                periods.append((end, _row_from_mapping(entry)))
            elif isinstance(entry.get("income"), list) or entry.get("endDate"):
                end = _date_prefix(entry.get("endDate") or entry.get("filedDate") or "") or "9999-99-99"
                facts = {}
                for fact in entry.get("income") or []:
                    if isinstance(fact, dict) and fact.get("label") is not None:
                        facts[str(fact.get("label"))] = fact.get("value")
                if entry.get("unit"):
                    currency = str(entry.get("unit"))
                periods.append((end, _row_from_mapping(facts)))
    if not periods:
        raise SpecError(
            "financials payload has no income-statement periods this chart can read; "
            "write a spec file instead"
        )
    periods.sort(key=lambda item: item[0], reverse=True)
    # Drop a placeholder sort key if the date was missing and a real date exists.
    dated = [item for item in periods if item[0] != "9999-99-99"]
    if dated:
        periods = dated
    current_end, current = periods[0]
    prior_end, prior = None, None
    if len(periods) > 1:
        prior_end, prior, skew = _pick_prior(current_end, periods[1:], period)
        if skew:
            notes.append(skew)
    return current, prior or {}, currency, current_end, prior_end, notes


def _date_prefix(value):
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value).strip())
    return match.group(1) if match else None


def _pick_prior(current_end, older, period):
    if not _date_prefix(current_end):
        return older[0][0], older[0][1], "Prior period is the previous reported column; the period end date was not a date."
    current = dt.date.fromisoformat(current_end)
    best = None
    best_err = 10 ** 9
    for end, row in older:
        if not _date_prefix(end):
            continue
        err = abs((current - dt.date.fromisoformat(end)).days - 365)
        if err < best_err:
            best_err = err
            best = (end, row)
    if best is None:
        return older[0][0], older[0][1], "Prior period is the previous reported column."
    note = None
    if best_err > 45:
        which = "year-ago quarter" if period == "quarterly" else "prior year"
        note = (
            f"No column within 45 days of the {which}; "
            f"prior is {best[0]} ({best_err} days off a 365-day lag)."
        )
    return best[0], best[1], note


def spec_from_financials(data, *, symbol, period, source, source_url, retrieved_at):
    """Build a draft spec from a `fin.py financials` income payload.

    Values are converted from reported currency units to millions. The bridge
    under operating profit is the plug that makes the chart tie, and the footer
    keeps the source note. The spec is marked draft.
    """
    current, prior, currency, current_end, prior_end, notes = _extract_periods(data, period)

    def millions(row, key):
        value = _pick(row, key)
        if value is None:
            return None
        return value / 1e6

    def line(label, key):
        value = millions(current, key)
        if value is None:
            return None
        item = {"label": label, "value": value}
        previous = millions(prior, key) if prior else None
        if previous is not None:
            item["prior"] = previous
        return item

    revenue = line("Revenue", "revenue")
    cor = line("Cost of revenue", "cor")
    gp = line("Gross profit", "gp")
    opex = line("Operating expenses", "opex")
    rd = line("Research & development", "rd")
    sga = line("SG&A", "sga")
    op = line("Operating profit", "op")
    ni = line("Net profit", "ni")
    tax = millions(current, "tax")
    tax_prior = millions(prior, "tax") if prior else None

    if revenue is None:
        raise SpecError(
            f"no revenue line in the {source} income statement for {symbol}; "
            "the draft mapper knows Total Revenue / totalRevenue and close variants"
        )

    children = [item for item in (rd, sga) if item is not None]
    if opex is not None and children:
        used = sum(item["value"] for item in children)
        residual = opex["value"] - used
        if residual > max(0.5, 0.01 * abs(opex["value"])):
            children.append({"label": "Other operating", "value": residual})
            notes.append("Other operating is the residual of operating expenses minus R&D and SG&A.")
    if opex is None and children:
        opex = {"label": "Operating expenses", "value": sum(item["value"] for item in children)}

    if op is not None and op["value"] < 0:
        op["label"] = "Operating loss"
    if ni is not None and ni["value"] < 0:
        ni["label"] = "Net loss"
    if gp is not None and gp["value"] < 0:
        gp["label"] = "Gross loss"

    bridges = []
    detail = []
    for key, name in (("intinc", "interest income"), ("intexp", "interest expense"), ("other", "other income (expense)")):
        reported = millions(current, key)
        if reported is not None:
            detail.append(f"{name} {fmt_usd(reported, currency)}")
    if op is not None and ni is not None:
        tax_bridge = -(tax or 0.0)
        other_bridge = ni["value"] - op["value"] - tax_bridge
        if abs(other_bridge) >= 0.05:
            bridges.append({
                "label": "Other income, net" if other_bridge >= 0 else "Other expense, net",
                "value": other_bridge,
                "note": "(see note)",
            })
        if tax is not None and abs(tax_bridge) >= 0.05:
            bridges.append({
                "label": "Tax benefit" if tax_bridge > 0 else "Tax expense",
                "value": tax_bridge,
                "prior": None if tax_prior is None else -tax_prior,
            })
        plug = (
            "Other income, net is the plug that makes operating profit, tax, and net income tie"
        )
        if detail:
            plug += " (" + "; ".join(detail) + ")"
        plug += "."
        notes.append(plug)
    elif tax is not None:
        notes.append("Tax was reported but operating profit or net income was missing, so it was not bridged.")

    if abs(revenue["value"]) < 1:
        notes.append(
            "Revenue is under 1 after converting reported units to millions; confirm the source scale before using the chart."
        )

    compared = f", compared with {prior_end}" if prior_end else ", no prior period on the statement"
    footer = [
        f"{period.capitalize()} statement ended {current_end}{compared}. Figures are millions of {currency}.",
        _source_footer(source, source_url, retrieved_at),
    ]
    if notes:
        footer.append(" ".join(notes))
    footer.append("Draft for review; not investment advice.")

    raw = {
        "title": f"{symbol} {period} income statement, {current_end} (draft)",
        "currency": currency,
        "unit": "millions",
        "draft": True,
        "footer": footer,
        "segments": [],
        "revenue": revenue,
        "cost_of_revenue": cor,
        "gross_profit": gp,
        "operating_profit": op,
        "bridges": bridges,
        "net_profit": ni,
        "notes": notes,
    }
    if opex is not None:
        opex_out = {"label": opex["label"], "value": opex["value"], "children": children}
        if "prior" in opex:
            opex_out["prior"] = opex["prior"]
        raw["operating_expenses"] = opex_out
    return normalize(raw)


def _source_footer(source, source_url, retrieved_at):
    text = f"Source: {source or 'financials'}"
    if source_url:
        text += f" {source_url}"
    if retrieved_at:
        text += f", retrieved {retrieved_at}"
    text += ". Aggregator statements are normalized and can differ from as-filed figures."
    return text


# ---------------------------------------------------------------- layout
def positive_topology(spec):
    if spec["revenue"]["value"] <= 0 or spec["gross_profit"]["value"] <= 0:
        return False
    if spec["cost_of_revenue"]["value"] < 0 or spec["operating_expenses"]["value"] < 0:
        return False
    if spec["operating_profit"]["value"] < 0 or spec["net_profit"]["value"] < 0:
        return False
    if any(item["value"] < 0 for item in spec["segments"]):
        return False
    if any(item["value"] < 0 for item in spec["operating_expenses"]["children"]):
        return False
    if any(item["value"] < 0 for item in spec["bridges"]):
        return False
    return True


def layout_geometry(spec, width, height):
    """Column anchors and the pixels-per-unit scale.

    On a 1280×720 canvas with a revenue magnitude that fills the reference bar,
    the anchors match the reference design exactly (no company figures involved).
    """
    if width <= 0 or height <= 0:
        raise SpecError("width and height must be positive")
    sx = width / REF_W
    sy = height / REF_H
    base = abs(spec["revenue"]["value"])
    if base < 1e-9:
        base = max(
            abs(spec["gross_profit"]["value"]),
            abs(spec["net_profit"]["value"]),
            abs(spec["operating_expenses"]["value"]),
            1.0,
        )
    scale = (REF_REVENUE_PX * sy) / base
    gap_seg = 34.0 * sy
    gap_gp_cor = 30.0 * sy
    gap_op_ox = 78.0 * sy
    gap_oth = 14.0 * sy
    kids_gap = 10.0 * sy
    kids_offset = -12.0 * sy
    xs = [232.0 * sx, 392.0 * sx, 598.0 * sx, 806.0 * sx, 1040.0 * sx]
    if not spec["segments"]:
        shift = xs[1] - xs[0]
        xs = [value - shift for value in xs]
    revenue_y = 240.0 * sy
    op_y = 270.0 * sy
    np_y = 108.0 * sy
    footer_ys = _footer_ys(len(spec["footer"]), sy)
    fitted = False
    header_limit = (86.0 + 32.0 + 16.0) * sy
    for _ in range(48):
        if _fits(spec, scale, revenue_y, op_y, np_y, gap_seg, gap_gp_cor, gap_op_ox, gap_oth, kids_gap, kids_offset, sy, header_limit, footer_ys):
            break
        fitted = True
        scale *= 0.92
        gap_seg = max(4.0 * sy, gap_seg * 0.92)
        kids_gap = max(4.0 * sy, kids_gap * 0.92)
        gap_gp_cor = max(8.0 * sy, gap_gp_cor * 0.92)
        gap_op_ox = max(28.0 * sy, gap_op_ox * 0.92)
        gap_oth = max(4.0 * sy, gap_oth * 0.92)
    child_heights, child_gaps = child_stack(spec, scale, sy, kids_gap)
    return {
        "S": scale,
        "sx": sx,
        "sy": sy,
        "node_w": 14.0 * sx,
        "x": xs,
        "revenue_y": revenue_y,
        "op_y": op_y,
        "np_y": np_y,
        "gap_seg": gap_seg,
        "gap_gp_cor": gap_gp_cor,
        "gap_op_ox": gap_op_ox,
        "gap_oth": gap_oth,
        "kids_gap": kids_gap,
        "kids_offset": kids_offset,
        "child_heights": child_heights,
        "child_gaps": child_gaps,
        "footer_ys": footer_ys,
        "width": width,
        "height": height,
        "fitted": fitted,
        "positive": positive_topology(spec),
    }


def _footer_ys(count, sy):
    reference = [668.0, 682.0, 696.0, 712.0]
    if count <= 0:
        return []
    if count == 4:
        return [value * sy for value in reference]
    if count < 4:
        return [value * sy for value in reference[-count:]]
    return [(712.0 - 14.0 * (count - 1 - index)) * sy for index in range(count)]


def _label_px(text, size, sy):
    """Ink height of a chart label in pixels.

    Measured on Open Sans semibold at 10.5pt and 100 dpi: one line is about
    16px, two lines about 41px. Other sizes scale from that. The window
    extent is taller than the ink, so this uses the ink.
    """
    lines = str(text).count("\n") + 1
    scale = (size / 10.5) * sy
    if lines <= 1:
        return 16.0 * scale
    return (16.0 + (lines - 1) * 25.0) * scale


def child_stack(spec, scale, sy, minimum_gap):
    """Node heights and the gap under each operating-expense child.

    The minimum gap is the reference 10px. A short bar with a two-line label
    gets a wider gap so the next label does not sit on top of it. Tall bars
    (the reference chart) stay on the minimum.
    """
    items = spec["operating_expenses"]["children"]
    labels = build_labels(spec)["children"] if items else []
    heights = [max(abs(item["value"]) * scale, 2.5 * sy) for item in items]
    gaps = []
    for index, height in enumerate(heights):
        if index + 1 >= len(heights):
            gaps.append(minimum_gap)
            continue
        need = (_label_px(labels[index], 10.5, sy) + _label_px(labels[index + 1], 10.5, sy)) / 2.0 + 4.0 * sy
        have = height / 2.0 + heights[index + 1] / 2.0
        gaps.append(max(minimum_gap, need - have))
    return heights, gaps


def _fits(spec, scale, revenue_y, op_y, np_y, gap_seg, gap_gp_cor, gap_op_ox, gap_oth, kids_gap, kids_offset, sy, header_limit, footer_ys):
    footer_limit = (footer_ys[0] - 14.0 * sy) if footer_ys else (REF_H * sy - 20.0 * sy)
    rev_h = abs(spec["revenue"]["value"]) * scale
    seg_heights = [abs(item["value"]) * scale for item in spec["segments"]]
    if seg_heights:
        total = sum(seg_heights) + gap_seg * (len(seg_heights) - 1)
        top = revenue_y + rev_h / 2.0 - total / 2.0
        if top < header_limit or top + total > footer_limit:
            return False
    gp_h = abs(spec["gross_profit"]["value"]) * scale
    cor_h = abs(spec["cost_of_revenue"]["value"]) * scale
    if revenue_y + gp_h + gap_gp_cor + cor_h > footer_limit:
        return False
    op_h = abs(spec["operating_profit"]["value"]) * scale
    ox_h = abs(spec["operating_expenses"]["value"]) * scale
    oxy = op_y + op_h + gap_op_ox
    if oxy + ox_h > footer_limit:
        return False
    ni_h = abs(spec["net_profit"]["value"]) * scale
    kids_top = oxy + kids_offset
    if kids_top < np_y + ni_h + 8.0 * sy and spec["operating_expenses"]["children"]:
        return False
    heights, gaps = child_stack(spec, scale, sy, kids_gap)
    ky = kids_top
    for height, gap in zip(heights, gaps):
        ky += height + gap
    if heights and ky > footer_limit:
        return False
    bridge_bottom = np_y
    for bridge in spec["bridges"]:
        bridge_bottom += abs(bridge["value"]) * scale + gap_oth
    if spec["bridges"] and bridge_bottom > op_y - 8.0 * sy and spec["operating_profit"]["value"] >= 0:
        # Bridges live above the operating-profit node. Overlap means the scale is too big.
        if np_y + ni_h > op_y:
            return False
    return True


# ---------------------------------------------------------------- rendering
_FONTS = None


def load_fonts():
    """Open Sans from fonts/, else DejaVu Sans (matplotlib / the OS)."""
    global _FONTS
    if _FONTS is not None:
        return _FONTS
    from matplotlib import font_manager as fm
    regular = FONT_DIR / "OpenSans-400.ttf"
    semi = FONT_DIR / "OpenSans-600.ttf"
    bold = FONT_DIR / "OpenSans-800.ttf"
    if regular.is_file() and semi.is_file() and bold.is_file():
        for path in (regular, semi, bold):
            fm.fontManager.addfont(str(path))
        _FONTS = {
            "reg": fm.FontProperties(fname=str(regular)),
            "semi": fm.FontProperties(fname=str(semi)),
            "bold": fm.FontProperties(fname=str(bold)),
            "name": "Open Sans",
        }
        return _FONTS
    import matplotlib
    data = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    dejavu = data / "DejaVuSans.ttf"
    dejavu_bold = data / "DejaVuSans-Bold.ttf"
    if not dejavu.is_file():
        dejavu = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        dejavu_bold = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if dejavu.is_file():
        reg = fm.FontProperties(fname=str(dejavu))
        bold_fp = fm.FontProperties(fname=str(dejavu_bold if dejavu_bold.is_file() else dejavu))
    else:
        reg = fm.FontProperties(family="DejaVu Sans")
        bold_fp = fm.FontProperties(family="DejaVu Sans", weight="bold")
    _FONTS = {"reg": reg, "semi": bold_fp, "bold": bold_fp, "name": "DejaVu Sans"}
    return _FONTS


def render_chart(spec, out_path, *, svg_path=None, width=1280, height=720, dpi=100,
                 byline_icon=None, logo=None, logo_width=None):
    if not matplotlib_available():
        raise ChartDependencyError(MATPLOTLIB_HINT)
    if not isinstance(spec, dict) or "revenue" not in spec or "notes" not in spec:
        spec = normalize(spec)
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    import numpy as np

    labels = build_labels(spec)
    lay = layout_geometry(spec, width, height)
    fonts = load_fonts()
    notes = list(spec.get("notes") or [])
    if lay["fitted"]:
        notes.append("Flow scale was reduced so columns stay clear of the header and the footer.")
    if not lay["positive"]:
        notes.append("A loss or credit is drawn in red. Ribbon widths use absolute values, and a negative remainder is not forced through a positive bar.")
    if fonts["name"] != "Open Sans":
        notes.append("Open Sans files were not found; the chart uses DejaVu Sans.")

    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.axis("off")
    ax.set_facecolor(BG)

    def font_pt(size):
        return size * lay["sy"] * (100.0 / dpi)

    def txt(x, y, text, fp, size, ha="left", va="center", color=NAVY):
        ax.text(
            x, y, str(text).replace("$", r"\$"),
            fontproperties=fp, fontsize=font_pt(size), ha=ha, va=va, color=color,
            zorder=5, linespacing=1.25,
        )

    def node(x, y, h, color):
        ax.add_patch(plt.Rectangle((x, y), lay["node_w"], max(abs(h), 2.5 * lay["sy"]), color=color, lw=0, zorder=3))

    def ribbon(x0, y0, h0, x1, y1, h1, color, alpha=0.42):
        if h0 < 0 or h1 < 0 or abs(x1 - x0) < 1e-6:
            return
        if h0 == 0 and h1 == 0:
            return
        xs = np.linspace(x0, x1, 60)
        u = (xs - x0) / (x1 - x0)
        smooth = u * u * (3 - 2 * u)
        top = y0 + (y1 - y0) * smooth
        bottom = top + h0 + (h1 - h0) * smooth
        ax.add_patch(plt.Polygon(
            np.r_[np.c_[xs, top], np.c_[xs[::-1], bottom[::-1]]],
            closed=True, color=color, alpha=alpha, lw=0, zorder=1,
        ))

    _draw_header(ax, spec, lay, fonts, txt, byline_icon)
    if lay["positive"]:
        _draw_positive(ax, spec, lay, labels, txt, node, ribbon)
    else:
        _draw_signed(ax, spec, lay, labels, txt, node, ribbon)
    _draw_footer(spec, lay, fonts, txt)

    icon_path = byline_icon or spec.get("byline_icon")
    logo_path = logo or spec.get("logo")
    if logo_width is None:
        logo_width = spec.get("logo_width")
    if logo_width is None:
        logo_width = 320.0 * lay["sx"]
    logo_path = str(logo_path) if logo_path else None
    if logo_path:
        _place_logo(fig, ax, logo_path, float(logo_width), lay)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    previous_svg_font = matplotlib.rcParams.get("svg.fonttype")
    matplotlib.rcParams["svg.fonttype"] = "path"
    try:
        fig.savefig(out_path, dpi=dpi, facecolor=BG)
        if svg_path:
            svg_path = Path(svg_path)
            svg_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(svg_path, format="svg", facecolor=BG)
    finally:
        matplotlib.rcParams["svg.fonttype"] = previous_svg_font
        plt.close(fig)

    return {
        "png": str(out_path),
        "svg": str(svg_path) if svg_path else None,
        "width": width,
        "height": height,
        "dpi": dpi,
        "font": fonts["name"],
        "draft": bool(spec.get("draft")),
        "positive_topology": bool(lay["positive"]),
        "notes": notes,
    }


def _draw_header(ax, spec, lay, fonts, txt, byline_icon):
    sx, sy = lay["sx"], lay["sy"]
    txt(40 * sx, 46 * sy, spec["title"], fonts["bold"], 27, color=NAVY)
    if not spec.get("byline") and not (byline_icon or spec.get("byline_icon")):
        return
    icon_h = 32.0 * sy
    icon_x = 40.0 * sx
    icon_y = 86.0 * sy
    icon_w = 0.0
    path = byline_icon or spec.get("byline_icon")
    if path:
        image = _open_image(path, crop=False)
        import numpy as np
        array = np.array(image)
        icon_w = icon_h * (array.shape[1] / array.shape[0])
        ax.imshow(
            array,
            extent=[icon_x, icon_x + icon_w, icon_y + icon_h, icon_y],
            interpolation="lanczos", zorder=6, aspect="auto",
        )
    if spec.get("byline"):
        text_x = icon_x + icon_w + (8.0 * sx if icon_w else 0.0)
        txt(text_x, icon_y + icon_h / 2.0, spec["byline"], fonts["semi"], 15, va="center", color=spec.get("byline_color") or NAVY)


def _draw_footer(spec, lay, fonts, txt):
    sx = lay["sx"]
    for index, (line, y) in enumerate(zip(spec["footer"], lay["footer_ys"])):
        face = fonts["semi"] if index == len(spec["footer"]) - 1 else fonts["reg"]
        txt(40 * sx, y, line, face, 8, color=MUTED)


def _draw_positive(ax, spec, lay, labels, txt, node, ribbon):
    scale = lay["S"]
    nw = lay["node_w"]
    sx, sy = lay["sx"], lay["sy"]
    x0, x1, x2, x3, x4 = lay["x"]
    revenue_y = lay["revenue_y"]
    op_y = lay["op_y"]
    np_y = lay["np_y"]
    gap_seg = lay["gap_seg"]
    gap_gp_cor = lay["gap_gp_cor"]
    gap_op_ox = lay["gap_op_ox"]
    gap_oth = lay["gap_oth"]

    rev_h = spec["revenue"]["value"] * scale
    node(x1, revenue_y, rev_h, NAVY)
    heights = [item["value"] * scale for item in spec["segments"]]
    total = sum(heights) + gap_seg * max(len(heights) - 1, 0)
    seg_y = revenue_y + rev_h / 2.0 - total / 2.0
    dest = revenue_y
    for item, height, label in zip(spec["segments"], heights, labels["segments"]):
        node(x0, seg_y, height, SEG)
        ribbon(x0 + nw, seg_y, height, x1, dest, height, SEG, 0.30)
        txt(x0 - 10 * sx, seg_y + max(height, 2.5 * sy) / 2.0, label, _semi(), 11.5, ha="right")
        seg_y += height + gap_seg
        dest += height
    txt(x1 + nw / 2.0, revenue_y - 12 * sy, labels["revenue"], _bold(), 12.5, ha="center", va="bottom")

    gp_h = spec["gross_profit"]["value"] * scale
    cor_h = spec["cost_of_revenue"]["value"] * scale
    cor_y = revenue_y + gp_h + gap_gp_cor
    node(x2, revenue_y, gp_h, GREEN)
    node(x2, cor_y, cor_h, RED)
    ribbon(x1 + nw, revenue_y, gp_h, x2, revenue_y, gp_h, GREEN)
    ribbon(x1 + nw, revenue_y + gp_h, cor_h, x2, cor_y, cor_h, RED)
    txt(x2, revenue_y - 9 * sy, labels["gross_profit"], _bold(), 11.5, va="bottom", color=GREEN)
    txt(x2 + nw + 10 * sx, cor_y + max(cor_h, 2.5 * sy) / 2.0, labels["cost_of_revenue"], _semi(), 11.5, color=RED)

    op_h = spec["operating_profit"]["value"] * scale
    ox_h = spec["operating_expenses"]["value"] * scale
    ox_y = op_y + op_h + gap_op_ox
    node(x3, op_y, op_h, GREEN)
    node(x3, ox_y, ox_h, RED)
    ribbon(x2 + nw, revenue_y, op_h, x3, op_y, op_h, GREEN)
    ribbon(x2 + nw, revenue_y + op_h, ox_h, x3, ox_y, ox_h, RED)
    txt(x3, ox_y + ox_h + 16 * sy, labels["operating_expenses"], _semi(), 11.5, color=RED, va="top")

    ni_h = spec["net_profit"]["value"] * scale
    node(x4, np_y, ni_h, GREEN)
    bridge_y = np_y
    bridge_dest = np_y
    bridge_pos = []
    for item, label in zip(spec["bridges"], labels["bridges"]):
        height = item["value"] * scale
        bridge_pos.append((bridge_y, height, label))
        node(x3, bridge_y, height, GREEN)
        bridge_y += height + gap_oth
    for y, height, label in bridge_pos:
        ribbon(x3 + nw, y, height, x4, bridge_dest, height, GREEN)
        txt(x3 - 8 * sx, y + max(height, 2.5 * sy) / 2.0, label, _semi(), 10.5, ha="right", color=GREEN)
        bridge_dest += height
    ribbon(x3 + nw, op_y, op_h, x4, bridge_dest, op_h, GREEN)
    txt(x3 + nw + 8 * sx, op_y + op_h + 8 * sy, labels["operating_profit"], _bold(), 11.5, color=GREEN, va="top")
    txt(x4 + nw + 10 * sx, np_y + max(ni_h, 2.5 * sy) / 2.0, labels["net_profit"], _bold(), 11.5, color=GREEN)

    kid_y = ox_y + lay["kids_offset"]
    kid_src = ox_y
    for item, label, height, gap in zip(
        spec["operating_expenses"]["children"], labels["children"], lay["child_heights"], lay["child_gaps"],
    ):
        source_h = max(item["value"] * scale, 0.0)
        node(x4, kid_y, height, RED)
        ribbon(x3 + nw, kid_src, source_h, x4, kid_y, height, RED)
        txt(x4 + nw + 10 * sx, kid_y + height / 2.0, label, _semi(), 10.5, color=RED)
        kid_y += height + gap
        kid_src += source_h


def _draw_signed(ax, spec, lay, labels, txt, node, ribbon):
    """Losses and credits. Nodes use absolute height and turn red; ribbons are skipped when a split would cross or go negative."""
    scale = lay["S"]
    nw = lay["node_w"]
    sx, sy = lay["sx"], lay["sy"]
    x0, x1, x2, x3, x4 = lay["x"]
    revenue_y = lay["revenue_y"]
    op_y = lay["op_y"]
    np_y = lay["np_y"]
    rev = spec["revenue"]["value"]
    gp = spec["gross_profit"]["value"]
    cor = spec["cost_of_revenue"]["value"]
    op = spec["operating_profit"]["value"]
    ox = spec["operating_expenses"]["value"]
    ni = spec["net_profit"]["value"]

    rev_h = abs(rev) * scale
    node(x1, revenue_y, rev_h, NAVY if rev >= 0 else RED)
    heights = [abs(item["value"]) * scale for item in spec["segments"]]
    total = sum(heights) + lay["gap_seg"] * max(len(heights) - 1, 0)
    seg_y = revenue_y + rev_h / 2.0 - total / 2.0
    dest = revenue_y
    for item, height, label in zip(spec["segments"], heights, labels["segments"]):
        color = SEG if item["value"] >= 0 else RED
        node(x0, seg_y, height, color)
        ribbon(x0 + nw, seg_y, height, x1, dest, height, color, 0.30)
        txt(x0 - 10 * sx, seg_y + max(height, 2.5 * sy) / 2.0, label, _semi(), 11.5, ha="right", color=NAVY if item["value"] >= 0 else RED)
        seg_y += height + lay["gap_seg"]
        dest += height
    txt(
        x1 + nw / 2.0, revenue_y - 12 * sy, labels["revenue"], _bold(), 12.5,
        ha="center", va="bottom", color=NAVY if rev >= 0 else RED,
    )

    gp_h = abs(gp) * scale
    cor_h = abs(cor) * scale
    if gp >= 0 and cor >= 0 and rev > 0:
        cor_y = revenue_y + gp_h + lay["gap_gp_cor"]
        node(x2, revenue_y, gp_h, GREEN)
        node(x2, cor_y, cor_h, RED)
        ribbon(x1 + nw, revenue_y, gp_h, x2, revenue_y, gp_h, GREEN)
        ribbon(x1 + nw, revenue_y + min(gp_h, rev_h), cor_h, x2, cor_y, cor_h, RED)
        txt(x2, revenue_y - 9 * sy, labels["gross_profit"], _bold(), 11.5, va="bottom", color=GREEN)
        txt(x2 + nw + 10 * sx, cor_y + max(cor_h, 2.5 * sy) / 2.0, labels["cost_of_revenue"], _semi(), 11.5, color=RED)
        gp_top = revenue_y
    else:
        cursor = revenue_y
        if gp != 0:
            node(x2, cursor, gp_h, GREEN if gp >= 0 else RED)
            txt(x2, cursor - 9 * sy, labels["gross_profit"], _bold(), 11.5, va="bottom", color=GREEN if gp >= 0 else RED)
            if rev > 0 and gp > 0:
                ribbon(x1 + nw, revenue_y, min(gp_h, rev_h), x2, cursor, gp_h, GREEN)
            gp_top = cursor
            cursor += gp_h + lay["gap_gp_cor"]
        else:
            gp_top = revenue_y
        if cor != 0:
            node(x2, cursor, cor_h, RED if cor >= 0 else GREEN)
            txt(x2 + nw + 10 * sx, cursor + max(cor_h, 2.5 * sy) / 2.0, labels["cost_of_revenue"], _semi(), 11.5, color=RED if cor >= 0 else GREEN)
            if rev > 0 and cor > 0 and gp < 0:
                ribbon(x1 + nw, revenue_y, min(cor_h, rev_h), x2, cursor, cor_h, RED)

    op_h = abs(op) * scale
    ox_h = abs(ox) * scale
    op_color = GREEN if op >= 0 else RED
    ox_color = RED if ox >= 0 else GREEN
    if gp > 0 and op >= 0 and ox >= 0:
        ox_y = op_y + op_h + lay["gap_op_ox"]
        node(x3, op_y, op_h, GREEN)
        node(x3, ox_y, ox_h, RED)
        ribbon(x2 + nw, gp_top, op_h, x3, op_y, op_h, GREEN)
        ribbon(x2 + nw, gp_top + op_h, ox_h, x3, ox_y, ox_h, RED)
    else:
        ox_y = op_y + op_h + lay["gap_op_ox"]
        node(x3, op_y, op_h, op_color)
        if ox != 0:
            node(x3, ox_y, ox_h, ox_color)
        if gp > 0 and ox > 0:
            inflow = min(gp_h, ox_h)
            ribbon(x2 + nw, gp_top, inflow, x3, ox_y, inflow, RED)
    if ox != 0:
        txt(x3, ox_y + ox_h + 16 * sy, labels["operating_expenses"], _semi(), 11.5, color=ox_color, va="top")
    txt(x3 + nw + 8 * sx, op_y + op_h + 8 * sy, labels["operating_profit"], _bold(), 11.5, color=op_color, va="top")

    ni_h = abs(ni) * scale
    ni_color = GREEN if ni >= 0 else RED
    node(x4, np_y, ni_h, ni_color)
    if op >= 0 and ni >= 0 and all(item["value"] >= 0 for item in spec["bridges"]):
        bridge_y = np_y
        bridge_dest = np_y
        placed = []
        for item, label in zip(spec["bridges"], labels["bridges"]):
            height = item["value"] * scale
            placed.append((bridge_y, height, label))
            node(x3, bridge_y, height, GREEN)
            bridge_y += height + lay["gap_oth"]
        for y, height, label in placed:
            ribbon(x3 + nw, y, height, x4, bridge_dest, height, GREEN)
            txt(x3 - 8 * sx, y + height / 2.0, label, _semi(), 10.5, ha="right", color=GREEN)
            bridge_dest += height
        ribbon(x3 + nw, op_y, op_h, x4, bridge_dest, op_h, GREEN)
    else:
        bridge_y = np_y
        for item, label in zip(spec["bridges"], labels["bridges"]):
            height = abs(item["value"]) * scale
            color = GREEN if item["value"] >= 0 else RED
            node(x3, bridge_y, height, color)
            txt(x3 - 8 * sx, bridge_y + max(height, 2.5 * sy) / 2.0, label, _semi(), 10.5, ha="right", color=color)
            if item["value"] > 0 and ni > 0:
                ribbon(x3 + nw, bridge_y, height, x4, bridge_y, height, GREEN)
            bridge_y += height + lay["gap_oth"]
        if op < 0 and ni < 0:
            ribbon(x3 + nw, op_y, min(op_h, ni_h), x4, np_y, min(op_h, ni_h), RED)
        elif op >= 0 and ni < 0:
            ribbon(x3 + nw, op_y, min(op_h, ni_h), x4, np_y, min(op_h, ni_h), RED)
        elif op >= 0 and ni >= 0:
            ribbon(x3 + nw, op_y, min(op_h, ni_h), x4, np_y + max(ni_h - op_h, 0), min(op_h, ni_h), GREEN)
    txt(x4 + nw + 10 * sx, np_y + max(ni_h, 2.5 * sy) / 2.0, labels["net_profit"], _bold(), 11.5, color=ni_color)

    if ox > 0 and spec["operating_expenses"]["children"]:
        kid_y = ox_y + lay["kids_offset"]
        kid_src = ox_y
        for item, label, height, gap in zip(
            spec["operating_expenses"]["children"], labels["children"], lay["child_heights"], lay["child_gaps"],
        ):
            source_h = abs(item["value"]) * scale
            color = RED if item["value"] >= 0 else GREEN
            node(x4, kid_y, height, color)
            ribbon(x3 + nw, kid_src, source_h, x4, kid_y, height, color)
            txt(x4 + nw + 10 * sx, kid_y + height / 2.0, label, _semi(), 10.5, color=color)
            kid_y += height + gap
            kid_src += source_h


def _semi():
    return load_fonts()["semi"]


def _bold():
    return load_fonts()["bold"]


def _open_image(path, raster_width=2000, crop=True):
    path = Path(path)
    if not path.is_file():
        raise SpecError(f"image not found: {path}")
    try:
        from PIL import Image
    except ImportError as exc:
        raise ChartDependencyError(PILLOW_HINT) from exc
    if path.suffix.lower() == ".svg":
        try:
            import cairosvg
        except ImportError as exc:
            raise ChartDependencyError(SVG_HINT) from exc
        png = cairosvg.svg2png(bytestring=path.read_bytes(), output_width=max(int(raster_width), 2000))
        image = Image.open(io.BytesIO(png)).convert("RGBA")
    else:
        image = Image.open(path).convert("RGBA")
    # Logos are cropped to their ink so a viewBox's padding does not change the
    # aspect ratio. Byline icons keep their canvas, transparent margin included.
    if crop:
        box = image.getbbox()
        if box:
            image = image.crop(box)
    return image


def _ink_mask(rgba):
    import numpy as np
    background = np.array([0xEC, 0xEC, 0xEC], dtype=np.int16)
    return np.max(np.abs(rgba[..., :3].astype(np.int16) - background), axis=2) > 8


def _bbox(mask):
    import numpy as np
    ys, xs = np.where(mask)
    if xs.size == 0:
        raise ChartDependencyError("could not measure chart ink for logo placement")
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _place_logo(fig, ax, logo_path, logo_w, lay):
    """Right-align the logo to the chart ink and center it on the title band."""
    import numpy as np
    fig.canvas.draw()
    rgba = np.asarray(fig.canvas.buffer_rgba())
    ink = _ink_mask(rgba)
    sy = lay["sy"]
    sx = lay["sx"]
    title_cut_y = max(1, int(round(80 * sy)))
    title_cut_x = max(1, int(round(800 * sx)))
    body_top = max(1, int(round(90 * sy)))
    body_bot = max(body_top + 1, int(round(660 * sy)))
    tx0, ty0, tx1, ty1 = _bbox(ink[:title_cut_y, :title_cut_x])
    title_cy = (ty0 + ty1) / 2.0
    _x0, _y0, chart_right, _y1 = _bbox(ink[body_top:body_bot, :])
    image = _open_image(logo_path)
    aspect = image.size[0] / image.size[1]
    fit_w = float(logo_w)
    fit_h = fit_w / aspect
    # The +2 lands the last non-fringe logo pixel on the chart's right ink,
    # which is what the reference placement does at 1280×720.
    px_right = chart_right + 2.0
    px = px_right - fit_w
    py = title_cy - fit_h / 2.0
    guard = 0
    while px - tx1 < 40 and fit_w > 80 and guard < 8:
        fit_w *= 0.85
        fit_h = fit_w / aspect
        px = px_right - fit_w
        py = title_cy - fit_h / 2.0
        guard += 1
    ax.imshow(
        np.array(image),
        extent=[px, px + fit_w, py + fit_h, py],
        interpolation="lanczos", zorder=4, aspect="auto",
    )
