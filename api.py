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
    COMPRAS_CODES,
    FORMULA_CODES,
    SPANISH_MONTHS,
    extract_taxpayer,
    fill_year_months,
    format_variation,
    group_rows_by_year,
    monthly_rows,
    pdf_to_text,
    pdf_to_text_pypdf,
    per_invoice,
    row_in_thousands,
)
import storage

log = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="IVA / F29 Sales Extractor",
    description="Extract monthly Formulario 29 sales from a Chilean tax PDF.",
    version="1.0.0",
)


def thousands_by_period(rows: list[dict]) -> dict:
    """Map {(year, month): (sales_k, compras_k)} in thousands, for looking up the
    same month of the previous year when computing year-over-year variation."""
    lookup = {}
    for r in rows:
        _, sales_k, compras_k = row_in_thousands(r)
        lookup[(r["year"], r["month"])] = (sales_k, compras_k)
    return lookup


def prior_year(lookup: dict, row: dict):
    """(sales_k, compras_k) for the same month one year earlier, or (None, None)."""
    return lookup.get((row["year"] - 1, row["month"]), (None, None))


def fmt(v) -> str:
    """Format an integer with Chilean dot thousands separators (e.g. 1.872.854).

    None (a missing code) renders as an em dash.
    """
    return "—" if v is None else f"{v:,}".replace(",", ".")


def avg_invoice(sales_k, invoices):
    """Average value of one invoice for a month, in thousands of pesos.

    `sales_k` is the month's Venta del mes already in thousands and `invoices`
    its sales-invoice count (code 503). Returns None — rendered as a dash — when
    the month has no invoice count to divide by, so a declaration without code
    503 never divides by zero. Feeds the *Promedio de monto por factura* column.
    """
    v = per_invoice(sales_k, invoices)
    return None if v is None else round(v)


class _ColumnAverages(NamedTuple):
    """The figures a year's *Promedio* row prints — each the mean of its column.

    `sales` and `compras` are the year's totals over the months actually
    declared, in thousands of pesos; `invoices` is the mean number of facturas
    per declared month, a plain count. `per_factura` is the odd one out: the
    year's whole Venta divided by its whole sales-invoice count (code 503),
    which is what the *Promedio de monto por factura* column measures. Each is
    None when its divisor is 0, so the cell renders as a dash rather than
    dividing by zero. The remaining columns have no meaningful mean.
    """

    sales: int | None
    compras: int | None
    invoices: int | None
    per_factura: int | None


