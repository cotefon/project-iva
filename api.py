"""FastAPI service for the F29 / IVA sales extractor.

Upload a Chilean tax PDF (Carpeta Tributaria) and get back a per-month table of
the Formulario 29 sales figures. All amounts are rounded to the nearest thousand
pesos: e.g. 1.000.000 is reported as 1.000.

Endpoints:
    GET  /            HTML upload form + results (browser-friendly).
    POST /extract     JSON: {"unit": "miles de pesos", "months": [...]}.
    POST /extract.md  Markdown table (text/markdown), values in thousands.

The extraction logic lives in `extract_codes.py`; this module only handles the
HTTP layer, temp-file plumbing, and the round-to-thousand presentation.
"""

from __future__ import annotations

import html
import logging
import os
import tempfile
from typing import NamedTuple

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from extract_codes import (
    ALL_CODES,
    COMPRAS_CODES,
    FORMULA_CODES,
    SPANISH_MONTHS,
    extract_taxpayer,
    format_variation,
    group_rows_by_year,
    month_compras,
    month_sales,
    monthly_rows,
    pdf_to_text,
    pdf_to_text_pypdf,
    per_invoice,
)
import storage

log = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="IVA / F29 Sales Extractor",
    description="Extract monthly Formulario 29 sales from a Chilean tax PDF.",
    version="1.0.0",
)


def round_thousand(n):
    """Round a peso amount to the nearest thousand, expressed in thousands:
    1_807_028_373 -> 1_807_028.

    None (a missing code) passes through unchanged.
    """
    return None if n is None else int(round(n / 1000))


def row_in_thousands(row: dict):
    """Everything-in-thousands view of a monthly row.

    Codes are rounded to the nearest thousand *first*, then each sales formula is
    applied to those rounded values and its result rounded to the nearest
    thousand too. Missing codes stay None (the formulas count them as 0).

    Returns (codes_thousands: dict, sales_thousands: int, compras_thousands: int).
    """
    codes_k = {c: round_thousand(row["codes"][c]) for c in ALL_CODES}
    sales_k = int(round(month_sales(codes_k)))
    compras_k = int(round(month_compras(codes_k)))
    return codes_k, sales_k, compras_k


def fmt(v) -> str:
    """Format an integer with comma thousands separators (e.g. 1,872,854).

    None (a missing code) renders as an em dash.
    """
    return "—" if v is None else f"{v:,}"


class _ColumnAverages(NamedTuple):
    """Per-invoice averages for a year's *Promedio* row (thousands of pesos).

    Each field is the year's total divided by its sales-invoice count (code 503),
    rounded to the nearest thousand, or None when the year has no invoice count
    to divide by. Only Venta del mes and Compras carry a per-invoice value; the
    other columns have no per-invoice meaning and render as a dash.
    """

    sales: int | None
    compras: int | None


class _YearAggregator:
    """Accumulates a year's per-month totals to build its Promedio row.

    The Promedio row reports the year's Venta del mes and Compras totals divided
    by the number of sales invoices issued that year (code 503 summed across its
    months) — an average value per invoice, expressed in thousands of pesos.
    """

    def __init__(self) -> None:
        self.tot_sales = 0
        self.tot_compras = 0
        self.tot_invoices = 0

    def add(self, sales_k, compras_k, invoices):
        self.tot_sales += sales_k
        self.tot_compras += compras_k
        self.tot_invoices += invoices or 0

    def averages(self) -> _ColumnAverages:
        def avg(total):
            v = per_invoice(total, self.tot_invoices)
            return None if v is None else round(v)

        return _ColumnAverages(
            sales=avg(self.tot_sales), compras=avg(self.tot_compras)
        )


