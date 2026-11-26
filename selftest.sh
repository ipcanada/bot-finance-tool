#!/usr/bin/env bash
# Smoke test: every command x (AAPL, RY.TO). Prints one line per call: OK/FAIL, answering source, attempts.
# Usage: SEC_USER_AGENT="Name email" ./selftest.sh [SYMBOLS...]
cd "$(dirname "$0")"
syms=("${@:-AAPL RY.TO}"); [ $# -eq 0 ] && syms=(AAPL RY.TO)
run(){ python3 fin.py "$@" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin); a=[x['source']+':'+x['status'] for x in d['attempts']]
print(' '.join(sys.argv[1:])[:52].ljust(52), 'OK  ' if d['ok'] else 'FAIL', str(d['source']).ljust(18), ', '.join(a))" "$@"; }
python3 fin.py doctor | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin)['data']))"
for s in "${syms[@]}"; do
  run quote "$s"; run quote "$s" --source google; run profile "$s"; run financials "$s" --period quarterly --limit 4
  run facts "$s" --concept Revenues,NetIncomeLoss,ProfitLoss --limit 3; run earnings "$s"; run estimates "$s"
  run peers "$s"; run holders "$s"; run news "$s" --limit 5; run prices "$s" --period 3mo; run filings "$s" --limit 5
  run transcripts "$s" --quarter 2026Q3
done
run search '"agreement and plan of merger"' --forms 8-K --limit 3
