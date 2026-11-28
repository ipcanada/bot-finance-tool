# Changelog

## 1.1.0 — 2026-11-28

### Added

- `constituents`: resolve an index from the registry at `/home/box/agent-data/finance/index-registry.json` (`--registry` or `FIN_INDEX_REGISTRY`). Order is the Wikipedia table, then `fallback_source` (Slickcharts for the Nasdaq-100, Dow, and S&P 500; SPDR holdings XLSX for DIA, SPY, and MDY; iShares Canada CSV for XIU and XIC), then a user file. Yahoo tickers are mapped from the registry suffix. Class-share dots become hyphens. Rows with no single Yahoo suffix are listed under `unmapped` and are not guessed. Cache is `FIN_CACHE_DIR/constituents/<id>.json` (30 days, 7 for the Nasdaq-100 and STOXX Europe 600).
- `earnings-calendar --from YYYY-MM-DD --to YYYY-MM-DD` with `--index`, or `--tickers` / `--symbols-file`, or `--region us,ca`. Bulk pass is Finnhub's earnings calendar when `FINNHUB_API_KEY` is set, otherwise the Yahoo screener via `yfinance.Calendars` (US and CA). Names that bulk pass misses get a per-ticker Yahoo earnings-date lookup (4 workers, one throttle retry). JSON rows include date, ticker, company, BMO/AMC/intraday/unknown, EPS estimate, source, and as-of. `counts` reports found, no date, and unmapped. Optional `--csv` and `--md`.
- `doctor` reports the registry path, the constituents cache, whether yfinance Calendars is available (needs yfinance 1.8 or newer), and that openpyxl is optional. `./selftest.sh` checks the new commands offline, then live `constituents tsx60`, `constituents nasdaq-100`, and a one-day US earnings calendar. A block or an unreachable host prints `SKIP`.
- `examples/index-registry.example.json`, the seed registry plus the SPDR and iShares fallbacks. The live registry path is not part of this repo.

### Limitation

A block page, captcha, HTTP 403, or HTTP 406 stops that source. The attempt records it and the client does not retry with another identity. Slickcharts answered HTTP 403 ("Just a moment...") from the environment used to cut this release, so `constituents nasdaq-100` stops there until the registry has another source or you pass `--file`. The Wikipedia S&P/TSX 60 table has an empty symbol cell for National Bank of Canada; that row stays unmapped. SPDR and iShares files are ETF holdings, not the official index, and can include a residual line. The Nasdaq.com earnings API is not used. Yahoo screener dates are projected.

## 1.0.0 — 2026-11-26

First semantic version. The in-file label before this release was an informal `1.0`.

### Added

- `__version__` (`1.0.0`), `fin.py --version`, and `version` in `fin.py doctor`.
- `sedar` command: look up a TSX/TSXV/NEO/CSE ticker or a Canadian issuer name on the SEDAR+ public site, and download one `sedarplus.ca` document URL with `--document` and `--save`.
- `filings` tries SEDAR+ after SEC EDGAR when the symbol has no CIK and is a `.TO` / `.V` / `.NE` / `.CN` ticker or a company name. Interlisted issuers that resolve to a CIK stay on EDGAR. `--source sedarplus` forces the Canadian site when a base ticker collides with a different SEC registrant.
- A per-machine pause of at least 5 seconds between SEDAR+ requests, an identifiable User-Agent, and a hard stop when the response is a block page or captcha. The client does not bypass either.
- `./selftest.sh` offline checks for that routing and block detection, plus live SEDAR+ calls that print `SKIP` when the site is blocked or unreachable.

### Limitation

SEDAR+ has no public API. Its terms prohibit robots, scraping, automated searches, and building a database. On 2026-11-26 the public site answered this client with HTTP 403 (`server: rdwr`) and with HTTP 200 titled "Radware Block Page" (hCaptcha from `captcha.perfdrive.com`). Stock headless Chrome received the same block page. This release detects that and reports it. It does not list filings from behind the challenge, and it does not save a block page when `--document` is used. If a later response is a 200 page this client does not recognize as a filing list, the attempt is `empty` with a layout-changed message rather than a guessed parse.

### Unchanged

Yahoo Finance, Google Finance, Alpha Vantage, Finnhub, and SEC EDGAR commands. Quotes, financials, facts, and transcripts are not routed to SEDAR+.
