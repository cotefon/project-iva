"""Extract F29 (Formulario 29 / IVA) code values from a PDF.

Pipeline:
    1. PDF in  -> pdf_to_text()     (via pdfplumber)
    2. Text    -> extract_codes()   (regex)
    3. dict {code: [values]} out, ready to feed a formula.

The extracted text for these SII forms is NOT a clean table: the same document
mixes loose text lines and rows that pack up to two `codigo / glosa / valor`
triples onto one physical line. So we do not parse table structure. Instead, for
each target code we locate the 3-digit code token and take the first Chilean-
formatted number that follows it *on the same line* as its value.

Assumptions (hold for the target codes 020, 142, 538, 587 in the F29 layout):
  - Form codes are 3-digit, zero-padded ("20" -> "020").
  - A value is a plain integer or dot-grouped thousands ("4.425.564.773").
  - The value sits after the code's glosa, on the same line, and the glosa
    itself contains no digits before that value.
  - A code token is standalone, never a fragment inside a value: we require it
    to be surrounded by non-digit/non-dot chars so "142" does NOT match inside
    "4.142.500".

Monthly table:
    Each monthly declaration is delimited by its PERIODO field ("MM / YYYY").
    We slice the document per period, extract the formula codes for each month,
    apply the sales formula, and render a Markdown table.
"""

from __future__ import annotations

import os
import re
import sys
from itertools import groupby

# Codes used by the monthly sales formula, in the order shown in the table.
FORMULA_CODES = ["020", "142", "538", "587"]
# Codes used by the purchases formula (Compras):
#   535 + 520 + 528 / 0,19 + 521 + 532 + 560 + 562
COMPRAS_CODES = ["535", "520", "528", "521", "532", "560", "562"]
# Number of sales invoices issued in the period (F29 "Facturas emitidas").
# A plain count, NOT a peso amount — kept out of ALL_CODES so the API's
# round-to-thousand presentation never touches it. Used to average a year's
# totals per invoice in the Promedio row.
INVOICE_CODE = "503"
# Every peso-valued code we extract per declaration (both formulas). The two
# lists are disjoint, so a plain concatenation holds each code exactly once.
ALL_CODES = FORMULA_CODES + COMPRAS_CODES
TARGET_CODES = ALL_CODES + [INVOICE_CODE]

# VAT rate applied to a tax code (538 for Venta del mes, 528 for Compras) to
# recover the net base.
RATE = 0.19

SPANISH_MONTHS = {
    1: "Ene",
    2: "Feb",
    3: "Mar",
    4: "Abr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Ago",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dic",
}

# A Chilean-formatted amount: 1-3 leading digits, then dot-grouped thousands.
# Matches "0", "331", "17.738.025", "4.425.564.773".
_VALUE = r"\d{1,3}(?:\.\d{3})*"

# PERIODO field of a declaration. Two producer encodings, both anchored on the
# "PERIODO" label; .*? skips the field's box code ("15" / "[15]") and lands on
# the date. `.` never crosses a newline, so each match stays on its own line.
#   htmldoc:     "PERIODO | 15 | 09 / 2024"  -> MM / YYYY  (month, year)
#   Apache FOP:  "PERIODO [15] 202409"        -> YYYYMM     (year, month)
PERIOD_MMYYYY_RE = re.compile(r"PERIODO.*?(\d{1,2})\s*/\s*(\d{4})")
PERIOD_YYYYMM_RE = re.compile(r"PERIODO.*?\b(\d{4})(0[1-9]|1[0-2])\b")
# Back-compat alias (the MM/YYYY form was the original PERIOD_RE).
PERIOD_RE = PERIOD_MMYYYY_RE

# FOLIO of a declaration: the "FOLIO" label, its box code ("07"), then the
# folio number itself. `.` never crosses a newline, so the match stays on the
# FOLIO line. Requiring 7+ digits skips the 2-digit box code and lands on the
# 10-digit folio, tolerant of a stray separator ("FOLIO 07 7845086876",
# "FOLIO | 07 | 7845086876"). Case-sensitive so the header line
# "Corrige a Folio(s):" (lower-case) never matches.
FOLIO_RE = re.compile(r"FOLIO.*?(\d{7,})")