class _YearAggregator:
    """Accumulates a year's per-month totals to build its Promedio row.

    Feed it one `add()` per *declared* month — blank padding months must be
    skipped, since they are what the monthly means divide by. `averages()` turns
    the accumulated totals into the row's cells.
    """

    def __init__(self) -> None:
        self.tot_sales = 0
        self.tot_compras = 0
        self.tot_invoices = 0
        self.months = 0  # months actually declared, the divisor for monthly means

    def add(self, sales_k, compras_k, invoices):
        self.tot_sales += sales_k
        self.tot_compras += compras_k
        self.tot_invoices += invoices or 0
        self.months += 1

    def averages(self) -> _ColumnAverages:
        # Monthly mean: divide by the months actually declared, not by twelve, so
        # a year with two declarations reports the mean of those two.
        def per_month(total):
            return round(total / self.months) if self.months else None

        v = per_invoice(self.tot_sales, self.tot_invoices)
        return _ColumnAverages(
            sales=per_month(self.tot_sales),
            compras=per_month(self.tot_compras),
            invoices=per_month(self.tot_invoices),
            per_factura=None if v is None else round(v),
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

    # (prev / curr - 1) * 100, or None when undefined (no matching month a year
    # earlier, or a current value of 0). Compared against the same month of the
    # previous year.
    def var_pct(prev, curr):
        return None if prev is None or not curr else round((prev / curr - 1) * 100, 1)

    # Accumulated Venta del mes / Compras reset each year, matching the per-year
    # tables the other endpoints render.
    lookup = thousands_by_period(rows)
    months = []
    for _year, year_rows in group_rows_by_year(rows):
        acc_sales = acc_compras = 0
        for r in year_rows:
            codes_k, sales_k, compras_k = row_in_thousands(r)
            prev_sales, prev_compras = prior_year(lookup, r)
            acc_sales += sales_k
            acc_compras += compras_k
            months.append(
                {
                    "Mes": r["label"],
                    "year": r["year"],
                    "month": r["month"],
                    "folio": r.get("folio"),
                    "invoices": r.get("invoices"),
                    "codes": codes_k,
                    "venta_del_mes": sales_k,
                    "promedio_facturas": avg_invoice(sales_k, r.get("invoices")),
                    "venta_acumulada": acc_sales,
                    "venta_variacion_pct": var_pct(prev_sales, sales_k),
                    "compras": compras_k,
                    "compras_acumulada": acc_compras,
                    "compras_variacion_pct": var_pct(prev_compras, compras_k),
                    "missing": r["missing"],
                    "missing_compras": r["missing_compras"],
                }
            )

    return {
        "unit": "miles de pesos (codes rounded to nearest thousand before the "
        "formula; result rounded to nearest thousand)",
        "formula": "020 + 142 + 538 / 0,19 + 587",
        "formula_compras": "535 / 0,19 + 520 / 0,19 - 528 / 0,19 + 532 / 0,19 + 521 + 560 + 562",
        "codes": FORMULA_CODES,
        "codes_compras": COMPRAS_CODES,
        "acumulado": "Venta/Compras acumuladas: suma mes a mes dentro de cada año.",
        "facturas": "invoices: cantidad de facturas emitidas en el mes (código "
        "503), un conteo — nunca redondeado al millar. promedio_facturas: Venta "
        "del mes / invoices; null si el mes no declara facturas.",
        "variacion": "*_variacion_pct: (mismo mes año anterior / mes actual) - 1, en porcentaje.",
        "months": months,
    }


def _render_table(rows: list[dict], sep: str) -> list[str]:
    """Shared Markdown lines: one small table per year, its months as rows."""
    header = [
        "Período",
        "Folio",
        "Venta del mes",
        "Facturas Emitidas",
        "Promedio de monto por factura",
        "Venta acumulada",
        "Var. Venta",
        "Compras",
        "Compras acumulada",
        "Var. Compras",
    ]
    lines: list[str] = []
    lookup = thousands_by_period(rows)
    for year, year_rows in group_rows_by_year(rows):
        lines += [
            f"## {year}",
            "",
            "| " + sep.join(header) + " |",
            "| " + sep.join(["---"] * len(header)) + " |",
        ]
        acc_sales = acc_compras = 0
        agg = _YearAggregator()
        for r in fill_year_months(year, year_rows):
            # Month not declared in the document: dashes across the row, and it
            # contributes nothing to the accumulators or the yearly totals.
            if r.get("blank"):
                lines.append(
                    "| "
                    + sep.join([r["month_name"]] + ["—"] * (len(header) - 1))
                    + " |"
                )
                continue
            _, sales_k, compras_k = row_in_thousands(r)
            prev_sales, prev_compras = prior_year(lookup, r)
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
                fmt(r.get("invoices")),
                fmt(avg_invoice(sales_k, r.get("invoices"))),
                fmt(acc_sales),
                var_sales,
                compras,
                fmt(acc_compras),
                var_compras,
            ]
            lines.append("| " + sep.join(row) + " |")
            agg.add(sales_k, compras_k, r.get("invoices"))
        a = agg.averages()
        avg_row = [
            "**Promedio**",
            "—",
            fmt(a.sales),
            fmt(a.invoices),
            fmt(a.per_factura),
            "—",
            "—",
            fmt(a.compras),
            "—",
            "—",
        ]
        lines.append("| " + sep.join(avg_row) + " |")
        lines.append("")
    return lines


