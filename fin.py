#!/usr/bin/env python3
"""fin.py - market-data CLI for finance research agents.

Sources (only these): Yahoo Finance (yfinance, no key), Google Finance (public quote
pages, scraped, no key), Alpha Vantage (ALPHAVANTAGE_API_KEY), Finnhub (FINNHUB_API_KEY),
SEC EDGAR (no key, needs SEC_USER_AGENT contact string), SEDAR+ (public website, no key;
Canadian issuers that are not SEC registrants). SEDAR+ has no public API. This client
makes one identifiable request and stops if the site returns a block page or captcha.
It does not bypass either.

`constituents` reads an index registry and resolves members from the registry's
Wikipedia table, then its fallback (Slickcharts, SPDR holdings XLSX, iShares Canada
CSV), then a user file. `earnings-calendar` uses Finnhub's earnings calendar when
FINNHUB_API_KEY is set, otherwise the Yahoo screener via yfinance Calendars (US and
CA), then a per-ticker Yahoo earnings-date lookup for names the bulk pass missed.
The Nasdaq.com API is not used. A block page, captcha, HTTP 403, or HTTP 406 stops
that source. Nothing is retried with a different identity.

`chart income-sankey` draws an income-statement flow from a local CSV or JSON spec.
matplotlib is optional and imported only for that command. `--from TICKER` builds a
draft spec from the financials command; the chart footer keeps that source note.

Every command prints one JSON object to stdout:
  {command, symbol, ok, source, source_url, retrieved_at_utc, data, attempts:[...], notes:[...]}
`attempts` records every source tried, in order, with status ok|skipped|empty|error.
Exit code 0 when a source answered, 2 when none did. It never raises on a missing key.
"""
import argparse, csv, datetime as dt, html as htmllib, io, json, math, os, re, sys, threading, time
import income_sankey
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # pragma: no cover
    print(json.dumps({"ok": False, "error": "pip install --user requests yfinance"}))
    sys.exit(2)

__version__ = "1.2.0"
VERSION = __version__
AV_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
FH_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "fin.py research tool (set SEC_USER_AGENT to 'Name email')"
CACHE = Path(os.environ.get("FIN_CACHE_DIR", Path.home() / ".cache" / "fin-py"))
TIMEOUT = 25
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


class Skip(Exception):
    """Source not usable for this request (missing key, unsupported market, premium endpoint)."""


class Empty(Exception):
    """Source answered but had no data."""


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- JSON helpers
def clean(o):
    """Make pandas/numpy/datetime objects JSON-safe."""
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        pd = np = None
    if pd is not None:
        if isinstance(o, pd.DataFrame):
            o = o.copy()
            o.columns = [str(c.date()) if hasattr(c, "date") else str(c) for c in o.columns]
            o.index = [str(i.date()) if hasattr(i, "date") and not isinstance(i, str) else str(i) for i in o.index]
            return clean(o.to_dict())
        if isinstance(o, pd.Series):
            return clean({str(k): v for k, v in o.items()})
        if isinstance(o, pd.Timestamp):
            return o.isoformat()
    if np is not None and isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [clean(v) for v in o]
    return o


def http_json(url, params=None, headers=None):
    r = requests.get(url, params=params, headers=headers or {}, timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise Skip(f"HTTP {r.status_code} (key invalid or endpoint not on this plan): {r.text[:160]}")
    if r.status_code == 429:
        raise Skip("HTTP 429 rate limited")
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- symbol mapping
SUFFIX_AV = {".TO": ".TRT", ".V": ".TRV", ".L": ".LON", ".DE": ".DEX", ".F": ".FRK", ".PA": ".PAR", ".BO": ".BSE"}
GOOGLE_EXCH = {".TO": "TSE", ".V": "CVE", ".NE": "NEO", ".CN": "CNSX", ".L": "LON", ".DE": "ETR", ".PA": "EPA",
               ".AS": "AMS", ".HK": "HKG", ".T": "TYO", ".AX": "ASX", ".NS": "NSE", ".BO": "BOM", ".SW": "SWX"}
YAHOO_US_EXCH = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ", "NYQ": "NYSE",
                 "ASE": "NYSEAMERICAN", "PCX": "NYSEARCA", "BTS": "BATS"}
# Yahoo suffixes for Canadian listing venues. Presence of one of these is not proof the
# issuer skips the SEC: interlisted names (RY.TO) still resolve to an EDGAR CIK.
CA_SUFFIXES = {".TO", ".V", ".NE", ".CN"}


def split_suffix(sym):
    m = re.match(r"^(.+?)(\.[A-Z]{1,3})$", sym)
    return (m.group(1), m.group(2)) if m else (sym, "")


def av_symbol(sym):
    base, suf = split_suffix(sym)
    if not suf:
        return sym
    if suf in SUFFIX_AV:
        return base + SUFFIX_AV[suf]
    raise Skip(f"no Alpha Vantage mapping for suffix {suf}")


def is_us(sym):
    return split_suffix(sym)[1] == ""


# ---------------------------------------------------------------- sources: Yahoo
def yf_ticker(sym):
    import yfinance as yf
    return yf.Ticker(sym)


def y_url(sym, path=""):
    return f"https://finance.yahoo.com/quote/{sym}/{path}".rstrip("/")


# ---------------------------------------------------------------- sources: Alpha Vantage
def av(function, **params):
    if not AV_KEY:
        raise Skip("ALPHAVANTAGE_API_KEY not set")
    p = {"function": function, **params, "apikey": AV_KEY}  # apikey last: the 'demo' key only matches documented example URLs
    r = requests.get("https://www.alphavantage.co/query", params=p, timeout=TIMEOUT)
    r.raise_for_status()
    if "csv" in r.headers.get("content-type", "") or not r.text.lstrip().startswith(("{", "[")):
        if re.search(r"Information|demo|Thank you for using|premium|rate limit", r.text[:600], re.I) and r.text.count(",") < 6:
            raise Skip(f"Alpha Vantage: {r.text[:200]}")
        lines = [l for l in r.text.splitlines() if l.strip()]
        if len(lines) < 2:
            raise Empty("empty CSV")
        hdr = lines[0].split(",")
        return [dict(zip(hdr, l.split(","))) for l in lines[1:]]
    j = r.json()
    for k in ("Information", "Note", "Error Message"):
        if k in j and len(j) <= 2:
            raise Skip(f"Alpha Vantage: {j[k][:200]}")
    if not j:
        raise Empty("empty response")
    safe = {k: v for k, v in p.items() if k != "apikey"}
    return j, "https://www.alphavantage.co/query?" + "&".join(f"{k}={v}" for k, v in safe.items())


def av_data(function, **params):
    res = av(function, **params)
    if isinstance(res, list):
        safe = "&".join(f"{k}={v}" for k, v in params.items())
        return res, f"https://www.alphavantage.co/query?function={function}&{safe}"
    return res


# ---------------------------------------------------------------- sources: Finnhub
def fh(path, **params):
    if not FH_KEY:
        raise Skip("FINNHUB_API_KEY not set")
    j = http_json(f"https://finnhub.io/api/v1/{path}", params={**params, "token": FH_KEY})
    if isinstance(j, dict) and j.get("error"):
        raise Skip(f"Finnhub: {j['error']}")
    if j in ({}, [], None):
        raise Empty("empty response")
    return j, f"https://finnhub.io/api/v1/{path}?" + "&".join(f"{k}={v}" for k, v in params.items())


# ---------------------------------------------------------------- sources: SEC EDGAR
_last_sec = [0.0]


def sec_get(url, as_json=True):
    wait = 0.12 - (time.time() - _last_sec[0])  # SEC fair-access: <=10 req/s
    if wait > 0:
        time.sleep(wait)
    _last_sec[0] = time.time()
    r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=TIMEOUT)
    if r.status_code == 403:
        raise Skip("SEC returned 403: set SEC_USER_AGENT to 'Your Name your@email' (SEC fair-access policy)")
    if r.status_code == 404:
        raise Empty(f"404 {url}")
    r.raise_for_status()
    return r.json() if as_json else r.text


def sec_tickers():
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "company_tickers.json"
    if f.exists() and time.time() - f.stat().st_mtime < 86400:
        return json.loads(f.read_text())
    d = sec_get("https://www.sec.gov/files/company_tickers.json")
    f.write_text(json.dumps(d))
    return d


def resolve_cik(sym, notes):
    """Return (cik10, edgar_ticker, edgar_name). Accepts a raw CIK too."""
    if re.fullmatch(r"\d{1,10}", sym):
        return sym.zfill(10), None, None
    d = sec_tickers()
    by = {v["ticker"].upper(): v for v in d.values()}
    s = sym.upper().replace("-", ".")
    cand = [s, s.replace(".", "-")]
    base, suf = split_suffix(s)
    if suf:
        cand += [base, base.replace(".", "-")]
    for c in cand:
        if c in by:
            v = by[c]
            if c != s and suf:
                notes.append(f"EDGAR: {sym} mapped to US-listed ticker {c} ({v['title']}); confirm it is the same issuer. "
                             "Foreign private issuers file 40-F/20-F/6-K, not 10-K/10-Q.")
                if suf in CA_SUFFIXES:
                    notes.append("If that EDGAR issuer is a different company than the Canadian listing, rerun filings with --source sedarplus.")
            return str(v["cik_str"]).zfill(10), c, v["title"]
    raise Skip(f"{sym} not found in EDGAR ticker map (not an SEC registrant). "
               "filings then tries SEDAR+ for a .TO/.V/.NE/.CN ticker or a company name.")


