# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Extracts monthly sales figures from Chilean tax/IVA PDF documents (Carpeta
Tributaria / Formulario 29) and serves them as a table over HTTP, behind a
React web app with per-user accounts. Source PDFs live in `docs/` (e.g. tax
folders / IVA declarations for "Frutam").

> **Note for Claude:** You do not have access to the contents of the `docs/`
> directory. Do not assume the structure or values of those PDFs — ask the user
> for a converted sample when the table format matters. `outputs/output.md` is a real text extraction of the sample PDF and is safe to use for testing
> the parsing/formatting logic.

## Architecture

The pipeline is: **PDF → text → F29 code values → per-month sales table.**

Repository layout:

```
apps/api/     FastAPI service + extraction pipeline (Python)
apps/web/     React + TypeScript front end (Vite)
docs/         source tax PDFs (inputs; not readable by Claude)
outputs/      generated Markdown from the CLI (gitignored)
scripts/      run-api.mjs, the launcher Turborepo uses for the API
```

| File | Role |
|---|---|
| `apps/api/extract_codes.py` | The real logic. `pdf_to_text()` (pdfplumber), regex `extract_codes()`, `split_by_period()`, `monthly_rows()` (structured per-month data), `extract_taxpayer()` (Nombre/Razón Social + RUT from the header), `build_monthly_table()` (Markdown report). Importable and CLI-runnable. |
| `apps/api/api.py` | FastAPI HTTP layer. Reuses `extract_codes` — stages the upload in a temp file, calls `monthly_rows()`, and applies the round-to-thousand presentation. Serves JSON / Markdown and a `/extract.pdf` download (`build_pdf()` via **fpdf2**, taxpayer header + Período\|Ventas table), plus `/me` and `/history/{rut}`. Adds no extraction logic of its own, and renders **no HTML** — the UI is `apps/web/`. |
| `apps/api/auth.py` | Verifies the Supabase access token on every request (`current_user` dependency). Never issues tokens and never sees a password. |
| `apps/api/storage.py` | Supabase persistence: extractions (scoped by `document.user_id`) and the `profile` table. The only module that talks to Supabase. |
| `apps/web/` | React + TypeScript (Vite) front end — the app users actually see. Login, drag-and-drop extraction, profile editing. |
| `apps/api/paths.py` | Repo-root-relative locations (`.env`, `docs/`, `outputs/`), anchored on `__file__` rather than the cwd. |

### Auth and data ownership

Supabase Auth owns identity. `apps/web/` signs in with `supabase-js` and sends the
access token as `Authorization: Bearer …`; `auth.current_user` verifies it
against the project's JWKS endpoint (the configured project signs with **ES256**;
an HS256 branch using `SUPABASE_JWT_SECRET` exists for legacy projects). Audience
and issuer are checked, not just the signature.

**Every endpoint is authenticated**, and every read is scoped to the caller:
uploads record `document.user_id`, and `/ruts` and `/history/{rut}` filter on it.
Adding a new data endpoint without `Depends(current_user)` and without that
filter would leak one user's tax documents to another.

The **service_role** key (`SUPABASE_KEY`) stays server-side — it bypasses RLS.
The browser only ever gets the **anon** key (`VITE_SUPABASE_ANON_KEY`).

### Keeping the frontend table honest

`apps/web/src/lib/table.ts` recomputes the things the `/extract` payload does not
carry — the Ene–Dic blank padding (`fill_year_months`), the **Total** row and
its deficit flag (`year_is_complete`), and the **Promedio** row
(`_YearAggregator`). Those rules are duplicated from Python on purpose, so the
screen, the Markdown report and the PDF agree. Two consequences when editing:

- Change one side and you must change the other, or the same document reads
  differently in the browser and in the downloaded PDF.
- `apps/web/src/lib/format.ts` has `roundHalfEven`, because Python's `round()` breaks
  ties toward even and JavaScript's `Math.round` breaks them upward. Use it for
  anything mirroring a Python `round()`.

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
python3 -m pip install -r apps/api/requirements.txt
```

Turborepo drives both halves from the repo root:

```bash
npm install                   # once; installs both workspaces
cp apps/web/.env.example apps/web/.env.local   # Supabase URL + anon key

