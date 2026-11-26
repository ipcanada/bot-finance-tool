#!/usr/bin/env python3
"""fin.py - market-data CLI for finance research agents.

Sources (only these): Yahoo Finance (yfinance, no key), Google Finance (public quote
pages, scraped, no key), Alpha Vantage (ALPHAVANTAGE_API_KEY), Finnhub (FINNHUB_API_KEY),
SEC EDGAR (no key, needs SEC_USER_AGENT contact string), SEDAR+ (public website, no key;
Canadian issuers that are not SEC registrants). SEDAR+ has no public API. This client
makes one identifiable request and stops if the site returns a block page or captcha.
It does not bypass either.

Every command prints one JSON object to stdout:
  {command, symbol, ok, source, source_url, retrieved_at_utc, data, attempts:[...], notes:[...]}
`attempts` records every source tried, in order, with status ok|skipped|empty|error.
Exit code 0 when a source answered, 2 when none did. It never raises on a missing key.
"""
import argparse, datetime as dt, html as htmllib, json, math, os, re, sys, time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print(json.dumps({"ok": False, "error": "pip install --user requests yfinance"}))
    sys.exit(2)

__version__ = "1.0.0"
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

    a = p.parse_args(argv)
    R = Runner(a.cmd, getattr(a, "symbol", None) or getattr(a, "query", None))
    try:
        a.fn(a, R)
    except Exception as e:  # last-resort guard: still emit JSON
        R.out["attempts"].append({"source": "fin.py", "status": "error", "message": f"{type(e).__name__}: {e}"})
    return R.emit()


if __name__ == "__main__":
    sys.exit(main())