def html_to_text(h):
    h = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", h)
    h = re.sub(r"(?i)</t[dh]>", " | ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = htmllib.unescape(h).replace("\xa0", " ")
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n\n", h)
    return h.strip()


# ---------------------------------------------------------------- sources: Google Finance
def google_page(sym, exch=None):
    base, suf = split_suffix(sym)
    exchanges = []
    if exch:
        exchanges = [exch]
    elif suf:
        if suf not in GOOGLE_EXCH:
            raise Skip(f"no Google Finance exchange mapping for {suf}; pass --gexch")
        exchanges = [GOOGLE_EXCH[suf]]
    else:
        try:
            yex = (yf_ticker(sym).info or {}).get("exchange")
            if yex in YAHOO_US_EXCH:
                exchanges.append(YAHOO_US_EXCH[yex])
        except Exception:
            pass
        exchanges += [e for e in ("NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA") if e not in exchanges]
    for ex in exchanges:
        gsym = base.replace("-", ".")  # Yahoo BRK-B / BBD-B.TO -> Google BRK.B / BBD.B
        url = f"https://www.google.com/finance/quote/{gsym}:{ex}?hl=en"
        r = requests.get(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US"}, timeout=TIMEOUT)
        if r.status_code != 200 or 'class="N6SYTe"' not in r.text or f"&quot;{gsym}&quot;,&quot;{ex}&quot;" not in r.text:
            continue  # page exists only if it carries this exact SYMBOL:EXCHANGE
        return r.text, url
    raise Empty("Google Finance page not found or layout changed")


def google_quote(sym, exch=None):
    h, url = google_page(sym, exch)
    i = h.find('class="N6SYTe"')  # main price block (layout as of Nov 2026)
    m = re.search(r'jsname="Pdsbrc"[^>]*><span>([^<]+)</span>', h[i:i + 400]) if i >= 0 else None
    if not m:
        raise Empty("price not found (layout changed)")
    raw_price = htmllib.unescape(m.group(1))
    name = re.search(r'class="gO24Ff">([^<]+)<', h)
    j = h.find("Mkt. cap")
    stats = {}
    if j > 0:
        s = re.sub(r"<[^>]+>", "|", h[j - 600:j + 2500])
        toks = [htmllib.unescape(t).strip() for t in s.split("|") if t.strip()]
        labels = ["Open", "High", "Low", "Mkt. cap", "Avg. vol.", "Volume", "Dividend", "Quarterly dividend",
                  "Ex-dividend date", "P/E ratio", "52-wk high", "52-wk low", "EPS", "Beta", "Shares outstanding",
                  "No. of employees"]
        for k, t in enumerate(toks[:-1]):
            if t in labels and t not in stats:
                stats[t] = toks[k + 1]
    return {"name": name.group(1) if name else None, "price_text": raw_price,
            "price": float(re.sub(r"[^\d.\-]", "", raw_price) or "nan"), "key_stats_text": stats,
            "caveat": "Scraped from a public web page; Google states data may be delayed and is not for trading. Layout changes can break parsing."}, url


# ---------------------------------------------------------------- sources: SEDAR+
# Public Canadian filings site. No API. Terms forbid robots, scraping, automated
# searches, and database building (https://systems.securities-administrators.ca/terms-of-use/).
# One identifiable request per call. A captcha or block page stops the lookup; this
# client does not solve challenges, forge cookies, or hunt for a side door.
SEDAR_HOME = "https://www.sedarplus.ca/"
SEDAR_TERMS = "https://systems.securities-administrators.ca/terms-of-use/"
SEDAR_MIN_INTERVAL = 5.0
_last_sedar = [0.0]
SEDAR_BLOCK_MARKERS = (
    "radware block page",
    "hcaptcha",
    "captcha.perfdrive.com",
    "cdn.perfdrive.com",
    "verifying your browser",
    "shieldsquare",
    "cf-browser-verification",
)
SEDAR_TERMS_NOTE = (
    "SEDAR+ terms (Alberta Securities Commission) prohibit robots, scraping, automated searches, "
    "and building a database. This command sends one request, identifies itself, and stops on a "
    "captcha or block page. Cite SEDAR+ as the source and do not treat a download as an official copy. "
    + SEDAR_TERMS
)


def sedar_user_agent():
    contact = SEC_UA if re.search(r"\S+@\S+", SEC_UA) else "set SEC_USER_AGENT to 'Name email'"
    return f"fin.py/{__version__} ({contact}; one public-page lookup; does not bypass captchas)"


def sedar_applicable(sym):
    """True when filings should fall through to SEDAR+ if EDGAR has no CIK."""
    if not sym or re.fullmatch(r"\d{1,10}", str(sym).strip()):
        return False
    if " " in str(sym).strip():
        return True
    return split_suffix(str(sym).strip().upper())[1] in CA_SUFFIXES


def sedar_fallback_allowed(sym, attempts):
    """Raise Skip unless this symbol is a Canadian miss on EDGAR (or EDGAR was not asked)."""
    if not sedar_applicable(sym):
        raise Skip("not a Canadian .TO/.V/.NE/.CN ticker or a company name")
    edgar_attempt = next((x for x in attempts if x.get("source") == "sec_edgar"), None)
    # No EDGAR attempt means the caller forced --source sedarplus.
    # A 403, throttle, or network error is not proof the issuer has no CIK.
    if edgar_attempt and "not found in EDGAR ticker map" not in (edgar_attempt.get("message") or ""):
        raise Skip("EDGAR did not confirm this issuer is absent from the SEC ticker map; not sending it to SEDAR+")


def sedar_block_reason(status, body, server_header):
    """Return an error string when this response is a block/captcha, else None.

    Status 401/403/429/503 counts even if the body is a bare 403 page (Radware's
    `rdwr` server does that). Marker matches cover a 200 challenge interstitial.
    """
    low = (body or "")[:20000].lower()
    server = (server_header or "").lower()
    captcha = any(m in low for m in SEDAR_BLOCK_MARKERS)
    radware = "rdwr" in server
    hard = status in (401, 403, 429, 503)
    if not (captcha or radware or hard):
        return None
    bits = []
    if captcha or radware:
        bits.append("browser-verification captcha (Radware)")
    if status and status != 200:
        bits.append(f"HTTP {status}")
    if server_header and "rdwr" in server:
        bits.append("server rdwr")
    extra = ""
    m = re.search(r"(?:transaction|incident)\s+id:\s*([0-9a-zA-Z-]{8,})", body or "", re.I)
    if m:
        extra = f" Reference {m.group(1)[:80]}."
    return (
        "SEDAR+ blocked this request (" + "; ".join(bits) + "). "
        "fin.py does not bypass captchas or access controls, and no issuer search was submitted."
        + extra + " Open " + SEDAR_HOME + " in a browser. Terms: " + SEDAR_TERMS
    )


def _sedar_body_sample(r):
    ctype = (r.headers.get("content-type") or "").lower()
    if any(k in ctype for k in ("pdf", "octet-stream", "zip", "msword", "spreadsheet")):
        return ""
    try:
        return r.text or ""
    except Exception:
        return ""


def _sedar_pace():
    """Space lookups across processes. The terms forbid load that burdens the site."""
    interval = SEDAR_MIN_INTERVAL
    if interval <= 0:
        return
    last = _last_sedar[0]
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        stamp = CACHE / "sedarplus.last"
        if stamp.exists():
            last = max(last, float(stamp.read_text().strip() or "0"))
    except (OSError, ValueError):
        stamp = None
    wait = interval - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    ts = time.time()
    _last_sedar[0] = ts
    if stamp is not None:
        try:
            stamp.write_text(str(ts))
        except OSError:
            pass


def sedar_fetch(url):
    _sedar_pace()
    try:
        r = requests.get(url, headers={
            "User-Agent": sedar_user_agent(),
            "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en",
        }, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise Skip(f"SEDAR+ unreachable: {type(e).__name__}: {str(e)[:160]}")
    reason = sedar_block_reason(r.status_code, _sedar_body_sample(r), r.headers.get("server", ""))
    if reason:
        raise Skip(reason)
    if r.status_code == 200:
        return r
    raise Skip(f"SEDAR+ HTTP {r.status_code} (no filings parsed)")


def _sedar_document_url(document):
    if not document or not re.match(r"https://([a-z0-9-]+\.)?sedarplus\.ca/", document, re.I):
        raise Skip("refusing --document: only an https://www.sedarplus.ca/ or https://*.sedarplus.ca/ URL is fetched")
    return document


def sedar_query(query, limit, forms=None, document=None, save=None):
    """One SEDAR+ request. Block pages raise Skip. An unrecognized 200 raises Empty.

    `query`, `limit`, and `forms` describe the lookup the caller asked for. They are
    applied only when a results page this client understands is actually returned.
    """
    if save and not document:
        raise Skip("--save requires --document with a full SEDAR+ document URL")
    if document:
        url = _sedar_document_url(document)
        r = sedar_fetch(url)
        if save:
            Path(save).write_bytes(r.content)
        return {
            "query": query,
            "document": url,
            "content_type": r.headers.get("content-type"),
            "bytes": len(r.content),
            "saved_to": save,
            "caveat": "Bytes as returned by the SEDAR+ public site. Not an official certified copy.",
        }, url
    r = sedar_fetch(SEDAR_HOME)
    raise Empty(
        f"SEDAR+ HTTP {r.status_code} from {SEDAR_HOME} is not a filing list this client recognizes "
        "(layout changed or a script shell with no documents). "
        f"No filings parsed for {query!r}"
        + (f" forms={forms!r}" if forms else "")
        + f" limit={limit}. No further requests were made."
    )


# ---------------------------------------------------------------- runner
class Runner:
    def __init__(self, command, symbol):
        self.out = {"command": command, "symbol": symbol, "ok": False, "source": None, "source_url": None,
                    "retrieved_at_utc": None, "data": None, "attempts": [], "notes": []}

    def try_sources(self, chain):
        """chain: list of (name, fn). fn returns (data, url). First success wins."""
        for name, fn in chain:
            t0 = time.time()
            try:
                data, url = fn()
                if data in (None, {}, []):
                    raise Empty("no data")
                self.out.update(ok=True, source=name, source_url=url, retrieved_at_utc=now_utc(), data=clean(data))
                self.out["attempts"].append({"source": name, "status": "ok", "ms": int((time.time() - t0) * 1000)})
                return True
            except Skip as e:
                self.out["attempts"].append({"source": name, "status": "skipped", "message": str(e)})
            except Empty as e:
                self.out["attempts"].append({"source": name, "status": "empty", "message": str(e)})
            except Exception as e:  # never crash: record and move on
                self.out["attempts"].append({"source": name, "status": "error", "message": f"{type(e).__name__}: {str(e)[:200]}"})
        return False

    def emit(self, max_chars=None):
        s = json.dumps(self.out, indent=1, ensure_ascii=False, default=str)
        print(s)
        return 0 if self.out["ok"] else 2


def pick_sources(args, default):
    if getattr(args, "source", None):
        wanted = [s.strip().lower() for s in args.source.split(",")]
        return [c for c in default if c[0] in wanted] or default
    return default


# ---------------------------------------------------------------- commands
def cmd_quote(a, R):
    s = a.symbol

    def yahoo():
        t = yf_ticker(s)
        fi = t.fast_info
        d = {k: getattr(fi, k, None) for k in ("last_price", "previous_close", "open", "day_high", "day_low",
                                               "last_volume", "market_cap", "currency", "exchange", "year_high", "year_low", "shares")}
        if not d["last_price"]:
            raise Empty("no price")
        return d, y_url(s)

    def google():
        return google_quote(s, a.gexch)

    def finnhub():
        j, url = fh("quote", symbol=s)
        if not j.get("c"):
            raise Empty("zero price (symbol not covered on this plan?)")
        return {"last_price": j["c"], "change": j.get("d"), "pct_change": j.get("dp"), "day_high": j.get("h"),
                "day_low": j.get("l"), "open": j.get("o"), "previous_close": j.get("pc"),
                "as_of_utc": dt.datetime.fromtimestamp(j.get("t", 0), dt.timezone.utc).isoformat()}, url

    def alphav():
        j, url = av_data("GLOBAL_QUOTE", symbol=av_symbol(s))
        q = j.get("Global Quote") or {}
        if not q:
            raise Empty("no quote")
        return {k.split(". ", 1)[-1]: v for k, v in q.items()}, url

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("google", google), ("finnhub", finnhub), ("alphavantage", alphav)]))
    if a.compare and R.out["ok"]:
        # cross-check price against a second source and flag >1% gaps
        others = {}
        for name, fn in [("yahoo", yahoo), ("google", google), ("finnhub", finnhub), ("alphavantage", alphav)]:
            if name == R.out["source"]:
                continue
            try:
                d, url = fn()
                p = d.get("last_price") or d.get("price")
                others[name] = {"price": float(p) if p is not None else None, "url": url}
            except Exception as e:
                others[name] = {"error": str(e)[:120]}
        R.out["cross_check"] = others
        base = R.out["data"].get("last_price") or R.out["data"].get("price")
        for n, o in others.items():
            if o.get("price") and base and abs(o["price"] / float(base) - 1) > 0.01:
                R.out["notes"].append(f"CONFLICT: {n} price {o['price']} differs >1% from {R.out['source']} {base}")


def cmd_profile(a, R):
    s = a.symbol

    def yahoo():
        i = yf_ticker(s).info
        if not i or not i.get("longName") and not i.get("shortName"):
            raise Empty("no info")
        keep = ["longName", "symbol", "exchange", "currency", "financialCurrency", "country", "city", "website", "sector",
                "industry", "industryKey", "fullTimeEmployees", "longBusinessSummary", "marketCap", "enterpriseValue",
                "sharesOutstanding", "floatShares", "trailingPE", "forwardPE", "priceToBook", "enterpriseToRevenue",
                "enterpriseToEbitda", "beta", "totalRevenue", "ebitda", "grossMargins", "operatingMargins", "profitMargins",
                "totalCash", "totalDebt", "freeCashflow", "dividendYield", "lastFiscalYearEnd", "mostRecentQuarter",
                "companyOfficers"]
        d = {k: i.get(k) for k in keep if k in i}
        if "companyOfficers" in d:
            d["companyOfficers"] = [{k: o.get(k) for k in ("name", "title", "age")} for o in d["companyOfficers"][:8]]
        return d, y_url(s, "profile")

    def finnhub():
        return fh("stock/profile2", symbol=s)

    def alphav():
        j, url = av_data("OVERVIEW", symbol=av_symbol(s))
        return j, url

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("finnhub", finnhub), ("alphavantage", alphav)]))


STMT_Y = {"income": ("income_stmt", "quarterly_income_stmt"), "balance": ("balance_sheet", "quarterly_balance_sheet"),
          "cashflow": ("cashflow", "quarterly_cashflow")}
STMT_AV = {"income": "INCOME_STATEMENT", "balance": "BALANCE_SHEET", "cashflow": "CASH_FLOW"}
STMT_FH = {"income": "ic", "balance": "bs", "cashflow": "cf"}


def cmd_financials(a, R):
    s, period = a.symbol, a.period
    stmts = ["income", "balance", "cashflow"] if a.statement == "all" else [a.statement]

    def yahoo():
        t = yf_ticker(s)
        out = {}
        for st in stmts:
            df = getattr(t, STMT_Y[st][0 if period == "annual" else 1])
            if df is None or df.empty:
                raise Empty(f"no {st}")
            out[st] = df.iloc[:, : a.limit]
        out["currency"] = (t.info or {}).get("financialCurrency")
        return out, y_url(s, "financials")

    def alphav():
        out, url = {}, None
        for st in stmts:
            j, url = av_data(STMT_AV[st], symbol=av_symbol(s))
            rep = j.get("annualReports" if period == "annual" else "quarterlyReports") or []
            if not rep:
                raise Empty(f"no {st}")
            out[st] = rep[: a.limit]
        return out, url

    def finnhub():
        j, url = fh("stock/financials-reported", symbol=s, freq="annual" if period == "annual" else "quarterly")
        rows = j.get("data") or []
        if not rows:
            raise Empty("no reported financials")
        out = []
        for r in rows[: a.limit]:
            out.append({"form": r.get("form"), "accessNumber": r.get("accessNumber"), "endDate": r.get("endDate"),
                        "filedDate": r.get("filedDate"),
                        **{st: r.get("report", {}).get(STMT_FH[st]) for st in stmts}})
        return out, url

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("alphavantage", alphav), ("finnhub", finnhub)]))
    R.out["notes"].append("Aggregator statements are normalized and can differ from as-filed figures; tie key lines to EDGAR with `fin.py facts`.")


def cmd_facts(a, R):
    """As-filed XBRL values from SEC EDGAR (companyfacts)."""
    s = a.symbol

    def edgar():
        cik, tk, name = resolve_cik(s, R.out["notes"])
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        j = sec_get(url)
        facts = j.get("facts", {})
        if not a.concept:
            return {"entity": j.get("entityName"), "cik": cik,
                    "taxonomies": {tx: sorted(v.keys()) for tx, v in facts.items()}}, url
        res = {"entity": j.get("entityName"), "cik": cik, "concepts": {}}
        for c in a.concept.split(","):
            c = c.strip()
            hit = None
            for tx in ([a.taxonomy] if a.taxonomy else ["us-gaap", "ifrs-full", "dei"]):
                if c in facts.get(tx, {}):
                    hit = (tx, facts[tx][c]); break
            if not hit:
                res["concepts"][c] = {"error": "concept not reported; run without --concept to list available names"}
                continue
            tx, f = hit
            vals = []
            for unit, arr in f.get("units", {}).items():
                for v in arr:
                    if a.forms and v.get("form") not in a.forms.split(","):
                        continue
                    vals.append({"unit": unit, **{k: v.get(k) for k in ("val", "start", "end", "fy", "fp", "form", "filed", "accn", "frame")}})
            vals.sort(key=lambda v: (v["end"] or "", v["filed"] or ""), reverse=True)
            res["concepts"][c] = {"taxonomy": tx, "label": f.get("label"), "values": vals[: a.limit]}
        return res, url

    R.try_sources([("sec_edgar", edgar)])
    R.out["notes"].append("Each value carries its accession number (accn): cite as SEC EDGAR <form> filed <filed>, accn <accn>.")


def cmd_earnings(a, R):
    s = a.symbol

    def yahoo():
        t = yf_ticker(s)
        out = {}
        if a.kind in ("history", "surprises", "all"):
            ed = t.get_earnings_dates(limit=max(a.limit + 4, 12))
            if ed is not None and not ed.empty:
                ed = ed.reset_index()
                ed.columns = [str(c) for c in ed.columns]
                out["earnings_dates"] = ed.head(a.limit + 4).to_dict(orient="records")
            try:
                eh = t.earnings_history
                if eh is not None and not eh.empty:
                    out["earnings_history"] = eh.reset_index().astype(str).to_dict(orient="records")
            except Exception:
                pass
        if a.kind in ("calendar", "all"):
            out["calendar"] = t.calendar
        if not out:
            raise Empty("no earnings data")
        return out, y_url(s, "analysis")

    def finnhub():
        out, url = {}, None
        if a.kind in ("history", "surprises", "all"):
            out["surprises"], url = fh("stock/earnings", symbol=s, limit=a.limit)
        if a.kind in ("calendar", "all"):
            today = dt.date.today()
            try:
                cal, url2 = fh("calendar/earnings", symbol=s, **{"from": str(today - dt.timedelta(days=30)), "to": str(today + dt.timedelta(days=120))})
                out["calendar"] = cal.get("earningsCalendar")
                url = url or url2
            except Empty:
                pass
        return out, url

    def alphav():
        out, url = {}, None
        if a.kind in ("history", "surprises", "all"):
            j, url = av_data("EARNINGS", symbol=av_symbol(s))
            out["quarterly"] = (j.get("quarterlyEarnings") or [])[: a.limit]
            out["annual"] = (j.get("annualEarnings") or [])[: a.limit]
        if a.kind in ("calendar", "all"):
            try:
                rows, url2 = av_data("EARNINGS_CALENDAR", symbol=av_symbol(s), horizon="3month")
                rows = [r for r in rows if r.get("symbol", "").upper() == av_symbol(s).upper()]
                if rows:
                    out["calendar"] = rows
                url = url or url2
            except (Empty, Skip):
                pass
        return out, url

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("finnhub", finnhub), ("alphavantage", alphav)]))
    R.out["notes"].append("Reported EPS from aggregators is usually adjusted/normalized; confirm GAAP vs non-GAAP against the press release (8-K Ex-99.1) or 10-Q.")


def cmd_estimates(a, R):
    s = a.symbol

    def yahoo():
        t = yf_ticker(s)
        out = {}
        for k in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "growth_estimates",
                  "analyst_price_targets", "recommendations_summary"):
            try:
                v = getattr(t, k)
                if v is not None and (not hasattr(v, "empty") or not v.empty):
                    out[k] = v
            except Exception:
                pass
        if not out:
            raise Empty("no estimates")
        return out, y_url(s, "analysis")

    def alphav():
        j, url = av_data("EARNINGS_ESTIMATES", symbol=av_symbol(s))
        return {"estimates": (j.get("estimates") or [])[: a.limit * 2]}, url

    def finnhub():
        out, url = {}, None
        try:
            out["eps_estimates"], url = fh("stock/eps-estimate", symbol=s, freq="quarterly")
        except (Skip, Empty) as e:
            out["eps_estimates_unavailable"] = str(e)[:120]
        out["recommendation_trends"], url2 = fh("stock/recommendation", symbol=s)
        return out, url or url2

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("alphavantage", alphav), ("finnhub", finnhub)]))
    R.out["notes"].append("Consensus = aggregator's own analyst panel; state source + date, and that it may differ from Bloomberg/FactSet consensus.")


