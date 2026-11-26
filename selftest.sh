#!/usr/bin/env bash
# Smoke test: every command x (AAPL, RY.TO). Prints one line per call: OK/FAIL, answering source, attempts.
# SEDAR+ is an extra check: SKIP when the site is blocked, captcha-gated, or unreachable.
# Usage: SEC_USER_AGENT="Name email" ./selftest.sh [SYMBOLS...]
cd "$(dirname "$0")"
syms=("${@:-AAPL RY.TO}"); [ $# -eq 0 ] && syms=(AAPL RY.TO)
python3 - <<'PY'
import fin
assert fin.__version__
html = "<html><title>Radware Block Page</title><script src='https://captcha.perfdrive.com/x.js'></script>hcaptcha</html>"
assert fin.sedar_block_reason(200, html, "")
assert "blocked this request" in fin.sedar_block_reason(403, "<h2>403 Forbidden</h2>Transaction ID: abcdef0123456789", "rdwr")
assert fin.sedar_block_reason(200, "<html><title>SEDAR+</title><body>annual information form</body></html>", "nginx") is None
assert fin.sedar_applicable("CSU.TO") and fin.sedar_applicable("ctc-a.to") and fin.sedar_applicable("ABC.V")
assert fin.sedar_applicable("Constellation Software Inc.")
assert not fin.sedar_applicable("AAPL") and not fin.sedar_applicable("0000320193")
assert fin.sedar_applicable("RY.TO")  # candidate only; filings still prefers EDGAR when a CIK exists
fin.sedar_fallback_allowed("CSU.TO", [{"source": "sec_edgar", "status": "skipped", "message": "CSU.TO not found in EDGAR ticker map"}])
fin.sedar_fallback_allowed("RY.TO", [])  # forced --source sedarplus
try:
    fin.sedar_fallback_allowed("RY.TO", [{"source": "sec_edgar", "status": "skipped", "message": "SEC returned 403: set SEC_USER_AGENT"}])
    raise SystemExit("EDGAR outage was allowed to fall through to SEDAR+")
except fin.Skip as e:
    assert "did not confirm" in str(e)

class Resp:
    def __init__(self, status, body, server="rdwr", ctype="text/html"):
        self.status_code = status
        self.text = body
        self.content = body.encode()
        self.headers = {"server": server, "content-type": ctype}
        self.encoding = "utf-8"

def fake_get(url, headers=None, timeout=None):
    return Resp(403, "<html><title>Radware Block Page</title>hcaptcha captcha.perfdrive.com</html>")

fin.SEDAR_MIN_INTERVAL = 0
fin.requests.get = fake_get
try:
    fin.sedar_query("CSU.TO", 5)
    raise SystemExit("block page was treated as success")
except fin.Skip as e:
    assert "blocked this request" in str(e) and "captcha" in str(e).lower()
try:
    fin.sedar_query("CSU.TO", 5, document="https://evil.example/secret.pdf", save="/tmp/fin-sedar-should-not-exist")
    raise SystemExit("off-site document URL was accepted")
except fin.Skip as e:
    assert "sedarplus.ca" in str(e)
print("offline sedar checks OK", fin.__version__)
PY
if [ $? -ne 0 ]; then
  echo "offline sedar checks FAIL"
  exit 1
fi
python3 fin.py --version
run(){ python3 fin.py "$@" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin); a=[x['source']+':'+x['status'] for x in d['attempts']]
print(' '.join(sys.argv[1:])[:52].ljust(52), 'OK  ' if d['ok'] else 'FAIL', str(d['source']).ljust(18), ', '.join(a))" "$@"; }
# SKIP when SEDAR+ is blocked, captcha-gated, or unreachable. FAIL when the site answered and we still could not read it.
run_sedar(){ python3 fin.py "$@" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
a=d.get('attempts') or []
blob=' '.join((x.get('message') or '') for x in a).lower()
if d.get('ok'):
    flag='OK  '
elif any(s in blob for s in ('blocked this request','unreachable','captcha','browser-verification')):
    flag='SKIP'
else:
    flag='FAIL'
print(' '.join(sys.argv[1:])[:52].ljust(52), flag, str(d.get('source')).ljust(18), ', '.join(x['source']+':'+x['status'] for x in a))" "$@"; }
python3 fin.py doctor | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin)['data']))"
for s in "${syms[@]}"; do
  run quote "$s"; run quote "$s" --source google; run profile "$s"; run financials "$s" --period quarterly --limit 4
  run facts "$s" --concept Revenues,NetIncomeLoss,ProfitLoss --limit 3; run earnings "$s"; run estimates "$s"
  run peers "$s"; run holders "$s"; run news "$s" --limit 5; run prices "$s" --period 3mo; run filings "$s" --limit 5
  run transcripts "$s" --quarter 2026Q3
done
run search '"agreement and plan of merger"' --forms 8-K --limit 3
echo "-- SEDAR+ (SKIP if blocked, captcha-gated, or unreachable) --"
run_sedar sedar CSU.TO --limit 5
run_sedar sedar DOL.TO --limit 5
run_sedar filings CSU.TO --limit 3

