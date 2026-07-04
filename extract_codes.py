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

# Codes used by the sales formula, in the order shown in the table.
FORMULA_CODES = ["020", "142", "538", "587"]
TARGET_CODES = FORMULA_CODES

# VAT rate applied to code 538 (TOTAL DÉBITOS) to recover the net sales base.
RATE = 0.19

SPANISH_MONTHS = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre",
    12: "Diciembre",
}

# A Chilean-formatted amount: 1-3 leading digits, then dot-grouped thousands.
# Matches "0", "331", "17.738.025", "4.425.564.773".
_VALUE = r"\d{1,3}(?:\.\d{3})*"

# PERIODO field of a declaration, e.g. "PERIODO | 15 | 09 / 2024" -> (09, 2024).
# .*? skips the field's box number and lands on the first "MM / YYYY".
PERIOD_RE = re.compile(r"PERIODO.*?(\d{1,2})\s*/\s*(\d{4})")

# Taxpayer identity, from the carpeta header ("Nombre del emisor: ...",
# "RUT del emisor: 79527050 − 7"). The value runs to the end of the line.
EMISOR_NAME_RE = re.compile(r"Nombre del emisor:\s*(.+)")
# RUT body then verifier digit, tolerant of any dash (hyphen, U+2212, en/em dash)
# and surrounding spaces: "79527050 − 7" or "79.527.050-7".
EMISOR_RUT_RE = re.compile(r"RUT del emisor:\s*([\d.]+)\s*[-−–—]\s*([\dkK])")


def format_rut(body: str, dv: str) -> str:
    """('79527050', '7') -> '79.527.050-7' (Chilean dots + verifier digit)."""
    digits = re.sub(r"\D", "", body)
    grouped = f"{int(digits):,}".replace(",", ".")
    return f"{grouped}-{dv.upper()}"


def extract_taxpayer(text: str) -> dict:
    """Pull the taxpayer's name and RUT from the carpeta header.

    Returns {"nombre": str | None, "rut": str | None}; a field is None when its
    label is absent from the document.
    """
    name_m = EMISOR_NAME_RE.search(text)
    rut_m = EMISOR_RUT_RE.search(text)
    return {
        "nombre": name_m.group(1).strip() if name_m else None,
        "rut": format_rut(rut_m.group(1), rut_m.group(2)) if rut_m else None,
    }


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


def split_by_period(markdown: str):
    """Slice the document into monthly blocks keyed by (year, month).

    Each block runs from one PERIODO marker to the next, so it holds all the
    codes for that month. Blocks sharing a period (e.g. a 2-page declaration
    that repeats PERIODO) are merged. Returns a list sorted by (year, month).
    """
    marks = [
        (m.start(), int(m.group(1)), int(m.group(2)))
        for m in PERIOD_RE.finditer(markdown)
    ]
    blocks: dict[tuple[int, int], str] = {}
    for i, (start, month, year) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(markdown)
        blocks[(year, month)] = blocks.get((year, month), "") + markdown[start:end]
    return sorted(blocks.items())


def month_sales(values: dict) -> float:
    """Apply 020 + 142 + 538/0.19 + 587. Missing codes count as 0."""
    g = lambda c: values.get(c) or 0
    return g("020") + g("142") + g("538") / RATE + g("587")


def format_int(n) -> str:
    """4425564773 -> '4.425.564.773' (Chilean thousands separators)."""
    return f"{int(round(n)):,}".replace(",", ".")


def monthly_rows(markdown: str) -> list[dict]:
    """Structured monthly data for programmatic use (API/report).

    Returns one dict per month, sorted chronologically:
        {
            "year", "month", "month_name", "label",
            "codes": {code: value|None, ...},   # raw peso amounts
            "sales": float,                      # raw peso sales
            "missing": [codes counted as 0],
        }
    """
    rows = []
    for (year, month), block in split_by_period(markdown):
        values = {c: first_value(block, c) for c in FORMULA_CODES}
        rows.append({
            "year": year,
            "month": month,
            "month_name": SPANISH_MONTHS.get(month, str(month)),
            "label": f"{SPANISH_MONTHS.get(month, month)} {year}",
            "codes": values,
            "sales": month_sales(values),
            "missing": [c for c in FORMULA_CODES if values[c] is None],
        })
    return rows


def build_monthly_table(markdown: str) -> str:
    """Build the Markdown report: one row per month with the sales formula."""
    header = ["Período"] + FORMULA_CODES + ["Venta del mes"]
    lines = [
        "# Ventas por mes (Formulario 29)",
        "",
        "Fórmula: `020 + 142 + 538 / 0,19 + 587`",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]

    incomplete = []
    for (year, month), block in split_by_period(markdown):
        values = {c: first_value(block, c) for c in FORMULA_CODES}
        label = f"{SPANISH_MONTHS.get(month, month)} {year}"
        cells = [
            format_int(values[c]) if values[c] is not None else "—"
            for c in FORMULA_CODES
        ]
        sales = format_int(month_sales(values))
        missing = [c for c in FORMULA_CODES if values[c] is None]
        if missing:
            sales += " *"
            incomplete.append(f"{label} (faltan: {', '.join(missing)})")
        lines.append("| " + " | ".join([label] + cells + [sales]) + " |")

    if incomplete:
        lines += ["", "\\* Mes con códigos faltantes (contados como 0):"]
        lines += [f"- {item}" for item in incomplete]
    return "\n".join(lines) + "\n"


def pdf_to_text(pdf_path: str) -> str:
    """Extract the PDF's text via pdfplumber, one page after another.

    pdfplumber preserves the physical line layout of these SII forms, which is
    exactly what the regex pipeline relies on (a code and its value sit on the
    same line). pdfplumber is pure-Python, so it installs cleanly everywhere and
    is imported lazily to keep the parsing/formatting helpers importable and
    testable without it.
    """
    import pdfplumber

    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def main(argv=None):
    argv = argv or sys.argv[1:]
    # Load .env BEFORE reading PDF_FILE, otherwise the var isn't set yet.
    from dotenv import load_dotenv

    load_dotenv()
    # Path precedence: CLI arg -> PDF_FILE env var (.env).
    pdf_path = argv[0] if argv else os.getenv("PDF_FILE")
    if not pdf_path:
        sys.exit("Usage: python extract_codes.py <file.pdf>  (or set PDF_FILE)")

    text = pdf_to_text(pdf_path)
    # Keep the intermediate extracted text for inspection/debugging.
    with open("output.md", "w", encoding="utf-8") as fh:
        fh.write(text)

    table = build_monthly_table(text)
    with open("ventas_por_mes.md", "w", encoding="utf-8") as fh:
        fh.write(table)
    print(table)
    print("-> ventas_por_mes.md")


if __name__ == "__main__":
    main()