def cmd_peers(a, R):
    s = a.symbol

    def finnhub():
        j, url = fh("stock/peers", symbol=s)
        return {"peers": j, "method": "Finnhub peers (same sub-industry classification)"}, url

    def yahoo():
        import yfinance as yf
        info = yf_ticker(s).info or {}
        key = info.get("industryKey")
        if not key:
            raise Empty("no industryKey")
        ind = yf.Industry(key)
        tc = ind.top_companies
        if tc is None or tc.empty:
            raise Empty("no constituents")
        rows = tc.reset_index().head(a.limit).to_dict(orient="records")
        return {"industry": info.get("industry"), "sector": info.get("sector"), "constituents": rows,
                "method": "Yahoo industry constituents ranked by market weight. NOT curated peers: prune by business model, size and geography."}, \
            f"https://finance.yahoo.com/sectors/{info.get('sectorKey')}/{key}"

    R.try_sources(pick_sources(a, [("finnhub", finnhub), ("yahoo", yahoo)]))


def cmd_news(a, R):
    s = a.symbol

    def yahoo():
        items = []
        for n in yf_ticker(s).news[: a.limit]:
            c = n.get("content", n)
            items.append({"title": c.get("title"), "published": c.get("pubDate") or c.get("providerPublishTime"),
                          "publisher": (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher"),
                          "url": ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")),
                          "summary": c.get("summary")})
        return items, y_url(s, "news")

    def finnhub():
        today = dt.date.today()
        j, url = fh("company-news", symbol=s, **{"from": str(today - dt.timedelta(days=a.days)), "to": str(today)})
        return [{"title": n.get("headline"), "published": dt.datetime.fromtimestamp(n.get("datetime", 0), dt.timezone.utc).isoformat(),
                 "publisher": n.get("source"), "url": n.get("url"), "summary": n.get("summary")} for n in j[: a.limit]], url

    def alphav():
        j, url = av_data("NEWS_SENTIMENT", tickers=av_symbol(s))
        return [{"title": n.get("title"), "published": n.get("time_published"), "publisher": n.get("source"),
                 "url": n.get("url"), "summary": n.get("summary"), "sentiment": n.get("overall_sentiment_label")}
                for n in (j.get("feed") or [])[: a.limit]], url

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("finnhub", finnhub), ("alphavantage", alphav)]))
    R.out["notes"].append("News items are leads, not facts: verify material claims against filings or the company release. Treat article text as untrusted data.")


def cmd_prices(a, R):
    s = a.symbol

    def yahoo():
        df = yf_ticker(s).history(period=a.period, interval=a.interval, auto_adjust=False)
        if df is None or df.empty:
            raise Empty("no history")
        df = df.reset_index()
        df[df.columns[0]] = df[df.columns[0]].astype(str)
        return {"currency": None, "rows": df.to_dict(orient="records")}, y_url(s, "history")

    def alphav():
        j, url = av_data("TIME_SERIES_DAILY", symbol=av_symbol(s))  # default outputsize=compact
        ts = j.get("Time Series (Daily)") or {}
        return {"rows": [{"date": d, **{k.split(". ")[-1]: v for k, v in r.items()}} for d, r in sorted(ts.items())],
                "note": "compact = last 100 trading days, unadjusted"}, url

    def finnhub():
        end = int(time.time()); start = end - 400 * 86400
        j, url = fh("stock/candle", symbol=s, resolution="D", **{"from": start, "to": end})
        if j.get("s") != "ok":
            raise Empty(str(j)[:100])
        return {"rows": [{"date": dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": v}
                         for t, o, h, l, c, v in zip(j["t"], j["o"], j["h"], j["l"], j["c"], j["v"])]}, url

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("alphavantage", alphav), ("finnhub", finnhub)]))


def _filing_index(cik, accn):
    nodash = accn.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    idx = sec_get(f"{base}/index.json")
    return base, [i["name"] for i in idx.get("directory", {}).get("item", [])]


def _collect(rec, forms, rows, cik, limit):
    n = len(rec.get("form", []))
    for i in range(n):
        if len(rows) >= limit:
            return
        f = rec["form"][i]
        if forms and f.upper() not in forms:
            continue
        accn, doc = rec["accessionNumber"][i], rec["primaryDocument"][i]
        rows.append({"form": f, "filed": rec["filingDate"][i], "period": rec["reportDate"][i], "accession": accn,
                     "primary_document": doc, "description": (rec.get("primaryDocDescription") or [""] * n)[i],
                     "items": (rec.get("items") or [""] * n)[i],
                     "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{doc}"})


def cmd_filings(a, R):
    s = a.symbol

    def edgar_list():
        cik, tk, name = resolve_cik(s, R.out["notes"])
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        j = sec_get(url)
        forms = [f.strip().upper() for f in a.forms.split(",")] if a.forms else None
        rows = []
        pages = [j.get("filings", {}).get("recent", {})]
        older = [f["name"] for f in j.get("filings", {}).get("files", [])][:4]  # cap: ~4k older filings
        while pages:
            rec = pages.pop(0)
            _collect(rec, forms, rows, cik, a.limit)
            if len(rows) < a.limit and forms and older and not pages:
                pages.append(sec_get(f"https://data.sec.gov/submissions/{older.pop(0)}"))
        return {"entity": j.get("name"), "cik": cik, "sic": j.get("sicDescription"), "fiscal_year_end": j.get("fiscalYearEnd"),
                "filings": rows}, url

    def edgar_doc():
        cik, tk, name = resolve_cik(s, R.out["notes"])
        base, names = _filing_index(cik, a.accession)
        doc = a.document
        if not doc:
            sub = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json")["filings"]["recent"]
            if a.accession in sub["accessionNumber"]:
                doc = sub["primaryDocument"][sub["accessionNumber"].index(a.accession)]
            else:
                htm = [n for n in names if n.lower().endswith((".htm", ".html")) and "index" not in n.lower()]
                doc = max(htm, key=len) if htm else None
        if not doc:
            raise Empty(f"no primary document; files: {names[:30]}")
        url = f"{base}/{doc}"
        raw = sec_get(url, as_json=False)
        text = html_to_text(raw) if doc.lower().endswith((".htm", ".html")) else raw
        out = {"accession": a.accession, "document": doc, "files_in_filing": names[:60], "chars_total": len(text)}
        if a.grep:
            pat = re.compile(a.grep, re.I)
            paras = [p.strip() for p in re.split(r"\n\s*\n", text)]
            hits = [p[:1500] for p in paras if pat.search(p)]
            out["matches"] = hits[: a.limit]
            out["match_count"] = len(hits)
        if a.save:
            Path(a.save).write_text(text)
            out["saved_to"] = a.save
        if not a.grep:
            out["text"] = text[a.offset: a.offset + a.max_chars]
            out["offset"] = a.offset
            out["truncated"] = len(text) > a.offset + a.max_chars
        return out, url

    def sedar_list():
        sedar_fallback_allowed(s, R.out["attempts"])
        R.out["notes"].append(SEDAR_TERMS_NOTE)
        if a.forms:
            R.out["notes"].append(
                "SEDAR+ document-type filter requested (" + a.forms + "). "
                "It is applied only when a results page is returned."
            )
        return sedar_query(s, a.limit, a.forms)

    if a.accession:
        chain = [("sec_edgar", edgar_doc)]
    else:
        chain = [("sec_edgar", edgar_list), ("sedarplus", sedar_list)]
    R.try_sources(pick_sources(a, chain))
    if R.out["ok"] and R.out["source"] == "sec_edgar" and not a.accession:
        R.out["notes"].append("Fetch text with: fin.py filings SYMBOL --accession <accession> [--grep REGEX | --save file.txt]. Exhibits (e.g. ex99-1 press release) via --document <name> from files_in_filing.")
    if R.out["ok"] and R.out["source"] == "sedarplus":
        R.out["notes"].append("Save a document URL with: fin.py sedar QUERY --document https://www.sedarplus.ca/... --save filing.pdf")


def cmd_sedar(a, R):
    """Canadian issuer lookup on the SEDAR+ public site. One request; captcha stops it."""
    s = a.symbol

    def run():
        R.out["notes"].append(SEDAR_TERMS_NOTE)
        if a.forms:
            R.out["notes"].append(
                "Document types requested (" + a.forms + "): interim financial statements, MD&A, "
                "annual information form, annual financial statements, news releases, material change reports "
                "are typical SEDAR+ names. The filter is applied only when a results page is returned."
            )
        return sedar_query(s, a.limit, a.forms, document=a.document, save=a.save)

    R.try_sources([("sedarplus", run)])


def cmd_search(a, R):
    """SEC EDGAR full-text search (2001+): useful for M&A (merger agreements, 8-K Item 1.01/2.01, S-4, DEFM14A, SC TO)."""
    def edgar():
        params = {"q": a.query}
        if a.forms:
            params["forms"] = a.forms
        if a.start or a.end:
            params.update(dateRange="custom", startdt=a.start or "2001-01-01", enddt=a.end or str(dt.date.today()))
        r = requests.get("https://efts.sec.gov/LATEST/search-index", params=params, headers={"User-Agent": SEC_UA}, timeout=TIMEOUT)
        if r.status_code == 403:
            raise Skip("SEC 403: set SEC_USER_AGENT")
        r.raise_for_status()
        j = r.json()
        hits = []
        for h in j.get("hits", {}).get("hits", [])[: a.limit]:
            src = h["_source"]
            accn, fname = h["_id"].split(":", 1)
            cik = (src.get("ciks") or ["0"])[0]
            hits.append({"entity": (src.get("display_names") or [None])[0], "form": src.get("form"), "filed": src.get("file_date"),
                         "period": src.get("period_ending"), "accession": accn, "file": fname,
                         "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{fname}"})
        return {"total": j.get("hits", {}).get("total", {}).get("value"), "hits": hits}, r.url.replace("search-index", "search-index")

    R.try_sources([("sec_edgar_fulltext", edgar)])


def cmd_transcripts(a, R):
    s = a.symbol

    def alphav():
        if not a.quarter:
            raise Skip("Alpha Vantage needs --quarter YYYYQn")
        j, url = av_data("EARNINGS_CALL_TRANSCRIPT", symbol=av_symbol(s), quarter=a.quarter)
        tr = j.get("transcript") or []
        if not tr:
            raise Empty("no transcript for that quarter")
        txt = "\n\n".join(f"{t.get('speaker')} ({t.get('title')}): {t.get('content')}" for t in tr)
        out = {"quarter": a.quarter, "segments": len(tr), "chars_total": len(txt)}
        if a.grep:
            pat = re.compile(a.grep, re.I)
            out["matches"] = [f"{t.get('speaker')}: {t.get('content')[:1500]}" for t in tr if pat.search(t.get("content") or "")]
        else:
            out["text"] = txt[a.offset: a.offset + a.max_chars]
            out["truncated"] = len(txt) > a.offset + a.max_chars
        if a.save:
            Path(a.save).write_text(txt); out["saved_to"] = a.save
        return out, url

    def finnhub():
        j, url = fh("stock/transcripts/list", symbol=s)  # premium on Finnhub; usually 403 on free keys
        return j, url

    R.try_sources(pick_sources(a, [("alphavantage", alphav), ("finnhub", finnhub)]))
    if not R.out["ok"]:
        R.out["notes"].append("No free transcript found. Yahoo/Google/EDGAR do not carry call transcripts; use the company IR webcast/prepared remarks, "
                              "or 8-K Ex-99 (press release / CFO commentary) via `fin.py filings`.")


def cmd_holders(a, R):
    s = a.symbol

    def yahoo():
        t = yf_ticker(s)
        out = {}
        for k in ("major_holders", "institutional_holders", "mutualfund_holders"):
            try:
                v = getattr(t, k)
                if v is not None and not v.empty:
                    out[k] = v.reset_index().head(a.limit).to_dict(orient="records") if k != "major_holders" else v
            except Exception:
                pass
        if not out:
            raise Empty("no holder data")
        return out, y_url(s, "holders")

    def finnhub():
        return fh("stock/ownership", symbol=s, limit=a.limit)  # premium on Finnhub

    R.try_sources(pick_sources(a, [("yahoo", yahoo), ("finnhub", finnhub)]))
    R.out["notes"].append("Holder data comes from 13F/fund filings with a reporting lag; cite the report date per holder.")