def pdf_to_rows(
    data: bytes, filename: str, extract=pdf_to_text
) -> tuple[str, list[dict]]:
    """Convert uploaded PDF bytes to (extracted text, structured monthly rows).

    The raw text is returned too so callers can pull header data (taxpayer name
    / RUT) without re-reading the file. The `extract` callable turns a PDF path
    into text (defaults to pdfplumber's `pdf_to_text`; pass `pdf_to_text_pypdf`
    for the faster backend). Both need a real path, so we stage the upload in a
    temp file that is always cleaned up. Rejects non-PDF uploads and empty
    results early.
    """
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a .pdf file.")
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        text = extract(tmp_path)
    except HTTPException:
        raise
    except Exception as exc:  # pdfplumber / parsing failure
        raise HTTPException(status_code=422, detail=f"Could not read PDF: {exc}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    rows = monthly_rows(text)
    if not rows:
        raise HTTPException(
            status_code=422,
            detail="No monthly declarations (PERIODO) found in the document.",
        )

    # Persist the extraction (best-effort): a storage failure must never break
    # the response. We log it rather than swallow it silently, so a broken DB
    # write is visible in the server console. Extraction is the product; the DB
    # is a record.
    try:
        doc_id = storage.save_extraction(
            extract_taxpayer(text), rows, source_file=filename
        )
        log.info("Persisted extraction as document %s (%s rows)", doc_id, len(rows))
    except Exception:
        log.exception("Failed to persist extraction to Supabase")

    return text, rows


@app.get("/ruts")
async def list_ruts():
    """Return every RUT that has stored extractions, each with its name.

    503 if persistence isn't configured (SUPABASE_URL / SUPABASE_KEY missing).
    """
    try:
        taxpayers = storage.list_taxpayers()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "count": len(taxpayers),
        "ruts": [t["rut"] for t in taxpayers],
        "taxpayers": taxpayers,
    }


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    """Return the monthly sales as JSON, all amounts in thousands of pesos."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "")

    # (prev / curr - 1) * 100, or None when undefined (no previous month, or a
    # current value of 0). Compared month-over-month across years.
    def var_pct(prev, curr):
        return None if prev is None or not curr else round((prev / curr - 1) * 100, 1)

    # Accumulated Venta del mes / Compras reset each year, matching the per-year
    # tables the other endpoints render; the variation carries across years.
    months = []
    prev_sales = prev_compras = None
    for _year, year_rows in group_rows_by_year(rows):
        acc_sales = acc_compras = 0
        for r in year_rows:
            codes_k, sales_k, compras_k = row_in_thousands(r)
            acc_sales += sales_k
            acc_compras += compras_k
            months.append({
                "periodo": r["label"],
                "year": r["year"],
                "month": r["month"],
                "folio": r.get("folio"),
                "invoices": r.get("invoices"),
                "codes": codes_k,
                "venta_del_mes": sales_k,
                "venta_acumulada": acc_sales,
                "venta_variacion_pct": var_pct(prev_sales, sales_k),
                "compras": compras_k,
                "compras_acumulada": acc_compras,
                "compras_variacion_pct": var_pct(prev_compras, compras_k),
                "missing": r["missing"],
                "missing_compras": r["missing_compras"],
            })
            prev_sales, prev_compras = sales_k, compras_k

    return {
        "unit": "miles de pesos (codes rounded to nearest thousand before the "
        "formula; result rounded to nearest thousand)",
        "formula": "020 + 142 + 538 / 0,19 + 587",
        "formula_compras": "535 / 0,19 + 520 / 0,19 - 528 / 0,19 + 532 / 0,19 + 521 + 560 + 562",
        "codes": FORMULA_CODES,
        "codes_compras": COMPRAS_CODES,
        "acumulado": "Venta/Compras acumuladas: suma mes a mes dentro de cada año.",
        "variacion": "*_variacion_pct: (mes anterior / mes actual) - 1, en porcentaje.",
        "months": months,
    }


def _render_table(rows: list[dict], sep: str) -> list[str]:
    """Shared Markdown lines: one small table per year, its months as rows."""
    header = [
        "Período",
        "Folio",
        "Venta del mes",
        "Venta acumulada",
        "Var. Venta",
        "Compras",
        "Compras acumulada",
        "Var. Compras",
    ]
    lines: list[str] = []
    prev_sales = prev_compras = None
    for year, year_rows in group_rows_by_year(rows):
        lines += [
            f"## {year}",
            "",
            "| " + sep.join(header) + " |",
            "| " + sep.join(["---"] * len(header)) + " |",
        ]
        acc_sales = acc_compras = 0
        agg = _YearAggregator()
        for r in year_rows:
            _, sales_k, compras_k = row_in_thousands(r)
            acc_sales += sales_k
            acc_compras += compras_k
            var_sales = format_variation(prev_sales, sales_k)
            var_compras = format_variation(prev_compras, compras_k)
            sales = fmt(sales_k) + (" *" if r["missing"] else "")
            compras = fmt(compras_k) + (" *" if r["missing_compras"] else "")
            row = [
                r["month_name"],
                r.get("folio") or "—",
                sales,
                fmt(acc_sales),
                var_sales,
                compras,
                fmt(acc_compras),
                var_compras,
            ]
            lines.append("| " + sep.join(row) + " |")
            agg.add(sales_k, compras_k, r.get("invoices"))
            prev_sales, prev_compras = sales_k, compras_k
        a = agg.averages()
        avg_row = [
            "**Promedio**", "—",
            fmt(a.sales), "—", "—",
            fmt(a.compras), "—", "—",
        ]
        lines.append("| " + sep.join(avg_row) + " |")
        lines.append("")
    return lines


@app.post("/extract.md", response_class=PlainTextResponse)
async def extract_markdown(file: UploadFile = File(...)):
    """Return the monthly sales as a Markdown table (values in thousands)."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "")
    body = [
        "# Ventas por mes (Formulario 29) — miles de pesos",
        "",
        "Venta del mes: `020 + 142 + 538 / 0,19 + 587`  ·  "
        "Compras: `535 / 0,19 + 520 / 0,19 - 528 / 0,19 + 532 / 0,19 + 521 + 560 + 562`  ·  "
        "Todos los montos en **miles de pesos**: los códigos se redondean al "
        "millar antes de aplicar cada fórmula y los totales también se redondean "
        "al millar.",
        "",
        *_render_table(rows, " | "),
    ]
    incomplete = [
        f"- {r['label']} (faltan: "
        f"{', '.join(sorted(set(r['missing'] + r['missing_compras'])))})"
        for r in rows
        if r["missing"] or r["missing_compras"]
    ]
    if incomplete:
        body += ["", "\\* Mes con códigos faltantes (contados como 0):", *incomplete]
    return PlainTextResponse("\n".join(body) + "\n", media_type="text/markdown")