@app.post("/extract.md", response_class=PlainTextResponse)
async def extract_markdown(file: UploadFile = File(...)):
    """Return the monthly sales as a Markdown table (values in thousands)."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "")
    body = [
        "# Resumen de ventas y compras formulario 29 — miles de pesos",
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
    """Render the monthly table as a PDF, one small table per year: Mes, Venta
    del mes, Promedio de monto por factura and Compras totals (values in thousands).

    Kept on landscape A4 (two tables per grid row fit within the usable width);
    the taxpayer header block sits above the tables.

    `taxpayer` is the {"nombre", "rut"} dict from extract_taxpayer(); its fields
    are printed as a header block above the table when present.

    fpdf2 is pure-Python and imported lazily to keep the module importable
    without it. Core (Helvetica) fonts are Latin-1, which covers every glyph we
    emit here (accents like "í"); we avoid non-Latin-1 characters such as em
    dashes on purpose.
    """
    from fpdf import FPDF

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_title("Resumen de ventas y compras formulario 29 (miles de pesos)")
    # Quadrant positions are placed by hand, so keep fpdf from inserting its own
    # page breaks mid-grid.
    pdf.set_auto_page_break(False)
    page_left = pdf.l_margin
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(
        0,
        10,
        "Resumen de ventas y compras formulario 29 (miles de pesos)",
        new_x="LMARGIN",
        new_y="NEXT",
        align="C",
    )
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

    # Compact per-year table (header, width_mm, align), sized so two sit side by
    # side within landscape A4's ~277mm usable width. Widths sum to 132mm, so a
    # pair plus the 8mm column gap needs 272mm; the 2x2 grid places four of them
    # per page. Amount columns are the widest since they carry up to ~11 chars
    # ("127.138.974"); Facturas Emitidas holds a 2-4 digit count, so it is narrow.
    columns = [
        ("Mes", 11, "L"),
        ("Ventas", 18, "R"),
        ("Facturas Emitidas", 11, "R"),
        ("Promedio de monto por factura", 17, "R"),
        ("Ventas Acumulado", 19, "R"),
        ("% Var Ventas Acum Año Anterior", 19, "R"),
        ("Compras", 18, "R"),
        ("Compras Acumulado", 19, "R"),
    ]
    table_w = sum(w for _, w, _ in columns)  # 132mm

    orange = (230, 81, 0)  # matches the HTML deficit colour (#e65100)
    ROW_H = 4.0  # data / total / average row height
    HEAD_H = 10  # column-header row height (labels wrap onto 2-3 lines)
    TITLE_H = 6  # per-year title height

    def emit_row(cells, x0, deficit=False):
        """Draw one bordered row from `cells` starting at x0, on one line.

        When `deficit` is set, the value columns (every column except Mes) are
        printed in orange; the Mes column stays black.
        """
        pdf.set_left_margin(x0)
        pdf.set_x(x0)
        for i, ((_, w, align), text) in enumerate(zip(columns, cells)):
            last = i == len(columns) - 1
            pdf.set_text_color(*(orange if deficit and i >= 1 else (0, 0, 0)))
            pdf.cell(
                w,
                ROW_H,
                text,
                border=1,
                align=align,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP",
            )
        pdf.set_text_color(0, 0, 0)

    def emit_header(x0):
        """Draw the shaded column-header row at x0, wrapping long labels.

        Each header is a filled, bordered box of fixed height HEAD_H with its
        (possibly multi-line) label centred inside via multi_cell.
        """
        pdf.set_font("Helvetica", "B", 6)
        pdf.set_fill_color(244, 244, 245)
        x, y = x0, pdf.get_y()
        for label, w, _ in columns:
            pdf.set_xy(x, y)
            pdf.cell(w, HEAD_H, "", border=1, fill=True)
            pdf.set_xy(x, y + 0.8)
            pdf.multi_cell(w, 2.8, label, align="C")
            x += w
        pdf.set_left_margin(x0)
        pdf.set_xy(x0, y + HEAD_H)

    # Core Helvetica is Latin-1, which excludes the em dash fmt()/format_variation
    # use for a missing value, so render numeric cells with a plain hyphen.
    def money(v):
        return "-" if v is None else f"{v:,}".replace(",", ".")

    def pct(prev, curr):
        return format_variation(prev, curr).replace("—", "-")

    has_incomplete = False
    bottom_y = 0.0
    lookup = thousands_by_period(rows)

    def render_year(year, year_rows, x0, y0):
        """Draw one year's compact table with its top-left corner at (x0, y0)."""
        nonlocal has_incomplete, bottom_y
        pdf.set_xy(x0, y0)
        pdf.set_left_margin(x0)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(table_w, TITLE_H, str(year), new_x="LMARGIN", new_y="NEXT")
        emit_header(x0)
        pdf.set_font("Helvetica", "", 6)
        acc_sales = acc_compras = 0
        agg = _YearAggregator()
        for r in fill_year_months(year, year_rows):
            # Month not declared in the document: dashes across the row, and it
            # contributes nothing to the accumulators or the yearly totals.
            if r.get("blank"):
                emit_row([r["month_name"]] + ["-"] * (len(columns) - 1), x0)
                continue
            _, sales_k, compras_k = row_in_thousands(r)
            prev_sales, _ = prior_year(lookup, r)
            acc_sales += sales_k
            acc_compras += compras_k
            # Monthly year-over-year variation (this month vs the same month a
            # year earlier), shown in the "% Var Ventas Acum Año Anterior" column.
            var_sales = pct(prev_sales, sales_k)
            ventas = money(sales_k) + (" *" if r["missing"] else "")
            compras = money(compras_k) + (" *" if r["missing_compras"] else "")
            if r["missing"] or r["missing_compras"]:
                has_incomplete = True
            cells = [
                r["month_name"],
                ventas,
                money(r.get("invoices")),
                money(avg_invoice(sales_k, r.get("invoices"))),
                money(acc_sales),
                var_sales,
                compras,
                money(acc_compras),
            ]
            # Orange row when this month's accumulated Compras pass accumulated Venta.
            emit_row(cells, x0, acc_compras > acc_sales)
            agg.add(sales_k, compras_k, r.get("invoices"))
        # Per-year Total row: the year's summed Ventas and Compras (acc_sales /
        # acc_compras hold those sums after the month loop) plus its summed
        # Facturas Emitidas — a count, so summing is its natural aggregate.
        pdf.set_font("Helvetica", "B", 6)
        emit_row(
            [
                "Total",
                money(acc_sales),
                money(agg.tot_invoices or None),
                "-",
                "-",
                "-",
                money(acc_compras),
                "-",
            ],
            x0,
        )
        # Per-year Promedio row: each cell is the mean of its own column over the
        # months actually declared, except Promedio de monto por factura, which
        # divides the year's Venta by its invoice count (code 503). Acumuladas
        # and % Var have no meaningful mean ("-").
        a = agg.averages()
        emit_row(
            [
                "Promedio",
                money(a.sales),
                money(a.invoices),
                money(a.per_factura),
                "-",
                "-",
                money(a.compras),
                "-",
            ],
            x0,
        )
        pdf.set_font("Helvetica", "", 6)
        bottom_y = max(bottom_y, pdf.get_y())

    # 2x2 grid geometry: up to four years per page, filled left-to-right then
    # top-to-bottom (first year top-left, last year bottom-right). More than four
    # years spill onto additional pages, four at a time.
    years = list(group_rows_by_year(rows))
    col_gap = 8
    x_left = page_left
    x_right = page_left + table_w + col_gap
    # Every year table is now exactly TITLE_H + HEAD_H + 14*ROW_H high (Ene-Dic,
    # padded with blank months, plus Total and Promedio); pitch the two grid rows
    # so the bottom table clears the top one.
    slot_pitch = TITLE_H + HEAD_H + 14 * ROW_H + 6

    for start in range(0, len(years), 4):
        group = years[start : start + 4]
        if start == 0:
            grid_top = pdf.get_y()
        else:
            pdf.add_page()
            grid_top = pdf.t_margin
        slots = [
            (x_left, grid_top),
            (x_right, grid_top),
            (x_left, grid_top + slot_pitch),
            (x_right, grid_top + slot_pitch),
        ]
        for (year, year_rows), (sx, sy) in zip(group, slots):
            render_year(year, year_rows, sx, sy)

    pdf.set_left_margin(page_left)
    if has_incomplete:
        note_y = bottom_y + 4
        if note_y > pdf.h - pdf.b_margin - 8:
            pdf.add_page()
            note_y = pdf.t_margin
        pdf.set_xy(page_left, note_y)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(
            table_w * 2,
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
  h1 {{ font-size: 1.4rem; text-align: center; }}
  h2 {{ font-size: 1.1rem; margin: 1.5rem 0 .5rem; }}
  form {{ margin: 1rem 0 2rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem;
          font-variant-numeric: tabular-nums; }}
  th, td {{ border: 1px solid #ddd; padding: .35rem .6rem; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  thead th {{ background: #f4f4f5; }}
  tr.avg td {{ background: #fafafa; font-weight: 600; border-top: 2px solid #ccc; }}
  td.num {{ font-feature-settings: "tnum"; }}
  /* Month whose accumulated Compras exceed accumulated Venta: numbers in orange. */
  tr.deficit td.num {{ color: #e65100; }}
  /* Month with no declaration in the document: dashes, dimmed. */
  tr.blank td {{ color: #aaa; }}
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
        "Facturas Emitidas",
        "Promedio de monto por factura",
        "Venta acumulada",
        "Var. Venta",
        "Compras",
        "Compras acumulada",
        "Var. Compras",
    ]
    head = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in columns) + "</tr></thead>"
    sections = ""
    lookup = thousands_by_period(rows)
    for year, year_rows in group_rows_by_year(rows):
        body = ""
        acc_sales = acc_compras = 0
        agg = _YearAggregator()
        for r in fill_year_months(year, year_rows):
            # Month not declared in the document: dashes across the row, and it
            # contributes nothing to the accumulators or the yearly totals.
            if r.get("blank"):
                body += (
                    f'<tr class="blank"><td>{html.escape(r["month_name"])}</td>'
                    + '<td class="num">—</td>' * (len(columns) - 1)
                    + "</tr>"
                )
                continue
            _, sales_k, compras_k = row_in_thousands(r)
            prev_sales, prev_compras = prior_year(lookup, r)
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
                f'<td class="num">{fmt(r.get("invoices"))}</td>'
                f'<td class="num">{fmt(avg_invoice(sales_k, r.get("invoices")))}</td>'
                f'<td class="num">{fmt(acc_sales)}</td>'
                f'<td class="num">{html.escape(var_sales)}</td>'
                f'<td class="num">{compras}</td>'
                f'<td class="num">{fmt(acc_compras)}</td>'
                f'<td class="num">{html.escape(var_compras)}</td></tr>'
            )
            agg.add(sales_k, compras_k, r.get("invoices"))
        a = agg.averages()
        body += (
            '<tr class="avg"><td>Promedio</td>'
            '<td class="num">—</td>'
            f'<td class="num">{fmt(a.sales)}</td>'
            f'<td class="num">{fmt(a.invoices)}</td>'
            f'<td class="num">{fmt(a.per_factura)}</td>'
            '<td class="num">—</td>'
            '<td class="num">—</td>'
            f'<td class="num">{fmt(a.compras)}</td>'
            '<td class="num">—</td>'
            '<td class="num">—</td></tr>'
        )
        sections += f"<h2>{year}</h2><table>{head}<tbody>{body}</tbody></table>"
    return sections


_FORMULA_NOTE = (
    '<p class="note">Venta del mes: <code>020 + 142 + 538 / 0,19 + 587</code>. '
    "Compras: "
    "<code>535 / 0,19 + 520 / 0,19 - 528 / 0,19 + 532 / 0,19 + 521 + 560 + 562</code>. "
    "Facturas Emitidas: cantidad de facturas del mes (<code>503</code>), un conteo "
    "sin redondeo. Promedio de monto por factura: Venta del mes / Facturas "
    "Emitidas del mismo mes. "
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
                "missing_compras": [c for c in COMPRAS_CODES if r["codes"][c] is None],
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