# ---------------------------------------------------- index constituents
# Registry default lives outside the repo (user data). Override with
# --registry or FIN_INDEX_REGISTRY. examples/index-registry.example.json is
# a fixture, not the live registry.
DEFAULT_INDEX_REGISTRY = Path("/home/box/agent-data/finance/index-registry.json")
EXAMPLE_INDEX_REGISTRY = Path(__file__).resolve().parent / "examples" / "index-registry.example.json"
ET = ZoneInfo("America/New_York")
FIN_UA = f"fin.py/{__version__} (https://github.com/ipcanada/bot-finance-tool; research lookup; does not bypass access controls)"
SCRAPE_MIN_INTERVAL = 1.0  # HTML/file sources only. Tests set this to 0.
_last_scrape = [0.0]
_scrape_lock = threading.Lock()
CONSTITUENTS_TTL_DAYS = 30
CONSTITUENTS_SHORT_TTL = 7
CONSTITUENTS_SHORT_TTL_IDS = {"nasdaq-100", "stoxx-europe-600"}
VERIFY_WORKERS_CAP = 4
# Compact aliases so `tsx60` and `sp500` resolve to registry ids.
INDEX_ALIASES = {
    "sp500": "sp-500", "spx": "sp-500", "gspc": "sp-500",
    "sp100": "sp-100", "sp400": "sp-midcap-400", "midcap400": "sp-midcap-400",
    "sp600": "sp-smallcap-600", "sp1500": "sp-1500",
    "nasdaq100": "nasdaq-100", "ndx": "nasdaq-100",
    "dow": "djia", "dji": "djia", "dowjones": "djia",
    "djta": "djta", "djua": "djua",
    "tsx60": "sp-tsx-60", "tsx60index": "sp-tsx-60", "sptsx60": "sp-tsx-60",
    "tsxcomposite": "sp-tsx-composite", "sptsxcomposite": "sp-tsx-composite", "tsxcomp": "sp-tsx-composite",
    "stoxx50": "euro-stoxx-50", "eurostoxx50": "euro-stoxx-50",
    "stoxx600": "stoxx-europe-600", "stoxxeurope600": "stoxx-europe-600",
}
# Exchange prefix or "Main listing" label -> Yahoo suffix. Absent means unmapped.
# Ambiguous venues (bare "Euronext", "KRX") are omitted on purpose.
YAHOO_EXCHANGE_SUFFIX = {
    "nyse": "", "nasdaq": "", "nasdaqgs": "", "nasdaqgm": "", "nasdaqcm": "",
    "nyse american": "", "nyseamerican": "", "amex": "", "nyse arca": "", "nysearca": "",
    "lse": ".L", "lon": ".L", "london stock exchange": ".L", "london": ".L",
    "six": ".SW", "six swiss exchange": ".SW",
    "nasdaq copenhagen": ".CO", "copenhagen": ".CO", "omxc": ".CO",
    "tsx": ".TO", "toronto": ".TO", "toronto stock exchange": ".TO", "xtsx": ".TO",
    "tsxv": ".V", "tsx venture": ".V", "cve": ".V",
    "fwb": ".DE", "xetra": ".DE", "frankfurt": ".DE", "frankfurt stock exchange": ".DE",
    "tyo": ".T", "tokyo": ".T", "tokyo stock exchange": ".T",
    "sehk": ".HK", "hkex": ".HK", "hong kong": ".HK", "hong kong stock exchange": ".HK",
    "twse": ".TW", "tpe": ".TW", "taiwan": ".TW",
    "sgx": ".SI", "singapore": ".SI", "singapore exchange": ".SI",
    "b3": ".SA", "bovespa": ".SA", "sao paulo": ".SA",
    "bcs": ".SN", "santiago": ".SN", "bolsa de santiago": ".SN",
    "bmv": ".MX", "mexico": ".MX", "bolsa mexicana de valores": ".MX",
    "bvc": ".CL", "bolsa de valores de colombia": ".CL",
    "borsa italiana": ".MI", "bit": ".MI", "milan": ".MI",
    "bolsa de madrid": ".MC", "bmad": ".MC", "madrid": ".MC",
    "nasdaq helsinki": ".HE", "helsinki": ".HE",
    "nasdaq stockholm": ".ST", "stockholm": ".ST",
    "euronext paris": ".PA", "paris": ".PA",
    "euronext amsterdam": ".AS", "amsterdam": ".AS",
    "euronext brussels": ".BR", "brussels": ".BR",
    "euronext lisbon": ".LS", "lisbon": ".LS",
    "euronext dublin": ".IR", "dublin": ".IR",
    "euronext athens": ".AT", "athens": ".AT",
    "warsaw stock exchange": ".WA", "warsaw": ".WA",
    "wiener borse": ".VI", "vienna": ".VI",
    "oslo": ".OL", "oslo bors": ".OL",
}
KNOWN_YAHOO_SUFFIXES = {
    ".TO", ".V", ".NE", ".CN", ".L", ".DE", ".F", ".PA", ".AS", ".MI", ".MC", ".BR",
    ".HE", ".LS", ".IR", ".SW", ".ST", ".CO", ".OL", ".HK", ".T", ".AX", ".NS", ".BO",
    ".SA", ".MX", ".SN", ".KS", ".KQ", ".SI", ".TW", ".TWO", ".WA", ".VI", ".AT",
    ".PR", ".IC", ".SS", ".SZ", ".JO", ".TA", ".CL",
}
BLOCK_TITLES = (
    "just a moment", "access denied", "attention required", "robot check",
    "pardon our interruption", "verify you are human", "radware block",
    "blocked", "captcha",
)
INTERSTITIAL_MARKERS = (
    "cf-browser-verification", "captcha.perfdrive.com", "cdn-cgi/challenge-platform",
    "g-recaptcha", "h-captcha", "please verify you are a human",
    "enable javascript and cookies to continue", "shieldsquare",
)


def constituent_block_reason(status, body, server_header=""):
    """Return a stop-message for a block, captcha, 403, or 406. Else None.

    A long article that merely mentions a captcha widget (Wikipedia's edit
    config does) is not a block. Short interstitials and hard statuses are.
    """
    title = _html_title(body)
    if status in (401, 403, 406, 429):
        extra = f" ({title})" if title else ""
        return (
            f"blocked: HTTP {status}{extra}. fin.py does not bypass captchas or access "
            "controls, and this source was not retried."
        )
    if _looks_like_interstitial(body, title):
        label = f"HTTP {status} " if status and status != 200 else ""
        return (
            f"blocked: {label}captcha or browser-check page{(': ' + title) if title else ''}. "
            "fin.py does not bypass captchas or access controls, and this source was not retried."
        )
    return None


def _html_title(body):
    m = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip()[:140]


def _looks_like_interstitial(body, title=""):
    if not body:
        return False
    title = (title or _html_title(body)).lower()
    if any(t in title for t in BLOCK_TITLES):
        return True
    # Wikipedia articles are large and include an hCaptcha site key in mw.config.
    if len(body) > 80000:
        return False
    low = body.lower()
    return any(m in low for m in INTERSTITIAL_MARKERS)


def scrape_pace():
    """At most one scraped HTML or holdings-file request per second. No retry loop."""
    interval = SCRAPE_MIN_INTERVAL
    if interval <= 0:
        return
    with _scrape_lock:
        wait = interval - (time.time() - _last_scrape[0])
        if wait > 0:
            time.sleep(wait)
        _last_scrape[0] = time.time()


def polite_get(url, accept="text/html,application/xhtml+xml,*/*;q=0.8", binary=False):
    """One identifiable GET. A block, 403, or 406 raises Skip and is not retried."""
    scrape_pace()
    try:
        r = requests.get(url, headers={
            "User-Agent": FIN_UA,
            "Accept": accept,
            "Accept-Language": "en",
        }, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise Skip(f"unreachable: {type(e).__name__}: {str(e)[:160]}")
    body = ""
    raw_head = r.content[:400].lstrip().lower()
    htmlish = raw_head.startswith(b"<!doctype") or raw_head.startswith(b"<html") or raw_head.startswith(b"<head")
    ctype = (r.headers.get("content-type") or "").lower()
    if (not binary) or htmlish or r.status_code in (401, 403, 406, 429):
        if "html" in ctype or "text" in ctype or "json" in ctype or "csv" in ctype or htmlish or r.status_code in (401, 403, 406, 429):
            try:
                body = r.text or ""
            except Exception:
                body = r.content[:20000].decode("utf-8", "replace")
    reason = constituent_block_reason(r.status_code, body, r.headers.get("server", ""))
    if reason:
        raise Skip(reason + f" URL {url}")
    if r.status_code != 200:
        raise Skip(f"HTTP {r.status_code} for {url}")
    if binary and htmlish:
        raise Empty(f"expected a data file and received HTML from {url} (not parsed, not retried)")
    return r


def registry_path(explicit=None):
    if explicit:
        return Path(explicit)
    env = os.environ.get("FIN_INDEX_REGISTRY", "").strip()
    if env:
        return Path(env)
    return DEFAULT_INDEX_REGISTRY


def load_index_registry(path):
    path = Path(path)
    if not path.is_file():
        hint = f" An example fixture is {EXAMPLE_INDEX_REGISTRY}." if EXAMPLE_INDEX_REGISTRY.is_file() else ""
        raise Skip(
            f"index registry not found at {path}. Pass --registry PATH or set FIN_INDEX_REGISTRY "
            f"(default {DEFAULT_INDEX_REGISTRY}).{hint}"
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise Skip(f"could not read index registry {path}: {type(e).__name__}: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("indices"), list):
        raise Skip(f"index registry {path} has no indices list")
    return data


def _compact(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def resolve_index_entry(registry, token):
    token = (token or "").strip()
    if not token:
        raise Skip("missing index id")
    key = _compact(token)
    alias = INDEX_ALIASES.get(key)
    indices = registry.get("indices") or []
    by_id = {idx.get("id"): idx for idx in indices if idx.get("id")}
    if token in by_id:
        idx = by_id[token]
    elif alias and alias in by_id:
        idx = by_id[alias]
    else:
        idx = None
        for item in indices:
            if key and key in (_compact(item.get("id")), _compact(item.get("name"))):
                idx = item
                break
    if not idx:
        raise Skip(f"unknown index {token!r}. Use a registry id such as sp-500, sp-tsx-60, or nasdaq-100.")
    if idx.get("enabled") is False:
        raise Skip(f"index {idx.get('id')} is disabled in the registry")
    return idx


def cell_str(v):
    if v is None:
        return ""
    try:
        import pandas as pd
        if pd.isna(v):
            return ""
    except Exception:
        pass
    s = str(v).replace("\xa0", " ").replace("\u200b", "").strip()
    s = re.sub(r"\[\d+\]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_weight(v):
    s = cell_str(v).replace("%", "").replace(",", "").strip()
    if not s or s in ("-", "—", "n/a", "na"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def split_known_suffix(sym):
    base, suf = split_suffix((sym or "").upper())
    if suf in KNOWN_YAHOO_SUFFIXES:
        return base, suf
    return (sym or "").upper(), ""


def apply_yahoo_suffix(local, suffix):
    """Turn a local code into a Yahoo ticker. None when the form is ambiguous."""
    local = cell_str(local).upper()
    if not local or " " in local or "/" in local:
        return None
    base, existing = split_known_suffix(local)
    base = re.sub(r"[^A-Z0-9\-]", "", base.replace(".", "-"))
    if not base:
        return None
    suf = existing or (suffix or "")
    if suf and not str(suf).startswith("."):
        suf = "." + str(suf)
    if suf and suf not in KNOWN_YAHOO_SUFFIXES:
        return None
    # Numeric local codes are real on HK/TW/JP/KR/CN. An all-digit fund identifier
    # on a letter-ticker market (XIU's "2299955D") is not a Yahoo symbol.
    numeric_markets = {".HK", ".TW", ".TWO", ".T", ".KS", ".KQ", ".SS", ".SZ", ".SI"}
    if suf in numeric_markets:
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{0,8}", base):
            return None
    elif not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}(?:-[A-Z0-9]{1,4})?", base):
        return None
    return base + (suf or "")


def exchange_suffix(label):
    if not label:
        return None
    key = re.sub(r"\s+", " ", label.replace("\xa0", " ")).strip().lower()
    key = key.replace("ö", "o").replace("é", "e")
    if key in YAHOO_EXCHANGE_SUFFIX:
        return YAHOO_EXCHANGE_SUFFIX[key]
    return None


def listing_parts(raw):
    """Split 'Nasdaq: GOOG; Nasdaq: GOOGL' into (exchange, code) pairs."""
    text = cell_str(raw)
    if not text:
        return []
    out = []
    for part in re.split(r"\s*;\s*", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9 .'\-]{0,40}?):\s*(.+)$", part)
        if m:
            prefix, code = m.group(1).strip(), m.group(2).strip()
            # Split only when the left side is an exchange label, not a ticker that happens to contain ":".
            if exchange_suffix(prefix) is not None or re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,30}", prefix):
                out.append((prefix, code))
                continue
        out.append((None, part))
    return out


def map_to_yahoo(raw, suffix_mode, listing=""):
    """Return (yahoo_symbols, reason). yahoo_symbols is a list; empty means unmapped.

    suffix_mode is a concrete suffix ('', '.TO'), 'per-row', or 'none'.
    A space inside a local code is left unmapped rather than guessed.
    """
    text = cell_str(raw)
    if not text or text in ("-", "—", "NAN", "NONE"):
        return [], "empty symbol"
    parts = listing_parts(text)
    # A bare ticker that happens to contain a colon-like exchange prefix was split.
    # A class-share code such as BRK.B has no colon and stays one part.
    if len(parts) == 1 and parts[0][0] is None:
        parts = [(None, text)]
    found = []
    reasons = []
    for prefix, code in parts:
        code = cell_str(code)
        if not code:
            reasons.append("empty symbol")
            continue
        if prefix:
            suf = exchange_suffix(prefix)
            if suf is None:
                reasons.append(f"unmapped exchange {prefix}")
                continue
            if suf == ".HK" and re.fullmatch(r"\d{1,5}", code.replace(" ", "")):
                code = code.replace(" ", "").zfill(4)
            yahoo = apply_yahoo_suffix(code, suf)
            if not yahoo:
                reasons.append(f"ambiguous symbol {code} on {prefix}")
                continue
            found.append(yahoo)
            continue
        mode = suffix_mode if suffix_mode is not None else ""
        if mode == "none":
            reasons.append("registry yahoo_suffix is none")
            continue
        if mode == "per-row":
            base, existing = split_known_suffix(code.upper())
            if existing:
                yahoo = apply_yahoo_suffix(code, "")
            else:
                listed = exchange_suffix(listing) if listing else None
                if listed is None and listing:
                    reasons.append(f"unmapped listing {listing}")
                    continue
                if listed is None and not existing:
                    reasons.append("no Yahoo suffix on the row (listing/exchange not mapped)")
                    continue
                local = code
                if listed == ".HK" and re.fullmatch(r"\d{1,5}", local.replace(" ", "")):
                    local = local.replace(" ", "").zfill(4)
                yahoo = apply_yahoo_suffix(local, listed or "")
            if not yahoo:
                reasons.append(f"ambiguous symbol {code}")
                continue
            found.append(yahoo)
            continue
        # Concrete suffix from the registry, including '' for US listings.
        local = code
        if mode == ".HK" and re.fullmatch(r"\d{1,5}", local.replace(" ", "")):
            local = local.replace(" ", "").zfill(4)
        yahoo = apply_yahoo_suffix(local, mode)
        if not yahoo:
            reasons.append(f"ambiguous symbol {code}")
            continue
        found.append(yahoo)
    # de-dupe, keep order
    uniq = []
    for y in found:
        if y not in uniq:
            uniq.append(y)
    return uniq, ("; ".join(reasons) if reasons and not uniq else "")


def flatten_columns(df):
    cols = []
    for c in df.columns:
        if isinstance(c, tuple):
            parts = [cell_str(p) for p in c if cell_str(p) and not str(p).startswith("Unnamed")]
            c = parts[-1] if parts else ""
        c = re.sub(r"\[[^\]]*\]", "", cell_str(c))
        cols.append(re.sub(r"\s+", " ", c).strip())
    out = df.copy()
    out.columns = cols
    return out


def find_col(df, name):
    if not name:
        return None
    want = re.sub(r"\s+", " ", str(name)).strip().lower()
    for c in df.columns:
        if str(c).strip().lower() == want:
            return c
    for c in df.columns:
        if want and want in str(c).strip().lower():
            return c
    return None


def _weight_col(df):
    for cand in ("Weight", "Weight (%)", "Portfolio Weight", "Index Weight", "% Weight", "Weighting"):
        hit = find_col(df, cand)
        if hit:
            return hit
    return None


def rows_from_table(df, spec, suffix_mode):
    """Parse one table. None if it does not have the requested columns."""
    df = flatten_columns(df)
    match = spec.get("table_match")
    if match and match.lower() not in " ".join(str(c) for c in df.columns).lower():
        return None
    sym_col = find_col(df, spec.get("symbol_col") or "Symbol")
    name_col = find_col(df, spec.get("name_col") or "Company") or find_col(df, "Name") or find_col(df, "Security")
    if not sym_col or not name_col:
        return None
    weight_col = _weight_col(df)
    listing_col = find_col(df, "Main listing")
    mapped, unmapped = [], []
    for _, rec in df.iterrows():
        raw = cell_str(rec.get(sym_col))
        name = cell_str(rec.get(name_col))
        if not raw and not name:
            continue
        if raw.lower() in ("symbol", "ticker", "ticker symbol"):
            continue
        if not raw:
            unmapped.append({"symbol": None, "name": name or None, "reason": "empty symbol"})
            continue
        listing = cell_str(rec.get(listing_col)) if listing_col else ""
        weight = parse_weight(rec.get(weight_col)) if weight_col else None
        yahoos, reason = map_to_yahoo(raw, suffix_mode, listing)
        if not yahoos:
            unmapped.append({"symbol": raw, "name": name or None, "reason": reason or "unmapped"})
            continue
        # Source symbol is the local code (after an exchange prefix), shared by dual listings.
        local = raw
        parts = listing_parts(raw)
        if len(parts) == 1 and parts[0][0]:
            local = parts[0][1]
        elif len(parts) > 1:
            local = raw
        for yahoo in yahoos:
            symbol = local
            if len(yahoos) > 1:
                symbol = yahoo
                for prefix, code in parts:
                    suf = exchange_suffix(prefix) if prefix else None
                    local_code = cell_str(code)
                    if suf == ".HK" and re.fullmatch(r"\d{1,5}", local_code.replace(" ", "")):
                        local_code = local_code.replace(" ", "").zfill(4)
                    if apply_yahoo_suffix(local_code, suf or "") == yahoo:
                        symbol = cell_str(code)
                        break
            row = {"symbol": symbol, "yahoo_symbol": yahoo, "name": name or None}
            if weight is not None:
                row["weight"] = weight
            mapped.append(row)
    if not mapped and not unmapped:
        return None
    return mapped, unmapped


def parse_html_tables(html, spec, suffix_mode):
    import pandas as pd
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError as e:
        if "No tables" in str(e) or "No table" in str(e):
            raise Empty("page has no HTML tables")
        raise Empty(f"could not read HTML tables: {str(e)[:160]}")
    except ImportError as e:
        raise Skip(f"pandas/lxml missing ({e}); pip install --user yfinance")
    best = None
    for table in tables:
        parsed = rows_from_table(table, spec, suffix_mode)
        if not parsed:
            continue
        score = len(parsed[0]) + len(parsed[1])
        if best is None or score > best[0]:
            best = (score, parsed)
    if not best:
        raise Empty(
            f"no table with columns {spec.get('symbol_col')!r} and {spec.get('name_col')!r}"
            + (f" matching {spec.get('table_match')!r}" if spec.get("table_match") else "")
        )
    return best[1]


def wikipedia_asof(html):
    m = re.search(
        r"This page was last edited on\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})(?:,\s+at\s+(\d{1,2}:\d{2}))?",
        html or "",
    )
    if not m:
        return None
    try:
        day = dt.datetime.strptime(m.group(1), "%d %B %Y").date()
    except ValueError:
        return None
    if m.group(2):
        hh, mm = m.group(2).split(":")
        return dt.datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=dt.timezone.utc).isoformat()
    return day.isoformat()


def parse_ishares_csv(text, spec, suffix_mode):
    text = (text or "").lstrip("\ufeff")
    asof = None
    m = re.search(r"as of[,\"\s]*([A-Za-z]+\s+\d{1,2},\s+\d{4})", text, re.I)
    if m:
        try:
            asof = dt.datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
        except ValueError:
            asof = None
    lines = text.splitlines()
    header_i = None
    for i, line in enumerate(lines):
        if line.lower().startswith("ticker,"):
            header_i = i
            break
    if header_i is None:
        raise Empty("iShares CSV has no Ticker header (layout changed)")
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_i:])))
    mapped, unmapped = [], []
    for rec in reader:
        if not rec:
            continue
        raw = cell_str(rec.get("Ticker") or rec.get("Symbol"))
        name = cell_str(rec.get("Name") or rec.get("Issuer Name") or "")
        asset = cell_str(rec.get("Asset Class") or "")
        if asset and asset.lower() not in ("equity", "equities"):
            continue
        if not raw or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,14}", raw):
            continue
        weight = parse_weight(rec.get("Weight (%)") or rec.get("Weight"))
        yahoos, reason = map_to_yahoo(raw, suffix_mode, "")
        if not yahoos:
            unmapped.append({"symbol": raw, "name": name or None, "reason": reason or "unmapped"})
            continue
        row = {"symbol": raw, "yahoo_symbol": yahoos[0], "name": name or None}
        if weight is not None:
            row["weight"] = weight
        mapped.append(row)
    if not mapped:
        raise Empty("iShares CSV had no equity rows")
    return (mapped, unmapped), asof


