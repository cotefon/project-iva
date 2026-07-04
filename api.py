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

import os
import tempfile

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse

from extract_codes import (
    FORMULA_CODES,
    extract_taxpayer,
    month_sales,
    monthly_rows,
    pdf_to_text,
)

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

    Codes are rounded to the nearest thousand *first*, then the sales formula is
    applied to those rounded values and its result rounded to the nearest
    thousand too. Missing codes stay None (the formula counts them as 0).

    Returns (codes_thousands: dict, sales_thousands: int).
    """
    codes_k = {c: round_thousand(row["codes"][c]) for c in FORMULA_CODES}
    sales_k = int(round(month_sales(codes_k)))
    return codes_k, sales_k


def fmt(v) -> str:
    """Format an integer with comma thousands separators (e.g. 1,872,854).

    None (a missing code) renders as an em dash.
    """
    return "—" if v is None else f"{v:,}"


def pdf_to_rows(data: bytes, filename: str) -> tuple[str, list[dict]]:
    """Convert uploaded PDF bytes to (extracted text, structured monthly rows).

    The raw text is returned too so callers can pull header data (taxpayer name
    / RUT) without re-reading the file. pdfplumber needs a real path, so we stage
    the upload in a temp file that is always cleaned up. Rejects non-PDF uploads
    and empty results early.
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
        text = pdf_to_text(tmp_path)
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
    return text, rows


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    """Return the monthly sales as JSON, all amounts in thousands of pesos."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "")

    def month_json(r):
        codes_k, sales_k = row_in_thousands(r)
        return {
            "periodo": r["label"],
            "year": r["year"],
            "month": r["month"],
            "codes": codes_k,
            "venta_del_mes": sales_k,
            "missing": r["missing"],
        }

    return {
        "unit": "miles de pesos (codes rounded to nearest thousand before the "
        "formula; result rounded to nearest thousand)",
        "formula": "020 + 142 + 538 / 0,19 + 587",
        "codes": FORMULA_CODES,
        "months": [month_json(r) for r in rows],
    }


def _render_table(rows: list[dict], sep: str) -> list[str]:
    """Shared table rows (list of pipe-joined lines) for Markdown and HTML."""
    header = ["Período"] + FORMULA_CODES + ["Venta del mes"]
    lines = [
        "| " + sep.join(header) + " |",
        "| " + sep.join(["---"] * len(header)) + " |",
    ]
    for r in rows:
        codes_k, sales_k = row_in_thousands(r)
        cells = [fmt(codes_k[c]) for c in FORMULA_CODES]
        sales = fmt(sales_k)
        if r["missing"]:
            sales += " "
        lines.append("| " + sep.join([r["label"]] + cells + [sales]) + " |")
    return lines


@app.post("/extract.md", response_class=PlainTextResponse)
async def extract_markdown(file: UploadFile = File(...)):
    """Return the monthly sales as a Markdown table (values in thousands)."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "")
    body = [
        "# Ventas por mes (Formulario 29) — miles de pesos",
        "",
        "Fórmula: `020 + 142 + 538 / 0,19 + 587`  ·  Todos los montos en "
        "**miles de pesos**: los códigos se redondean al millar antes de "
        "aplicar la fórmula y la **Venta del mes** también se redondea al millar.",
        "",
        *_render_table(rows, " | "),
    ]
    incomplete = [
        f"- {r['label']} (faltan: {', '.join(r['missing'])})"
        for r in rows
        if r["missing"]
    ]
    if incomplete:
        body += ["", "\\* Mes con códigos faltantes (contados como 0):", *incomplete]
    return PlainTextResponse("\n".join(body) + "\n", media_type="text/markdown")


def build_pdf(rows: list[dict], taxpayer: dict | None = None) -> bytes:
    """Render a Período | Ventas PDF (the code columns are omitted).

    `taxpayer` is the {"nombre", "rut"} dict from extract_taxpayer(); its fields
    are printed as a header block above the table when present.

    fpdf2 is pure-Python and imported lazily to keep the module importable
    without it. Core (Helvetica) fonts are Latin-1, which covers every glyph we
    emit here (accents like "í"); we avoid non-Latin-1 characters such as em
    dashes on purpose.
    """
    from fpdf import FPDF

    pdf = FPDF(orientation="P", unit="mm", format="A4")
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

    period_w, ventas_w = 90, 55
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(244, 244, 245)
    pdf.cell(period_w, 9, "Período", border=1, fill=True)
    pdf.cell(
        ventas_w,
        9,
        "Ventas",
        border=1,
        align="R",
        fill=True,
        new_x="LMARGIN",
        new_y="NEXT",
    )

    pdf.set_font("Helvetica", "", 11)
    has_incomplete = False
    for r in rows:
        _, sales_k = row_in_thousands(r)
        ventas = fmt(sales_k)
        if r["missing"]:
            ventas += " "
            has_incomplete = True
        pdf.cell(period_w, 8, r["label"], border=1)
        pdf.cell(
            ventas_w, 8, ventas, border=1, align="R", new_x="LMARGIN", new_y="NEXT"
        )

    if has_incomplete:
        pdf.ln(3)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(
            period_w + ventas_w,
            5,
        )

    return bytes(pdf.output())


@app.post("/extract.pdf")
async def extract_pdf(file: UploadFile = File(...)):
    """Return a PDF with a Período | Ventas table (values in thousands)."""
    text, rows = pdf_to_rows(await file.read(), file.filename or "")
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
  form {{ margin: 1rem 0 2rem; }}
  table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  th, td {{ border: 1px solid #ddd; padding: .35rem .6rem; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  thead th {{ background: #f4f4f5; }}
  td.num {{ font-feature-settings: "tnum"; }}
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


@app.post("/", response_class=HTMLResponse)
async def home_submit(file: UploadFile = File(...)):
    """Handle the browser form upload and render the results as an HTML table."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "")
    table = "\n".join(
        [
            '<p class="note">Fórmula: <code>020 + 142 + 538 / 0,19 + 587</code>. '
            "Montos en miles de pesos (códigos redondeados al millar antes de la "
            "fórmula). * = mes con códigos faltantes (contados como 0).</p>",
            "<table><thead>",
            "<tr>"
            + "".join(
                f"<th>{h}</th>" for h in ["Período", *FORMULA_CODES, "Venta del mes"]
            )
            + "</tr>",
            "</thead><tbody>",
        ]
    )
    for r in rows:
        codes_k, sales_k = row_in_thousands(r)
        cells = "".join(
            f'<td class="num">{fmt(codes_k[c])}</td>' for c in FORMULA_CODES
        )
        sales = fmt(sales_k) + (" *" if r["missing"] else "")
        table += (
            f'<tr><td>{r["label"]}</td>{cells}' f'<td class="num">{sales}</td></tr>'
        )
    table += "</tbody></table>"
    return PAGE.format(form=UPLOAD_FORM, results=table)