def build_pdf(rows: list[dict], taxpayer: dict | None = None) -> bytes:
    """Render the monthly table as a PDF, one small table per year: Período,
    Folio, Venta del mes and Compras totals (values in thousands).

    Kept on landscape A4 (the four columns fit comfortably); the taxpayer header
    block sits above the tables.

    `taxpayer` is the {"nombre", "rut"} dict from extract_taxpayer(); its fields
    are printed as a header block above the table when present.

    fpdf2 is pure-Python and imported lazily to keep the module importable
    without it. Core (Helvetica) fonts are Latin-1, which covers every glyph we
    emit here (accents like "í"); we avoid non-Latin-1 characters such as em
    dashes on purpose.
    """
    from fpdf import FPDF

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_title("Ventas por mes (Formulario 29)")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 10, "Ventas por mes (Formulario 29)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(
        0,
        6,
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # Taxpayer header: label in bold, value in regular, one per line.
    taxpayer = taxpayer or {}
    fields = [
        ("Nombre / Razón Social", taxpayer.get("nombre")),
        ("RUT", taxpayer.get("rut")),
    ]
    for label, value in fields:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(42, 6, f"{label}:", new_x="RIGHT", new_y="TOP")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(
            0, 6, value if value else "No disponible", new_x="LMARGIN", new_y="NEXT"
        )
    pdf.ln(3)

    # Column layout as (header, width_mm, align), driving both the header row
    # and the data rows so they can never drift out of sync. Widths sum to
    # 266mm, within landscape A4's ~277mm usable width.
    columns = [
        ("Período", 28, "L"),
        ("Folio", 34, "R"),
        ("Venta del mes", 40, "R"),
        ("Venta acumulada", 40, "R"),
        ("Var. Venta", 22, "R"),
        ("Compras", 40, "R"),
        ("Compras acumulada", 40, "R"),
        ("Var. Compras", 22, "R"),
    ]

    red = (198, 40, 40)  # matches the HTML deficit colour (#c62828)

    def emit_row(cells, height, deficit=False):
        """Draw one bordered row from `cells`, keeping every cell on one line.

        When `deficit` is set, the value columns (index >= 2: Venta/Compras and
        their derived columns) are printed in red; Período and Folio stay black.
        """
        for i, ((_, w, align), text) in enumerate(zip(columns, cells)):
            last = i == len(columns) - 1
            pdf.set_text_color(*(red if deficit and i >= 2 else (0, 0, 0)))
            pdf.cell(
                w,
                height,
                text,
                border=1,
                align=align,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP",
            )
        pdf.set_text_color(0, 0, 0)

    def emit_header():
        """Draw the shaded column-header row of a year's table."""
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(244, 244, 245)
        for i, (label, w, align) in enumerate(columns):
            last = i == len(columns) - 1
            pdf.cell(
                w,
                9,
                label,
                border=1,
                align=align,
                fill=True,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP",
            )

    # Core Helvetica is Latin-1, which excludes the em dash fmt()/format_variation
    # use for a missing value, so render numeric cells with a plain hyphen.
    def money(v):
        return "-" if v is None else f"{v:,}"

    def pct(prev, curr):
        return format_variation(prev, curr).replace("—", "-")

    has_incomplete = False
    prev_sales = prev_compras = None
    for year, year_rows in group_rows_by_year(rows):
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, str(year), new_x="LMARGIN", new_y="NEXT")
        emit_header()
        pdf.set_font("Helvetica", "", 8)
        acc_sales = acc_compras = 0
        agg = _YearAggregator()
        for r in year_rows:
            _, sales_k, compras_k = row_in_thousands(r)
            acc_sales += sales_k
            acc_compras += compras_k
            var_sales = pct(prev_sales, sales_k)
            var_compras = pct(prev_compras, compras_k)
            ventas = money(sales_k) + (" *" if r["missing"] else "")
            compras = money(compras_k) + (" *" if r["missing_compras"] else "")
            if r["missing"] or r["missing_compras"]:
                has_incomplete = True
            cells = [
                r["month_name"],
                r.get("folio") or "-",
                ventas,
                money(acc_sales),
                var_sales,
                compras,
                money(acc_compras),
                var_compras,
            ]
            # Red row when this month's accumulated Compras pass accumulated Venta.
            emit_row(cells, 7, acc_compras > acc_sales)
            agg.add(sales_k, compras_k, r.get("invoices"))
            prev_sales, prev_compras = sales_k, compras_k
        # Per-year Promedio row: total per invoice (code 503) for Venta del mes
        # and Compras; the other columns have no per-invoice meaning ("-").
        a = agg.averages()
        pdf.set_font("Helvetica", "B", 8)
        emit_row(
            [
                "Promedio",
                "-",
                money(a.sales),
                "-",
                "-",
                money(a.compras),
                "-",
                "-",
            ],
            7,
        )
        pdf.set_font("Helvetica", "", 8)
        pdf.ln(4)

    if has_incomplete:
        pdf.ln(3)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(
            sum(w for _, w, _ in columns),
            5,
            "* Mes con codigos faltantes (contados como 0).",
        )

    return bytes(pdf.output())