def parse_spdr_xlsx(content, spec, suffix_mode):
    try:
        import openpyxl
    except ImportError:
        raise Skip("openpyxl is not installed; SPDR XLSX needs it (pip install openpyxl). This source was skipped.")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise Empty(f"could not read SPDR workbook: {type(e).__name__}: {str(e)[:160]}")
    mapped, unmapped = [], []
    asof = None
    try:
        ws = wb.worksheets[0]
        header = None
        for row in ws.iter_rows(values_only=True):
            vals = [cell_str(c) for c in row]
            if asof is None:
                for v in vals:
                    m = re.search(r"As of\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", v, re.I)
                    if m:
                        try:
                            asof = dt.datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
                        except ValueError:
                            pass
            low = [v.lower() for v in vals]
            if header is None:
                if "ticker" in low and "name" in low:
                    header = low
                continue
            rec = {header[j]: vals[j] if j < len(vals) else "" for j in range(len(header))}
            raw = rec.get("ticker", "")
            name = rec.get("name", "")
            if name and len(name) > 180:
                continue
            if not raw or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,12}", raw.upper()):
                continue
            weight = parse_weight(rec.get("weight"))
            yahoos, reason = map_to_yahoo(raw, suffix_mode, "")
            if not yahoos:
                unmapped.append({"symbol": raw, "name": name or None, "reason": reason or "unmapped"})
                continue
            row_out = {"symbol": raw, "yahoo_symbol": yahoos[0], "name": name or None}
            if weight is not None:
                row_out["weight"] = weight
            mapped.append(row_out)
    finally:
        wb.close()
    if not mapped:
        raise Empty("SPDR workbook had no ticker rows (layout changed)")
    return (mapped, unmapped), asof


def parse_user_holdings(path):
    """CSV, JSON, or one ticker per line. Symbols are taken as given (Yahoo-style)."""
    path = Path(path)
    if not path.is_file():
        raise Skip(f"constituent file not found: {path}")
    text = path.read_text(errors="replace")
    mapped, unmapped = [], []

    def add(raw, name=None, weight=None, yahoo=None):
        raw = cell_str(raw)
        if not raw or raw.startswith("#"):
            return
        yahoo_sym = cell_str(yahoo) if yahoo else ""
        if yahoo_sym:
            mode = "per-row" if split_known_suffix(yahoo_sym.upper())[1] or ":" in yahoo_sym else ""
            yahoos, reason = map_to_yahoo(yahoo_sym, mode)
            if not yahoos:
                yahoos, reason = map_to_yahoo(yahoo_sym, "")
        else:
            mode = "per-row" if split_known_suffix(raw.upper())[1] or ":" in raw else ""
            yahoos, reason = map_to_yahoo(raw, mode)
            if not yahoos and mode == "per-row":
                yahoos, reason = map_to_yahoo(raw, "")
        if not yahoos:
            unmapped.append({"symbol": raw, "name": name, "reason": reason or "unmapped"})
            return
        for y in yahoos:
            row = {"symbol": raw, "yahoo_symbol": y, "name": name}
            if weight is not None:
                row["weight"] = weight
            mapped.append(row)

    if not text.strip():
        raise Empty(f"no tickers in {path}")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise Empty(f"constituent JSON could not be parsed: {e}")
        rows = data.get("constituents") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise Empty("constituent JSON must be a list, or an object with constituents")
        for item in rows:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("symbol") or item.get("ticker") or item.get("yahoo_symbol"),
                    item.get("name") or item.get("company"), parse_weight(item.get("weight")),
                    item.get("yahoo_symbol"))
            else:
                continue
    elif "," in text.splitlines()[0] and re.search(r"symbol|ticker", text.splitlines()[0], re.I):
        reader = csv.DictReader(io.StringIO(text))
        for rec in reader:
            keys = {k.lower().strip(): v for k, v in rec.items() if k}
            add(keys.get("symbol") or keys.get("ticker") or keys.get("yahoo_symbol"),
                keys.get("name") or keys.get("company") or keys.get("security"),
                parse_weight(keys.get("weight") or keys.get("weight (%)")),
                keys.get("yahoo_symbol"))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            bits = re.split(r"[\t,]", line, maxsplit=1)
            add(bits[0], bits[1].strip() if len(bits) > 1 else None)
    if not mapped:
        raise Empty(f"no tickers in {path}")
    asof = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).date().isoformat()
    return (mapped, unmapped), asof


def _source_kind(spec):
    t = (spec or {}).get("type") or "none"
    return {
        "wikipedia_table": "wikipedia",
        "slickcharts": "slickcharts",
        "spdr_xlsx": "spdr",
        "ishares_csv": "ishares",
        "file": "file",
        "composite": "composite",
        "none": "none",
    }.get(t, t)


def parse_source_arg(source):
    """Return (kind set or None, file path or None) from --source."""
    if not source:
        return None, None
    kinds, file_path = set(), None
    for part in str(source).split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower().startswith("file:"):
            kinds.add("file")
            file_path = part.split(":", 1)[1]
        else:
            kinds.add(part.lower())
    return kinds, file_path


def _as_source_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def cache_ttl_days(index_id):
    return CONSTITUENTS_SHORT_TTL if index_id in CONSTITUENTS_SHORT_TTL_IDS else CONSTITUENTS_TTL_DAYS


def constituent_cache_path(index_id):
    return CACHE / "constituents" / f"{index_id}.json"


def read_constituent_cache(index_id):
    path = constituent_cache_path(index_id)
    if not path.is_file():
        return None
    try:
        env = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cached_at = env.get("cached_at")
    try:
        stamp = dt.datetime.fromisoformat(cached_at)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - stamp
    env["_age_days"] = age.total_seconds() / 86400
    env["_ttl_days"] = env.get("ttl_days") or cache_ttl_days(index_id)
    env["_stale"] = age.total_seconds() > float(env["_ttl_days"]) * 86400
    return env


def write_constituent_cache(index_id, source, source_url, asof, data):
    path = constituent_cache_path(index_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        env = {
            "index_id": index_id,
            "cached_at": now_utc(),
            "ttl_days": cache_ttl_days(index_id),
            "source": source,
            "source_url": source_url,
            "asof": asof,
            "data": data,
        }
        path.write_text(json.dumps(env))
    except OSError:
        pass


def _payload(index, mapped, unmapped, asof, source_url, fetched_at=None):
    notes = []
    data = {
        "index_id": index.get("id"),
        "name": index.get("name"),
        "count": len(mapped),
        "unmapped_count": len(unmapped),
        "asof": asof,
        "fetched_at_utc": fetched_at or now_utc(),
        "constituents": mapped,
        "unmapped": unmapped,
    }
    return data, notes


def _finish_rows(index, parsed, asof, url, source_name):
    mapped, unmapped = parsed
    # Drop exact yahoo duplicates, keep the first.
    seen, uniq = set(), []
    for row in mapped:
        key = row.get("yahoo_symbol")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)
    if not uniq:
        sample = ""
        if unmapped:
            sample = f"; first: {unmapped[0].get('symbol')} ({unmapped[0].get('reason')})"
        raise Empty(f"no Yahoo tickers mapped ({len(unmapped)} unmapped){sample}")
    data, extra_notes = _payload(index, uniq, unmapped, asof, url)
    if index.get("large"):
        extra_notes.append(f"large index ({index.get('name')}, about {index.get('approx_size')} names); consider --top N")
    if source_name in ("wikipedia_table", "slickcharts", "spdr_xlsx", "ishares_csv"):
        extra_notes.append(
            "Constituent lists from Wikipedia, Slickcharts, and ETF holdings can lag the official index "
            "and can include extra names. Confirm membership with the index provider."
        )
    if source_name in ("spdr_xlsx", "ishares_csv"):
        extra_notes.append("ETF holdings are not the official index membership; cash and non-members are dropped when the file labels them.")
    if unmapped:
        extra_notes.append(f"{len(unmapped)} row(s) had no Yahoo ticker and were left unmapped (not guessed).")
    data["_notes"] = extra_notes
    return data, url


def fetch_spec(index, spec, stack):
    """Fetch one registry source. Returns (data, url). Raises Skip or Empty."""
    kind = (spec or {}).get("type") or "none"
    suffix = index.get("yahoo_suffix")
    if suffix is None:
        suffix = ""
    if kind == "none":
        raise Skip(spec.get("note") or "no constituents source configured")
    if kind == "composite":
        return fetch_composite(index, spec, stack)
    if kind == "wikipedia_table":
        url = spec.get("url")
        if not url:
            raise Skip("wikipedia source has no url")
        r = polite_get(url)
        parsed = parse_html_tables(r.text, spec, suffix)
        asof = wikipedia_asof(r.text) or dt.datetime.now(dt.timezone.utc).date().isoformat()
        return _finish_rows(index, parsed, asof, url, kind)
    if kind == "slickcharts":
        url = spec.get("url")
        if not url:
            raise Skip("slickcharts source has no url")
        r = polite_get(url)
        parsed = parse_html_tables(r.text, spec, suffix)
        asof = dt.datetime.now(dt.timezone.utc).date().isoformat()
        return _finish_rows(index, parsed, asof, url, kind)
    if kind == "ishares_csv":
        url = spec.get("url")
        if not url:
            raise Skip("ishares source has no url")
        r = polite_get(url, accept="text/csv,text/plain,*/*;q=0.8")
        if "html" in (r.headers.get("content-type") or "").lower() and "ticker," not in (r.text or "")[:2000].lower():
            raise Empty(f"iShares URL returned HTML rather than a CSV ({url}). Not worked around.")
        parsed, asof = parse_ishares_csv(r.text, spec, suffix)
        asof = asof or dt.datetime.now(dt.timezone.utc).date().isoformat()
        return _finish_rows(index, parsed, asof, url, kind)
    if kind == "spdr_xlsx":
        url = spec.get("url")
        if not url:
            raise Skip("spdr source has no url")
        r = polite_get(url, accept="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8", binary=True)
        parsed, asof = parse_spdr_xlsx(r.content, spec, suffix)
        asof = asof or dt.datetime.now(dt.timezone.utc).date().isoformat()
        return _finish_rows(index, parsed, asof, url, kind)
    if kind == "file":
        path = spec.get("path") or spec.get("url")
        if not path:
            raise Skip("pass --file PATH or --source file:PATH")
        parsed, asof = parse_user_holdings(path)
        return _finish_rows(index, parsed, asof, f"file://{Path(path).resolve()}", kind)
    raise Skip(f"unsupported constituents source type {kind}")


def fetch_composite(index, spec, stack):
    ids = spec.get("of") or []
    if not ids:
        raise Skip("composite source has no member indices")
    if index.get("id") in stack:
        raise Skip(f"composite cycle at {index.get('id')}")
    registry = spec.get("_registry")
    if registry is None:
        raise Skip("composite source is missing the registry")
    merged = []
    unmapped = []
    attempts = []
    asofs = []
    urls = []
    seen = set()
    for child_id in ids:
        try:
            child = resolve_index_entry(registry, child_id)
        except Skip as e:
            attempts.append({"source": f"composite:{child_id}", "status": "skipped", "message": str(e)})
            raise Skip(f"composite member {child_id} failed: {e}")
        sub = Runner("constituents", child.get("id"))
        run_constituent_chain(child, registry, selected=None, file_path=None, refresh=True, runner=sub, stack=stack | {index.get("id")})
        attempts.extend({"source": f"{child.get('id')}:{a.get('source')}", **{k: v for k, v in a.items() if k != "source"}} for a in sub.out["attempts"])
        if not sub.out["ok"]:
            raise Empty(f"composite member {child.get('id')} returned no constituents")
        urls.append(sub.out.get("source_url"))
        if sub.out["data"].get("asof"):
            asofs.append(sub.out["data"]["asof"])
        for row in sub.out["data"].get("constituents") or []:
            if row.get("yahoo_symbol") in seen:
                continue
            seen.add(row.get("yahoo_symbol"))
            merged.append(row)
        unmapped.extend(sub.out["data"].get("unmapped") or [])
    asof = min(asofs) if asofs else None
    data, url = _finish_rows(index, (merged, unmapped), asof, " + ".join(u for u in urls if u), "composite")
    data["_child_attempts"] = attempts
    return data, url


