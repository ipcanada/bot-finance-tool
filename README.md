# bot-finance-tool

Data tools for a Grok Bot finance research analyst template. `fin.py` is a market-data CLI that prints JSON from SEC EDGAR, Yahoo Finance, Google Finance, Alpha Vantage, Finnhub, and SEDAR+ (Canadian issuers that do not file with the SEC). `xlsx/` holds small helpers for spreadsheet models.

## Install

```bash
git clone https://github.com/ipcanada/bot-finance-tool /home/box/finance-tools
cd /home/box/finance-tools
pip install --user yfinance requests
export SEC_USER_AGENT="Jane Analyst jane@example.com"   # SEC requires a name and contact email
# optional free-tier keys:
# export ALPHAVANTAGE_API_KEY=...
# export FINNHUB_API_KEY=...
python3 fin.py doctor
python3 fin.py --version
```

SEDAR+ uses the same `requests` install. Do not add a headless browser for it. On 2026-11-26 the public site answered both `requests` and stock headless Chrome with a Radware block page that loads hCaptcha. This tool stops there and does not try to solve or bypass the check.

## Disclaimer

Output is not investment advice. The data sources are unofficial interfaces or free tiers. Quotes, filing extracts, and estimates can be delayed, incomplete, rate-limited, or wrong. Check primary documents before you rely on a figure.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

---

# fin.py — market-data CLI for finance research

One Python file, JSON out, no paid data. Built for an agent that does sector research,
quarterly-report analysis and M&A work.

## Sources (and only these)

| Source | Key | What it is good for | Limits |
|---|---|---|---|
| Yahoo Finance (`yfinance`) | none | quotes, profile, statements, earnings dates/surprises, consensus, news, price history, US + TSX | unofficial API; can break or throttle; data normalized |
| Google Finance (public quote page) | none | second-opinion quote + key stats (mkt cap, P/E, EPS, div) | no official API; HTML scrape; delayed; layout changes break it; quote only |
| Alpha Vantage | `ALPHAVANTAGE_API_KEY` | statements, EPS history, estimates, news sentiment, **earnings-call transcripts** | free tier 25 req/day; TSX via `.TRT` suffix (auto-mapped) |
| Finnhub | `FINNHUB_API_KEY` | peers, company news, EPS surprises, as-reported financials, recommendation trends | free tier 60/min, US-centric; transcripts, estimates, candles are premium |
| SEC EDGAR | none (set `SEC_USER_AGENT="Name email"`) | filing lists, filing/exhibit text, as-filed XBRL facts, full-text search | US registrants + foreign filers (40-F/20-F/6-K); 10 req/s fair-access |
| SEDAR+ (public website) | none | recent filings for a Canadian issuer that is not an SEC registrant: interim and annual financial statements, MD&A, annual information form, news releases, material change reports; one document download when you already have a `sedarplus.ca` URL | no public API; terms prohibit robots, scraping, automated search, and databases; one request per lookup, at least 5 seconds apart; a captcha or layout change fails the command. See below |

Interlisted Canadian issuers (RY, TD, SHOP, BN, ...) still resolve from `RY.TO` to the US ticker on EDGAR, with a note to confirm the issuer. `filings` calls SEDAR+ only when that EDGAR lookup has no CIK: a `.TO` / `.V` / `.NE` / `.CN` ticker such as `CSU.TO` or `DOL.TO`, or a company name. A base ticker that belongs to a different SEC registrant (for example `NA.TO` matching Nano Labs, or `L.TO` matching Loews) stays on EDGAR with the confirmation note. Pass `--source sedarplus` to force the Canadian site.

## SEDAR+