# Taxpayer identity, from the document header. The label is not spelled the same
# in every document we get, so each field is a list of patterns tried in order:
#
#   "Nombre del emisor: ..."  / "RUT del emisor: 79527050 − 7"   carpeta tributaria
#   "Nombre del Emisor: ..."  / "RUT del Emisor: 76044491-K"     same, capitalised
#   "Nombre/Razón Social: ..." / "RUT: 76.044.491-K"             resumen IVA
#
# The emisor labels come first: the bare "RUT:" fallback is generic enough to hit
# an unrelated line in a carpeta, so it must only be reached when no "RUT del
# emisor" exists in the document at all.
#
# Values run to the end of the line. `re.I` covers the emisor/Emisor casing; the
# pypdf backend pads the label with spaces, which `\s*` absorbs.
EMISOR_NAME_RES = [
    re.compile(r"Nombre\s+del\s+emisor\s*:\s*(.+)", re.I),
    re.compile(r"Nombre\s*/\s*Raz[oó]n\s+Social\s*:\s*(.+)", re.I),
]
# RUT body then verifier digit, tolerant of any dash (hyphen, U+2212, en/em dash)
# and surrounding spaces: "79527050 − 7", "79.527.050-7" or "76044491-K".
EMISOR_RUT_RES = [
    re.compile(r"RUT\s+del\s+emisor\s*:\s*([\d.]+)\s*[-−–—]\s*([\dkK])", re.I),
    re.compile(r"^RUT\s*:\s*([\d.]+)\s*[-−–—]\s*([\dkK])", re.I | re.M),
]


def _first_match(patterns: list[re.Pattern], text: str) -> re.Match | None:
    """The first pattern in `patterns` that matches anywhere in `text`."""
    return next(filter(None, (p.search(text) for p in patterns)), None)


def format_rut(body: str, dv: str) -> str:
    """('79527050', '7') -> '79.527.050-7' (Chilean dots + verifier digit)."""
    digits = re.sub(r"\D", "", body)
    grouped = f"{int(digits):,}".replace(",", ".")
    return f"{grouped}-{dv.upper()}"


def extract_taxpayer(text: str) -> dict:
    """Pull the taxpayer's name and RUT from the carpeta header.

    Returns {"nombre": str | None, "rut": str | None}; a field is None when none
    of its label spellings appear in the document.
    """
    name_m = _first_match(EMISOR_NAME_RES, text)
    rut_m = _first_match(EMISOR_RUT_RES, text)
    return {
        "nombre": name_m.group(1).strip() if name_m else None,
        "rut": format_rut(rut_m.group(1), rut_m.group(2)) if rut_m else None,
    }


def folios_by_period(text: str) -> dict[tuple[int, int], str]:
    """Map {(year, month): folio} for every declaration in the document.

    Unlike the codes — which sit *below* the PERIODO line — the FOLIO field is
    printed just *above* it, so it belongs to the PERIODO immediately following
    it, not the one preceding it. Slicing on PERIODO alone therefore misfiles the
    folio by one declaration. Instead we pair each PERIODO marker with the last
    FOLIO that falls between the previous PERIODO marker and this one, i.e. the
    folio inside this declaration's own form. A declaration with no FOLIO in that
    window maps to None.
    """
    marks = _period_marks(text)
    folios = [(m.start(), m.group(1)) for m in FOLIO_RE.finditer(text)]
    result: dict[tuple[int, int], str] = {}
    prev = -1
    for offset, month, year in marks:
        folio = next(
            (val for off, val in reversed(folios) if prev < off < offset), None
        )
        result.setdefault((year, month), folio)
        prev = offset
    return result


def normalize_code(code: str) -> str:
    """'20' -> '020', '538' -> '538'. Non-numeric codes pass through."""
    return f"{int(code):03d}" if str(code).isdigit() else str(code)


def parse_amount(raw: str) -> int:
    """'4.425.564.773' -> 4425564773 (dots are thousands separators)."""
    return int(raw.replace(".", ""))


def extract_codes(markdown: str, codes=TARGET_CODES):
    """Return {code: [ {"raw": str, "value": int}, ... ]} for each code.

    An empty list means the code was not found in the document. Multiple hits
    can occur when a multi-page PDF holds several monthly forms.
    """
    results: dict[str, list[dict]] = {}
    for code in codes:
        norm = normalize_code(code)
        # Standalone code token (not a fragment inside a value: reject a leading
        # or trailing digit/dot) -> non-digit filler on same line -> value.
        pattern = re.compile(
            rf"(?<![\d.]){re.escape(norm)}(?![\d.])[^\d\n]*?({_VALUE})"
        )
        hits = []
        for m in pattern.finditer(markdown):
            raw = m.group(1)
            hits.append({"raw": raw, "value": parse_amount(raw)})
        results[norm] = hits
    return results