def run_constituent_chain(index, registry, selected, file_path, refresh, runner, stack=None):
    """Try cache, then constituents_source, fallback_source, then a user file."""
    stack = stack or set()
    index_id = index.get("id")
    stash = {}
    # A --source filter or a user file must not read or replace the default cache.
    use_cache = selected is None and not file_path

    def cache_fn():
        if not use_cache:
            raise Skip("cache is only used for the default source order")
        if refresh:
            raise Skip("--refresh")
        env = read_constituent_cache(index_id)
        if not env:
            raise Skip("no cache")
        if env.get("_stale"):
            raise Skip(f"stale cache ({env.get('_age_days', 0):.1f}d > ttl {env.get('_ttl_days')}d)")
        stash["source"] = env.get("source")
        stash["asof"] = env.get("asof")
        data = dict(env.get("data") or {})
        data["cached"] = True
        data["cached_at"] = env.get("cached_at")
        return data, env.get("source_url")

    chain = [("cache", cache_fn)]
    specs = []
    primary = index.get("constituents_source") or {"type": "none"}
    specs.append(primary)
    specs.extend(_as_source_list(index.get("fallback_source")))
    if file_path:
        specs.append({"type": "file", "path": file_path})
    for spec in specs:
        spec = dict(spec)
        spec["_registry"] = registry
        kind = _source_kind(spec)
        label = spec.get("type") or "none"
        if selected is not None and kind not in selected:
            runner.out["attempts"].append({"source": label, "status": "skipped", "message": "not selected"})
            continue

        def fn(spec=spec, label=label):
            data, url = fetch_spec(index, spec, stack)
            notes = data.pop("_notes", [])
            children = data.pop("_child_attempts", None)
            if children:
                runner.out["attempts"].extend(children)
            runner.out["notes"].extend(notes)
            stash["asof"] = data.get("asof")
            return data, url

        chain.append((label, fn))
    ok = runner.try_sources(chain)
    if ok and runner.out["source"] == "cache":
        runner.out["source"] = stash.get("source") or "cache"
        runner.out["asof"] = (runner.out["data"] or {}).get("asof") or stash.get("asof")
        runner.out["notes"].append("Served from the constituents cache. Pass --refresh to fetch again.")
    elif ok and use_cache:
        runner.out["asof"] = (runner.out["data"] or {}).get("asof")
        write_constituent_cache(index_id, runner.out["source"], runner.out["source_url"], runner.out["asof"], runner.out["data"])
    elif ok:
        runner.out["asof"] = (runner.out["data"] or {}).get("asof")
    return ok


def rank_constituents(rows, top, notes):
    if not top or top <= 0 or len(rows) <= top:
        return rows
    weighted = [r for r in rows if isinstance(r.get("weight"), (int, float))]
    if weighted and len(weighted) == len(rows):
        notes.append(f"--top {top} ranked by weight")
        return sorted(rows, key=lambda r: r["weight"], reverse=True)[:top]
    notes.append(f"--top {top} ranked by Yahoo market cap (the list has no weights)")
    caps = {}

    def one(sym):
        try:
            cap = getattr(yf_ticker(sym).fast_info, "market_cap", None)
            return sym, float(cap) if cap else 0.0
        except Exception:
            return sym, 0.0

    syms = [r.get("yahoo_symbol") for r in rows if r.get("yahoo_symbol")]
    with ThreadPoolExecutor(max_workers=VERIFY_WORKERS_CAP) as pool:
        for sym, cap in pool.map(one, syms):
            caps[sym] = cap
    return sorted(rows, key=lambda r: caps.get(r.get("yahoo_symbol")) or 0.0, reverse=True)[:top]


def cmd_constituents(a, R):
    selected, source_file = parse_source_arg(getattr(a, "source", None))
    file_path = getattr(a, "file", None) or source_file
    try:
        registry = load_index_registry(registry_path(getattr(a, "registry", None)))
        index = resolve_index_entry(registry, a.symbol)
    except Skip as e:
        R.out["attempts"].append({"source": "registry", "status": "skipped", "message": str(e)})
        return
    R.out["symbol"] = index.get("id")
    R.out["notes"].append(f"registry {registry_path(getattr(a, 'registry', None))}")
    if _compact(a.symbol) != _compact(index.get("id")) and a.symbol != index.get("id"):
        R.out["notes"].append(f"resolved {a.symbol!r} to {index.get('id')}")
    run_constituent_chain(index, registry, selected, file_path, bool(getattr(a, "refresh", False)), R)
    if R.out["ok"] and R.out.get("source") in ("wikipedia_table", "slickcharts", "spdr_xlsx", "ishares_csv"):
        lag = ("Constituent lists from Wikipedia, Slickcharts, and ETF holdings can lag the official index "
               "and can include extra names. Confirm membership with the index provider.")
        if lag not in R.out["notes"]:
            R.out["notes"].append(lag)
    if R.out["ok"] and index.get("large") and not any(str(n).startswith("large index") for n in R.out["notes"]):
        R.out["notes"].append(f"large index ({index.get('name')}, about {index.get('approx_size')} names); consider --top N")
    if R.out["ok"] and getattr(a, "top", None):
        rows = rank_constituents(R.out["data"].get("constituents") or [], a.top, R.out["notes"])
        R.out["data"]["constituents"] = rows
        R.out["data"]["count"] = len(rows)
        R.out["data"]["top"] = a.top


# ---------------------------------------------------- earnings calendar
def parse_ymd(value, flag):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise Skip(f"{flag} must be YYYY-MM-DD, got {value!r}")


def symkey(symbol):
    return re.sub(r"[^A-Z0-9\-]", "", (symbol or "").upper().replace(".", "-"))


def norm_timing(value, when=None):
    """Map provider timing to BMO, AMC, intraday, or unknown."""
    text = cell_str(value).lower().replace("_", " ")
    if text in ("bmo", "before market open", "pre-market", "premarket", "before open"):
        return "BMO"
    if text in ("amc", "after market close", "after-hours", "after hours", "afterhours", "after close"):
        return "AMC"
    if text in ("dmh", "intraday", "during market", "during market hours"):
        return "intraday"
    if text in ("n/a", "na", "none", "unknown", "--", "time not supplied", "tns"):
        return "unknown"
    if when is not None:
        hour, minute = when.hour, when.minute
        if hour == 0 and minute == 0:
            return "unknown"
        if hour < 12:
            return "BMO"
        if hour >= 16:
            return "AMC"
        return "intraday"
    return "unknown"


def _as_et(value):
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(ET)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=ET)
    text = str(value).strip()
    if not text or text.lower() == "nat":
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = dt.datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=ET)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(ET)


def _num(value):
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except Exception:
        pass
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(n) or math.isinf(n):
        return None
    return n


def yahoo_calendar_status():
    try:
        import yfinance
    except ImportError:
        return {"lib": "MISSING: pip install --user yfinance", "calendars": False}
    raw = str(getattr(yfinance, "__version__", "0"))
    parts = []
    for p in raw.split(".")[:3]:
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 2:
        parts.append(0)
    ok = tuple(parts[:2]) >= (1, 8)
    has = False
    try:
        from yfinance.calendars import Calendars
        has = hasattr(Calendars, "get_earnings_calendar")
    except Exception as e:
        return {"lib": f"yfinance {raw}", "calendars": False, "detail": str(e)[:160]}
    return {
        "lib": f"yfinance {raw}",
        "calendars": bool(ok and has),
        "needs": "yfinance>=1.8" if not ok else None,
        "regions": "us via Calendars.get_earnings_calendar; other regions via Calendars._get_data",
    }


def _frame_records(df):
    if df is None or getattr(df, "empty", True):
        return []
    work = df.reset_index()
    work.columns = [str(c) for c in work.columns]
    return work.to_dict(orient="records")


def rows_from_yahoo_frame(df, source):
    rows = []
    for rec in _frame_records(df):
        symbol = cell_str(rec.get("Symbol") or rec.get("index"))
        if not symbol or symbol.lower() == "symbol":
            continue
        when = _as_et(rec.get("Event Start Date"))
        if when is None:
            continue
        timing = norm_timing(rec.get("Timing"), when)
        rows.append({
            "date": when.date().isoformat(),
            "time_et": when.strftime("%H:%M"),
            "timing": timing,
            "ticker": symbol,
            "yahoo_symbol": symbol,
            "symbol": symbol,
            "company": cell_str(rec.get("Company")) or None,
            "eps_estimate": _num(rec.get("EPS Estimate")),
            "marketcap": _num(rec.get("Marketcap")),
            "source": source,
            "projected": True,
        })
    return rows


def yahoo_earnings_calendar(region, start, end):
    """Page the Yahoo earnings screener. US uses the public method; other regions use _get_data."""
    status = yahoo_calendar_status()
    if not status.get("calendars"):
        raise Skip(f"yfinance Calendars unavailable ({status.get('lib')}). Need yfinance>=1.8.")
    import yfinance as yf
    from yfinance.calendars import CalendarQuery
    # Yahoo treats the end bound as midnight at the start of that date, so a
    # same-day window drops every print that afternoon. Ask through the next
    # date, then keep rows whose New York date is inside the caller's range.
    query_end = end + dt.timedelta(days=1)
    cal = yf.Calendars(start=start.isoformat(), end=query_end.isoformat())
    region = region.lower()
    rows, pages = [], 0
    offset = 0
    while pages < 40:
        try:
            if region == "us":
                df = cal.get_earnings_calendar(
                    start=start.isoformat(), end=query_end.isoformat(),
                    filter_most_active=False, limit=100, offset=offset, force=True,
                )
            else:
                query = CalendarQuery("and", [
                    CalendarQuery("eq", ["region", region]),
                    CalendarQuery("or", [
                        CalendarQuery("eq", ["eventtype", "EAD"]),
                        CalendarQuery("eq", ["eventtype", "ERA"]),
                    ]),
                    CalendarQuery("gte", ["startdatetime", start.isoformat()]),
                    CalendarQuery("lte", ["startdatetime", query_end.isoformat()]),
                ])
                df = cal._get_data("sp_earnings", query, limit=100, offset=offset, force=True)
        except Skip:
            raise
        except Exception as e:
            if rows:
                rows = [r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()]
                return rows, f"region {region}: {len(rows)} rows in {pages} page(s); paging stopped: {type(e).__name__}: {str(e)[:120]}"
            raise Skip(f"yfinance Calendars region {region} failed: {type(e).__name__}: {str(e)[:180]}")
        pages += 1
        batch = rows_from_yahoo_frame(df, "yahoo_calendar")
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    rows = [r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()]
    note = f"region {region}: {len(rows)} rows, {pages} page(s)"
    if pages >= 40 and len(rows) >= 4000:
        note += "; stopped at the page cap"
    return rows, note


def finnhub_earnings_calendar(start, end):
    payload, url = fh("calendar/earnings", **{"from": start.isoformat(), "to": end.isoformat()})
    rows_in = payload.get("earningsCalendar") if isinstance(payload, dict) else payload
    if not rows_in:
        raise Empty("Finnhub earnings calendar was empty")
    rows = []
    for rec in rows_in:
        if not isinstance(rec, dict):
            continue
        symbol = cell_str(rec.get("symbol"))
        date = cell_str(rec.get("date"))[:10]
        if not symbol or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        hour = cell_str(rec.get("hour"))
        rows.append({
            "date": date,
            "time_et": None,
            "timing": norm_timing(hour),
            "ticker": symbol,
            "yahoo_symbol": symbol,
            "symbol": symbol,
            "company": None,
            "eps_estimate": _num(rec.get("epsEstimate")),
            "marketcap": None,
            "source": "finnhub",
            "projected": False,
        })
    if not rows:
        raise Empty("Finnhub earnings calendar had no dated rows")
    return rows, url


def ticker_earnings_in_window(yahoo, start, end):
    """One per-ticker Yahoo earnings-date lookup. One retry on a throttle, then stop."""
    def once():
        ed = yf_ticker(yahoo).get_earnings_dates(limit=12)
        if ed is None or getattr(ed, "empty", True):
            return []
        out = []
        work = ed.reset_index()
        work.columns = [str(c) for c in work.columns]
        for rec in work.to_dict(orient="records"):
            when = _as_et(rec.get("Earnings Date") or rec.get("index"))
            if when is None:
                continue
            day = when.date()
            if day < start or day > end:
                continue
            out.append({
                "date": day.isoformat(),
                "time_et": when.strftime("%H:%M"),
                "timing": norm_timing(None, when),
                "ticker": yahoo,
                "yahoo_symbol": yahoo,
                "symbol": yahoo,
                "company": None,
                "eps_estimate": _num(rec.get("EPS Estimate")),
                "marketcap": None,
                "source": "yahoo_earnings_dates",
                "projected": True,
            })
        return out

    try:
        return once(), None
    except Exception as e:
        if not re.search(r"429|too many requests|rate limit|throttle", str(e), re.I):
            return [], f"{type(e).__name__}: {str(e)[:160]}"
        time.sleep(1.0)
        try:
            return once(), None
        except Exception as e2:
            return [], f"throttle retry failed: {type(e2).__name__}: {str(e2)[:160]}"


def _constituent_index(constituents):
    """Map several symbol spellings to the constituent row."""
    index = {}
    for row in constituents:
        for field in ("yahoo_symbol", "symbol", "ticker"):
            raw = row.get(field)
            if not raw:
                continue
            index.setdefault(symkey(raw), row)
    return index


def lookup_constituent(index, symbol):
    if not symbol:
        return None
    return index.get(symkey(symbol))


