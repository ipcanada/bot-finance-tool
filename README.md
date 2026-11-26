# bot-finance-tool

Data tools for a Grok Bot finance research analyst template. `fin.py` is a market-data CLI that prints JSON from SEC EDGAR, Yahoo Finance, Google Finance, Alpha Vantage, and Finnhub. `xlsx/` holds small helpers for spreadsheet models.

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
```

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

Canadian issuers that do not file with the SEC (TSX-only) are on SEDAR+, which has no
public API and is **not** covered. Interlisted names (RY, TD, SHOP, BN...) are mapped
from `RY.TO` to the US ticker on EDGAR, with a note to confirm the issuer.

## Install

```bash
pip install --user yfinance requests      # add --break-system-packages on Debian/Ubuntu if pip refuses
export SEC_USER_AGENT="Jane Analyst jane@example.com"   # SEC requires a real contact
export ALPHAVANTAGE_API_KEY=...   # optional, free at alphavantage.co/support/#api-key
export FINNHUB_API_KEY=...        # optional, free at finnhub.io/register
python3 fin.py doctor             # shows what is configured
./selftest.sh                     # smoke test on AAPL and RY.TO
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
| `filings` | sec_edgar | `fin.py filings AAPL --forms 10-K,10-Q,8-K --limit 10` |
| `filings --accession` | sec_edgar | `fin.py filings AAPL --accession 0000320193-26-000020 --grep "total net sales"` / `--save q.txt` / `--document ex99-1.htm` |
| `search` | sec_edgar full-text | `fin.py search '"agreement and plan of merger"' --forms 8-K --start 2026-01-01` |
| `transcripts` | alphavantage → finnhub (premium) | `fin.py transcripts IBM --quarter 2024Q1 --grep guidance` |
| `doctor` | local | key/UA status |

Useful XBRL concepts: `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`,
`NetIncomeLoss`, `EarningsPerShareDiluted`, `OperatingIncomeLoss`, `CashAndCashEquivalentsAtCarryingValue`,
`LongTermDebt`, `WeightedAverageNumberOfDilutedSharesOutstanding`; IFRS filers (`ifrs-full`):
`Revenue`, `ProfitLoss`, `ProfitLossAttributableToOwnersOfParent`, `DilutedEarningsLossPerShare`.
Run `fin.py facts TICKER` with no `--concept` to list everything a company reports.

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

## Tested 2026-11-25 (America/Toronto)

| Source | Result |
|---|---|
| Yahoo | all commands OK for AAPL and RY.TO |
| Google | quote OK for AAPL:NASDAQ, RY:TSE, SHOP:TSE, BRK.B:NYSE, BBD.B:TSE; rejects unknown tickers |
| EDGAR | filings list, 10-Q text + grep, 40-F/6-K for RY, XBRL facts (us-gaap and ifrs-full), full-text search OK |
| Alpha Vantage | `demo` key: quote, overview, statements, earnings, estimates, daily prices, transcript (IBM) and news (AAPL) OK. Real key, TSX mapping and rate-limit handling untested |
| Finnhub | untested: no free demo key exists (`demo` returns 401, handled as skipped) |
