# project-iva

Extract monthly sales figures from Chilean tax PDFs (Carpeta Tributaria /
Formulario 29 / IVA declarations) and serve them as a table over HTTP.

Upload a tax PDF and get back one row per month with the F29 sales figure. All
amounts are reported in **thousands of pesos** with comma separators: each code
is rounded to the nearest thousand *before* the sales formula is applied, and the
result is rounded to the nearest thousand too (so `1,807,028,373` appears as
`1,807,028`).

## Components

| File               | Role                                                                                                             |
| ------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `api.py`           | FastAPI service — upload a PDF, get the monthly table (HTML / JSON / Markdown / PDF).                            |
| `extract_codes.py` | Extraction pipeline: PDF → text (pdfplumber) → F29 code values → monthly table. Importable and CLI-runnable. |
| `asd.py`           | Legacy first-draft script (superseded by `extract_codes.py`).                                                    |

The sales formula is `020 + 142 + 538 / 0,19 + 587`, applied per monthly
declaration (delimited by the `PERIODO` field). Missing codes count as 0.

## Setup

```bash
python3 -m pip install -r requirements.txt
```

Pins `pdfplumber`, `fpdf2`, `python-dotenv`, `fastapi`, `uvicorn`,
`python-multipart`. All are pure-Python or ship prebuilt wheels, so this installs
cleanly on current Python versions.

## Run the API

```bash
uvicorn api:app --reload
```

Then either open <http://127.0.0.1:8000/> in a browser and upload a PDF, or:

```bash
# JSON (amounts in thousands of pesos)
curl -F file=@"docs/CARPETA TRIBUTARIA FRUTAM.pdf" http://127.0.0.1:8000/extract

# Markdown table
curl -F file=@"docs/CARPETA TRIBUTARIA FRUTAM.pdf" http://127.0.0.1:8000/extract.md

# PDF (Nombre / Razón Social + RUT header, then a Período | Ventas table)
curl -F file=@"docs/CARPETA TRIBUTARIA FRUTAM.pdf" http://127.0.0.1:8000/extract.pdf -o ventas_por_mes.pdf
```

Interactive API docs are at <http://127.0.0.1:8000/docs>.

### Endpoints

| Method | Path          | Returns                                             |
| ------ | ------------- | --------------------------------------------------- |
| `GET`  | `/`           | HTML upload form + rendered results table           |
| `POST` | `/`           | HTML results table (browser form submit)            |
| `POST` | `/extract`    | JSON: `{"unit": "miles de pesos", "months": [...]}` |
| `POST` | `/extract.md` | Markdown table (`text/markdown`)                    |
| `POST` | `/extract.pdf`| PDF download: taxpayer header (Nombre/Razón Social + RUT) + Período\|Ventas table |

## Run the extractor as a CLI (peso-level, not rounded)

```bash
python3 extract_codes.py <file.pdf>      # or set PDF_FILE in .env
```

Writes `output.md` (the intermediate extracted text) and `ventas_por_mes.md`
(the report, in full pesos).

## Configuration

- `.env` (gitignored) holds `PDF_FILE`, the default PDF path for the CLI.
