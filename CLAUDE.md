# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Extracts monthly sales figures from Chilean tax/IVA PDF documents (Carpeta
Tributaria / Formulario 29) and serves them as a table over HTTP. Source PDFs
live in `docs/` (e.g. tax folders / IVA declarations for "Frutam").

> **Note for Claude:** You do not have access to the contents of the `docs/`
> directory. Do not assume the structure or values of those PDFs — ask the user
> for a converted sample when the table format matters. `output.md` in the repo
> root is a real text extraction of the sample PDF and is safe to use for testing
> the parsing/formatting logic.

## Architecture

The pipeline is: **PDF → text → F29 code values → per-month sales table.**

| File | Role |
|---|---|
| `extract_codes.py` | The real logic. `pdf_to_text()` (pdfplumber), regex `extract_codes()`, `split_by_period()`, `monthly_rows()` (structured per-month data), `extract_taxpayer()` (Nombre/Razón Social + RUT from the header), `build_monthly_table()` (Markdown report). Importable and CLI-runnable. |
| `api.py` | FastAPI HTTP layer. Reuses `extract_codes` — stages the upload in a temp file, calls `monthly_rows()`, and applies the round-to-thousand presentation. Serves JSON / Markdown / HTML and a `/extract.pdf` download (`build_pdf()` via **fpdf2**, taxpayer header + Período\|Ventas table). Adds no extraction logic of its own. |
| `asd.py` | Legacy first-draft stub, superseded by `extract_codes.py`. Still broken (see below); left in place but not used by the app. Prefer `extract_codes.py`. |

### Key conventions

- The sales formula is `020 + 142 + 538 / 0,19 + 587` (`FORMULA_CODES`), applied
  per monthly declaration delimited by the `PERIODO` field. Missing codes = 0.
- `extract_codes.py` works in **full pesos** and formats with Chilean dots
  (`format_int`). The **API** presents everything in **thousands with comma
  separators** (`round_thousand` / `row_in_thousands` / `fmt`): each code is
  rounded to the nearest thousand *before* the formula runs, and the result is
  rounded to the nearest thousand too. Keep this rounding/formatting in the API
  layer, not in the extraction functions.
- `pdfplumber` and `dotenv` are imported **lazily** (inside the functions that
  use them) so the parsing/formatting helpers — and `api.py` — import and test
  cleanly without those packages installed. Preserve this when editing.
- Extraction was originally built on `markitdown`, swapped to `pdfplumber`
  because markitdown's ML deps (magika, onnxruntime) fail to install on recent
  Python (e.g. 3.14). pdfplumber is pure-Python and format-appropriate for these
  text-based tax PDFs. Only `pdf_to_text()` knows about the PDF library; the
  regex pipeline is source-agnostic.

## Running

Install deps (there is now a manifest):

```bash
python3 -m pip install -r requirements.txt
```

Serve the API:

```bash
uvicorn api:app --reload      # browser UI at /, JSON at /extract, docs at /docs
```

Run the extractor as a CLI (writes `output.md` and `ventas_por_mes.md`):

```bash
python3 extract_codes.py <file.pdf>      # or set PDF_FILE in .env
```

## Configuration

- `.env` (gitignored) holds `PDF_FILE`, the default PDF path for the CLI.
  Loaded via `python-dotenv`.

## Known issues in `asd.py` (legacy stub)

`asd.py` predates `extract_codes.py` and does not run correctly. It is no longer
part of the app; use `extract_codes.py` instead. If you must fix `asd.py`:
- `os.getenv("PDF_FILE")` (currently references an undefined name / wrong arg).
- Pass the file path to `md.convert(...)`, not an empty string.
- Write `result.text_content`, not `str(result)`.