def first_value(block: str, code: str):
    """First value of `code` in `block`, or None if the code is absent."""
    hits = extract_codes(block, [code])[normalize_code(code)]
    return hits[0]["value"] if hits else None


def _period_marks(markdown: str) -> list[tuple[int, int, int]]:
    """(offset, month, year) for every PERIODO marker, in document order.

    Accepts both producer encodings (MM/YYYY and YYYYMM). The two patterns are
    mutually exclusive per line — the MM/YYYY form has a "/" the YYYYMM form
    lacks, and the YYYYMM form needs six contiguous digits the MM/YYYY form never
    has — so a marker is never counted twice.
    """
    marks = [
        (m.start(), int(m.group(1)), int(m.group(2)))
        for m in PERIOD_MMYYYY_RE.finditer(markdown)
    ]
    marks += [
        (m.start(), int(m.group(2)), int(m.group(1)))  # YYYYMM -> (month, year)
        for m in PERIOD_YYYYMM_RE.finditer(markdown)
    ]
    marks.sort()
    return marks


def split_by_period(markdown: str):
    """Slice the document into monthly blocks keyed by (year, month).

    Each block runs from one PERIODO marker to the next, so it holds all the
    codes for that month. Blocks sharing a period (e.g. a 2-page declaration
    that repeats PERIODO) are merged. Returns a list sorted by (year, month).
    """
    marks = _period_marks(markdown)
    blocks: dict[tuple[int, int], str] = {}
    for i, (start, month, year) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(markdown)
        blocks[(year, month)] = blocks.get((year, month), "") + markdown[start:end]
    return sorted(blocks.items())


def month_sales(values: dict) -> float:
    """Apply 020 + 142 + 538/0.19 + 587. Missing codes count as 0."""
    g = lambda c: values.get(c) or 0
    return g("020") + g("142") + g("538") / RATE + g("587")


def month_compras(values: dict) -> float:
    """Apply 535/0.19 + 520/0.19 - 528/0.19 + 532/0.19 + 521 + 560 + 562.
    Missing codes = 0."""
    g = lambda c: values.get(c) or 0
    return (
        g("535") / RATE
        + g("520") / RATE
        - g("528") / RATE
        + g("532") / RATE
        + g("521")
        + g("560")
        + g("562")
    )


def format_int(n) -> str:
    """4425564773 -> '4.425.564.773' (Chilean thousands separators)."""
    return f"{int(round(n)):,}".replace(",", ".")


def round_thousand(n):
    """Round a peso amount to the nearest thousand, expressed in thousands:
    1_807_028_373 -> 1_807_028.

    None (a missing code) passes through unchanged.
    """
    return None if n is None else int(round(n / 1000))


def row_in_thousands(row: dict):
    """Everything-in-thousands view of a monthly row.

    Codes are rounded to the nearest thousand *first*, then each formula is
    applied to those rounded values and its result rounded to the nearest
    thousand too. Missing codes stay None (the formulas count them as 0).

    Every report — the CLI Markdown table and each API view — presents figures
    in thousands, so this lives beside the formulas rather than in one caller,
    keeping a single definition of the rounding both layers show.

    Returns (codes_thousands: dict, sales_thousands: int, compras_thousands: int).
    """
    codes_k = {c: round_thousand(row["codes"][c]) for c in ALL_CODES}
    sales_k = int(round(month_sales(codes_k)))
    compras_k = int(round(month_compras(codes_k)))
    return codes_k, sales_k, compras_k


def variation_pct(prev, curr):
    """Numeric year-over-year variation: (prev / curr - 1) * 100.

    `prev` is the figure for the *same month of the previous year*. Returns None
    when undefined: no matching month a year earlier, or a current value of
    0/None that would divide by zero.
    """
    if prev is None or not curr:
        return None
    return (prev / curr - 1) * 100


def format_pct(pct) -> str:
    """Format a percentage with a Chilean decimal comma and an explicit sign,
    e.g. 12.3 -> "+12,3%", -4.0 -> "-4,0%". None (undefined) renders as "—"."""
    return "—" if pct is None else f"{pct:+.1f}%".replace(".", ",")