def assemble_earnings(constituents, event_rows):
    """Intersect events with constituents. Returns rows, counts, missing, unmapped."""
    unmapped = [c for c in constituents if not c.get("yahoo_symbol")]
    mapped = [c for c in constituents if c.get("yahoo_symbol")]
    by_symbol = _constituent_index(mapped)
    kept = {}
    for event in event_rows:
        match = lookup_constituent(by_symbol, event.get("yahoo_symbol") or event.get("ticker") or event.get("symbol"))
        if constituents and not match:
            continue
        yahoo = (match or {}).get("yahoo_symbol") or event.get("yahoo_symbol") or event.get("ticker")
        symbol = (match or {}).get("symbol") or event.get("symbol") or yahoo
        company = event.get("company") or (match or {}).get("name")
        row = {
            "date": event.get("date"),
            "time_et": event.get("time_et"),
            "timing": event.get("timing") or "unknown",
            "ticker": yahoo,
            "symbol": symbol,
            "yahoo_symbol": yahoo,
            "company": company,
            "eps_estimate": event.get("eps_estimate"),
            "marketcap": event.get("marketcap"),
            "source": event.get("source"),
            "asof": event.get("asof"),
            "projected": bool(event.get("projected")),
        }
        key = (row["date"], symkey(yahoo or ""))
        prev = kept.get(key)
        if not prev:
            kept[key] = row
            continue
        if prev.get("eps_estimate") is None and row.get("eps_estimate") is not None:
            prev["eps_estimate"] = row["eps_estimate"]
        if prev.get("timing") in (None, "unknown") and row.get("timing") not in (None, "unknown"):
            prev["timing"] = row["timing"]
            prev["time_et"] = row.get("time_et") or prev.get("time_et")
        if not prev.get("company") and row.get("company"):
            prev["company"] = row["company"]
        if prev.get("source") != row.get("source") and row.get("source"):
            prev["source"] = prev["source"]  # keep the earlier pass (bulk before verify)
    rows = sorted(kept.values(), key=lambda r: (r.get("date") or "", r.get("ticker") or ""))
    found_keys = {symkey(r.get("yahoo_symbol") or "") for r in rows}
    missing = []
    for c in mapped:
        if symkey(c.get("yahoo_symbol")) not in found_keys:
            missing.append({"symbol": c.get("symbol"), "yahoo_symbol": c.get("yahoo_symbol"), "name": c.get("name")})
    counts = {
        "constituents": len(constituents),
        "found": len({symkey(r.get("yahoo_symbol") or "") for r in rows}) if constituents else len(rows),
        "no_date": len(missing) if constituents else 0,
        "unmapped": len(unmapped),
        "in_window": len({symkey(r.get("yahoo_symbol") or "") for r in rows}) if constituents else len(rows),
        "missing": len(missing) if constituents else 0,
        "events": len(rows),
    }
    return rows, counts, missing, unmapped


def regions_for(index, explicit, constituents):
    if explicit:
        return [p.strip().lower() for p in explicit.split(",") if p.strip()]
    if index is not None:
        regs = [str(r).lower() for r in (index.get("calendar_regions") or [])]
        return [r for r in regs if r in ("us", "ca")]
    regs = set()
    for c in constituents:
        suf = split_suffix((c.get("yahoo_symbol") or "").upper())[1]
        if suf in CA_SUFFIXES or suf == ".V":
            regs.add("ca")
        elif suf == "":
            regs.add("us")
    return sorted(regs)


def sparse_index(index, regions):
    """Non-US/CA indices get a per-ticker pass for every mapped name."""
    if index is None:
        return False
    configured = [str(r).lower() for r in (index.get("calendar_regions") or [])]
    if not configured:
        return True
    return any(r not in ("us", "ca") for r in configured)


def write_calendar_table(rows, path, kind):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["date", "ticker", "company", "timing", "eps_estimate", "source", "asof", "yahoo_symbol", "symbol", "time_et", "marketcap", "projected"]
    if kind == "csv":
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow(row)
        return
    def esc(v):
        return ("" if v is None else str(v)).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(esc(row.get(c)) for c in cols) + " |")
    path.write_text("\n".join(lines) + "\n")


def _manual_constituents(tickers=None, symbols_file=None):
    rows = []
    if tickers:
        for part in str(tickers).split(","):
            part = part.strip()
            if part:
                rows.append({"symbol": part})
    if symbols_file:
        parsed, _asof = parse_user_holdings(symbols_file)
        for row in parsed[0]:
            rows.append(row)
        for row in parsed[1]:
            rows.append(row)
    out, unmapped = [], []
    seen = set()
    for row in rows:
        raw = row.get("yahoo_symbol") or row.get("symbol")
        mode = "per-row" if (raw and (split_known_suffix(raw.upper())[1] or ":" in raw)) else ""
        yahoos, reason = map_to_yahoo(raw, mode)
        if not yahoos:
            yahoos, reason = map_to_yahoo(raw, "")
        if not yahoos:
            unmapped.append({"symbol": raw, "name": row.get("name"), "reason": reason or "unmapped"})
            continue
        for y in yahoos:
            if y in seen:
                continue
            seen.add(y)
            out.append({"symbol": row.get("symbol") or y, "yahoo_symbol": y, "name": row.get("name"), "weight": row.get("weight")})
    return out, unmapped


def cmd_earnings_calendar(a, R):
    try:
        start = parse_ymd(getattr(a, "date_from", None), "--from")
        end = parse_ymd(getattr(a, "date_to", None), "--to")
    except Skip as e:
        R.out["attempts"].append({"source": "fin.py", "status": "error", "message": str(e)})
        return
    if end < start:
        R.out["attempts"].append({"source": "fin.py", "status": "error", "message": "--to is before --from"})
        return
    if (end - start).days > 366:
        R.out["attempts"].append({"source": "fin.py", "status": "error", "message": "date range is longer than 366 days"})
        return

    index = None
    constituents = []
    manual_unmapped = []
    tickers = getattr(a, "tickers", None)
    symbols_file = getattr(a, "symbols_file", None)
    index_token = getattr(a, "index", None)
    if tickers or symbols_file:
        try:
            constituents, manual_unmapped = _manual_constituents(tickers, symbols_file)
        except (Skip, Empty) as e:
            R.out["attempts"].append({"source": "tickers", "status": "skipped" if isinstance(e, Skip) else "empty", "message": str(e)})
            return
        R.out["symbol"] = tickers or str(symbols_file)
        if index_token:
            R.out["notes"].append("--tickers/--symbols-file supplied; --index was not used to build the constituent list")
    elif index_token:
        selected, source_file = parse_source_arg(getattr(a, "source", None))
        file_path = getattr(a, "file", None) or source_file
        sub = Runner("constituents", index_token)
        try:
            registry = load_index_registry(registry_path(getattr(a, "registry", None)))
            index = resolve_index_entry(registry, index_token)
        except Skip as e:
            R.out["attempts"].append({"source": "registry", "status": "skipped", "message": str(e)})
            return
        R.out["symbol"] = index.get("id")
        run_constituent_chain(index, registry, selected, file_path, bool(getattr(a, "refresh", False)), sub)
        for attempt in sub.out["attempts"]:
            R.out["attempts"].append({"source": f"constituents:{attempt.get('source')}", **{k: v for k, v in attempt.items() if k != "source"}})
        R.out["notes"].extend(sub.out.get("notes") or [])
        if not sub.out["ok"]:
            R.out["notes"].append("No constituent source answered, so the earnings calendar was not queried.")
            return
        constituents = list(sub.out["data"].get("constituents") or [])
        manual_unmapped = list(sub.out["data"].get("unmapped") or [])
        if getattr(a, "top", None):
            constituents = rank_constituents(constituents, a.top, R.out["notes"])
    elif not getattr(a, "region", None):
        R.out["attempts"].append({"source": "fin.py", "status": "error", "message": "pass --index, --tickers, --symbols-file, or --region"})
        return

    # Carry registry unmapped into the result even when the name has no Yahoo ticker.
    all_constituents = constituents + manual_unmapped
    regions = regions_for(index, getattr(a, "region", None), constituents)
    verify_all = sparse_index(index, regions)
    if index is not None and not regions and not verify_all:
        verify_all = True
    events = []
    bulk_answered = False
    retrieved = now_utc()

    if FH_KEY and (index is not None or constituents or getattr(a, "region", None)):
        t0 = time.time()
        try:
            rows, url = finnhub_earnings_calendar(start, end)
            for row in rows:
                row["asof"] = retrieved
            events.extend(rows)
            bulk_answered = True
            R.out["attempts"].append({
                "source": "finnhub", "status": "ok", "ms": int((time.time() - t0) * 1000),
                "message": f"{len(rows)} rows {start.isoformat()}..{end.isoformat()}",
            })
            R.out["source_url"] = url
        except Skip as e:
            R.out["attempts"].append({"source": "finnhub", "status": "skipped", "message": str(e)})
        except Empty as e:
            R.out["attempts"].append({"source": "finnhub", "status": "empty", "message": str(e)})
        except Exception as e:
            R.out["attempts"].append({"source": "finnhub", "status": "error", "message": f"{type(e).__name__}: {str(e)[:200]}"})
    else:
        R.out["attempts"].append({"source": "finnhub", "status": "skipped", "message": "FINNHUB_API_KEY not set"})

    # Yahoo screener is the bulk path when Finnhub is unset. If Finnhub was set and
    # answered, still record that Yahoo was not required. If Finnhub failed, fall through.
    finnhub_ok = any(x.get("source") == "finnhub" and x.get("status") == "ok" for x in R.out["attempts"])
    yahoo_regions = [] if finnhub_ok else [r for r in regions if r in ("us", "ca")]
    if not finnhub_ok and not yahoo_regions and not verify_all and constituents:
        # US/CA index whose regions were filtered out should not happen; keep a note.
        pass
    for region in yahoo_regions:
        t0 = time.time()
        try:
            rows, message = yahoo_earnings_calendar(region, start, end)
            for row in rows:
                row["asof"] = retrieved
            events.extend(rows)
            bulk_answered = True
            R.out["attempts"].append({
                "source": "yahoo_calendar", "status": "ok" if rows else "empty",
                "ms": int((time.time() - t0) * 1000), "message": message,
            })
            if rows and not R.out.get("source_url"):
                R.out["source_url"] = "https://finance.yahoo.com/calendar/earnings"
        except Skip as e:
            R.out["attempts"].append({"source": "yahoo_calendar", "status": "skipped", "message": f"region {region}: {e}"})
        except Empty as e:
            R.out["attempts"].append({"source": "yahoo_calendar", "status": "empty", "message": f"region {region}: {e}"})
        except Exception as e:
            R.out["attempts"].append({"source": "yahoo_calendar", "status": "error", "message": f"region {region}: {type(e).__name__}: {str(e)[:180]}"})
    if finnhub_ok:
        R.out["attempts"].append({"source": "yahoo_calendar", "status": "skipped", "message": "Finnhub calendar answered; Yahoo screener not required"})
    elif not yahoo_regions:
        why = "non-US/CA index uses the per-ticker lookup" if verify_all else "no us/ca calendar region"
        R.out["attempts"].append({"source": "yahoo_calendar", "status": "skipped", "message": why})

    # Intersect the bulk pass first so the verify pass only sees misses.
    if constituents:
        pre_rows, pre_counts, pre_missing, pre_unmapped = assemble_earnings(all_constituents, events)
        if verify_all:
            to_verify = [c for c in constituents if c.get("yahoo_symbol")]
        else:
            missing_keys = {symkey(c.get("yahoo_symbol")) for c in pre_missing}
            to_verify = [c for c in constituents if symkey(c.get("yahoo_symbol")) in missing_keys]
    else:
        pre_rows, pre_missing = [], []
        to_verify = []
        pre_unmapped = []

    verify_errors = []
    verify_rows = []
    if to_verify:
        workers = int(getattr(a, "workers", None) or VERIFY_WORKERS_CAP)
        if workers > VERIFY_WORKERS_CAP:
            R.out["notes"].append(f"--workers capped at {VERIFY_WORKERS_CAP}")
        workers = max(1, min(VERIFY_WORKERS_CAP, workers))
        t0 = time.time()

        def job(row):
            found, err = ticker_earnings_in_window(row["yahoo_symbol"], start, end)
            for item in found:
                item["company"] = item.get("company") or row.get("name")
                item["symbol"] = row.get("symbol") or item.get("symbol")
                item["asof"] = retrieved
            return found, err

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for found, err in pool.map(job, to_verify):
                verify_rows.extend(found)
                if err:
                    verify_errors.append(err)
        status = "ok" if not verify_errors or verify_rows else "error"
        if verify_rows and verify_errors:
            status = "ok"
        R.out["attempts"].append({
            "source": "yahoo_earnings_dates", "status": status,
            "ms": int((time.time() - t0) * 1000),
            "message": f"looked up {len(to_verify)}, dated {len({r['yahoo_symbol'] for r in verify_rows})}, errors {len(verify_errors)}",
        })
        if verify_errors and len(verify_errors) <= 3:
            R.out["notes"].append("per-ticker lookup errors: " + "; ".join(verify_errors))
        elif verify_errors:
            R.out["notes"].append(f"{len(verify_errors)} per-ticker lookups failed; first: {verify_errors[0]}")
    else:
        R.out["attempts"].append({"source": "yahoo_earnings_dates", "status": "skipped", "message": "bulk pass covered every mapped constituent" if constituents else "no constituent list"})

    rows, counts, missing, unmapped = assemble_earnings(all_constituents, events + verify_rows)
    for row in rows:
        row["asof"] = row.get("asof") or retrieved
        row["retrieved_at_utc"] = retrieved
    answered = bulk_answered or any(x.get("source") == "yahoo_earnings_dates" and x.get("status") in ("ok", "empty") for x in R.out["attempts"])
    # A region-only screener that returned rows, or a constituent run whose lookups answered.
    if not constituents and bulk_answered:
        rows, counts, missing, unmapped = assemble_earnings([], events)
        for row in rows:
            row["asof"] = retrieved
            row["retrieved_at_utc"] = retrieved
        answered = True
    if not answered and not rows:
        R.out["data"] = {
            "from": start.isoformat(), "to": end.isoformat(), "rows": [],
            "counts": {"constituents": len(all_constituents), "found": 0, "no_date": len(constituents), "unmapped": len(manual_unmapped), "in_window": 0, "missing": len(constituents), "events": 0},
            "missing": [{"symbol": c.get("symbol"), "yahoo_symbol": c.get("yahoo_symbol"), "name": c.get("name")} for c in constituents],
            "unmapped": manual_unmapped,
            "asof": retrieved,
        }
        return
    sources = {r.get("source") for r in rows}
    if len(sources) == 1:
        R.out["source"] = next(iter(sources))
    elif rows:
        R.out["source"] = "mixed"
    elif bulk_answered:
        R.out["source"] = "finnhub" if finnhub_ok else "yahoo_calendar"
    else:
        R.out["source"] = "yahoo_earnings_dates"
    if not R.out.get("source_url"):
        R.out["source_url"] = "https://finance.yahoo.com/calendar/earnings"
    data = {
        "index_id": index.get("id") if index else None,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "asof": retrieved,
        "rows": rows,
        "counts": counts,
        "missing": missing,
        "unmapped": unmapped,
    }
    R.out["data"] = clean(data)
    R.out["asof"] = retrieved
    R.out["ok"] = True
    R.out["retrieved_at_utc"] = retrieved
    R.out["notes"].append("Yahoo Finance dates are projected. This command does not call the Nasdaq.com API.")
    if any(r.get("projected") for r in rows):
        R.out["notes"].append("Rows with projected true came from Yahoo (screener or per-ticker earnings dates), not from a company filing.")
    for flag, kind in (("csv_path", "csv"), ("md_path", "md")):
        dest = getattr(a, flag, None)
        if not dest:
            continue
        try:
            write_calendar_table(rows, dest, kind)
            R.out["notes"].append(f"wrote {kind} {dest}")
        except OSError as e:
            R.out["notes"].append(f"could not write {kind} {dest}: {e}")