SEDAR+ (https://www.sedarplus.ca/) is the CSA's public filing site. It has no documented filing API. The Alberta Securities Commission terms (https://systems.securities-administrators.ca/terms-of-use/, updated 28 April 2023) say you may use limited unaltered extracts for research, and they prohibit:

- robots, spiders, or other automatic access, scraping, or copying
- automated searches and repeated requests that burden the site
- building a database, or a product that redistributes the filings

`fin.py` is a single-issuer lookup, not a crawler. Each `sedar` or fall-through `filings` call sends one GET, names itself (`fin.py/<version>` plus your `SEC_USER_AGENT` contact when that looks like an email), and waits at least 5 seconds after the previous SEDAR+ request in this cache directory. It does not impersonate a browser, solve a captcha, reuse a challenge cookie, or try alternate endpoints when the first response is a block page.

If the site returns HTTP 401/403/429/503, a Radware/`rdwr` block, or an hCaptcha / "verifying your browser" page, the command exits 2 with `attempts[].status` of `skipped` and a message that starts with `SEDAR+ blocked this request`. No issuer search is submitted in that case. If a 200 page is not a filing list this client knows how to read, the attempt is `empty` and the message says the layout changed. Treat either as a hard stop and use the site in a browser.

Document download is `fin.py sedar CSU.TO --document 'https://www.sedarplus.ca/...' --save filing.pdf`. The URL has to be on `sedarplus.ca`. The bytes are whatever the site returned. They are not an official certified copy. A block page is not saved.

`./selftest.sh` runs an offline check of that block detection, then live `sedar` calls for `CSU.TO` and `DOL.TO`. Those live calls print `SKIP` when the site is blocked, captcha-gated, or unreachable, so a WAF does not fail the smoke test.

## Install

```bash
pip install --user yfinance requests      # add --break-system-packages on Debian/Ubuntu if pip refuses
export SEC_USER_AGENT="Jane Analyst jane@example.com"   # SEC requires a real contact
export ALPHAVANTAGE_API_KEY=...   # optional, free at alphavantage.co/support/#api-key
export FINNHUB_API_KEY=...        # optional, free at finnhub.io/register
python3 fin.py doctor             # shows what is configured, including the SEDAR+ terms and interval
python3 fin.py --version
./selftest.sh                     # smoke test on AAPL and RY.TO, plus a SEDAR+ check that skips if the site blocks
```

## Commands

Tickers are Yahoo-style: `AAPL`, `BRK-B`, `RY.TO`, `SHOP.TO`, `XYZ.V`. EDGAR commands also accept a CIK.
`--source a,b` restricts/forces the source order on any multi-source command.

| Command | Fallback order | Example |
|---|---|---|
| `quote` | yahoo → google → finnhub → alphavantage | `fin.py quote RY.TO --compare` (cross-checks, flags >1% gaps) |
| `profile` | yahoo → finnhub → alphavantage | `fin.py profile AAPL` |
| `financials` | yahoo → alphavantage → finnhub (as-reported) | `fin.py financials AAPL --statement income --period quarterly --limit 8` |
| `facts` | sec_edgar (XBRL as filed, with accession numbers) | `fin.py facts AAPL --concept RevenueFromContractWithCustomerExcludingAssessedTax --forms 10-Q` |
| `earnings` | yahoo → finnhub → alphavantage | `fin.py earnings RY.TO --kind surprises` (`history`/`calendar`/`all`) |
| `estimates` | yahoo → alphavantage → finnhub | `fin.py estimates AAPL` (EPS/revenue consensus, revisions, price targets) |
| `peers` | finnhub → yahoo (industry constituents, needs pruning) | `fin.py peers AAPL --limit 15` |
| `holders` | yahoo → finnhub (premium) | `fin.py holders AAPL --limit 10` |
| `news` | yahoo → finnhub → alphavantage | `fin.py news AAPL --days 7` |
| `prices` | yahoo → alphavantage → finnhub | `fin.py prices RY.TO --period 5y --interval 1wk` |
| `filings` | sec_edgar, then sedarplus if there is no CIK | `fin.py filings AAPL --forms 10-K,10-Q,8-K --limit 10` |
| `filings` (TSX-only) | sedarplus after EDGAR misses | `fin.py filings CSU.TO --limit 10` or `fin.py filings "Constellation Software"` |
| `filings --accession` | sec_edgar | `fin.py filings AAPL --accession 0000320193-26-000020 --grep "total net sales"` / `--save q.txt` / `--document ex99-1.htm` |
| `sedar` | sedarplus | `fin.py sedar DOL.TO --forms "annual information form,MD&A" --limit 10` |
| `sedar --document` | sedarplus | `fin.py sedar DOL.TO --document 'https://www.sedarplus.ca/...' --save filing.pdf` |
| `search` | sec_edgar full-text | `fin.py search '"agreement and plan of merger"' --forms 8-K --start 2026-01-01` |
| `transcripts` | alphavantage → finnhub (premium) | `fin.py transcripts IBM --quarter 2024Q1 --grep guidance` |
| `constituents` | registry order: Wikipedia table, then fallback (Slickcharts, SPDR XLSX, iShares Canada CSV), then a user file | `fin.py constituents tsx60` |
| `earnings-calendar` | Finnhub calendar if `FINNHUB_API_KEY` is set, else Yahoo screener (US/CA), then per-ticker Yahoo dates for names the bulk pass missed | `fin.py earnings-calendar --index sp500 --from 2026-12-01 --to 2026-12-04` |
| `doctor` | local | key/UA status |

Useful XBRL concepts: `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`,
`NetIncomeLoss`, `EarningsPerShareDiluted`, `OperatingIncomeLoss`, `CashAndCashEquivalentsAtCarryingValue`,
`LongTermDebt`, `WeightedAverageNumberOfDilutedSharesOutstanding`; IFRS filers (`ifrs-full`):
`Revenue`, `ProfitLoss`, `ProfitLossAttributableToOwnersOfParent`, `DilutedEarningsLossPerShare`.
Run `fin.py facts TICKER` with no `--concept` to list everything a company reports.

## Index constituents and the earnings calendar

`constituents` and `earnings-calendar` read an index registry. The default path is `/home/box/agent-data/finance/index-registry.json`. Override it with `--registry PATH` or `FIN_INDEX_REGISTRY`. That file stays out of this repo. `examples/index-registry.example.json` is a fixture: the seed list plus the SPDR and iShares Canada holdings URLs checked on 2026-11-28. Ids and short names both work (`sp-500` / `sp500`, `sp-tsx-60` / `tsx60`, `nasdaq-100`).

Source order for `constituents` is the registry `constituents_source`, then each `fallback_source` (one object or a list), then `--file` or `--source file:PATH`. Wikipedia tables are fetched as the article HTML (a descriptive User-Agent, at most one request a second). robots.txt allows those article pages; this client does not call the MediaWiki API. Slickcharts is the HTML table for the Nasdaq-100, Dow, and S&P 500. SPDR daily holdings workbooks (DIA, SPY, MDY) need `openpyxl`, imported only when that source runs. iShares Canada CSV holdings are XIU for the TSX 60 and XIC for the TSX Composite. A user file may be CSV, JSON, or one ticker per line.

Symbols are mapped to Yahoo tickers with the registry suffix (`.` in a class share becomes `-`, so `BBD.B` with `.TO` is `BBD-B.TO`, and `BRK.B` is `BRK-B`). A per-row suffix uses an exchange prefix or a Main listing value only when that venue maps to one Yahoo suffix. `Euronext` without a city, `KRX`, and a local code that contains a space stay in `unmapped`. Nothing is guessed. Results are cached under `FIN_CACHE_DIR/constituents` (default `~/.cache/fin-py/constituents`) for 30 days, 7 days for `nasdaq-100` and `stoxx-europe-600`. `--refresh` fetches again. `--source` and `--file` do not read or replace that default cache. `--top N` keeps the N largest weights, or asks Yahoo for market cap when the list has no weights.

`earnings-calendar --from YYYY-MM-DD --to YYYY-MM-DD` takes `--index`, or `--tickers` / `--symbols-file` instead of an index, or `--region us,ca` for the screener alone. The bulk pass calls Finnhub `calendar/earnings` when `FINNHUB_API_KEY` is set. Otherwise it pages the Yahoo earnings screener through `yfinance.Calendars` (yfinance 1.8 or newer): the public method for `us`, and `Calendars._get_data` with a region for `ca`. Yahoo's end bound is midnight at the start of `--to`, so the request runs through the next date and the rows are filtered back to the requested dates. Rows are intersected with the constituents. Names the bulk pass missed, and every mapped name on a non-US/CA index, get one `yfinance` earnings-date lookup (at most 4 workers, one retry if Yahoo throttles). The Nasdaq.com API is not called. JSON rows have `date`, `ticker`, `company`, `timing` (`BMO`, `AMC`, `intraday`, or `unknown`), `eps_estimate`, `source`, and `asof`. `counts` includes `found`, `no_date`, and `unmapped`. `--csv` and `--md` write the same rows beside the JSON. Yahoo dates are marked `projected`.

On HTTP 401, 403, 406, or 429, or on a captcha / browser-check page, that source stops. The attempt is `skipped` and the message starts with `blocked`. There is no second request, no alternate User-Agent, and no browser. Exit code is 2 when no source answered.

## Output contract

```json
{"command": "quote", "symbol": "AAPL", "ok": true, "source": "yahoo",
 "source_url": "https://finance.yahoo.com/quote/AAPL", "retrieved_at_utc": "2026-11-26T02:08:56+00:00",
 "data": {...}, "attempts": [{"source": "yahoo", "status": "ok"}], "notes": []}
```

- `attempts` lists every source tried with `ok | skipped | empty | error` and a message
  (e.g. `ALPHAVANTAGE_API_KEY not set`, `HTTP 403 ... not on this plan`).
- Exit 0 if a source answered, 2 if none did. Missing keys never crash the tool.
- API keys never appear in `source_url`.

## Transcripts

Only Alpha Vantage has free transcripts (`EARNINGS_CALL_TRANSCRIPT`, needs your key; the `demo`
key works only for IBM 2024Q1). Yahoo, Google and EDGAR have none; Finnhub's are premium.
Fallback: the earnings press release / CFO commentary in the 8-K Exhibit 99.1 via `filings --document`.

## Tested 2026-11-25, SEDAR+ rechecked 2026-11-26

| Source | Result |
|---|---|
| Yahoo | all commands OK for AAPL and RY.TO |
| Google | quote OK for AAPL:NASDAQ, RY:TSE, SHOP:TSE, BRK.B:NYSE, BBD.B:TSE; rejects unknown tickers |
| EDGAR | filings list, 10-Q text + grep, 40-F/6-K for RY, XBRL facts (us-gaap and ifrs-full), full-text search OK |
| SEDAR+ | 2026-11-26, from this environment, no issuer search was submitted. `requests` saw two block shapes: HTTP 403 from server `rdwr`, and HTTP 200 from Apache titled "Radware Block Page" loading hCaptcha from `captcha.perfdrive.com`. Stock headless Chrome got the same block page. `fin.py sedar CSU.TO` (Constellation Software) and `fin.py sedar "Dollarama Inc."` both stopped there, exit 2. `filings CSU.TO` and `filings DOL.TO` skipped EDGAR (no CIK) and then hit the same block. `filings AAPL` and `filings RY.TO` stayed on EDGAR. A `--document` download was refused and nothing was written. No Playwright dependency. |
| Alpha Vantage | `demo` key: quote, overview, statements, earnings, estimates, daily prices, transcript (IBM) and news (AAPL) OK. Real key, TSX mapping and rate-limit handling untested |
| Finnhub | untested: no free demo key exists (`demo` returns 401, handled as skipped) |
