"""FastAPI service for the F29 / IVA sales extractor.

Upload a Chilean tax PDF (Carpeta Tributaria) and get back a per-month table of
the Formulario 29 sales figures. All amounts are rounded to the nearest thousand
pesos: e.g. 1.000.000 is reported as 1.000.

Every endpoint requires a signed-in caller: the React app in `web/` authenticates
against Supabase Auth and sends the access token as a bearer header, which
`auth.current_user` verifies. Uploads are recorded against that user, and the
history endpoints only ever return the caller's own documents.

Endpoints (all authenticated):
    GET   /me             the caller's profile: {"id", "email", "nombre"}.
    PATCH /me             edit the profile (nombre) and credentials (email/password).
    GET   /ruts           the RUTs this user has uploaded; ?q= filters by RUT.
    GET   /periods/{rut}  the (year, month) periods stored for a RUT.
    GET   /history/{rut}  the user's latest stored extraction for a RUT, as JSON.
    GET   /report/{rut}   the same, narrowed to ?desde=&hasta= (AAAA-MM).
    GET   /report/{rut}.pdf  that narrowed report as a PDF.
    POST  /extract        JSON: {"unit": "miles de pesos", "months": [...]}.
    POST  /extract.md     Markdown table (text/markdown), values in thousands.
    POST  /extract.pdf    the rendered PDF report.

The /report routes are the period-range pair: they read the stored document (the
uploaded file is never kept) and render only the months the caller asked for.
Their /history counterparts always render the whole document.

The extraction logic lives in `extract_codes.py`; this module only handles the
HTTP layer, temp-file plumbing, and the round-to-thousand presentation. The UI
lives in `web/` — this module renders no HTML.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import NamedTuple

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from auth import User, current_user
from extract_codes import (
    BLANK,
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

# The React app runs on its own origin (Vite's dev server by default), so the
# browser needs CORS to call this API at all. Set ALLOWED_ORIGINS to a
# comma-separated list for other environments.
#
# expose_headers is not optional here: without it the browser hides
# Content-Disposition from JavaScript, and the download button in web/ would
# lose the "ventas_por_mes.pdf" filename that /extract.pdf sets.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


# A period bound as the API takes it: "2024-03" (AAAA-MM).
PERIOD_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


class PeriodRange(NamedTuple):
    """An inclusive [desde, hasta] filter over (year, month), either side open.

    Every report endpoint accepts `?desde=&hasta=` and narrows its tables to the
    range. Two rules make the narrowed report mean what it says:

    - The **accumulated** columns restart at `desde`, because they sum only the
      months on the report. A range starting in Mar 2023 accumulates from Mar.
    - Every year table still spans **Ene-Dic**. A month outside the range is
      padded like one that was never declared — a hyphen in each column,
      contributing nothing — so a partial year always reads as twelve rows.
      The range decides which months carry figures, not how many rows there are.
    - The **year-over-year** columns still compare against the same month a year
      earlier even when that month falls outside the range. The renderers
      therefore take both the narrowed rows and the full set: what is *rendered*
      is narrowed, what is *looked up* is not. Filtering the lookup too would
      blank out the variation of the first year on every report.

    The filter is presentation only: `pdf_to_rows()` persists the whole document
    regardless, so narrowing a report never narrows what is stored.
    """

    desde: tuple[int, int] | None = None
    hasta: tuple[int, int] | None = None

    @property
    def is_open(self) -> bool:
        """True when neither bound is set, i.e. the whole document is reported."""
        return self.desde is None and self.hasta is None

    def as_json(self) -> dict:
        """The range as the frontend receives it: {"desde": "2023-03", ...}."""

        def fmt_period(p):
            return None if p is None else f"{p[0]:04d}-{p[1]:02d}"

        return {"desde": fmt_period(self.desde), "hasta": fmt_period(self.hasta)}

    def label(self) -> str:
        """Human-readable span for a report header: "Mar 2023 - Ago 2024".

        Latin-1 only (a plain hyphen, not an en dash), since the PDF's core
        Helvetica cannot encode one. An open side reads "Desde ..." / "Hasta
        ...", and a fully open range reports the whole document.
        """

        def month_year(p):
            return f"{SPANISH_MONTHS.get(p[1], p[1])} {p[0]}"

        if self.is_open:
            return "Todos los períodos del documento"
        if self.desde and self.hasta:
            return f"{month_year(self.desde)} - {month_year(self.hasta)}"
        if self.desde:
            return f"Desde {month_year(self.desde)}"
        return f"Hasta {month_year(self.hasta)}"


def _parse_period(value: str | None, field: str) -> tuple[int, int] | None:
    """'2024-03' -> (2024, 3). None/empty means "no bound on this side"."""
    if not value or not value.strip():
        return None
    match = PERIOD_RE.match(value.strip())
    if not match:
        raise HTTPException(
            status_code=400,
            detail=f"'{field}' debe tener el formato AAAA-MM (por ejemplo 2024-03).",
        )
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise HTTPException(
            status_code=400, detail=f"'{field}': el mes debe estar entre 01 y 12."
        )
    return (year, month)


def period_range(
    desde: str | None = Query(
        None, description="Primer período del informe, AAAA-MM (por ejemplo 2023-03)."
    ),
    hasta: str | None = Query(
        None, description="Último período del informe, AAAA-MM (por ejemplo 2024-08)."
    ),
) -> PeriodRange:
    """FastAPI dependency: the `?desde=&hasta=` filter, validated.

    Shared by every report endpoint so the JSON, the Markdown and the PDF cannot
    disagree about what a range means. Omitting both reports the whole document.
    """
    rng = PeriodRange(_parse_period(desde, "desde"), _parse_period(hasta, "hasta"))
    if rng.desde and rng.hasta and rng.desde > rng.hasta:
        raise HTTPException(
            status_code=400,
            detail="El período 'desde' no puede ser posterior a 'hasta'.",
        )
    return rng


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


def year_is_complete(year_rows: list[dict]) -> bool:
    """True when the year has a declaration for all twelve months.

    The deficit highlight (accumulated Compras above accumulated Venta) is only
    meaningful on a full year: in a partial year the accumulated figures cover
    different spans of months and would flag a gap in the document as a deficit.
    """
    return len({r["month"] for r in year_rows}) == 12


def fmt(v) -> str:
    """Format an integer with Chilean dot thousands separators (e.g. 1.872.854).

    None (a missing code) renders as BLANK.
    """
    return BLANK if v is None else f"{v:,}".replace(",", ".")


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
    data: bytes, filename: str, user_id: str, extract=pdf_to_text
) -> tuple[str, list[dict]]:
    """Convert uploaded PDF bytes to (extracted text, structured monthly rows).

    The raw text is returned too so callers can pull header data (taxpayer name
    / RUT) without re-reading the file. The `extract` callable turns a PDF path
    into text (defaults to pdfplumber's `pdf_to_text`; pass `pdf_to_text_pypdf`
    for the faster backend). Both need a real path, so we stage the upload in a
    temp file that is always cleaned up. Rejects non-PDF uploads and empty
    results early.

    `user_id` is recorded as the document's owner, so the history endpoints can
    return this upload to its uploader and to nobody else.
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
            extract_taxpayer(text), rows, source_file=filename, user_id=user_id
        )
        log.info("Persisted extraction as document %s (%s rows)", doc_id, len(rows))
    except Exception:
        log.exception("Failed to persist extraction to Supabase")

    return text, rows