def cmd_chart_income_sankey(a, R):
    """Render an income-statement Sankey, or write a draft spec from financials."""
    if a.from_symbol and a.spec:
        R.out["attempts"].append({"source": "spec", "status": "error", "message": "pass either --spec or --from, not both"})
        R.out["notes"].append("pass either --spec or --from, not both")
        return
    if not a.from_symbol and not a.spec:
        R.out["attempts"].append({"source": "spec", "status": "error", "message": "pass --spec PATH or --from TICKER"})
        R.out["notes"].append("pass --spec PATH or --from TICKER")
        return
    try:
        if a.from_symbol:
            limit = a.limit or (8 if a.period == "quarterly" else 3)
            ns = argparse.Namespace(
                symbol=a.from_symbol, period=a.period, statement="income", limit=limit, source=a.source,
            )
            inner = Runner("financials", a.from_symbol)
            cmd_financials(ns, inner)
            R.out["attempts"].extend(inner.out["attempts"])
            R.out["notes"].extend(inner.out.get("notes") or [])
            if not inner.out["ok"]:
                R.out["notes"].append("No income statement was returned, so no draft spec was built.")
                return
            spec = income_sankey.spec_from_financials(
                inner.out["data"],
                symbol=a.from_symbol,
                period=a.period,
                source=inner.out.get("source"),
                source_url=inner.out.get("source_url"),
                retrieved_at=inner.out.get("retrieved_at_utc"),
            )
            R.out["source"] = inner.out.get("source")
            R.out["source_url"] = inner.out.get("source_url")
        else:
            spec = income_sankey.load_spec(a.spec)
            R.out["source"] = "spec"
            R.out["source_url"] = str(Path(a.spec))
    except income_sankey.SpecError as exc:
        R.out["attempts"].append({"source": "spec", "status": "error", "message": str(exc)})
        R.out["notes"].append(str(exc))
        return
    spec = income_sankey.apply_overrides(
        spec, title=a.title, byline=a.byline, byline_color=a.byline_color, footer=a.footer,
    )
    R.out["notes"].extend(spec.get("notes") or [])
    if a.spec_out:
        dest = Path(a.spec_out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(income_sankey.dump_spec(spec), encoding="utf-8")
    if a.no_render:
        R.out.update(ok=True, retrieved_at_utc=now_utc(), data={
            "draft": bool(spec.get("draft")),
            "spec_out": str(a.spec_out) if a.spec_out else None,
            "title": spec.get("title"),
            "rendered": False,
        })
        return
    if not income_sankey.matplotlib_available():
        hint = income_sankey.MATPLOTLIB_HINT
        R.out["attempts"].append({"source": "matplotlib", "status": "skipped", "message": hint})
        R.out["notes"].append(hint)
        if a.spec_out:
            R.out["notes"].append(f"Spec written to {a.spec_out}.")
        return
    try:
        result = income_sankey.render_chart(
            spec, a.out, svg_path=a.svg, width=a.width, height=a.height, dpi=a.dpi,
            byline_icon=a.byline_icon, logo=a.logo, logo_width=a.logo_width,
        )
    except income_sankey.ChartDependencyError as exc:
        R.out["attempts"].append({"source": "matplotlib", "status": "skipped", "message": str(exc)})
        R.out["notes"].append(str(exc))
        return
    except income_sankey.SpecError as exc:
        R.out["attempts"].append({"source": "spec", "status": "error", "message": str(exc)})
        R.out["notes"].append(str(exc))
        return
    R.out["notes"].extend(note for note in result.get("notes") or [] if note not in R.out["notes"])
    R.out.update(ok=True, retrieved_at_utc=now_utc(), data={
        "png": result["png"],
        "svg": result["svg"],
        "width": result["width"],
        "height": result["height"],
        "dpi": result["dpi"],
        "font": result["font"],
        "draft": result["draft"],
        "spec_out": str(a.spec_out) if a.spec_out else None,
        "positive_topology": result["positive_topology"],
    })


def cmd_doctor(a, R):
    checks = {"version": __version__}
    try:
        import yfinance
        checks["yahoo"] = {"lib": f"yfinance {yfinance.__version__}"}
    except ImportError:
        checks["yahoo"] = {"lib": "MISSING: pip install --user yfinance"}
    checks["alphavantage"] = {"key": "set" if AV_KEY else "missing (ALPHAVANTAGE_API_KEY)", "free_tier": "25 requests/day"}
    checks["finnhub"] = {"key": "set" if FH_KEY else "missing (FINNHUB_API_KEY)", "free_tier": "60 calls/min; US-centric, many endpoints premium"}
    checks["sec_edgar"] = {"user_agent": SEC_UA, "ok_format": bool(re.search(r"\S+@\S+", SEC_UA))}
    checks["google"] = {"method": "public quote page scrape, no key, fragile"}
    checks["sedarplus"] = {
        "method": "one identifiable GET of the public site; no API key; captcha is not bypassed",
        "user_agent": sedar_user_agent(),
        "min_interval_seconds": SEDAR_MIN_INTERVAL,
        "routing": "filings tries sedarplus only after EDGAR has no CIK, for .TO/.V/.NE/.CN or a company name",
        "terms": SEDAR_TERMS,
        "limits": "Terms prohibit robots, scraping, automated searches, and databases. A block page or layout change fails the lookup.",
    }
    reg = registry_path(None)
    checks["index_registry"] = {
        "path": str(reg),
        "exists": reg.is_file(),
        "env": "FIN_INDEX_REGISTRY",
        "flag": "--registry",
        "example": str(EXAMPLE_INDEX_REGISTRY),
    }
    checks["constituents_cache"] = str(CACHE / "constituents")
    checks["yahoo_calendars"] = yahoo_calendar_status()
    try:
        import openpyxl
        checks["openpyxl"] = f"optional, installed {getattr(openpyxl, '__version__', '')}".strip()
    except ImportError:
        checks["openpyxl"] = "optional, not installed (SPDR holdings XLSX fallback needs it; imported only then)"
    checks["earnings_calendar"] = {
        "bulk": "Finnhub calendar/earnings when FINNHUB_API_KEY is set, otherwise yfinance Calendars for us and ca",
        "verify": "per-ticker yfinance earnings dates for names the bulk pass missed",
        "nasdaq_com": "not used",
    }
    checks["charting"] = income_sankey.charting_status()
    R.out.update(ok=True, source="local", retrieved_at_utc=now_utc(), data=checks)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    p = argparse.ArgumentParser(prog="fin.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"fin.py {__version__}")
    sp = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, symbol=True, src=True, symbol_help=None, **kw):
        q = sp.add_parser(name, **kw)
        if symbol:
            q.add_argument("symbol", help=symbol_help or "Yahoo-style ticker (AAPL, RY.TO, CSU.TO), a Canadian company name, or an SEC CIK for EDGAR")
        if src:
            q.add_argument("--source", help="comma list to restrict/force sources, e.g. finnhub,alphavantage or sec_edgar,sedarplus")
        q.set_defaults(fn=fn)
        return q

    q = add("quote", cmd_quote, help="latest price"); q.add_argument("--gexch", help="Google exchange code override (NASDAQ, NYSE, TSE...)")
    q.add_argument("--compare", action="store_true", help="cross-check price on the other sources")
    add("profile", cmd_profile, help="company profile + key stats")
    q = add("financials", cmd_financials, help="income/balance/cashflow statements")
    q.add_argument("--statement", default="all", choices=["income", "balance", "cashflow", "all"])
    q.add_argument("--period", default="annual", choices=["annual", "quarterly"]); q.add_argument("--limit", type=int, default=5)
    q = add("facts", cmd_facts, src=False, help="as-filed XBRL values from SEC EDGAR")
    q.add_argument("--concept", help="comma list, e.g. Revenues,RevenueFromContractWithCustomerExcludingAssessedTax,NetIncomeLoss")
    q.add_argument("--taxonomy", help="us-gaap | ifrs-full | dei"); q.add_argument("--forms", help="e.g. 10-K,10-Q")
    q.add_argument("--limit", type=int, default=12)
    q = add("earnings", cmd_earnings, help="EPS history, surprises, next date")
    q.add_argument("--kind", default="all", choices=["history", "surprises", "calendar", "all"]); q.add_argument("--limit", type=int, default=8)
    q = add("estimates", cmd_estimates, help="consensus EPS/revenue, revisions, price targets"); q.add_argument("--limit", type=int, default=8)
    q = add("peers", cmd_peers, help="peer candidates"); q.add_argument("--limit", type=int, default=15)
    q = add("holders", cmd_holders, help="top institutional / fund holders"); q.add_argument("--limit", type=int, default=10)
    q = add("news", cmd_news, help="recent company news"); q.add_argument("--limit", type=int, default=10); q.add_argument("--days", type=int, default=14)
    q = add("prices", cmd_prices, help="price history"); q.add_argument("--period", default="1y"); q.add_argument("--interval", default="1d")
    q = add("filings", cmd_filings, help="EDGAR filing list, or SEDAR+ when the issuer has no SEC CIK")
    q.add_argument("--forms", help="EDGAR: 10-K,10-Q,8-K,40-F,6-K. SEDAR+: annual information form, interim financial statements, MD&A, news release, material change report")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--accession"); q.add_argument("--document", help="file name inside the EDGAR filing (exhibits)")
    q.add_argument("--grep", help="regex; return matching paragraphs only"); q.add_argument("--max-chars", type=int, default=20000)
    q.add_argument("--offset", type=int, default=0); q.add_argument("--save", help="write full text to this path")
    q = add("sedar", cmd_sedar, src=False, symbol_help="TSX/TSXV/NEO/CSE ticker (CSU.TO, ABC.V) or issuer name",
            help="list recent SEDAR+ filings, or download one document URL")
    q.add_argument("--forms", help="e.g. annual information form,interim financial statements,MD&A,news release,material change report")
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--document", help="https://www.sedarplus.ca/... document URL to fetch (one request)")
    q.add_argument("--save", help="write --document bytes to this path")
    q = add("search", cmd_search, symbol=False, src=False, help="EDGAR full-text search")
    q.add_argument("query", help='e.g. "agreement and plan of merger" "Acme Corp"'); q.add_argument("--forms")
    q.add_argument("--start"); q.add_argument("--end"); q.add_argument("--limit", type=int, default=20)
    q = add("transcripts", cmd_transcripts, help="earnings call transcript (Alpha Vantage)")
    q.add_argument("--quarter", help="YYYYQn, e.g. 2026Q3"); q.add_argument("--grep"); q.add_argument("--max-chars", type=int, default=20000)
    q.add_argument("--offset", type=int, default=0); q.add_argument("--save")
    add("doctor", cmd_doctor, symbol=False, src=False, help="show configured sources/keys")
    q = add("constituents", cmd_constituents, src=False,
            symbol_help="index id or name (sp-500, sp500, tsx60, nasdaq-100, \"S&P/TSX 60\")",
            help="index members from the registry, with Yahoo tickers")
    q.add_argument("--registry", help="index registry JSON (default /home/box/agent-data/finance/index-registry.json or $FIN_INDEX_REGISTRY)")
    q.add_argument("--source", help="wikipedia,slickcharts,spdr,ishares,file:PATH (default: registry order, then --file)")
    q.add_argument("--file", help="CSV, JSON, or one ticker per line, tried after the registry sources")
    q.add_argument("--top", type=int, help="keep N names by weight, or by Yahoo market cap when there are no weights")
    q.add_argument("--refresh", action="store_true", help="ignore the constituents cache")
    q = add("earnings-calendar", cmd_earnings_calendar, symbol=False, src=False,
            help="earnings dates for an index or a ticker list")
    q.add_argument("--index", help="registry index id or name (sp500, tsx60, nasdaq-100)")
    q.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    q.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    q.add_argument("--tickers", help="comma-separated Yahoo tickers, instead of --index")
    q.add_argument("--symbols-file", help="ticker file (csv, json, or one per line), instead of --index")
    q.add_argument("--region", help="us,ca when not using an index: Yahoo screener only, no constituent intersection")
    q.add_argument("--registry", help="index registry JSON (default /home/box/agent-data/finance/index-registry.json or $FIN_INDEX_REGISTRY)")
    q.add_argument("--file", help="constituent file passed through to the index lookup")
    q.add_argument("--source", help="constituent source filter, same as constituents --source")
    q.add_argument("--top", type=int, help="limit the constituent list before the calendar lookup")
    q.add_argument("--refresh", action="store_true", help="ignore the constituents cache")
    q.add_argument("--workers", type=int, default=4, help="per-ticker lookup workers (capped at 4)")
    q.add_argument("--csv", dest="csv_path", help="also write a CSV table to this path")
    q.add_argument("--md", dest="md_path", help="also write a Markdown table to this path")

    chart = sp.add_parser("chart", help="render a chart from a local spec")
    chart_sp = chart.add_subparsers(dest="chart_kind", required=True)
    ik = chart_sp.add_parser(
        "income-sankey",
        help="income statement as a flow from segments to net profit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Render an income-statement Sankey to PNG (and optional SVG) from a CSV or JSON spec.\n"
            "matplotlib is optional: pip install matplotlib\n"
            "SVG logos need cairosvg, or pass a PNG. Open Sans is bundled; DejaVu Sans is the fallback.\n\n"
            "  fin.py chart income-sankey --spec examples/spacex-q3-2026-income-sankey.json --out chart.png\n"
            "  fin.py chart income-sankey --from AAPL --period quarterly --spec-out aapl.json --out aapl.png"
        ),
    )
    ik.add_argument("--spec", help="CSV or JSON spec (segments, lines, labels, footnotes)")
    ik.add_argument("--from", dest="from_symbol", help="ticker; build a draft spec from financials (review before relying on it)")
    ik.add_argument("--period", default="quarterly", choices=["annual", "quarterly"])
    ik.add_argument("--limit", type=int, help="periods to pull for --from (default 8 quarterly, 3 annual)")
    ik.add_argument("--source", help="financials source order for --from, same as financials --source")
    ik.add_argument("--title", help="override the spec title")
    ik.add_argument("--byline", help="override the byline under the title")
    ik.add_argument("--byline-icon", help="PNG or SVG path for the byline lockup (not bundled)")
    ik.add_argument("--byline-color", help="byline text color, e.g. #14213D")
    ik.add_argument("--logo", help="PNG or SVG path for the top-right logo (not bundled)")
    ik.add_argument("--logo-width", type=float, help="logo width in pixels (default 320 on a 1280-wide canvas, scaled with --width)")
    ik.add_argument("--footer", action="append", help="footer line; repeat to replace the spec footer")
    ik.add_argument("--out", default="income-sankey.png", help="PNG path (default income-sankey.png)")
    ik.add_argument("--svg", help="also write an SVG to this path")
    ik.add_argument("--width", type=int, default=1280, help="image width in pixels")
    ik.add_argument("--height", type=int, default=720, help="image height in pixels")
    ik.add_argument("--dpi", type=int, default=100, help="PNG/SVG dpi; font size tracks the pixel canvas")
    ik.add_argument("--spec-out", help="write the resolved spec JSON to this path")
    ik.add_argument("--no-render", action="store_true", help="only write --spec-out; do not draw (no matplotlib needed)")
    ik.set_defaults(fn=cmd_chart_income_sankey)

    a = p.parse_args(argv)
    command = a.cmd
    if command == "chart":
        command = f"chart {a.chart_kind}"
    symbol = getattr(a, "symbol", None) or getattr(a, "query", None) or getattr(a, "index", None) or getattr(a, "from_symbol", None)
    R = Runner(command, symbol)
    try:
        a.fn(a, R)
    except Exception as e:  # last-resort guard: still emit JSON
        R.out["attempts"].append({"source": "fin.py", "status": "error", "message": f"{type(e).__name__}: {e}"})
    return R.emit()


if __name__ == "__main__":
    sys.exit(main())