def format_variation(prev, curr) -> str:
    """Year-over-year variation as a percentage: (prev / curr - 1) * 100.

    Compares a month's figure with the same month of the previous year, per the
    reporting convention requested (previous divided by current, minus one).
    Returns "—" when undefined: no matching month a year earlier, or a current
    value of 0/None that would divide by zero. Formatted with a Chilean decimal
    comma and an explicit sign, e.g. "+12,3%", "-4,0%".
    """
    return format_pct(variation_pct(prev, curr))


def per_invoice(total, invoices):
    """Average per invoice: `total / invoices`, or None when undefined.

    `invoices` is the year's total sales-invoice count (code 503 summed across
    its months). Returns None when that count is 0 or None — there is nothing to
    average over — so the caller renders a dash instead of dividing by zero.
    Used to build the per-year *Promedio* row appended to each annual table.
    """
    return None if not invoices else total / invoices


def monthly_rows(markdown: str) -> list[dict]:
    """Structured monthly data for programmatic use (API/report).

    Returns one dict per month, sorted chronologically:
        {
            "year", "month", "month_name", "label",
            "folio": str | None,                # F29 declaration folio number
            "codes": {code: value|None, ...},   # raw peso amounts (ALL_CODES)
            "invoices": int | None,             # sales invoice count (code 503)
            "sales": float,                      # raw peso Venta del mes
            "compras": float,                    # raw peso Compras
            "missing": [formula codes counted as 0],
            "missing_compras": [compras codes counted as 0],
        }
    """
    rows = []
    folios = folios_by_period(markdown)
    for (year, month), block in split_by_period(markdown):
        values = {c: first_value(block, c) for c in ALL_CODES}
        rows.append(
            {
                "year": year,
                "month": month,
                "month_name": SPANISH_MONTHS.get(month, str(month)),
                "label": f"{SPANISH_MONTHS.get(month, month)} {year}",
                "folio": folios.get((year, month)),
                "codes": values,
                "invoices": first_value(block, INVOICE_CODE),
                "sales": month_sales(values),
                "compras": month_compras(values),
                "missing": [c for c in FORMULA_CODES if values[c] is None],
                "missing_compras": [c for c in COMPRAS_CODES if values[c] is None],
            }
        )
    return rows


def group_rows_by_year(rows: list[dict]) -> list[tuple[int, list[dict]]]:
    """Group monthly_rows() output into [(year, [row, ...]), ...] by year.

    Rows come out of monthly_rows() already sorted by (year, month), so a plain
    groupby yields each year once with its months in chronological order.
    """
    return [(year, list(g)) for year, g in groupby(rows, key=lambda r: r["year"])]


def fill_year_months(
    year: int, year_rows: list[dict], first_month: int = 1, last_month: int = 12
) -> list[dict]:
    """Return the year's months in order, padding the ones not declared.

    `year_rows` is one year's slice of monthly_rows(). A month with no F29
    declaration in the document becomes a placeholder carrying only its identity
    plus `blank: True`, so every year table spans Ene-Dic. Renderers print a dash
    in each value column of a blank month rather than a computed zero — an
    unfiled month must never read as a month that declared nothing.

    `first_month`/`last_month` narrow that span, for a report restricted to a
    period range: a range starting in Mar 2023 pads 2023 from Mar, not from Ene,
    so months outside the requested range are absent rather than shown as
    undeclared. They default to the full Ene-Dic year.
    """
    by_month = {r["month"]: r for r in year_rows}
    return [
        by_month.get(
            m,
            {
                "year": year,
                "month": m,
                "month_name": SPANISH_MONTHS[m],
                "label": f"{SPANISH_MONTHS[m]} {year}",
                "blank": True,
            },
        )
        for m in range(first_month, last_month + 1)
    ]