npm run dev                   # uvicorn (:8000) AND Vite (:5173) together
npm run build                 # typecheck + bundle the front end
npm run typecheck
```

Single halves, when you only want one: `npm run api` / `npm run web`. Running
`cd apps/api && uvicorn api:app --reload`, or `cd apps/web && npm run dev`, still works
identically — Turborepo is a launcher here, not a build step the code depends on.

### Turborepo layout

- `turbo.json` — `dev` is `persistent` + uncached; `build` depends on
  `typecheck`, outputs `dist/**`, and lists `.env`/`.env.local` in `inputs`
  because Vite inlines `VITE_*` values into the bundle. Drop those inputs and a
  cached build could be replayed with the wrong Supabase project baked in.
- Workspaces are `apps/*` — `iva-api` and `iva-web`. `apps/api/package.json`
  carries only the turbo tasks; the Python is the real content of that
  directory.
- `scripts/run-api.mjs` — starts uvicorn with **`apps/api`** as cwd (so
  `api:app` and the flat imports between the service's modules resolve) and
  probes `python3` / `python` / `python.exe` / `py` for one that can import
  uvicorn. Set `PYTHON` to override.

### Paths are anchored on `__file__`, never the cwd

uvicorn starts in `apps/api`, but `.env`, `docs/` and `outputs/` live at the
repo root, and the CLI can be launched from anywhere. `apps/api/paths.py`
resolves all of them from `__file__`, so nothing depends on the working
directory. Use `paths.from_root()` for anything a user might write as a relative
path in `.env` — that is what keeps `PDF_FILE=docs/CARPETA ....pdf` working.
Never add a bare `open("output.md", ...)` back: it would land wherever the
process happened to start.

### Run npm from ONE side: Windows

This checkout lives on `/mnt/c` and is reachable from both Windows and WSL. Pick
one and stay there — **Windows**, because that is where the Python dependencies
are installed (`python -m uvicorn` resolves there; WSL's `python3` has no
uvicorn).

Running `npm install` from WSL and then from Windows (or vice versa) against the
same `node_modules` produces a flood of `npm warn cleanup ... EACCES` and swaps
every platform-specific binary. `@esbuild/*` and `@rollup/rollup-*` ship
per-platform builds, so the tree ends up native to whichever side ran last and
unusable from the other. The install still "succeeds"; it is the *next* command
that fails.

If it happens, do not pick the leftovers out by hand — wipe and reinstall once:

```bash
rm -rf node_modules apps/web/node_modules && npm install    # from Windows
```

Testing from WSL: uvicorn started this way is a Windows process, so a WSL-side
`curl 127.0.0.1:8000` cannot reach it — that is the WSL↔Windows loopback
boundary, not a broken server. Use `curl.exe`. (Vite is reachable either way,
since Windows forwards localhost inbound to WSL but not outbound.)

Run the extractor as a CLI (writes into `outputs/`):

```bash
python3 apps/api/extract_codes.py <file.pdf>      # or set PDF_FILE in .env
```

## Configuration

- `.env` (gitignored) holds `PDF_FILE` (the default PDF path for the CLI),
  `SUPABASE_URL` and `SUPABASE_KEY` (service_role). Loaded via `python-dotenv`.
  Optional: `ALLOWED_ORIGINS` (comma-separated CORS origins, defaults to the
  Vite dev server) and `SUPABASE_JWT_SECRET` (only for legacy HS256 projects).
- `apps/web/.env.local` (gitignored, template in `apps/web/.env.example`) holds
  `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` and `VITE_API_URL`. Everything
  in it ships to the browser — anon key only.
- `SUPABASE_URL` in this project is stored with a `/rest/v1/` suffix.
  `storage.supabase_url()` normalises it; use that rather than reading the env
  var directly, or the JWKS URL and the token issuer check will be wrong.
- `schema.sql` is idempotent (`if not exists` / `add column if not exists`) —
  re-run it in the Supabase SQL editor after pulling schema changes.