@app.get("/ruts")
async def list_ruts(
    q: str | None = Query(
        None,
        description=(
            "Filtra por RUT (fragmento). Los puntos y el guion se ignoran, "
            "de modo que 79527050 y 79.527.050-7 encuentran lo mismo. "
            "No busca por nombre."
        ),
    ),
    user: User = Depends(current_user),
):
    """Return the RUTs this user has uploaded, each with its name.

    `?q=` narrows the list by **RUT only** — never by name. A query is read as a
    RUT fragment with dots and hyphens ignored, so "79527050" finds
    79.527.050-7 while "Frutam" finds nothing. That is deliberate; see
    `search_taxpayers` in schema.sql.

    Filtering keeps the same response shape as the unfiltered list, so the
    frontend renders both through one code path. Omitting `q` (or sending it
    blank) returns everything, exactly as before.

    503 if persistence isn't configured (SUPABASE_URL / SUPABASE_KEY missing).
    """
    try:
        taxpayers = (
            storage.search_taxpayers(user.id, q)
            if q and q.strip()
            else storage.list_taxpayers(user.id)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "count": len(taxpayers),
        "ruts": [t["rut"] for t in taxpayers],
        "taxpayers": taxpayers,
    }


def _extract_payload(
    rows: list[dict],
    all_rows: list[dict] | None = None,
    rng: PeriodRange = PeriodRange(),
) -> dict:
    """The JSON body for a set of monthly rows, all amounts in thousands.

    Shared by /extract and /history/{rut} so a freshly uploaded document and a
    stored one arrive in the same shape and the React table renders both without
    branching.

    `rows` is what the report shows (already narrowed to `rng`); `all_rows` is
    the whole document, used only for the year-over-year lookup so a month at the
    start of the range still compares against the year before it. `rng` is echoed
    back as `period` so the frontend knows which span it is rendering.
    """

    # (prev / curr - 1) * 100, or None when undefined (no matching month a year
    # earlier, or a current value of 0). Compared against the same month of the
    # previous year.
    def var_pct(prev, curr):
        return None if prev is None or not curr else round((prev / curr - 1) * 100, 1)

    # Accumulated Venta del mes / Compras reset each year, matching the per-year
    # tables the other endpoints render.
    lookup = thousands_by_period(all_rows if all_rows is not None else rows)
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
        # The requested span, echoed so the caller can label the report and pad
        # its boundary years to the same months the server did. null/null means
        # the whole document.
        "period": rng.as_json(),
        "months": months,
    }


