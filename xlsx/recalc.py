#!/usr/bin/env python3
"""Recalculate every formula in an .xlsx with headless LibreOffice, then scan for Excel errors.

Usage: python3 recalc.py model.xlsx [timeout_seconds]
Prints JSON: {"status": "success"|"errors_found"|"failed", "total_formulas", "total_errors", "error_summary"}
The recalculated values are written back into the same file (formulas preserved).
"""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path

ERRORS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NULL!", "#NUM!", "#N/A")


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    src = Path(sys.argv[1]).resolve()
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print(json.dumps({"status": "failed", "error": "LibreOffice (soffice) not installed"})); sys.exit(2)
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        cmd = [soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--calc",
               "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", tmp, str(src)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        except Exception as e:
            print(json.dumps({"status": "failed", "error": str(e)[:300]})); sys.exit(2)
        out = Path(tmp) / src.name
        if not out.exists():
            print(json.dumps({"status": "failed", "error": "LibreOffice produced no output"})); sys.exit(2)
        from openpyxl import load_workbook
        vals = load_workbook(out, data_only=True)
        forms = load_workbook(src, data_only=False)
        n_f, errs = 0, {}
        for ws in forms.worksheets:
            vws = vals[ws.title]
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, str) and c.value.startswith("="):
                        n_f += 1
                        v = vws[c.coordinate].value
                        if isinstance(v, str) and v.strip() in ERRORS:
                            errs.setdefault(v.strip(), []).append(f"{ws.title}!{c.coordinate}")
        shutil.copy(out, src)  # keep LibreOffice-recalculated file (cached values present for viewers)
    res = {"status": "errors_found" if errs else "success", "total_formulas": n_f,
           "total_errors": sum(len(v) for v in errs.values())}
    if errs:
        res["error_summary"] = {k: {"count": len(v), "locations": v[:25]} for k, v in errs.items()}
    print(json.dumps(res, indent=1))
    sys.exit(0 if not errs else 1)


if __name__ == "__main__":
    main()
