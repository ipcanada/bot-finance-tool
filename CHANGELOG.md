# Changelog

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