@app.post("/extract")
async def extract(file: UploadFile = File(...), user: User = Depends(current_user)):
    """Return the monthly sales as JSON, all amounts in thousands of pesos.

    The whole document is reported; narrowing it to a period range is what the
    /report routes do. The `taxpayer` block is included for the same reason
    /history/{rut} carries one: it names the document that was just stored, which
    is how the caller addresses it as /report/{rut} afterwards — without that,
    the period picker would have no RUT to ask about a fresh upload. `rut` is
    null when the header could not be parsed, and such a document is only
    reachable by re-uploading it.
    """
    text, rows = pdf_to_rows(await file.read(), file.filename or "", user.id)
    return {"taxpayer": extract_taxpayer(text), **_extract_payload(rows)}


def _render_table(
    rows: list[dict],
    sep: str,
    all_rows: list[dict] | None = None,
    rng: PeriodRange = PeriodRange(),
) -> list[str]:
    """Shared Markdown lines: one small table per year, its months as rows.

    `rows` is the selection to render, `all_rows` the whole document (kept for
    the year-over-year lookup) and `rng` the range that produced the selection,
    which also clips the Ene-Dic padding of the boundary years.
    """
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
    lines: list[str] = []
    lookup = thousands_by_period(all_rows if all_rows is not None else rows)
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
                    + sep.join([r["month_name"]] + [BLANK] * (len(header) - 1))
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
                r.get("folio") or BLANK,
                sales,
                fmt(acc_sales),
                var_sales,
                fmt(r.get("invoices")),
                fmt(avg_invoice(sales_k, r.get("invoices"))),
                compras,
                fmt(acc_compras),
                var_compras,
            ]
            lines.append("| " + sep.join(row) + " |")
            agg.add(sales_k, compras_k, r.get("invoices"))
        a = agg.averages()
        avg_row = [
            "**Promedio**",
            BLANK,
            fmt(a.sales),
            BLANK,
            BLANK,
            fmt(a.invoices),
            fmt(a.per_factura),
            fmt(a.compras),
            BLANK,
            BLANK,
        ]
        lines.append("| " + sep.join(avg_row) + " |")
        lines.append("")
    return lines


@app.post("/extract.md", response_class=PlainTextResponse)
async def extract_markdown(
    file: UploadFile = File(...), user: User = Depends(current_user)
):
    """Return the monthly sales as a Markdown table (values in thousands)."""
    _, rows = pdf_to_rows(await file.read(), file.filename or "", user.id)
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


