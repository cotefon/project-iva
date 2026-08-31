# project-iva

Extract monthly sales figures from Chilean tax PDFs (Carpeta Tributaria /
Formulario 29 / IVA declarations) and serve them as a table over HTTP, behind a
React web app with per-user accounts.

Upload a tax PDF and get back one row per month with the F29 sales figure. All
amounts are reported in **thousands of pesos** with comma separators: each code
is rounded to the nearest thousand *before* the sales formula is applied, and the
result is rounded to the nearest thousand too (so `1,807,028,373` appears as
`1,807,028`).

## Components

| File               | Role                                                                                                             |
| ------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `apps/web/`        | React + TypeScript front end: login, drag-and-drop extraction, profile editing.                                  |
| `turbo.json`       | Turborepo tasks (`dev`, `build`, `typecheck`) across the front end and the API.                                  |

| `apps/api/api.py`  | FastAPI service — upload a PDF, get the monthly table (JSON / Markdown / PDF). Authenticated; renders no HTML.   |
| `apps/api/auth.py` | Verifies the Supabase access token on every request.                                                             |
| `apps/api/storage.py` | Supabase persistence: extractions (per user) and profiles.                                                       |
| `apps/api/extract_codes.py` | Extraction pipeline: PDF → text (pdfplumber) → F29 code values → monthly table. Importable and CLI-runnable. |

The sales formula is `020 + 142 + 538 / 0,19 + 587`, applied per monthly
declaration (delimited by the `PERIODO` field). Missing codes count as 0.

## Setup

```bash
python3 -m pip install -r apps/api/requirements.txt
```

Pins `pdfplumber`, `fpdf2`, `python-dotenv`, `fastapi`, `uvicorn`,
`python-multipart`, `supabase` and `PyJWT[crypto]`. All are pure-Python or ship
prebuilt wheels, so this installs cleanly on current Python versions.

Then create the database objects: run `apps/api/schema.sql` in the Supabase SQL editor
(Dashboard → SQL → New query). It is idempotent, so re-running it is safe.

Front end and task runner (Turborepo drives both halves from the repo root):

```bash
npm install                            # installs the web/ workspace too
cp apps/web/.env.example apps/web/.env.local     # Supabase URL + anon key, and the API URL
```

## Run everything

```bash
npm run dev
```

Starts the API on <http://127.0.0.1:8000> and the web app on
<http://localhost:5173> in one terminal. `npm run api` and `npm run web` start
just one half; `npm run build` and `npm run typecheck` cover the front end.

`scripts/run-api.mjs` finds the Python interpreter that actually has uvicorn
installed (handy on mixed WSL/Windows setups). Override it with `PYTHON=...`.

## Run the API on its own

```bash
cd apps/api && uvicorn api:app --reload    # or, from the root: npm run api
```

Every endpoint requires a signed-in caller, so `curl` needs a bearer token —
copy one from the browser devtools after logging in to the web app:

```bash
TOKEN=...   # Supabase access_token

# JSON (amounts in thousands of pesos)
curl -H "Authorization: Bearer $TOKEN" \
     -F file=@"docs/CARPETA TRIBUTARIA FRUTAM.pdf" http://127.0.0.1:8000/extract

# Markdown table
curl -H "Authorization: Bearer $TOKEN" \
     -F file=@"docs/CARPETA TRIBUTARIA FRUTAM.pdf" http://127.0.0.1:8000/extract.md

# PDF (Nombre / Razón Social + RUT header, then a Período | Ventas table)
curl -H "Authorization: Bearer $TOKEN" \
     -F file=@"docs/CARPETA TRIBUTARIA FRUTAM.pdf" http://127.0.0.1:8000/extract.pdf -o ventas_por_mes.pdf
```

Interactive API docs are at <http://127.0.0.1:8000/docs>.

## Run the web app on its own

With the API running:

```bash
cd apps/web && npm run dev        # or, from the root: npm run web
```

Sign up, then drag a tax PDF onto the drop zone: the monthly tables render on
screen and *Descargar PDF* produces the same report `/extract.pdf` returns.
Uploads are recorded against your account — one user never sees another's
documents.

To work with a company you already uploaded, find it under **Documentos
anteriores**. The search box matches the **RUT**, not the company name: puntos
and the guion are ignored, so `76.044.491-K`, `76044491` and even `044491` all
find the same company.

Opening one shows its whole timeline, merged across every carpeta you uploaded
for that RUT. The **Desde / Hasta** selectors then narrow it to the months you
want, offering only months actually stored — so a carpeta running Nov 2022 to
Sep 2024 offers Nov and Dic in 2022, and Ene to Sep in 2024. *Descargar PDF*
renders exactly the span on screen.

### Endpoints

All endpoints require `Authorization: Bearer <supabase access token>`.

| Method  | Path             | Returns                                             |
| ------- | ---------------- | --------------------------------------------------- |
| `GET`   | `/me`            | The caller's profile: `{"id", "email", "nombre"}`   |
| `PATCH` | `/me`            | Edit name, email or password                        |
| `GET`   | `/ruts`          | RUTs this user has uploaded; `?q=` filters by RUT (not by name) |
| `GET`   | `/periods/{rut}` | The `(year, month)` periods stored for a RUT        |
| `GET`   | `/history/{rut}` | The user's latest stored extraction for a RUT (JSON) |
| `GET`   | `/history/{rut}.pdf` | The same PDF report, rebuilt from the stored rows |
| `GET`   | `/report/{rut}`  | The RUT's merged timeline, narrowed to `?desde=&hasta=` (`AAAA-MM`) |
| `GET`   | `/report/{rut}.pdf` | That narrowed report as a PDF                    |
| `POST`  | `/extract`       | JSON: `{"unit": "miles de pesos", "months": [...]}` |
| `POST`  | `/extract.md`    | Markdown table (`text/markdown`)                    |
| `POST`  | `/extract.pdf`   | PDF download: taxpayer header (Nombre/Razón Social + RUT) + Período\|Ventas table |

## Run the extractor as a CLI (peso-level, not rounded)

```bash
python3 apps/api/extract_codes.py <file.pdf>      # or set PDF_FILE in .env
```

Writes `outputs/output.md` (the intermediate extracted text) and
`outputs/ventas_por_mes.md` (the report, in full pesos).

## Configuration

- `.env` (gitignored): `PDF_FILE` (default PDF path for the CLI), `SUPABASE_URL`,
  `SUPABASE_KEY` (service_role — server-side only). Optional: `ALLOWED_ORIGINS`
  for CORS, `SUPABASE_JWT_SECRET` for legacy HS256 projects.
- `web/.env.local` (gitignored, template in `web/.env.example`):
  `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_URL`. Anon key only —
  everything here ships to the browser.
