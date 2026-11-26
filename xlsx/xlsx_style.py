"""Tiny openpyxl helpers that enforce the model conventions (import from your build script).

    import sys; sys.path.insert(0, "<skill dir>/scripts"); from xlsx_style import *
    wb = Workbook(); ws = wb.active; ws.title = "Inputs"
    section(ws, "A1:F1", "KEY INPUTS")
    inp(ws, "C3", 355.58, "Yahoo Finance quote, retrieved 2026-11-25 21:08 ET", fmt=USD2)
    calc(ws, "C10", "=C3*C4", fmt=USD0)          # black formula
    link(ws, "C11", "=WACC!C20")                    # green cross-sheet / purple same-sheet
    output(ws, "C30", "=C28/C29", fmt=USD2)         # medium-blue fill, bold
"""
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

BLUE, BLACK, GREEN, PURPLE = "0000FF", "000000", "008000", "800080"
DARK, LIGHT, MID, GREY = "1F4E79", "D9E1F2", "BDD7EE", "F2F2F2"
USD0, USD2, PCT, MULT, MULT2 = '$#,##0;($#,##0);"-"', "$#,##0.00", "0.0%", '0.0"x"', '0.00"x"'
THIN = Side(style="thin", color="999999")
THICK = Side(style="medium", color="1F4E79")


def _fill(c):
    return PatternFill("solid", start_color=c, end_color=c)


def inp(ws, ref, value, source, fmt=None):
    """Hard-coded input: blue font, grey fill, mandatory source comment."""
    if not source:
        raise ValueError(f"{ref}: every input needs a source comment")
    c = ws[ref]; c.value = value
    c.font = Font(color=BLUE); c.fill = _fill(GREY); c.alignment = Alignment(horizontal="right")
    c.comment = Comment(f"Source: {source}", "model")
    if fmt: c.number_format = fmt
    return c


def calc(ws, ref, formula, fmt=None, bold=False):
    if not str(formula).startswith("="):
        raise ValueError(f"{ref}: calc cells must be formulas, got {formula!r}")
    c = ws[ref]; c.value = formula; c.font = Font(color=BLACK, bold=bold); c.alignment = Alignment(horizontal="right")
    if fmt: c.number_format = fmt
    return c


def link(ws, ref, formula, fmt=None):
    """Pure reference: green if cross-sheet, purple if same sheet."""
    c = calc(ws, ref, formula, fmt)
    c.font = Font(color=GREEN if "!" in formula else PURPLE)
    return c


def output(ws, ref, formula, fmt=None):
    c = calc(ws, ref, formula, fmt, bold=True); c.fill = _fill(MID)
    return c


def section(ws, rng, title):
    first = rng.split(":")[0]
    ws[first] = title  # value first, then merge (avoids merged-cell write errors)
    ws.merge_cells(rng)
    ws[first].font = Font(color="FFFFFF", bold=True); ws[first].fill = _fill(DARK)


def header_row(ws, row, col_start, labels):
    for i, lab in enumerate(labels):
        c = ws.cell(row=row, column=col_start + i, value=str(lab))
        c.font = Font(bold=True); c.fill = _fill(LIGHT); c.alignment = Alignment(horizontal="center")


def box(ws, rng):
    """Medium outline around a section."""
    rows = list(ws[rng])
    for r_i, row in enumerate(rows):
        for c_i, c in enumerate(row):
            c.border = Border(top=THICK if r_i == 0 else c.border.top, bottom=THICK if r_i == len(rows) - 1 else c.border.bottom,
                              left=THICK if c_i == 0 else c.border.left, right=THICK if c_i == len(row) - 1 else c.border.right)