@app.post("/extract.pdf")
async def extract_pdf(file: UploadFile = File(...)):
    """Return a PDF with the full monthly table (all codes + totals, thousands)."""
    text, rows = pdf_to_rows(await file.read(), file.filename or "")
    taxpayer = extract_taxpayer(text)
    return Response(
        content=build_pdf(rows, taxpayer),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="ventas_por_mes.pdf"'},
    )


@app.post("/extract-fast.pdf")
async def extract_pdf_fast(file: UploadFile = File(...)):
    """Same PDF as /extract.pdf but read with the faster pypdf backend.

    Identical output; only the source-PDF text extraction differs (pypdf instead
    of pdfplumber), which is where nearly all the request time is spent. Use this
    to compare speed and confirm the parsed figures match /extract.pdf on real
    documents before switching the default backend.
    """
    text, rows = pdf_to_rows(
        await file.read(), file.filename or "", extract=pdf_to_text_pypdf
    )
    taxpayer = extract_taxpayer(text)
    return Response(
        content=build_pdf(rows, taxpayer),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="ventas_por_mes.pdf"'},
    )


UPLOAD_FORM = """
<form action="/" method="post" enctype="multipart/form-data">
  <input type="file" name="file" accept="application/pdf" required>
  <button type="submit">Extraer ventas</button>
</form>
"""

PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Extractor IVA / F29</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 60rem; margin: 2rem auto;
         padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin: 1.5rem 0 .5rem; }}
  form {{ margin: 1rem 0 2rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem;
          font-variant-numeric: tabular-nums; }}
  th, td {{ border: 1px solid #ddd; padding: .35rem .6rem; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  thead th {{ background: #f4f4f5; }}
  tr.avg td {{ background: #fafafa; font-weight: 600; border-top: 2px solid #ccc; }}
  td.num {{ font-feature-settings: "tnum"; }}
  /* Month whose accumulated Compras exceed accumulated Venta: numbers in red. */
  tr.deficit td.num {{ color: #c62828; }}
  .note {{ color: #666; font-size: .85rem; }}
</style></head><body>
<h1>Extractor de ventas IVA / Formulario 29</h1>
<p class="note">Sube una carpeta tributaria en PDF. Todos los montos se muestran
en <strong>miles de pesos</strong>: los códigos se redondean al millar antes
de aplicar la fórmula, y la Venta del mes también se redondea al millar.</p>
{form}
{results}
</body></html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    """Upload form."""
    return PAGE.format(form=UPLOAD_FORM, results="")


def _html_table(rows: list[dict]) -> str:
    """Render monthly rows as an HTML table (values in thousands).

    Shared by the upload view and the history view so both look identical. Rows
    must carry `label`, `codes`, `sales`, `missing` (monthly_rows() shape).
    """
    columns = [
        "Período",
        "Folio",
        "Venta del mes",
        "Venta acumulada",
        "Var. Venta",
        "Compras",
        "Compras acumulada",
        "Var. Compras",
    ]
    head = (
        "<thead><tr>"
        + "".join(f"<th>{h}</th>" for h in columns)
        + "</tr></thead>"
    )
    sections = ""
    prev_sales = prev_compras = None
    for year, year_rows in group_rows_by_year(rows):
        body = ""
        acc_sales = acc_compras = 0
        agg = _YearAggregator()
        for r in year_rows:
            _, sales_k, compras_k = row_in_thousands(r)
            acc_sales += sales_k
            acc_compras += compras_k
            var_sales = format_variation(prev_sales, sales_k)
            var_compras = format_variation(prev_compras, compras_k)
            sales = fmt(sales_k) + (" *" if r["missing"] else "")
            compras = fmt(compras_k) + (" *" if r["missing_compras"] else "")
            # Red row when this month's accumulated Compras pass accumulated Venta.
            tr = '<tr class="deficit">' if acc_compras > acc_sales else "<tr>"
            body += (
                f'{tr}<td>{html.escape(r["month_name"])}</td>'
                f'<td class="num">{html.escape(r.get("folio") or "—")}</td>'
                f'<td class="num">{sales}</td>'
                f'<td class="num">{fmt(acc_sales)}</td>'
                f'<td class="num">{html.escape(var_sales)}</td>'
                f'<td class="num">{compras}</td>'
                f'<td class="num">{fmt(acc_compras)}</td>'
                f'<td class="num">{html.escape(var_compras)}</td></tr>'
            )
            agg.add(sales_k, compras_k, r.get("invoices"))
            prev_sales, prev_compras = sales_k, compras_k
        a = agg.averages()
        body += (
            '<tr class="avg"><td>Promedio</td>'
            '<td class="num">—</td>'
            f'<td class="num">{fmt(a.sales)}</td>'
            '<td class="num">—</td>'
            '<td class="num">—</td>'
            f'<td class="num">{fmt(a.compras)}</td>'
            '<td class="num">—</td>'
            '<td class="num">—</td></tr>'
        )
        sections += (
            f"<h2>{year}</h2><table>{head}<tbody>{body}</tbody></table>"
        )
    return sections


_FORMULA_NOTE = (
    '<p class="note">Venta del mes: <code>020 + 142 + 538 / 0,19 + 587</code>. '
    "Compras: "
    "<code>535 / 0,19 + 520 / 0,19 - 528 / 0,19 + 532 / 0,19 + 521 + 560 + 562</code>. "
    "Montos en miles de pesos (códigos redondeados al millar antes de cada "
    "fórmula). * = mes con códigos faltantes (contados como 0).</p>"
)


@app.post("/", response_class=HTMLResponse)
async def home_submit(file: UploadFile = File(...)):
    """Handle the browser form upload and render the results as an HTML table."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "")
    return PAGE.format(form=UPLOAD_FORM, results=_FORMULA_NOTE + _html_table(rows))


def _stored_rows(doc: dict) -> list[dict]:
    """Adapt a stored document's rows to the monthly_rows() render shape.

    latest_document() returns bare {year, month, codes, sales}; the renderer also
    needs `label` and `missing`, which we recompute (labels aren't persisted).
    """
    rows = []
    for r in doc["rows"]:
        month, year = r["month"], r["year"]
        rows.append(
            {
                "year": year,
                "month": month,
                "month_name": SPANISH_MONTHS.get(month, str(month)),
                "label": f"{SPANISH_MONTHS.get(month, month)} {year}",
                "folio": r.get("folio"),
                "invoices": r.get("invoices"),
                "codes": r["codes"],
                "sales": r["sales"],
                "compras": r["compras"],
                "missing": [c for c in FORMULA_CODES if r["codes"][c] is None],
                "missing_compras": [
                    c for c in COMPRAS_CODES if r["codes"][c] is None
                ],
            }
        )
    return rows


@app.get("/history/{rut}", response_class=HTMLResponse)
async def history(rut: str):
    """Render the most recent stored extraction for a RUT as an HTML table.

    404 if nothing is stored for that RUT; 503 if persistence isn't configured
    (SUPABASE_URL / SUPABASE_KEY missing), so the reason is explicit.
    """
    try:
        doc = storage.latest_document(rut)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if doc is None:
        raise HTTPException(
            status_code=404, detail=f"Sin extracciones guardadas para el RUT {rut}."
        )

    header = (
        '<p class="note">Última extracción guardada · <strong>'
        + html.escape(doc["nombre"] or "—")
        + "</strong> (RUT "
        + html.escape(doc["rut"])
        + ") · archivo: "
        + html.escape(doc["source_file"] or "—")
        + " · "
        + html.escape(str(doc["extracted_at"]))
        + "</p>"
    )
    return PAGE.format(
        form=UPLOAD_FORM,
        results=header + _FORMULA_NOTE + _html_table(_stored_rows(doc)),
    )