def build_monthly_table(markdown: str) -> str:
    """Build the Markdown report: one small table per year, its months as rows."""
    header = [
        "Período",
        "Folio",
        "Venta del mes",
        "Venta acumulada",
        "Var. Venta",
        "Facturas Emitidas",
        "Promedio de monto por factura",
        "Compras",
        "Compras acumulada",
        "Var. Compras",
    ]
    lines = [
        "# Ventas por mes (Formulario 29) — miles de pesos",
        "",
        "Todos los montos van en **miles de pesos**: cada código se redondea al "
        "millar antes de aplicar la fórmula y el resultado también.  ·  "
        "Venta del mes: `020 + 142 + 538 / 0,19 + 587`  ·  "
        "Compras: `535 / 0,19 + 520 / 0,19 - 528 / 0,19 + 532 / 0,19 + 521 + 560 + 562`  ·  "
        "*Facturas Emitidas* = cantidad de facturas del mes (código 503, un "
        "conteo — es lo único que no va en miles).  ·  "
        "*Promedio de monto por factura* = Venta del mes / cantidad de facturas emitidas "
        "(código 503) del mismo mes.  ·  "
        "Cada año muestra sus doce meses; los meses sin declaración en el "
        "documento van con — en todas sus columnas.  ·  "
        "Las columnas *acumuladas* suman mes a mes dentro de cada año.  ·  "
        "*Var.* = variación respecto al mismo mes del año anterior "
        "`(mismo mes año anterior / mes actual) - 1`.",
        "",
    ]

    incomplete = []
    all_rows = monthly_rows(markdown)
    # Same month, previous year -> figure, for year-over-year variation. Kept in
    # thousands like everything the table prints, so the percentage is computed
    # from exactly the figures shown.
    sales_by_period = {}
    compras_by_period = {}
    for r in all_rows:
        _, sales_k, compras_k = row_in_thousands(r)
        sales_by_period[(r["year"], r["month"])] = sales_k
        compras_by_period[(r["year"], r["month"])] = compras_k
    for year, year_rows in group_rows_by_year(all_rows):
        lines += [
            f"## {year}",
            "",
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * len(header)) + " |",
        ]
        acc_sales = acc_compras = 0
        tot_sales = tot_compras = 0
        tot_invoices = 0
        n_months = 0  # months actually declared, the divisor for monthly averages
        for r in fill_year_months(year, year_rows):
            # Month not declared in the document: dashes across the row, and it
            # contributes nothing to the accumulators or the yearly totals.
            if r.get("blank"):
                lines.append(
                    "| "
                    + " | ".join([r["month_name"]] + ["—"] * (len(header) - 1))
                    + " |"
                )
                continue
            _, sales_k, compras_k = row_in_thousands(r)
            acc_sales += sales_k
            acc_compras += compras_k
            prev_sales = sales_by_period.get((r["year"] - 1, r["month"]))
            prev_compras = compras_by_period.get((r["year"] - 1, r["month"]))
            var_sales = format_variation(prev_sales, sales_k)
            var_compras = format_variation(prev_compras, compras_k)
            sales = format_int(sales_k)
            compras = format_int(compras_k)
            if r["missing"]:
                sales += " *"
            if r["missing_compras"]:
                compras += " *"
            missing = r["missing"] + r["missing_compras"]
            if missing:
                incomplete.append(f"{r['label']} (faltan: {', '.join(missing)})")
            # Average value of one invoice this month: Venta del mes / código 503.
            avg_invoice = per_invoice(sales_k, r["invoices"])
            row = [
                r["month_name"],
                r["folio"] or "—",
                sales,
                format_int(acc_sales),
                var_sales,
                format_int(r["invoices"]) if r["invoices"] is not None else "—",
                format_int(avg_invoice) if avg_invoice is not None else "—",
                compras,
                format_int(acc_compras),
                var_compras,
            ]
            lines.append("| " + " | ".join(row) + " |")
            tot_sales += sales_k
            tot_compras += compras_k
            tot_invoices += r["invoices"] or 0
            n_months += 1
        # Promedio row: every cell is the mean of its own column. Venta del mes,
        # Facturas Emitidas and Compras average over the months actually declared
        # — not over twelve — so a year with two declarations reports the mean of
        # those two. Only the Promedio de monto por factura cell divides by the
        # invoice count instead, matching what that column measures. Acumuladas
        # and Var. have no meaningful mean ("—").
        avg = lambda total: total / n_months if n_months else None
        avg_sales = avg(tot_sales)
        avg_compras = avg(tot_compras)
        avg_invoices = avg(tot_invoices)
        avg_per_factura = per_invoice(tot_sales, tot_invoices)
        avg_row = [
            "**Promedio**",
            "—",
            format_int(avg_sales) if avg_sales is not None else "—",
            "—",
            "—",
            format_int(avg_invoices) if avg_invoices is not None else "—",
            format_int(avg_per_factura) if avg_per_factura is not None else "—",
            format_int(avg_compras) if avg_compras is not None else "—",
            "—",
            "—",
        ]
        lines.append("| " + " | ".join(avg_row) + " |")
        lines.append("")

    if incomplete:
        lines += ["\\* Mes con códigos faltantes (contados como 0):"]
        lines += [f"- {item}" for item in incomplete]
    return "\n".join(lines) + "\n"