def build_pdf(
    rows: list[dict],
    taxpayer: dict | None = None,
    all_rows: list[dict] | None = None,
    rng: PeriodRange = PeriodRange(),
) -> bytes:
    """Render the monthly table as a PDF, one small table per year: Mes, Venta
    del mes, Promedio de monto por factura and Compras totals (values in thousands).

    Kept on landscape A4 (two tables per grid row fit within the usable width);
    the taxpayer header block sits above the tables.

    `taxpayer` is the {"nombre", "rut"} dict from extract_taxpayer(); its fields
    are printed as a header block above the table when present.

    `rows` is the selection to render; `all_rows` is the whole document, used
    only for the year-over-year lookup so the first year of a narrowed report
    keeps its variation column. `rng` is the range that produced the selection:
    it clips the boundary years' padding and is printed under the taxpayer block
    so a partial report says so on its face.

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
    pdf.ln(2)

    # Taxpayer header: label in bold, value in regular, one per line. The período
    # line is only printed for a narrowed report — on a full document it would
    # just restate the tables below it.
    taxpayer = taxpayer or {}
    fields = [
        ("Nombre / Razón Social", taxpayer.get("nombre")),
        ("RUT", taxpayer.get("rut")),
    ]
    if not rng.is_open:
        fields.append(("Período", rng.label()))
    for label, value in fields:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(42, 6, f"{label}:", new_x="RIGHT", new_y="TOP")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(
            0, 6, value if value else "No disponible", new_x="LMARGIN", new_y="NEXT"
        )
    pdf.ln(3)

    # Compact per-year table (header, width_mm, align), holding the same ten
    # columns as the on-screen table and the Markdown report, in the same order.
    #
    # Sized so two sit side by side within landscape A4's ~277mm usable width:
    # the widths sum to 133mm, so a pair plus the 8mm column gap needs 274mm and
    # the 2x2 grid places four of them per page.
    #
    # Each width was measured with get_string_width at 6pt against two things:
    # the widest real cell, and the **longest single word of the header**, which
    # emit_header wraps with multi_cell. Sizing on the header text as a whole is
    # not enough — a column narrower than its longest word breaks it mid-word
    # ("Compra/s", "Emiti/das"). The remaining slack went to the amount columns,
    # which are the ones that grow with a larger taxpayer.
    #
    # The variation headers stay short ("% Var Ventas") and the comparison they
    # make is spelled out in the footnote, rather than wrapping "Año Anterior"
    # onto two more lines in every table.
    columns = [
        ("Mes", 11, "L"),
        ("Folio", 13, "R"),
        ("Ventas", 16, "R"),
        ("Ventas Acumulado", 16, "R"),
        ("% Var Ventas", 10, "R"),
        ("Facturas Emitidas", 11, "R"),
        ("Promedio de monto por factura", 12, "R"),
        ("Compras", 16, "R"),
        ("Compras Acumulado", 16, "R"),
        ("% Var Compras", 12, "R"),
    ]
    table_w = sum(w for _, w, _ in columns)  # 133mm

    orange = (230, 81, 0)  # matches the HTML deficit colour (#e65100)
    ROW_H = 4.0  # data / total / average row height
    TITLE_H = 6  # per-year title height
    HEAD_LINE_H = 2.8  # one wrapped line of a column header
    HEAD_PAD = 1.6  # total vertical padding inside the header box

    # The column-header height is measured, not hard-coded. emit_header wraps
    # each label with multi_cell, so a label needing one more line than the box
    # is tall enough for spills over the first data rows ("Promedio de monto por
    # factura" wraps onto four lines at 12mm, which a fixed 10mm box clipped).
    # Ask fpdf how many lines each label actually takes at its own width and
    # size the box to the tallest, so renaming or renarrowing a column adjusts
    # the header instead of overflowing it.
    pdf.set_font("Helvetica", "B", 6)
    head_lines = [
        len(pdf.multi_cell(w, HEAD_LINE_H, label, align="C", dry_run=True, output="LINES"))
        for label, w, _ in columns
    ]
    HEAD_H = HEAD_PAD + max(head_lines) * HEAD_LINE_H

    COMPRAS_COL = 7  # index of "Compras" in `columns`

    def emit_row(cells, x0, deficit_col=None):
        """Draw one bordered row from `cells` starting at x0, on one line.

        `deficit_col` is the index of the single cell to print in orange (the
        Total row's Compras when the year's purchases pass its sales); every
        other cell stays black.
        """
        pdf.set_left_margin(x0)
        pdf.set_x(x0)
        for i, ((_, w, align), text) in enumerate(zip(columns, cells)):
            last = i == len(columns) - 1
            pdf.set_text_color(*(orange if i == deficit_col else (0, 0, 0)))
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

        Each header is a filled, bordered box of height HEAD_H with its
        (possibly multi-line) label centred inside via multi_cell — horizontally
        by `align`, vertically by offsetting the text block by the slack its own
        line count leaves, so a one-line label sits level with a four-line one.
        """
        pdf.set_font("Helvetica", "B", 6)
        pdf.set_fill_color(244, 244, 245)
        x, y = x0, pdf.get_y()
        for (label, w, _), lines in zip(columns, head_lines):
            pdf.set_xy(x, y)
            pdf.cell(w, HEAD_H, "", border=1, fill=True)
            pdf.set_xy(x, y + (HEAD_H - lines * HEAD_LINE_H) / 2)
            pdf.multi_cell(w, HEAD_LINE_H, label, align="C")
            x += w
        pdf.set_left_margin(x0)
        pdf.set_xy(x0, y + HEAD_H)

    # BLANK is a plain hyphen precisely because core Helvetica is Latin-1 and
    # cannot encode an em dash, so these need no substitution of their own.
    def money(v):
        return BLANK if v is None else f"{v:,}".replace(",", ".")

    def pct(prev, curr):
        return format_variation(prev, curr)

    has_incomplete = False
    bottom_y = 0.0
    lookup = thousands_by_period(all_rows if all_rows is not None else rows)

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
        complete = year_is_complete(year_rows)
        agg = _YearAggregator()
        for r in fill_year_months(year, year_rows):
            # Month not declared in the document: dashes across the row, and it
            # contributes nothing to the accumulators or the yearly totals.
            if r.get("blank"):
                emit_row([r["month_name"]] + [BLANK] * (len(columns) - 1), x0)
                continue
            _, sales_k, compras_k = row_in_thousands(r)
            prev_sales, prev_compras = prior_year(lookup, r)
            acc_sales += sales_k
            acc_compras += compras_k
            # Monthly year-over-year variation (this month vs the same month a
            # year earlier), for Ventas and Compras alike.
            var_sales = pct(prev_sales, sales_k)
            var_compras = pct(prev_compras, compras_k)
            ventas = money(sales_k) + (" *" if r["missing"] else "")
            compras = money(compras_k) + (" *" if r["missing_compras"] else "")
            if r["missing"] or r["missing_compras"]:
                has_incomplete = True
            cells = [
                r["month_name"],
                r.get("folio") or BLANK,
                ventas,
                money(acc_sales),
                var_sales,
                money(r.get("invoices")),
                money(avg_invoice(sales_k, r.get("invoices"))),
                compras,
                money(acc_compras),
                var_compras,
            ]
            emit_row(cells, x0)
            agg.add(sales_k, compras_k, r.get("invoices"))
        # Per-year Total row: the year's summed Ventas and Compras (acc_sales /
        # acc_compras hold those sums after the month loop) plus its summed
        # Facturas Emitidas — a count, so summing is its natural aggregate.
        #
        # Its Compras cell turns orange when the year bought more than it sold,
        # and only on a year with all twelve months declared: in a partial year
        # the two totals cover different spans of months, so a gap in the
        # document would read as a deficit.
        pdf.set_font("Helvetica", "B", 6)
        emit_row(
            [
                "Total",
                BLANK,
                money(acc_sales),
                BLANK,
                BLANK,
                money(agg.tot_invoices or None),
                BLANK,
                money(acc_compras),
                BLANK,
                BLANK,
            ],
            x0,
            COMPRAS_COL if complete and acc_compras > acc_sales else None,
        )
        # Per-year Promedio row: each cell is the mean of its own column over the
        # months actually declared, except Promedio de monto por factura, which
        # divides the year's Venta by its invoice count (code 503). Acumuladas
        # and % Var have no meaningful mean (BLANK).
        a = agg.averages()
        emit_row(
            [
                "Promedio",
                BLANK,
                money(a.sales),
                BLANK,
                BLANK,
                money(a.invoices),
                money(a.per_factura),
                money(a.compras),
                BLANK,
                BLANK,
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
    # A year table is TITLE_H + HEAD_H + (months + 2) * ROW_H high: its padded
    # months plus the Total and Promedio rows. That is 14 rows on a full Ene-Dic
    # year, fewer when a period range clips the span, so pitch the two grid rows
    # by the tallest table actually being drawn rather than by a fixed twelve —
    # a three-month report would otherwise leave two thirds of the page blank.
    tallest = max(
        (len(fill_year_months(y, r)) for y, r in years), default=12
    )
    slot_pitch = TITLE_H + HEAD_H + (tallest + 2) * ROW_H + 6

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

    # Footnotes. The variation note is always shown, since the "% Var" headers
    # are deliberately short and do not say what they compare against; the
    # missing-codes note only when some month actually carries the marker.
    notes = [
        "% Var Ventas / % Var Compras: variacion respecto del mismo mes del "
        "ano anterior."
    ]
    if has_incomplete:
        notes.append("* Mes con codigos faltantes (contados como 0).")

    pdf.set_left_margin(page_left)
    note_y = bottom_y + 4
    if note_y > pdf.h - pdf.b_margin - 8 * len(notes):
        pdf.add_page()
        note_y = pdf.t_margin
    pdf.set_xy(page_left, note_y)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(table_w * 2, 5, "\n".join(notes))

    return bytes(pdf.output())


@app.post("/extract.pdf")
async def extract_pdf(file: UploadFile = File(...), user: User = Depends(current_user)):
    """Return a PDF with the full monthly table (all codes + totals, thousands)."""
    text, rows = pdf_to_rows(await file.read(), file.filename or "", user.id)
    taxpayer = extract_taxpayer(text)
    return Response(
        content=build_pdf(rows, taxpayer),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="ventas_por_mes.pdf"'},
    )


@app.post("/extract-fast.pdf")
async def extract_pdf_fast(
    file: UploadFile = File(...), user: User = Depends(current_user)
):
    """Same PDF as /extract.pdf but read with the faster pypdf backend.

    Identical output; only the source-PDF text extraction differs (pypdf instead
    of pdfplumber), which is where nearly all the request time is spent. Use this
    to compare speed and confirm the parsed figures match /extract.pdf on real
    documents before switching the default backend.
    """
    text, rows = pdf_to_rows(
        await file.read(), file.filename or "", user.id, extract=pdf_to_text_pypdf
    )
    taxpayer = extract_taxpayer(text)
    return Response(
        content=build_pdf(rows, taxpayer),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="ventas_por_mes.pdf"'},
    )


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


def _timeline_or_404(rut: str, user_id: str, rng: PeriodRange) -> tuple[dict, dict]:
    """The caller's whole timeline for a RUT, plus the same timeline narrowed.

    Returns `(full, selected)`. Both come from `storage.rut_timeline`, which
    merges every document the user uploaded for the RUT — so a report can span
    more months than any single upload covers.

    The range is applied by the database, not in Python: `selected` is fetched
    with the bounds, so a three-month report reads three months. `full`
    is fetched unbounded because the renderers still need the months *outside*
    the range for the year-over-year lookup — that is the one thing the narrowed
    query cannot supply.

    Errors mirror _latest_or_404: 503 when persistence is unconfigured, 404 when
    this user has nothing stored for the RUT (including when another user has
    uploaded it), and 404 when the range selects no months.
    """
    try:
        full = storage.rut_timeline(rut, user_id)
        selected = (
            full
            if rng.is_open
            else storage.rut_timeline(rut, user_id, rng.desde, rng.hasta)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if full is None or not full["rows"]:
        raise HTTPException(
            status_code=404, detail=f"Sin extracciones guardadas para el RUT {rut}."
        )
    if selected is None or not selected["rows"]:
        raise HTTPException(
            status_code=404,
            detail=(
                f"El RUT {rut} no tiene declaraciones en el período solicitado "
                f"({rng.label()})."
            ),
        )
    return full, selected


def _latest_or_404(rut: str, user_id: str) -> dict:
    """The caller's most recent stored document for a RUT, or the right error.

    404 if this user has nothing stored for that RUT — including when another
    user has uploaded it, since one user's documents are never readable by
    another. 503 if persistence isn't configured (SUPABASE_URL / SUPABASE_KEY
    missing), so the reason is explicit.
    """
    try:
        doc = storage.latest_document(rut, user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if doc is None:
        raise HTTPException(
            status_code=404, detail=f"Sin extracciones guardadas para el RUT {rut}."
        )
    return doc


# Declared before /history/{rut}: a path parameter matches dots too, so
# "/history/79.527.050-7.pdf" would otherwise be served by the JSON route with
# the ".pdf" swallowed into the RUT. Starlette matches in declaration order.
@app.get("/history/{rut}.pdf")
async def history_pdf(rut: str, user: User = Depends(current_user)):
    """The same PDF report as /extract.pdf, rebuilt from a stored document.

    The uploaded file itself is never kept — only the extracted figures — so the
    report is re-rendered from the stored rows rather than re-parsed. That is
    what lets a past document be downloaded without uploading the PDF again.
    """
    doc = _latest_or_404(rut, user.id)
    taxpayer = {"nombre": doc["nombre"], "rut": doc["rut"]}
    # The RUT is in the filename so several downloads stay tellable apart in the
    # browser's download folder. It is digits, dots and a dash — no quoting or
    # encoding needed in the header.
    return Response(
        content=build_pdf(_stored_rows(doc), taxpayer),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="ventas_{doc["rut"]}.pdf"'
        },
    )


# Declared before /report/{rut}, for the same reason /history/{rut}.pdf is
# declared before /history/{rut}: a path parameter matches dots, so the JSON
# route would otherwise swallow the ".pdf".
@app.get("/report/{rut}.pdf")
async def report_pdf(
    rut: str,
    rng: PeriodRange = Depends(period_range),
    user: User = Depends(current_user),
):
    """The report for a RUT, narrowed to a period range, as a PDF.

    Two things separate it from /history/{rut}.pdf, which renders one upload:

    - It reports the **RUT**, merging every document the caller uploaded for it,
      newest declaration winning per period. A 2021-2024 carpeta and a 2023-2026
      one together report 2021-2026, which neither covers alone.
    - The caller chooses the span with `?desde=` and `?hasta=` (AAAA-MM, either
      side optional, both inclusive):

          GET /report/76.044.491-K.pdf?desde=2023-03&hasta=2024-08

      reports Mar-Dic 2023 and Ene-Ago 2024. The accumulated columns restart at
      `desde`; the year-over-year columns still compare against the same month a
      year earlier even when it falls outside the range.

    Omitting both bounds reports the RUT's whole timeline.
    """
    full, selected = _timeline_or_404(rut, user.id, rng)
    taxpayer = {"nombre": full["nombre"], "rut": full["rut"]}
    # The span goes in the filename so downloads of different ranges of the same
    # RUT do not overwrite each other. Every part is digits, dots and dashes, so
    # the header needs no quoting.
    bounds = rng.as_json()
    span = "".join(f"_{b}" for b in (bounds["desde"], bounds["hasta"]) if b)
    filename = f"ventas_{full['rut']}{span}.pdf"
    return Response(
        content=build_pdf(
            _stored_rows(selected), taxpayer, _stored_rows(full), rng
        ),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/report/{rut}")
async def report(
    rut: str,
    rng: PeriodRange = Depends(period_range),
    user: User = Depends(current_user),
):
    """The same narrowed report as /report/{rut}.pdf, as JSON.

    Same body as /history/{rut} — so the React table renders it without
    branching — plus the `period` block echoing the range that was applied and
    `documents`, how many uploads the timeline draws on. It exists so the screen
    can preview exactly what the PDF will contain before the user downloads it.
    """
    full, selected = _timeline_or_404(rut, user.id, rng)
    return {
        "taxpayer": {"nombre": full["nombre"], "rut": full["rut"]},
        "document_id": full["document_id"],
        "source_file": full["source_file"],
        "extracted_at": full["extracted_at"],
        "documents": full["documents"],
        **_extract_payload(_stored_rows(selected), _stored_rows(full), rng),
    }


@app.get("/periods/{rut}")
async def periods(rut: str, user: User = Depends(current_user)):
    """The periods this user actually holds for a RUT: [{"year", "month"}, ...].

    Calendar coverage, not figures. The period picker needs to know which months
    exist before it can offer them, and asking /report for that would transfer a
    whole timeline to answer a question about which months are there.

    Merged across every upload for the RUT, like /report — so the picker offers
    the union of the user's carpetas rather than the newest one's window.

    404 when the caller has nothing stored for the RUT, matching the other
    stored-document routes; a RUT another user uploaded reads the same way.
    """
    try:
        found = storage.declaration_periods(rut, user.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not found:
        raise HTTPException(
            status_code=404, detail=f"Sin extracciones guardadas para el RUT {rut}."
        )
    return {"rut": rut, "count": len(found), "periods": found}


@app.get("/history/{rut}")
async def history(rut: str, user: User = Depends(current_user)):
    """The caller's most recent stored extraction for a RUT, in /extract's shape.

    Same body as /extract plus the document's provenance, so the frontend renders
    a stored document with the same table component as a fresh upload.
    """
    doc = _latest_or_404(rut, user.id)
    return {
        "taxpayer": {"nombre": doc["nombre"], "rut": doc["rut"]},
        "document_id": doc["document_id"],
        "source_file": doc["source_file"],
        "extracted_at": doc["extracted_at"],
        **_extract_payload(_stored_rows(doc)),
    }


class ProfileUpdate(BaseModel):
    """The editable fields of a user account. Every field is optional: the client
    sends only what changed, and an omitted field is left alone.

    `nombre` lands in our `profile` table; `email` and `password` belong to
    Supabase Auth and go through the admin API. The minimum password length
    mirrors Supabase's own default so an obviously-too-short password is
    rejected here with a clear message instead of as an opaque upstream error.
    """

    nombre: str | None = None
    email: str | None = None
    password: str | None = Field(default=None, min_length=6)


@app.get("/me")
async def read_me(user: User = Depends(current_user)):
    """The caller's profile. `nombre` is null until they save one."""
    try:
        profile = storage.get_profile(user.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "id": user.id,
        "email": user.email,
        "nombre": (profile or {}).get("nombre"),
    }


@app.patch("/me")
async def update_me(changes: ProfileUpdate, user: User = Depends(current_user)):
    """Edit the caller's own profile and credentials.

    A user can only ever edit themselves: the id comes from the verified token,
    never from the request body, so there is no id to tamper with.

    Changing the email does not take effect immediately — Supabase sends a
    confirmation mail to the new address first — so the response flags that and
    the UI can say so rather than appearing to have silently failed.
    """
    credentials = {
        k: v
        for k, v in (("email", changes.email), ("password", changes.password))
        if v
    }
    try:
        if credentials:
            storage.update_auth_user(user.id, **credentials)
        if changes.nombre is not None:
            storage.upsert_profile(user.id, changes.nombre.strip() or None)
        profile = storage.get_profile(user.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        # A rejected email or password is the user's problem to fix, not a
        # server fault: report it as a 400 with the reason Supabase gave.
        log.warning("Rechazado el cambio de credenciales de %s: %s", user.id, exc)
        raise HTTPException(status_code=400, detail=f"No se pudo guardar: {exc}")

    return {
        "id": user.id,
        "email": user.email,
        "nombre": (profile or {}).get("nombre"),
        "email_confirmation_pending": bool(changes.email),
    }