# Words whose `top` differs by no more than this many points share one visual
# row. SII forms print rows well over 3pt apart, so this separates rows cleanly.
ROW_V_TOLERANCE_PT = 3.0


def _words_to_lines(words, tolerance=ROW_V_TOLERANCE_PT):
    """Rebuild visual text rows from pdfplumber's positioned words.

    Words are clustered by their `top` coordinate (within `tolerance`) into
    rows, each row sorted left→right by `x0`, then joined into newline-separated
    text. This makes extraction independent of the PDF's content-stream order.

    `htmldoc` carpetas already flow in reading order, but `Apache FOP 2.7`
    carpetas emit each table column as a separate block — every código, then
    every glosa, then every valor — so `page.extract_text()` returns them
    detached and the line-based regex finds nothing. Regrouping by coordinates
    puts each código back on the same line as its valor, and each PERIODO label
    on the same line as its date, which is exactly what the downstream regex
    pipeline expects — for both producers, one code path.
    """
    rows: list[list[dict]] = []
    current: list[dict] = []
    anchor_top = None
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if anchor_top is None or word["top"] - anchor_top <= tolerance:
            if anchor_top is None:
                anchor_top = word["top"]
            current.append(word)
        else:
            rows.append(current)
            current = [word]
            anchor_top = word["top"]
    if current:
        rows.append(current)
    return "\n".join(
        " ".join(str(w["text"]) for w in sorted(row, key=lambda w: w["x0"]))
        for row in rows
    )


def pdf_to_text(pdf_path: str) -> str:
    """Extract the PDF's text, rebuilding each page's rows from word coordinates.

    Rather than trust `page.extract_text()`'s reading order (which breaks on
    `Apache FOP` carpetas, where columns are emitted as detached blocks), we read
    each word's position and regroup them into true visual rows via
    `_words_to_lines`. One code path then handles both the linear `htmldoc` and
    the column-block `Apache FOP` variants: a código and its valor always end up
    on the same line. pdfplumber is pure-Python and imported lazily to keep the
    parsing/formatting helpers importable and testable without it.
    """
    import pdfplumber

    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(
                x_tolerance=3,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            pages.append(_words_to_lines(words))
    return "\n".join(pages)


def pdf_to_text_pypdf(pdf_path: str) -> str:
    """Faster pure-Python text extraction via pypdf (alternative to pdf_to_text).

    pypdf is typically several times faster than pdfplumber on these text-based
    SII PDFs. We pass extraction_mode="layout" so the physical line layout is
    preserved — the regex pipeline relies on a code and its value sitting on the
    same physical line, exactly what pdfplumber gives us by default.

    Like pdf_to_text(), the backend is imported lazily so the parsing/formatting
    helpers stay importable and testable without pypdf installed. Only the two
    pdf_to_text* functions know which PDF library is in use; the regex pipeline
    downstream is source-agnostic, so this is a drop-in replacement for text.
    """
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    return "\n".join(pages)


def main(argv=None):
    argv = argv or sys.argv[1:]
    # Load .env BEFORE reading PDF_FILE, otherwise the var isn't set yet.
    from dotenv import load_dotenv

    from paths import ENV_FILE, OUTPUTS_DIR, from_root

    load_dotenv(ENV_FILE)
    # Path precedence: CLI arg -> PDF_FILE env var (.env).
    pdf_path = argv[0] if argv else os.getenv("PDF_FILE")
    if not pdf_path:
        sys.exit("Usage: python extract_codes.py <file.pdf>  (or set PDF_FILE)")

    # Both sources may be relative ("docs/CARPETA ....pdf"); anchor them on the
    # repo root so the command works from anywhere, not only from apps/api.
    text = pdf_to_text(from_root(pdf_path))

    OUTPUTS_DIR.mkdir(exist_ok=True)
    # Keep the intermediate extracted text for inspection/debugging.
    (OUTPUTS_DIR / "output.md").write_text(text, encoding="utf-8")

    table = build_monthly_table(text)
    report = OUTPUTS_DIR / "ventas_por_mes.md"
    report.write_text(table, encoding="utf-8")
    print(table)
    print(f"-> {report}")


if __name__ == "__main__":
    main()
